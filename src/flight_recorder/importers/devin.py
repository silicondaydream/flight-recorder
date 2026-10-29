"""Import Devin sessions into chained logs, through Devin's v3 REST API.

Devin runs its own shell and editor, outside any MCP proxy, so what it did
reaches Flight Recorder through the API instead: each session's messages, its
status changes, and a snapshot of its metadata (ACUs, PRs, tags) whenever that
changes. One log per Devin session, `devin-<id>.ndjson` (Devin's own ids
already start `devin-`), extended on every poll.

Polling is idempotent and incremental. A SQLite state database, kept apart from
the logs, remembers each session's status, snapshot and message cursor, plus a
watermark of the newest `updated_at` seen, so a restart, or two imports in a
row, never records anything twice and a poll only fetches what changed.

Every endpoint and field this module relies on is in `DevinClient`,
`_message_key` and `_status`; nothing else knows Devin's API. Written against
the v3 organization API (docs.devin.ai/api-reference, `v3-openapi.json`):

- `GET /v3/self` → `org_id` of the service user, when none is given
- `GET /v3/organizations/{org_id}/sessions` → `items[]` of SessionResponse,
  cursor-paged with `first` / `after` / `end_cursor` / `has_next_page`
- `GET /v3/organizations/{org_id}/sessions/{devin_id}/messages` → `items[]` of
  SessionMessage (`event_id`, `source`, `message`, `created_at`, ...)

Auth is a service-user key, `Authorization: Bearer cog_...`, with the
`ViewOrgSessions` permission. Note what the API does not expose: the shell
commands, edits and tool calls a session made. Only messages, status and usage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from flight_recorder.chain import Recorder, canonical, safe_name
from flight_recorder.redact import Redactor, keep

DEFAULT_BASE_URL = "https://api.devin.ai"
PAGE = 100
# Statuses after which nothing more happens to a session (SessionResponse.status).
FINAL = frozenset({"exit", "error"})


class DevinError(Exception):
    pass


def iso(value: Any) -> str | None:
    """Devin's timestamps are Unix epoch integers; logs use RFC 3339.

    Seconds or milliseconds, whichever the magnitude says. Anything else is
    passed through as text, or dropped.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        moment = datetime.fromtimestamp(seconds, UTC)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value)


def segment(value: str) -> str:
    # Ids come from the API or the operator: encode each whole, so a "/" or ".."
    # in one cannot aim the request at another endpoint.
    return quote(value, safe="")


