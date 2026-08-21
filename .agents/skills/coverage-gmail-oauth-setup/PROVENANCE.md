# Provenance

**Source repository:** https://github.com/jezweb/claude-skills
**Source path:** `plugins/integrations/skills/gws-setup/SKILL.md`
**Commit SHA fetched:** `e875a6bfff809e5d42c584104031e36e1f014f18`
**Date fetched:** 2026-07-23
**Original author:** Jeremy Dawes (Jezweb)
**Original license:** MIT (Copyright (c) 2025 Jeremy Dawes (Jezweb)) — preserved in `LICENSE` in this directory, unmodified.

## What this is

An adapted derivative of the `gws-setup` skill's Google Cloud / OAuth
credential setup steps (its Steps 1–3 in the original), narrowed to
Gmail-only and rewritten for a web-app OAuth flow instead of a personal CLI
tool. This is not a verbatim copy — see Modifications below.

## Why adapted rather than installed as-is

The original `gws-setup` (and its companion `gws-install`, also reviewed)
sets up the third-party `@googleworkspace/cli` ("gws") npm package: it
enables 11 Google Workspace APIs (Gmail, Drive, Calendar, Sheets, Docs,
Chat, Tasks, Slides, Forms, People, Admin SDK), recommends `gws auth login
--full` ("full access ... recommended for power users"), and finishes by
running `npx skills add googleworkspace/cli -g --agent claude-code --all`,
which installs 90+ skills **globally** into `~/.claude/skills/` (not scoped
to this project). That is the opposite of what this project needs: a
narrow, Gmail-only skill that helps *build* Coverage's own OAuth
integration, not a skill that installs and drives a separate general-purpose
Workspace CLI against a live mailbox.

## Modifications made

- Removed all non-Gmail API/service content (Drive, Calendar, Sheets, Docs,
  Chat — including the Chat App configuration sub-section, Tasks, Slides,
  Forms, People, Admin SDK).
- Removed the "install the `gws` CLI" and "install 90+ agent skills
  globally" steps entirely. This skill does not depend on, install, or
  reference the `gws` CLI or `@googleworkspace/cli` package.
- Changed the OAuth client type guidance from **Desktop app** (correct for
  a personal CLI tool) to **Web application** (correct for Coverage, a
  server-side web app doing per-user OAuth via HTTP redirect), including
  the corresponding JSON shape (`"web"` key with `redirect_uris`, not
  `"installed"`).
- Replaced "full access (recommended)" scope guidance with a least-privilege
  Gmail scope table (`gmail.readonly`, `gmail.metadata`, `gmail.send`,
  `gmail.compose`, `gmail.modify`, full-account scope) mapped to Coverage's
  actual features (mail scanning, BCC/draft handling).
- Replaced the `gws auth login` / `gws` CLI-based authenticate-and-verify
  steps with a generic OAuth 2.0 authorization-code flow description
  (redirect, callback/token exchange, refresh-token storage, revocation)
  plus a plain `curl` call to `gmail.googleapis.com` for verification —
  since Coverage implements its own OAuth code rather than depending on a
  third-party CLI.
- Added a "Gotchas" section, including a note that BCC-prefill into a
  user's own native Gmail compose window has no REST API path (needs a
  Gmail Add-on or browser extension) — directly relevant to one of
  Coverage's stated features and not covered by the source skill or any
  of the other candidates evaluated.
- Added a short mail-scanning-patterns section (`users.history.list`
  incremental sync vs. `users.watch` + Cloud Pub/Sub push) since this is
  central to Coverage's "scan sent/received mail" feature and wasn't
  covered by the source skill.
- Added a verification/sensitive-scope callout (Google's OAuth app
  verification requirement and ~100-test-user cap for Gmail scopes) since
  it materially affects launch planning and wasn't in the source skill.
- Rewrote the `description:` frontmatter to trigger narrowly on Gmail API
  OAuth/credential setup work, and to explicitly exclude other Workspace
  apps and day-to-day mailbox operation, to avoid collision with other
  skills in this project (a Stripe/billing skill and a CRM/lead-scoring
  skill installed by other agents into the same `.claude/skills/`
  directory).
- Did not incorporate the companion `gws-install` skill (quick reinstall
  of `gws` on an additional machine using existing credentials) — that
  workflow is specific to a personal per-machine CLI tool and doesn't map
  to Coverage's one-time, per-project OAuth client setup.

## Audit verdict

**Clean.** Both source files (`gws-setup/SKILL.md`, `gws-install/SKILL.md`)
were read in full. No prompt injection, no hidden/encoded instructions, no
data-exfiltration patterns, and no `curl | bash`-style install steps were
found in either file (a sibling skill in the same monorepo, `nemoclaw-setup`,
does contain `curl | bash` installers, but that skill was not reviewed for
installation and nothing from it was used here). The scope/design concerns
above are about fitness for this project's narrow purpose, not security.
