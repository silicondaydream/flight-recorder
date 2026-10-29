# REMAINING TASKS

Updated 2026-10-29 (devin branch). Everything in section 1 was run.

## 1. Where things stand — verified

```console
make check     # ruff, mypy --strict, all tests: passing
```

Also checked by hand:

- A session recorded through the proxy (with `tests/fake_server.py`) verifies, and
  `tail` shows it.
- `lincoln ingest` on the log directory needs no format pack. LQL over
  `flight.tool`, `event.outcome` and `event.duration` works.
- `watchdog demo` end to end: the stuck session goes log → alert → nudge →
  sleep in about a minute. The healthy one is never flagged, waiting-on-human
  stops at alert, `watchdog:off` is held, and drift is nudged and recovers.
  Dashboard checked at 1500px (light and dark) and 390px.
- `watchdog run --no-import` against the demo's logs; a non-editable install
  ships `dashboard.html`.

**The Devin importer now targets the v3 API**, from Devin's published OpenAPI
spec (`docs.devin.ai/v3-openapi.json`, read 2026-10-29). v1 is deprecated.

**Not checked yet: a real Devin org.** No key was available. Run
`flight-recorder import devin --dry-run` with a service-user key (`cog_…`), then
`watchdog run` (DRY RUN) and read its decisions before `--live`. Things to
confirm against the real API:
- whether list-sessions includes archived sessions by default (a slept session
  must keep being imported);
- that the `qs` query object really serialises as flat `first`, `after`,
  `updated_after` and `tags` parameters (the docs' own examples use flat
  `first`/`after`);
- whether `created_at` / `updated_at` are seconds (assumed; ms is handled too).

**Not checked yet: Docker.** There was no Docker daemon in the build sandbox.
`docker compose run --rm --service-ports demo` is the first thing to try.

**Not checked yet: a real MCP server through the proxy.** Same as before; try
`@modelcontextprotocol/server-everything` in a real client.

## Branches

- `main` — the general recorder: proxy, chain, redaction, verify and tail. No
  vendor code and no runtime dependencies.
- `devin` — `main` plus the Devin API importer and **Watchdog**
  (`src/devin_watchdog`). Merge `main` into it to pick up core changes; never
  merge `devin` into `main`.

## Watchdog (the work task this branch serves)

`docs/watchdog.md` is the reference: architecture, rules, ladder, guardrails,
config, and the demo script. Layout:

- `observe.py` folds verified entries into a `SessionView` per session. The
  commands, errors and edits it reads out of Devin's messages are heuristic.
- `rules.py` has the 7 rules as pure functions, and `Thresholds`.
- `engine.py` has the ladder, guardrails, SQLite state, Watchdog's own chained
  log, and the dashboard snapshot.
- `judge.py` is the optional Claude judge. It can veto a step, never add one.
- `actions.py` has the Devin write calls (message, archive, tags, delete) and
  the Teams card.
- `runner.py` wires importer, feed and engine threads; `cli.py` is
  `watchdog run|demo|approve|replay`.
- `fakedevin.py` is the v3 fake used by the tests and the demo; `demo.py` is
  the scenario.

Rule for this package: it reads only `reader.follow()`, never Devin's API, so
what it acted on is always in the chain.

## 2. Next

0. **(devin) Run against a real org**, as above. Then tune `Thresholds` with
   `watchdog replay` over a week of real sessions before `--live`.
1. **(devin) Commands for the heuristic rules.** The v3 API has no commands or
   edits. Options: give Devin shell/editor MCP servers through the proxy (exact
   events, and the rules already read `tool.call`/`tool.result`); or check
   whether Devin adds a session events/timeline endpoint (`insights.timeline`
   exists but is post-hoc).
2. **(devin) Teams approval button.** The approval card links to the session;
   an `Action.Http`/Workflows callback to `/api/approve` would make approval one
   click from Teams.
3. **Streamable HTTP MCP proxy.** Only stdio servers today. Many hosted servers
   are HTTP.
4. **Anchoring.** `verify --head` prints the head hash; nothing stores it. A
   `flight-recorder anchor` that appends heads to a separate file, or posts them
   somewhere the agent cannot write, would close the deleted-tail gap.
5. **Lincoln's audit chain has the ambiguity fixed here.** It concatenates fields
   with no separators. That is a Lincoln issue, worth filing there.
6. **Site docs.** Add `/docs/flight-recorder/` to arboria-foundation.

## Out of scope — do not add

Acting on recorded content, rewriting or filtering traffic, sending logs anywhere
by default, sampling or dropping entries, and vendor-specific code on `main`.
Reasons in [CONTRIBUTING.md](CONTRIBUTING.md#things-that-will-be-declined).
Acting belongs in a separate supervisor package (Watchdog here), never in
`flight_recorder`.
