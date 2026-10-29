"""Judging and escalating: rules in, at most one step up the ladder out.

Escalation, per session: log → alert (Teams) → nudge → sleep → terminate.
While a session keeps matching rules, it moves up one rung per cooldown. It
goes back to the start once it has matched nothing for `reset_after`.

Guardrails, in the order they are checked:

- the session must be running (a suspended one is never nudged: Devin's API
  wakes a session up to deliver a message);
- a `watchdog:off` tag stops everything but the log;
- a cooldown between rungs;
- a finding's own ceiling (scope drift alone stops at a nudge);
- no nudge, sleep or terminate while the session waits on a human
  (`waiting_for_user`, `waiting_for_approval`), only alerts;
- the optional judge can veto a step;
- terminate needs a person's approval, given within `approval_ttl`;
- DRY_RUN: nothing is sent to Devin (alerts still go out, marked dry run).

Everything Watchdog decides is written to its own hash-chained log, one per
session, beside the session's log: what fired, what it did, and why not when
it held back.
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

from devin_watchdog.actions import DevinActions, Teams
from devin_watchdog.judge import Judge, Verdict
from devin_watchdog.observe import NUDGE_MARK, Entry, SessionView
from devin_watchdog.rules import RUNGS, Finding, Thresholds, evaluate
from flight_recorder.chain import Recorder, safe_name
from flight_recorder.importers.devin import DevinError

LOG, ALERT, NUDGE, SLEEP, TERMINATE = range(len(RUNGS))


@dataclass(frozen=True)
class Settings:
    dry_run: bool = True
    cooldown: float = 600.0  # seconds between rungs, per session
    reset_after: float = 1800.0  # this long with no findings: back to the start
    approval_ttl: float = 3600.0
    off_tag: str = "watchdog:off"
    judge_min_confidence: float = 0.6
    thresholds: Thresholds = field(default_factory=Thresholds)


@dataclass
class Ladder:
    rung: int = -1  # -1: nothing done yet
    last_action: float = 0.0
    last_finding: float = 0.0
    pending: bool = False  # terminate waits on approval
    approved_at: float = 0.0
    approved_by: str = ""


class State:
    """Where each session is on the ladder. Survives restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS ladder (session_id TEXT PRIMARY KEY, rung INTEGER, "
            "last_action REAL, last_finding REAL, pending INTEGER, approved_at REAL, "
            "approved_by TEXT)"
        )
        self._db.commit()

    def get(self, session_id: str) -> Ladder:
        with self._lock:
            row = self._db.execute(
                "SELECT rung, last_action, last_finding, pending, approved_at, approved_by "
                "FROM ladder WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return Ladder()
        return Ladder(row[0], row[1], row[2], bool(row[3]), row[4], row[5] or "")

    def put(self, session_id: str, ladder: Ladder) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO ladder VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE "
                "SET rung = excluded.rung, last_action = excluded.last_action, "
                "last_finding = excluded.last_finding, pending = excluded.pending, "
                "approved_at = excluded.approved_at, approved_by = excluded.approved_by",
                (
                    session_id,
                    ladder.rung,
                    ladder.last_action,
                    ladder.last_finding,
                    int(ladder.pending),
                    ladder.approved_at,
                    ladder.approved_by,
                ),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class Event:
    at: float
    session_id: str
    kind: str  # action / held / judged / approval / recovered / error
    rung: str = ""
    detail: str = ""
    rules: list[str] = field(default_factory=list)
    dry_run: bool = False
    ok: bool = True


class Watchdog:
    def __init__(
        self,
        settings: Settings,
        state: State,
        *,
        devin: DevinActions | None = None,
        teams: Teams | None = None,
        judge: Judge | None = None,
        record_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.state = state
        self.devin = devin
        self.teams = teams
        self.judge = judge
        self.record_dir = record_dir
        self.clock = clock
        self.views: dict[str, SessionView] = {}
        self.findings: dict[str, list[Finding]] = {}
        self.held: dict[str, str] = {}  # why the last step was not taken
        self.verdicts: dict[str, tuple[frozenset[str], float, Verdict]] = {}
        self.events: deque[Event] = deque(maxlen=200)
        self.broken: str = ""  # a log failed verification: Watchdog stops acting
        self.lock = threading.RLock()
        self._recorders: dict[str, Recorder] = {}

    # -- input -----------------------------------------------------------------

    def observe(self, entry: Entry) -> None:
        if entry.get("agent", {}).get("name") == "watchdog":
            return
        sid = str(entry.get("session", {}).get("id", ""))
        if not sid:
            return
        with self.lock:
            self.views.setdefault(sid, SessionView(sid)).add(entry)

    def chain_broken(self, reason: str) -> None:
        with self.lock:
            self.broken = reason
            self._event(
                Event(self.clock(), "-", "error", detail=f"log failed verification: {reason}")
            )
        self._safe_alert("Watchdog stopped: a session log failed verification", {"reason": reason})

    # -- the loop --------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            if self.broken:
                return
            for sid, view in list(self.views.items()):
                self._step(sid, view, now)

    def _step(self, sid: str, view: SessionView, now: float) -> None:
        s = self.settings
        ladder = self.state.get(sid)
        if ladder.pending and ladder.approved_at and now - ladder.approved_at <= s.approval_ttl:
            self._terminate(sid, view, ladder, now)
            return
        findings = evaluate(view, now, s.thresholds)
        self.findings[sid] = findings
        if not findings:
            self.held.pop(sid, None)
            if ladder.rung >= 0 and now - ladder.last_finding > s.reset_after:
                self._event(Event(now, sid, "recovered", RUNGS[ladder.rung], "no findings; reset"))
                self._record(
                    sid, "watchdog.recovered", "success", {"from_rung": RUNGS[ladder.rung]}
                )
                self.state.put(sid, Ladder())
            return
        ladder.last_finding = now
        self.state.put(sid, ladder)

        rules = [f.rule for f in findings]
        if not view.active:
            return self._hold(sid, "session is not running", now, rules)
        if s.off_tag in view.tags:
            return self._hold(sid, f"tagged {s.off_tag}", now, rules)
        if ladder.pending:
            return self._hold(sid, "terminate is waiting for approval", now, rules)
        wait = ladder.last_action + s.cooldown - now
        if ladder.last_action and wait > 0:
            return self._hold(sid, f"cooldown, {wait:.0f}s left", now, rules, quiet=True)

        ceiling = max(RUNGS.index(f.max_rung) for f in findings)
        reason = f"ceiling for {', '.join(rules)}"
        if view.waiting_on_human and ceiling > ALERT:
            ceiling, reason = ALERT, "waiting on a human: alerts only"
        if not view.is_devin and ceiling > ALERT:
            ceiling, reason = ALERT, "not a Devin session: alerts only"
        step = ladder.rung + 1
        if step > ceiling:
            return self._hold(sid, f"held at {RUNGS[ladder.rung]} ({reason})", now, rules)

        verdict = self._judge(sid, view, findings, now)
        if (
            verdict is not None
            and not verdict.stuck
            and verdict.confidence >= s.judge_min_confidence
        ):
            ladder.last_action = now  # don't ask again until the cooldown is over
            self.state.put(sid, ladder)
            self._event(
                Event(now, sid, "judged", RUNGS[step], f"not stuck: {verdict.reason}", rules)
            )
            self._record(
                sid, "watchdog.judged", "success", {"verdict": verdict.as_dict(), "rules": rules}
            )
            return

        if step == TERMINATE:
            ladder.pending = True
            self.state.put(sid, ladder)
            self._event(Event(now, sid, "approval", "terminate", "approval requested", rules))
            self._record(sid, "watchdog.approval_requested", None, {"rules": rules})
            self._safe_alert(
                f"Approve terminating {view.title or sid}?",
                {"session": sid, "rules": ", ".join(rules), "approve": f"watchdog approve {sid}"},
                link=view.url,
            )
            return
        self._act(sid, view, ladder, step, findings, verdict, now)

    def _hold(
        self, sid: str, why: str, now: float, rules: list[str], *, quiet: bool = False
    ) -> None:
        if self.held.get(sid) == why:
            return  # said once is enough
        self.held[sid] = why
        if not quiet:
            self._event(Event(now, sid, "held", detail=why, rules=rules))
            self._record(sid, "watchdog.held", None, {"reason": why, "rules": rules})

    def _judge(
        self, sid: str, view: SessionView, findings: list[Finding], now: float
    ) -> Verdict | None:
        if self.judge is None:
            return None
        key = frozenset(f.rule for f in findings)
        cached = self.verdicts.get(sid)
        if cached and cached[0] == key and now - cached[1] < self.settings.cooldown:
            return cached[2]
        verdict = self.judge.review(view, findings)
        if verdict is not None:
            self.verdicts[sid] = (key, now, verdict)
        return verdict

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
        dry = self.settings.dry_run
        worst = max(findings, key=lambda f: ("low", "medium", "high").index(f.severity))
        message = f"{NUDGE_MARK} {(verdict.nudge if verdict and verdict.nudge else worst.nudge)}"
        detail, ok = rung, True
        try:
            if step == ALERT:
                detail = self._alert(
                    f"{'[dry run] ' if dry else ''}Devin session looks stuck: {view.title or sid}",
                    {
                        "session": sid,
                        "rules": ", ".join(rules),
                        "why": worst.summary,
                        "acus": f"{view.acus_now:.1f}",
                    },
                    link=view.url,
                )
            elif step == NUDGE:
                detail = f"sent: {message}"
                if not dry:
                    self._devin().nudge(sid, message)
            elif step == SLEEP:
                detail = "archived (asleep) and tagged watchdog:slept"
                if not dry:
                    self._devin().sleep(sid)
            else:
                detail = "; ".join(f.summary for f in findings)
        except DevinError as exc:
            detail, ok = str(exc), False
        if dry and step in (NUDGE, SLEEP):
            detail = "[dry run] would have " + detail.replace("sent:", "sent")
        if ok:
            ladder.rung, ladder.last_action = step, now
            self.state.put(sid, ladder)
        self.held.pop(sid, None)
        self._event(Event(now, sid, "action", rung, detail, rules, dry, ok))
        self._record(
            sid,
            f"watchdog.{rung}",
            "success" if ok else "failure",
            {
                "rung": rung,
                "dry_run": dry,
                "detail": detail,
                "findings": [f.as_dict() for f in findings],
                "verdict": verdict.as_dict() if verdict else None,
                "acus_consumed": view.acus_now,
            },
        )

    def _terminate(self, sid: str, view: SessionView, ladder: Ladder, now: float) -> None:
        dry = self.settings.dry_run
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
        self._event(Event(now, sid, "action", "terminate", detail, dry_run=dry, ok=ok))
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
            self._event(Event(self.clock(), sid, "approval", "terminate", f"approved by {by}"))
            self._record(sid, "watchdog.approved", "success", {"by": by})
            return True

    def _safe_alert(self, title: str, facts: dict[str, str], *, link: str = "") -> None:
        try:
            self._alert(title, facts, link=link)
        except DevinError as exc:
            self._event(
                Event(self.clock(), facts.get("session", "-"), "error", detail=str(exc), ok=False)
            )

    def _devin(self) -> DevinActions:
        if self.devin is None:
            raise DevinError("no Devin client: set DEVIN_API_KEY")
        return self.devin

    def _alert(self, title: str, facts: dict[str, str], *, link: str = "") -> str:
        if self.teams is None:
            return "alert logged (no Teams webhook configured)"
        self.teams.post(title, facts, link=link)
        return "alert posted to Teams"

    # -- the record ------------------------------------------------------------

    def _event(self, event: Event) -> None:
        self.events.append(event)

    def _record(self, sid: str, action: str, outcome: str | None, flight: dict[str, Any]) -> None:
        if self.record_dir is None:
            return
        rec = self._recorders.get(sid)
        if rec is None:
            path = self.record_dir / f"watchdog-{safe_name(sid)}.ndjson"
            rec = self._recorders[sid] = Recorder(path, session_id=sid, agent="watchdog")
        rec.append(action, outcome=outcome, flight={"source": "watchdog", **flight})

    def close(self) -> None:
        for rec in self._recorders.values():
            rec.close()
        for client in (self.devin, self.teams):
            if client is not None:
                client.close()
        self.state.close()

    # -- what the dashboard shows ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            sessions: list[dict[str, Any]] = []
            for sid, view in self.views.items():
                ladder = self.state.get(sid)
                findings = self.findings.get(sid, [])
                kinds: dict[str, int] = {}
                for obs in view.observations:
                    kinds[obs.kind] = kinds.get(obs.kind, 0) + 1
                sessions.append(
                    {
                        "id": sid,
                        "title": view.title or sid,
                        "url": view.url,
                        "agent": view.agent,
                        "task": view.task[:300],
                        "status": view.status,
                        "status_detail": view.status_detail,
                        "tags": view.tags,
                        "acus": round(view.acus_now, 2),
                        "acus_series": [[round(t - now), round(a, 2)] for t, a in view.acus[-60:]],
                        "acus_since_progress": round(view.acus_now - view.acus_at_progress, 2),
                        "minutes_since_progress": round((now - view.last_progress) / 60, 1)
                        if view.last_progress
                        else None,
                        "rung": ladder.rung,
                        "rung_name": RUNGS[ladder.rung] if ladder.rung >= 0 else "watching",
                        "pending_approval": ladder.pending,
                        "held": self.held.get(sid, ""),
                        "off": self.settings.off_tag in view.tags,
                        "waiting_on_human": view.waiting_on_human,
                        "findings": [f.as_dict() for f in findings],
                        "counts": kinds,
                        "recent": [
                            {"from": o.source, "text": o.value[:240], "ago": round(now - o.at)}
                            for o in [o for o in view.observations if o.kind == "message"][-4:]
                        ],
                    }
                )
            sessions.sort(key=lambda x: (-int(x["rung"]), -len(x["findings"]), str(x["id"])))
            return {
                "now": now,
                "dry_run": self.settings.dry_run,
                "broken": self.broken,
                "settings": {
                    "cooldown": self.settings.cooldown,
                    "reset_after": self.settings.reset_after,
                    "off_tag": self.settings.off_tag,
                    "judge": getattr(self.judge, "model", None),
                    "teams": self.teams is not None,
                    "thresholds": asdict(self.settings.thresholds),
                },
                "rungs": list(RUNGS),
                "sessions": sessions,
                "events": [asdict(e) | {"ago": round(now - e.at)} for e in reversed(self.events)],
            }
