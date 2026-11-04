"""Devin hooks: the plugin's script, /api/hook, and the engine reading hook events."""

from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from helpers import config

from agent_watchdog import devin_hook, redact
from agent_watchdog.actions import DevinActions, Teams
from agent_watchdog.chain import Recorder, verify_file
from agent_watchdog.engine import State, Watchdog
from agent_watchdog.fakedevin import FakeDevin
from agent_watchdog.hooks import HookStore, outcome, plugin_files
from agent_watchdog.public import serve_public
from agent_watchdog.reader import entries

ROOT = Path(__file__).parent.parent
T0 = 1_793_000_000.0
DEVIN = "devin-0123456789abcdef0123456789abcdef"
HOOK = "3f8d1c2a-0b4e-4c55-9a1e-7d2f6c0e9b31"
FAIL = "Ran 5 tests\n\nFAILED (failures=2)\nAssertionError: Decimal('2.67') != Decimal('2.66')"


# -- the plugin stays in step with the package ------------------------------------------


def test_the_plugin_folder_is_what_the_package_exports() -> None:
    for name, text in plugin_files().items():
        assert (ROOT / "devin-plugin" / name).read_text(encoding="utf-8") == text, name


def test_the_hook_redacts_with_watchdogs_own_patterns() -> None:
    assert devin_hook.BUILTIN_PATTERNS == redact.BUILTIN_PATTERNS
    assert devin_hook.SENSITIVE_KEY.pattern == redact.SENSITIVE_KEY.pattern


# -- the hook script ---------------------------------------------------------------------


def run_hook(payload: dict[str, Any], env: dict[str, str], capsys: Any) -> str:
    stdin = io.BytesIO(json.dumps(payload).encode())
    assert devin_hook.main(stdin=stdin, environ=env) == 0
    return str(capsys.readouterr().out)


def post_tool(
    command: str = "python3 -m unittest", ok: bool = False, **extra: Any
) -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": HOOK,
        "tool_name": "exec",
        "tool_input": {"command": command},
        "tool_response": {"success": ok, "output": "" if ok else FAIL, "error": None},
        **extra,
    }


def test_secrets_are_removed_and_long_output_cut_before_it_leaves(
    tmp_path: Path, capsys: Any
) -> None:
    env = {"WATCHDOG_HOOK_DIR": str(tmp_path)}
    token = "ghp_" + "q" * 36
    payload = post_tool(f"curl -H 'Authorization: Bearer {token}' x")
    payload["tool_response"]["output"] = "x" * 50_000
    run_hook(payload, env, capsys)
    line = (tmp_path / "spool.jsonl").read_text(encoding="utf-8")
    assert token not in line and "REDACTED" in line
    event = json.loads(line)
    assert len(event["tool_response"]["output"]) < 21_000
    assert event["tool_response"]["output"].startswith("[30000 characters cut]")


def test_a_broken_payload_or_a_missing_watchdog_never_fails_the_hook(
    tmp_path: Path, capsys: Any
) -> None:
    env = {
        "WATCHDOG_HOOK_DIR": str(tmp_path),
        "WATCHDOG_HOOK_URL": "http://127.0.0.1:9/api/hook",  # nothing listens here
        "WATCHDOG_INGEST_TOKEN": "t",
    }
    assert devin_hook.main(stdin=io.BytesIO(b"not json"), environ=env) == 0
    started = time.monotonic()
    run_hook(post_tool(), env, capsys)
    run_hook(post_tool(), env, capsys)  # inside the backoff: no second attempt
    assert time.monotonic() - started < 3
    assert len((tmp_path / "spool.jsonl").read_text().splitlines()) == 2  # kept for later


def test_plain_http_is_refused_except_to_this_machine() -> None:
    assert devin_hook.target({"WATCHDOG_HOOK_URL": "http://wd.example.com/api/hook"}) == ""
    assert devin_hook.target({"WATCHDOG_HOOK_URL": "http://127.0.0.1:1/api/hook"}).endswith("/hook")
    public = {"WATCHDOG_PUBLIC_URL": "https://wd.example.com/"}
    assert devin_hook.target(public) == "https://wd.example.com/api/hook"


