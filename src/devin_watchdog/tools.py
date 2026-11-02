"""`watchdog tools`: a shell and editor MCP server for Devin, made to be recorded.

Devin's API shows what a session *says*, not what it *runs*. Give Devin this
server through the proxy, and every command, its exit code and every edit
becomes an exact entry in a chained log:

    flight-recorder proxy --name watchdog-tools -- watchdog tools

Tools:

- `run_command`: runs a shell command; a non-zero exit is an MCP error
  (`isError`), so the proxy records `event.outcome = failure`.
- `edit_file`, `write_file`, `read_file`: file edits the rules can count.
- `link_session`: Devin passes its own session link once, at the start. The
  call is recorded like any other, and that is how Watchdog learns which Devin
  session this log belongs to. Devin's API documents no per-session variable an
  MCP server could read instead.

The server runs on the session machine, so the proxy's log is written there.
With `WATCHDOG_INGEST_URL` and `WATCHDOG_INGEST_TOKEN` set (as Devin secrets),
a background thread ships each verified entry of that log to Watchdog. It
resends from the start after a restart; the receiver skips what it already holds.

Standard library only. Newline-delimited JSON-RPC 2.0 on stdin/stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

LINK_TOOL = "link_session"
OUTPUT_LIMIT = 20_000  # characters of command output returned (the tail)
PROTOCOL = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Run a shell command (bash) in the workspace and return its exit code "
        "and output. Use this for every test, build, install, lint or other shell command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["command"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text in a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": LINK_TOOL,
        "description": "Call once at the start of the session with your Devin session link "
        "(https://app.devin.ai/sessions/...), so this session's commands are attributed to it.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}},
            "required": ["session"],
        },
    },
]

_SESSION = re.compile(r"(?:devin-)?([0-9a-f]{16,64})", re.IGNORECASE)


def devin_session_id(text: str) -> str:
    """`devin-<hex>` from a session link or id; '' if there is none."""
    tail = text.strip().rstrip("/").rsplit("/", 1)[-1]
    match = _SESSION.fullmatch(tail)
    return f"devin-{match.group(1).lower()}" if match else ""


class ToolError(Exception):
    pass


def _text(result: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": result}], "isError": error}


def run_command(args: dict[str, Any]) -> dict[str, Any]:
    command = str(args.get("command", ""))
    if not command.strip():
        raise ToolError("command is empty")
    timeout = int(args.get("timeout_seconds") or 600)
    cwd = args.get("cwd") or None
    try:
        done = subprocess.run(  # noqa: S602 -- running the agent's command is this tool's job
            command,
            shell=True,
            executable="/bin/bash" if Path("/bin/bash").exists() else None,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return _text(f"timed out after {timeout}s\n{out[-OUTPUT_LIMIT:]}", error=True)
    except OSError as exc:
        return _text(f"could not run: {exc}", error=True)
    output = (done.stdout + done.stderr)[-OUTPUT_LIMIT:]
    return _text(f"exit code {done.returncode}\n{output}", error=done.returncode != 0)


def edit_file(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("path", "")))
    old, new = str(args.get("old_text", "")), str(args.get("new_text", ""))
    text = path.read_text(encoding="utf-8")
    count = text.count(old) if old else 0
    if count != 1:
        raise ToolError(f"old_text occurs {count} times in {path}; it must occur exactly once")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return _text(f"edited {path}")


def write_file(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("path", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")), encoding="utf-8")
    return _text(f"wrote {path}")


def read_file(args: dict[str, Any]) -> dict[str, Any]:
    return _text(Path(str(args.get("path", ""))).read_text(encoding="utf-8", errors="replace"))


def link_session(args: dict[str, Any]) -> dict[str, Any]:
    sid = devin_session_id(str(args.get("session", "")))
    if not sid:
        raise ToolError("pass your session link, like https://app.devin.ai/sessions/<id>")
    return _text(f"linked to {sid}")


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "run_command": run_command,
    "edit_file": edit_file,
    "write_file": write_file,
    "read_file": read_file,
    LINK_TOOL: link_session,
}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method, rid = message.get("method"), message.get("id")
    if rid is None:
        return None  # a notification
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": asked or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "watchdog-tools", "version": "0.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        handler = HANDLERS.get(str(params.get("name")))
        if handler is None:
            return _error(rid, -32602, f"unknown tool {params.get('name')!r}")
        try:
            result = handler(params.get("arguments") or {})
        except (ToolError, OSError, ValueError) as exc:
            result = _text(str(exc), error=True)
    else:
        return _error(rid, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}


def serve(stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            reply = handle(message) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


# -- shipping the proxy's log to Watchdog ------------------------------------------


class Shipper:
    """Follows the proxy's log directory and posts verified entries to Watchdog."""

    def __init__(self, log_dir: Path, url: str, token: str, *, poll: float = 1.0) -> None:
        self.log_dir, self.url, self.token, self.poll = log_dir, url, token, poll
        self.stop = threading.Event()
        self.sent = 0
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, name="shipper", daemon=True)

    def start(self) -> Shipper:
        self._thread.start()
        return self

    def close(self, wait: float = 5.0) -> None:
        self.stop.set()
        self._thread.join(timeout=wait)

    def _run(self) -> None:
        from flight_recorder.reader import ChainError, follow

        self.log_dir.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                # One entry per request: tool calls are seconds apart, and an
                # entry is on its way as soon as it is verified.
                for entry in follow(self.log_dir, poll=self.poll, stop=self.stop.is_set):
                    self._flush([entry])
            except ChainError as exc:
                self.last_error = f"local log does not verify: {exc}"
                return  # never ship past a broken link
            except _ResendError:
                continue  # start over from the beginning; the receiver skips duplicates
            return

    def _flush(self, pending: list[dict[str, Any]]) -> None:
        delay = 1.0
        while True:
            code, body = self._post({"entries": pending})
            if code == 200:
                self.sent += int(body.get("accepted", 0))
                return
            if code == 409:
                raise _ResendError
            self.last_error = f"ingest {code}: {body.get('error', '')}"
            # Retry in order, with backoff; on shutdown, a last try and then give up.
            if self.stop.wait(delay):
                code, body = self._post({"entries": pending})
                return
            delay = min(delay * 2, 30.0)

    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(  # noqa: S310 -- the scheme is checked in main()
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 0, {"error": str(exc)}


class _ResendError(Exception):
    """The receiver has a gap: follow the log again from the start."""


def main() -> int:
    from flight_recorder.cli import default_log_dir

    shipper = None
    url, token = os.environ.get("WATCHDOG_INGEST_URL"), os.environ.get("WATCHDOG_INGEST_TOKEN")
    if url and token:
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            print("watchdog tools: WATCHDOG_INGEST_URL must be https", file=sys.stderr)
            return 2
        shipper = Shipper(default_log_dir(), url, token).start()
    try:
        serve(sys.stdin, sys.stdout)
    finally:
        if shipper is not None:
            time.sleep(1.5)  # let the proxy write the last result before the final read
            shipper.close()
    return 0
