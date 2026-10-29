# Devin

Devin works in its own environment: a shell, an editor, a browser. Those
actions never pass through an MCP proxy. Flight Recorder covers Devin in two
ways:

1. **MCP servers you give Devin.** Configure each custom MCP server with
   `flight-recorder proxy --name <name> -- <command>` as the command. Every call
   Devin makes to that server is recorded live, like any other client.
2. **Everything else, through the API.** `flight-recorder import devin` reads
   sessions through Devin's v3 REST API and records their messages, status
   changes and metadata.

## Importing

```console
export DEVIN_API_KEY=cog_…                   # a service-user key; environment only, never a flag
export DEVIN_ORG_ID=org-…                    # optional: otherwise read from GET /v3/self
flight-recorder import devin --dry-run       # check first: prints what it would record
flight-recorder import devin                 # one pass
flight-recorder import devin --watch --interval 60
flight-recorder import devin --tag team-a    # only sessions with this tag
flight-recorder import devin --since-hours 0 # first run: every session, not the last 24 h
```

The key needs the `ViewOrgSessions` permission. Each session gets
`<session id>.ndjson` (Devin's ids start `devin-`) in the log directory,
extended on every pass. A state database remembers each session's status,
snapshot and message cursor, and the newest `updated_at` seen. A restart or a
repeated run adds only what is new, and a poll lists only sessions updated since
the last one. The database is SQLite, at
`~/.local/state/flight-recorder/devin-state.db`, kept out of the log directory so
that ingesting the logs never reads it.

| Recorded | When | `flight` holds |
|---|---|---|
| `session.snapshot` | The session's metadata changed | `session`: the whole SessionResponse: `acus_consumed`, `pull_requests`, `tags`, `status`, `status_detail`, `title`, `url`… |
| `message` | Each new message, at the time it was sent | `from` (`devin` or `user`), `origin`, `username`, `event_id`, `text` |
| `session.status` | `status` or `status_detail` changed | `status`, `status_detail`, `previous_status`, `previous_status_detail` |

## The API it uses

Written against Devin's published v3 OpenAPI spec (`docs.devin.ai/v3-openapi.json`).
v1 and v2 are deprecated.

| Call | Used for |
|---|---|
| `GET /v3/self` | the key's `org_id`, when none is given |
| `GET /v3/organizations/{org_id}/sessions?first&after&updated_after&tags` | sessions, cursor-paged (`items`, `end_cursor`, `has_next_page`) |
| `GET /v3/organizations/{org_id}/sessions/{devin_id}/messages?first&after` | messages, oldest first, cursor-paged |

Timestamps are Unix epochs; the importer writes them as RFC 3339. Every endpoint
and field name is in `DevinClient`, `_message_key` and `_status` in
`src/flight_recorder/importers/devin.py`. Watchdog's write calls are in
`src/devin_watchdog/actions.py`.

**What the API does not expose:** the shell commands, file edits and tool calls
a session made. Only messages, status and usage. The `insights` endpoints add an
AI-written analysis (`issues`, `timeline`), but it is generated after the fact,
not live.

## Feeding a supervisor

Watchdog, on this branch, is the supervisor these logs were built for. See
[watchdog.md](watchdog.md). To write your own:

```python
from pathlib import Path
from flight_recorder.reader import follow

for entry in follow(Path("~/.local/share/flight-recorder").expanduser()):
    # Every entry here has already been verified against the chain.
    session = entry["session"]["id"]
    action = entry["event"]["action"]
    ...
```

The supervisor keeps its own state, alerts and actions. Flight Recorder only
records. That way the record stays trustworthy whatever the supervisor does, and
the supervisor's actions can be recorded too.
