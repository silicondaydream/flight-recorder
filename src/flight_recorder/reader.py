"""Read logs back, verifying every entry as it is read.

`follow` is the feed for anything that watches agents live (a supervisor such
as Watchdog, a dashboard): it yields each entry of a growing log, or of every
log in a directory, only after checking its link and its hash. A consumer never
acts on an entry the chain does not vouch for.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from flight_recorder.chain import GENESIS, BrokenLinkError, Entry, check


class ChainError(Exception):
    def __init__(self, path: Path, line: int, reason: str) -> None:
        super().__init__(f"{path}:{line}: {reason}")
        self.path, self.line, self.reason = path, line, reason


@dataclass
class _Cursor:
    """Where one file has been read and verified up to."""

    path: Path
    offset: int = 0
    line: int = 0
    sequence: int = 0
    previous: str = GENESIS

    def read(self) -> Iterator[Entry]:
        """Every complete, verified entry past the offset. A partial last line waits."""
        with self.path.open("rb") as f:
            f.seek(self.offset)
            while True:
                raw = f.readline()
                if not raw.endswith(b"\n"):
                    return  # EOF, or a line still being written
                self.offset += len(raw)
                self.line += 1
                if raw.strip():
                    yield self._check(raw)

    def _check(self, raw: bytes) -> Entry:
        try:
            entry = check(raw, sequence=self.sequence + 1, previous=self.previous)
        except BrokenLinkError as exc:
            raise ChainError(self.path, self.line, str(exc)) from None
        self.sequence, self.previous = self.sequence + 1, entry["chain"]["hash"]
        return entry


def logs_in(path: Path) -> list[Path]:
    return sorted(path.glob("*.ndjson")) if path.is_dir() else [path]


def entries(path: Path) -> Iterator[Entry]:
    """Every verified entry of a file, or of each file in a directory, in turn."""
    for log in logs_in(path):
        yield from _Cursor(log).read()


def follow(
    path: Path, *, poll: float = 0.5, stop: Callable[[], bool] = lambda: False
) -> Iterator[Entry]:
    """Verified entries as they are written, until `stop()` is true.

    `path` may be a file or a directory; in a directory, logs that appear later
    are picked up too. Entries from different files interleave in the order
    they are read.
    """
    cursors: dict[Path, _Cursor] = {}
    while True:
        for log in logs_in(path):
            if log.exists():
                cursor = cursors.setdefault(log, _Cursor(log))
                yield from cursor.read()
        if stop():
            return
        time.sleep(poll)
