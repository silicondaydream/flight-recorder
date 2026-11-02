# Watchdog

Watchdog notices Devin sessions that are stuck or drifting off-task and still
spending ACUs, and does something about it: log → Teams alert → nudge → sleep →
terminate (with a person's approval).

![The Watchdog dashboard during the demo](img/watchdog-dashboard.png)

## How it fits together

```
 Devin v3 API ──poll──▶ importer ──append──────────▶ session logs (hash-chained)
                                                           ▲
 Devin's machine: proxy ▶ watchdog tools                   │ POST /api/ingest
   (run_command, edit_file, …) ──ship verified entries─────┘ (each must chain on)
                                                           │
                                                    ▼ reader.follow(): verified entries only
                                              SessionView per session
                                                    │
                                     7 rules ──▶ findings ──▶ (optional LLM judge)
                                                    │
                                    ladder + guardrails (SQLite state)
                                                    │
                 ┌───────────────┬─────────────────┼───────────────┐
             Teams card    nudge message      archive (sleep)   terminate*
                                                                      ▲
                                    signed "Approve / Keep running" buttons on the card
                                                    │
                          watchdog-<session>.ndjson (its own chained log)
```

Two packages, one branch:

- **`flight_recorder`** records. The importer turns Devin's API into chained
  logs; the proxy does the same for MCP tool calls. It never acts.
- **`devin_watchdog`** judges and acts. It reads only verified log entries, never
  the API directly, so the record of what it saw is the record of what it acted
  on. Its own decisions go into a chained log too.

That split is the main design choice, and the main change from the October 19
plan (one poller reading the API straight into SQLite). What it buys:

- **Replay.** `watchdog replay <logs>` runs the rules over recorded sessions and
  prints what Watchdog would have done. Tune thresholds against real sessions
  before switching off DRY_RUN.
- **An audit trail.** Every nudge, sleep and terminate has a tamper-evident
  record of the findings and the evidence behind it. That matters most for
  terminate.
- **One code path for every source.** Devin's API, MCP servers through the
  proxy, and any future `shell.command` source all become the same entries.
- **A demo that is not a mock-up.** `watchdog demo` runs the real importer and
  engine against a fake API over HTTP.

## Signals: what Devin's API gives you

Devin's v3 API exposes, per session: `status` (`new`, `claimed`, `running`,
`exit`, `error`, `suspended`, `resuming`), `status_detail` (`working`,
`waiting_for_user`, `waiting_for_approval`, `finished`, or a suspension reason),
`acus_consumed`, `pull_requests`, `tags`, and the chat messages (`source` is
`devin` or `user`). It does **not** expose the shell commands, file edits or
tool calls a session made.

So:

| Source | Exact or heuristic | Feeds |
|---|---|---|
| Session status, ACUs, PRs, tags | exact | ACUs-without-progress, guardrails |
| Devin's messages ("Running `npm test`… failed: Error: …") | **heuristic**: commands in backticks, failure words, error lines, edited paths | failing commands, errors, edit thrash, loops, env blockers, drift |
| `watchdog tools`, given to Devin through `flight-recorder proxy` | **exact**: every command with its exit code, every edit | failing commands, errors, edit thrash, loops, env blockers |
| Other MCP servers given to Devin, through the proxy | exact, with outcomes | failing commands, errors, edits |
| `shell.command` / `file.edit` entries, if a source ever provides them | exact | the same |

The dashboard marks each finding "read from messages" or "exact", and a session
whose commands come through MCP shows "MCP · exact".

## Exact events: the `watchdog tools` MCP server

`watchdog tools` is a small shell and editor MCP server (standard library only).
Give it to Devin through the proxy and the four command rules stop guessing:

| Tool | Recorded as |
|---|---|
| `run_command` | `tool.call` with the command; `tool.result` with `event.outcome = failure` on a non-zero exit (MCP `isError`), and the output tail |
| `edit_file`, `write_file` | an edit of that path |
| `read_file` | nothing the rules count |
| `link_session` | which Devin session this log belongs to |

Three problems, and how each is solved:

1. **Devin has to use it.** Devin keeps its own shell. A Knowledge note tells it
   to run tests, builds and other commands through `run_command`
   (`watchdog mcp-setup` prints one). Commands Devin runs in its own shell are
   still only visible through its messages.