def test_the_devin_session_id_is_found_where_it_appears() -> None:
    find = devin_hook.devin_session
    assert find({"session_id": DEVIN}, {}) == DEVIN
    assert find({"session_id": HOOK}, {}) == ""
    link = f"https://app.devin.ai/sessions/{DEVIN.removeprefix('devin-')}"
    assert find({"session_id": HOOK}, {"SOMETHING": link}) == DEVIN


# -- the receiving end -------------------------------------------------------------------


def test_outcomes_come_from_exit_codes_first() -> None:
    assert outcome({"success": True, "output": "Process exited with code 1"}) == ("failure", 1)
    assert outcome({"success": False, "output": "exit code 0"}) == ("success", 0)
    assert outcome({"success": False, "output": "boom"}) == ("failure", None)
    assert outcome({"output": "?"}) == ("unknown", None)


def test_hook_events_become_a_verified_log_and_resends_are_skipped(tmp_path: Path) -> None:
    store = HookStore(tmp_path)
    events = [
        {**post_tool(), "id": "e1", "at": T0},
        {**post_tool(ok=True), "id": "e2", "at": T0 + 1},
    ]
    assert store.add(events) == (2, 0, [HOOK])
    assert store.add(events) == (0, 2, [])
    store.close()
    log = store.path(HOOK)
    assert verify_file(log).valid
    recorded = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["event"]["outcome"] for r in recorded] == ["failure", "success"]
    assert recorded[0]["agent"]["name"] == "devin-hook"
    with pytest.raises(ValueError, match="session_id"):
        store.add([{"hook_event_name": "Stop"}])


# -- the engine --------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class Rig:
    def __init__(self, tmp_path: Path, **values: Any) -> None:
        self.clock = Clock()
        self.fake = FakeDevin(clock=self.clock)
        self.fake.add(DEVIN, title="Make the pricing tests pass")
        self.logs = tmp_path / "logs"
        self.devin = Recorder(self.logs / f"{DEVIN}.ndjson", session_id=DEVIN, agent="devin")
        meta = {
            "session_id": DEVIN,
            "title": "Make the pricing tests pass",
            "status": "running",
            "status_detail": "working",
            "tags": [],
            "acus_consumed": 1.0,
            "pull_requests": [],
        }
        self.devin.append(
            "session.snapshot", timestamp=stamp(T0), activity={"session": {"value": meta}}
        )
        prompt = {"from": "user", "text": {"value": "Make `python3 -m unittest` pass."}}
        self.devin.append("message", timestamp=stamp(T0), activity=prompt)
        self.store = HookStore(self.logs)
        self.engine = Watchdog(
            config(**{"dry_run": False, "cooldown": 60.0, **values}),
            State(tmp_path / "w.db"),
            devin=DevinActions("cog_t", transport=self.fake.transport()),
            teams=Teams("https://teams.example/teams", transport=self.fake.transport()),
            clock=self.clock,
        )
        self.fed = 0
        self.n = 0

    def hook(self, **event: Any) -> None:
        self.n += 1
        self.store.add([{"session_id": HOOK, "id": f"e{self.n}", "at": self.clock.now, **event}])

    def failing_runs(self, n: int = 3, **extra: Any) -> None:
        for _ in range(n):
            self.hook(**{**post_tool(), **extra})

    def at(self, seconds: float) -> None:
        self.clock.now = T0 + seconds
        stream = list(entries(self.logs))
        for entry in stream[self.fed :]:
            self.engine.observe(entry)
        self.fed = len(stream)
        self.engine.tick()

    def rungs(self) -> list[str]:
        return [e.rung for e in self.engine.events if e.kind == "action"]

    def close(self) -> None:
        self.devin.close()
        self.store.close()
        self.engine.close()


def stamp(at: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Any]:
    made: list[Rig] = []

    def build(**values: Any) -> Rig:
        r = Rig(tmp_path / str(len(made)), **values)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


