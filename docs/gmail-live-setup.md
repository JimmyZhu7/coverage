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
   alive on your machine. **On a Mac, run `./scripts/launchd/install.sh`
   and this step is done**: `com.coverage.gmailpoll` is that job, with
   `KeepAlive` rather than a timer for exactly this reason, and the same
   `--interval 120` production uses. Do not leave it to "I'll start it in a
   terminal." The vagueness of this paragraph is itself how the step got
   skipped for two days while Gmail read as connected and 137 messages sat
   behind the cursor unprocessed. See `docs/see-it-locally.md`.
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
     before trusting it against a student's. `scripts/launchd/install.sh`
     installs this one too, as `com.coverage.gmailbackfill`, at
     production's own `*/5`.

## 9. The 7-day token expiry — resolve this before any pilot, not after

Step 4 above covers *your own account* staying in the free Testing tier
(under 100 test users, no review, no cost). It does not cover a fact that
only matters once someone other than you is a test user: Google's own
documentation states that a refresh token issued to a **test user** on an
app in **Testing** publishing status expires **7 days after consent**, not
on a plan or verification event, on a clock. When it expires, the next sync
attempt fails with an `invalid_grant`-shaped error, `capture/gmail_live.py`
already catches this and flips the connection to `status="revoked"`, and
that student's automatic capture goes silent until they reconnect.

This means Coverage's core differentiator, capture that runs without the
student doing anything, cannot survive a multi-week pilot in plain Testing
mode. Nobody has to remember to reconnect if nothing tells them to, and
nothing does yet (see `ops/views.py` for whatever visibility exists as of
whenever you're reading this).

**One open question decides the whole path, and it is a half-day, no-cost
experiment, not a research question:**

Google's docs are unclear on whether flipping the OAuth consent screen from
"Testing" to "In production" **without** submitting for verification removes
the 7-day expiry while keeping the 100-user cap (just a scarier unverified-app
warning screen), or whether it does nothing for the expiry at all. Nobody has
run this experiment. To run it:

1. In the same OAuth consent screen from step 4, find the button to move the
   app from **Testing** to **In production** (not "submit for verification",
   just the publishing-status toggle — these are separate actions in the
   Console). Do this on the test client, not whatever's live for real users.
2. Connect a test Gmail account through the normal flow (`connect_gmail`,
   Settings → Connect Gmail).
3. Wait 8 days (or, faster: manually adjust `GmailConnection.connected_at`
   in the DB to 8 days ago and force a sync — `python manage.py gmail_poll`
   or trigger `sync_connection()` directly — to see if it still fails; this
   is a heuristic shortcut, not proof, since `connected_at` isn't updated on
   token issuance either, see the caveat below).
4. If the sync still succeeds past day 7: "In production, unverified" is the
   path. Pilot proceeds on this setting, capped at 100 users, no CASA, no
   verification cost, just the scarier consent screen every student clicks
   through once.
5. If it still expires at day 7: full CASA verification is the only fix for
   reliable multi-week capture. Budget $1,500-$8,000 and 2-3 months elapsed
   (third-party CASA assessor pricing, Google itself publishes no price
   list) — meaning submission needs to happen by **early November 2026** to
   clear before a February 2027 pilot.

**Caveat carried over from the code:** `GmailConnection.connected_at` is
`auto_now_add=True` and is NOT updated on a reconnect
(`connect_gmail`'s `update_or_create` doesn't include it in `defaults`), so
it is not a reliable "when was the current token issued" timestamp for any
mailbox that has ever been reconnected. Don't build anything that trusts it
as precise; it's a rough proxy at best, accurate only for a connection's
first-ever consent.

**Also gates on this, independent of which path wins:**
`templates/legal/privacy.html` is still marked `DRAFT. NOT REVIEWED BY A
LAWYER.` at the top of the file. Both the Testing path (a reviewer never
looks, but you're still asking real students to trust a draft policy) and
the CASA path (a reviewer explicitly checks this page for the Limited Use
language per step 4 above) need this off DRAFT before real students connect
Gmail. This is on you and counsel, not something to script around.

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
