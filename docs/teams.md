# Microsoft Teams

Watchdog posts Adaptive Cards to a Teams channel through an incoming webhook.
You control which events produce a card, how often, and how much each says.
The approval card has buttons that approve or decline a terminate.

## Create the webhook

1. In Teams, open the channel, then **⋯ → Workflows**.
2. Choose the template **"Post to a channel when a webhook request is
   received"**, name it `Watchdog`, pick the team and channel, and create it.
3. Copy the URL it shows. That is `TEAMS_WEBHOOK_URL` on the machine that runs
   Watchdog. Treat it as a secret: anyone with it can post to the channel.
4. Start Watchdog and press **Settings → Send test card**.

Power Automate is not needed. The Workflows template is a Power Automate flow
under the hood, and Watchdog only needs its webhook URL.

## Choose what reaches Teams

Everything is in **Settings → Teams**, and every notice also appears on the
dashboard whether or not it became a card (the session's **Decisions & Teams**
tab shows what happened to each one).

**Send a card when…** (each a switch):

| Trigger | Default | |
|---|---|---|
| A problem is first logged | off | the first step of the ladder |
| The alert step | **on** | the ladder's alert |
| A session is nudged / put to sleep / terminated | **on** | |
| Approval is needed | **on** | the card with *Approve terminate* and *Keep running* |
| Someone approves or declines | **on** | |
| Someone acts from the dashboard | **on** | a message, sleep, terminate or mute sent by hand |
| A session recovers | off | |
| Watchdog holds back | off | a guardrail stopped a step (opt-out tag, waiting on a person, a step's ceiling) |
| The AI judge says not stuck | off | |
| A session starts / ends or finishes | off | sessions started or ended after Watchdog started |
| A session runs long | **on**, after 60 min | once per session |
| A session's spend passes a threshold | **on**, 20 ACUs | once per session |
| A session waits on a person | off, after 15 min | once per wait |
| Watchdog hits an error | **on** | a log that fails verification, an API call that failed |

**Delivery:**

| Setting | Default | |
|---|---|---|
| Card detail | Standard | *Compact*: title and one line. *Standard*: plus facts (session, status, rules, step, ACUs, runtime). *Detailed*: plus the evidence for each finding, and the agent's last messages if allowed |
| Include agent messages | off | lets *Detailed* cards quote the agent. Off keeps session content out of Teams |
| At most one card per session every | 300 s | per-session rate limit |
| At most, per hour | 30 | across all sessions |
| Batch into a digest every | 0 (off) | when set, non-urgent cards are combined into one digest card per interval |
| Send cards in dry run | on | rehearse the notifications too; cards say "dry run" |

Approval requests and errors are **urgent**: they skip the rate limits and the
digest. A card dropped by a rate limit is counted, and the next card says how
many were dropped.

## Approving from Teams

When a stuck session reaches the last step, Watchdog posts an approval card with
**Approve terminate** and **Keep running** buttons. Cards from a webhook can
only open links, so each button is a link to Watchdog's public listener,
signed with `WATCHDOG_APPROVAL_TOKEN` and valid for an hour (a setting).

Opening the link shows a confirmation page. Only that page's button acts,
because Teams and link scanners fetch links before anyone clicks them. *Keep
running* stops Watchdog from asking again until the session recovers.

The link proves it came from Watchdog, not who clicked it: the choice is
recorded as "approval link (IP address)". Post approval cards only to a channel
of people allowed to make that call. For approvals tied to a named person, a
Power Automate flow with "Post adaptive card and wait for a response" that runs
`watchdog approve <session>` is the next step; it is not built in.

For the buttons to work, `WATCHDOG_PUBLIC_URL` must be an address the clicker's
browser can reach, and Watchdog must run with `--public`.
