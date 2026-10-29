"""Running Watchdog: importer → logs → verified feed → engine, on threads.

    importer thread   polls Devin's API, appends to the session logs
    feed thread       `reader.follow()` over the log directory: verified entries only
    main thread       folds entries into views and ticks the engine
    dashboard thread  serves the page and `/api/state`

The engine never reads Devin's API itself: everything it judges went through
the chain first, so the record of what it saw cannot differ from what it acted on.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from devin_watchdog.engine import Watchdog
from flight_recorder.importers.devin import DevinError, Importer
from flight_recorder.reader import ChainError, follow

Feed = queue.Queue[tuple[str, Any]]


def start_feed(log_dir: Path, stop: threading.Event, poll: float = 0.5) -> Feed:
    feed: Feed = queue.Queue()

    def run() -> None:
        try:
            for entry in follow(log_dir, poll=poll, stop=stop.is_set):
                feed.put(("entry", entry))
        except ChainError as exc:
            feed.put(("broken", str(exc)))

    log_dir.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=run, name="feed", daemon=True).start()
    return feed


def start_importer(
    make: Callable[[], Importer],
    interval: float,
    stop: threading.Event,
    *,
    since: int | None = None,
    tags: list[str] | None = None,
    on_error: Callable[[str], None] = lambda m: print(f"watchdog: {m}", file=sys.stderr),
) -> threading.Thread:
    def run() -> None:
        importer = make()  # SQLite connections stay on the thread that made them
        try:
            while not stop.is_set():
                try:
                    importer.run(tags=tags, since=since)
                except DevinError as exc:
                    on_error(str(exc))
                stop.wait(interval)
        finally:
            importer.client.close()
            importer.state.close()

    thread = threading.Thread(target=run, name="importer", daemon=True)
    thread.start()
    return thread


def loop(engine: Watchdog, feed: Feed, stop: threading.Event, tick: float = 2.0) -> None:
    """Fold entries in as they arrive; tick the engine every `tick` seconds."""
    next_tick = 0.0
    while not stop.is_set():
        try:
            kind, value = feed.get(timeout=max(0.0, next_tick - time.monotonic()))
        except queue.Empty:
            pass
        else:
            if kind == "entry":
                engine.observe(value)
            else:
                engine.chain_broken(value)
        if time.monotonic() >= next_tick:
            engine.tick()
            next_tick = time.monotonic() + tick
