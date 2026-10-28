"""The Devin importer, against a mock of the API: idempotent, ordered, safe."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from flight_recorder.chain import verify_file
from flight_recorder.cli import main
from flight_recorder.importers.devin import DevinClient, DevinError, Importer, State


class FakeDevin:
    """A stand-in for the Devin v1 API, holding sessions that can change."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.status = 200

    def add(self, sid: str, status: str = "working", messages: int = 2, **extra: Any) -> None:
        self.sessions[sid] = {
            "session_id": sid,
            "status_enum": status,
            "title": f"task {sid}",
            "tags": ["team-a"],
            "updated_at": "2026-10-28T10:00:00Z",
            "messages": [],
            **extra,
        }
        for _ in range(messages):
            self.say(sid, "devin_message", "working on it")

    def say(self, sid: str, kind: str, text: str) -> None:
        messages = self.sessions[sid]["messages"]
        messages.append(
            {
                "event_id": f"{sid}-e{len(messages) + 1}",
                "type": kind,
                "message": text,
                "timestamp": f"2026-10-28T10:00:{len(messages):02d}Z",
                "origin": "web",
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        if request.url.path == "/v1/sessions":
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            listed = [
                {k: v for k, v in s.items() if k != "messages"} for s in self.sessions.values()
            ]
            return httpx.Response(200, json={"sessions": listed[offset : offset + limit]})
        sid = unquote(request.url.raw_path.decode().rsplit("/", 1)[-1])
        if sid in self.sessions:
            return httpx.Response(200, json=self.sessions[sid])
        return httpx.Response(404, text="no such session")


@pytest.fixture
def devin() -> FakeDevin:
    return FakeDevin()


Make = Callable[..., Importer]


@pytest.fixture
def make(devin: FakeDevin, tmp_path: Path) -> Iterator[Make]:
    """Build importers the way separate runs would, closing each one after the test."""
    made: list[Importer] = []

    def build(**kw: Any) -> Importer:
        client = DevinClient("test-key", transport=httpx.MockTransport(devin.handler))
        importer = Importer(client, tmp_path / "logs", State(tmp_path / "state.db"), **kw)
        made.append(importer)
        return importer

    yield build
    for importer in made:
        importer.client.close()
        importer.state.close()


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_session_becomes_a_verified_log(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("s1")
    summary = make().run()
    assert (summary.sessions, summary.messages, summary.status_changes) == (1, 2, 1)
    log = tmp_path / "logs" / "devin-s1.ndjson"
    assert verify_file(log).valid
    actions = [e["event"]["action"] for e in _entries(log)]
    assert actions == ["session.snapshot", "session.status", "message", "message"]
    message = _entries(log)[2]
    assert message["@timestamp"] == "2026-10-28T10:00:00Z"  # when it happened
    assert message["agent"]["name"] == "devin" and message["session"]["id"] == "s1"
    assert message["flight"]["text"]["value"] == "working on it"


def test_the_key_is_sent_as_a_bearer_token(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("s1")
    make().run()
    assert devin.requests[0].headers["authorization"] == "Bearer test-key"


def test_a_second_run_records_only_what_is_new(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.add("s1")
    make().run()
    again = make().run()  # a fresh process: state comes from disk
    assert (again.messages, again.status_changes, again.snapshots) == (0, 0, 0)

    devin.say("s1", "user_message", "please also update the docs")
    devin.sessions["s1"]["status_enum"] = "blocked"
    devin.sessions["s1"]["updated_at"] = "2026-10-28T10:05:00Z"
    third = make().run()
    assert (third.messages, third.status_changes) == (1, 1)

    log = tmp_path / "logs" / "devin-s1.ndjson"
    assert verify_file(log).valid  # the chain was extended, not forked
    status = [e for e in _entries(log) if e["event"]["action"] == "session.status"]
    assert [(s["flight"]["previous_status"], s["flight"]["status"]) for s in status] == [
        (None, "working"),
        ("working", "blocked"),
    ]


def test_every_page_of_sessions_is_read(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    for i in range(130):
        devin.add(f"s{i:03d}", messages=0)
    assert make().run().sessions == 130


def test_unknown_session_fields_are_kept_in_the_snapshot(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    # Whatever the API adds (usage, ACUs, PR links) is recorded without code changes.
    devin.add("s1", messages=0, acus_consumed=3.5, pull_request={"url": "https://x.example/pr/1"})
    make().run()
    snapshot = _entries(tmp_path / "logs" / "devin-s1.ndjson")[0]
    assert snapshot["flight"]["session"]["value"]["acus_consumed"] == 3.5


def test_secrets_in_messages_are_redacted(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("s1", messages=0)
    devin.say("s1", "devin_message", "export GITHUB_TOKEN=ghp_" + "q" * 36)
    make().run()
    text = (tmp_path / "logs" / "devin-s1.ndjson").read_text(encoding="utf-8")
    assert "ghp_" + "q" * 36 not in text and "REDACTED" in text


def test_a_hostile_session_id_cannot_escape_the_log_directory(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.add("../../escape", messages=1)
    importer = make()
    importer.run()
    # The id was sent encoded, as one path segment, not as "../.." in the URL.
    detail = [r for r in devin.requests if r.url.path != "/v1/sessions"]
    assert detail and b"..%2F..%2Fescape" in detail[0].url.raw_path
    logs = list((tmp_path / "logs").glob("*.ndjson"))
    assert [p.name for p in logs] == ["devin-escape.ndjson"]
    assert not (tmp_path.parent / "escape.ndjson").exists()


def test_dry_run_writes_nothing(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("s1")
    summary = make(dry_run=True).run()
    assert len(summary.planned) == 4  # snapshot, status, two messages
    assert not (tmp_path / "logs").exists()
    # And nothing was marked as seen: a real run afterwards records everything.
    assert make().run().messages == 2


def test_a_rejected_key_is_reported_plainly(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.status = 401
    with pytest.raises(DevinError, match="DEVIN_API_KEY"):
        make().run()


def test_the_cli_requires_the_key_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    assert main(["import", "devin", "--log-dir", str(tmp_path)]) == 2
    assert "DEVIN_API_KEY" in capsys.readouterr().err
