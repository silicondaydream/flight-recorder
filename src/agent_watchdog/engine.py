"""Judging and escalating: rules in, at most one step up the ladder out.

Escalation, per session: log → alert (Teams) → nudge → sleep → terminate.
While a session keeps matching rules, it moves up one step per cooldown, and
goes back to the start once it has matched nothing for a while. Every number,
every step and every rule is a setting (`settings.py`), read on each tick.

Guardrails, in the order they are checked:

- the session must be running (a suspended one is never nudged: Devin's API
  wakes a session up to deliver a message);
- an opt-out tag, or muting from the dashboard, stops everything but the log;
- a pending approval stops everything until someone decides;
- a cooldown between steps;
- ceilings: each rule's own, and alerts only while a session waits on a person;
- the optional AI judge can veto a step;
- terminate needs a person's approval;
- dry run: nothing is sent to Devin.

Everything Watchdog decides is written to its own hash-chained log, one per
session: what fired, what it did, and why not when it held back. Notices about
those decisions (and about sessions starting, running long, spending) go
through the `Notifier`, which decides what reaches Teams.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_watchdog.actions import DevinActions, Teams
from agent_watchdog.approvals import Signer
from agent_watchdog.chain import Recorder, safe_name
from agent_watchdog.devin import DevinError
from agent_watchdog.judge import Judge, Verdict
from agent_watchdog.notify import Notice, Notifier
from agent_watchdog.observe import NUDGE_MARK, Entry, SessionView, epoch, kept_value
from agent_watchdog.rules import RUNGS, Finding, evaluate
from agent_watchdog.settings import Config

LOG, ALERT, NUDGE, SLEEP, TERMINATE = range(len(RUNGS))
DECISIONS, NOTABLE, EVERYTHING = 1, 2, 3  # live log levels
LABELS = {
    "log": "Log",
    "alert": "Alert",
    "nudge": "Nudge",
    "sleep": "Sleep",
    "terminate": "Terminate",
}


@dataclass
class Ladder:
    rung: int = -1  # -1: nothing done yet
    last_action: float = 0.0
    last_finding: float = 0.0
    pending: bool = False  # terminate waits on approval
    approved_at: float = 0.0
    approved_by: str = ""
    declined_by: str = ""  # someone chose "keep running": no terminate this incident
    muted: bool = False  # muted from the dashboard: watched, never acted on


COLUMNS = (
    ("rung", "INTEGER", -1),
    ("last_action", "REAL", 0.0),
    ("last_finding", "REAL", 0.0),
    ("pending", "INTEGER", 0),
    ("approved_at", "REAL", 0.0),
    ("approved_by", "TEXT", ""),
    ("declined_by", "TEXT", ""),
    ("muted", "INTEGER", 0),
)


class State:
    """Where each session is on the ladder, and which notices it has had. Survives restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("CREATE TABLE IF NOT EXISTS ladder (session_id TEXT PRIMARY KEY)")
        have = {row[1] for row in self._db.execute("PRAGMA table_info(ladder)")}
        for name, kind, _ in COLUMNS:  # a database from an older version gains new columns
            if name not in have:
                self._db.execute(f"ALTER TABLE ladder ADD COLUMN {name} {kind}")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS notices (session_id TEXT, key TEXT, "
            "PRIMARY KEY (session_id, key))"
        )
        self._db.commit()

    def get(self, session_id: str) -> Ladder:
        names = ", ".join(c[0] for c in COLUMNS)
        with self._lock:
            row = self._db.execute(
                f"SELECT {names} FROM ladder WHERE session_id = ?",  # noqa: S608 -- fixed names
                (session_id,),
            ).fetchone()
        if row is None:
            return Ladder()
        values: list[Any] = [
            default if v is None else v for v, (_, _, default) in zip(row, COLUMNS, strict=True)
        ]
        return Ladder(
            int(values[0]),
            float(values[1]),
            float(values[2]),
            bool(values[3]),
            float(values[4]),
            str(values[5]),
            str(values[6]),
            bool(values[7]),
        )

    def put(self, session_id: str, ladder: Ladder) -> None:
        names = [c[0] for c in COLUMNS]
        values = [
            ladder.rung,
            ladder.last_action,
            ladder.last_finding,
            int(ladder.pending),
            ladder.approved_at,
            ladder.approved_by,
            ladder.declined_by,
            int(ladder.muted),
        ]
        sql = (
            f"INSERT INTO ladder (session_id, {', '.join(names)}) "  # noqa: S608 -- fixed names
            f"VALUES (?, {', '.join('?' * len(names))}) ON CONFLICT(session_id) DO UPDATE SET "
            + ", ".join(f"{n} = excluded.{n}" for n in names)
        )
        with self._lock:
            self._db.execute(sql, (session_id, *values))
            self._db.commit()

    def noticed(self, session_id: str, key: str) -> bool:
        """True the first time only: whether `key` was already noticed for the session."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO notices VALUES (?, ?)", (session_id, key))
            self._db.commit()
            return cur.rowcount == 0

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class Event:
    at: float
    session_id: str
    kind: str  # action / held / judged / approval / recovered / error / linked / manual
    rung: str = ""
    detail: str = ""
    rules: list[str] = field(default_factory=list)
    dry_run: bool = False
    ok: bool = True
    links: dict[str, str] = field(default_factory=dict)  # e.g. the approval links sent
    teams: str = ""  # what happened to its Teams notice


@dataclass
class Activity:
    """One line of the live log that is not a decision: what the agent did."""

    at: float
    session_id: str
    kind: str  # message / tool / result / status / finding / lifecycle
    text: str
    level: int
    ok: bool = True
    sequence: int = 0  # the entry's sequence in its log, for the detail view
    source: str = ""


def _activity(entry: Entry, sid: str) -> Activity | None:
    event = entry.get("event", {})
    action = event.get("action")
    data = entry.get("activity") or {}
    at, seq = epoch(entry), int(event.get("sequence") or 0)
    source = str(entry.get("agent", {}).get("name", ""))
    if action == "message":
        sender = str(data.get("from") or "devin")
        text = str(kept_value(data.get("text")) or "")
        who = (
            "Watchdog"
            if text.startswith(NUDGE_MARK)
            else ("Agent" if sender == "devin" else "Person")
        )
        return Activity(
            at, sid, "message", f"{who}: {text}", EVERYTHING, sequence=seq, source=source
        )
    if action == "tool.call":
        args = kept_value(data.get("arguments"))
        args = args if isinstance(args, dict) else {}
        what = args.get("command") or args.get("path") or args.get("session") or ""
        text = f"{data.get('tool')}  {what}".strip()
        return Activity(at, sid, "tool", text, EVERYTHING, sequence=seq, source=source)
    if action == "tool.result":
        failed = event.get("outcome") == "failure"
        result = kept_value(data.get("result"))
        first = ""
        if isinstance(result, dict):
            content = result.get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            first = (texts[0] if texts else str(result.get("message", ""))).split("\n")[0]
        text = f"{data.get('tool')}  {'failed' if failed else 'ok'}  {first}".strip()
        level = NOTABLE if failed else EVERYTHING
        return Activity(at, sid, "result", text, level, not failed, seq, source)
    if action == "session.status":
        detail = data.get("status_detail")
        text = f"Status: {data.get('status')}" + (f" · {detail}" if detail else "")
        return Activity(at, sid, "status", text, NOTABLE, sequence=seq, source=source)
    if action in ("session.start", "session.end"):
        text = "MCP session started" if action == "session.start" else "MCP session ended"
        return Activity(at, sid, "lifecycle", text, NOTABLE, sequence=seq, source=source)
    return None


class Watchdog:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        devin: DevinActions | None = None,
        teams: Teams | None = None,
        judge: Judge | None = None,
        record_dir: Path | None = None,
        signer: Signer | None = None,
        clock: Callable[[], float] = time.time,
        info: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.devin = devin
        self.teams = teams
        self.notifier = Notifier(config, teams, clock)
        self.judge = judge
        self.record_dir = record_dir
        self.signer = signer
        self.clock = clock
        self.info = info or {}  # shown in settings: what is connected (never secrets)
        self.started_at = clock()
        self.aliases: dict[str, str] = {}  # MCP session -> the Devin session it linked to
        self.views: dict[str, SessionView] = {}
        self.entries: dict[str, deque[Entry]] = {}  # recent raw entries, for the detail view
        self.findings: dict[str, list[Finding]] = {}
        self.held: dict[str, str] = {}  # why the last step was not taken
        self.verdicts: dict[str, tuple[frozenset[str], float, Verdict]] = {}
        self.events: deque[Event] = deque(maxlen=1000)
        self.activity: deque[Activity] = deque(maxlen=3000)
        self.broken: str = ""  # a log failed verification: Watchdog stops acting
        self.lock = threading.RLock()
        self._recorders: dict[str, Recorder] = {}
        self._judge_calls: deque[float] = deque()

    # -- input -----------------------------------------------------------------

    def observe(self, entry: Entry) -> None:
        if entry.get("agent", {}).get("name") == "watchdog":
            return
        sid = str(entry.get("session", {}).get("id", ""))
        if not sid:
            return
        with self.lock:
            target = self.aliases.get(sid, sid)
            view = self.views.setdefault(target, SessionView(target))
            view.add(entry)
            keep = int(self.config.num("feed.detail_entries"))
            self.entries.setdefault(target, deque(maxlen=keep)).append(entry)
            item = _activity(entry, target)
            if item is not None:
                self.activity.append(item)
            if target == sid and view.link and view.link != sid:
                self._link(sid, view.link)

    def _link(self, mcp_sid: str, devin_sid: str) -> None:
        """An MCP log said which Devin session it belongs to: fold it in.

        From here on its tool calls count as that session's, exactly, and the
        ladder acts on the Devin session. Until the importer has seen that
        session, its view is not a Devin one, so it gets alerts only.
        """
        mcp_view = self.views.pop(mcp_sid)
        self.findings.pop(mcp_sid, None)
        self.held.pop(mcp_sid, None)
        self.aliases[mcp_sid] = devin_sid
        self.views.setdefault(devin_sid, SessionView(devin_sid)).absorb(mcp_view)
        moved = self.entries.pop(mcp_sid, deque())
        target = self.entries.setdefault(
            devin_sid, deque(maxlen=int(self.config.num("feed.detail_entries")))
        )
        target.extend(moved)
        for item in self.activity:
            if item.session_id == mcp_sid:
                item.session_id = devin_sid
        now = self.clock()
        self._event(Event(now, devin_sid, "linked", detail=f"MCP log {mcp_sid} linked"))
        self._record(devin_sid, "watchdog.linked", "success", {"mcp_session": mcp_sid})

    def chain_broken(self, reason: str) -> None:
        with self.lock:
            self.broken = reason
            event = Event(self.clock(), "-", "error", detail=f"log failed verification: {reason}")
            event.teams = self.notifier.notify(
                Notice(
                    "errors", "-", "Watchdog stopped acting", f"A log failed verification: {reason}"
                )
            )
            self._event(event)

    # -- the loop --------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            self.notifier.flush()
            if self.broken:
                return
            for sid, view in list(self.views.items()):
                if view.is_devin:
                    self._lifecycle(sid, view, now)
                self._step(sid, view, now)

    def _findings(self, view: SessionView, now: float) -> list[Finding]:
        c = self.config
        found = evaluate(view, now, c.thresholds())
        return [
            f
            for f in found
            if c.flag(f"rules.{f.rule}.enabled")
            and (c.flag("rules.use_message_findings") or not f.heuristic)
        ]

    def _step(self, sid: str, view: SessionView, now: float) -> None:
        c = self.config
        ladder = self.state.get(sid)
        approval_valid = now - ladder.approved_at <= c.num("ladder.approval_minutes") * 60
        if ladder.pending and ladder.approved_at and approval_valid:
            self._terminate(sid, view, ladder, now)
            return
        findings = self._findings(view, now)
        before = {f.rule for f in self.findings.get(sid, [])}
        self.findings[sid] = findings
        for f in findings:
            if f.rule not in before:
                text = f"Rule fired: {f.summary}"
                self.activity.append(Activity(now, sid, "finding", text, NOTABLE, False))
        if not findings:
            self.held.pop(sid, None)
            if ladder.rung >= 0 and now - ladder.last_finding > c.num("ladder.reset_after_seconds"):
                rung = RUNGS[ladder.rung]
                event = Event(now, sid, "recovered", rung, "no findings for a while: starting over")
                event.teams = self.notifier.notify(
                    self._notice("recovered", sid, view, "Session recovered", event.detail)
                )
                self._event(event)
                self._record(sid, "watchdog.recovered", "success", {"from_rung": rung})
                self.state.put(sid, Ladder(muted=ladder.muted))
            return
        ladder.last_finding = now
        self.state.put(sid, ladder)

        rules = [f.rule for f in findings]
        if not view.active:
            return self._hold(sid, view, "the session is not running", now, rules)
        if ladder.muted:
            return self._hold(sid, view, "muted from the dashboard", now, rules)
        off_tag = str(c.get("general.off_tag"))
        if off_tag and off_tag in view.tags:
            return self._hold(sid, view, f"tagged {off_tag}", now, rules)
        if ladder.pending:
            return self._hold(sid, view, "terminate is waiting for approval", now, rules)
        wait = ladder.last_action + c.num("ladder.cooldown_seconds") - now
        if ladder.last_action and wait > 0:
            return self._hold(sid, view, f"cooldown, {wait:.0f}s left", now, rules, quiet=True)

        ceiling = max(RUNGS.index(str(c.get(f"rules.{f.rule}.ceiling"))) for f in findings)
        reason = f"the highest step for {', '.join(rules)}"
        if view.waiting_on_human:
            cap = RUNGS.index(str(c.get("ladder.waiting_on_human")))
            if ceiling > cap:
                ceiling, reason = cap, "it is waiting on a person"
        if not view.is_devin and ceiling > ALERT:
            ceiling, reason = ALERT, "it is not linked to a Devin session"
        enabled = [r for r in c.rungs() if r > ladder.rung]
        if not enabled:
            return self._hold(
                sid, view, "at the top of the ladder (later steps are off)", now, rules
            )
        step = enabled[0]
        if step > ceiling:
            at = RUNGS[ladder.rung] if ladder.rung >= 0 else "the start"
            return self._hold(sid, view, f"held at {at}: {reason}", now, rules)

        verdict = self._judge(sid, view, findings, step, now)
        if (
            verdict is not None
            and not verdict.stuck
            and verdict.confidence >= c.num("judge.min_confidence")
        ):
            ladder.last_action = now  # don't ask again until the cooldown is over
            self.state.put(sid, ladder)
            event = Event(now, sid, "judged", RUNGS[step], f"not stuck: {verdict.reason}", rules)
            event.teams = self.notifier.notify(
                self._notice(
                    "judge_veto", sid, view, "AI judge: not stuck", verdict.reason, findings
                )
            )
            self._event(event)
            self._record(
                sid, "watchdog.judged", "success", {"verdict": verdict.as_dict(), "rules": rules}
            )
            return

        if step == TERMINATE and ladder.declined_by:
            why = f"terminate declined by {ladder.declined_by}; holding until it recovers"
            return self._hold(sid, view, why, now, rules)
        if step == TERMINATE:
            ladder.pending = True
            self.state.put(sid, ladder)
            links = self._approval_links(sid, now)
            event = Event(
                now, sid, "approval", "terminate", "approval requested", rules, links=links
            )
            notice = self._notice(
                "approval",
                sid,
                view,
                f"Approve terminating {view.title or sid}?",
                "Watchdog has tried everything below terminate and the session is still stuck.",
                findings,
            )
            notice.facts["Or run"] = f"watchdog approve {sid}"
            if links:
                notice.actions = [
                    ("Approve terminate", links["approve"]),
                    ("Keep running", links["decline"]),
                ]
            event.teams = self.notifier.notify(notice)
            self._event(event)
            self._record(sid, "watchdog.approval_requested", None, {"rules": rules})
            return
        self._act(sid, view, ladder, step, findings, verdict, now)

    def _hold(
        self,
        sid: str,
        view: SessionView,
        why: str,
        now: float,
        rules: list[str],
        *,
        quiet: bool = False,
    ) -> None:
        if self.held.get(sid) == why:
            return  # said once is enough
        self.held[sid] = why
        if not quiet:
            event = Event(now, sid, "held", detail=why, rules=rules)
            event.teams = self.notifier.notify(
                self._notice("held", sid, view, "Watchdog held back", why)
            )
            self._event(event)
            self._record(sid, "watchdog.held", None, {"reason": why, "rules": rules})

    def _judge(
        self, sid: str, view: SessionView, findings: list[Finding], step: int, now: float
    ) -> Verdict | None:
        c = self.config
        if self.judge is None or not c.flag("judge.enabled"):
            return None
        if step < RUNGS.index(str(c.get("judge.from_rung"))):
            return None
        key = frozenset(f.rule for f in findings)
        cached = self.verdicts.get(sid)
        if cached and cached[0] == key and now - cached[1] < c.num("ladder.cooldown_seconds"):
            return cached[2]
        while self._judge_calls and now - self._judge_calls[0] > 3600:
            self._judge_calls.popleft()
        if len(self._judge_calls) >= int(c.num("judge.max_calls_per_hour")):
            return None  # over budget: the rules stand
        self._judge_calls.append(now)
        verdict = self.judge.review(view, findings)
        if verdict is not None:
            self.verdicts[sid] = (key, now, verdict)
        return verdict

    # -- lifecycle notices ---------------------------------------------------------

    def _lifecycle(self, sid: str, view: SessionView, now: float) -> None:
        """Session started / ended / running long / spending / waiting: once each."""
        c = self.config
        fresh = self.started_at - 120  # don't announce what happened before Watchdog started

        def once(key: str, trigger: str, title: str, summary: str) -> None:
            if self.state.noticed(sid, key):
                return
            self.activity.append(Activity(now, sid, "lifecycle", f"{title}: {summary}", NOTABLE))
            self.notifier.notify(self._notice(trigger, sid, view, title, summary))

        runtime = (now - view.created_at) / 60 if view.created_at else 0.0
        if view.created_at and view.created_at >= fresh:
            once("start", "session_start", "Session started", view.title or sid)
        if view.ended_at:
            if view.ended_at >= fresh:
                how = "finished" if view.status_detail == "finished" else f"ended ({view.status})"
                once(
                    "end",
                    "session_end",
                    f"Session {how}",
                    f"{view.title or sid}: {view.acus_now:.1f} ACUs, {runtime:.0f} min",
                )
            return
        if not view.active:
            return
        limit = c.num("teams.runtime_minutes")
        if c.flag("teams.on_runtime") and runtime >= limit:
            once(
                "runtime",
                "runtime",
                "Session running long",
                f"{runtime:.0f} min so far (limit {limit:g})",
            )
        spend = c.num("teams.acus_threshold")
        if c.flag("teams.on_acus") and view.acus_now >= spend:
            once(
                "acus",
                "acus",
                "Session spend passed threshold",
                f"{view.acus_now:.1f} ACUs (threshold {spend:g})",
            )
        if view.waiting_since and c.flag("teams.on_waiting"):
            waited = (now - view.waiting_since) / 60
            if waited >= c.num("teams.waiting_minutes"):
                once(
                    f"waiting:{int(view.waiting_since)}",
                    "waiting",
                    "Session waiting on a person",
                    f"for {waited:.0f} min ({view.status_detail})",
                )

    # -- acting ----------------------------------------------------------------

    def _act(
        self,
        sid: str,
        view: SessionView,
        ladder: Ladder,
        step: int,
        findings: list[Finding],
        verdict: Verdict | None,
        now: float,
    ) -> None:
        rung = RUNGS[step]
        rules = [f.rule for f in findings]
        dry = self.config.flag("general.dry_run")
        worst = max(findings, key=lambda f: ("low", "medium", "high").index(f.severity))
        message = f"{NUDGE_MARK} {(verdict.nudge if verdict and verdict.nudge else worst.nudge)}"
        titles = {
            "log": "Problem logged",
            "alert": "Session looks stuck",
            "nudge": "Session nudged",
            "sleep": "Session put to sleep",
        }
        detail, ok = worst.summary, True
        try:
            if step == NUDGE:
                detail = f"sent: {message}"
                if not dry:
                    self._devin().nudge(sid, message)
            elif step == SLEEP:
                detail = "archived (asleep) and tagged watchdog:slept"
                if not dry:
                    self._devin().sleep(sid)
        except DevinError as exc:
            detail, ok = str(exc), False
        if dry and step in (NUDGE, SLEEP):
            detail = "[dry run] would have " + detail.replace("sent:", "sent")
        if ok:
            ladder.rung, ladder.last_action = step, now
            self.state.put(sid, ladder)
        self.held.pop(sid, None)
        event = Event(now, sid, "action", rung, detail, rules, dry, ok)
        summary = message.removeprefix(NUDGE_MARK).strip() if step == NUDGE else worst.summary
        notice = self._notice(rung if ok else "errors", sid, view, titles[rung], summary, findings)
        notice.dry_run = dry
        event.teams = self.notifier.notify(notice)
        if step == ALERT:
            event.detail = f"{worst.summary} (Teams: {event.teams})"
        self._event(event)
        self._record(
            sid,
            f"watchdog.{rung}",
            "success" if ok else "failure",
            {
                "rung": rung,
                "dry_run": dry,
                "detail": event.detail,
                "findings": [f.as_dict() for f in findings],
                "verdict": verdict.as_dict() if verdict else None,
                "acus_consumed": view.acus_now,
                "teams": event.teams,
            },
        )

    def _terminate(self, sid: str, view: SessionView, ladder: Ladder, now: float) -> None:
        dry = self.config.flag("general.dry_run")
        ok, detail = True, f"terminated (approved by {ladder.approved_by or 'unknown'})"
        if not view.active and view.status in ("exit", "error"):
            detail = "already ended; approval cleared"
        else:
            try:
                if not dry:
                    self._devin().terminate(sid)
            except DevinError as exc:
                ok, detail = False, str(exc)
        if dry:
            detail = "[dry run] would have " + detail
        ladder.pending, ladder.approved_at = False, 0.0
        if ok:
            ladder.rung, ladder.last_action = TERMINATE, now
        self.state.put(sid, ladder)
        event = Event(now, sid, "action", "terminate", detail, dry_run=dry, ok=ok)
        notice = self._notice(
            "terminate" if ok else "errors", sid, view, "Session terminated", detail
        )
        notice.dry_run = dry
        event.teams = self.notifier.notify(notice)
        self._event(event)
        self._record(
            sid,
            "watchdog.terminate",
            "success" if ok else "failure",
            {
                "rung": "terminate",
                "dry_run": dry,
                "detail": detail,
                "approved_by": ladder.approved_by,
            },
        )

    def approve(self, sid: str, by: str) -> bool:
        """A person allows the pending terminate. Applied on the next tick."""
        with self.lock:
            ladder = self.state.get(sid)
            if not ladder.pending:
                return False
            ladder.approved_at, ladder.approved_by = self.clock(), by
            self.state.put(sid, ladder)
            self._decided(sid, f"terminate approved by {by}")
            self._record(sid, "watchdog.approved", "success", {"by": by})
            return True

    def decline(self, sid: str, by: str) -> bool:
        """A person says keep it running: no terminate until the session recovers."""
        with self.lock:
            ladder = self.state.get(sid)
            if not ladder.pending:
                return False
            ladder.pending, ladder.approved_at, ladder.declined_by = False, 0.0, by
            ladder.last_action = self.clock()
            self.state.put(sid, ladder)
            self._decided(sid, f"kept running by {by}")
            self._record(sid, "watchdog.declined", "success", {"by": by})
            return True

    def _decided(self, sid: str, what: str) -> None:
        view = self.views.get(sid) or SessionView(sid)
        event = Event(self.clock(), sid, "approval", "terminate", what)
        event.teams = self.notifier.notify(
            self._notice("decision", sid, view, "Decision made", what)
        )
        self._event(event)

    def manual(self, sid: str, action: str, by: str, text: str = "") -> tuple[bool, str]:
        """A person acting from the dashboard: message, sleep, terminate, mute, unmute."""
        with self.lock:
            view = self.views.get(sid)
            if view is None:
                return False, "no such session"
            ladder = self.state.get(sid)
            dry = self.config.flag("general.dry_run")
            ok, detail = True, ""
            try:
                if action == "message":
                    body = text.strip()
                    if not body:
                        return False, "the message is empty"
                    detail = f"sent: {body[:500]}"
                    if not view.is_devin:
                        return False, "not a Devin session"
                    if not view.active:
                        return False, "the session is not running (a message would wake it)"
                    if not dry:
                        self._devin().nudge(sid, f"{NUDGE_MARK} {body}")
                elif action == "sleep":
                    detail = "put to sleep"
                    if not dry:
                        self._devin().sleep(sid)
                elif action == "terminate":
                    detail = "terminated"
                    if not dry:
                        self._devin().terminate(sid)
                    ladder.pending, ladder.rung = False, TERMINATE
                elif action in ("mute", "unmute"):
                    ladder.muted = action == "mute"
                    detail = "muted: watched, never acted on" if ladder.muted else "unmuted"
                    self.held.pop(sid, None)
                else:
                    return False, f"unknown action {action!r}"
            except DevinError as exc:
                ok, detail = False, str(exc)
            if dry and action in ("message", "sleep", "terminate"):
                detail = "[dry run] would have " + detail.replace("sent:", "sent")
            if ok:
                ladder.last_action = self.clock()
                self.state.put(sid, ladder)
            event = Event(
                self.clock(), sid, "manual", action, f"{detail} (by {by})", dry_run=dry, ok=ok
            )
            notice = self._notice(
                "manual" if ok else "errors", sid, view, f"{by}: {action}", detail
            )
            notice.dry_run = dry
            event.teams = self.notifier.notify(notice)
            self._event(event)
            self._record(
                sid,
                f"watchdog.manual.{action}",
                "success" if ok else "failure",
                {"by": by, "detail": detail},
            )
            return ok, detail

    def pending(self, sid: str) -> bool:
        return self.state.get(sid).pending

    def _approval_links(self, sid: str, now: float) -> dict[str, str]:
        if self.signer is None:
            return {}
        expires = int(now + self.config.num("ladder.approval_minutes") * 60)
        return {d: self.signer.link(d, sid, expires) for d in ("approve", "decline")}

    def _notice(
        self,
        trigger: str,
        sid: str,
        view: SessionView,
        title: str,
        summary: str,
        findings: list[Finding] | None = None,
    ) -> Notice:
        now = self.clock()
        runtime = f"{(now - view.created_at) / 60:.0f} min" if view.created_at else ""
        ladder = self.state.get(sid)
        facts = {
            "Session": view.title or sid,
            "Status": " · ".join(x for x in (view.status, view.status_detail or "") if x),
            "Rules": ", ".join(f.rule.replace("_", " ") for f in findings or []),
            "Step": LABELS.get(RUNGS[ladder.rung], "") if ladder.rung >= 0 else "",
            "ACUs": f"{view.acus_now:.1f}" if view.acus else "",
            "Running": runtime,
        }
        evidence = [f"{f.summary}" for f in findings or []]
        messages = [o.value[:300] for o in view.messages()[-3:]]
        return Notice(
            trigger, sid, title, summary, facts, evidence, messages, link=view.url, at=now
        )

    def _devin(self) -> DevinActions:
        if self.devin is None:
            raise DevinError("no Devin client: set DEVIN_API_KEY")
        return self.devin

    # -- the record ------------------------------------------------------------

    def _event(self, event: Event) -> None:
        self.events.append(event)

    def _record(self, sid: str, action: str, outcome: str | None, activity: dict[str, Any]) -> None:
        if self.record_dir is None:
            return
        rec = self._recorders.get(sid)
        if rec is None:
            path = self.record_dir / f"watchdog-{safe_name(sid)}.ndjson"
            rec = self._recorders[sid] = Recorder(path, session_id=sid, agent="watchdog")
        rec.append(action, outcome=outcome, activity={"source": "watchdog", **activity})

    def close(self) -> None:
        for rec in self._recorders.values():
            rec.close()
        for client in (self.devin, self.teams):
            if client is not None:
                client.close()
        self.state.close()

    # -- what the dashboard shows ------------------------------------------------

    def _plain(
        self, sid: str, view: SessionView, ladder: Ladder, now: float
    ) -> tuple[str, str, str]:
        """(state, tone, next step) in plain words."""
        findings = self.findings.get(sid, [])
        off = str(self.config.get("general.off_tag"))
        if view.ended_at:
            done = "Finished" if view.status_detail == "finished" else "Ended"
            return done, "ended", "Nothing more to do."
        if ladder.pending:
            return (
                "Needs approval",
                "critical",
                "Waiting for someone to approve or decline terminate.",
            )
        if view.status == "suspended":
            return "Asleep", "asleep", "Asleep. Watchdog will not wake it."
        if ladder.muted or (off and off in view.tags):
            return "Muted", "muted", "Recorded only: Watchdog will not act on it."
        if view.waiting_on_human:
            return (
                "Waiting on you",
                "waiting",
                "The agent asked a question. No nudges while it waits.",
            )
        if not findings:
            if ladder.rung >= 0:
                left = ladder.last_finding + self.config.num("ladder.reset_after_seconds") - now
                return "Recovering", "ok", f"Looks fine again. Starts over in {max(0, left):.0f}s."
            return "Healthy", "ok", "No problems detected."
        state = (
            "Nudged" if ladder.rung >= NUDGE else ("Alerted" if ladder.rung >= ALERT else "Flagged")
        )
        held = self.held.get(sid, "")
        if held and not held.startswith("cooldown"):
            return state, "warning", f"Held back: {held}."
        upcoming = [r for r in self.config.rungs() if r > ladder.rung]
        if not upcoming:
            return state, "warning", "At the top of the ladder."
        wait = ladder.last_action + self.config.num("ladder.cooldown_seconds") - now
        label = LABELS[RUNGS[upcoming[0]]].lower()
        if ladder.last_action and wait > 0:
            return state, "warning", f"Will {label} in {wait:.0f}s if still stuck."
        return state, "warning", f"Will {label} on the next check."

    def _session(self, sid: str, view: SessionView, now: float) -> dict[str, Any]:
        ladder = self.state.get(sid)
        state, tone, next_step = self._plain(sid, view, ladder, now)
        findings = self.findings.get(sid, [])
        chars = int(self.config.num("feed.text_chars"))
        return {
            "id": sid,
            "title": view.title or sid,
            "url": view.url,
            "agent": view.agent,
            "task": view.task[:600],
            "status": view.status,
            "status_detail": view.status_detail,
            "state": state,
            "tone": tone,
            "next": next_step,
            "tags": view.tags,
            "acus": round(view.acus_now, 2),
            "acus_series": [[round(t - now), round(a, 2)] for t, a in view.acus[-60:]],
            "acus_since_progress": round(view.acus_now - view.acus_at_progress, 2),
            "minutes_since_progress": round((now - view.last_progress) / 60, 1)
            if view.last_progress
            else None,
            "runtime_minutes": round((now - view.created_at) / 60, 1) if view.created_at else None,
            "last_activity": round(now - view.last_seen) if view.last_seen else None,
            "rung": ladder.rung,
            "rung_name": RUNGS[ladder.rung] if ladder.rung >= 0 else "",
            "pending_approval": ladder.pending,
            "declined_by": ladder.declined_by,
            "muted": ladder.muted,
            "held": self.held.get(sid, ""),
            "mcp_sources": view.sources,
            "findings": [f.as_dict() for f in findings],
            "recent": [
                {"from": o.source, "text": o.value[:chars], "ago": round(now - o.at)}
                for o in [o for o in view.observations if o.kind == "message"][-3:]
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        c = self.config
        with self.lock:
            hide = c.num("feed.hide_ended_minutes") * 60
            sessions = [
                self._session(sid, view, now)
                for sid, view in self.views.items()
                if not (hide and view.ended_at and now - view.ended_at > hide)
            ]
            order = {
                "critical": 0,
                "warning": 1,
                "waiting": 2,
                "ok": 3,
                "asleep": 4,
                "muted": 5,
                "ended": 6,
            }
            # Most urgent first; within a group, the most recently active first.
            sessions.sort(
                key=lambda s: (order.get(str(s["tone"]), 9), s["last_activity"] or 1e12, s["id"])
            )
            chars = int(c.num("feed.text_chars"))
            feed: list[dict[str, Any]] = []
            for e in self.events:
                if e.kind == "held" and not c.flag("feed.show_held"):
                    continue
                level = NOTABLE if e.kind in ("held", "linked") else DECISIONS
                feed.append(
                    {
                        "at": e.at,
                        "ago": round(now - e.at),
                        "session_id": e.session_id,
                        "kind": e.kind,
                        "rung": e.rung,
                        "text": e.detail[:chars],
                        "rules": e.rules,
                        "dry_run": e.dry_run,
                        "ok": e.ok,
                        "links": e.links,
                        "teams": e.teams,
                        "level": level,
                        "decision": True,
                    }
                )
            for a in self.activity:
                feed.append(
                    {
                        "at": a.at,
                        "ago": round(now - a.at),
                        "session_id": a.session_id,
                        "kind": a.kind,
                        "text": a.text[:chars],
                        "ok": a.ok,
                        "level": a.level,
                        "sequence": a.sequence,
                        "decision": False,
                    }
                )
            feed.sort(key=lambda x: -float(x["at"]))
            feed = feed[: int(c.num("feed.max_items"))]
            titles = {sid: (v.title or sid) for sid, v in self.views.items()}
            hour = [e for e in self.events if now - e.at <= 3600]
            return {
                "now": now,
                "dry_run": c.flag("general.dry_run"),
                "broken": self.broken,
                "feed_level": c.get("feed.level"),
                "refresh_seconds": c.num("feed.refresh_seconds"),
                "connections": {
                    "teams": self.notifier.configured and c.flag("teams.enabled"),
                    "judge": self.judge is not None and c.flag("judge.enabled"),
                    "judge_model": c.get("judge.model"),
                    "devin": self.devin is not None,
                    "approval_links": self.signer is not None,
                },
                "totals": {
                    "sessions": len(sessions),
                    "attention": sum(1 for s in sessions if s["tone"] in ("critical", "warning")),
                    "approvals": sum(1 for s in sessions if s["pending_approval"]),
                    "actions_hour": sum(1 for e in hour if e.kind in ("action", "manual")),
                    "acus_at_risk": round(
                        sum(
                            max(0.0, float(s["acus_since_progress"]))
                            for s in sessions
                            if s["tone"] in ("critical", "warning") and s["status"] == "running"
                        ),
                        1,
                    ),
                },
                "rungs": list(RUNGS),
                "enabled_rungs": c.rungs(),
                "sessions": sessions,
                "titles": titles,
                "feed": feed,
                # The decisions in full, for tests and tools; the page uses `feed`.
                "events": [asdict(e) | {"ago": round(now - e.at)} for e in reversed(self.events)],
            }

    def detail(self, sid: str) -> dict[str, Any] | None:
        """Everything about one session, for the detail view."""
        now = self.clock()
        with self.lock:
            view = self.views.get(sid)
            if view is None:
                return None
            session = self._session(sid, view, now)
            session["observations"] = [
                {
                    "at": o.at,
                    "ago": round(now - o.at),
                    "kind": o.kind,
                    "value": o.value,
                    "outcome": o.outcome,
                    "source": o.source,
                }
                for o in view.observations
            ]
            session["entries"] = list(self.entries.get(sid, ()))
            session["decisions"] = [
                asdict(e) | {"ago": round(now - e.at)} for e in self.events if e.session_id == sid
            ]
            session["notices"] = [
                n.as_dict() | {"ago": round(now - n.at)}
                for n in self.notifier.history
                if n.session == sid
            ]
            session["findings_full"] = [
                f.as_dict() | {"nudge": f.nudge} for f in self.findings.get(sid, [])
            ]
            return session
