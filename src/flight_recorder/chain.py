"""The hash-chained log: append entries, read them back, verify them.

One file per recorded session, one JSON object per line. Every entry carries
the hash of the entry before it and a hash of itself, so editing, removing or
reordering any entry breaks every link after it.

The hash is SHA-256 over the entry's canonical JSON (sorted keys, no
whitespace, UTF-8) with `chain.hash` removed. Hashing one unambiguous
serialisation, rather than concatenating fields, means two different entries
can never hash the same input: "ab" + "c" and "a" + "bc" are different JSON.

What this proves, and what it does not: the chain detects tampering *within* a
log that is kept. It cannot detect the last entries, or the whole file, being
deleted — a shorter chain is still a valid chain. Anchoring the head hash
somewhere the agent cannot write (`flight-recorder verify --head`) closes that.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flight_recorder import __version__

GENESIS = "0" * 64

Entry = dict[str, Any]


def canonical(value: Any) -> bytes:
    """The one serialisation that is hashed."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: Entry) -> str:
    """SHA-256 of the entry with its own `chain.hash` left out."""
    chain = {k: v for k, v in entry.get("chain", {}).items() if k != "hash"}
    return hashlib.sha256(canonical({**entry, "chain": chain})).hexdigest()


def now() -> str:
    """UTC, millisecond precision, RFC 3339 — the form Lincoln parses."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_name(name: str) -> str:
    """A file-name fragment from untrusted text: no separators, no dot-dot."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)
    return cleaned.strip("-.") or "unnamed"


class Recorder:
    """Appends entries to one session's log.

    Safe to share between threads: the proxy writes from both directions of a
    connection at once. Every entry is flushed and fsynced before `append`
    returns, because an entry lost in a buffer when the process dies leaves a
    gap that looks like nothing happened.
    """

    def __init__(self, path: Path, *, session_id: str, agent: str) -> None:
        self.path = path
        self.session_id = session_id
        self.agent = agent
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resume an existing chain rather than forking it.
        self._sequence, self._previous = 0, GENESIS
        if path.exists():
            result = verify_file(path)
            if not result.valid:
                raise ValueError(f"{path}: refusing to extend a broken chain: {result.reason}")
            self._sequence, self._previous = result.entries, result.head
        # 0600: a log of tool calls can hold whatever the agent saw.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._file = os.fdopen(fd, "a", encoding="utf-8")

    @property
    def sequence(self) -> int:
        return self._sequence

    def append(
        self,
        action: str,
        *,
        outcome: str | None = None,
        timestamp: str | None = None,
        **sections: Any,
    ) -> Entry:
        """Write one entry. `sections` are merged in as top-level objects.

        `timestamp` is when the event happened, for entries imported after the
        fact; it defaults to now. Either way `event.ingested` is when it was
        written, so a reader can tell a live record from an import.
        """
        with self._lock:
            written = now()
            event: dict[str, Any] = {
                "sequence": self._sequence + 1,
                "action": action,
                "ingested": written,
            }
            if outcome is not None:
                event["outcome"] = outcome
            event.update(sections.pop("event", {}))
            entry: Entry = {
                "@timestamp": timestamp or written,
                "event": event,
                "session": {"id": self.session_id},
                "agent": {"name": self.agent},
                **sections,
                "recorder": {"version": __version__},
                "chain": {"previous": self._previous},
            }
            entry["chain"]["hash"] = entry_hash(entry)
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
            self._sequence += 1
            self._previous = entry["chain"]["hash"]
            return entry

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class Verification:
    valid: bool
    entries: int
    head: str
    broken_at: int | None = None  # line number of the first bad entry
    reason: str | None = None


def read_lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            if line.strip():
                yield number, line


class BrokenLinkError(ValueError):
    """An entry that does not fit the chain at this point."""


def check(raw: str | bytes, *, sequence: int, previous: str) -> Entry:
    """Parse one line and check it is the next link: entry `sequence` after `previous`.

    The one definition of a valid link, shared by `verify` and the live reader.
    """
    try:
        entry: Entry = json.loads(raw)
        found = entry["event"]["sequence"]
        chain = entry["chain"]
    except (ValueError, KeyError, TypeError):
        raise BrokenLinkError("not a recorder entry") from None
    if found != sequence:
        raise BrokenLinkError(
            f"sequence {found}, expected {sequence}: an entry is missing or reordered"
        )
    if chain.get("previous") != previous:
        raise BrokenLinkError("does not link to the entry before it")
    if chain.get("hash") != entry_hash(entry):
        raise BrokenLinkError("contents changed after it was written")
    return entry


def verify_lines(lines: Iterator[tuple[int, str]]) -> Verification:
    """Check every link and every hash, stopping at the first failure."""
    previous, count = GENESIS, 0
    for number, line in lines:
        try:
            entry = check(line, sequence=count + 1, previous=previous)
        except BrokenLinkError as exc:
            return Verification(False, count, previous, number, str(exc))
        previous, count = entry["chain"]["hash"], count + 1
    return Verification(True, count, previous)


def verify_file(path: Path) -> Verification:
    return verify_lines(read_lines(path))
