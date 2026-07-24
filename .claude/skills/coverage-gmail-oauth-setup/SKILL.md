---
name: coverage-gmail-oauth-setup
description: "Set up Gmail API access for the Coverage app: create/configure a Google Cloud project, enable the Gmail API, configure the OAuth consent screen, create a Web-application OAuth 2.0 client, choose least-privilege Gmail scopes, and wire up the authorization-code flow (redirect, token exchange, refresh-token storage). Use when building or debugging Gmail sign-in/authorization, Gmail OAuth client credentials, Google Cloud Console setup for Gmail, Gmail API scopes, or the mail-scanning (history.list / watch+Pub/Sub) calls that read a connected mailbox's message history. Not for Drive, Calendar, Docs, Sheets, or Chat setup, and not for operating an already-connected mailbox day to day."
compatibility: claude-code-only
---

# Gmail API OAuth Setup

Guide for setting up Google Cloud + OAuth 2.0 credentials so the Coverage app
can obtain per-user Gmail API access, and for wiring the resulting credentials
into an authorization-code OAuth flow. Scope is intentionally Gmail-only —
do not enable or reference other Google Workspace APIs (Drive, Calendar,
Docs, Sheets, Chat, Admin SDK) from this skill; those are out of scope for
this project.

## Prerequisites

- Access to Google Cloud Console (console.cloud.google.com)
- A decision on Coverage's OAuth redirect URI(s) — e.g.
  `http://localhost:3000/auth/google/callback` for local dev and
  `https://<prod-domain>/auth/google/callback` for production

## Workflow

### Step 1: Pre-flight checks

Check what's already done before repeating steps:

```bash
# Does a client secret / env config already exist?
ls .env 2>/dev/null && grep -i GOOGLE_CLIENT .env
```

If Google OAuth client credentials are already configured, skip to Step 6
(Choose Gmail Scopes) or Step 8 (Verify Access) as appropriate.

### Step 2: Create or select a GCP project

Direct the user to `https://console.cloud.google.com/projectcreate`, or use
an existing project. Ask which they prefer.

### Step 3: Enable the Gmail API (only)

Direct the user to:
`https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=PROJECT_ID`

Enable **Gmail API**. Do not enable other Workspace APIs unless a specific,
explicit feature of Coverage needs them — this project intentionally stays
Gmail-only.

### Step 4: Configure the OAuth consent screen

Direct the user to:
`https://console.cloud.google.com/apis/credentials/consent?project=PROJECT_ID`

Settings:
- User Type: **External** (works for any Google account) unless the user
  confirms Coverage will only ever be used inside a single Google Workspace org
- App name: Coverage (or the name the user prefers)
- User support email / developer contact: their email
- Scopes: leave blank here — request scopes at auth-flow time (Step 6)
- Add their own Google account (and any other early testers) as test users
  while the app is in "Testing" status
- Save through all screens

> **Verification heads-up:** Gmail scopes (`gmail.readonly`, `gmail.send`,
> `gmail.modify`, etc.) are treated by Google as sensitive/restricted scopes.
> "Testing" status caps the app at ~100 manually-added test users and shows
> an "unverified app" warning to anyone else. Moving past that (more users,
> no warning screen) requires Google's OAuth app verification process, which
> for these scopes typically includes a third-party security assessment.
> Budget real time for this before assuming Coverage can onboard arbitrary
> users — it is not a same-day process. Confirm current requirements at
> Google's OAuth verification docs before committing to a launch date.

### Step 5: Create the OAuth client — Web application

Direct the user to:
`https://console.cloud.google.com/apis/credentials?project=PROJECT_ID`

1. **Create Credentials → OAuth client ID**
2. Application type: **Web application** (not Desktop app — Coverage is a
   server-side web app doing per-user OAuth via an HTTP redirect, not a
   personal CLI tool)
3. Add the exact redirect URI(s) decided in Prerequisites under
   "Authorized redirect URIs"
4. Create, then copy the JSON or download it

Ask the user to provide the downloaded JSON (paste content or give the file
path). Expected shape for a **Web application** client (note the top-level
`"web"` key, not `"installed"` — that's the Desktop-app shape and won't
match what Console gives you here):

```json
{
  "web": {
    "client_id": "...",
    "project_id": "...",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_secret": "...",
    "redirect_uris": ["https://yourapp.com/auth/google/callback"]
  }
}
```

Store `client_id` / `client_secret` as environment variables or in a secrets
manager — never commit them. Add the credentials file (if downloaded to
disk) to `.gitignore` immediately.

### Step 6: Choose Gmail scopes (least privilege)

Pick the narrowest scopes that cover the feature being built. Don't default
to "request everything" — each added scope is more surface for Google's
verification review and more blast radius if a token leaks.

