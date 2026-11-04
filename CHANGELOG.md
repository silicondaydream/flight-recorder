# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Devin hooks.** Watchdog's Devin plugin (`devin-plugin/`) runs on every tool
  call Devin makes, including its own shell (`exec`) and editor (`edit`,
  `write`, `apply_patch`), and on prompts, session starts and wakes, and turn
  ends. Each event goes to the new `POST /api/hook`. The hook script redacts,
  spools locally, sends with a short timeout and backs off, never blocks a
  tool, and always exits 0.
- Watchdog records hook events in hash-chained logs. The rules read native
  commands and edits exactly, and the dashboard shows **Exact (hooks)**.
- Hook sessions are linked to their Devin session by a `devin-…` id when the
  hook carries one, otherwise by matching the first prompt.
- **Nudges through the hook**: **Settings → Escalation → Deliver nudges** can
  put the nudge in the agent's context on its next tool call, and the session
  page can send such a note by hand.
- `watchdog hooks-setup` prints the plugin setup (`--export` writes the
  plugin), `docs/devin-hooks.md` covers it all, and `docs/hook-probe.json`
  checks Devin's hook payloads in your organization.
- The demo adds a session seen through the real hook script, linked by its first
  prompt and nudged through the hook.

## [0.2.0]

### Added

- **Settings, applied live.** Every threshold, step and switch is a validated
  setting, changed from the dashboard's Settings panel, saved to
  `settings.json` and read on every tick. Flags override settings for one run.
- **Granular Teams notifications.** A switch for each trigger: each ladder step,
  approvals and decisions, dashboard actions, recoveries, guardrails, judge
  vetoes, sessions starting and ending, running long, passing an ACU threshold,
  waiting on a person, and errors. A per-session and an hourly rate limit, an
  optional digest, three levels of detail, and a test card.
- **Session detail view**: an overview with findings, evidence and the nudge
  each would send; every recorded entry with its raw JSON; the signals the
  rules use; every decision and what became of its Teams notice.
- **Manual controls**: send a message, sleep, terminate, mute and unmute a
  session from the dashboard, recorded like every other decision.
- **Live log levels**: decisions, notable, or everything the agent does.
- **Lifecycle notices**: session started, finished, running long, spend
  threshold, waiting on a person, each once.
- **Steps can be turned off**, and each rule can be turned off or capped.
- `demo-target/`: a project an agent cannot finish, for the demo.
- Docs: architecture, hackathon runbook, configuration, Devin and Teams setup.

### Changed

- **One app, Watchdog.** One package (`agent_watchdog`) and one command
  (`watchdog proxy|tools|run|demo|…`). Log entries keep their content under
  `activity`; the log directory is `~/.local/share/watchdog`.
- **The AI judge runs on OpenAI** (a ChatGPT Enterprise or OpenAI platform API
  key, `OPENAI_API_KEY`), with a strict JSON-schema response. Model, reasoning
  effort, which steps it is asked about, and an hourly budget are settings.
- **The dashboard is rebuilt**: plain-language states ("Needs approval",
  "Waiting on you", "Asleep") and next steps ("Will nudge in 42s"), new colours.

## [0.1.0]

### Added

- The MCP proxy, hash-chained logs, redaction, `verify` and `tail`.
- The Devin v3 importer.
- The supervisor: seven rules, the escalation ladder with guardrails,
  approval links for Teams, `watchdog tools` with log shipping and
  `link_session`, the dashboard, `replay` and the scripted demo.
