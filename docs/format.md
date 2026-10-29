# Log format

One file per recorded session, one JSON object per line (NDJSON), UTF-8,
created with mode `0600`. Field names follow the Elastic Common Schema where one
exists, so ECS-aware tools (Lincoln, Elastic, anything that reads NDJSON) need no
mapping.

## An entry

```json
{
  "@timestamp": "2026-10-28T23:42:58.147Z",
  "event": {
    "sequence": 8,
    "action": "tool.result",
    "ingested": "2026-10-28T23:42:58.147Z",
    "outcome": "success",
    "duration": 29000000
  },
  "session": { "id": "8a3978ab…" },
  "agent": { "name": "mcp-proxy" },
  "flight": {
    "server": "demo",
    "tool": "echo",
    "request_id": "1",
    "result": { "value": { "content": [{ "type": "text", "text": "deploy to staging" }] } }
  },
  "recorder": { "version": "0.1.0" },
  "chain": { "previous": "4c1f…", "hash": "9fb2…" }
}
```

| Field | Meaning |
|---|---|
| `@timestamp` | When it happened. An importer sets the platform's own time. |
| `event.ingested` | When Flight Recorder wrote it |
| `event.sequence` | 1, 2, 3… within the file, with no gaps |
| `event.action` | What it records (see below) |
| `event.outcome` | `success`, `failure` or `unknown`, where it applies |
| `event.duration` | Nanoseconds, from the call to its result (ECS unit) |
| `session.id` | The proxy's session id, or an importer's session id |
| `agent.name` | `mcp-proxy`, or the importer's platform |
| `flight.*` | The recorded content, by action |
| `chain.previous`, `chain.hash` | The links |

## Actions

| Action | Written by | `flight` holds |
|---|---|---|
| `session.start` | proxy | `server`, `command` (redacted), `cwd`, `log` |
| `session.initialize` | proxy | `client`: the client's name and version |
| `session.initialized` | proxy | `server_info`: the server's name and version |
| `tool.call` | proxy | `tool`, `request_id`, `arguments` |
| `tool.result` | proxy | `tool`, `request_id`, `result` (or the JSON-RPC error) |
| `recorder.error` | proxy | `side`, `error`, `line`: something relayed that could not be read |
| `session.end` | proxy | `exit_code`, `messages` (counts by method), `unanswered` (tools) |

Importers (on their own branches) add their own actions, documented there.

## Kept payloads

`arguments`, `result`, `text`, `line` and `session` are wrapped:

```json
{ "value": … }                                              // as sent
{ "value": …, "redactions": 2 }                             // secrets replaced
{ "truncated": true, "bytes": 812345, "sha256": "…", "preview": "…" }
```

A truncated payload keeps its full size and SHA-256, so the cut is visible and
can be matched against a copy kept elsewhere. Redaction always runs before the
cut.

## The chain

- The first entry's `chain.previous` is 64 zeros.
- `chain.hash` is the SHA-256 of the entry's **canonical JSON** with
  `chain.hash` removed: keys sorted, no whitespace, UTF-8, non-ASCII unescaped.
  It is the same as Python's
  `json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- Each entry's `chain.previous` is the `chain.hash` of the entry before it.

Hashing one serialisation, rather than concatenating fields, means no two
different entries can produce the same input. Verifying needs only a JSON parser
and SHA-256, so a log can be checked without Flight Recorder.

**Detected:** an edited entry, an edited entry that was re-hashed (caught at the
next link), a removed or reordered entry, and a line that is not an entry.
**Not detected on its own:** entries removed from the end, or the whole file.
Record `verify --head` somewhere the agent cannot write to close that.
