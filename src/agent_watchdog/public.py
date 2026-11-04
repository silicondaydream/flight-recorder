"""The public listener: the only part of Watchdog meant to be reachable from outside.

The dashboard stays on loopback: it shows session content. This listener
serves just two things, each with its own credential:

- `POST /api/ingest`: MCP proxy entries shipped from Devin's session machines,
  with `Authorization: Bearer $WATCHDOG_INGEST_TOKEN`. See `ingest.py`.
- `GET /approve?...` shows a confirmation page for a signed approval link
  (the Teams card's buttons). `POST /approve` acts on it. See `approvals.py`.

Put it behind HTTPS (a tunnel or a reverse proxy) and set
`WATCHDOG_PUBLIC_URL` to the address people's browsers and Devin's machines use.
"""

from __future__ import annotations

import hmac
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_watchdog.approvals import Signer
from agent_watchdog.engine import Watchdog
from agent_watchdog.ingest import IngestStore

MAX_BODY = 8 * 1024 * 1024

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watchdog approval</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; min-height: 100vh; background-color: #09090b; color: #FFFFFF; isolation: isolate;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
body::before {{ content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background-image: radial-gradient(rgba(255,255,255,0.2) 1px, transparent 1px);
  background-size: 22px 22px;
  -webkit-mask-image: radial-gradient(ellipse 70% 60% at 50% 0%, #000 50%, transparent 100%);
  mask-image: radial-gradient(ellipse 70% 60% at 50% 0%, #000 50%, transparent 100%); }}
main {{ max-width: 520px; margin: 12vh auto; padding: 0 16px; }}
.brand {{ font-size: 12px; font-weight: 700; letter-spacing: .08em; color: rgba(255,255,255,.6);
  margin-bottom: 10px; }}
.card {{ background: #051F40; border: 1px solid rgba(255,255,255,.14); border-radius: 16px;
  padding: 24px; }}
h1 {{ font-size: 21px; margin: 0 0 6px; }}
p {{ color: rgba(255,255,255,.75); margin: 6px 0 16px; }}
code {{ font-size: 13px; color: rgba(255,255,255,.85); }}
button {{ font: inherit; font-weight: 650; border: 1px solid #F0001F; border-radius: 10px;
  padding: 10px 18px;
  cursor: pointer; color: #FFFFFF; background: #F0001F; }}
button.keep {{ background: #FFFFFF; border-color: #FFFFFF; color: #051F40; }}
button:focus-visible {{ outline: 2px solid #FFFFFF; outline-offset: 2px; }}
.done {{ font-weight: 650; }}
</style></head>
<body><main><div class="brand">WATCHDOG</div><div class="card">{body}</div></main></body></html>"""


def _page(body: str) -> bytes:
    return PAGE.format(body=body).encode()


def serve_public(
    engine: Watchdog,
    store: IngestStore | None,
    signer: Signer | None,
    host: str,
    port: int,
    *,
    ingest_token: str = "",
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")  # the link carries a signature
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: Any) -> None:
            self._send(code, json.dumps(value).encode(), "application/json")

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(length, MAX_BODY))

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path != "/approve" or signer is None:
                return self._json(404, {"error": "not found"})
            self._approval(parse_qs(parts.query), confirm=False)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/ingest" and store is not None and ingest_token:
                return self._ingest()
            if path == "/approve" and signer is not None:
                form = parse_qs(self._body().decode("utf-8", errors="replace"))
                return self._approval(form, confirm=True)
            self._json(404, {"error": "not found"})

        def _ingest(self) -> None:
            if store is None:
                return self._json(404, {"error": "not found"})
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, ingest_token):
                return self._json(401, {"error": "bad ingest token"})
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                body = json.loads(self._body() or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            entries = body.get("entries") if isinstance(body, dict) else None
            if not isinstance(entries, list):
                return self._json(400, {"error": 'send {"entries": [...]}'})
            result = store.add(entries)
            if result.error and result.expected:
                return self._json(
                    409,
                    {"error": result.error, "session": result.session, "expected": result.expected},
                )
            if result.error:
                return self._json(400, {"error": result.error})
            self._json(200, {"accepted": result.accepted, "skipped": result.skipped})

        def _approval(self, query: dict[str, list[str]], *, confirm: bool) -> None:
            if signer is None:
                return self._json(404, {"error": "not found"})
            field = {k: (v[0] if v else "") for k, v in query.items()}
            decision, session = field.get("decision", ""), field.get("session", "")
            expires, sig = field.get("expires", ""), field.get("sig", "")
            problem = signer.check(decision, session, expires, sig, time.time())
            title = html.escape(_title(engine, session))
            sid = html.escape(session)
            if problem:
                return self._send(
                    403, _page(f"<h1>Link not valid</h1><p>{html.escape(problem)}.</p>"), _HTML
                )
            if not engine.pending(session):
                return self._send(
                    409,
                    _page(
                        f"<h1>Nothing to decide</h1><p><b>{title}</b> (<code>{sid}</code>) is "
                        "not waiting for a terminate decision any more.</p>"
                    ),
                    _HTML,
                )
            if not confirm:
                verb = "Terminate" if decision == "approve" else "Keep running"
                what = (
                    "Watchdog will terminate this Devin session on its next tick."
                    if decision == "approve"
                    else "Watchdog will not terminate it for as long as this incident lasts."
                )
                hidden = "".join(
                    f'<input type="hidden" name="{k}" value="{html.escape(v)}">'
                    for k, v in (
                        ("decision", decision),
                        ("session", session),
                        ("expires", expires),
                        ("sig", sig),
                    )
                )
                cls = "" if decision == "approve" else ' class="keep"'
                body = (
                    f"<h1>{verb}?</h1><p><b>{title}</b><br><code>{sid}</code></p><p>{what}</p>"
                    f'<form method="post" action="/approve">{hidden}'
                    f'<button{cls} type="submit">{verb}</button></form>'
                )
                return self._send(200, _page(body), _HTML)
            who = f"approval link ({self.client_address[0]})"
            ok = (
                engine.approve(session, by=who)
                if decision == "approve"
                else engine.decline(session, by=who)
            )
            done = "Terminate approved." if decision == "approve" else "Kept running."
            body = f'<h1 class="done">{done if ok else "Already decided."}</h1><p>{title}</p>'
            self._send(200 if ok else 409, _page(body), _HTML)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="public", daemon=True).start()
    return server


_HTML = "text/html; charset=utf-8"


def _title(engine: Watchdog, session: str) -> str:
    with engine.lock:
        view = engine.views.get(session)
        return (view.title if view and view.title else session)[:200]
