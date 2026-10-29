"""`watchdog demo`: the whole pipeline against a scripted fake of Devin's API.

Five sessions run on a local fake of the v3 API, each showing one behaviour:

    checkout   deliberately stuck: the same failing test, the same error, ACUs
               climbing. Watchdog logs it, alerts, nudges, then puts it to sleep.
    docs       healthy: builds pass, a PR opens, it finishes. Never flagged.
    migrate    blocked on credentials, then waiting on a human: alerts only.
    search     just as stuck, but tagged watchdog:off: held, never touched.
    readme     drifts off-task, gets nudged, comes back: recovers.

Everything real runs: the importer polls over HTTP, entries are chained and
verified, the engine sends real requests (to the fake) and Teams cards (to the
fake's /teams). Only Devin is pretend.
"""

from __future__ import annotations

import itertools
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from devin_watchdog.fakedevin import FakeDevin

CHECKOUT = "devin-demo-checkout"
DOCS = "devin-demo-docs"
MIGRATE = "devin-demo-migrate"
SEARCH = "devin-demo-search"
README = "devin-demo-readme"

STUCK = [
    "Running `npm test -- checkout.spec.ts`. It failed: Error: Cannot find module "
    "'./payment-gateway' from 'src/checkout/payment.ts'",
    "I updated `src/checkout/payment.ts` to change the import path. Running "
    "`npm test -- checkout.spec.ts` again... still failing with Error: Cannot find module "
    "'./payment-gateway' from 'src/checkout/payment.ts'",
    "Let me try once more. `npm test -- checkout.spec.ts` failed again: Error: Cannot find "
    "module './payment-gateway' from 'src/checkout/payment.ts'",
]
DOCS_STEPS = [
    "I'll add a rate-limit section to the API docs. Reading `docs/api/limits.md` and the "
    "gateway config first.",
    "Drafted the rate-limit section with the per-plan request quotas.",
    "Updated `docs/api/limits.md` with response headers and a retry example.",
    "Ran `npm run build` for the docs site. Build passed.",
    "Added the new page to `docs/sidebar.json` so it shows in navigation.",
    "Ran `npm run build` again. Build passed. Opening a pull request.",
    None,  # the PR opens here
    "The PR is up for review: it adds the rate-limit docs page and the sidebar entry.",
    None,  # finished
]
MIGRATE_STEPS = [
    "Starting the billing database migration to Postgres 16. Checking connectivity first.",
    "`psql -h billing-db -U migrator` failed: FATAL: permission denied for database billing",
    "`psql -h billing-db -U migrator` failed again: permission denied for database billing. "
    "The migrator credentials seem to be missing the CONNECT grant.",
    "I don't have access to the billing database with the credentials provided. Could you "
    "grant CONNECT to the migrator role, or give me a secret with the right credentials?",
    None,  # now waiting for the user
]
SEARCH_LOOP = [
    "Running `pytest tests/test_indexer.py`. It failed: AssertionError: expected 120 docs, got 0",
    "Changed `search/indexer.py` batching. `pytest tests/test_indexer.py` still failing: "
    "AssertionError: expected 120 docs, got 0",
]
README_DRIFT = [
    "Looking at the README badge for the CI workflow.",
    "While here I noticed the authentication middleware stores session tokens in memory.",
    "Rewriting the authentication middleware to persist session tokens in Redis.",
    "Adding a Redis client module with connection pooling and retry logic.",
    "Writing migrations for a sessions table as a Redis fallback.",
    "Refactoring the login controller to use the new session store.",
    "Adding integration tests for session expiry across Redis restarts.",
    "Benchmarking Redis pipeline throughput for the session store.",
]
README_BACK = [
    "You're right, that was out of scope. Reverting the middleware work.",
    "Updated the CI badge and coverage badge URLs in `README.md` to the new workflow names.",
    "README badges now point at the renamed workflows; the badge images render correctly.",
]


