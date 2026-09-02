# See Coverage on your own Mac

No accounts, no cost, no coding. This runs Coverage privately on your computer
so you can click around before deciding to put it online.

## Start it

1. In **Finder**, open the **Coverage** folder.
2. Double-click **`Start Coverage.command`**.
   - A black window opens and shows "Starting Coverage…". That window is normal
     — it's Coverage running. (If macOS says it can't open the file because it's
     from an unidentified developer: **right-click** the file → **Open** →
     **Open**. You only do this the first time.)
   - After a few seconds, a **browser tab opens** to your Opportunities feed.
3. **Keep the black window open** while you use Coverage. To stop, just close it.

The first start is the slowest (it wakes the database and sets up a demo
student). Later starts are quick.

## What you're looking at

**The Opportunities feed** (opens first, no login) — the free, public listing.
Real internship openings pulled from company career sites, ranked by deadline
then freshness, with honest labels: "verified today", "may be stale", "no
deadline posted". This is the part that's never paywalled. (There was once a
separate heat-mapped cycle *calendar* page; it was retired in favour of this
feed. Per-firm cycle timelines still live on each firm's detail page.)

**The CRM** — the part that makes Coverage different. To see it, log in:

- Go to **http://127.0.0.1:8000/accounts/login/** (or click "Sign in")
- **Your own account:** `you@example.com`, with your 69 target firms
  already imported from `recruiting-radar/targets.yaml` and tiered by priority.
- **The demo student** (sample contacts and touches, for showing other people):
  `demo@coverage.local` / `demo1234`

Then visit **http://127.0.0.1:8000/app/**. On the demo account, which is
pre-filled so you can see the loop working:

- **This Week** — the priority list: who to contact, why, and a ready-to-send
  email with the tracking address already filled in. It's telling this student
  to send a thank-you, do a first outreach, and re-ping a contact before a
  deadline.
- **Contacts** — six people at different "warmth" levels (from a cold first
  email up to an advocate who offered a referral).
- Click a contact (e.g. **Maya Chen**) — you'll see the warmth meter, the
  **fit score** with its reasoning ("1 chat; replies within a day; alum…"),
  and a one-click compose that BCCs the tracking address automatically.

**The admin view** (optional, power-user) —
**http://127.0.0.1:8000/admin/**, log in with **admin@coverage.local** /
**coverage-local**. This is the raw data behind everything; browse firms,
contacts, opportunities.

## Stopping and restarting

- **Stop:** close the black window.
- **Restart:** double-click `Start Coverage.command` again.

## The background jobs (optional, and the reason mail goes stale without them)

Everything above renders without any of this. What the background jobs buy is
the app being *current*: mail turning into touches, queued work actually
getting claimed, listings not going a week old. On Render those are separate
services in `render.yaml`; on a Mac they are launchd agents. Install all four:

```
./scripts/launchd/install.sh
```

That renders the templates in `scripts/launchd/` (they hold `__REPO__` where an
absolute path goes, because a plist cannot expand a variable) into
`~/Library/LaunchAgents/` and loads them. It is idempotent — it boots each job
out before loading it — so re-running is how you apply an edit. Check with
`launchctl list | grep coverage`; remove with `install.sh --uninstall`.

| launchd agent | stands in for | shape |
|---|---|---|
| `com.coverage.gmailpoll` | `coverage-gmail-live` (**worker**) | runs forever, `KeepAlive` |
| `com.coverage.gmailbackfill` | `coverage-gmail-backfill` cron | one tick every 5 min |
| `com.coverage.autopilot` | `coverage-autopilot` cron | one tick every 5 min |
| `com.coverage.refresh` | `coverage-scrape` cron | daily 08:30, via `scripts/refresh.sh` |

**`gmailpoll` is the one that is easy to miss, and it was missed.** It is the
only worker in the set: the other three are crons that launchd fires on a
timer, while this one is a process that has to stay alive. `gmail_poll` is what
turns a new Gmail message into a touch, and for two days nothing ran it. Gmail
showed connected, the watch was registered, and 137 unread messages sat behind
the stored cursor with `last_notification_at` still null since the day the
mailbox was linked. Nothing was broken; a process simply did not exist. That is
what this directory is for.

The three plists that are crons deliberately reuse production's own numbers
(`*/5`), so local latency is the latency a deployed student sees. `refresh` is
the exception at once a day against Render's every six hours: it is the only
job with a cost outside this machine, since it hits every firm's careers site.

Three of `render.yaml`'s services have **no** local stand-in and are not
missing by accident: `coverage-push-alerts`, `coverage-weekly-digest` and
`coverage-pro-trial-expire` all send something outward, which wants a real
sending domain rather than a laptop. `coverage-gmail-watch-renew` has none
either, and that one is genuinely unnecessary here: it keeps a *push*
registration alive, and the local set polls.

Everything here is only on your Mac — no one else can see it until you deploy
(see `docs/deploy.md`).
