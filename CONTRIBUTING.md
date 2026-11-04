# Contributing

## Getting set up

```console
make setup     # Python 3.11+: .venv with the dev tools
make check     # ruff, mypy --strict, pytest: what a change must pass
make fmt       # format and auto-fix
.venv/bin/watchdog demo    # the dashboard against a scripted fake, to see a change
```

## The rules that are not negotiable

- **The proxy never changes a message.** Any change to `proxy.py` keeps
  `test_the_relay_is_byte_for_byte` passing.
- **The log format is a contract.** Logs written today must verify with every
  later version. Changing what is hashed needs a new format version and a
  migration story, not an edit.
- **Redaction runs before every write.** New fields that carry agent content go
  through `redact.keep`.
- **The engine judges only what was recorded.** It reads verified entries from
  the log, never a platform's API directly.
- **People decide the irreversible step.** Terminate always waits for approval
  (or a person pressing *Terminate*).
- **Secrets are environment variables**, never settings, never logged.
- **What runs on an agent's machine stays standard-library only**: `proxy`,
  `tools`, `chain`, `reader`, `redact`.
- **Every bug fix comes with a test that fails without it.**

## Adding a setting

Add a `Field` to `FIELDS` in `settings.py` (key, kind, default, label, help,
limits). The settings panel and `docs/configuration.md` are generated from that
list. Read the value with `config.get(key)` where it is used, so a change applies
without a restart.

## Adding a rule

Rules are pure functions in `rules.py`: a session view and the time in, a
`Finding` (or nothing) out, with its evidence and the nudge it would send. Add
it to `RULES`, give it `rules.<name>.*` settings, and test it with a healthy
session too.

## Adding a platform

Everything a platform's API defines (endpoints, field names) lives in one
client class, so a changed API is a one-file fix. Importers are idempotent
(state kept outside the log directory, marked only after an entry is written),
treat every id from the API as untrusted, and are tested against a fake of the
API (`httpx.MockTransport`), including a hostile id and a rejected key.

## Style

Match the code around you. Comments say why, not what. Names are spelled out.
User-facing text is plain: say what happened and what to do.
