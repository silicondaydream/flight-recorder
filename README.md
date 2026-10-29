# Flight Recorder

A tamper-evident record of what an AI agent actually did. Put it between an
agent and its MCP servers, and every tool call and result goes into a
hash-chained log that shows if anyone edits it afterwards.

```console
$ flight-recorder tail ~/.local/share/flight-recorder
2026-10-28T23:42:58.084Z  #1  session.start
2026-10-28T23:42:58.117Z  #2  session.initialize
2026-10-28T23:42:58.119Z  #3  tool.call  echo
2026-10-28T23:42:58.119Z  #4  tool.call  fail
2026-10-28T23:42:58.147Z  #8  tool.result  echo  success  29ms
2026-10-28T23:42:58.148Z  #9  tool.result  fail  failure  29ms
2026-10-28T23:42:58.153Z  #12  session.end  success

$ flight-recorder verify ~/.local/share/flight-recorder
ok      …/20261028T234258Z-demo-8a3978ab.ndjson  12 entries  head 9fb2a6b7…
```

Change one byte of a recorded entry and `verify` names the line, and every
entry after it fails too.

## Why

Agents act on their own: they run commands, call APIs, edit files. Afterwards,
"what did it actually do?" is hard to answer. The transcript is the agent's own
account, a platform's dashboard can't be queried alongside your other logs, and
nothing shows whether either was edited. Flight Recorder keeps its own record,
outside the agent:

- **Generic.** Any MCP client that starts stdio servers works: Claude Code,
  Cursor, agent platforms that launch custom servers by command, your own. No
  vendor is assumed.
- **Tamper-evident.** Each entry carries the hash of the one before it. Edits,
  removals and reordering are caught.
- **Safe to keep.** Secrets are redacted before anything is written, large
  payloads are capped visibly, and files are `0600`.
- **Queryable.** Entries are ECS-shaped JSON lines. [Lincoln](https://github.com/silicondaydream/lincoln)
  ingests them with no configuration, and so will anything else that reads
  NDJSON.
- **A feed for supervisors.** `tail --follow` and `reader.follow()` yield only
  verified entries, so a supervisor never acts on a forged one.

## Install

```console
git clone <this repository> && cd flight-recorder
make setup            # Python 3.11+: venv + pip install -e ".[dev]"
make check            # ruff, mypy --strict, tests
```

The core uses only the Python standard library. This branch adds `httpx`, for the
Devin importer.

## Record an MCP server

Put `flight-recorder proxy --` in front of the server's command in your MCP
client's configuration. For example, in Claude Code's or Cursor's `mcp.json`:

```json
{
  "mcpServers": {
    "github": {
      "command": "flight-recorder",
      "args": ["proxy", "--name", "github", "--", "npx", "-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "…" }
    }
  }
}
```

Any client that launches a server from a command and arguments takes the same
form, including hosted agent platforms that configure custom MCP servers by
command.

The proxy relays the JSON-RPC stream unchanged and writes one log per session to
`$FLIGHT_RECORDER_DIR` (default `~/.local/share/flight-recorder`). Every
`tools/call` is written to the log *before* it is forwarded. With `--strict`, a
call that cannot be written is never forwarded and the session stops, so no tool
runs without its record.

| Option | |
|---|---|
| `--name` | Label for the server in the log |
| `--log-dir` | Where logs go |
| `--max-payload BYTES` | Larger arguments and results are kept as a preview plus size and SHA-256 (default 64 KB) |
| `--redact REGEX` | Redact more than the built-in secret patterns (repeatable) |
| `--strict` | Stop rather than forward a call that could not be recorded |

## Import Devin sessions

> This is the `devin` branch: `main` plus a Devin importer. `main` stays
> vendor-neutral; merge `main` into this branch to pick up core changes.

Devin runs its own shell and editor, outside MCP, so its sessions come in
through the API:

```console
export DEVIN_API_KEY=…
flight-recorder import devin --dry-run          # what would be recorded
flight-recorder import devin                    # one log per session
flight-recorder import devin --watch --interval 60 --tag team-a
```

Messages, status changes and session metadata are recorded, and repeated runs add
only what is new. See [docs/devin.md](docs/devin.md), including how this feeds an
agent supervisor such as Watchdog.

## Read, verify, query

```console
flight-recorder verify <file|dir>        # exit 1 if any log was altered
flight-recorder verify --head <file>     # the head hash, to anchor elsewhere
flight-recorder tail -f --json <dir>     # verified entries, live
lincoln ingest <dir>                     # then query with LQL or SQL
```

```console
$ lincoln query 'event.action = "tool.result" | stats count() as calls by flight.tool, event.outcome | sort -calls'
flight.tool  event.outcome  calls
───────────  ─────────────  ─────
echo         success        2
fail         failure        2
```

More queries are in [docs/lincoln.md](docs/lincoln.md). The entry format is in
[docs/format.md](docs/format.md).

## What it does not do

- **It is not a sandbox.** It records what the agent did. Use permissions and
  sandboxing to limit what it can do.
- **A deleted tail is not detected on its own.** A shorter chain is still a
  valid chain. Anchor `verify --head` somewhere the agent cannot write.
- **Redaction is pattern-based.** It catches common token formats and named
  fields, not every secret.
- **Only stdio MCP servers, for now.** Streamable HTTP servers are next.
- **The Devin importer was written against the v1 API without access to its
  live docs.** Check it with `--dry-run` on your organisation first.

## Status

**v0.1, early and working.** `make check` passes (ruff, `mypy --strict`, 67
tests). The tests run real child processes and cover tampering, redaction
and relay fidelity, and the Devin importer against a mocked API.

## License

MIT.