class DevinClient:
    """Read access to one organization's sessions."""

    def __init__(
        self,
        api_key: str,
        *,
        org_id: str | None = None,
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
        self._org_id = org_id

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DevinError(f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code in (401, 403):
            raise DevinError(
                f"{method} {path}: {response.status_code}: check DEVIN_API_KEY and its "
                "role (ViewOrgSessions to read, ManageOrgSessions to act)"
            )
        if response.status_code >= 400:
            raise DevinError(f"{method} {path}: {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params={k: v for k, v in params.items() if v})

    @property
    def org_id(self) -> str:
        if not self._org_id:
            found = self._get("/v3/self").get("org_id")
            if not found:
                raise DevinError(
                    "GET /v3/self: this key has no organization; set DEVIN_ORG_ID "
                    "(an enterprise key can act in any organization)"
                )
            self._org_id = str(found)
        return self._org_id

    def _org(self, rest: str = "") -> str:
        return f"/v3/organizations/{segment(self.org_id)}/sessions{rest}"

    def _pages(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        after: str | None = None
        while True:
            page = self._get(path, first=params.pop("first", PAGE), after=after, **params)
            yield from (item for item in page.get("items") or [] if isinstance(item, dict))
            after = page.get("end_cursor")
            if not page.get("has_next_page") or not after:
                return

    def sessions(
        self, *, tags: list[str] | None = None, updated_after: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Every session the key can see (optionally: changed since a time)."""
        yield from self._pages(self._org(), tags=tags, updated_after=updated_after)

    def messages(
        self, session_id: str, *, after: str | None = None
    ) -> tuple[list[Any], str | None]:
        """Messages after a cursor, oldest first, and the cursor to resume from."""
        items: list[Any] = []
        cursor = after
        while True:
            page = self._get(self._org(f"/{segment(session_id)}/messages"), first=200, after=cursor)
            items.extend(page.get("items") or [])
            cursor = page.get("end_cursor") or cursor
            if not page.get("has_next_page"):
                return items, cursor

    def close(self) -> None:
        self._http.close()


def _message_key(message: dict[str, Any]) -> str:
    """A stable identity for a message: Devin's event id, else a content hash."""
    if message.get("event_id"):
        return str(message["event_id"])
    return "sha256:" + hashlib.sha256(canonical(message)).hexdigest()


def _status(session: dict[str, Any]) -> tuple[str, str | None]:
    """(status, status_detail): `running`/`waiting_for_user`, `suspended`/`inactivity`..."""
    detail = session.get("status_detail")
    return str(session.get("status") or "unknown"), (str(detail) if detail else None)


def log_path(log_dir: Path, session_id: str) -> Path:
    # Devin's ids already read "devin-…"; don't double the prefix.
    name = safe_name(session_id)
    return log_dir / f"{name if name.startswith('devin-') else 'devin-' + name}.ndjson"


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
                session_id TEXT PRIMARY KEY, status TEXT, snapshot TEXT, cursor TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )

    def seen(self, session_id: str, key: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM seen WHERE session_id = ? AND message_key = ?", (session_id, key)
        ).fetchone()
        return row is not None

    def mark(self, session_id: str, key: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (session_id, key))

    def last(self, session_id: str) -> tuple[str | None, str | None, str | None]:
        row = self._db.execute(
            "SELECT status, snapshot, cursor FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return (row[0], row[1], row[2]) if row else (None, None, None)

    def remember(self, session_id: str, status: str, snapshot: str, cursor: str | None) -> None:
        self._db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "status = excluded.status, snapshot = excluded.snapshot, cursor = excluded.cursor",
            (session_id, status, snapshot, cursor),
        )

    def watermark(self) -> int | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'updated_after'").fetchone()
        return int(row[0]) if row else None

    def set_watermark(self, value: int) -> None:
        self._db.execute(
            "INSERT INTO meta VALUES ('updated_after', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
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
    # A session's updated_at can trail its newest message by a little; re-list
    # this many seconds before the watermark so nothing slips between polls.
    overlap: int = 120

    def run(self, *, tags: list[str] | None = None, since: int | None = None) -> Summary:
        """One poll. `since` (epoch seconds) bounds the first poll; later polls
        use the stored watermark."""
        summary = Summary()
        mark = self.state.watermark()
        updated_after = mark - self.overlap if mark is not None else since
        newest = mark or 0
        for session in self.client.sessions(tags=tags, updated_after=updated_after):
            session_id = str(session.get("session_id", ""))
            if not session_id:
                continue
            self._import(session, session_id, summary)
            summary.sessions += 1
            updated = session.get("updated_at")
            if isinstance(updated, int | float) and not isinstance(updated, bool):
                newest = max(newest, int(updated / 1000 if updated > 1e11 else updated))
        if newest and not self.dry_run:
            self.state.set_watermark(newest)
            self.state.commit()
        return summary

    def _import(self, session: dict[str, Any], session_id: str, summary: Summary) -> None:
        status, detail = _status(session)
        status_key = f"{status}/{detail or ''}"
        snapshot = hashlib.sha256(canonical(session)).hexdigest()
        last_status, last_snapshot, cursor = self.state.last(session_id)
        changed_status = status_key != last_status
        changed_meta = snapshot != last_snapshot
        if not (changed_status or changed_meta):
            return  # nothing about it moved, so no new messages either

        fetched, next_cursor = self.client.messages(session_id, after=cursor)
        messages = [m for m in fetched if isinstance(m, dict)]
        new = [m for m in messages if not self.state.seen(session_id, _message_key(m))]

        if self.dry_run:
            if changed_meta:
                summary.planned.append(f"{session_id}  session.snapshot")
            if changed_status:
                summary.planned.append(
                    f"{session_id}  session.status  {last_status} -> {status_key}"
                )
            for m in new:
                summary.planned.append(f"{session_id}  message  {m.get('source', '?')}")
            return

        previous_status, _, previous_detail = (last_status or "").partition("/")
        with Recorder(
            log_path(self.log_dir, session_id), session_id=session_id, agent="devin"
        ) as rec:
            if changed_meta:
                rec.append(
                    "session.snapshot",
                    timestamp=iso(session.get("updated_at")),
                    flight={"source": "devin", "session": self._kept(session)},
                )
                summary.snapshots += 1
            for m in new:
                rec.append(
                    "message",
                    timestamp=iso(m.get("created_at")),
                    flight={
                        "source": "devin",
                        "from": m.get("source"),
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
            # After the messages: a status reflects what they led to.
            if changed_status:
                rec.append(
                    "session.status",
                    timestamp=iso(session.get("updated_at")),
                    flight={
                        "source": "devin",
                        "status": status,
                        "status_detail": detail,
                        "previous_status": previous_status or None,
                        "previous_status_detail": previous_detail or None,
                    },
                )
                summary.status_changes += 1
        self.state.remember(session_id, status_key, snapshot, next_cursor)
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
