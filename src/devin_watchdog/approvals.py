"""Signed approval links: approving a terminate from a Teams card.

A Teams incoming webhook can only show link buttons (`Action.OpenUrl`): cards
posted that way cannot call back. So the card's "Approve terminate" and "Keep
running" buttons open a link to Watchdog's public listener, signed with
HMAC-SHA256 over the decision, the session and an expiry:

    {public_url}/approve?decision=approve&session=devin-…&expires=1793…&sig=…

Opening the link shows a confirmation page, and only the button on that page
(a POST carrying the same signature) acts. A GET never changes anything,
because Teams, mail scanners and link previewers fetch links before anyone
clicks them.

The signature proves the link came from Watchdog and has not expired. It does
not say who clicked it: anyone the card was shared with can use it, so post
approval cards only to a channel of people who may make that call.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import urlencode

DECISIONS = ("approve", "decline")


@dataclass(frozen=True)
class Signer:
    key: str  # WATCHDOG_APPROVAL_TOKEN
    public_url: str  # where people's browsers reach the public listener

    def _sig(self, decision: str, session: str, expires: int) -> str:
        message = f"{decision}\n{session}\n{expires}".encode()
        return hmac.new(self.key.encode(), message, hashlib.sha256).hexdigest()

    def link(self, decision: str, session: str, expires: int) -> str:
        query = urlencode(
            {
                "decision": decision,
                "session": session,
                "expires": expires,
                "sig": self._sig(decision, session, expires),
            }
        )
        return f"{self.public_url.rstrip('/')}/approve?{query}"

    def check(self, decision: str, session: str, expires: str, sig: str, now: float) -> str:
        """'' when the link is good, else why not."""
        if decision not in DECISIONS:
            return "unknown decision"
        try:
            when = int(expires)
        except ValueError:
            return "malformed link"
        if not hmac.compare_digest(self._sig(decision, session, when), sig):
            return "this link was not signed by Watchdog"
        if now > when:
            return "this link has expired"
        return ""
