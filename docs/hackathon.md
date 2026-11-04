# Hackathon day: the runbook

Everything to do before and during the demo, in order. Budget 45 minutes for
the setup the first time; on the day itself, about 10.

## What you need

| Thing | Where it comes from |
|---|---|
| A laptop with Python 3.11+ and this repository | `git clone <watchdog repo>`, then `make setup` |
| A Devin service-user key (`cog_…`) | Devin → **Settings → Devin API → Service users → Provision service user**, role **Admin** (see [devin.md](devin.md)) |
| The demo target repository, connected to Devin | Split out `demo-target/` into its own repository (see [its README](../demo-target/README.md)) |
| A Teams channel with a Workflows webhook | Teams → channel → **Workflows** → "Post to a channel when a webhook request is received" (see [teams.md](teams.md)) |
| A public HTTPS URL for this laptop | For Devin's machines and the Teams approval buttons: `cloudflared tunnel --url http://localhost:8766` |
| Optional: an OpenAI API key | For the AI judge (a ChatGPT Enterprise or OpenAI platform project key) |

## The day before

1. **Install and test locally.**
   ```bash
   make setup && make check && .venv/bin/watchdog demo
   ```
   Open http://127.0.0.1:8765 and watch the scripted sessions climb the ladder
   for two minutes. This is also your fallback if anything fails on the day.
2. **Split out the demo target.** Copy `demo-target/` into a new repository,
   push it, and connect it to Devin (Devin → Settings → Repositories).
3. **Set up Devin** as in [devin.md](devin.md):
   - the service user;
   - **run the hook probe** ([devin-hooks.md](devin-hooks.md#check-it-first-the-probe))
     and check the answers;
   - install Watchdog's Devin plugin, which carries the hooks: either for the
     organization, or only for the demo target (`.devin/config.json`);
   - optional: the `watchdog-tools` MCP server and its Knowledge note.
4. **Create the Teams webhook** and keep its URL.
5. **Make two secrets** and keep them somewhere safe:
   ```bash
   openssl rand -hex 32   # WATCHDOG_APPROVAL_TOKEN: signs approval links, protects the dashboard
   ```
   ```bash
   openssl rand -hex 32   # WATCHDOG_INGEST_TOKEN: Devin's machines send this; also a Devin secret
   ```
6. **Rehearse once, end to end**, with the steps below, in dry run.

## On the day

1. **Start the tunnel** (one terminal):
   ```bash
   cloudflared tunnel --url http://localhost:8766
   ```
   Copy the `https://….trycloudflare.com` URL. Quick tunnels get a new URL
   every time: if it changed since you set up Devin, update `WATCHDOG_HOOK_URL`
   in the plugin's manifest entry (and `WATCHDOG_INGEST_URL` in the MCP config,
   if you use it), or use a named tunnel with a fixed hostname. Running sessions
   keep the old URL: start a new one.
2. **Set the environment** (second terminal):
   ```bash
   export DEVIN_API_KEY=cog_... TEAMS_WEBHOOK_URL='https://...' WATCHDOG_PUBLIC_URL=https://<tunnel> WATCHDOG_APPROVAL_TOKEN=<secret 1> WATCHDOG_INGEST_TOKEN=<secret 2>
   ```
   Optionally `export OPENAI_API_KEY=sk-...` for the AI judge.
3. **Start Watchdog:**
   ```bash
   .venv/bin/watchdog run --public 127.0.0.1:8766 --since-hours 1
   ```
   Open http://127.0.0.1:8765.
4. **Check the connections.** In **Settings → Connections** every line should
   say *set* or *on*. Then press **Send test card** and confirm it arrives in
   Teams. If you use the judge, press **Test AI judge**.
5. **Tune for a live demo** in **Settings**:
   - **Escalation → Wait between steps**: 60 s (the default, 600 s, is for real
     use).
   - **General → Check Devin every**: 10 s.
   - **Teams → At most one card per session every**: 0 s, so the audience sees
     every card.
   - **Live log → Show**: *Everything*, so the audience sees the agent's tool
     calls as they happen.
   - **General → Dry run**: off, when you are ready to act for real. The badge in
     the header turns from DRY RUN to LIVE.
6. **Start the Devin session** on the demo target with the prompt from
   [demo-target/README.md](../demo-target/README.md).
7. **Within a minute** the session appears on the dashboard. The badge turns to
   **Exact (hooks)** after Devin's first tool call (or **Exact (MCP)** once it
   calls `link_session`, if you use the MCP server).
8. Optional: **Settings → Escalation → Deliver nudges → Note via hooks**, so the
   nudge lands in Devin's context on its next tool call. The audience sees
   Devin react to it mid-task.

## The demo script (about 6 minutes)

1. *The problem* (30 s): agents get stuck and keep spending; nobody notices.
   Point at the **ACUs burning without progress** tile.
2. *What it sees* (1 min): click the session. Open **Activity**: every tool call
   Devin made, with its own shell and editor (through the hooks), and every
   result, as recorded. Click a row to show the raw entry and its hash-chain
   link. Then **Signals**: what the rules work from.
3. *It notices* (1 min): after three failing test runs the card says **Flagged**,
   *Repeated failing command*, and **Next: Will alert in …s**.
4. *It escalates* (2 min): the Teams card arrives (show the channel); then the
   nudge appears in the Devin session itself (open it with **Open in Devin**);
   then the session is put to sleep.
5. *People stay in charge* (1 min): open **Settings → Teams** to show how
   granular notifications are. Show a guardrail: Watchdog holds back while a
   session waits on a person. If you woke the session and it is still stuck,
   approve the terminate from the Teams card.
6. *The record* (30 s):
   ```bash
   .venv/bin/watchdog verify ~/.local/share/watchdog
   ```
   Every log, including the one shipped from Devin's machine, verifies.

## If something goes wrong

| Symptom | Fix |
|---|---|
| No sessions appear | **Settings → Connections**: is the Devin key set? Run `watchdog import devin --dry-run` to see what the API returns. An **Admin** service user sees every session; a Member may not. |
| The session shows **From messages**, never **Exact (hooks)** | The plugin did not load in that session (Customize → Plugins: is it indexed, not blocked?), the session started before the plugin was installed, or the VM can't reach `WATCHDOG_HOOK_URL` (a quick tunnel's URL changes on every start). An unlinked **hook session** card on its own means the events arrive but could not be matched to a Devin session: check the probe's `session_id`. |
| The session shows **From messages**, never **Exact (MCP)** | Devin did not call `link_session`, or the MCP server did not start. Check the Devin session for MCP errors, and that `WATCHDOG_INGEST_URL` is the current tunnel URL. |
| No Teams cards | Press **Send test card**. Check **Teams → Send Teams cards** and the step's trigger, and the rate limits. In dry run, **Send cards in dry run** must be on. |
| Approval buttons say *link not valid* | `WATCHDOG_PUBLIC_URL` must be the tunnel URL, and the link not expired (**Escalation → Approval links valid for**). |
| Escalation is too slow for the room | Lower **Wait between steps**, or a rule's threshold in **Rules**. Changes apply within seconds. |
| Anything else | Run `watchdog demo` instead: the same dashboard and ladder, with a scripted Devin. |

## After the demo

Stop Watchdog with Ctrl-C. Logs stay in `~/.local/share/watchdog`; settings in
`~/.local/state/watchdog/settings.json`. Rotate or delete the service-user key
and the two tokens if they were shown on screen.
