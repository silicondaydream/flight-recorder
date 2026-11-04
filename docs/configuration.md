# Configuration

Watchdog has two kinds of configuration:

- **Secrets and connections** are environment variables. They are never shown,
  saved or logged; the dashboard's **Settings → Connections** says only whether
  each is set.
- **Everything else is a setting**, changed from the dashboard's **Settings**
  panel. Settings are validated, saved to `settings.json` (next to Watchdog's
  state, `~/.local/state/watchdog/` by default) and applied within seconds,
  without a restart. Only changed values are saved, so a later version's new
  defaults still apply.

Command-line flags (`--live`, `--dry-run`, `--cooldown`, `--interval`, `--tick`,
`--tag`, `--judge-model`) override settings for one run. They are not saved, and
changing the same setting in the panel replaces them.

## Environment variables

| Variable | Where | Needed for |
|---|---|---|
| `DEVIN_API_KEY` | Watchdog | A Devin service-user key (`cog_…`). Reading sessions; nudge, sleep and terminate |
| `DEVIN_ORG_ID` | Watchdog | Optional. Default: the key's own organization (`GET /v3/self`) |
| `TEAMS_WEBHOOK_URL` | Watchdog | Optional. Teams cards (see [teams.md](teams.md)) |
| `WATCHDOG_PUBLIC_URL` | Watchdog | The https address browsers and Devin's machines reach the public listener at. Turns approval links on |
| `WATCHDOG_APPROVAL_TOKEN` | Watchdog | A random secret. Signs approval links. Also required by the dashboard for changes when it is not on loopback |
| `WATCHDOG_INGEST_TOKEN` | Watchdog *and* Devin | A random secret. Devin's machines send it with shipped logs. Turns ingest on |
| `OPENAI_API_KEY` | Watchdog | Optional. The AI judge: a ChatGPT Enterprise or OpenAI platform API key |
| `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` | Watchdog | Optional. Pin the judge's calls to an OpenAI organization or project |
| `OPENAI_BASE_URL` | Watchdog | Optional. A different endpoint, such as Azure OpenAI or a gateway |
| `WATCHDOG_DRY_RUN` | Watchdog | Optional. `1` or `0`: the dry-run setting for this run |
| `WATCHDOG_LOG_DIR` | both | Optional. Where logs go (default `~/.local/share/watchdog`) |
| `WATCHDOG_INGEST_URL` | Devin | For `watchdog tools`: `$WATCHDOG_PUBLIC_URL/api/ingest`. Must be https |

## Settings

The full list, as in the panel. The panel shows each with its help text and
limits.

### General

Acting or rehearsing, how often to look, which sessions to watch.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Dry run | `general.dry_run` | on | Decide and report, but send nothing to Devin. Teams cards still go out, marked dry run. |
| Check Devin every | `general.poll_seconds` | 30 s | How often sessions and messages are fetched from Devin's API. (2–600) |
| Evaluate rules every | `general.tick_seconds` | 2 s | How often the rules run over what has been recorded. (1–60) |
| Only sessions tagged | `general.session_tags` | (empty) | Comma-separated Devin tags. Empty: every session in the organization. |
| Opt-out tag | `general.off_tag` | watchdog:off | Sessions with this Devin tag are recorded but never acted on. |

### Escalation

The steps Watchdog takes for a stuck session, and how fast.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Wait between steps | `ladder.cooldown_seconds` | 600 s | A stuck session moves up one step per this interval. (5–86400) |
| Start over after | `ladder.reset_after_seconds` | 1800 s | This long with no findings and a session goes back to the start. (10–86400) |
| Step: alert | `ladder.alert` | on | Notify Teams. Off: skipped. |
| Step: nudge | `ladder.nudge` | on | Send the session a message. Off: skipped. |
| Step: sleep | `ladder.sleep` | on | Put the session to sleep. Off: skipped. |
| Step: terminate | `ladder.terminate` | on | Ask a person to approve terminating. Off: Watchdog stops at the step before. |
| While waiting on a person | `ladder.waiting_on_human` | Alert, no more | A session waiting for a reply is never nudged. How far it may go instead. (Log only, Alert, no more) |
| Approval links valid for | `ladder.approval_minutes` | 60 min | How long the Teams approval buttons work. (5–1440) |

### Rules

