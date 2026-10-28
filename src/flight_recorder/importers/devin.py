"""Import Devin sessions into chained logs, through Devin's REST API.

Devin runs its own shell and editor, outside any MCP proxy, so what it did
reaches Flight Recorder through the API instead: each session's messages, its
status changes, and a snapshot of its metadata whenever that changes. One log
per Devin session, `devin-<session id>.ndjson`, extended on every poll.

Polling is idempotent. `state.db` (SQLite) remembers which messages and which
status each session has been recorded with, so a restart, or two imports in a
row, never records anything twice.

Every endpoint and field this module relies on is in `DevinClient` and
`_message_key`; nothing else knows Devin's API. Written against the v1 API
(`GET /v1/sessions`, `GET /v1/session/{id}`, Bearer key). Check it against
your organisation's API with `--dry-run` before relying on it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from flight_recorder.chain import Recorder, canonical, safe_name
from flight_recorder.redact import Redactor, keep

DEFAULT_BASE_URL = "https://api.devin.ai/v1"
PAGE = 100


class DevinError(Exception):
    pass


class DevinClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def _get(self, path: str, **params: Any) -> Any:
        try:
            response = self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise DevinError(f"GET {path}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code in (401, 403):
            raise DevinError(f"GET {path}: {response.status_code}: check DEVIN_API_KEY")
        if response.status_code >= 400:
            raise DevinError(f"GET {path}: {response.status_code}: {response.text[:200]}")
        return response.json()

    def sessions(self, *, tags: list[str] | None = None) -> Iterator[dict[str, Any]]:
        """Every session the key can see, newest first, a page at a time."""
        offset = 0
        while True:
            params: dict[str, Any] = {"limit": PAGE, "offset": offset}
            if tags:
                params["tags"] = tags
            page = self._get("/sessions", **params).get("sessions", [])
            yield from page
            if len(page) < PAGE:
                return
            offset += PAGE

    def session(self, session_id: str) -> dict[str, Any]:
        # The id came from the API: encode it whole, so a "/" or ".." in it
        # cannot aim the request at another endpoint.
        result: dict[str, Any] = self._get(f"/session/{quote(session_id, safe='')}")
        return result

    def close(self) -> None:
        self._http.close()


def _message_key(message: dict[str, Any]) -> str:
    """A stable identity for a message: Devin's event id, else a content hash."""
    if message.get("event_id"):
        return str(message["event_id"])
    return "sha256:" + hashlib.sha256(canonical(message)).hexdigest()


def _status(session: dict[str, Any]) -> str:
    return str(session.get("status_enum") or session.get("status") or "unknown")


class State:
    """What has been recorded already, per session."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                session_id TEXT NOT NULL, message_key TEXT NOT NULL,
                PRIMARY KEY (session_id, message_key));
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, status TEXT, snapshot TEXT);
            """
        )

    def seen(self, session_id: str, key: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM seen WHERE session_id = ? AND message_key = ?", (session_id, key)
        ).fetchone()
        return row is not None

    def mark(self, session_id: str, key: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (session_id, key))

    def last(self, session_id: str) -> tuple[str | None, str | None]:
        row = self._db.execute(
            "SELECT status, snapshot FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def remember(self, session_id: str, status: str, snapshot: str) -> None:
        self._db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(session_id) "
            "DO UPDATE SET status = excluded.status, snapshot = excluded.snapshot",
            (session_id, status, snapshot),
        )

    def commit(self) -> None:
        self._db.commit()

    def close(self) -> None:
        self._db.close()


@dataclass
class Summary:
    sessions: int = 0
    messages: int = 0
    status_changes: int = 0
    snapshots: int = 0
    planned: list[str] = field(default_factory=list)  # --dry-run: what would be written


@dataclass
class Importer:
    client: DevinClient
    log_dir: Path
    state: State
    redactor: Redactor = field(default_factory=Redactor)
    max_payload: int = 65_536
    dry_run: bool = False

    def run(self, *, tags: list[str] | None = None) -> Summary:
        summary = Summary()
        for listed in self.client.sessions(tags=tags):
            session_id = str(listed.get("session_id", ""))
            if not session_id:
                continue
            self._import(self.client.session(session_id), session_id, summary)
            summary.sessions += 1
        return summary

    def _import(self, session: dict[str, Any], session_id: str, summary: Summary) -> None:
        status = _status(session)
        messages = [m for m in session.get("messages") or [] if isinstance(m, dict)]
        meta = {k: v for k, v in session.items() if k != "messages"}
        snapshot = hashlib.sha256(canonical(meta)).hexdigest()
        last_status, last_snapshot = self.state.last(session_id)
        new = [m for m in messages if not self.state.seen(session_id, _message_key(m))]
        changed_status = status != last_status
        changed_meta = snapshot != last_snapshot
        if not (new or changed_status or changed_meta):
            return

        if self.dry_run:
            if changed_meta:
                summary.planned.append(f"{session_id}  session.snapshot")
            if changed_status:
                summary.planned.append(f"{session_id}  session.status  {last_status} -> {status}")
            for m in new:
                summary.planned.append(f"{session_id}  message  {m.get('type', '?')}")
            return

        path = self.log_dir / f"devin-{safe_name(session_id)}.ndjson"
        with Recorder(path, session_id=session_id, agent="devin") as rec:
            if changed_meta:
                rec.append(
                    "session.snapshot",
                    timestamp=session.get("updated_at"),
                    flight={"source": "devin", "session": self._kept(meta)},
                )
                summary.snapshots += 1
            if changed_status:
                rec.append(
                    "session.status",
                    timestamp=session.get("updated_at"),
                    flight={"source": "devin", "status": status, "previous_status": last_status},
                )
                summary.status_changes += 1
            for m in new:
                rec.append(
                    "message",
                    timestamp=m.get("timestamp"),
                    flight={
                        "source": "devin",
                        "type": m.get("type"),
                        "origin": m.get("origin"),
                        "username": m.get("username"),
                        "event_id": m.get("event_id"),
                        "text": self._kept(m.get("message")),
                    },
                )
                # Marked only after the entry is on disk: a crash between the two
                # re-imports the message rather than losing it.
                self.state.mark(session_id, _message_key(m))
                summary.messages += 1
        self.state.remember(session_id, status, snapshot)
        self.state.commit()

    def _kept(self, value: Any) -> dict[str, Any]:
        return keep(self.redactor, value, self.max_payload)


def describe(summary: Summary) -> str:
    return json.dumps(
        {
            "sessions": summary.sessions,
            "messages": summary.messages,
            "status_changes": summary.status_changes,
            "snapshots": summary.snapshots,
        }
    )
