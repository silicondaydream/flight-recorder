"""Receiving Devin hook events: what the agent does with its own tools.

Devin's hooks (`devin-plugin/`) fire before and after every tool call, including
Devin's built-in shell (`exec`) and editor (`edit`, `write`, `apply_patch`), on
each prompt, and when a session starts or wakes. The plugin's script sends them
to `POST /api/hook`. This module records them: one hash-chained log per hook
session (`hook-<id>.ndjson`) in the log directory, so the engine reads them,
verified, like every other entry.

Recorded actions (`activity` holds the event's fields, redacted and capped):

    hook.post_tool      a tool finished: tool, arguments, output, outcome, exit code
    hook.pre_tool       a tool is about to run (timeline only)
    hook.prompt         a person's message
    hook.session_start  the session started or woke (`source`: startup / resume)
    hook.stop           the agent ended its turn
    hook.compaction     the agent compacted its context
    hook.other          anything else

The chain starts here, on Watchdog's side: the script spools events on Devin's
machine and Watchdog makes them tamper-evident as they arrive.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from agent_watchdog import __version__
from agent_watchdog.chain import Recorder, safe_name
from agent_watchdog.redact import Redactor, keep

MAX_EVENTS = 1000  # per request

# The Devin plugin (`devin-plugin/` in the repository; `watchdog hooks-setup --export`).
HOOK_COMMAND = (
    'python3 "${DEVIN_PLUGIN_ROOT:-${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}}/hooks/watchdog_hook.py"'
)
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "PostCompaction",
)


def plugin_files() -> dict[str, str]:
    """The plugin's files, by path inside the plugin."""
    hooks = {
        event: [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": HOOK_COMMAND,
                        "timeout": 10 if event == "SessionStart" else 5,
                    }
                ],
            }
        ]
        for event in HOOK_EVENTS
    }
    manifest = {
        "name": "watchdog",
        "version": __version__,
        "description": "Sends each session's tool calls, prompts and lifecycle events to "
        "Watchdog, which notices stuck or drifting sessions.",
        "skills": [],
    }
    script = files("agent_watchdog").joinpath("devin_hook.py").read_text(encoding="utf-8")
    return {
        ".devin-plugin/plugin.json": json.dumps(manifest, indent=2) + "\n",
        "hooks.json": json.dumps(hooks, indent=2) + "\n",
        "hooks/watchdog_hook.py": script,
    }


def export_plugin(target: Path) -> list[Path]:
    written = []
    for name, text in plugin_files().items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


ACTIONS = {
    "PostToolUse": "hook.post_tool",
    "PreToolUse": "hook.pre_tool",
    "UserPromptSubmit": "hook.prompt",
    "SessionStart": "hook.session_start",
    "Stop": "hook.stop",
    "PostCompaction": "hook.compaction",
    "PostCompact": "hook.compaction",
}
_EXIT = re.compile(r"(?im)\bexit(?:ed)?(?: with)? (?:code|status)[:= ]+(-?\d+)")


def _when(at: Any) -> str | None:
    if isinstance(at, int | float) and not isinstance(at, bool) and at > 0:
        stamp = datetime.fromtimestamp(float(at), UTC).isoformat(timespec="milliseconds")
        return stamp.replace("+00:00", "Z")
    return None


def outcome(response: Any) -> tuple[str, int | None]:
    """(success / failure / unknown, exit code) from a PostToolUse `tool_response`."""
    if not isinstance(response, dict):
        return "unknown", None
    text = f"{response.get('output') or ''}\n{response.get('error') or ''}"
    code: int | None = None
    for key in ("exit_code", "exitCode", "returncode"):
        if isinstance(response.get(key), int):
            code = int(response[key])
    if code is None:
        found = _EXIT.findall(text)
        code = int(found[-1]) if found else None
    if code is not None:
        return ("success" if code == 0 else "failure"), code
    success = response.get("success")
    if isinstance(success, bool):
        return ("success" if success else "failure"), None
    return ("failure" if response.get("error") else "unknown"), None


class HookStore:
    def __init__(
        self, log_dir: Path, redactor: Redactor | None = None, max_payload: int = 65_536
    ) -> None:
        self.log_dir = log_dir
        self.redactor = redactor or Redactor()
        self.max_payload = max_payload
        self._recorders: dict[str, Recorder] = {}
        self._seen: deque[str] = deque(maxlen=20_000)
        self._seen_set: set[str] = set()
        self._lock = threading.Lock()

    def path(self, session: str) -> Path:
        return self.log_dir / f"hook-{safe_name(session)}.ndjson"

    def add(self, events: list[Any]) -> tuple[int, int, list[str]]:
        """Record events. Returns (accepted, duplicates skipped, hook sessions seen)."""
        if len(events) > MAX_EVENTS:
            raise ValueError(f"at most {MAX_EVENTS} events per request")
        accepted = skipped = 0
        sessions: list[str] = []
        with self._lock:
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("every event must be an object")
                session = str(event.get("session_id") or "")
                if not session:
                    raise ValueError("an event has no session_id")
                eid = str(event.get("id") or "")
                if eid and eid in self._seen_set:
                    skipped += 1
                    continue
                self._record(session, event)
                if eid:
                    if len(self._seen) == self._seen.maxlen:
                        self._seen_set.discard(self._seen[0])
                    self._seen.append(eid)
                    self._seen_set.add(eid)
                accepted += 1
                if session not in sessions:
                    sessions.append(session)
        return accepted, skipped, sessions

    def _kept(self, value: Any) -> dict[str, Any]:
        return keep(self.redactor, value, self.max_payload)

    def _record(self, session: str, event: dict[str, Any]) -> None:
        rec = self._recorders.get(session)
        if rec is None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            rec = Recorder(self.path(session), session_id=session, agent="devin-hook")
            self._recorders[session] = rec
        name = str(event.get("hook_event_name") or "")
        action = ACTIONS.get(name, "hook.other")
        activity: dict[str, Any] = {
            "source": "devin-hook",
            "hook_event": name,
            "event_id": event.get("id"),
            "prompt_id": event.get("prompt_id"),
            "devin_session": event.get("devin_session") or None,
        }
        result: str | None = None
        if "tool_name" in event:
            activity["tool"] = str(event.get("tool_name"))
            activity["arguments"] = self._kept(event.get("tool_input"))
        if action == "hook.post_tool":
            result, code = outcome(event.get("tool_response"))
            activity["exit_code"] = code
            activity["result"] = self._kept(event.get("tool_response"))
        if action == "hook.prompt":
            activity["text"] = self._kept(event.get("prompt"))
        if action == "hook.session_start":
            activity["hook_source"] = event.get("source")
        if action == "hook.stop":
            activity["text"] = self._kept(event.get("last_assistant_message"))
        if action == "hook.compaction":
            activity["text"] = self._kept(event.get("summary"))
        if event.get("tool_provenance") is not None:
            activity["provenance"] = self._kept(event.get("tool_provenance"))
        rec.append(action, outcome=result, timestamp=_when(event.get("at")), activity=activity)

    def close(self) -> None:
        with self._lock:
            for rec in self._recorders.values():
                rec.close()
