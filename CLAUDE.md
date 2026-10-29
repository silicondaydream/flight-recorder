# REMAINING TASKS

Updated 2026-10-28. Everything in section 1 was run.

## 1. Where things stand — verified

```console
make check     # ruff, mypy --strict, 63 tests: passing
```

Also checked by hand:

- A session recorded through the proxy (with `tests/fake_server.py`) verifies, and
  `tail` shows it.
- `lincoln ingest` on the log directory needs no format pack. LQL over
  `flight.tool`, `event.outcome` and `event.duration` works, and so does a Devin
  `session.status` query.
- Importer state used to sit in the log directory and was ingested as junk. It
  now lives in `~/.local/state/flight-recorder/`.

**Not checked yet:**

- **A real MCP server through the proxy.** Running a downloaded npm server was
  blocked in the build sandbox. Try it with `@modelcontextprotocol/server-everything`
  in a real client.
- **The Devin API.** Devin's docs and API were unreachable from the sandbox. The
  importer follows the v1 API as known: `GET /v1/sessions` and
  `GET /v1/session/{id}`, with `messages[]` holding `event_id`, `type`,
  `message` and `timestamp`. Run `import devin --dry-run` with a real key, and fix
  `DevinClient` / `_message_key` if the fields differ.

There is no GitHub repository yet. This is a local git repo. When one exists, push
`main` and open PRs from here on.

## 2. Next

1. **Verify the Devin importer against the live API** (above). Check whether the
   API exposes the commands a session ran, not only its messages. If it does,
   record them as `shell.command` entries: Watchdog's "repeated failing
   commands" and "edit thrash" rules need them.
2. **Streamable HTTP MCP proxy.** Only stdio servers today. Many hosted servers
   are HTTP.
3. **Anchoring.** `verify --head` prints the head hash; nothing stores it. A
   `flight-recorder anchor` that appends heads to a separate file, or posts them
   somewhere the agent cannot write, would close the deleted-tail gap.
4. **Lincoln's audit chain has the ambiguity fixed here.** It concatenates fields
   with no separators. That is a Lincoln issue, worth filing there.
5. **Site docs.** Add `/docs/flight-recorder/` to arboria-foundation once this
   has a repository.

## Watchdog (the work task this enables)

Watchdog is a supervisor that notices stuck or drifting Devin sessions. It is a
separate program that reads `reader.follow()`. Flight Recorder records; Watchdog
judges and acts (alerts, nudges, sleep, terminate) and can record its own
actions as entries. `docs/devin.md` maps its rules to recorded fields.

## Out of scope — do not add

Acting on recorded content, rewriting or filtering traffic, sending logs anywhere
by default, sampling or dropping entries. Reasons in
[CONTRIBUTING.md](CONTRIBUTING.md#things-that-will-be-declined).
