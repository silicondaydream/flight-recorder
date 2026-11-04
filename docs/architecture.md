# Architecture

Watchdog has three jobs, kept apart on purpose: **record** what agent sessions
do, **judge** whether a session is in trouble, and **act** on it. Recording never
depends on judging, and judging only ever reads what was recorded, so the record
of what Watchdog saw is exactly the record of what it acted on.

## The picture

```
 ┌───────────────────────────── Devin session machine (one per session) ──────────┐
 │                                                                                │
 │   Devin (the agent) ──stdio──▶ watchdog proxy ──stdio──▶ watchdog tools         │
 │                                    │                     run_command, edit_file, │
 │                                    │ appends             write_file, read_file,  │
 │                                    ▼                     link_session            │
 │                        proxy log (hash-chained)                                  │
 │                                    │ Shipper: follows the log, sends each        │
 └────────────────────────────────────┼─ verified entry ───────────────────────────┘
                                      │ HTTPS POST /api/ingest (bearer token)
                                      ▼
 ┌────────────────────────────────── Watchdog host ──────────────────────────────────┐
 │                                                                                   │
 │  public listener ── ingest: appends an entry only if it chains on ──┐            │
 │  (/api/ingest, /approve)                                            ▼            │
 │                                   log directory: one hash-chained log per session │
 │  importer ── polls Devin's API ──▶ devin-<id>.ndjson   mcp-<id>.ndjson            │
 │  (sessions, messages,                     │                                        │
 │   status, ACUs, PRs)                      │ reader.follow(): verified entries only │
 │                                           ▼                                        │
 │                     observe: one SessionView per session                           │
 │                     (commands + exit codes, errors, edits, blockers, messages,    │
 │                      status, ACU history, signs of progress)                       │
 │                                           │  link_session folds an MCP log into    │
 │                                           │  its Devin session                     │
 │                                           ▼                                        │
 │                     rules (7, pure functions) ──▶ findings                         │
 │                                           │                                        │
 │                     engine: escalation ladder + guardrails ◀── settings (live)     │
 │                          │            │             │                              │
 │                          │            │             └─▶ optional AI judge (OpenAI) │
 │                          │            └─▶ notifier ──▶ Teams webhook               │
 │                          └─▶ Watchdog's own log: watchdog/watchdog-<id>.ndjson     │
 │                                                                                   │
 │  dashboard (loopback) ── sessions, live log, session detail, settings, controls   │
 └───────────────────────────────────────┬───────────────────────────────────────────┘
                                         ▼
                  Devin API: message (nudge) · archive (sleep) · delete (terminate)
```

## The pieces

| Piece | File | Does |
|---|---|---|
| MCP proxy | `proxy.py` | Starts a stdio MCP server and relays JSON-RPC both ways, byte for byte. Records every `tools/call` before forwarding it, then its result, outcome and duration. |
| Tools server | `tools.py` | The MCP server Devin uses for shell and file work, so commands arrive with real exit codes. Its `Shipper` thread sends the proxy's log to Watchdog. |
| Log | `chain.py`, `reader.py` | One NDJSON file per session. Each entry holds the SHA-256 of the one before it. `follow()` yields only entries whose link and hash check out. |
| Redaction | `redact.py` | Removes tokens, keys and passwords before anything is written, then caps payload size. |
| Importer | `devin.py` | Polls Devin's v3 API incrementally (a watermark, plus a cursor per session) and writes messages, status changes and metadata into the logs. |
| Ingest | `ingest.py`, `public.py` | Receives shipped MCP log entries. Appends them only if each is the next link of its chain. |
| Observe | `observe.py` | Folds entries into a `SessionView`. Commands and errors come exactly from MCP events, or heuristically from Devin's messages. |
| Rules | `rules.py` | Seven pure functions of a view and the time. Each returns a finding with its evidence and the nudge it would send. |
| Engine | `engine.py` | The ladder, the guardrails, manual actions, lifecycle notices, and the dashboard's view of it all. Writes every decision to Watchdog's own chained log. |
| Settings | `settings.py` | One schema for every setting, with validation, persistence (`settings.json`) and live reads. The settings panel is generated from it. |
| Notifier | `notify.py` | Decides which notices become Teams cards: triggers, rate limits, digest, verbosity. |
| AI judge | `judge.py` | An OpenAI chat model with a strict JSON schema. It can stop a step, never add one. |
| Dashboard | `dashboard.py`, `.html` | The single-page console: sessions, live log, session detail, settings, controls. |

