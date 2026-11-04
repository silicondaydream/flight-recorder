"""Every setting Watchdog has: one schema, validated, saved to disk, applied live.

The dashboard's settings panel is generated from `FIELDS`, so a setting added
here shows up there with its label, help text, unit and limits. Values are
saved as JSON (`settings.json` in the state directory) and read on every tick,
so a change takes effect within seconds, without a restart.

Secrets are never settings: API keys, the webhook URL and tokens stay in the
environment, and the panel only says whether each one is set.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_watchdog.rules import RUNGS, Thresholds

RULES = (
    "repeated_failing_commands",
    "repeated_errors",
    "edit_thrash",
    "test_build_loop",
    "acus_without_progress",
    "environment_blocker",
    "scope_drift",
)


@dataclass(frozen=True)
class Field:
    key: str
    kind: str  # bool / int / float / str / choice
    default: Any
    label: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "default": self.default,
            "label": self.label,
            "help": self.help,
            "min": self.min,
            "max": self.max,
            "choices": [list(c) for c in self.choices],
            "unit": self.unit,
            "group": self.key.split(".", 1)[0],
        }


RUNG_CHOICES = tuple((r, r.capitalize()) for r in RUNGS)

GROUPS: tuple[tuple[str, str, str], ...] = (
    ("general", "General", "Acting or rehearsing, how often to look, which sessions to watch."),
    ("ladder", "Escalation", "The steps Watchdog takes for a stuck session, and how fast."),
    ("rules", "Rules", "What counts as stuck. Each rule can be turned off or capped."),
    ("teams", "Teams", "What reaches the Teams channel, how often, and how much detail."),
    ("feed", "Live log", "What the dashboard's live log shows, and how fast it refreshes."),
    ("judge", "AI judge", "An optional second opinion from an OpenAI model before each step."),
)

_RULE_LABELS = {
    "repeated_failing_commands": "Repeated failing command",
    "repeated_errors": "Repeated error",
    "edit_thrash": "Edit thrash",
    "test_build_loop": "Test/build loop",
    "acus_without_progress": "ACUs without progress",
    "environment_blocker": "Environment blocker",
    "scope_drift": "Scope drift",
}


def _rule_fields() -> list[Field]:
    out = [
        Field(
            "rules.window_minutes",
            "float",
            30.0,
            "Look-back window",
            "Rules only consider activity this recent (and after the last sign of progress).",
            1,
            1440,
            unit="min",
        ),
        Field(
            "rules.use_message_findings",
            "bool",
            True,
            "Use findings read from messages",
            "Commands and errors guessed from what the agent says. "
            "Off: only exact MCP events count.",
        ),
    ]
    tuned: dict[str, list[Field]] = {
        "repeated_failing_commands": [
            Field(
                "rules.repeated_failing_commands.repeats",
                "int",
                3,
                "Failures in a row",
                "The same command failing this many times with no success in between.",
                2,
                50,
            )
        ],
        "repeated_errors": [
            Field(
                "rules.repeated_errors.repeats",
                "int",
                3,
                "Same error, times",
                "The same error (numbers and paths ignored) this many times.",
                2,
                50,
            )
        ],
        "edit_thrash": [
            Field(
                "rules.edit_thrash.repeats",
                "int",
                5,
                "Edits to one file",
                "The same file edited this many times without progress.",
                2,
                100,
            )
        ],
        "test_build_loop": [
            Field(
                "rules.test_build_loop.runs",
                "int",
                4,
                "Test/build runs",
                "This many test or build runs with none passing.",
                2,
                100,
            )
        ],
        "acus_without_progress": [
            Field(
                "rules.acus_without_progress.acus",
                "float",
                5.0,
                "ACUs spent",
                "ACUs spent since the last progress (a passing test, a PR, a person replying).",
                0.5,
                500,
                unit="ACU",
            ),
            Field(
                "rules.acus_without_progress.minutes",
                "float",
                20.0,
                "Over at least",
                "…and at least this long since the last progress.",
                1,
                1440,
                unit="min",
            ),
        ],
        "environment_blocker": [
            Field(
                "rules.environment_blocker.mentions",
                "int",
                2,
                "Mentions",
                "Permission denied, missing credentials, command not found… this many times.",
                1,
                50,
            )
        ],
        "scope_drift": [
            Field(
                "rules.scope_drift.messages",
                "int",
                6,
                "Recent messages",
                "How many of the agent's recent messages to compare with the task.",
                3,
                50,
            ),
            Field(
                "rules.scope_drift.off_topic",
                "int",
                5,
                "Of which off-topic",
                "How many of those may share nothing with the task before it counts.",
                1,
                50,
            ),
        ],
    }
    for rule in RULES:
        label = _RULE_LABELS[rule]
        out.append(Field(f"rules.{rule}.enabled", "bool", True, label, "Turn this rule on or off."))
        out.extend(tuned[rule])
        out.append(
            Field(
                f"rules.{rule}.ceiling",
                "choice",
                "nudge" if rule == "scope_drift" else "terminate",
                "Highest step",
                "The furthest this rule alone can escalate a session.",
                choices=RUNG_CHOICES,
            )
        )
    return out


FIELDS: tuple[Field, ...] = (
    # -- general
    Field(
        "general.dry_run",
        "bool",
        True,
        "Dry run",
        "Decide and report, but send nothing to Devin. Teams cards still go out, marked dry run.",
    ),
    Field(
        "general.poll_seconds",
        "float",
        30.0,
        "Check Devin every",
        "How often sessions and messages are fetched from Devin's API.",
        2,
        600,
        unit="s",
    ),
    Field(
        "general.tick_seconds",
        "float",
        2.0,
        "Evaluate rules every",
        "How often the rules run over what has been recorded.",
        1,
        60,
        unit="s",
    ),
    Field(
        "general.session_tags",
        "str",
        "",
        "Only sessions tagged",
        "Comma-separated Devin tags. Empty: every session in the organization.",
    ),
    Field(
        "general.off_tag",
        "str",
        "watchdog:off",
        "Opt-out tag",
        "Sessions with this Devin tag are recorded but never acted on.",
    ),
    # -- ladder
    Field(
        "ladder.cooldown_seconds",
        "float",
        600.0,
        "Wait between steps",
        "A stuck session moves up one step per this interval.",
        5,
        86400,
        unit="s",
    ),
    Field(
        "ladder.reset_after_seconds",
        "float",
        1800.0,
        "Start over after",
        "This long with no findings and a session goes back to the start.",
        10,
        86400,
        unit="s",
    ),
    Field("ladder.alert", "bool", True, "Step: alert", "Notify Teams. Off: skipped."),
    Field("ladder.nudge", "bool", True, "Step: nudge", "Send the session a message. Off: skipped."),
    Field("ladder.sleep", "bool", True, "Step: sleep", "Put the session to sleep. Off: skipped."),
    Field(
        "ladder.terminate",
        "bool",
        True,
        "Step: terminate",
        "Ask a person to approve terminating. Off: Watchdog stops at the step before.",
    ),
    Field(
        "ladder.nudge_channel",
        "choice",
        "message",
        "Deliver nudges",
        "As a chat message, or as a note the agent reads on its next tool call "
        "(needs Watchdog's Devin hooks; falls back to a message for sessions without them).",
        choices=(("message", "Chat message"), ("hook", "Note via hooks"), ("both", "Both")),
    ),
    Field(
        "ladder.waiting_on_human",
        "choice",
        "alert",
        "While waiting on a person",
        "A session waiting for a reply is never nudged. How far it may go instead.",
        choices=(("log", "Log only"), ("alert", "Alert, no more")),
    ),
    Field(
        "ladder.approval_minutes",
        "float",
        60.0,
        "Approval links valid for",
        "How long the Teams approval buttons work.",
        5,
        1440,
        unit="min",
    ),
    *_rule_fields(),
    # -- teams
    Field(
        "teams.enabled",
        "bool",
        True,
        "Send Teams cards",
        "Needs TEAMS_WEBHOOK_URL. Off: everything is still on the dashboard.",
    ),
    Field(
        "teams.verbosity",
        "choice",
        "standard",
        "Card detail",
        "Compact: one line. Standard: the key facts. Detailed: evidence and recent activity.",
        choices=(("compact", "Compact"), ("standard", "Standard"), ("detailed", "Detailed")),
    ),
    Field(
        "teams.include_agent_text",
        "bool",
        False,
        "Include agent messages",
        "Detailed cards show the agent's last messages. Off keeps session content out of Teams.",
    ),
    Field(
        "teams.per_session_seconds",
        "float",
        300.0,
        "At most one card per session every",
        "Approval requests and errors always go straight through.",
        0,
        86400,
        unit="s",
    ),
    Field(
        "teams.max_per_hour",
        "int",
        30,
        "At most, per hour",
        "Across all sessions. Extra cards are dropped and counted on the next one.",
        1,
        1000,
    ),
    Field(
        "teams.digest_minutes",
        "float",
        0.0,
        "Batch into a digest every",
        "0: send each card as it happens. Otherwise non-urgent cards are combined.",
        0,
        1440,
        unit="min",
    ),
    Field(
        "teams.in_dry_run",
        "bool",
        True,
        "Send cards in dry run",
        "Rehearse the notifications too. Cards say dry run.",
    ),
    Field("teams.on_log", "bool", False, "When: a problem is first logged", ""),
    Field("teams.on_alert", "bool", True, "When: the alert step", "The ladder's alert step."),
    Field("teams.on_nudge", "bool", True, "When: a session is nudged", ""),
    Field("teams.on_sleep", "bool", True, "When: a session is put to sleep", ""),
    Field("teams.on_terminate", "bool", True, "When: a session is terminated", ""),
    Field(
        "teams.on_approval",
        "bool",
        True,
        "When: approval is needed",
        "The card with the Approve / Keep running buttons.",
    ),
    Field("teams.on_decision", "bool", True, "When: someone approves or declines", ""),
    Field("teams.on_manual", "bool", True, "When: someone acts from the dashboard", ""),
    Field("teams.on_recovered", "bool", False, "When: a session recovers", ""),
    Field(
        "teams.on_held",
        "bool",
        False,
        "When: Watchdog holds back",
        "A guardrail stopped a step (opt-out tag, waiting on a person, a step's ceiling).",
    ),
    Field("teams.on_judge_veto", "bool", False, "When: the AI judge says not stuck", ""),
    Field("teams.on_session_start", "bool", False, "When: a session starts", ""),
    Field("teams.on_session_end", "bool", False, "When: a session ends or finishes", ""),
    Field("teams.on_runtime", "bool", True, "When: a session runs long", ""),
    Field(
        "teams.runtime_minutes",
        "float",
        60.0,
        "Long-running after",
        "Once per session.",
        1,
        10080,
        unit="min",
    ),
    Field("teams.on_acus", "bool", True, "When: a session's spend passes", ""),
    Field(
        "teams.acus_threshold",
        "float",
        20.0,
        "Spend threshold",
        "Once per session.",
        0.5,
        10000,
        unit="ACU",
    ),
    Field("teams.on_waiting", "bool", False, "When: a session waits on a person", ""),
    Field(
        "teams.waiting_minutes",
        "float",
        15.0,
        "Waiting for",
        "Once per wait.",
        1,
        1440,
        unit="min",
    ),
    Field(
        "teams.on_errors",
        "bool",
        True,
        "When: Watchdog hits an error",
        "A log that fails verification, a failed API call.",
    ),
    # -- live log
    Field(
        "feed.level",
        "choice",
        "notable",
        "Show",
        "Decisions: what Watchdog did. Notable: plus findings and status changes. "
        "Everything: plus every agent message, tool call and result.",
        choices=(("decisions", "Decisions"), ("notable", "Notable"), ("everything", "Everything")),
    ),
    Field("feed.max_items", "int", 200, "Keep", "Entries in the live log.", 20, 2000, unit="items"),
    Field("feed.refresh_seconds", "float", 2.0, "Refresh every", "", 0.5, 60, unit="s"),
    Field("feed.show_held", "bool", True, "Show held-back decisions", ""),
    Field("feed.text_chars", "int", 280, "Trim text to", "", 40, 5000, unit="chars"),
    Field(
        "feed.detail_entries",
        "int",
        500,
        "Session detail keeps",
        "Recorded entries per session for the detail view (applies to new sessions).",
        50,
        5000,
        unit="entries",
    ),
    Field(
        "feed.hide_ended_minutes",
        "float",
        120.0,
        "Hide ended sessions after",
        "0: never hide.",
        0,
        10080,
        unit="min",
    ),
    # -- judge
    Field(
        "judge.enabled",
        "bool",
        False,
        "Ask the AI judge",
        "Needs OPENAI_API_KEY (a ChatGPT Enterprise or OpenAI API project key).",
    ),
    Field(
        "judge.model",
        "str",
        "gpt-5",
        "Model",
        "Any chat model your OpenAI organization can use.",
    ),
    Field(
        "judge.reasoning_effort",
        "choice",
        "",
        "Reasoning effort",
        "For reasoning models only. Default: the model's own.",
        choices=(("", "Default"), ("low", "Low"), ("medium", "Medium"), ("high", "High")),
    ),
    Field(
        "judge.from_rung",
        "choice",
        "nudge",
        "Ask before steps from",
        "Steps below this go ahead on the rules alone.",
        choices=RUNG_CHOICES,
    ),
    Field(
        "judge.min_confidence",
        "float",
        0.6,
        "Hold back when 'not stuck' at",
        "The judge's confidence needed to stop a step.",
        0,
        1,
    ),
    Field("judge.max_calls_per_hour", "int", 60, "At most, per hour", "", 1, 10000, unit="calls"),
)

BY_KEY = {f.key: f for f in FIELDS}


class SettingsError(ValueError):
    pass


def _coerce(field: Field, value: Any) -> Any:
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
            return value.lower() in ("true", "1")
        raise SettingsError(f"{field.label}: expected on or off")
    if field.kind in ("int", "float"):
        if isinstance(value, bool):
            raise SettingsError(f"{field.label}: expected a number")
        try:
            number = int(value) if field.kind == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{field.label}: expected a number") from None
        if field.kind == "int" and float(value) != number:
            raise SettingsError(f"{field.label}: expected a whole number")
        if field.min is not None and number < field.min:
            raise SettingsError(f"{field.label}: at least {field.min:g}")
        if field.max is not None and number > field.max:
            raise SettingsError(f"{field.label}: at most {field.max:g}")
        return number
    if field.kind == "choice":
        if value not in [c[0] for c in field.choices]:
            raise SettingsError(f"{field.label}: not one of the choices")
        return value
    if not isinstance(value, str) or len(value) > 500:
        raise SettingsError(f"{field.label}: expected text")
    return value.strip()


class Config:
    """The current settings. Thread-safe; `get` is cheap enough to call every tick.

    `values` are command-line overrides (`--live`, `--cooldown`…): they win over
    the saved file for this run, are never saved, and a change made in the
    settings panel replaces them.
    """

    def __init__(self, path: Path | None = None, values: dict[str, Any] | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._values = {f.key: f.default for f in FIELDS}
        self._overrides: dict[str, Any] = {}
        if path is not None and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}
            for key, value in (saved if isinstance(saved, dict) else {}).items():
                if key in BY_KEY:  # a value from an older version is dropped
                    with contextlib.suppress(SettingsError):
                        self._values[key] = _coerce(BY_KEY[key], value)
        for key, value in (values or {}).items():
            if key not in BY_KEY:
                raise SettingsError(f"unknown setting {key!r}")
            self._overrides[key] = _coerce(BY_KEY[key], value)

    def get(self, key: str) -> Any:
        with self._lock:
            return self._overrides.get(key, self._values[key])

    def flag(self, key: str) -> bool:
        return bool(self.get(key))

    def num(self, key: str) -> float:
        return float(self.get(key))

    def values(self) -> dict[str, Any]:
        with self._lock:
            return {**self._values, **self._overrides}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate all, then apply all and save. Raises SettingsError naming the field."""
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in BY_KEY:
                raise SettingsError(f"unknown setting {key!r}")
            clean[key] = _coerce(BY_KEY[key], value)
        with self._lock:
            for key in clean:
                self._overrides.pop(key, None)
            self._values.update(clean)
            saved = dict(self._values)
            current = {**self._values, **self._overrides}
        self._save(saved)
        return current

    def _save(self, values: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        changed = {k: v for k, v in values.items() if v != BY_KEY[k].default}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(changed, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- what the engine reads ---------------------------------------------------------

    def thresholds(self) -> Thresholds:
        g = self.num
        return Thresholds(
            window_minutes=g("rules.window_minutes"),
            failing_command_repeats=int(g("rules.repeated_failing_commands.repeats")),
            error_repeats=int(g("rules.repeated_errors.repeats")),
            edit_repeats=int(g("rules.edit_thrash.repeats")),
            loop_runs=int(g("rules.test_build_loop.runs")),
            acu_budget=g("rules.acus_without_progress.acus"),
            stall_minutes=g("rules.acus_without_progress.minutes"),
            blocker_repeats=int(g("rules.environment_blocker.mentions")),
            drift_messages=int(g("rules.scope_drift.messages")),
            drift_off_topic=int(g("rules.scope_drift.off_topic")),
        )

    def rungs(self) -> list[int]:
        """The ladder's enabled steps, as indexes into RUNGS. Log is always on."""
        on = [0]
        for i, rung in enumerate(RUNGS[1:], start=1):
            if self.flag(f"ladder.{rung}"):
                on.append(i)
        return on

    def tags(self) -> list[str]:
        return [t.strip() for t in str(self.get("general.session_tags")).split(",") if t.strip()]


def schema() -> dict[str, Any]:
    return {
        "groups": [{"id": g, "title": t, "description": d} for g, t, d in GROUPS],
        "fields": [f.as_dict() for f in FIELDS],
    }
