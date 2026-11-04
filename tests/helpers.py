"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from agent_watchdog.settings import Config


def config(
    *, dry_run: bool = True, cooldown: float = 600.0, reset_after: float = 1800.0, **values: Any
) -> Config:
    """Settings for a test: the common ones by name, any other by its key."""
    return Config(
        values={
            "general.dry_run": dry_run,
            "ladder.cooldown_seconds": cooldown,
            "ladder.reset_after_seconds": reset_after,
            **values,
        }
    )
