"""The dashboard: one page, polling the JSON API. Standard library only.

    GET  /                        the page
    GET  /api/state               sessions, totals and the live log
    GET  /api/session/<id>        everything about one session
    GET  /api/settings            the settings schema, values and what is connected
    PUT  /api/settings            change settings (validated, saved, applied live)
    POST /api/approve | /api/decline     a pending terminate
    POST /api/action              message / sleep / terminate / mute / unmute
    POST /api/test/teams | /api/test/judge

It binds to 127.0.0.1 by default: it shows session content. Every change needs
`WATCHDOG_APPROVAL_TOKEN` when one is set; without one, only a dashboard bound to
a loopback address accepts changes. Changes must be JSON, which forces a CORS
preflight this server never answers, so another site open in the browser cannot
post here.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_watchdog.engine import Watchdog
from agent_watchdog.settings import SettingsError, schema

PAGE = files("agent_watchdog").joinpath("dashboard.html").read_bytes()
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def serve(
    engine: Watchdog, host: str = "127.0.0.1", port: int = 8765, *, token: str = ""
) -> ThreadingHTTPServer:
    """Start the dashboard on a background thread; returns the server."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: Any) -> None:
            self._send(code, json.dumps(value, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, engine.snapshot())
            elif path.startswith("/api/session/"):
                detail = engine.detail(unquote(path.removeprefix("/api/session/")))
                self._json(200 if detail else 404, detail or {"error": "no such session"})
            elif path == "/api/settings":
                self._json(
                    200,
                    {
                        **schema(),
                        "values": engine.config.values(),
                        "connections": engine.info,
                        "can_edit": bool(token) or host in LOOPBACK,
                        "needs_token": bool(token),
                    },
                )
            else:
                self._json(404, {"error": "not found"})

        def _body(self) -> dict[str, Any] | None:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "send application/json"})
                return None
            length = min(int(self.headers.get("Content-Length") or 0), 200_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "bad json"})
                return None
            if not isinstance(body, dict):
                self._json(400, {"error": "send an object"})
                return None
            given = str(body.get("token", "") or self.headers.get("X-Watchdog-Token", ""))
            allowed = hmac.compare_digest(given, token) if token else host in LOOPBACK
            if not allowed:
                self._json(403, {"error": "token required"})
                return None
            return body

        def do_PUT(self) -> None:
            if urlsplit(self.path).path != "/api/settings":
                return self._json(404, {"error": "not found"})
            body = self._body()
            if body is None:
                return
            changes = body.get("values")
            if not isinstance(changes, dict):
                return self._json(400, {"error": 'send {"values": {...}}'})
            try:
                values = engine.config.update(changes)
            except SettingsError as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, {"values": values})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in (
                "/api/approve",
                "/api/decline",
                "/api/action",
                "/api/test/teams",
                "/api/test/judge",
            ):
                return self._json(404, {"error": "not found"})
            body = self._body()
            if body is None:
                return
            sid = str(body.get("session_id", ""))
            if path == "/api/approve":
                ok = engine.approve(sid, by="dashboard")
                return self._json(200 if ok else 409, {"ok": ok})
            if path == "/api/decline":
                ok = engine.decline(sid, by="dashboard")
                return self._json(200 if ok else 409, {"ok": ok})
            if path == "/api/action":
                ok, detail = engine.manual(
                    sid, str(body.get("action", "")), by="dashboard", text=str(body.get("text", ""))
                )
                return self._json(200 if ok else 409, {"ok": ok, "detail": detail})
            if path == "/api/test/teams":
                with engine.lock:
                    outcome = engine.notifier.test()
                return self._json(200, {"ok": outcome == "sent", "detail": outcome})
            judge = engine.judge
            if judge is None or not hasattr(judge, "test"):
                return self._json(200, {"ok": False, "detail": "no judge: set OPENAI_API_KEY"})
            outcome = str(judge.test())
            self._json(200, {"ok": outcome.startswith("ok"), "detail": outcome})

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server
