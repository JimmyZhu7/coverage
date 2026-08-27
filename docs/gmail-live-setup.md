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
- Privacy policy URL: your deployed `/privacy/`. When you do go through
  verification, a reviewer reads that page looking for the Limited Use
  representation for `gmail.readonly`. It is there, in the "Google API
  Limited Use" section, in Google's own required wording — see the
  DO-NOT-REWORD note at the top of `templates/legal/privacy.html`. The page
  is still marked DRAFT and still has unfilled placeholders, so it needs the
  lawyer pass and the entity details before it is submittable.

## 5. Pub/Sub topic + subscription — the topic is required, the rest is optional

**Read this before doing the subscription half.** There are two ways to
drive Gmail Live, and only one of them needs Pub/Sub at all:

| | `gmail_pubsub_listen` (push) | `gmail_poll` (polling) |
|---|---|---|
| How it wakes up | Gmail notifies via Pub/Sub | a timer |
| Latency | a second or two | up to one `--interval` (default 120s) |
| Needs a Pub/Sub **topic** | yes | no (but `is_configured()` still wants the setting — see below) |
| Needs a Pub/Sub **subscription** | yes | no |
| Needs Google Cloud credentials (ADC) | **yes** | **no** |

Both end in the identical call — `gmail_live.sync_connection()` — so
classification, the history window, the 7-day re-anchor and
`apply_findings` behave exactly the same either way. Latency is the only
difference.

**If `gcloud` works for you, do the whole section and use the listener.**
It is genuinely better.

**If it doesn't, skip the subscription and use `gmail_poll`.** Pulling from
Pub/Sub is the one step that needs Application Default Credentials, and
there are two common walls:

- `gcloud auth application-default login` opens the browser, you consent,
  and then it reports *"Could not reach the login server."* The token comes
  back to a loopback listener on your own machine, and a captive or
  filtered network (USC campus wifi, most guest networks, plenty of
  corporate ones) blocks that hop. `--no-launch-browser` does not help —
  the hand-back is what fails, not the browser.
- The usual workaround, a service-account JSON key, is refused outright if
  your Google Workspace tenant sets the org policy
  `iam.disableServiceAccountKeyCreation`. University tenants generally do.
  Only a domain admin can lift it.

Neither is fixable from a terminal, and neither blocks polling, because
polling never talks to Google Cloud — only to the Gmail API, with the same
per-user OAuth refresh token the connect flow already stored.

### The topic (still needed)

Pub/Sub → Topics → Create Topic. Name it anything, e.g. `gmail-live`.

Creating a topic needs no ADC and no key — it is a click in the Console.
Do it even for the polling-only path: `gmail_live.is_configured()` is the
whole feature's off-switch and checks `GMAIL_LIVE_PUBSUB_TOPIC` is set, and
`users.watch()` needs a real topic if you ever get push working later.

Grant Gmail's own push service publish rights on it — this is the one
non-obvious step:
- Open the topic → Permissions → Add Principal.
- Principal: `gmail-api-push@system.gserviceaccount.com`
- Role: **Pub/Sub Publisher**

### The subscription (push only — skip it if you are polling)

Then create a subscription on that topic:
- Pub/Sub → Subscriptions → Create Subscription.
- Delivery type: **Pull** (not push — see `gmail_live.py`'s docstring for
  why: pull means this works before Coverage is deployed anywhere public).

And give the listener credentials to pull with:
`gcloud auth application-default login`, or a service-account key with the
`roles/pubsub.subscriber` role pointed at by
`GOOGLE_APPLICATION_CREDENTIALS`. Without one of those,
`gmail_pubsub_listen` exits immediately with
`google.auth.exceptions.DefaultCredentialsError` — see the two walls above.

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

`GMAIL_LIVE_PUBSUB_SUBSCRIPTION` is the only one of the five you can leave
blank on the polling path — nothing but `gmail_pubsub_listen` reads it.

`gmail_live.is_configured()` gates everything on the other four being set —
until then the Settings page shows nothing new and the management commands
below just no-op.

## 8. Connect, then run the processes

1. Log into Coverage, go to Settings, click **Connect Gmail** under the new
   "Gmail Live" section. Approve the Google consent screen.
2. Keep the sync running. Pick ONE of these two (see §5 for which):
   - `python manage.py gmail_poll --interval 120` — **the path that works
     without any Google Cloud credentials.** Polls every connected mailbox
     every two minutes, forever, until killed. Runs one pass and exits if
     you drop `--interval`, which makes it a normal cron command instead.
     Two minutes is the default because quota is not the constraint (an
     empty poll is two quota units against a per-project allowance in the
     hundreds of millions per day) — OAuth token-refresh churn is, and an
     access token lasts an hour. Worst-case latency is one interval; for
     "a recruiter replied" that is indistinguishable from push. Run
     `--dry-run` first: it reports how many messages are waiting per
     mailbox and writes nothing.
   - `python manage.py gmail_pubsub_listen` — the real-time listener, if
     you got ADC working. Runs forever; syncs the moment Gmail notifies.
   Either way this is a long-running process, not a cron job like the rest
   of this app's commands — launchd/tmux/systemd, whatever keeps a process
   alive on your machine.
3. Also run, on a cron:
   - `python manage.py gmail_watch_renew` — daily. Google's `watch()`
     registration expires every 7 days regardless of activity; this keeps
     it alive. **Only matters on the push path** — a poller does not use
     the watch at all, so on the polling path this command is harmless but
     pointless, and you can leave it off.
   - `python manage.py gmail_backfill` — run this one every 10-15 minutes
     (cron). It's the one-time historical pass for each newly-connected
     mailbox: `connect_gmail()` marks a fresh connection
     `backfill_status="pending"` the moment the live watch is registered,
     and this command is what fills in the past for it — per-contact Gmail
     search over existing contacts only, so it can never invent a new
     contact from old mail. See `capture/gmail_live.py::backfill_connection`
     and `capture/management/commands/gmail_backfill.py` for the full
     window logic (365 days for a contact with zero touches, a 90-day-capped
     7-day-overlap window for one already touched). Most ticks find nothing
     pending and no-op instantly. Use `--dry-run` to see what a run would do
     without writing anything — worth doing once against your own mailbox
     before trusting it against a student's.

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
Step 2 (`capture_discover`); this build only covers the Step 1 half. The
one-time backfill (`gmail_backfill`, above) inherits the same rule: it only
searches mail for contacts already in the CRM, which is also why onboarding
now tells a new student to import their existing contacts before connecting
Gmail — a contact Coverage has never heard of has no history to backfill.
