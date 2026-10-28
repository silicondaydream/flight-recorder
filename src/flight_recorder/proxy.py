"""A stdio MCP proxy that records every tool call and its result.

The client starts `flight-recorder proxy -- <server command>` where it would
have started the server. The proxy starts the server and relays the
newline-delimited JSON-RPC stream both ways, unchanged: every line is
forwarded exactly as read. It only *reads* the traffic, to record:

- `tools/call` requests, written **before** they are forwarded. With
  `--strict`, a call that cannot be recorded is not forwarded at all, so the
  server never runs a tool call the log does not hold.
- their results (or errors), paired by request id, with the duration.
- `initialize`, for the client's and server's names and versions.

Everything else is counted, not stored, and the counts close the session.
Anything that is not JSON is still relayed, and noted.

Works with any MCP client that launches stdio servers: Claude Code, Cursor,
Devin's custom MCP servers, and others.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from flight_recorder.chain import Recorder, safe_name
from flight_recorder.redact import Redactor, keep

EXIT_RECORDER_FAILED = 70  # EX_SOFTWARE: --strict and a write failed


@dataclass
class Options:
    command: list[str]
    log_dir: Path
    name: str
    session_id: str
    max_payload: int = 65_536
    redact: list[str] = field(default_factory=list)
    strict: bool = False


class _StrictError(Exception):
    """A write failed under --strict: stop relaying."""


class Proxy:
    def __init__(self, options: Options, recorder: Recorder) -> None:
        self.options = options
        self.recorder = recorder
        self.redactor = Redactor(options.redact)
        self.pending: dict[str, tuple[float, str]] = {}  # request id -> (start, tool)
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.failed = threading.Event()
        self._warned = False

    # -- recording -----------------------------------------------------------

    def _kept(self, value: Any) -> dict[str, Any]:
        return keep(self.redactor, value, self.options.max_payload)

    def record(self, action: str, **sections: Any) -> None:
        try:
            self.recorder.append(action, **sections)
        except Exception as exc:
            if self.options.strict:
                self.failed.set()
                raise _StrictError from exc
            if not self._warned:
                self._warned = True
                print(f"flight-recorder: could not write the log: {exc}", file=sys.stderr)

    def _flight(self, **fields: Any) -> dict[str, Any]:
        return {"server": self.options.name, **fields}

    # -- inspection ----------------------------------------------------------

    def from_client(self, line: bytes) -> None:
        for message in self._messages(line, "client"):
            method = message.get("method")
            if not isinstance(method, str):
                self.counts["client.response"] += 1
                continue
            self.counts[f"client.{method}"] += 1
            if method == "tools/call" and "id" in message:
                params = message.get("params") or {}
                tool = str(params.get("name", ""))
                with self.lock:
                    self.pending[_key(message["id"])] = (time.monotonic(), tool)
                self.record(
                    "tool.call",
                    flight=self._flight(
                        tool=tool,
                        request_id=_key(message["id"]),
                        arguments=self._kept(params.get("arguments")),
                    ),
                )
            elif method == "initialize":
                info = (message.get("params") or {}).get("clientInfo") or {}
                self.record("session.initialize", flight=self._flight(client=info))

    def from_server(self, line: bytes) -> None:
        for message in self._messages(line, "server"):
            if "method" in message:
                self.counts[f"server.{message['method']}"] += 1
                continue
            self.counts["server.response"] += 1
            with self.lock:
                started = self.pending.pop(_key(message.get("id")), None)
            if started is None:
                result = message.get("result") or {}
                if isinstance(result, dict) and "serverInfo" in result:
                    self.record(
                        "session.initialized", flight=self._flight(server_info=result["serverInfo"])
                    )
                continue
            start, tool = started
            error = message.get("error")
            result = message.get("result")
            failed = error is not None or (isinstance(result, dict) and result.get("isError"))
            self.record(
                "tool.result",
                outcome="failure" if failed else "success",
                event={"duration": int((time.monotonic() - start) * 1e9)},  # ECS: nanoseconds
                flight=self._flight(
                    tool=tool,
                    request_id=_key(message.get("id")),
                    result=self._kept(error if error is not None else result),
                ),
            )

    def _messages(self, line: bytes, side: str) -> list[dict[str, Any]]:
        text = line.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            self.counts[f"{side}.not_json"] += 1
            self.record(
                "recorder.error",
                outcome="unknown",
                flight=self._flight(
                    side=side,
                    error="a line that is not JSON was relayed unchanged",
                    line=self._kept(text.decode("utf-8", errors="replace")),
                ),
            )
            return []
        batch = value if isinstance(value, list) else [value]
        return [m for m in batch if isinstance(m, dict)]


def _key(request_id: Any) -> str:
    # JSON-RPC ids are strings or numbers; 1 and "1" are different requests.
    return json.dumps(request_id)


def _relay(
    source: IO[bytes],
    sink: IO[bytes],
    inspect: Callable[[bytes], None],
    on_close: Callable[[], None],
) -> None:
    """Copy lines from source to sink, inspecting each. Record first, then forward."""
    try:
        for line in iter(source.readline, b""):
            inspect(line)
            sink.write(line)
            sink.flush()
    except _StrictError:
        pass
    except (BrokenPipeError, ValueError, OSError):
        pass  # the other end went away; the main thread sees the exit
    finally:
        on_close()


def run(options: Options) -> int:
    """Run the server under the proxy. Returns the server's exit code."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = (
        options.log_dir
        / f"{stamp}-{safe_name(options.name)}-{safe_name(options.session_id)[:8]}.ndjson"
    )
    recorder = Recorder(path, session_id=options.session_id, agent="mcp-proxy")
    proxy = Proxy(options, recorder)
    command, _ = proxy.redactor.value(options.command)
    proxy.record(
        "session.start",
        flight=proxy._flight(command=command, cwd=os.getcwd(), log=str(path)),
        host={"hostname": socket.gethostname()},
        process={"pid": os.getpid()},
    )

    try:
        child = subprocess.Popen(options.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as exc:
        proxy.record("session.end", outcome="failure", flight=proxy._flight(error=str(exc)))
        recorder.close()
        print(f"flight-recorder: could not start {options.command[0]!r}: {exc}", file=sys.stderr)
        return 127
    child_in, child_out = child.stdin, child.stdout
    if child_in is None or child_out is None:  # cannot happen with PIPE; narrows the types
        raise RuntimeError("the server's pipes were not created")

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, forward)

    def strict_stop() -> None:
        if proxy.failed.is_set():
            child.terminate()

    def client_closed() -> None:
        # The client hung up: close the server's stdin so it can exit cleanly.
        with contextlib.suppress(OSError):
            child_in.close()
        strict_stop()

    upstream = threading.Thread(
        target=_relay,
        args=(
            sys.stdin.buffer,
            child_in,
            proxy.from_client,
            client_closed,
        ),
        daemon=True,
    )
    downstream = threading.Thread(
        target=_relay,
        args=(child_out, sys.stdout.buffer, proxy.from_server, strict_stop),
        daemon=True,
    )
    upstream.start()
    downstream.start()
    code = child.wait()
    downstream.join()

    if proxy.failed.is_set():
        print(
            "flight-recorder: stopped: a tool call could not be recorded (--strict)",
            file=sys.stderr,
        )
        code = EXIT_RECORDER_FAILED
    else:
        with proxy.lock:
            unanswered = sorted(tool for _, tool in proxy.pending.values())
        proxy.record(
            "session.end",
            outcome="success" if code == 0 else "failure",
            flight=proxy._flight(
                exit_code=code, messages=dict(sorted(proxy.counts.items())), unanswered=unanswered
            ),
        )
    recorder.close()
    return code
