"""What Watchdog can do to a session, and how it tells people.

The Devin write endpoints (v3 organization API, `ManageOrgSessions`):

- nudge:     `POST /v3/organizations/{org_id}/sessions/{devin_id}/messages`
             `{"message": ...}`. Messaging a suspended session wakes it up, so
             the engine never nudges one.
- sleep:     `POST .../sessions/{devin_id}/archive`: "archive session and put it
             to sleep if currently running". The API's only way to sleep a
             session. Watchdog also tags it `watchdog:slept`, so people can find
             it, and it can be unarchived.
- terminate: `DELETE .../sessions/{devin_id}`. Only ever after a person approves.

Teams alerts go to an incoming webhook (a Teams Workflows "post to a channel
when a webhook request is received" URL) as an Adaptive Card.
"""

from __future__ import annotations

from typing import Any

import httpx

from flight_recorder.importers.devin import DevinClient, DevinError, segment

SLEPT_TAG = "watchdog:slept"


class DevinActions(DevinClient):
    def nudge(self, session_id: str, text: str) -> None:
        self._request("POST", self._org(f"/{segment(session_id)}/messages"), json={"message": text})

    def sleep(self, session_id: str) -> None:
        self._request("POST", self._org(f"/{segment(session_id)}/archive"))
        self._request("POST", self._org(f"/{segment(session_id)}/tags"), json={"tags": [SLEPT_TAG]})

    def terminate(self, session_id: str) -> None:
        self._request("DELETE", self._org(f"/{segment(session_id)}"))


class Teams:
    def __init__(self, webhook_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.url = webhook_url
        self._http = httpx.Client(transport=transport, timeout=15.0)

    def post(self, title: str, facts: dict[str, str], text: str = "", link: str = "") -> None:
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [{"title": k, "value": v} for k, v in facts.items()],
            },
        ]
        if text:
            body.append({"type": "TextBlock", "text": text, "wrap": True})
        card: dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
        }
        if link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open session", "url": link}]
        payload = {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }
        try:
            response = self._http.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise DevinError(f"Teams webhook: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise DevinError(f"Teams webhook: {response.status_code}: {response.text[:200]}")

    def close(self) -> None:
        self._http.close()
