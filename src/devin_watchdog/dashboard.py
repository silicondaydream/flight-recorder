"""The live dashboard: one page, polling `/api/state`. Standard library only.

It binds to 127.0.0.1 by default. Approving a terminate from the page needs
`WATCHDOG_APPROVAL_TOKEN` when one is set; without one, only a dashboard bound
to a loopback address accepts approvals.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any

from devin_watchdog.engine import Watchdog

PAGE = files("devin_watchdog").joinpath("dashboard.html").read_bytes()
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
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: Any) -> None:
            self._send(code, json.dumps(value).encode(), "application/json")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, engine.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/approve":
                return self._json(404, {"error": "not found"})
            # A JSON content type forces a CORS preflight, which this server never
            # answers: another site open in the browser cannot post here.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "send application/json"})
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            given = str(body.get("token", ""))
            allowed = hmac.compare_digest(given, token) if token else host in LOOPBACK
            if not allowed:
                return self._json(403, {"error": "approval token required"})
            ok = engine.approve(str(body.get("session_id", "")), by="dashboard")
            self._json(200 if ok else 409, {"approved": ok})

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server
