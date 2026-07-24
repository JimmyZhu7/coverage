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
   - After a few seconds, a **browser tab opens** to your deadline calendar.
3. **Keep the black window open** while you use Coverage. To stop, just close it.

The first start is the slowest (it wakes the database and sets up a demo
student). Later starts are quick.

## What you're looking at

**The calendar** (opens first, no login) — the free, public deadline tracker.
Real internship openings pulled from company career sites, with honest labels:
"verified today", "may be stale", "no deadline posted". This is the part that's
never paywalled.

**The CRM** — the part that makes Coverage different. To see it, log in:

- Go to **http://127.0.0.1:8000/accounts/login/** (or click "Sign in")
- Email: **demo@coverage.local**
- Password: **demo1234**

Then visit **http://127.0.0.1:8000/app/** — this is a demo student's account,
pre-filled so you can see it working:

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

Everything here is only on your Mac — no one else can see it until you deploy
(see `docs/deploy.md`).