2. **Which session is this?** The server runs on the session machine, and Devin's
   docs describe no per-session variable or header a server could read. So
   Devin calls `link_session` once with its session link (Devin knows it: it
   puts it in its PRs). That call is recorded in the proxy's log, and when
   Watchdog reads it, it folds that log into the Devin session's view. An MCP
   log that never links stays a separate session that gets **alerts only**.
3. **The log is on Devin's machine.** With `WATCHDOG_INGEST_URL` and
   `WATCHDOG_INGEST_TOKEN` set (as Devin secrets), `watchdog tools` follows the
   proxy's log and posts each verified entry to Watchdog's public listener.
   Watchdog appends an entry only if it is the next link of that log's chain
   (the same check `verify` makes), so the copy verifies with the same head
   hash as the original. A restarted shipper resends from the start and
   duplicates are skipped; a gap is refused with the sequence to resend from.

Set up, in Devin (run `watchdog mcp-setup https://watchdog.example.com` for
copy-paste versions):

1. Environment/blueprint: `pip install 'git+https://github.com/silicondaydream/flight-recorder@devin'`
2. Customize → MCPs → custom server, STDIO: command `flight-recorder`, args
   `proxy --name watchdog-tools -- watchdog tools`, env `WATCHDOG_INGEST_URL`,
   with `WATCHDOG_INGEST_TOKEN` as a secret.
3. Organization Knowledge: the note `mcp-setup` prints.

## The seven rules

All thresholds are in `rules.Thresholds`. A rule only looks at what happened
since the later of one window ago (30 min) and the session's last progress: a
test or build passing, a PR opening or changing, a person replying, or
`status_detail` becoming `finished`. A recovering session stops matching on its
own.

| Rule | Fires when (defaults) | Ceiling |
|---|---|---|
| `repeated_failing_commands` | the same command (numbers and hashes normalised) fails 3 times with no success in between | terminate |
| `repeated_errors` | the same error signature (paths, quotes and numbers normalised) 3 times | terminate |
| `edit_thrash` | the same file edited 5 times without progress | terminate |
| `test_build_loop` | 4 test/build runs, none passing, at least half failing | terminate |
| `acus_without_progress` | ≥ 5 ACUs over ≥ 20 min since the last progress, while running | terminate |
| `environment_blocker` | 2 mentions of permission denied, missing credentials, command not found, rate limits, no space… | terminate |
| `scope_drift` | 5 of the last 6 Devin messages share no content word with the task (its first user message, title and Devin's first reply) | **nudge** |

Scope drift is the weakest signal, so on its own it never goes past a nudge. It
is the rule the LLM judge helps most.

## The ladder and its guardrails

One rung per cooldown (default 10 min) while a session keeps matching. Back to
the start after 30 minutes with no findings.

| Rung | What happens |
|---|---|
| log | Recorded in Watchdog's log and on the dashboard |
| alert | An Adaptive Card to the Teams webhook, with the rules, the evidence, ACUs and a link to the session |
| nudge | `POST /v3/organizations/{org}/sessions/{id}/messages`: the judge's nudge, or the worst finding's. It always starts with `[Watchdog]`, so Watchdog never mistakes its own message for a person replying |
| sleep | `POST …/archive`, which the API documents as "archive session and put it to sleep if currently running". Then the session is tagged `watchdog:slept` |
| terminate | An approval request: a Teams card with **Approve terminate** and **Keep running** buttons, and the same on the dashboard. `DELETE …/sessions/{id}` only after someone approves: from the card, the dashboard, or `watchdog approve <id>` |

Checked in this order, every tick:

1. **Running only.** A suspended session is never nudged: the API wakes a
   session to deliver a message, so a nudge would undo the sleep.
2. **`watchdog:off` tag.** Logged once, then left alone.
3. **Pending approval.** Nothing more until someone decides. **Keep running**
   (or `watchdog decline <id>`) holds terminate for the rest of the incident:
   Watchdog leaves the session where it is on the ladder, and starts over once
   it recovers.
4. **Cooldown** between rungs.
5. **Ceilings.** A rule's own ceiling (drift stops at nudge). A session
   **waiting on a human** (`waiting_for_user`, `waiting_for_approval`) gets
   alerts only. An MCP log that has not linked to a Devin session gets alerts
   only.
6. **The judge**, if configured, can veto the step. The veto holds for a
   cooldown.
7. **DRY_RUN** (the default): nothing is sent to Devin. Teams alerts still go
   out, marked dry run. The ladder still advances, so you see exactly what it
   would have done.

A log that fails verification stops Watchdog acting altogether, and it alerts.

## Approving from Teams

![The page an approval button opens](img/watchdog-approval.png)

Cards posted to a Teams incoming webhook can only open links: they cannot call
back. So the approval card's buttons are links to Watchdog's public listener,
signed with HMAC-SHA256 (key: `WATCHDOG_APPROVAL_TOKEN`) over the decision, the
session and an expiry (`approval_ttl`, an hour):

```
https://watchdog.example.com/approve?decision=approve&session=devin-…&expires=…&sig=…
```

Clicking one opens a confirmation page, and only that page's button acts (a
POST with the same signature). Opening the link never changes anything:
Teams, mail scanners and link previewers fetch links before anyone clicks.
After a click, the choice is recorded with who-clicked as "approval link (IP)",
because a signed link proves it came from Watchdog, not who used it. Post
approval cards only to a channel of people allowed to make that call.

