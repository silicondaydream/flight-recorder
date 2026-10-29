# REMAINING TASKS

Updated 2026-10-29. Everything in section 1 was run.

## 1. Where things stand — verified

```console
make check     # ruff, mypy --strict, all tests: passing
```

Also checked by hand:

- A session recorded through the proxy (with `tests/fake_server.py`) verifies, and
  `tail` shows it.
- `lincoln ingest` on the log directory needs no format pack. LQL over
  `flight.tool`, `event.outcome` and `event.duration` works.

**Not checked yet: a real MCP server through the proxy.** Running a downloaded
npm server was blocked in the build sandbox. Try
`@modelcontextprotocol/server-everything` in a real client.

**Not checked yet: the Devin API.** Devin's docs and API were unreachable from
the sandbox. The importer follows the v1 API as known: `GET /v1/sessions` and
`GET /v1/session/{id}`, with `messages[]` holding `event_id`, `type`, `message`
and `timestamp`. Run `import devin --dry-run` with a real key, and fix
`DevinClient` / `_message_key` if the fields differ.

## Branches

- `main` — the general recorder: proxy, chain, redaction, verify and tail. No
  vendor code and no runtime dependencies.
- `devin` — `main` plus the Devin API importer, for the Watchdog work. Merge
  `main` into it to pick up core changes; never merge `devin` into `main`.

## 2. Next

0. **(devin) Verify the importer against the live API**, as above. Then check
   whether the API exposes the commands a session ran, not only its messages. If
   it does, record them as `shell.command` entries: Watchdog's "repeated failing
   commands" and "edit thrash" rules need them.
1. **Streamable HTTP MCP proxy.** Only stdio servers today. Many hosted servers
   are HTTP.
2. **Anchoring.** `verify --head` prints the head hash; nothing stores it. A
   `flight-recorder anchor` that appends heads to a separate file, or posts them
   somewhere the agent cannot write, would close the deleted-tail gap.
3. **Lincoln's audit chain has the ambiguity fixed here.** It concatenates fields
   with no separators. That is a Lincoln issue, worth filing there.
4. **Site docs.** Add `/docs/flight-recorder/` to arboria-foundation.

## Watchdog (the work task this branch serves)

Watchdog is a supervisor that notices stuck or drifting Devin sessions. It is a
separate program that reads `reader.follow()`. Flight Recorder records; Watchdog
judges and acts (alerts, nudges, sleep, terminate) and can record its own
actions as entries. `docs/devin.md` maps its rules to recorded fields.

## Out of scope — do not add

Acting on recorded content, rewriting or filtering traffic, sending logs anywhere
by default, sampling or dropping entries, and vendor-specific code on `main`.
Reasons in [CONTRIBUTING.md](CONTRIBUTING.md#things-that-will-be-declined).
