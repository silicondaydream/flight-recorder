#!/usr/bin/env python3
"""Watchdog's Devin hook: what the agent does, sent to Watchdog as it happens.

Devin runs this on its session machine for each hook event (`hooks.json`): before
and after every tool call, including its own shell (`exec`) and editor (`edit`,
`write`, `apply_patch`), when a person sends a message, when the session starts
or wakes, and when the agent stops its turn. The event arrives as JSON on stdin.

For each event this script:

1. redacts secrets (the same patterns Watchdog uses) and caps large outputs;
2. appends the event to a local spool (`~/.watchdog-hooks/spool.jsonl`), so
   nothing is lost if Watchdog is unreachable;
3. sends whatever is unsent to Watchdog (`WATCHDOG_HOOK_URL`, with
   `WATCHDOG_INGEST_TOKEN`) with a short timeout. After a failure it waits 20
   seconds before trying again, so a Watchdog outage costs Devin nothing;
4. if Watchdog has a note for the session (a nudge sent through hooks), prints it
   as `additionalContext`, which Devin reads on its next step.

It never blocks or changes a tool call, and it always exits 0: a hook that fails
must not stop the session. Standard library only, so it needs nothing installed
beyond python3.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # not on Linux or macOS: run without file locks
    fcntl = None  # type: ignore[assignment]

VERSION = "0.2.0"
MAX_TEXT = 20_000  # characters of a tool's output kept (the tail)
MAX_INPUT = 2_000_000  # bytes read from stdin
BATCH = 200  # events per request
TIMEOUT = 2.0  # seconds per request
BACKOFF = 20.0  # seconds to wait after a failed send
SPOOL_LIMIT = 5_000_000  # bytes: a fully-sent spool larger than this is truncated

# The same as Watchdog's redact.py (a test keeps them identical).
BUILTIN_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("api-key", r"\bsk-(?:[A-Za-z]+-)?[A-Za-z0-9_-]{20,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ("bearer", r"(?i)(?P<keep>\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    (
        "assignment",
        r"(?i)(?P<keep>\b(?:password|passwd|secret|token|api_key|apikey|access_key)[=:])"
        r"[^\s\"'&]{6,}",
    ),
)
SENSITIVE_KEY = re.compile(
    r"(?i)^(?:password|passwd|secret|client_secret|token|access_token|refresh_token|"
    r"api[_-]?key|authorization|cookie|set-cookie|private_key)$"
)
_PATTERNS = [(name, re.compile(p)) for name, p in BUILTIN_PATTERNS]

# How a Devin session id can show up: `devin-<hex>`, or a session link.
_DEVIN_ID = re.compile(r"\bdevin-([0-9a-f]{16,64})\b")
_DEVIN_LINK = re.compile(r"app\.devin\.ai/sessions/([0-9a-f]{16,64})")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for name, pattern in _PATTERNS:
            marker = f"[REDACTED:{name}]"

            def replace(m: re.Match[str], marker: str = marker) -> str:
                return (m.groupdict().get("keep") or "") + marker

            value = pattern.sub(replace, value)
        return value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "[REDACTED:key]"
            if SENSITIVE_KEY.match(str(k)) and v not in (None, "", [], {})
            else redact(v)
            for k, v in value.items()
        }
    return value


def tail(text: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return f"[{len(text) - limit} characters cut]\n" + text[-limit:]
    return text


def capped(value: Any) -> Any:
    """Long strings anywhere in a value are cut to their tail."""
    if isinstance(value, str):
        return tail(value)
    if isinstance(value, list):
        return [capped(v) for v in value[:500]]
    if isinstance(value, dict):
        return {k: capped(v) for k, v in list(value.items())[:200]}
    return value


def devin_session(payload: dict[str, Any], environ: dict[str, str]) -> str:
    """The Devin session id, if the payload or the environment carries one."""
    sid = str(payload.get("session_id") or "")
    for text in (sid, *environ.values()):
        match = _DEVIN_ID.search(text) or _DEVIN_LINK.search(text)
        if match:
            return f"devin-{match.group(1)}"
    return ""


def event_from(payload: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    keep = (
        "hook_event_name",
        "session_id",
        "prompt_id",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_provenance",
        "prompt",
        "source",
        "summary",
        "last_assistant_message",
        "stop_hook_active",
        "reason",
    )
    event: dict[str, Any] = capped(redact({k: payload[k] for k in keep if k in payload}))
    event["id"] = uuid.uuid4().hex
    event["at"] = time.time()
    event["devin_session"] = devin_session(payload, environ)
    event["hook_version"] = VERSION
    return event


class Spool:
    """An append-only file of events, and how far into it Watchdog has them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "spool.jsonl"
        self.offset_path = root / "sent.offset"
        self.failed_path = root / "last_failure"

    @contextlib.contextmanager
    def _lock(self, name: str, *, wait: bool) -> Any:
        with (self.root / name).open("a") as f:
            if fcntl is None:
                yield True
                return
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock("append.lock", wait=True):
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)

    def _offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def recently_failed(self, now: float) -> bool:
        try:
            return now - float(self.failed_path.read_text().strip()) < BACKOFF
        except (OSError, ValueError):
            return False

    def flush(self, url: str, token: str, now: float) -> dict[str, str]:
        """Send what is unsent. Returns Watchdog's notes, by hook session id."""
        notes: dict[str, str] = {}
        if self.recently_failed(now):
            return notes
        with self._lock("flush.lock", wait=False) as got:
            if not got:
                return notes  # another hook is sending
            offset = self._offset()
            with self.path.open("rb") as f:
                f.seek(offset)
                while True:
                    lines = [f.readline() for _ in range(BATCH)]
                    lines = [ln for ln in lines if ln.endswith(b"\n")]
                    if not lines:
                        break
                    events = [json.loads(ln) for ln in lines if ln.strip()]
                    answer = post(url, token, events)
                    if answer is None:
                        self.failed_path.write_text(str(now))
                        return notes
                    notes.update({k: str(v) for k, v in (answer.get("context") or {}).items()})
                    offset += sum(len(ln) for ln in lines)
                    self.offset_path.write_text(str(offset))
            self._rotate(offset)
        return notes

    def keep_notes(self, notes: dict[str, str]) -> None:
        """Notes wait in a file until an event that can show them comes along."""
        if not notes:
            return
        with self._lock("notes.lock", wait=True):
            held = self._notes()
            for sid, note in notes.items():
                held[sid] = f"{held[sid]}\n{note}" if held.get(sid) else note
            (self.root / "notes.json").write_text(json.dumps(held))

    def take_note(self, sid: str) -> str:
        with self._lock("notes.lock", wait=True):
            held = self._notes()
            note = held.pop(sid, "")
            if note:
                (self.root / "notes.json").write_text(json.dumps(held))
            return note

    def _notes(self) -> dict[str, str]:
        try:
            data = json.loads((self.root / "notes.json").read_text())
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _rotate(self, offset: int) -> None:
        with self._lock("append.lock", wait=True):
            if offset >= SPOOL_LIMIT and self.path.stat().st_size == offset:
                self.path.write_bytes(b"")
                self.offset_path.write_text("0")