What counts as stuck. Each rule can be turned off or capped.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Look-back window | `rules.window_minutes` | 30 min | Rules only consider activity this recent (and after the last sign of progress). (1–1440) |
| Use findings read from messages | `rules.use_message_findings` | on | Commands and errors guessed from what the agent says. Off: only exact MCP events count. |
| Repeated failing command | `rules.repeated_failing_commands.enabled` | on | Turn this rule on or off. |
| Failures in a row | `rules.repeated_failing_commands.repeats` | 3 | The same command failing this many times with no success in between. (2–50) |
| Highest step | `rules.repeated_failing_commands.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| Repeated error | `rules.repeated_errors.enabled` | on | Turn this rule on or off. |
| Same error, times | `rules.repeated_errors.repeats` | 3 | The same error (numbers and paths ignored) this many times. (2–50) |
| Highest step | `rules.repeated_errors.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| Edit thrash | `rules.edit_thrash.enabled` | on | Turn this rule on or off. |
| Edits to one file | `rules.edit_thrash.repeats` | 5 | The same file edited this many times without progress. (2–100) |
| Highest step | `rules.edit_thrash.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| Test/build loop | `rules.test_build_loop.enabled` | on | Turn this rule on or off. |
| Test/build runs | `rules.test_build_loop.runs` | 4 | This many test or build runs with none passing. (2–100) |
| Highest step | `rules.test_build_loop.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| ACUs without progress | `rules.acus_without_progress.enabled` | on | Turn this rule on or off. |
| ACUs spent | `rules.acus_without_progress.acus` | 5 ACU | ACUs spent since the last progress (a passing test, a PR, a person replying). (0.5–500) |
| Over at least | `rules.acus_without_progress.minutes` | 20 min | …and at least this long since the last progress. (1–1440) |
| Highest step | `rules.acus_without_progress.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| Environment blocker | `rules.environment_blocker.enabled` | on | Turn this rule on or off. |
| Mentions | `rules.environment_blocker.mentions` | 2 | Permission denied, missing credentials, command not found… this many times. (1–50) |
| Highest step | `rules.environment_blocker.ceiling` | Terminate | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |
| Scope drift | `rules.scope_drift.enabled` | on | Turn this rule on or off. |
| Recent messages | `rules.scope_drift.messages` | 6 | How many of the agent's recent messages to compare with the task. (3–50) |
| Of which off-topic | `rules.scope_drift.off_topic` | 5 | How many of those may share nothing with the task before it counts. (1–50) |
| Highest step | `rules.scope_drift.ceiling` | Nudge | The furthest this rule alone can escalate a session. (Log, Alert, Nudge, Sleep, Terminate) |

### Teams

What reaches the Teams channel, how often, and how much detail.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Send Teams cards | `teams.enabled` | on | Needs TEAMS_WEBHOOK_URL. Off: everything is still on the dashboard. |
| Card detail | `teams.verbosity` | Standard | Compact: one line. Standard: the key facts. Detailed: evidence and recent activity. (Compact, Standard, Detailed) |
| Include agent messages | `teams.include_agent_text` | off | Detailed cards show the agent's last messages. Off keeps session content out of Teams. |
| At most one card per session every | `teams.per_session_seconds` | 300 s | Approval requests and errors always go straight through. (0–86400) |
| At most, per hour | `teams.max_per_hour` | 30 | Across all sessions. Extra cards are dropped and counted on the next one. (1–1000) |
| Batch into a digest every | `teams.digest_minutes` | 0 min | 0: send each card as it happens. Otherwise non-urgent cards are combined. (0–1440) |
| Send cards in dry run | `teams.in_dry_run` | on | Rehearse the notifications too. Cards say dry run. |
| When a problem is first logged | `teams.on_log` | off |  |
| When the alert step | `teams.on_alert` | on | The ladder's alert step. |
| When a session is nudged | `teams.on_nudge` | on |  |
| When a session is put to sleep | `teams.on_sleep` | on |  |
| When a session is terminated | `teams.on_terminate` | on |  |
| When approval is needed | `teams.on_approval` | on | The card with the Approve / Keep running buttons. |
| When someone approves or declines | `teams.on_decision` | on |  |
| When someone acts from the dashboard | `teams.on_manual` | on |  |
| When a session recovers | `teams.on_recovered` | off |  |
| When Watchdog holds back | `teams.on_held` | off | A guardrail stopped a step (opt-out tag, waiting on a person, a step's ceiling). |
| When the AI judge says not stuck | `teams.on_judge_veto` | off |  |
| When a session starts | `teams.on_session_start` | off |  |
| When a session ends or finishes | `teams.on_session_end` | off |  |
| When a session runs long | `teams.on_runtime` | on |  |
| Long-running after | `teams.runtime_minutes` | 60 min | Once per session. (1–10080) |
| When a session's spend passes | `teams.on_acus` | on |  |
| Spend threshold | `teams.acus_threshold` | 20 ACU | Once per session. (0.5–10000) |
| When a session waits on a person | `teams.on_waiting` | off |  |
| Waiting for | `teams.waiting_minutes` | 15 min | Once per wait. (1–1440) |
| When Watchdog hits an error | `teams.on_errors` | on | A log that fails verification, a failed API call. |

### Live log

What the dashboard's live log shows, and how fast it refreshes.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Show | `feed.level` | Notable | Decisions: what Watchdog did. Notable: plus findings and status changes. Everything: plus every agent message, tool call and result. (Decisions, Notable, Everything) |
| Keep | `feed.max_items` | 200 items | Entries in the live log. (20–2000) |
| Refresh every | `feed.refresh_seconds` | 2 s |  (0.5–60) |
| Show held-back decisions | `feed.show_held` | on |  |
| Trim text to | `feed.text_chars` | 280 chars |  (40–5000) |
| Session detail keeps | `feed.detail_entries` | 500 entries | Recorded entries per session for the detail view (applies to new sessions). (50–5000) |
| Hide ended sessions after | `feed.hide_ended_minutes` | 120 min | 0: never hide. (0–10080) |

### AI judge

An optional second opinion from an OpenAI model before each step.

| Setting | Key | Default | Meaning |
|---|---|---|---|
| Ask the AI judge | `judge.enabled` | off | Needs OPENAI_API_KEY (a ChatGPT Enterprise or OpenAI API project key). |
| Model | `judge.model` | gpt-5 | Any chat model your OpenAI organization can use. |
| Reasoning effort | `judge.reasoning_effort` | Default | For reasoning models only. Default: the model's own. (Default, Low, Medium, High) |
| Ask before steps from | `judge.from_rung` | Nudge | Steps below this go ahead on the rules alone. (Log, Alert, Nudge, Sleep, Terminate) |
| Hold back when 'not stuck' at | `judge.min_confidence` | 0.6 | The judge's confidence needed to stop a step. (0–1) |
| At most, per hour | `judge.max_calls_per_hour` | 60 calls |  (1–10000) |

