# REMAINING TASKS

Updated 2026-11-04. Everything in section 1 was run.

Watchdog is one app: an MCP proxy and supervisor that records agent sessions
and their tool use, notices when one is stuck or drifting, and steps in (alert,
nudge, sleep, terminate with approval). It is built for Devin. This repository is
about to move to the organization's own repository (Bitbucket, visible to
Devin); `demo-target/` becomes its own repository there.

## 1. Where things stand — verified

```console
make check     # ruff, mypy --strict, all tests: passing
```

Also checked by hand:

- `watchdog demo` end to end, with the rebuilt dashboard. The session detail
  drawer (overview, activity with raw entries, signals, decisions and Teams
  notices), the settings panel (every group) and the phone layout (390px, no
  horizontal scroll) were checked in Chromium, with no console errors.
- The demo's MCP session: a real `watchdog proxy` + `watchdog tools` really runs
  `python3 -m unittest`. Its log ships over `/api/ingest` and links via
  `link_session`. It climbs to an approval request, and the signed link
  terminated it.
- `demo-target/`: `python3 -m unittest` fails three ways (the two rounding
  tests contradict each other, and the gateway test needs a missing secret).

**Not checked yet** (no credentials in the build sandbox):

- **A real Devin org.** Run `watchdog import devin --dry-run` with the
  service-user key, then `watchdog run` in dry run. Confirm against the real
  API:
  - whether list-sessions includes archived sessions by default;
  - that `updated_after` and `tags` work as flat query parameters;
  - that timestamps are seconds.
- **`watchdog tools` inside a real Devin session**: the STDIO server starts with
  `watchdog` on the PATH, Devin follows the Knowledge note, and its machine
  reaches `WATCHDOG_INGEST_URL`.
- **A real Teams webhook** rendering the cards and their buttons.
- **The OpenAI judge against the real API.** It is tested with a fake client. The
  default model name `gpt-5` is a guess: use **Settings → AI judge → Test AI
  judge**, and change the model to one the organization's key can use.
- **Docker.** No daemon in the build sandbox.

## Layout

`src/agent_watchdog/`, one package, one `watchdog` command. See README's
"Repository layout". The parts that run on an agent's machine (`proxy`, `tools`,
`chain`, `reader`, `redact`) are standard-library only. `settings.py` is the
single schema for every setting; the dashboard's settings panel and
`docs/configuration.md` come from it (regenerate the doc's table when fields
change).

Rule: the engine reads only verified log entries (`reader.follow()`), never
Devin's API, so what it acted on is always in the chain.

## 2. Next

0. **Run against the real org** (above), in dry run first. Tune `Thresholds` in
   Settings, or with `watchdog replay` over recorded sessions, before going live.
1. **Move the repository.** Then update the `--repo` default in `cli.py`
   (`mcp-setup`) and the install line in `docs/devin.md` to the new URL. Split
   out `demo-target/`.
2. **Approver identity.** A signed link proves the link, not the person. For
   named approvals, use a Power Automate "post card and wait for a response"
   flow, or a Teams bot (`Action.Execute`), calling `watchdog approve`.
3. **Streamable HTTP MCP proxy.** Only stdio servers today.
4. **Anchoring.** `verify --head` prints the head hash; nothing stores it
   somewhere the agent cannot write.

## Out of scope — do not add

Rewriting or blocking an agent's traffic (the proxy relays byte for byte),
sending logs anywhere by default, sampling or dropping entries, terminating
without a person's approval, and secrets as settings.
