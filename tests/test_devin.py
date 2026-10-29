"""The Devin importer, against a fake of the v3 API: idempotent, ordered, safe."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from devin_watchdog.fakedevin import FakeDevin
from flight_recorder.chain import verify_file
from flight_recorder.cli import main
from flight_recorder.importers.devin import DevinClient, DevinError, Importer, State, iso


class Clock:
    def __init__(self) -> None:
        self.now = 1_793_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def devin() -> FakeDevin:
    return FakeDevin(clock=Clock())


Make = Callable[..., Importer]


@pytest.fixture
def make(devin: FakeDevin, tmp_path: Path) -> Iterator[Make]:
    """Build importers the way separate runs would, closing each one after the test."""
    made: list[Importer] = []

    def build(org_id: str | None = None, **kw: Any) -> Importer:
        client = DevinClient("cog_test", org_id=org_id, transport=devin.transport())
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
    devin.add("devin-s1", messages=2)
    summary = make().run()
    assert (summary.sessions, summary.messages, summary.status_changes) == (1, 2, 1)
    log = tmp_path / "logs" / "devin-s1.ndjson"
    assert verify_file(log).valid
    actions = [e["event"]["action"] for e in _entries(log)]
    assert actions == ["session.snapshot", "message", "message", "session.status"]
    message = _entries(log)[1]
    assert message["@timestamp"] == iso(1_793_000_000)  # when it happened, not when read
    assert message["agent"]["name"] == "devin" and message["session"]["id"] == "devin-s1"
    assert message["flight"]["from"] == "devin"
    assert message["flight"]["text"]["value"] == "working on it"
    status = _entries(log)[3]["flight"]
    assert (status["status"], status["status_detail"]) == ("running", "working")


def test_the_org_comes_from_the_key_unless_given(devin: FakeDevin, make: Make) -> None:
    devin.add("devin-s1")
    make().run()
    assert devin.requests[0] == ("GET", "/v3/self")
    assert devin.requests[1] == ("GET", "/v3/organizations/org-demo/sessions")

    devin.requests.clear()
    make(org_id="org-demo").run()
    assert ("GET", "/v3/self") not in devin.requests


def test_a_second_run_records_only_what_is_new(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.add("devin-s1", messages=2)
    make().run()
    again = make().run()  # a fresh process: state comes from disk
    assert (again.messages, again.status_changes, again.snapshots) == (0, 0, 0)

    devin.say("devin-s1", "user", "please also update the docs")
    devin.update("devin-s1", status_detail="waiting_for_user")
    third = make().run()
    assert (third.messages, third.status_changes) == (1, 1)

    log = tmp_path / "logs" / "devin-s1.ndjson"
    assert verify_file(log).valid  # the chain was extended, not forked
    status = [e["flight"] for e in _entries(log) if e["event"]["action"] == "session.status"]
    assert [(s["previous_status_detail"], s["status_detail"]) for s in status] == [
        (None, "working"),
        ("working", "waiting_for_user"),
    ]


def test_later_polls_ask_only_for_what_changed(devin: FakeDevin, make: Make) -> None:
    devin.add("devin-s1", messages=1)
    make().run()
    devin.requests.clear()
    make().run()
    listed = [p for m, p in devin.requests if p.endswith("/sessions")]
    assert listed and not any(p.endswith("/messages") for _, p in devin.requests)


def test_every_page_of_sessions_and_messages_is_read(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.page = 7
    for i in range(20):
        devin.add(f"devin-s{i:02d}")
    for _ in range(16):
        devin.say("devin-s00", "devin", "step")
    summary = make().run()
    assert summary.sessions == 20 and summary.messages == 16


def test_usage_and_prs_are_kept_in_the_snapshot(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.add(
        "devin-s1",
        acus_consumed=3.5,
        pull_requests=[{"pr_url": "https://x.example/pr/1", "pr_state": "open"}],
    )
    make().run()
    snapshot = _entries(tmp_path / "logs" / "devin-s1.ndjson")[0]["flight"]["session"]["value"]
    assert snapshot["acus_consumed"] == 3.5
    assert snapshot["pull_requests"][0]["pr_state"] == "open"


def test_secrets_in_messages_are_redacted(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("devin-s1")
    devin.say("devin-s1", "devin", "export GITHUB_TOKEN=ghp_" + "q" * 36)
    make().run()
    text = (tmp_path / "logs" / "devin-s1.ndjson").read_text(encoding="utf-8")
    assert "ghp_" + "q" * 36 not in text and "REDACTED" in text


def test_a_hostile_session_id_cannot_escape_the_log_directory(
    devin: FakeDevin, tmp_path: Path, make: Make
) -> None:
    devin.add("../../escape", messages=1)
    make().run()
    logs = list((tmp_path / "logs").glob("*.ndjson"))
    assert [p.name for p in logs] == ["devin-escape.ndjson"]
    assert not (tmp_path.parent / "escape.ndjson").exists()


def test_a_hostile_session_id_is_one_url_segment() -> None:
    seen: list[bytes] = []
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={"items": []})

    client = DevinClient("k", org_id="org-x", transport=httpx.MockTransport(handler))
    client.messages("../../escape")
    client.close()
    assert seen == [b"/v3/organizations/org-x/sessions/..%2F..%2Fescape/messages?first=200"]


def test_devins_own_id_prefix_is_not_doubled(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("devin-0a1b2c", messages=1)
    devin.add("plain", messages=1)
    make().run()
    names = sorted(p.name for p in (tmp_path / "logs").glob("*.ndjson"))
    assert names == ["devin-0a1b2c.ndjson", "devin-plain.ndjson"]


def test_dry_run_writes_nothing(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.add("devin-s1", messages=2)
    summary = make(dry_run=True).run()
    assert len(summary.planned) == 4  # snapshot, status, two messages
    assert not (tmp_path / "logs").exists()
    # And nothing was marked as seen: a real run afterwards records everything.
    assert make().run().messages == 2


def test_a_rejected_key_is_reported_plainly(devin: FakeDevin, tmp_path: Path, make: Make) -> None:
    devin.status = 401
    with pytest.raises(DevinError, match="DEVIN_API_KEY"):
        make().run()


def test_epoch_timestamps_become_rfc3339() -> None:
    assert iso(1_793_000_000) == "2026-10-26T07:33:20.000Z"
    assert iso(1_793_000_000_123) == "2026-10-26T07:33:20.123Z"
    assert iso(None) is None


def test_the_cli_requires_the_key_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    assert main(["import", "devin", "--log-dir", str(tmp_path)]) == 2
    assert "DEVIN_API_KEY" in capsys.readouterr().err
