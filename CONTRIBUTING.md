# Contributing

Thanks for helping. Flight Recorder is part of the Arboria Foundation's
tools; the foundation-wide standards for commits, pull requests and review are
on its contributing page. This file covers what is specific here.

## Getting set up

```console
make setup     # Python 3.11+: .venv with the dev tools
make check     # ruff, mypy --strict, pytest: what a PR must pass
make fmt       # format and auto-fix
```

## The rules that are not negotiable

- **The relay never changes a message.** Any change to `proxy.py` keeps
  `test_the_relay_is_byte_for_byte` passing.
- **The chain format is a contract.** Logs written today must verify with every
  later version. Changing what is hashed needs a new format version and a
  migration story, not an edit.
- **Redaction runs before every write.** New fields that carry agent content go
  through `redact.keep`.
- **Every bug fix comes with a test that fails without it.**
- **The core stays standard-library only.** `chain`, `redact`, `proxy` and
  `reader` take no third-party imports; importers may.

## Adding an importer

Add `src/flight_recorder/importers/<platform>.py`:

- Keep everything the platform's API defines (endpoints, field names) in one
  client class, so a changed API is a one-file fix.
- Make it idempotent: record what has been imported, mark it only after the entry
  is written, and keep that state out of the log directory.
- Treat every id and string from the API as untrusted: `chain.safe_name` for file
  names, and percent-encoding for URL paths.
- Record when each thing happened as `timestamp`; the recorder adds when it was
  written.
- Test against a mocked API (`httpx.MockTransport`), including a hostile id and a
  rejected key, and give the CLI a `--dry-run`.

## Things that will be declined

- **Acting on what is recorded.** Alerting, nudging or stopping agents belongs in
  a supervisor that reads the log, not in the recorder. A recorder that acts
  becomes something agents can steer.
- **Rewriting or filtering traffic.** That is a policy engine. It would make the
  record differ from what happened.
- **Sending the log anywhere by default.** No telemetry, and no uploads unless
  explicitly configured.
- **Sampling or dropping entries for volume.** A record with gaps is not a
  record. Cap payload size instead.

## Style

Match the code around you. Comments say why, not what. Names are spelled out.