def post(url: str, token: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    request = urllib.request.Request(  # noqa: S310 -- the scheme is checked in main()
        url,
        data=json.dumps({"events": events}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            body = json.loads(response.read() or b"{}")
            return body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        return {} if exc.code == 409 else None  # 409: duplicates only, nothing to resend
    except (urllib.error.URLError, OSError, ValueError):
        return None


def target(environ: dict[str, str]) -> str:
    url = environ.get("WATCHDOG_HOOK_URL", "")
    if not url and environ.get("WATCHDOG_INGEST_URL"):
        url = environ["WATCHDOG_INGEST_URL"].rsplit("/api/", 1)[0] + "/api/hook"
    if not url and environ.get("WATCHDOG_PUBLIC_URL"):
        url = environ["WATCHDOG_PUBLIC_URL"].rstrip("/") + "/api/hook"
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    return url if url.startswith("https://") or local else ""


def main(stdin: Any = None, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    stream = stdin if stdin is not None else sys.stdin.buffer
    try:
        payload = json.loads(stream.read(MAX_INPUT) or b"{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    root = Path(environ.get("WATCHDOG_HOOK_DIR") or Path.home() / ".watchdog-hooks")
    try:
        spool = Spool(root)
        spool.append(event_from(payload, environ))
        url, token = target(environ), environ.get("WATCHDOG_INGEST_TOKEN", "")
        if url and token:
            spool.keep_notes(spool.flush(url, token, time.time()))
        event = str(payload.get("hook_event_name") or "")
        if event not in ("PostToolUse", "UserPromptSubmit", "SessionStart"):
            return 0  # these events cannot carry context back to the agent
        note = spool.take_note(str(payload.get("session_id") or ""))
    except OSError:
        return 0
    if note:
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- a hook must never break the session
        sys.exit(0)
