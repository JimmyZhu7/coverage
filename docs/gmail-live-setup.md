# Gmail Live setup — the steps only Jimmy can do

`capture/gmail_live.py` is the code side of docs/build-plan.md §5's "v2".
Everything in code is built and tested (`capture/tests/test_gmail_live.py`,
`pytest capture/` — 142 passed). What's left is Google Cloud Console work
that needs your own Google login — none of it can be done from a terminal
on your behalf. Do these once, then paste the results into `.env`.

## 1. Create the Google Cloud project (if you don't have one yet)

console.cloud.google.com → New Project. Free.

## 2. Enable two APIs

APIs & Services → Library → enable:
- **Gmail API**
- **Cloud Pub/Sub API**

## 3. Create a SEPARATE OAuth client — do not reuse the login one

APIs & Services → Credentials → Create Credentials → OAuth client ID →
**Web application**.

- Name it something you'll recognise later, e.g. "Coverage Gmail Live".
- Authorized redirect URI: `http://localhost:8000/capture/gmail/callback/`
  for local dev (add the real deployed URL's `/capture/gmail/callback/`
  once Coverage is hosted somewhere).
- Save the **Client ID** and **Client secret** it gives you.

This MUST be a different client than whatever you eventually use for Google
sign-in. `coverage_web/settings/base.py` has a hard rule about this (§3) —
mixing them means a Gmail verification problem could someday break the
ability to log in at all.

## 4. OAuth consent screen

APIs & Services → OAuth consent screen:
- User type: **External**.
- Add the `gmail.readonly` scope.
- Under "Test users", add your own Gmail address, and your friend's once
  he's connecting too. **This is what keeps you in the free, unreviewed
  tier — up to 100 test users, no Google review, no cost.** Only exceeding
  100 or removing the "Testing" publishing status triggers the paid
  verification process.

## 5. Pub/Sub topic + subscription

Pub/Sub → Topics → Create Topic. Name it anything, e.g. `gmail-live`.

Grant Gmail's own push service publish rights on it — this is the one
non-obvious step:
- Open the topic → Permissions → Add Principal.
- Principal: `gmail-api-push@system.gserviceaccount.com`
- Role: **Pub/Sub Publisher**

Then create a subscription on that topic:
- Pub/Sub → Subscriptions → Create Subscription.
- Delivery type: **Pull** (not push — see `gmail_live.py`'s docstring for
  why: pull means this works before Coverage is deployed anywhere public).

Note the full resource names, e.g.:
```
projects/your-project-id/topics/gmail-live
projects/your-project-id/subscriptions/gmail-live-sub
```

## 6. Generate the token-encryption key

One-time, from the `coverage_web` directory:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 7. Put it all in `.env`

```
GMAIL_LIVE_CLIENT_ID=<from step 3>
GMAIL_LIVE_CLIENT_SECRET=<from step 3>
GMAIL_LIVE_PUBSUB_TOPIC=projects/your-project-id/topics/gmail-live
GMAIL_LIVE_PUBSUB_SUBSCRIPTION=projects/your-project-id/subscriptions/gmail-live-sub
GMAIL_LIVE_TOKEN_KEY=<from step 6>
```

`gmail_live.is_configured()` gates everything on all four being set — until
then the Settings page shows nothing new and the two management commands
below just no-op.

## 8. Connect, then run the two processes

1. Log into Coverage, go to Settings, click **Connect Gmail** under the new
   "Gmail Live" section. Approve the Google consent screen.
2. Keep two things running (launchd/tmux — whatever keeps a process alive
   on your machine; neither is a cron job like the rest of this app's
   commands):
   - `python manage.py gmail_pubsub_listen` — the actual real-time listener.
     Runs forever; syncs a mailbox the moment Gmail notifies of a change.
   - `python manage.py gmail_watch_renew` — run this one daily (cron is
     fine). Google's `watch()` registration expires every 7 days regardless
     of activity; this keeps it alive.

## What this does and doesn't cover

Real-time detection covers: bounces, replies, outreach-sent, and any
calendar invite (`.ics`) — deterministically, no guessing. It does **not**
catch a chat someone describes only in plain-language prose with no
calendar invite attached ("great chatting yesterday!") — that residue still
needs the existing twice-daily agent-run sync to catch. See
`capture/gmail_live.py`'s module docstring for the full reasoning on why
that's deliberate rather than a gap to close later.

It also does not create new contacts from someone Coverage has never seen —
same division of labor as the daily sync's Step 1 (`capture_gmail`) vs.
Step 2 (`capture_discover`); this build only covers the Step 1 half.
