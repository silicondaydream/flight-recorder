"""Receiving MCP proxy logs from Devin's session machines.

A stdio MCP server runs on the Devin session machine, so the proxy in front of
it writes its log there, not where Watchdog runs. `watchdog tools` ships that
log's entries to Watchdog's public listener (`POST /api/ingest`). This module
is the receiving end.

Entries are stored exactly as sent, one file per proxy session
(`mcp-<session>.ndjson`) in the log directory, so Watchdog's verified feed picks
them up like any other log. Nothing is appended unless it is the next link of
that session's chain. That is checked with the same `chain.check` that
`verify` uses, so a shipped log verifies on the receiving side too. Entries the
store already holds are skipped, so a shipper can resend from the start after a
restart. A gap is refused with the sequence the store expects next, so the
shipper can resend from there.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flight_recorder.chain import BrokenLinkError, check, safe_name, verify_file

MAX_ENTRIES = 2000  # per request


@dataclass
class Result:
    accepted: int = 0
    skipped: int = 0
    error: str = ""
    session: str = ""
    expected: int = 0  # on a gap: the sequence to resend from


class IngestStore:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self._heads: dict[str, tuple[int, str]] = {}  # session -> (sequence, hash)
        self._lock = threading.Lock()

    def path(self, session: str) -> Path:
        return self.log_dir / f"mcp-{safe_name(session)}.ndjson"

    def _head(self, session: str) -> tuple[int, str]:
        if session not in self._heads:
            path = self.path(session)
            if path.exists():
                result = verify_file(path)
                if not result.valid:
                    raise BrokenLinkError(f"{path.name} does not verify: {result.reason}")
                self._heads[session] = (result.entries, result.head)
            else:
                self._heads[session] = (0, "0" * 64)
        return self._heads[session]

    def add(self, entries: list[Any]) -> Result:
        if len(entries) > MAX_ENTRIES:
            return Result(error=f"at most {MAX_ENTRIES} entries per request")
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            if not isinstance(entry, dict):
                return Result(error="every entry must be an object")
            session = str(entry.get("session", {}).get("id", ""))
            if not session:
                return Result(error="an entry has no session.id")
            by_session[session].append(entry)
        result = Result()
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            for session, batch in by_session.items():
                if not self._add(session, batch, result):
                    return result
        return result

    def _add(self, session: str, batch: list[dict[str, Any]], result: Result) -> bool:
        try:
            sequence, head = self._head(session)
        except BrokenLinkError as exc:
            result.error, result.session = str(exc), session
            return False
        lines: list[str] = []
        for entry in sorted(batch, key=lambda e: int(e.get("event", {}).get("sequence", 0))):
            found = int(entry.get("event", {}).get("sequence", 0))
            if found <= sequence:
                result.skipped += 1  # already held: a resend
                continue
            if found != sequence + 1:
                result.error, result.session = "gap in the chain", session
                result.expected = sequence + 1
                break
            line = json.dumps(entry, ensure_ascii=False)
            try:
                check(line, sequence=sequence + 1, previous=head)
            except BrokenLinkError as exc:
                result.error, result.session = f"entry {found}: {exc}", session
                result.expected = sequence + 1
                break
            sequence, head = found, entry["chain"]["hash"]
            lines.append(line + "\n")
        if lines:
            # 0600, like every log: these are tool calls and their output.
            fd = os.open(self.path(session), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())
            self._heads[session] = (sequence, head)
            result.accepted += len(lines)
        return not result.error
