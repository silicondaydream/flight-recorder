"""The seven rules. Each is a pure function of a session's view and the time.

A rule only looks at what happened since the later of "one window ago" and the
session's last sign of progress (a test passing, a PR, a person replying), so a
session that recovers stops matching on its own. Rules never act; they return
a `Finding` and the engine decides what, if anything, to do about it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from devin_watchdog.observe import TEST_OR_BUILD, Obs, SessionView, normalise_command, words

RUNGS = ("log", "alert", "nudge", "sleep", "terminate")


@dataclass(frozen=True)
class Thresholds:
    window_minutes: float = 30.0
    failing_command_repeats: int = 3
    error_repeats: int = 3
    edit_repeats: int = 5
    loop_runs: int = 4
    acu_budget: float = 5.0  # ACUs spent with no progress...
    stall_minutes: float = 20.0  # ...over at least this long
    blocker_repeats: int = 2
    drift_messages: int = 6  # this many recent messages...
    drift_off_topic: int = 5  # ...of which this many share nothing with the task


@dataclass(frozen=True)
class Finding:
    rule: str
    summary: str
    evidence: tuple[str, ...] = ()
    severity: str = "medium"  # low / medium / high
    max_rung: str = "terminate"  # how far this finding alone may escalate
    nudge: str = ""  # what to tell the session, if it comes to that
    heuristic: bool = field(default=False)  # read from message text, not exact events

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "severity": self.severity,
            "max_rung": self.max_rung,
            "heuristic": self.heuristic,
        }


Rule = Callable[[SessionView, float, Thresholds], "Finding | None"]


def _since(view: SessionView, now: float, t: Thresholds) -> float:
    return max(now - t.window_minutes * 60, view.last_progress)


def _guessed(obs: list[Obs]) -> bool:
    return all(o.source == "message" for o in obs)


def repeated_failing_commands(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    failures: Counter[str] = Counter()
    sources: dict[str, list[Obs]] = {}
    for obs in view.recent("command", _since(view, now, t)):
        key = normalise_command(obs.value)
        if obs.outcome == "success":
            failures.pop(key, None)
        elif obs.outcome == "failure":
            failures[key] += 1
            sources.setdefault(key, []).append(obs)
    if not failures:
        return None
    command, count = failures.most_common(1)[0]
    if count < t.failing_command_repeats:
        return None
    return Finding(
        "repeated_failing_commands",
        f"`{command}` failed {count} times in a row",
        (command,),
        "high" if count >= 2 * t.failing_command_repeats else "medium",
        nudge=f"`{command}` has failed {count} times the same way. Stop re-running it: "
        "read the error, state the root cause, and try a different approach.",
        heuristic=_guessed(sources[command]),
    )


def repeated_errors(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    errors = view.recent("error", _since(view, now, t))
    counts = Counter(o.value for o in errors)
    if not counts:
        return None
    signature, count = counts.most_common(1)[0]
    if count < t.error_repeats:
        return None
    return Finding(
        "repeated_errors",
        f"the same error {count} times: {signature}",
        (signature,),
        "high" if count >= 2 * t.error_repeats else "medium",
        nudge=f"You have hit the same error {count} times ({signature}). "
        "Summarise what you have tried and why it did not work before changing anything else.",
        heuristic=_guessed([o for o in errors if o.value == signature]),
    )


def edit_thrash(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    edits = view.recent("edit", _since(view, now, t))
    counts = Counter(o.value for o in edits)
    if not counts:
        return None
    path, count = counts.most_common(1)[0]
    if count < t.edit_repeats:
        return None
    return Finding(
        "edit_thrash",
        f"{path} edited {count} times without progress",
        (path,),
        nudge=f"You have edited {path} {count} times without the change sticking. "
        "Revert to the last known-good version and write down the intended change "
        "before editing again.",
        heuristic=_guessed([o for o in edits if o.value == path]),
    )


def test_build_loop(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    runs = [
        o for o in view.recent("command", _since(view, now, t)) if TEST_OR_BUILD.search(o.value)
    ]
    if len(runs) < t.loop_runs or any(o.outcome == "success" for o in runs):
        return None
    failed = sum(o.outcome == "failure" for o in runs)
    if failed < len(runs) // 2:
        return None
    names = tuple(dict.fromkeys(normalise_command(o.value) for o in runs))[:3]
    return Finding(
        "test_build_loop",
        f"{len(runs)} test/build runs, {failed} failed, none passed",
        names,
        nudge="You are in a test/build loop with no passing run. Run the single smallest "
        "failing test, read its full output, and fix that one failure first.",
        heuristic=_guessed(runs),
    )


def acus_without_progress(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    if not view.active or not view.acus:
        return None
    spent = view.acus_now - view.acus_at_progress
    idle = now - view.last_progress
    if spent < t.acu_budget or idle < t.stall_minutes * 60:
        return None
    return Finding(
        "acus_without_progress",
        f"{spent:.1f} ACUs over {idle / 60:.0f} min with no progress "
        "(no passing test, PR or reply)",
        (f"acus_consumed {view.acus_at_progress:.1f} -> {view.acus_now:.1f}",),
        "high" if spent >= 2 * t.acu_budget else "medium",
        nudge=f"You have used {spent:.1f} ACUs since the last visible progress. Post a short "
        "status: what is done, what is blocking you, and what you will try next.",
    )


def environment_blocker(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    blockers = view.recent("blocker", _since(view, now, t))
    counts = Counter(o.value for o in blockers)
    if len(blockers) < t.blocker_repeats:
        return None
    top = tuple(b for b, _ in counts.most_common(3))
    return Finding(
        "environment_blocker",
        f"blocked by the environment: {', '.join(top)}",
        top,
        "high",
        nudge="This looks like an environment problem (" + ", ".join(top) + "), not a code "
        "problem. Stop retrying, and ask for the access, secret or setup you need.",
        heuristic=_guessed(blockers),
    )


def scope_drift(view: SessionView, now: float, t: Thresholds) -> Finding | None:
    messages = view.messages()
    recent = [m for m in messages if m.at >= now - t.window_minutes * 60][-t.drift_messages :]
    if len(recent) < t.drift_messages:
        return None
    # The task as given, plus Devin's first reply (its plan), is what is on topic.
    vocabulary = words(view.task) | words(view.title)
    for m in messages[:1]:
        vocabulary |= words(m.value)
    if len(vocabulary) < 3:
        return None
    off = [m for m in recent if not (words(m.value) & vocabulary)]
    if len(off) < t.drift_off_topic:
        return None
    topics = Counter(w for m in off for w in words(m.value)).most_common(4)
    return Finding(
        "scope_drift",
        f"{len(off)} of the last {len(recent)} messages share nothing with the task",
        tuple(w for w, _ in topics),
        "low",
        max_rung="nudge",  # drift alone is never a reason to stop a session
        nudge=f"Your recent work ({', '.join(w for w, _ in topics)}) looks unrelated to the "
        f"task: {view.task[:200]!r}. Confirm it is needed, or return to the task.",
        heuristic=True,
    )


RULES: tuple[Rule, ...] = (
    repeated_failing_commands,
    repeated_errors,
    edit_thrash,
    test_build_loop,
    acus_without_progress,
    environment_blocker,
    scope_drift,
)


def evaluate(view: SessionView, now: float, t: Thresholds) -> list[Finding]:
    return [f for rule in RULES if (f := rule(view, now, t)) is not None]
