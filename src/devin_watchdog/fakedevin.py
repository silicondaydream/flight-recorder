"""An in-memory stand-in for Devin's v3 organization API.

Used by the tests (through `httpx.MockTransport`) and by `watchdog demo`
(through a local HTTP server), so the demo exercises the same importer and
action code a real deployment runs. It implements only what Flight Recorder and
Watchdog call, with the shapes from Devin's published OpenAPI spec
(`SessionResponse`, `SessionMessage`, cursor pagination).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

ORG = "org-demo"
Reply = tuple[int, Any]


class FakeDevin:
    def __init__(self, *, clock: Callable[[], float] = time.time, page: int = 100) -> None:
        self.clock = clock
        self.page = page
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str]] = []  # (method, path) as received
        self.sent: list[tuple[str, str]] = []  # (session, message) posted to sessions
        self.teams: list[dict[str, Any]] = []  # cards posted to /teams
        self.status = 200  # set to fail every request
        self.on_message: Callable[[str, str], None] | None = None
        self.lock = threading.RLock()

    # -- scripting -------------------------------------------------------------

    def add(
        self,
        sid: str,
        *,
        title: str = "",
        status: str = "running",
        detail: str | None = "working",
        messages: int = 0,
        **extra: Any,
    ) -> dict[str, Any]:
        with self.lock:
            now = int(self.clock())
            self.sessions[sid] = {
                "session_id": sid,
                "url": f"https://app.devin.ai/sessions/{sid.removeprefix('devin-')}",
                "status": status,
                "status_detail": detail,
                "title": title or f"task {sid}",
                "tags": [],
                "org_id": ORG,
                "created_at": now,
                "updated_at": now,
                "is_archived": False,
                "acus_consumed": 0.0,
                "pull_requests": [],
                **extra,
            }
            self.messages[sid] = []
            for _ in range(messages):
                self.say(sid, "devin", "working on it")
            return self.sessions[sid]

    def say(self, sid: str, source: str, text: str) -> None:
        with self.lock:
            log = self.messages[sid]
            log.append(
                {
                    "event_id": f"{sid}-e{len(log) + 1}",
                    "source": source,
                    "message": text,
                    "created_at": int(self.clock()),
                    "username": "dana" if source == "user" else None,
                    "origin": "api" if source == "user" else None,
                }
            )
            self.touch(sid)

    def update(self, sid: str, **fields: Any) -> None:
        with self.lock:
            self.sessions[sid].update(fields)
            self.touch(sid)

    def touch(self, sid: str) -> None:
        # Strictly increasing, so a watermark never hides a change made in the
        # same second as the last poll.
        session = self.sessions[sid]
        session["updated_at"] = max(int(self.clock()), int(session["updated_at"]) + 1)

    # -- the API ---------------------------------------------------------------

    def handle(self, method: str, url: str, body: Any) -> Reply:
        parts = urlsplit(url)
        segments = [unquote(s) for s in parts.path.strip("/").split("/")]
        query = parse_qs(parts.query)
        with self.lock:
            self.requests.append((method, parts.path))
            if self.status != 200:
                return self.status, {"detail": "nope"}
            if segments == ["teams"] and method == "POST":
                self.teams.append(body)
                return 202, {}
            if segments == ["v3", "self"]:
                return 200, {
                    "principal_type": "service_user",
                    "service_user_id": "service-user-watchdog",
                    "service_user_name": "watchdog",
                    "org_id": ORG,
                }
            if segments[:4] != ["v3", "organizations", ORG, "sessions"]:
                return 404, {"detail": f"no route {parts.path}"}
            rest = segments[4:]
            if not rest and method == "GET":
                return 200, self._list(query)
            session = self.sessions.get(rest[0]) if rest else None
            if session is None:
                return 404, {"detail": "session not found"}
            sid = rest[0]
            action = rest[1] if len(rest) > 1 else None
            if action is None and method == "GET":
                return 200, session
            if action is None and method == "DELETE":
                self.update(sid, status="exit", status_detail=None)
                return 200, session
            if action == "messages" and method == "GET":
                return 200, self._page(self.messages[sid], query)
            if action == "messages" and method == "POST":
                text = str((body or {}).get("message", ""))
                self.sent.append((sid, text))
                self.say(sid, "user", text)
                if session["status"] == "suspended":  # documented: messaging resumes
                    self.update(sid, status="running", status_detail="working")
                if self.on_message:
                    self.on_message(sid, text)
                return 200, session
            if action == "archive" and method == "POST":
                fields: dict[str, Any] = {"is_archived": True}
                if session["status"] == "running":  # documented: archive puts it to sleep
                    fields |= {"status": "suspended", "status_detail": "user_request"}
                self.update(sid, **fields)
                return 200, session
            if action == "tags" and method == "POST":
                tags = list(dict.fromkeys([*session["tags"], *(body or {}).get("tags", [])]))
                self.update(sid, tags=tags)
                return 200, {"tags": tags}
            return 405, {"detail": "method not allowed"}

    def _list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        items = sorted(self.sessions.values(), key=lambda s: -int(s["created_at"]))
        if "tags" in query:
            items = [s for s in items if set(query["tags"]) & set(s["tags"])]
        if "updated_after" in query:
            after = int(query["updated_after"][0])
            items = [s for s in items if int(s["updated_at"]) > after]
        return self._page(items, query)

    def _page(self, items: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        start = int(query.get("after", ["0"])[0])
        size = min(int(query.get("first", [str(self.page)])[0]), self.page)
        chunk = items[start : start + size]
        end = start + len(chunk)
        return {
            "items": [dict(i) for i in chunk],
            "end_cursor": str(end),
            "has_next_page": end < len(items),
        }

    # -- transports ------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            code, payload = self.handle(request.method, str(request.url), body)
            return httpx.Response(code, json=payload)

        return httpx.MockTransport(handler)
