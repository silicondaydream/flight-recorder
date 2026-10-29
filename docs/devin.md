# Devin

Devin works in its own environment: a shell, an editor, a browser. Those
actions never pass through an MCP proxy. Flight Recorder covers Devin in two
ways:

1. **MCP servers you give Devin.** Configure each custom MCP server with
   `flight-recorder proxy --name <name> -- <command>` as the command. Every call
   Devin makes to that server is recorded live, like any other client.
2. **Everything else, through the API.** `flight-recorder import devin` reads
   sessions through Devin's REST API and records their messages, status changes
   and metadata.

## Importing

```console
export DEVIN_API_KEY=…                       # environment only, never a flag
flight-recorder import devin --dry-run       # check first: prints what it would record
flight-recorder import devin                 # one pass
flight-recorder import devin --watch --interval 60
flight-recorder import devin --tag team-a    # only sessions with this tag
```

Each session gets `<session id>.ndjson` (Devin's ids start `devin-`) in the log
directory, extended on every pass. A state database remembers what has been
recorded, so a restart or a repeated run adds only what is new. It is SQLite, at
`~/.local/state/flight-recorder/devin-state.db` unless `--state` is given, and it
is kept out of the log directory so that ingesting the logs never reads it.

| Recorded | When |
|---|---|
| `session.snapshot` | The session's metadata changed. Every field the API returns is kept, including usage figures, PR links and tags. |
| `session.status` | The status changed, with the previous status |
| `message` | Each new message, at the time it was sent |

## Check it against your organisation

The importer was written against Devin's v1 API (`GET /v1/sessions`,
`GET /v1/session/{id}`, `Authorization: Bearer`, messages with `event_id`,
`type`, `message`, `timestamp`). Devin's documentation could not be reached from
the environment it was built in. Before relying on it:

1. Run `--dry-run` with a real key and confirm the sessions and messages it
   lists.
2. If your organisation uses a different API version or base URL, pass
   `--base-url`. If field names differ, the only places to change are
   `DevinClient` and `_message_key` in `src/flight_recorder/importers/devin.py`.
3. If the API exposes the commands a session ran (not only its messages), add
   them in `Importer._import` as their own action, such as `shell.command`.

## Feeding a supervisor

A supervisor such as Watchdog, which notices stuck or drifting sessions,
needs a reliable stream of what each session is doing. Flight Recorder provides
that and leaves the judgment to the supervisor:

```console
flight-recorder import devin --watch --interval 60 &     # writes logs
flight-recorder tail --follow --json ~/.local/share/flight-recorder | watchdog
```

Or in Python:

```python
from pathlib import Path
from flight_recorder.reader import follow

for entry in follow(Path("~/.local/share/flight-recorder").expanduser()):
    # Every entry here has already been verified against the chain.
    session = entry["session"]["id"]
    action = entry["event"]["action"]
    ...  # rules: repeated failures, edit thrash, status "blocked", no progress
```

What the recorded fields support, mapped to the kinds of rules a supervisor
runs:

| Signal | From |
|---|---|
| Repeated failing tool calls, repeated errors | `tool.result` with `event.outcome = failure`, grouped by `flight.tool` and its arguments |
| A session waiting on a human (don't nudge) | `session.status` → `blocked` |
| Spend with no progress | usage fields in `session.snapshot`, compared across snapshots, against message activity |
| Scope drift | `message` text over time, against the session's first user message |

The supervisor keeps its own state, alerts and actions (nudge, sleep,
terminate). Flight Recorder only records. That way the record stays trustworthy
whatever the supervisor does, and its actions can be recorded too.