For real one-click buttons that know who pressed them, use a Power Automate flow
with "Post adaptive card and wait for a response", calling `watchdog approve`.
The webhook and link approach needs nothing beyond a webhook URL.

## The LLM judge (optional)

`--judge-model claude-opus-5-5` (or `WATCHDOG_JUDGE_MODEL`). It is called only
when a rule fires and Watchdog is about to step up. It gets the task, the
findings and the last 40 observations, and answers with structured JSON:
`stuck`, `confidence`, `reason`, `nudge`. A confident "not stuck" holds
Watchdog back. A "stuck" verdict supplies the nudge text. An error or a refusal
leaves the rules in charge. Verdicts are cached per session and set of rules for
a cooldown, so a session that stays stuck costs one call per cooldown.

Session text goes to the judge inside `<session>` and is declared as data.
Session content is agent-written, so a prompt injection is possible. The worst
it can do is make the judge hold Watchdog back. It can never push Watchdog past
what the rules allow.

Needs `pip install '.[judge]'` and `ANTHROPIC_API_KEY`.

## Running it

A Devin **service user** (see Devin's API quick start) with `ViewOrgSessions`, and
`ManageOrgSessions` for nudge, sleep and terminate. Its key starts `cog_`.

```console
export DEVIN_API_KEY=cog_…               # environment only, never a flag
export DEVIN_ORG_ID=org-…                 # optional: found from the key via /v3/self
export TEAMS_WEBHOOK_URL=https://…        # a Teams Workflows webhook URL
watchdog run                              # DRY RUN; dashboard on http://127.0.0.1:8765
watchdog run --live                       # act on sessions
watchdog approve devin-1a2b3c             # allow a pending terminate
watchdog decline devin-1a2b3c             # keep it running
watchdog replay ~/.local/share/flight-recorder --cooldown 600

# The public listener: MCP log ingest and Teams approval links. Put HTTPS in
# front of it (a reverse proxy, or a tunnel for a hackathon).
export WATCHDOG_PUBLIC_URL=https://watchdog.example.com
export WATCHDOG_APPROVAL_TOKEN=$(openssl rand -hex 32)   # signs approval links
export WATCHDOG_INGEST_TOKEN=$(openssl rand -hex 32)     # Devin's machines send this
watchdog run --public 0.0.0.0:8766
watchdog mcp-setup https://watchdog.example.com          # what to paste into Devin
```

The dashboard stays on loopback because it shows session content. The public
listener serves only `/api/ingest` and `/approve`, each behind its own
credential.

Or in Docker (SQLite and logs on a volume):

```console
DEVIN_API_KEY=cog_… WATCHDOG_APPROVAL_TOKEN=… docker compose up -d watchdog
```

| Environment | Meaning |
|---|---|
| `DEVIN_API_KEY` | Service-user key (`cog_…`) |
| `DEVIN_ORG_ID` | Organization; default: the key's own |
| `TEAMS_WEBHOOK_URL` | Teams incoming webhook; without it, alerts are only logged |
| `WATCHDOG_DRY_RUN` | `1` (default) or `0`; `--live` overrides it |
| `WATCHDOG_JUDGE_MODEL` | Turns the judge on, with this model |
| `WATCHDOG_APPROVAL_TOKEN` | Signs Teams approval links; also required by the dashboard's approve button when it is not on loopback |
| `WATCHDOG_PUBLIC_URL` | Where browsers and Devin's machines reach the public listener; turns approval links on |
| `WATCHDOG_INGEST_TOKEN` | Bearer token for `/api/ingest`; turns ingest on. Set the same value as a Devin secret |
| `WATCHDOG_INGEST_URL` | (on Devin's machine, for `watchdog tools`) `$WATCHDOG_PUBLIC_URL/api/ingest`; must be https |
| `ANTHROPIC_API_KEY` | For the judge |

## The hackathon demo

```console
make setup
.venv/bin/watchdog demo                   # dashboard on http://127.0.0.1:8765
.venv/bin/watchdog demo --judge-model claude-opus-5-5   # with the judge (needs ANTHROPIC_API_KEY)
docker compose run --rm --service-ports demo           # the same, in Docker
```

Six scripted sessions on a local fake of the v3 API, a step every 4 seconds,
cooldown 12 seconds. The whole story takes about a minute and a half. The
dashboard is on :8765 and the public listener on :8766:

| Session | What it shows |
|---|---|
| **checkout** | Deliberately stuck: the same failing `npm test`, the same error, ACUs climbing. Logged at ~12 s, alert, nudge (it says "Understood" and carries on), then **asleep** at ~50 s. This is the "done" criterion |
| **docs** | Healthy: builds pass, a PR opens, it finishes. Never flagged |
| **migrate** | Blocked by permissions, then waiting on a human: alerted, held at alert, no nudge |
| **search** | Just as stuck as checkout, but tagged `watchdog:off`: held, never touched |
| **readme** | Drifts into rewriting auth middleware, gets nudged, comes back on task, recovers |
| **API client** | Works through **`watchdog tools`**, for real: a real proxy and tools server in a scratch workspace, really running `python3 -m unittest` (it fails on a wrong import) and rewriting the same file. The log ships over `/api/ingest`, links itself, and every finding is **exact**. It is slept, a teammate wakes it, it is still stuck, and Watchdog asks for approval. Click **Approve terminate** in the decisions feed: those are the links the Teams card carries |

Suggested talk track:

1. Open on the hero number (ACUs burning without progress).
2. Follow checkout up the ladder. Its findings say "read from messages".
3. Compare the API client session: "MCP · exact", with real exit codes.
4. Point at the decisions feed and the sessions Watchdog chose *not* to touch,
   and why.
5. When the API client asks for approval, click the Teams button and terminate
   it from the confirmation page.
6. End on the chained logs. The copy shipped from the "session machine" has
   the same head hash as the original:

```console
flight-recorder verify <demo dir>/logs <demo dir>/logs/watchdog <demo dir>/session-machine-logs
```

For the live version, start a real Devin session with a task it cannot finish
(for example "make `npm test` pass" in a repo whose test imports a module that
does not exist), run `watchdog run --live --cooldown 120`, and watch it climb.

## Known gaps

- **Real API calls not yet exercised.** Everything is written against Devin's
  published v3 OpenAPI spec and tested against a fake built from it. Run
  `flight-recorder import devin --dry-run` with a real key first.
- **Sleep is archive.** The API has no separate sleep endpoint. An archived
  session may drop out of the default list view; Watchdog tags it
  `watchdog:slept` so it can be found.
- **Exact events need Devin's cooperation.** It must call `link_session` and
  route commands through `run_command`; the Knowledge note asks it to, but
  nothing forces it. Anything run in its own shell is still heuristic.
- **A session could link itself to another session.** `link_session` is taken at
  its word, so an agent could make a different session in the same org look
  stuck. The link is in the chain, so it is visible afterwards.
- **The last proxy entry (`session.end`) is not shipped**: the proxy writes it
  after the tools server, and its shipper, have exited.
- **One poll a minute** by default, listing sessions updated since the last poll.
  Devin has no outbound session-event webhook in the v3 API, so polling it is.
