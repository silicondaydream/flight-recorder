"""Exact events through MCP, shipping logs to Watchdog, and approving from Teams."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from devin_watchdog import tools
from devin_watchdog.actions import DevinActions, Teams
from devin_watchdog.approvals import Signer
from devin_watchdog.engine import Settings, State, Watchdog
from devin_watchdog.fakedevin import FakeDevin
from devin_watchdog.ingest import IngestStore
from devin_watchdog.public import serve_public
from flight_recorder.chain import Recorder, verify_file
from flight_recorder.reader import entries

T0 = 1_793_000_000.0
HEX = "5f0c1e2a9b7d4c6e8a1f3b2d"
SID = f"devin-{HEX}"


def stamp(at: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- the tools server -------------------------------------------------------------


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    reply = tools.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert reply is not None
    result: dict[str, Any] = reply["result"]
    return result


def test_tools_are_listed_and_notifications_get_no_reply() -> None:
    reply = tools.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    names = {t["name"] for t in reply["result"]["tools"]}
    assert names == {"run_command", "edit_file", "write_file", "read_file", "link_session"}
    assert tools.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_a_failing_command_is_an_mcp_error_with_its_exit_code(tmp_path: Path) -> None:
    ok = _call("run_command", command="echo hi", cwd=str(tmp_path))
    assert not ok["isError"] and ok["content"][0]["text"] == "exit code 0\nhi\n"
    bad = _call("run_command", command="echo boom >&2; exit 3", cwd=str(tmp_path))
    assert bad["isError"] and bad["content"][0]["text"].startswith("exit code 3\n")


def test_edit_file_needs_exactly_one_match(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\nx = 1\n", encoding="utf-8")
    assert _call("edit_file", path=str(path), old_text="x = 1", new_text="x = 2")["isError"]
    path.write_text("x = 1\n", encoding="utf-8")
    assert not _call("edit_file", path=str(path), old_text="x = 1", new_text="x = 2")["isError"]
    assert path.read_text(encoding="utf-8") == "x = 2\n"


def test_session_links_are_parsed() -> None:
    assert tools.devin_session_id(f"https://app.devin.ai/sessions/{HEX}") == SID
    assert tools.devin_session_id(f"https://app.devin.ai/sessions/{HEX}/") == SID
    assert tools.devin_session_id(SID) == SID
    assert tools.devin_session_id("https://evil.example/../x") == ""
    assert _call("link_session", session="not a link")["isError"]


def test_the_real_proxy_records_exact_outcomes(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "flight_recorder.cli", "proxy", "--log-dir", str(logs),
            "--name", "watchdog-tools", "--", sys.executable, "-m", "devin_watchdog.cli", "tools",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("WATCHDOG_INGEST")},
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    for i, (name, args) in enumerate(
        [
            ("link_session", {"session": f"https://app.devin.ai/sessions/{HEX}"}),
            ("run_command", {"command": "exit 1"}),
        ],
        start=1,
    ):
        call = {"name": name, "arguments": args}
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": call}) + "\n"
        )
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == i
    proc.stdin.close()
    proc.wait(timeout=10)
    proc.stdout.close()
    results = [e for e in entries(logs) if e["event"]["action"] == "tool.result"]
    assert [(e["flight"]["tool"], e["event"]["outcome"]) for e in results] == [
        ("link_session", "success"),
        ("run_command", "failure"),
    ]


# -- ingest -----------------------------------------------------------------------


def _proxy_log(path: Path, n: int, sid: str = "mcp-1") -> list[dict[str, Any]]:
    rec = Recorder(path, session_id=sid, agent="mcp-proxy")
    for i in range(n):
        rec.append("tool.call", flight={"tool": "run_command", "request_id": str(i)})
    rec.close()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_ingest_appends_the_chain_skips_resends_and_refuses_gaps(tmp_path: Path) -> None:
    sent = _proxy_log(tmp_path / "vm.ndjson", 5)
    store = IngestStore(tmp_path / "logs")
    assert store.add(sent[:2]).accepted == 2
    again = store.add(sent[:3])  # a restarted shipper resends from the start
    assert (again.accepted, again.skipped) == (1, 2)
    gap = store.add(sent[4:])
    assert gap.error and gap.expected == 4
    assert store.add(sent[3:]).accepted == 2
    shipped = store.path("mcp-1")
    assert verify_file(shipped).valid and verify_file(shipped).entries == 5


def test_ingest_refuses_an_altered_entry(tmp_path: Path) -> None:
    sent = _proxy_log(tmp_path / "vm.ndjson", 2)
    sent[1]["flight"]["tool"] = "something_else"
    store = IngestStore(tmp_path / "logs")
    result = store.add(sent)
    assert result.accepted == 1 and "contents changed" in result.error


def test_ingest_keeps_sessions_apart_and_ids_safe(tmp_path: Path) -> None:
    store = IngestStore(tmp_path / "logs")
    store.add(_proxy_log(tmp_path / "a.ndjson", 1, sid="../../escape"))
    assert [p.name for p in (tmp_path / "logs").iterdir()] == ["mcp-escape.ndjson"]


# -- the public listener ------------------------------------------------------------


@pytest.fixture
def public(tmp_path: Path) -> Iterator[tuple[str, Watchdog, FakeDevin, Signer]]:
    fake = FakeDevin()
    fake.add(SID, title="Fix the API client")
    signer = Signer("k3y", "https://watchdog.example")
    engine = Watchdog(
        Settings(dry_run=False),
        State(tmp_path / "w.db"),
        devin=DevinActions("cog_t", transport=fake.transport()),
        teams=Teams("https://teams.example/teams", transport=fake.transport()),
        signer=signer,
    )
    server = serve_public(
        engine, IngestStore(tmp_path / "logs"), signer, "127.0.0.1", 0, ingest_token="ingest-t"
    )
    yield f"http://127.0.0.1:{server.server_address[1]}", engine, fake, signer
    server.shutdown()
    server.server_close()
    engine.close()


def _request(
    url: str, data: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _pending(engine: Watchdog) -> None:
    ladder = engine.state.get(SID)
    ladder.pending, ladder.rung = True, 3
    engine.state.put(SID, ladder)


def _local(link: str, base: str) -> str:
    return base + link.removeprefix("https://watchdog.example")


def test_opening_an_approval_link_changes_nothing(public: Any) -> None:
    base, engine, _, signer = public
    _pending(engine)
    link = _local(signer.link("approve", SID, int(time.time()) + 600), base)
    code, page = _request(link)
    assert code == 200 and "<form" in page and "Terminate?" in page
    assert engine.state.get(SID).approved_at == 0.0  # a GET never acts: link previews fetch it


def test_confirming_approves_and_a_tampered_link_does_not(public: Any) -> None:
    base, engine, _, signer = public
    _pending(engine)
    expires = int(time.time()) + 600
    form = {
        "decision": "approve",
        "session": SID,
        "expires": str(expires),
        "sig": signer.link("approve", SID, expires).rsplit("sig=", 1)[1],
    }
    forged = {**form, "decision": "approve", "session": "devin-someone-else"}
    assert _request(f"{base}/approve", urllib.parse.urlencode(forged).encode())[0] == 403
    code, page = _request(f"{base}/approve", urllib.parse.urlencode(form).encode())
    assert code == 200 and "Terminate approved" in page
    assert engine.state.get(SID).approved_by.startswith("approval link")


def test_expired_links_are_refused(public: Any) -> None:
    base, engine, _, signer = public
    _pending(engine)
    code, page = _request(_local(signer.link("approve", SID, int(time.time()) - 1), base))
    assert code == 403 and "expired" in page


def test_keep_running_declines_and_holds_terminate(public: Any) -> None:
    base, engine, _, signer = public
    _pending(engine)
    expires = int(time.time()) + 600
    sig = signer.link("decline", SID, expires).rsplit("sig=", 1)[1]
    body = urllib.parse.urlencode(
        {"decision": "decline", "session": SID, "expires": expires, "sig": sig}
    ).encode()
    assert _request(f"{base}/approve", body)[0] == 200
    ladder = engine.state.get(SID)
    assert not ladder.pending and ladder.declined_by.startswith("approval link")


def test_ingest_needs_its_token(public: Any) -> None:
    base, *_ = public
    body = json.dumps({"entries": []}).encode()
    assert _request(f"{base}/api/ingest", body, {"Authorization": "Bearer nope"})[0] == 401
    assert _request(f"{base}/api/ingest", body, {"Authorization": "Bearer ingest-t"})[0] == 200


def test_the_public_listener_does_not_serve_the_dashboard(public: Any) -> None:
    base, *_ = public
    assert _request(f"{base}/api/state")[0] == 404
    assert _request(f"{base}/")[0] == 404


def test_the_shipper_delivers_a_growing_log_and_survives_a_restart(
    public: Any, tmp_path: Path
) -> None:
    base, *_ = public
    vm = tmp_path / "vm"
    vm.mkdir()
    rec = Recorder(vm / "a.ndjson", session_id="mcp-7", agent="mcp-proxy")
    rec.append("tool.call", flight={"tool": "run_command", "request_id": "1"})
    shipper = tools.Shipper(vm, f"{base}/api/ingest", "ingest-t", poll=0.05).start()
    rec.append("tool.result", outcome="failure", flight={"tool": "run_command", "request_id": "1"})
    shipped = tmp_path / "logs" / "mcp-mcp-7.ndjson"
    _wait(lambda: shipped.exists() and verify_file(shipped).entries == 2)
    shipper.close()
    rec.append("tool.call", flight={"tool": "run_command", "request_id": "2"})
    rec.close()
    again = tools.Shipper(vm, f"{base}/api/ingest", "ingest-t", poll=0.05).start()  # from the top
    _wait(lambda: verify_file(shipped).entries == 3)
    again.close()
    result = verify_file(shipped)
    assert result.valid and result.entries == 3
    assert result.head == verify_file(vm / "a.ndjson").head  # the same chain, end to end


def _wait(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


# -- linking and the engine ---------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def _devin_log(log_dir: Path) -> Recorder:
    rec = Recorder(log_dir / f"{SID}.ndjson", session_id=SID, agent="devin")
    meta = {
        "session_id": SID,
        "title": "Fix the API client",
        "url": f"https://app.devin.ai/sessions/{HEX}",
        "status": "running",
        "status_detail": "working",
        "tags": [],
        "acus_consumed": 1.0,
        "pull_requests": [],
    }
    rec.append("session.snapshot", timestamp=stamp(T0), flight={"session": {"value": meta}})
    return rec


def _mcp_log(log_dir: Path, name: str, *, link: bool, failures: int, start: float) -> None:
    rec = Recorder(log_dir / f"{name}.ndjson", session_id=name, agent="mcp-proxy")
    calls: list[tuple[str, dict[str, Any], str, Any]] = []
    if link:
        url = f"https://app.devin.ai/sessions/{HEX}"
        calls.append(("link_session", {"session": url}, "success", {"content": []}))
    error = {
        "content": [
            {
                "type": "text",
                "text": "exit code 1\nModuleNotFoundError: No module named 'api_client'",
            }
        ],
        "isError": True,
    }
    calls += [
        ("run_command", {"command": "python3 -m unittest tests.test_client"}, "failure", error)
    ] * failures
    for i, (tool, args, outcome, result) in enumerate(calls, start=1):
        at = stamp(start + i)
        rec.append(
            "tool.call",
            timestamp=at,
            flight={"tool": tool, "request_id": "1", "arguments": {"value": args}},
        )
        rec.append(
            "tool.result",
            outcome=outcome,
            timestamp=at,
            flight={"tool": tool, "request_id": "1", "result": {"value": result}},
        )
    rec.close()


def _engine(tmp_path: Path, clock: Clock, fake: FakeDevin, **kw: Any) -> Watchdog:
    return Watchdog(
        Settings(dry_run=False, cooldown=60.0),
        State(tmp_path / "w.db"),
        devin=DevinActions("cog_t", transport=fake.transport()),
        teams=Teams("https://teams.example/teams", transport=fake.transport()),
        clock=clock,
        **kw,
    )


def test_a_linked_mcp_log_drives_the_devin_session_with_exact_findings(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    clock, fake = Clock(), FakeDevin()
    fake.add(SID)
    devin = _devin_log(logs)
    # Request ids repeat across MCP sessions: both use "1" for every call.
    _mcp_log(logs, "mcp-a", link=True, failures=2, start=T0 + 10)
    _mcp_log(logs, "mcp-b", link=True, failures=2, start=T0 + 20)
    engine = _engine(tmp_path, clock, fake)
    for entry in entries(logs):
        engine.observe(entry)
    clock.now = T0 + 40
    engine.tick()
    assert set(engine.views) == {SID}  # both MCP logs folded into the Devin session
    findings = engine.findings[SID]
    assert "repeated_failing_commands" in [f.rule for f in findings]
    assert not any(f.heuristic for f in findings)  # exact, from exit codes
    assert "error: no module named <s>" in engine.findings[SID][1].summary
    assert [e.kind for e in engine.events][:2] == ["linked", "linked"]
    devin.close()
    engine.close()


def test_an_unlinked_mcp_log_only_ever_alerts(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    clock, fake = Clock(), FakeDevin()
    _mcp_log(logs, "mcp-a", link=False, failures=4, start=T0)
    engine = _engine(tmp_path, clock, fake)
    for entry in entries(logs):
        engine.observe(entry)
    for t in (10, 100, 200, 300):
        clock.now = T0 + t
        engine.tick()
    assert [e.rung for e in engine.events if e.kind == "action"] == ["log", "alert"]
    assert fake.sent == []
    engine.close()


def test_the_approval_card_carries_signed_buttons(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    clock, fake = Clock(), FakeDevin()
    fake.add(SID)
    devin = _devin_log(logs)
    _mcp_log(logs, "mcp-a", link=True, failures=4, start=T0 + 1)
    signer = Signer("k3y", "https://watchdog.example")
    engine = _engine(tmp_path, clock, fake, signer=signer)
    for entry in entries(logs):
        engine.observe(entry)
    ladder = engine.state.get(SID)
    ladder.rung, ladder.last_action = 3, T0  # already asleep, and still stuck
    engine.state.put(SID, ladder)
    clock.now = T0 + 100
    engine.tick()
    card = fake.teams[-1]["attachments"][0]["content"]
    buttons = {a["title"]: a["url"] for a in card["actions"]}
    assert set(buttons) == {"Approve terminate", "Keep running", "Open session"}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(buttons["Approve terminate"]).query)
    assert signer.check("approve", SID, query["expires"][0], query["sig"][0], clock.now) == ""
    devin.close()
    engine.close()


def test_declined_terminate_is_held_until_the_session_recovers(tmp_path: Path) -> None:
    clock, fake = Clock(), FakeDevin()
    fake.add(SID)
    logs = tmp_path / "logs"
    devin = _devin_log(logs)
    _mcp_log(logs, "mcp-a", link=True, failures=4, start=T0 + 1)
    engine = _engine(tmp_path, clock, fake)
    for entry in entries(logs):
        engine.observe(entry)
    ladder = engine.state.get(SID)
    ladder.rung, ladder.last_action = 3, T0
    engine.state.put(SID, ladder)
    clock.now = T0 + 100
    engine.tick()
    assert engine.decline(SID, by="dana")
    clock.now = T0 + 400
    engine.tick()
    assert "declined by dana" in engine.held[SID]
    assert fake.sessions[SID]["status"] == "running"
    devin.close()
    engine.close()


def test_a_database_from_before_declining_is_upgraded(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute(
        "CREATE TABLE ladder (session_id TEXT PRIMARY KEY, rung INTEGER, last_action REAL, "
        "last_finding REAL, pending INTEGER, approved_at REAL, approved_by TEXT)"
    )
    db.execute("INSERT INTO ladder VALUES ('s', 2, 1.0, 1.0, 0, 0.0, '')")
    db.commit()
    db.close()
    state = State(tmp_path / "old.db")
    assert state.get("s").rung == 2 and state.get("s").declined_by == ""
    state.close()


def test_the_demo_runs_the_mcp_session_end_to_end(tmp_path: Path) -> None:
    from devin_watchdog import demo

    fake = FakeDevin()
    demo.Scenario(fake)
    store_dir = tmp_path / "logs"
    from devin_watchdog.public import serve_public as serve

    engine = Watchdog(Settings(), State(tmp_path / "w.db"))
    server = serve(engine, IngestStore(store_dir), None, "127.0.0.1", 0, ingest_token="t")
    agent = demo.McpAgent(
        tmp_path, f"http://127.0.0.1:{server.server_address[1]}/api/ingest", "t", fake
    )
    agent.start()
    agent.step()
    agent.close()
    server.shutdown()
    server.server_close()
    engine.close()
    shipped = list(store_dir.glob("mcp-*.ndjson"))
    assert len(shipped) == 1 and verify_file(shipped[0]).valid
    tools_called = [
        e["flight"]["tool"] for e in entries(shipped[0]) if e["event"]["action"] == "tool.call"
    ]
    assert tools_called == ["link_session", "write_file", "run_command"]