| Scope | Grants | Use for |
|---|---|---|
| `gmail.readonly` | Read all mail + metadata | Scanning sent/received mail to detect contacts and interaction history |
| `gmail.metadata` | Headers/labels only, no body/attachments | Cheaper alternative to `gmail.readonly` if body content is never needed |
| `gmail.send` | Send only, cannot read | Sending mail on the user's behalf without needing read access |
| `gmail.compose` | Create/update drafts, send | If Coverage creates drafts (e.g. a pre-filled reply) rather than sending directly |
| `gmail.modify` | Read/write/label, no permanent delete | If Coverage needs to apply labels (e.g. tag tracked threads) in addition to reading |
| `https://mail.google.com/` | Full account access | Avoid — almost never actually needed; the broadest possible grant |

For "scan sent/received mail to auto-detect contacts and warmth", start with
`gmail.readonly` (or `gmail.metadata` if the feature only needs headers, not
body text). Add `gmail.send` or `gmail.compose` only once a feature actually
sends or drafts mail.

### Step 7: Implement the authorization-code flow

Standard OAuth 2.0 authorization-code flow against Google's endpoints
(language-agnostic — implement with whatever HTTP/OAuth library fits
Coverage's stack):

1. **Redirect the user** to
   `https://accounts.google.com/o/oauth2/v2/auth` with `client_id`,
   `redirect_uri`, `response_type=code`, `scope` (space-separated scope
   URLs from Step 6), `access_type=offline`, and `prompt=consent`.
   - `access_type=offline` is required to receive a `refresh_token` at all.
   - `prompt=consent` is required to force a `refresh_token` on **every**
     grant — without it, Google only returns one on a user's very first
     consent, which silently breaks re-auth after a token is revoked or lost.
2. **Handle the callback** at the registered redirect URI: exchange the
   `code` for tokens via `POST https://oauth2.googleapis.com/token` with
   `client_id`, `client_secret`, `code`, `grant_type=authorization_code`,
   `redirect_uri`.
3. **Store the `refresh_token` encrypted, per user** — this is what lets
   Coverage scan mail in the background without the user re-authenticating
   each session. Never log it or return it to the client.
4. **Use the short-lived `access_token`** for Gmail API calls; refresh it
   via `grant_type=refresh_token` against the same token endpoint when it
   expires (Google client libraries usually do this automatically).
5. **Revoke** via `POST https://oauth2.googleapis.com/revoke` with
   `token=<refresh_token_or_access_token>` when a user disconnects Gmail.

### Step 8: Verify Gmail API access

A minimal end-to-end check once a token is obtained:

```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  https://gmail.googleapis.com/gmail/v1/users/me/profile
```

A healthy response looks like:

```json
{"emailAddress": "user@example.com", "messagesTotal": 12345, "threadsTotal": 6789, "historyId": "..."}
```

If this 401s, the token or scope is wrong before anything else is debugged.

### Step 9: Mail-scanning patterns (for contact/history detection)

Two standard patterns for "scan sent/received mail" — pick based on how
real-time Coverage needs to be:

- **Polling via `users.history.list`** — after an initial full sync, store
  the returned `historyId` and pass it as `startHistoryId` on subsequent
  calls to get only what changed. Far cheaper than repeatedly re-listing
  all messages with `users.messages.list`.
- **Push via `users.watch` + Cloud Pub/Sub** — register a watch that has
  Gmail publish new-mail notifications to a Pub/Sub topic Coverage
  subscribes to; near-real-time instead of polling, but requires Pub/Sub
  setup and watch renewal (watches expire after ~7 days).

## Gotchas

- **BCC-prefill on compose is not a Gmail REST API feature.** There is no
  API call that injects a BCC address into the native Gmail web compose
  window a user has open — the REST API only reads/writes mail Coverage
  already has a message/draft ID for. To get a tracking BCC onto mail the
  user composes by hand in Gmail's own UI, the real options are a Gmail
  **Workspace Add-on** (Apps Script, adds UI inside Gmail) or a **browser
  extension** (content script in the compose DOM). If instead Coverage
  itself sends or drafts the mail (`gmail.send` / `gmail.compose`), adding
  a BCC is trivial — it's only "the user's own native compose window" case
  that has no API path. Confirm which of these Coverage actually needs
  before committing to a scope/architecture.
- Without both `access_type=offline` and `prompt=consent`, re-auth after a
  revoked/expired refresh token can silently fail to issue a new one.
- Sensitive Gmail scopes trigger Google's "unverified app" warning and the
  100-test-user cap until the app passes verification — see Step 4.

## Environment Variable Convention

```bash
GOOGLE_CLIENT_ID="..."
GOOGLE_CLIENT_SECRET="..."
GOOGLE_REDIRECT_URI="https://yourapp.com/auth/google/callback"
```

Keep these out of version control; load from `.env` (gitignored) locally
and from the deployment platform's secret store in production.