def test_a_hook_session_with_a_devin_id_is_linked_and_its_findings_are_exact(rig: Any) -> None:
    r: Rig = rig()
    r.failing_runs(devin_session=DEVIN)
    r.at(10)
    assert set(r.engine.views) == {DEVIN}
    findings = r.engine.findings[DEVIN]
    assert findings and not any(f.heuristic for f in findings)
    assert "repeated_failing_commands" in [f.rule for f in findings]
    linked = [e for e in r.engine.events if e.kind == "linked"]
    assert "session id" in linked[0].detail


def test_a_hook_session_without_one_is_linked_by_its_first_prompt(rig: Any) -> None:
    r: Rig = rig()
    r.hook(hook_event_name="UserPromptSubmit", prompt="Make  `python3 -m unittest`  pass.")
    r.failing_runs()
    r.at(10)
    assert set(r.engine.views) == {DEVIN}
    assert "first prompt" in next(e for e in r.engine.events if e.kind == "linked").detail
    assert r.engine.snapshot()["sessions"][0]["hook_sources"] == [HOOK]


def test_an_ambiguous_prompt_links_nothing_and_gets_alerts_only(rig: Any) -> None:
    r: Rig = rig()
    other = Recorder(r.logs / "devin-other.ndjson", session_id="devin-other", agent="devin")
    other.append(
        "message",
        timestamp=stamp(T0),
        activity={"from": "user", "text": {"value": "Make `python3 -m unittest` pass."}},
    )
    other.close()
    r.hook(hook_event_name="UserPromptSubmit", prompt="Make `python3 -m unittest` pass.")
    r.failing_runs()
    for t in (10, 80, 150):
        r.at(t)
    assert HOOK in r.engine.views  # not folded into either
    hook_actions = [e.rung for e in r.engine.events if e.session_id == HOOK and e.kind == "action"]
    assert hook_actions == ["log", "alert"] and r.fake.sent == []


def test_native_edits_count_for_edit_thrash(rig: Any) -> None:
    r: Rig = rig()
    patch = "*** Begin Patch\n*** Update File: pricing/money.py\n@@\n-a\n+b\n*** End Patch"
    for i in range(5):
        tool = "apply_patch" if i % 2 else "edit"
        args = {"patch": patch} if tool == "apply_patch" else {"file_path": "pricing/money.py"}
        r.hook(
            hook_event_name="PostToolUse",
            tool_name=tool,
            tool_input=args,
            tool_response={"success": True, "output": "ok"},
            devin_session=DEVIN,
        )
    r.at(10)
    assert "edit_thrash" in [f.rule for f in r.engine.findings[DEVIN]]


def test_a_nudge_can_go_through_the_hook_instead_of_the_chat(rig: Any) -> None:
    r: Rig = rig(**{"ladder.nudge_channel": "hook"})
    r.failing_runs(devin_session=DEVIN)
    for t in (10, 80, 150):
        r.at(t)
    assert r.rungs() == ["log", "alert", "nudge"]
    assert r.fake.sent == []  # no chat message
    notes = r.engine.take_hook_notes([HOOK])
    assert notes[HOOK].startswith("[Watchdog]")
    assert r.engine.take_hook_notes([HOOK]) == {}  # delivered once


def test_without_hooks_a_hook_nudge_falls_back_to_a_message(tmp_path: Path) -> None:
    clock, fake = Clock(), FakeDevin()
    fake.add(DEVIN)
    logs = tmp_path / "logs"
    rec = Recorder(logs / f"{DEVIN}.ndjson", session_id=DEVIN, agent="devin")
    meta = {"session_id": DEVIN, "status": "running", "status_detail": "working", "tags": []}
    rec.append("session.snapshot", timestamp=stamp(T0), activity={"session": {"value": meta}})
    for i in range(3):
        rec.append(
            "message",
            timestamp=stamp(T0 + i),
            activity={
                "from": "devin",
                "text": {"value": "Running `npm test`. It failed: Error: x"},
            },
        )
    rec.close()
    channel: dict[str, Any] = {"ladder.nudge_channel": "hook"}
    engine = Watchdog(
        config(dry_run=False, cooldown=60.0, **channel),
        State(tmp_path / "w.db"),
        devin=DevinActions("cog_t", transport=fake.transport()),
        clock=clock,
    )
    for entry in entries(logs):
        engine.observe(entry)
    for t in (10, 80, 150):
        clock.now = T0 + t
        engine.tick()
    assert fake.sent and fake.sent[0][1].startswith("[Watchdog]")
    engine.close()


