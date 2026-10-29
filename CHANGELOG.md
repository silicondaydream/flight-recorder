# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0]

### Added

- **Hash-chained log.** One NDJSON file per session, ECS-shaped. Each entry is
  linked by SHA-256 over its canonical JSON, flushed and fsynced on every write,
  and created `0600`. A recorder refuses to extend a chain that does not verify.
- **`flight-recorder proxy`.** A stdio MCP proxy that relays traffic unchanged
  and records `tools/call` requests (before forwarding them), their results and
  durations, and both sides' `initialize`. `--strict` never forwards a call it
  could not record.
- **Redaction and payload caps.** Built-in secret patterns, sensitive field
  names and `--redact`, applied before every write. Large payloads are kept as a
  preview plus size and SHA-256.
- **`flight-recorder verify` and `tail`**, and `reader.follow()`, a live feed of
  verified entries from a file or a directory.
- **`flight-recorder import devin`** (this branch). Devin sessions' messages,
  status changes and metadata, through the API. Idempotent, with `--watch`,
  `--tag` and `--dry-run`. State is kept outside the log directory.
- Docs: the log format, querying with Lincoln, and Devin and supervisors.
