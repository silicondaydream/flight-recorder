"""Which events reach Teams, how often, and with how much detail.

Everything Watchdog notices or does can become a notice: a step on the ladder,
an approval request, a session starting, running long or spending past a
threshold. Settings decide which notices become a Teams card (`teams.on_*`),
and how often:

- **per session**: at most one card every `teams.per_session_seconds`;
- **overall**: at most `teams.max_per_hour` cards an hour;
- **digest**: with `teams.digest_minutes` > 0, non-urgent notices are combined
  into one card per interval.

Urgent notices (an approval request, an error) skip all three. A dropped notice
is counted, and the next card says how many were dropped. Every notice is kept
for the dashboard whether or not it became a card.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_watchdog.actions import Teams
from agent_watchdog.devin import DevinError
from agent_watchdog.settings import Config

URGENT = frozenset({"approval", "errors"})
STYLE = {  # Adaptive Card container styles
    "approval": "attention",
    "errors": "attention",
    "terminate": "attention",
    "sleep": "warning",
    "nudge": "warning",
    "alert": "warning",
    "recovered": "good",
    "decision": "accent",
}


@dataclass
class Notice:
    trigger: str  # the `teams.on_<trigger>` setting that governs it
    session: str
    title: str
    summary: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    link: str = ""
    actions: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False
    at: float = 0.0
    outcome: str = ""  # sent / off / limited / digest / no webhook / failed: …

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["actions"] = [list(a) for a in self.actions]
        return d


class Notifier:
    def __init__(
        self, config: Config, teams: Teams | None, clock: Callable[[], float] = time.time
    ) -> None:
        self.config = config
        self.teams = teams
        self.clock = clock
        self.history: deque[Notice] = deque(maxlen=300)
        self._last: dict[str, float] = {}  # session -> last card
        self._hour: deque[float] = deque()
        self._dropped = 0
        self._digest: list[Notice] = []
        self._digest_since = 0.0

    @property
    def configured(self) -> bool:
        return self.teams is not None

    def notify(self, notice: Notice) -> str:
        notice.at = notice.at or self.clock()
        notice.outcome = self._route(notice)
        self.history.append(notice)
        return notice.outcome

    def _route(self, notice: Notice) -> str:
        c = self.config
        if self.teams is None:
            return "no webhook"
        if not c.flag("teams.enabled") or not c.flag(f"teams.on_{notice.trigger}"):
            return "off"
        if notice.dry_run and not c.flag("teams.in_dry_run"):
            return "off (dry run)"
        now = notice.at
        urgent = notice.trigger in URGENT
        if not urgent:
            gap = c.num("teams.per_session_seconds")
            if gap and now - self._last.get(notice.session, -1e18) < gap:
                self._dropped += 1
                return "limited (per session)"
            if c.num("teams.digest_minutes") > 0:
                if not self._digest:
                    self._digest_since = now
                self._digest.append(notice)
                self._last[notice.session] = now
                return "queued for digest"
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()
        if not urgent and len(self._hour) >= int(c.num("teams.max_per_hour")):
            self._dropped += 1
            return "limited (per hour)"
        return self._send(card(notice, c, dropped=self._dropped), notice.session, now)

    def _send(self, content: dict[str, Any], session: str, now: float) -> str:
        if self.teams is None:
            return "no webhook"
        try:
            self.teams.send(content)
        except DevinError as exc:
            return f"failed: {exc}"
        self._hour.append(now)
        self._last[session] = now
        self._dropped = 0
        return "sent"

    def flush(self) -> None:
        """Send the digest when it is due. Called every tick."""
        minutes = self.config.num("teams.digest_minutes")
        if not self._digest or self.teams is None:
            return
        now = self.clock()
        if minutes > 0 and now - self._digest_since < minutes * 60:
            return
        batch, self._digest = self._digest, []
        self._send(digest_card(batch, self._dropped), "digest", now)

    def test(self) -> str:
        """A test card, from the settings panel: ignores triggers and limits."""
        if self.teams is None:
            return "no webhook: set TEAMS_WEBHOOK_URL"
        notice = Notice(
            "decision",
            "-",
            "Test card",
            "If you can read this, Watchdog can reach this channel.",
            facts={"Card detail": str(self.config.get("teams.verbosity"))},
            at=self.clock(),
        )
        notice.outcome = self._send(card(notice, self.config), "-", notice.at)
        self.history.append(notice)
        return notice.outcome


def _text(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, "wrap": True, **extra}


def card(notice: Notice, config: Config, *, dropped: int = 0) -> dict[str, Any]:
    """One Adaptive Card, as much as `teams.verbosity` allows."""
    level = str(config.get("teams.verbosity"))
    title = ("[dry run] " if notice.dry_run else "") + notice.title
    head: list[dict[str, Any]] = [
        _text("WATCHDOG", size="Small", weight="Bolder", isSubtle=True, spacing="None"),
        _text(title, size="Medium", weight="Bolder"),
    ]
    if notice.summary:
        head.append(_text(notice.summary, spacing="Small"))
    body: list[dict[str, Any]] = [
        {"type": "Container", "style": STYLE.get(notice.trigger, "default"), "items": head}
    ]
    if level != "compact" and notice.facts:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": k, "value": v} for k, v in notice.facts.items() if v],
            }
        )
    if level == "detailed":
        if notice.evidence:
            body.append(_text("Evidence", weight="Bolder", spacing="Medium"))
            body.extend(_text(f"• {e}", spacing="None") for e in notice.evidence[:6])
        if notice.messages and config.flag("teams.include_agent_text"):
            body.append(_text("Recent agent activity", weight="Bolder", spacing="Medium"))
            body.extend(_text(m, isSubtle=True, spacing="Small") for m in notice.messages[-3:])
    if dropped:
        body.append(_text(f"{dropped} earlier notice(s) not sent (rate limits).", isSubtle=True))
    buttons = list(notice.actions)
    if notice.link:
        buttons.append(("Open session", notice.link))
    content: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if buttons:
        content["actions"] = [{"type": "Action.OpenUrl", "title": t, "url": u} for t, u in buttons]
    return content


def digest_card(batch: list[Notice], dropped: int) -> dict[str, Any]:
    sessions = len({n.session for n in batch})
    body: list[dict[str, Any]] = [
        _text("WATCHDOG DIGEST", size="Small", weight="Bolder", isSubtle=True),
        _text(f"{len(batch)} updates across {sessions} session(s)", size="Medium", weight="Bolder"),
    ]
    for n in batch[-15:]:
        when = time.strftime("%H:%M", time.localtime(n.at))
        body.append(_text(f"{when}  **{n.title}**  {n.summary}", spacing="Small"))
    if dropped:
        body.append(_text(f"{dropped} more not sent (rate limits).", isSubtle=True))
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