def test_a_person_can_send_a_note_through_the_hook(rig: Any) -> None:
    r: Rig = rig()
    assert not r.engine.manual(DEVIN, "note", by="dana", text="hi")[0]  # no hooks yet
    r.failing_runs(n=1, devin_session=DEVIN)
    r.at(1)
    ok, _ = r.engine.manual(DEVIN, "note", by="dana", text="Check tests/ for a contradiction")
    assert ok and r.engine.take_hook_notes([HOOK])[HOOK].endswith("contradiction")


# -- end to end: the real script, the real endpoint ---------------------------------------


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[tuple[str, Watchdog, HookStore]]:
    engine = Watchdog(config(), State(tmp_path / "w.db"))
    store = HookStore(tmp_path / "logs")
    server = serve_public(engine, None, None, "127.0.0.1", 0, ingest_token="tok", hook_store=store)
    yield f"http://127.0.0.1:{server.server_address[1]}", engine, store
    server.shutdown()
    server.server_close()
    store.close()
    engine.close()


def test_the_endpoint_needs_the_token(listener: Any) -> None:
    base, *_ = listener
    request = urllib.request.Request(  # noqa: S310
        f"{base}/api/hook",
        data=b'{"events": []}',
        headers={"Content-Type": "application/json", "Authorization": "Bearer nope"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310
    assert err.value.code == 401
    err.value.close()


def test_the_script_delivers_in_order_and_hands_back_notes(
    listener: Any, tmp_path: Path, capsys: Any
) -> None:
    base, engine, store = listener
    env = {
        "WATCHDOG_HOOK_DIR": str(tmp_path / "spool"),
        "WATCHDOG_HOOK_URL": f"{base}/api/hook",
        "WATCHDOG_INGEST_TOKEN": "tok",
    }
    run_hook(post_tool(devin=1), env, capsys)
    run_hook(post_tool(devin=2), env, capsys)
    engine.queue_hook_note(HOOK, "[Watchdog] stop looping")
    pre = {"hook_event_name": "PreToolUse", "session_id": HOOK, "tool_name": "exec"}
    assert run_hook(pre, env, capsys) == ""  # PreToolUse cannot carry context: kept
    out = json.loads(run_hook(post_tool(), env, capsys))
    assert out["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": "[Watchdog] stop looping",
    }
    store.close()
    recorded = [json.loads(line) for line in store.path(HOOK).read_text().splitlines()]
    assert [r["event"]["action"] for r in recorded] == [
        "hook.post_tool",
        "hook.post_tool",
        "hook.pre_tool",
        "hook.post_tool",
    ]
    assert verify_file(store.path(HOOK)).valid


def test_events_spooled_while_watchdog_was_down_arrive_later(
    listener: Any, tmp_path: Path, capsys: Any
) -> None:
    base, _, store = listener
    spool = tmp_path / "spool"
    down = {
        "WATCHDOG_HOOK_DIR": str(spool),
        "WATCHDOG_HOOK_URL": "http://127.0.0.1:9/x",
        "WATCHDOG_INGEST_TOKEN": "tok",
    }
    run_hook(post_tool(), down, capsys)
    (spool / "last_failure").write_text("0")  # the backoff has passed
    up = {**down, "WATCHDOG_HOOK_URL": f"{base}/api/hook"}
    run_hook(post_tool(ok=True), up, capsys)
    store.close()
    outcomes = [
        json.loads(x)["event"]["outcome"] for x in store.path(HOOK).read_text().splitlines()
    ]
    assert outcomes == ["failure", "success"]


def test_hooks_json_runs_the_bundled_script() -> None:
    hooks = json.loads((ROOT / "devin-plugin" / "hooks.json").read_text())
    assert set(hooks) >= {"PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart"}
    command = hooks["PostToolUse"][0]["hooks"][0]["command"]
    assert re.search(r"DEVIN_PLUGIN_ROOT.*hooks/watchdog_hook\.py", command)
