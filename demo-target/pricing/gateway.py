"""The payments sandbox client. Reads its credentials from the environment."""

from __future__ import annotations

import json
import os
import urllib.request

SANDBOX = os.environ.get("PAYMENTS_SANDBOX_URL", "https://payments-sandbox.internal.example")


def quote(amount_cents: int) -> dict[str, object]:
    token = os.environ["PAYMENTS_SANDBOX_TOKEN"]  # KeyError when the secret is missing
    request = urllib.request.Request(
        f"{SANDBOX}/v1/quotes?amount={amount_cents}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return dict(json.load(response))
