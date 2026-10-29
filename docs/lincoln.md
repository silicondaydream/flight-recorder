# Querying with Lincoln

[Lincoln](https://github.com/silicondaydream/lincoln) reads Flight Recorder logs
with its stock JSON parser. There is no format pack to install, because the
field names are already ECS.

```console
lincoln ingest ~/.local/share/flight-recorder
lincoln serve                                  # the web UI, or query from the CLI:
```

## Queries

What each agent session did, call by call:

```
event.action = "tool.call" | fields @timestamp, session.id, flight.server, flight.tool | sort @timestamp
```

Failures by tool:

```
event.action = "tool.result" | stats count() as calls by flight.tool, event.outcome | sort -calls
```

The slowest calls (`event.duration` is in nanoseconds):

```
event.action = "tool.result" | sort -event.duration | fields flight.tool, event.duration, session.id | head 10
```

Every session that called a given tool:

```
event.action = "tool.call" flight.tool = "run_shell" | stats count() as calls by session.id
```

Devin sessions that went to `blocked`:

```
agent.name = "devin" event.action = "session.status" flight.status = "blocked" | fields @timestamp, session.id
```

Something hidden in a payload, anywhere:

```
"ACME-123456"
```

## Keep the evidence honest

Run `flight-recorder verify` on the directory before ingesting it. Lincoln
records its own audit chain of what you queried, but it does not re-check
Flight Recorder's chain.
