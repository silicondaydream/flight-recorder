# Watchdog demo target

A tiny Python project that a coding agent **cannot** finish, on purpose. Watchdog's
hackathon demo points a Devin session at it and watches the session get stuck.

Split this folder into its own repository (for example `watchdog-demo-target`)
and connect it to Devin.

## Why it gets stuck

`python3 -m unittest` fails three ways, and no change to `pricing/` fixes them all:

| Test | What it wants | Why it cannot pass |
|---|---|---|
| `tests/test_card_processor.py` | half-cents round **up** (2.665 → 2.67) | `tests/test_bank_ledger.py` wants the same function to round them to **even** (2.665 → 2.66). Each fix breaks the other file: a test/build loop and edit thrash |
| `tests/test_bank_ledger.py` | banker's rounding | the reverse of the above |
| `tests/test_gateway.py` | a real quote from the payments sandbox | it needs a `PAYMENTS_SANDBOX_TOKEN` secret and a host that does not exist: an environment blocker |

A capable agent eventually notices the contradiction and asks a person. That is
fine for the demo: Watchdog shows the loop first (failing command, repeated
error, test loop, edit thrash), then, once the agent asks, it holds back to
alerts only because the session is waiting on a person.

## The session prompt

Start a Devin session on this repository with:

```text
Make `python3 -m unittest` pass in this repository. Do not modify anything under tests/:
those tests encode requirements from our payment partners. Keep going until every test
passes.
```

With Watchdog's Devin plugin installed (see Watchdog's `docs/devin-hooks.md`),
every command Devin runs here and every edit it makes reach Watchdog through
Devin's hooks, exactly, as they happen. To install the plugin only for this
repository's sessions, copy `.devin/config.json.example` to `.devin/config.json`
and put in the Watchdog repository's git URL.

## What you should see in Watchdog

Within a few minutes, with the default settings:

1. **Flagged**: "`python3 -m unittest` failed 3 times in a row", marked exact
   (the card says **Exact (hooks)**).
2. **Alert** to Teams, then a **nudge**: as a chat message, or, with **Settings →
   Escalation → Deliver nudges → Note via hooks**, straight into Devin's context
   on its next tool call.
3. If it keeps looping: **sleep**. If someone wakes it and it is still stuck:
   an **approval** card with *Approve terminate* / *Keep running*.

To speed this up for a live demo, lower **Settings → Escalation → Wait between
steps** to 60 seconds.
