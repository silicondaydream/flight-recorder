# Setting up Devin

Watchdog reads Devin sessions in up to three ways:

| | What it gives Watchdog | Set up |
|---|---|---|
| **1. Devin's API** (required) | Every session in the organization: messages, status, credit use, PRs, tags | A service-user key (below) |
| **2. Devin hooks** (recommended) | Every tool call Devin makes, with its own shell and editor: commands, outputs, outcomes, edits, as they happen | Watchdog's Devin plugin: [devin-hooks.md](devin-hooks.md) |
| **3. The `watchdog-tools` MCP server** (optional) | Commands Devin chooses to run through it, with exit codes | Below |

With only (1), Watchdog still works, but it reads commands and errors out of
what Devin says, and marks those findings "from messages". (2) makes them exact
without asking anything of the agent. (3) predates (2) and is kept for clients
without hooks, and for wrapping other MCP servers with `watchdog proxy`.

## 1. A service user

1. Devin → **Settings → Devin API → Service users → Provision service user**.
2. Name it `Watchdog`. Choose the **Admin** role. A Member can manage sessions,
   but Devin's docs do not say whether a Member key sees sessions other people
   started; Admin does.
3. Copy the key (`cog_…`). It is shown once. It goes in `DEVIN_API_KEY` on the
   machine that runs Watchdog, never in a flag or a file in the repository.
4. Optional: the organization id is at the top of the same page (`DEVIN_ORG_ID`).
   Without it, Watchdog asks the API which organization the key belongs to.

Check it:

```bash
DEVIN_API_KEY=cog_... .venv/bin/watchdog import devin --dry-run
```

It prints the sessions and messages it would record, and writes nothing.

## 2. Devin hooks

See [devin-hooks.md](devin-hooks.md): what they are, how Watchdog's plugin
uses them, setup, and a probe to check them in your organization first.

## 3. The MCP server (optional)

Print everything to paste, with your public URL filled in:

```bash
.venv/bin/watchdog mcp-setup https://<your public url> --repo <git URL of the Watchdog repo>
```

It gives you three things:

1. **Install Watchdog on the session machine.** In Devin's environment or
   blueprint setup, add:
   ```bash
   pip install 'git+<git URL of the Watchdog repo>'
   ```
   If the repository is private, Devin's machine needs read access to it (the
   same Bitbucket/GitHub connection Devin uses for repositories).
2. **A custom MCP server.** Devin → **Customize → MCPs → add a custom server →
   STDIO**:
   - Command: `watchdog`
   - Arguments: `proxy --name watchdog-tools -- watchdog tools`
   - Environment variables:
     - `WATCHDOG_INGEST_URL` = `https://<your public url>/api/ingest`
     - `WATCHDOG_INGEST_TOKEN` = the same value as on the Watchdog machine.
       Prefer storing it as a Devin secret and referencing it; for a one-off
       demo, pasting the value works.
3. **A Knowledge note**, so Devin uses the tools:
   ```text
   Watchdog tools (MCP server "watchdog-tools"):
   - At the start of every session, before anything else, call `link_session` with this
     session's link (https://app.devin.ai/sessions/...).
   - Run every test, build, install, lint and other shell command with `run_command`.
   - Make file changes with `edit_file` or `write_file`.
   These calls are recorded so that a supervisor can tell when a session is stuck.
   ```

### Why `link_session`?

The MCP server runs on the session machine, and Devin does not tell an MCP
server which session it is serving. So Devin says so itself, once: the call is
recorded like any other, and when Watchdog reads it, it folds that MCP log into
the Devin session. Until then, the MCP log is a separate session on the
dashboard that only ever gets alerts.

### Check it

Start a session and ask Devin to "call link_session, then run `echo hello`
with run_command". Within one poll the session's card shows **Exact (MCP)**,
and its **Activity** tab shows the `tool.call` and `tool.result` entries.

If not:

- Look for MCP errors in the session. Is `watchdog` on the machine's PATH after
  the install step?
- Is `WATCHDOG_INGEST_URL` reachable from Devin's machine? A quick tunnel's URL
  changes every time it starts.
- Does the ingest token match on both sides?

## What Watchdog does to a session

All through the v3 organization API, with the service user's key:

| Step | Call |
|---|---|
| Nudge, or *Send message* from the dashboard | `POST /v3/organizations/{org}/sessions/{id}/messages`, text starting `[Watchdog]` |
| Sleep | `POST …/sessions/{id}/archive` ("archive session and put it to sleep if currently running"), then tag `watchdog:slept` |
| Terminate | `DELETE …/sessions/{id}`, only after a person approves (or presses *Terminate* in the dashboard) |

Watchdog never messages a suspended session, because Devin wakes a session to
deliver a message. To keep a session out of Watchdog's hands, give it the tag
`watchdog:off` (the tag is a setting), or press **Mute** on its detail page.

## The API it reads

Written against Devin's published v3 OpenAPI spec (`docs.devin.ai/v3-openapi.json`).

| Call | For |
|---|---|
| `GET /v3/self` | the key's organization, when `DEVIN_ORG_ID` is not set |
| `GET /v3/organizations/{org}/sessions?first&after&updated_after&tags` | sessions changed since the last poll, cursor-paged |
| `GET /v3/organizations/{org}/sessions/{id}/messages?first&after` | new messages, from a per-session cursor |

Every endpoint and field name lives in `DevinClient` (`src/agent_watchdog/devin.py`)
and `DevinActions` (`actions.py`), so a change in Devin's API is a one-file fix.
