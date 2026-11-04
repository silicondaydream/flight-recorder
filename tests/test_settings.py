"""Settings, Teams notification policy, lifecycle notices, manual actions, the judge."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import config

from agent_watchdog.actions import DevinActions, Teams
from agent_watchdog.chain import Recorder
from agent_watchdog.dashboard import serve
from agent_watchdog.engine import State, Watchdog
from agent_watchdog.fakedevin import FakeDevin
from agent_watchdog.notify import Notice, Notifier
from agent_watchdog.reader import entries
from agent_watchdog.settings import FIELDS, Config, SettingsError, schema

T0 = 1_793_000_000.0
STUCK = "Running `npm test`. It failed: Error: Cannot find module './gateway'"


def stamp(at: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


# -- the settings store -----------------------------------------------------------


def test_every_field_has_a_valid_default_and_a_group() -> None:
    groups = {g["id"] for g in schema()["groups"]}
    for f in FIELDS:
        assert f.key.split(".")[0] in groups, f.key
        Config(values={f.key: f.default})  # the default passes its own validation


def test_values_are_validated_and_saved(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    c = Config(path)
    with pytest.raises(SettingsError, match="at least"):
        c.update({"ladder.cooldown_seconds": 1})
    with pytest.raises(SettingsError, match="choices"):
        c.update({"teams.verbosity": "loud"})
    with pytest.raises(SettingsError, match="unknown"):
        c.update({"nope": 1})
    c.update({"ladder.cooldown_seconds": 90, "teams.verbosity": "detailed"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"ladder.cooldown_seconds": 90.0, "teams.verbosity": "detailed"}  # changes only
    assert Config(path).get("teams.verbosity") == "detailed"  # survives a restart


def test_command_line_overrides_win_for_one_run_and_are_never_saved(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    Config(path).update({"general.dry_run": True})
    c = Config(path, {"general.dry_run": False})  # --live
    assert c.get("general.dry_run") is False
    c.update({"teams.verbosity": "compact"})
    assert "general.dry_run" not in json.loads(path.read_text(encoding="utf-8"))  # not saved
    c.update({"general.dry_run": True})  # a change from the panel replaces the override
    assert c.get("general.dry_run") is True


def test_a_saved_value_from_an_older_version_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"gone.setting": 1, "teams.verbosity": "loud"}), encoding="utf-8")
    assert Config(path).get("teams.verbosity") == "standard"


def test_rungs_that_are_off_are_skipped() -> None:
    c = config(**{"ladder.alert": False, "ladder.terminate": False})
    assert c.rungs() == [0, 2, 3]


# -- the ladder, driven by settings ---------------------------------------------------


class Session:
    def __init__(self, log_dir: Path, sid: str = "devin-s1", **meta: Any) -> None:
        self.rec = Recorder(log_dir / f"{sid}.ndjson", session_id=sid, agent="devin")
        self.meta: dict[str, Any] = {
            "session_id": sid,
            "title": "Fix the checkout test",
            "url": f"https://app.devin.ai/sessions/{sid}",
            "status": "running",
            "status_detail": "working",
            "tags": [],
            "acus_consumed": 0.0,
            "pull_requests": [],
            "created_at": int(T0),
            **meta,
        }

    def snapshot(self, at: float, **fields: Any) -> None:
        self.meta.update(fields)
        value = {"value": dict(self.meta)}
        self.rec.append("session.snapshot", timestamp=stamp(at), activity={"session": value})

    def say(self, at: float, text: str, sender: str = "devin") -> None:
        body = {"from": sender, "text": {"value": text}}
        self.rec.append("message", timestamp=stamp(at), activity=body)


class Rig:
    def __init__(self, tmp_path: Path, **values: Any) -> None:
        self.clock = Clock()
        self.fake = FakeDevin(clock=self.clock)
        self.fake.add("devin-s1")
        self.logs = tmp_path / "logs"
        self.session = Session(self.logs)
        self.session.snapshot(T0)
        self.engine = Watchdog(
            config(**{"dry_run": False, "cooldown": 60.0, **values}),
            State(tmp_path / "w.db"),
            devin=DevinActions("cog_t", transport=self.fake.transport()),
            teams=Teams("https://teams.example/teams", transport=self.fake.transport()),
            record_dir=self.logs / "watchdog",
            clock=self.clock,
        )
        self.engine.started_at = T0 - 1
        self.fed = 0

    def at(self, seconds: float) -> None:
        self.clock.now = T0 + seconds
        stream = list(entries(self.logs))
        for entry in stream[self.fed :]:
            self.engine.observe(entry)
        self.fed = len(stream)
        self.engine.tick()

    def stuck(self, start: float, n: int = 3) -> None:
        for i in range(n):
            self.session.say(T0 + start + i, STUCK)

    def rungs(self) -> list[str]:
        return [e.rung for e in self.engine.events if e.kind == "action"]

    def cards(self) -> list[str]:
        return [
            c["attachments"][0]["content"]["body"][0]["items"][1]["text"] for c in self.fake.teams
        ]

    def close(self) -> None:
        self.session.rec.close()
        self.engine.close()


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    made: list[Rig] = []

    def build(**values: Any) -> Rig:
        r = Rig(tmp_path / str(len(made)), **values)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


def test_a_disabled_step_is_skipped(rig: Any) -> None:
    r: Rig = rig(**{"ladder.alert": False})
    r.stuck(10)
    for t in (20, 90, 160):
        r.at(t)
    assert r.rungs() == ["log", "nudge", "sleep"]


def test_turning_terminate_off_stops_before_approval(rig: Any) -> None:
    r: Rig = rig(**{"ladder.terminate": False})
    r.stuck(10)
    for t in (20, 90, 160, 230):
        r.at(t)
    r.fake.update("devin-s1", status="running")
    r.session.snapshot(T0 + 240, status="running", status_detail="working", is_archived=False)
    r.stuck(245)
    r.at(300)
    r.at(400)
    assert not r.engine.state.get("devin-s1").pending
    assert "top of the ladder" in r.engine.held["devin-s1"]


def test_a_rule_can_be_turned_off_and_capped(rig: Any) -> None:
    off = [f"rules.{rule}.enabled" for rule in ("repeated_errors", "test_build_loop")]
    r: Rig = rig(
        **dict.fromkeys(off, False), **{"rules.repeated_failing_commands.ceiling": "alert"}
    )
    r.stuck(10)
    for t in (20, 90, 160):
        r.at(t)
    assert [f.rule for f in r.engine.findings["devin-s1"]] == ["repeated_failing_commands"]
    assert r.rungs() == ["log", "alert"]


def test_message_findings_can_be_ignored(rig: Any) -> None:
    r: Rig = rig(**{"rules.use_message_findings": False})
    r.stuck(10)
    r.at(20)
    assert r.engine.findings["devin-s1"] == []


def test_thresholds_come_from_settings_live(rig: Any) -> None:
    r: Rig = rig()
    r.stuck(10, n=2)
    r.at(20)
    assert r.rungs() == []
    r.engine.config.update({"rules.repeated_failing_commands.repeats": 2})
    r.at(21)
    assert r.rungs() == ["log"]


# -- what reaches Teams ------------------------------------------------------------------


def test_each_step_sends_a_card_unless_its_trigger_is_off(rig: Any) -> None:
    r: Rig = rig(**{"teams.per_session_seconds": 0, "teams.on_nudge": False})
    r.stuck(10)
    for t in (20, 90, 160, 230):
        r.at(t)
    assert r.cards() == ["Session looks stuck", "Session put to sleep"]


def test_cards_per_session_are_rate_limited_and_counted(rig: Any) -> None:
    r: Rig = rig(**{"teams.per_session_seconds": 600})
    r.stuck(10)
    for t in (20, 90, 160, 230):
        r.at(t)
    assert r.cards() == ["Session looks stuck"]
    outcomes = [n.outcome for n in r.engine.notifier.history]
    assert outcomes.count("limited (per session)") == 2


def test_approval_requests_skip_the_rate_limit(rig: Any) -> None:
    r: Rig = rig(**{"teams.per_session_seconds": 86400})
    r.stuck(10)
    for t in (20, 90, 160, 230):
        r.at(t)
    r.fake.update("devin-s1", status="running")
    r.session.snapshot(T0 + 240, status="running", status_detail="working", is_archived=False)
    r.stuck(245)
    r.at(300)
    assert r.cards()[-1].startswith("Approve terminating")


def test_dry_run_cards_can_be_turned_off(rig: Any) -> None:
    r: Rig = rig(**{"dry_run": True, "teams.in_dry_run": False})
    r.stuck(10)
    r.at(20)
    r.at(90)
    assert r.fake.teams == []
    assert r.rungs() == ["log", "alert"]


def _notifier(fake: FakeDevin, clock: Clock, **values: Any) -> Notifier:
    teams = Teams("https://teams.example/teams", transport=fake.transport())
    return Notifier(config(**values), teams, clock)


def test_the_hourly_cap_and_the_digest() -> None:
    fake, clock = FakeDevin(), Clock()
    n = _notifier(fake, clock, **{"teams.max_per_hour": 2, "teams.per_session_seconds": 0})
    for i in range(4):
        n.notify(Notice("alert", f"s{i}", "Stuck", at=clock.now))
    assert len(fake.teams) == 2
    assert [x.outcome for x in n.history][-1] == "limited (per hour)"

    fake2 = FakeDevin()
    d = _notifier(fake2, clock, **{"teams.digest_minutes": 5, "teams.per_session_seconds": 0})
    for i in range(3):
        d.notify(Notice("alert", f"s{i}", f"Stuck {i}", "why"))
    d.flush()
    assert fake2.teams == []  # not due yet
    clock.now += 301
    d.flush()
    assert len(fake2.teams) == 1
    body = fake2.teams[0]["attachments"][0]["content"]["body"]
    assert "3 updates across 3 session(s)" in body[1]["text"]


def test_verbosity_shapes_the_card() -> None:
    fake, clock = FakeDevin(), Clock()
    notice = Notice(
        "alert",
        "s1",
        "Stuck",
        "why",
        facts={"ACUs": "4.0"},
        evidence=["npm test failed 3 times"],
        messages=["secret-ish agent text"],
    )
    compact = _notifier(fake, clock, **{"teams.verbosity": "compact"})
    compact.notify(Notice(**{**notice.__dict__, "session": "a"}))
    detailed = _notifier(
        fake,
        clock,
        **{
            "teams.verbosity": "detailed",
            "teams.include_agent_text": True,
            "teams.per_session_seconds": 0,
        },
    )
    detailed.notify(Notice(**{**notice.__dict__, "session": "b"}))
    small, big = (json.dumps(c) for c in fake.teams)
    assert "FactSet" not in small and "npm test failed" not in small
    assert "FactSet" in big and "npm test failed" in big and "secret-ish agent text" in big


def test_agent_text_stays_out_unless_allowed() -> None:
    fake, clock = FakeDevin(), Clock()
    n = _notifier(fake, clock, **{"teams.verbosity": "detailed"})
    n.notify(Notice("alert", "s1", "Stuck", messages=["private"]))
    assert "private" not in json.dumps(fake.teams)


# -- lifecycle notices -----------------------------------------------------------------


def test_runtime_and_spend_are_announced_once(rig: Any) -> None:
    r: Rig = rig(
        **{"teams.runtime_minutes": 10, "teams.acus_threshold": 5, "teams.per_session_seconds": 0}
    )
    r.session.snapshot(T0 + 30, acus_consumed=6.0)
    r.at(40)
    r.at(700)
    r.at(800)
    titles = r.cards()
    assert titles.count("Session spend passed threshold") == 1
    assert titles.count("Session running long") == 1


def test_session_start_and_end_notices(rig: Any) -> None:
    r: Rig = rig(
        **{
            "teams.on_session_start": True,
            "teams.on_session_end": True,
            "teams.per_session_seconds": 0,
        }
    )
    r.at(5)
    r.session.snapshot(T0 + 10, status_detail="finished")
    r.at(15)
    assert r.cards() == ["Session started", "Session finished"]


# -- manual actions --------------------------------------------------------------------


def test_manual_actions_reach_devin_and_are_recorded(rig: Any) -> None:
    r: Rig = rig()
    r.at(1)
    ok, _ = r.engine.manual("devin-s1", "message", by="dana", text="Try the staging config")
    assert ok and r.fake.sent[-1][1] == "[Watchdog] Try the staging config"
    assert r.engine.manual("devin-s1", "sleep", by="dana")[0]
    assert r.fake.sessions["devin-s1"]["status"] == "suspended"
    assert r.engine.manual("devin-s1", "terminate", by="dana")[0]
    assert r.fake.sessions["devin-s1"]["status"] == "exit"
    log = r.logs / "watchdog" / "watchdog-devin-s1.ndjson"
    actions = [json.loads(line)["event"]["action"] for line in log.read_text().splitlines()]
    assert actions == [
        "watchdog.manual.message",
        "watchdog.manual.sleep",
        "watchdog.manual.terminate",
    ]


def test_manual_actions_respect_dry_run(rig: Any) -> None:
    r: Rig = rig(**{"dry_run": True})
    r.at(1)
    ok, detail = r.engine.manual("devin-s1", "terminate", by="dana")
    assert ok and detail.startswith("[dry run]")
    assert r.fake.sessions["devin-s1"]["status"] == "running"


def test_a_muted_session_is_watched_but_not_acted_on(rig: Any) -> None:
    r: Rig = rig()
    r.at(1)
    r.engine.manual("devin-s1", "mute", by="dana")
    r.stuck(10)
    r.at(20)
    assert r.rungs() == [] and r.engine.held["devin-s1"] == "muted from the dashboard"
    assert r.engine.snapshot()["sessions"][0]["state"] == "Muted"


def test_a_message_to_a_sleeping_session_is_refused(rig: Any) -> None:
    r: Rig = rig()
    r.session.snapshot(T0 + 1, status="suspended", status_detail="inactivity")
    r.at(2)
    ok, why = r.engine.manual("devin-s1", "message", by="dana", text="hello")
    assert not ok and "wake" in why and r.fake.sent == []


# -- the plain-language view -------------------------------------------------------------


def test_states_and_next_steps_read_plainly(rig: Any) -> None:
    r: Rig = rig()
    r.at(1)
    s = r.engine.snapshot()["sessions"][0]
    assert (s["state"], s["next"]) == ("Healthy", "No problems detected.")
    r.stuck(10)
    r.at(20)
    s = r.engine.snapshot()["sessions"][0]
    assert s["state"] == "Flagged" and s["next"].startswith("Will alert in")


def test_the_detail_view_has_entries_signals_and_decisions(rig: Any) -> None:
    r: Rig = rig()
    r.stuck(10)
    r.at(20)
    d = r.engine.detail("devin-s1")
    assert d is not None
    assert [e["event"]["action"] for e in d["entries"]][:2] == ["session.snapshot", "message"]
    assert any(o["kind"] == "command" and o["outcome"] == "failure" for o in d["observations"])
    assert d["decisions"][0]["rung"] == "log"
    assert r.engine.detail("nope") is None


def test_the_live_log_has_levels(rig: Any) -> None:
    r: Rig = rig()
    r.stuck(10)
    r.at(20)
    feed = r.engine.snapshot()["feed"]
    levels = {(x["kind"], x["level"]) for x in feed}
    assert ("action", 1) in levels and ("finding", 2) in levels and ("message", 3) in levels


# -- the settings API --------------------------------------------------------------------


def _call(url: str, method: str, body: Any) -> tuple[int, Any]:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status), json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_the_settings_api_validates_and_applies(rig: Any) -> None:
    r: Rig = rig()
    r.at(1)
    server = serve(r.engine, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/settings", timeout=5) as response:  # noqa: S310
            got = json.load(response)
        assert got["can_edit"] and any(f["key"] == "teams.verbosity" for f in got["fields"])
        code, body = _call(f"{base}/api/settings", "PUT", {"values": {"teams.max_per_hour": 0}})
        assert code == 400 and "at least" in body["error"]
        code, body = _call(f"{base}/api/settings", "PUT", {"values": {"teams.max_per_hour": 5}})
        assert code == 200 and r.engine.config.get("teams.max_per_hour") == 5
        code, body = _call(
            f"{base}/api/action", "POST", {"session_id": "devin-s1", "action": "mute"}
        )
        assert code == 200 and r.engine.state.get("devin-s1").muted is True
        with urllib.request.urlopen(f"{base}/api/session/devin-s1", timeout=5) as response:  # noqa: S310
            assert json.load(response)["muted"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_settings_need_the_token_when_one_is_set(rig: Any) -> None:
    r: Rig = rig()
    server = serve(r.engine, "127.0.0.1", 0, token="s3cret")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _call(f"{base}/api/settings", "PUT", {"values": {}})[0] == 403
        assert _call(f"{base}/api/settings", "PUT", {"values": {}, "token": "s3cret"})[0] == 200
    finally:
        server.shutdown()
        server.server_close()


# -- the OpenAI judge -------------------------------------------------------------------


def test_the_openai_judge_asks_for_strict_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("openai")
    from agent_watchdog.judge import OpenAIJudge
    from agent_watchdog.observe import SessionView

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen: dict[str, Any] = {}

    def create(**request: Any) -> Any:
        seen.update(request)
        content = json.dumps({"stuck": True, "confidence": 0.8, "reason": "loop", "nudge": "Stop."})
        message = SimpleNamespace(content=content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    values: dict[str, Any] = {"judge.model": "my-model", "judge.reasoning_effort": "low"}
    c = config(**values)
    judge = OpenAIJudge(c)
    judge._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )  # type: ignore[assignment]
    verdict = judge.review(SessionView("s1", task="fix it"), [])
    assert verdict is not None and verdict.stuck and verdict.nudge == "Stop."
    assert seen["model"] == "my-model" and seen["reasoning_effort"] == "low"
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert "<session>" in seen["messages"][1]["content"]
