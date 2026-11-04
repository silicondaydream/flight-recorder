# Security Policy

## Reporting a vulnerability

Report security issues privately to the repository's maintainers, not in a
public issue or pull request.
Expect an acknowledgement within a few days and a fix or mitigation within 30
days for confirmed issues.

## The assumption

**The agent being recorded is not trusted, and neither is anything it sends or
receives.** Tool arguments, results and anything else the agent sends or receives can
all be shaped by whoever steers the agent. Watchdog's job is to keep an
accurate record of that traffic without being steered by it, to act on it only
within the guardrails people set, and to leak nothing that passes through it.

## In scope

| Threat | Control | Test |
|---|---|---|
| Editing a recorded entry after the fact | SHA-256 hash chain over canonical JSON; `verify` names the first bad line | `tests/test_chain.py` |
| Editing an entry and re-hashing it | The next entry's link no longer matches | `tests/test_chain.py` |
| Removing or reordering entries | Sequence numbers and links | `tests/test_chain.py` |
| Two different entries hashing alike | One canonical serialisation is hashed, not concatenated fields | `tests/test_chain.py` |
| A tool call executed without being recorded | Calls are written before they are forwarded; `--strict` refuses to forward one that could not be written | `tests/test_proxy.py` |
| A supervisor acting on a forged entry | `reader.follow` and `tail` yield only verified entries and stop at a break | `tests/test_reader.py` |
| Secrets in the log | Pattern and field-name redaction before any write, then the size cap | `tests/test_redact.py`, `tests/test_proxy.py` |
| Other local users reading the log | Files created `0600` | `tests/test_chain.py` |
| Altering what the agent and server see | The relay forwards every line unchanged, byte for byte | `tests/test_proxy.py` |
| A hostile session id or server name writing outside the log directory | Sanitised before use in a file name | `tests/test_chain.py`, `tests/test_devin.py` |
| A hostile Devin session id reaching another API path | Percent-encoded as one URL path segment | `tests/test_devin.py` |
| The Devin API key leaking | Read from the environment only, never a flag | `tests/test_devin.py` |
| Any secret shown in the dashboard | Secrets are environment variables, never settings; the panel shows only "set" or "not set" | `tests/test_settings.py` |
| Another site changing settings or acting through the dashboard | Loopback by default; changes need JSON (a CORS preflight it never answers) and `WATCHDOG_APPROVAL_TOKEN` when set | `tests/test_watchdog.py`, `tests/test_settings.py` |
| Forged or replayed approval links | HMAC-SHA256 over decision, session and expiry; a GET only shows a confirmation page | `tests/test_mcp.py` |
| Secrets in hook events | The hook script redacts with Watchdog's own patterns (a test keeps them identical) before spooling or sending; Watchdog redacts again before writing | `tests/test_hooks.py` |
| A Watchdog outage stalling Devin | The hook script never blocks a tool, always exits 0, times out after 2 s and backs off for 20 s, spooling events locally | `tests/test_hooks.py` |
| Forged hook events | `/api/hook` needs the bearer token | `tests/test_hooks.py` |
| Forged MCP log entries over ingest | A bearer token, and each entry must be the next link of its chain | `tests/test_mcp.py` |
| A session's text steering the AI judge | Given as data in `<session>`; the judge can only hold Watchdog back | `tests/test_watchdog.py` |
| A huge payload exhausting disk | Payloads over `--max-payload` are kept as a preview plus size and hash | `tests/test_redact.py`, `tests/test_proxy.py` |

## Out of scope

- **Deleting the end of a log, or the whole file.** A shorter chain is a valid
  chain. Anchor `verify --head` somewhere the agent cannot write: another host,
  a ticket, a commit.
- **An attacker with the recorder's own privileges.** They can write a new,
  consistent chain from scratch. The chain makes tampering *evident*; it does not
  make it impossible. Write-once storage or an external timestamp is the
  stronger control.
- **Secrets in formats the patterns don't know.** Redaction is best effort. Add
  `--redact` patterns for your own token formats.
- **Limiting what the agent can do.** Watchdog records and escalates; it is not
  a sandbox or a policy engine, and the proxy never blocks or rewrites a call.
- **Who clicked an approval link.** A signed link proves it came from Watchdog,
  not who used it. Post approval cards only to a channel of people allowed to
  decide.

## Design commitments

1. **The relay never changes a message.** What the agent and the server see is
   exactly what they would have seen without the recorder.
2. **Nothing leaves the host** except what you configure: requests to Devin's
   API, cards to your Teams webhook, the AI judge's calls to OpenAI (off by
   default), and, on Devin's machines, shipped logs to your Watchdog.
3. **No secret is written on purpose.** Redaction runs before every write.
4. **A broken chain is never extended.** The recorder refuses to append to a log
   that does not verify.

## Supported versions

Until 1.0, only the latest release receives security fixes.