class Scenario:
    """Advances the fake sessions one step at a time."""

    def __init__(self, fake: FakeDevin) -> None:
        self.fake = fake
        self.step = 0
        self.nudged: set[str] = set()
        fake.on_message = self._on_message
        fake.add(CHECKOUT, title="Fix the flaky checkout test in payments-service")
        fake.add(DOCS, title="Add a rate-limit section to the API docs")
        fake.add(MIGRATE, title="Migrate the billing database to Postgres 16")
        fake.add(SEARCH, title="Fix search indexing dropping documents", tags=["watchdog:off"])
        fake.add(README, title="Update the README badges after the CI rename")
        fake.say(CHECKOUT, "user", "The checkout test fails about half the time on CI. Fix it.")
        fake.say(README, "user", "Our CI workflows were renamed; update the README badges.")
        self._stuck = itertools.cycle(STUCK)
        self._search = itertools.cycle(SEARCH_LOOP)
        self._drift = iter(README_DRIFT)
        self._back = iter(README_BACK)

    def _on_message(self, sid: str, text: str) -> None:
        self.nudged.add(sid)
        if sid == CHECKOUT:  # deliberately stuck: it agrees, then carries on regardless
            self.fake.say(
                sid, "devin", "Understood. I'll look at the root cause before re-running."
            )

    def _running(self, sid: str) -> bool:
        session = self.fake.sessions[sid]
        return bool(session["status"] == "running" and session["status_detail"] == "working")

    def _spend(self, sid: str, acus: float) -> None:
        session = self.fake.sessions[sid]
        self.fake.update(sid, acus_consumed=round(session["acus_consumed"] + acus, 2))

    def advance(self) -> None:
        with self.fake.lock:
            self.step += 1
            if self._running(CHECKOUT):
                self.fake.say(CHECKOUT, "devin", next(self._stuck))
                self._spend(CHECKOUT, 0.45)
            if self._running(SEARCH):
                self.fake.say(SEARCH, "devin", next(self._search))
                self._spend(SEARCH, 0.3)
            self._docs()
            self._migrate()
            self._readme()

    def _docs(self) -> None:
        index = self.step - 1
        if index >= len(DOCS_STEPS) or not self._running(DOCS):
            return
        text = DOCS_STEPS[index]
        self._spend(DOCS, 0.25)
        if text is not None:
            self.fake.say(DOCS, "devin", text)
        elif index == 6:
            self.fake.update(
                DOCS,
                pull_requests=[
                    {"pr_url": "https://github.com/acme/api-docs/pull/482", "pr_state": "open"}
                ],
            )
        else:
            self.fake.update(DOCS, status_detail="finished")

    def _migrate(self) -> None:
        index = self.step - 1
        if index >= len(MIGRATE_STEPS) or not self._running(MIGRATE):
            return
        text = MIGRATE_STEPS[index]
        self._spend(MIGRATE, 0.2)
        if text is not None:
            self.fake.say(MIGRATE, "devin", text)
        else:
            self.fake.update(MIGRATE, status_detail="waiting_for_user")

    def _readme(self) -> None:
        if not self._running(README):
            return
        text = next(self._back if README in self.nudged else self._drift, None)
        if text is not None:
            self.fake.say(README, "devin", text)
            self._spend(README, 0.15)
        elif README in self.nudged:
            self.fake.update(README, status_detail="finished")


def serve_fake(fake: FakeDevin) -> ThreadingHTTPServer:
    import json

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            code, payload = fake.handle(self.command, self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # http.server dispatches on these names.
        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815
        do_PUT = _handle  # noqa: N815
        do_DELETE = _handle  # noqa: N815

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, name="fake-devin", daemon=True).start()
    return server


def start(step: float, stop: threading.Event) -> tuple[FakeDevin, Scenario, str]:
    """The fake API, its HTTP server and the scenario clock. Returns its base URL."""
    fake = FakeDevin()
    scenario = Scenario(fake)
    server = serve_fake(fake)

    def drive() -> None:
        while not stop.wait(step):
            scenario.advance()
        server.shutdown()

    threading.Thread(target=drive, name="scenario", daemon=True).start()
    host, port = server.server_address[:2]
    return fake, scenario, f"http://{host!s}:{port}"


def workdir(given: str | None) -> Path:
    return Path(given) if given else Path(tempfile.mkdtemp(prefix="watchdog-demo-"))