## A stuck session, step by step

1. Devin runs `python3 -m unittest` through `run_command`. The proxy writes a
   `tool.call` entry, forwards the call, and writes `tool.result` with
   `event.outcome = failure` when the exit code is non-zero.
2. The shipper sends both entries to `/api/ingest`. Watchdog checks they chain
   onto that log and appends them to `mcp-<proxy session>.ndjson`.
3. The feed thread reads them back, verified, and `observe` adds a failing
   command and an error signature to the session's view. Devin's first call was
   `link_session`, so this view *is* the Devin session's view.
4. On the next tick the rules run. After the third failure in a row,
   `repeated_failing_commands` returns a finding.
5. The engine checks the guardrails (running? muted? opted out? waiting on a
   person? cooldown? this rule's highest step?), asks the judge if it is on, and
   takes the next enabled step: **log**. A cooldown later, still stuck: **alert**,
   then **nudge**, then **sleep**.
6. Each step is written to `watchdog/watchdog-<session>.ndjson` and offered to
   the notifier, which sends a Teams card if that step's trigger is on and no
   rate limit applies.
7. If the session is woken and is still stuck, the next step is
   **terminate**. Watchdog posts an approval card with signed *Approve* and
   *Keep running* links, and does nothing until someone decides.

## Design choices

- **Judge only what was recorded.** The engine never calls Devin's API to look;
  it reads the verified log. So `watchdog replay` reproduces exactly what
  Watchdog would have done, and every action has its evidence in a
  tamper-evident record.
- **Exact beats heuristic.** Devin's API exposes messages, status and credit
  use, but not the commands a session ran. Through the tools server they are
  exact. Without it, the rules read commands out of what Devin says, and every
  finding says which kind it is.
- **One step at a time, with a cooldown.** An agent gets a chance to recover
  after each step, and a false positive costs a Teams card, not a terminated
  session.
- **People decide the irreversible step.** Terminate always waits for approval.
- **Settings are data, not code.** Every threshold and switch is in one schema,
  read live, so the demo can be tuned from the dashboard mid-session.
- **Standard library where it runs on Devin's machine.** The proxy, log and tools
  server need only Python, so installing Watchdog in a session's environment
  pulls in nothing else.

## Trust and security

- **The agent is untrusted.** Everything it sends can be shaped by whoever
  steers it. The proxy relays without changing anything, redacts before writing,
  and records calls before forwarding them.
- **Logs are tamper-evident.** Editing, removing or reordering an entry breaks
  the chain. A broken chain stops Watchdog from acting, and it says so.
- **Two listeners.** The dashboard binds to loopback (it shows session content).
  The public listener serves only `/api/ingest` (bearer token) and `/approve`
  (HMAC-signed, expiring links whose GET only shows a confirmation page).
- **Secrets are environment variables**, never settings, never logged. The
  settings panel only says whether each is set.
- **The AI judge sees session content.** It is given that content as data
  inside `<session>`, can only hold Watchdog back, and is off by default.

See [SECURITY.md](../SECURITY.md) for the threat table.

## Known limits

- **Exact events need Devin's cooperation.** It must call `link_session` and use
  `run_command`; the Knowledge note asks it to. Anything run in its own shell is
  only seen through its messages.
- **Sleep is archive.** Devin's API has no separate sleep call; archiving puts a
  running session to sleep. Watchdog tags it `watchdog:slept`.
- **Polling.** Devin has no outbound session events, so Watchdog polls (every 30
  seconds by default, as often as every 2).
- **A session could claim another's link** with `link_session`. The claim is in
  the chain, so it can be spotted afterwards.
