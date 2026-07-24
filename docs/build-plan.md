# Coverage — First-Build Plan

*Authored 2026-07-23. Inputs: `product-brief.md`, `existing-system.md`, founder decisions
(fresh codebase / port selectively; no billing in v1).*

## What this actually is

Coverage is a shared, centrally-scraped deadline calendar — a commodity, given away free —
wrapped around a private per-student relationship ledger whose only defensible value is the
email activity students feed it. The build is therefore **not** "make the existing system
multi-tenant." It is: stand up a small multi-tenant web app, lift four proven pure-logic
libraries out of the existing repos (state machine, cadence, apply layer, connectors), and
spend every remaining hour on the one unproven thing — getting outreach captured, and
instrumenting whether that capture actually happens.

## Decisions

| Decision | Choice | Why | What it forecloses |
|---|---|---|---|
| Language | Python 3.13 | ~2,000 LOC of connector code is Python; a rewrite buys nothing. 3.13 over 3.14 for PaaS base-image and wheel breadth; ported code uses nothing newer than 3.10 syntax. | Nothing real |
| Web framework | Django 5.x | Solo founder gets auth, migrations, and an admin for free. The admin alone replaces an internal-tools build for support/debugging. Ported raw-SQL logic stays raw SQL in one module. | Native async/websocket realtime (fine — "instant alerts" is a paid later feature) |
| Database | Managed Postgres | Concurrent writers (web + scrape worker + inbound-mail webhook) is precisely SQLite's weak spot. JSONB for signal payloads. The atomic `UPDATE ... CASE` ports near-verbatim. | Zero-ops local-file simplicity |
| Frontend | Django templates + htmx | One person cannot maintain an SPA *and* a backend. The product idiom (cards, queues, tables) renders fine server-side. | App-like offline/mobile feel |
| Hosting | One PaaS (Render or Fly) + managed PG + Postmark | Managed everything; Render has built-in cron. **Undecided between the two — resolve with a one-day deploy spike in M0.** | Fine-grained infra control |
| Background work | Cron management commands; webhook ingest handled synchronously | Extractors run in <1s; scraping is periodic. No Celery/Redis — queue infra is solo-founder overhead with no v1 payoff. | High throughput; revisit ~1k users |
| Auth | Google sign-in (**login-only scopes**) + email magic link | Students live on Google. Login scopes are unrestricted and never touch the CASA question. Magic link covers Microsoft-shop schools. No passwords stored, ever. | Username/password (deliberately) |
| Email capture v1 | Per-user inbound address (BCC/forward) + manual quick-log + spreadsheet import, all behind one `CaptureProvider` interface | Zero Google review required. The interface already exists in embryo as `apply_enrichment`'s findings contract. | Zero-effort capture — which is exactly the experiment v1 runs |
| Fit score | Fully deterministic over typed signals; LLM only as a residue classifier for ambiguous text | Reproducibility, cost, and the existing "code has final say over the model" pattern. | Model-magic scoring claims in marketing |
| Billing | None. No plan caps enforced. | Fixed constraint; caps ship with billing, not before. | Nothing |
| Testing | pytest + pytest-django from commit one; port the *semantics* of the 14 gmail_enrich and cadence regression tests first | Those tests encode real production bugs. Route-level tenant-isolation tests are new and mandatory. | The stdlib-assert style |
| Metrics | In-house append-only `product_events` + Monday founder email | The 40% gate must be a query, not a hope. No third-party analytics touching email-adjacent data. | Fancy funnels |

---

## 1. Stack

**Stay Python; move to Django + Postgres; keep ported logic as plain functions.**

The deciding asset is `sources.py` (1,095 lines) plus `discovery.py`/`verify_rows.py` —
framework-free fetchers for ~25 Workday boards, Greenhouse, Lever, Oracle Recruiting Cloud,
tal.net, HSBC sitemap, Amazon/Tencent/ByteDance. Rewriting that is weeks of re-verification
against live boards for zero user-visible gain.

Django over Flask/FastAPI is a solo-founder call, not a technology call: `django-allauth`,
real migrations (replacing the hand-rolled `PRAGMA table_info` style in `db.py:120-144`), and
the admin. Ported modules live in `coverage_domain/`, take a DB connection, and know nothing
about Django. `apply_touch`'s TOCTOU-safe single-statement update (`pipeline.py:88-101`)
stays raw SQL — it is the reviewed artifact.

**Repo layout:** one monorepo, three packages — `coverage_web` (Django project),
`coverage_domain` (state machine, cadence, apply layer, scoring), `coverage_connectors`
(scrapers + verification). Connectors run from cron on the same deployment.

## 2. Multi-tenant data model

Two zones, hard line between them. Shared tables have no `user_id` and are written only by the
central worker. Private tables carry `user_id` **denormalized onto every row including
`touches`**, so isolation checks and indexes are direct.

### Shared zone

```sql
firms          (id, slug UNIQUE, name, domains text[], regions text[],
                tracks text[], sponsors, status)
opportunities  (id, firm_id→firms, title, bucket, region, location, cohort,
                status, deadline date, deadline_precision, confidence,
                sponsorship, url, source, first_seen, last_verified,
                last_checked, content_hash, UNIQUE(firm_id, url))
firm_dates     (id, firm_id, cycle, region, event_kind, date, precision,
                confidence, source_url, found_on, history jsonb,
                UNIQUE(firm_id, cycle, region, event_kind))
email_pattern_stats (firm_id PK, delivered, bounced, last_updated)
scrape_runs    (id, connector, started, finished, status, stats jsonb, error)
```

`email_pattern_stats` is shared deliberately: aggregate counts only, never addresses. Every
user's bounces improve pattern confidence for everyone — a real, cheap network effect. Raw
bounce events stay private.

### Private zone

```sql
users            (id, email UNIQUE, google_sub, name, school, class_year,
                  target_cycle, regions text[], tracks text[], assets jsonb,
                  capture_slug UNIQUE, onboarded_at, created, deleted_at)
user_firms       (user_id, firm_id, tier smallint, status, PK(user_id, firm_id))
contacts         (id, user_id, name, firm_id NULL, firm_text NULL, role, email,
                  linkedin, source, warmth DEFAULT 'cold',
                  thread_state DEFAULT 'no_reply', angle, notes,
                  school_affiliation, created, archived, email_pattern_recorded)
touches          (id, user_id, contact_id→contacts, ts, channel, kind, note,
                  source enum(manual|capture|import), capture_event_id NULL)
                  -- append-only; no UPDATE path exists in app code
capture_events   (id, user_id, provider, provider_ref, direction,
                  counterparty_email, counterparty_name, occurred_at,
                  received_at, raw_ref, signals jsonb, extraction_version,
                  status enum(pending|applied|needs_review|ignored),
                  UNIQUE(user_id, provider, provider_ref))
tasks            (id, user_id, title, why, due, kind, firm_id, source_key, status, created)
user_opportunities (user_id, opportunity_id, applied_status, interview_dates jsonb,
                  dismissed, PK(user_id, opportunity_id))
fit_scores       (id, user_id, subject_type enum(contact|firm), subject_id,
                  axes jsonb, composite, reasoning text, params_version,
                  inputs_hash, computed_at)
product_events   (id, user_id NULL, event, props jsonb, ts)
imports          (id, user_id, kind, filename, row_stats jsonb, created)
```

### Deliberate calls

- **The warmth machine ports untouched.** Same touch kinds, same `TOUCH_TRANSITIONS`, same
  ratchet, same terminal-`advocate` guard, same single-statement atomic update
  (`pipeline.py:14-31`, `88-101`) — translated to Postgres parameter style, nothing more.
  One addition, not a redesign: the manual override path also inserts an audit touch, so the
  immutable log stays complete.
- **`contacts.firm_id` is nullable with a `firm_text` fallback.** Students will name firms
  outside the directory; capture must never block on directory coverage. A nightly job proposes
  `firm_text` → `firms` matches for one-click confirmation.
- **Dedup stays application-layer**, matching the existing design. The only load-bearing DB
  uniqueness is `capture_events(user_id, provider, provider_ref)` — it makes at-least-once
  webhook delivery idempotent.
- **Drop the `gender` column.** Defensible as one person's private notes; in a multi-tenant
  product it is a liability with no v1 feature attached.
- **Isolation enforcement:** a Django manager that refuses unscoped queries on private models,
  plus a parametrized route test asserting user B gets 404 on every user-A object. Postgres RLS
  is v2 hardening, not v1 scope.

## 3. Auth

**Google sign-in with login-only scopes (`openid email profile`) via django-allauth, plus email
magic link. No passwords.**

The audience lives in Google accounts; storing zero passwords is the right posture for one
founder with no security staff; and login-only Google OAuth is **unrestricted** — it keeps the
Google relationship entirely separate from the Gmail-scopes question.

> **The login client must never request any `gmail.*` scope.** If a Gmail capture provider ever
> ships, it uses a separate incremental-consent flow, so a verification stall can never break
> sign-in.

## 4. Port / rewrite / build-new triage

### Port (lift as libraries; edits limited to storage adapter + tenancy parameter)

| Component | Notes |
|---|---|
| `campaign/pipeline.py` | The core IP. Postgres parameter style only. **Port its tests first, then the code.** |
| `campaign/cadence.py` + `cadence.yaml` | `due_actions` runs per user; yaml values become global defaults. Region scoping and the second-chat thank-you fix come free. |
| `campaign/tasks.py` + `db.upsert_task` | Backward planner fires only on `confirmed_official` — keep exactly. The ≤3-day in-place update prevents duplicate-task spam at scale. |
| `campaign/confidence.py` | Port verbatim. The "code caps the model's claim" pattern becomes the rule for LLM-extracted capture signals. |
| `campaign/kb.py` corroboration/promotion | Logic ports; YAML load/save replaced by `firm_dates` reads/writes. |
| `netdash/gmail_enrich.py` apply layer | Port the stage ratchet, bounce-archive-with-evidence, contact dedup, thread-marker dedup, no-thread 7-day fallback. **Drop:** file-based sync-window/energy-cache plumbing, USC-specific constants (generalize to "school discovery"). |
| `sources.py` / `discovery.py` | Into `coverage_connectors`. |
| `verify_rows.py` | Pointed at `opportunities`. Staleness banners are a brand feature — keep. |
| `prescan.py` | **Port the mechanism, discard the taxonomy.** HOT/WARM/COLD + RESOLVED/RESIDUE is the generic LLM cost-control asset. `_IB_ST_FIRM_CATEGORY` and the HK/US Class-of-2028/29 scope become rows in `firms`/`user_firms`. |

### The shared-cache design, concretely

Connectors are already user-agnostic — a JPMorgan Oracle board fetch is identical for every
student. The per-user parts today are the *filters* applied at fetch time. In Coverage: ingest
broadly into shared `opportunities`; per-user relevance becomes a **read-time query**
(`user_firms` join + region/track predicates), not a fetch.

Consequences worth stating: outbound request volume scales with **boards, never users** — which
is also the legal posture (the brief's hiQ note) holding at scale. Heat, verification, and
residue are computed once globally. A conflict about Goldman's close date is resolved once for
everyone.

The `claude -p` residue pass stays a **centrally-run, founder-supervised weekly job** in v1 —
because the data it feeds is shared, the single-operator pattern still works. Converting it to
API calls is a v1.x cleanup, not a launch blocker.

### Rewrite (concept survives, implementation replaced)

- `tracker_io.py` + `tracker.md` + JSON side-stores → `opportunities` / `user_opportunities`.
  The map is unambiguous: this must *become* a database. Applied-status vocabulary and per-round
  interview dates carry over as data shapes.
- `config.py` root-walk + `profile.yaml` → `users` row + settings.
- Both Flask UIs → new Django/htmx UI. Port the *semantics* (opportunity cards, review queue,
  priority lists), not the code. No route code is tested today anyway.

### Build new
Auth/onboarding, capture layer + interface, fit score, weekly digest, instrumentation,
privacy/deletion/export, CSV import.

### Drop
`handshake-scan/` (per-user SSO + Playwright has no shippable multi-tenant story — founder keeps
it as a personal tool); `brief.py`/`week.py`/`status.py` (personal-voice reports; ideas inform
the digest); `coverage.py` gap engine (good year-2 club feature; also the naming collision);
the `gender` column.

## 5. Email capture — the swappable component

**The interface is the `InteractionEvent`, and the seam is exactly where
`gmail_enrich.apply_enrichment` already draws it** (`gmail_enrich.py:141-144`). The existing
system proved the shape: mailbox reading happens *somewhere else*; a typed finding arrives; a
deterministic apply layer ratchets state.

```
CaptureProvider (adapter per source) emits:

InteractionEvent {
  user_id, provider,          # bcc | forward | manual | import | (later: extension | gmail_api)
  provider_ref,               # stable dedup key: Message-ID / thread key / import-row hash
  direction,                  # outbound | inbound
  counterparty_email, counterparty_name?,
  occurred_at, raw_ref,       # pointer to stored raw artifact (retention-policied)
  signals {                   # typed, versioned — never raw text into scoring
    outreach_sent?, replied?, bounced?,
    chat_scheduled_at?, chat_completed?, evidence_quote?
  }
}
```

**Pipeline, identical for every provider:** idempotent insert → **resolver** (match counterparty
to a contact by email, else normalized name; unknown → auto-create a `pending` contact for
one-click confirm) → **deterministic extractors** (direction from headers; bounce via
delivery-failure patterns; scheduling via .ics/date-language detection) → **residue** (ambiguous
text only) goes to an LLM classifier constrained to the same typed signals, tagged
`extracted_by`, with low-confidence results landing `status=needs_review` as one-click
confirmations — which doubles as an engagement loop → **ported apply layer** ratchets state via
`apply_touch`. Same stage ratchet; `[gmail:<thread_id>]` becomes `[capture:<event_id>]`.

### v1 default — zero Google review

Every user gets `u-<slug>@in.coverage.app` (**unique-per-user, not plus-addressing** — plus-tags
get stripped by some clients). Postmark inbound webhook → the pipeline above. Users BCC it on
outbound mail and forward replies, or set a Gmail auto-forward filter. Gmail's
forwarding-confirmation email arrives *at our inbox*, so onboarding can display the confirmation
code in-app — a genuinely smooth assisted setup.

### The BCC-prefill gap, honestly

No REST API pre-fills BCC in native compose. The v1 answer is that **composes start from
Coverage**: every contact and cadence action carries a `mailto:` link with `to` + `bcc`
prefilled. Works in Gmail-as-handler, Apple Mail, Outlook — **verify with a 30-minute client
matrix spike in M3.** That converts the weekly priority list into the compose surface, which is
where we want users anyway.

### Documented upgrade ladder (interface constant, adapter swapped)

1. **v1** — capture address + mailto + manual + import. Zero review anywhere.
2. **v1.x** — MV3 browser extension injecting BCC into Gmail compose. Chrome Web Store review
   only; no Google API verification. The single strongest capture-rate lever available without
   restricted scopes. Build *only after* month-2 data says friction, not motivation, is the
   bottleneck.
3. **v2** — Gmail API provider (`gmail_api` adapter emitting the same events from `history.list`
   diffs) and/or a Workspace Add-on with compose-time BCC. **Gated behind an explicit
   cost-research decision** (CASA tier, dollars, months). The installed
   `coverage-gmail-oauth-setup` skill is the how-to for that day. **Marketing must not promise
   "connects to Gmail" until this gate is passed.**

## 6. The fit score

New IP; nothing to port. (`confidence.py` scores *sources*, not people.) Two scores from one
engine, both **pure functions of (immutable events + touches, as-of date, params_version)**,
cached in `fit_scores` with `inputs_hash` so identical inputs provably reproduce identical
outputs.

### Contact Warmth Score — independent axes, each 0–100

- **Depth** — highest stage *evidenced in the touch log* (replied < chatted < advocate-marked),
  from touches, not the warmth column.
- **Responsiveness** — reply ratio and median reply latency across their thread history.
- **Recency** — exponential decay on last meaningful interaction, half-life ~45 days. A warm
  reply 8 months back carries `0.5^(240/45)` ≈ 2.5% weight.
- **Leverage** (coarse in v1) — role seniority keyword tier + school affiliation flag.

> **Where decay lives, and why this isn't a contradiction.** The stored warmth ratchet
> deliberately never decays; the Recency axis owns decay entirely. *Stage is a historical fact;
> temperature is a computation.* That is how "port the state machine" and "compute warmth on
> read" coexist.

### Firm Fit Score (user × firm)

- **Network strength** — top-k weighted contact scores at the firm, advocates overweighted
  (the existing `advocate_target: 2` doctrine becomes the yardstick).
- **Momentum** — interaction volume trend over trailing 14/45-day windows.
- **Timeline readiness** — shared `firm_dates` position vs the user's coverage. App opens in 30
  days with zero warm contacts scores low **and says so**.
- **Structural fit** — rule-based in v1: region overlap, track overlap, sponsorship flag against
  the user's work-authorization answer. **Labeled honestly in-product as rules**, because v1 has
  no outcome data to learn from.

Composite = weighted sum, displayed as a band plus the axis breakdown. **Every score row stores
a reasoning line**, template-generated deterministically from the top axis contributions
("2 chats, last 12d ago; replies within a day; no advocate yet — app opens in ~5 weeks"). Not
LLM-written: reproducibility and cost beat prose polish, and this audience trusts a legible
formula over vibes.

### Deterministic vs LLM, explicitly

Scoring math, decay, reasoning lines, and axis extraction from typed signals are **all
deterministic**. The LLM appears in exactly one place in the entire product: classifying
ambiguous captured text into typed signals (the residue path in §5), upstream of scoring, with
code-enforced caps and human confirmation on low confidence. **The score itself never calls a
model.**

The weekly priority list is then the closed loop from the brief: ported `due_actions` output,
ranked by `(cadence priority, firm fit score desc, app_close proximity)` — deterministic and
explainable end to end.

## 7. Sequencing

Target: second-student end-to-end moment at ~10 weeks of focused solo work. M1 is deliberately
thin — seed data covers what connectors don't yet.

**M0 — Foundations (wk 1).** Django app deployed on PaaS with managed PG; Google + magic-link
sign-in; base templates; Sentry; CI (pytest + Postgres service). Privacy policy / ToS drafted.
*DoD: founder signs in on the production URL; CI green.*

**M1 — Shared calendar (wk 2–3).** `firms`/`opportunities`/`firm_dates` schema; one-time seed
importer from `firms.yaml`, `kb/timeline_*.yaml`, and a `tracker.md` snapshot; port Greenhouse +
Lever + Workday connectors only; port verification/staleness; cron scrape; **public (no-login)**
calendar page with confidence + staleness honesty markers.
*DoD: calendar updates daily for a week with zero founder intervention; a logged-in user sees a
my-firms overlay.*

**M2 — CRM + state machine (wk 4–5).** Contacts/touches; `apply_touch` port with its regression
tests ported *first*; manual quick-log UI with visible warmth-movement feedback; CSV import;
cadence `due_actions` + today view. **Founder cutover: migrate the real `campaign.db` through the
import path and stop using the CLI.**
*DoD: founder runs his actual recruiting week entirely in Coverage; import path proven on real
data.*

**M3 — Capture v1 (wk 6–7).** Per-user capture address via Postmark inbound; `capture_events`
pipeline; deterministic extractors; resolver with pending-contact confirm; ported apply ratchet;
capture-health strip ("last received…"); onboarding test-message step that verifies the loop
before the user relies on it; `mailto:` compose with BCC prefilled everywhere an action suggests
an email; client-matrix spike.
*DoD: founder BCCs a real outreach; touch + warmth movement visible in-app within 2 minutes, no
manual steps; duplicate webhook delivery provably no-ops.*

**M4 — Fit score + weekly list (wk 8–9).** Scoring engine with golden-fixture tests (same inputs
→ byte-identical axes/reasoning); score UI with axes + reasoning; weekly priority page; Monday
digest email (Postmark outbound, unsubscribe link).
*DoD: snapshot tests pass; founder's Monday digest arrives with correct, explainable rankings.*

**M5 — Second-student readiness (wk 10).** Onboarding (school/cycle/tracks → pick firms from
shared directory → import prompt → capture setup); instrumentation at every step; self-serve
deletion + CSV export; rate limiting; 3–5 real users from the founder's network.
***DoD — the real v1:** a non-founder completes signup → import → captured outreach → sees a fit
score → receives the weekly list, with no founder assistance, and the metrics page shows every
step of their funnel.*

**M6+ (gated on data, not scheduled):** more connectors, MV3 extension, residue-pass conversion
to API calls, Gmail-OAuth decision gate, clubs, billing.

## 8. Instrumenting the 40% gate

Make the gate a query on day one, with the metric defined now so month three cannot be argued
with.

- **Activated** = completed onboarding AND ≥5 contacts (import or manual).
- **Capturing (weekly)** = ≥1 `touches` row in trailing 7 days, any source. `touches.source` is
  a required column precisely so this falls out of the core data model.
- **The gate: ≥40% of activated users are Capturing in a given week, sustained over the last 4
  weeks of month three.**
- **Leading indicators from week 1:** time-to-first-capture; capture mix (email vs manual — a
  heavily-manual mix means the BCC loop is failing while the thesis might still hold); D7/D28
  capturing retention by cohort; digest open→action rate.
- **Mechanism:** append-only `product_events` at feature code points (`signup`,
  `import_completed`, `capture_email_received`, `touch_logged{source}`, `score_viewed`,
  `digest_sent/opened`); one founder metrics page; automated Monday email — the founder already
  runs on weekly reports, so put the health check where he already looks.
- **Pre-committed decision rule:** if email-sourced capture is <20% of touches among engaged
  users by end of month two, the MV3 extension jumps the queue.

## 9. Testing posture

pytest from the first commit. The existing 214-test suite's *content* is the asset; its style is
not. Priority order:

1. Port the gmail_enrich ratchet and cadence regression tests **before** the code they guard —
   they encode real production bugs.
2. Golden-fixture determinism tests on the scoring engine.
3. Idempotency tests on capture ingest (same webhook twice → one event, one touch).
4. The class the old system never had: pytest-django route tests — thin everywhere except
   **tenant isolation, parametrized across every private-object endpoint** (user B requesting
   user A's contact/touch/score/event must 404).

No browser E2E in v1; one post-deploy smoke script. CI runs migrations against real Postgres.

## 10. Legal & privacy before a non-founder user

Required before M5 admits anyone:

- Plain-language privacy policy stating exactly what is captured (only mail the user
  BCCs/forwards; headers + body; purpose = their own CRM) and what is never done with it (no
  sale, no cross-user visibility of contacts/touches, aggregate-only firm bounce stats).
- ToS.
- **Self-serve full deletion** (cascade all private rows + raw artifacts; state the
  backup-expiry window) and **CSV export**. Both are trust features for this audience, not
  chores.
- Raw MIME retention capped at 30 days; extracted events are the user's CRM data and persist
  until deletion.
- TLS + encrypted-at-rest (managed PG default), unguessable capture slugs, no email bodies in
  logs. CAN-SPAM basics on the digest.
- Scraping posture per the brief's hiQ note: official APIs, central fetch, reasonable rates.
- Google API Services User Data Policy / Limited Use attestations explicitly deferred to the
  OAuth gate. GDPR out of v1 scope (US/HK audience) — noted, not solved.

## Solo-founder reality check

Realistic because of what it excludes: no queue infra, no SPA, three connector families at
launch, no extension, no OAuth, no billing, no clubs, LLM confined to one narrow classifier,
founder-supervised weekly residue pass.

The two places most likely to blow up on one person: **inbound-email edge cases** (M3 — hold the
line: deterministic extractors + needs_review, resist parsing cleverness) and **connector
breakage once real users watch** (mitigated by the ported verification layer and honest staleness
banners). Anything beyond this list in the next ten weeks is scope creep.

## Risk register

| # | Risk | Trigger | Mitigation |
|---|---|---|---|
| 1 | **Capture rate fails** — product degrades to a worse Trackr | Month-3 gate <40%; capture mix heavily manual | Instrumented day one (§8); import-first onboarding; same-session warmth feedback; mailto-BCC composes; pre-committed escalation to MV3 at <20% month-2; honest pivot conversation if even engaged users won't capture |
| 2 | **Gmail restricted-scope burden** (CASA cost/timeline unknown) | Any plan to ship `gmail_api` or an Add-on | Foreclosed from v1 by design: capture behind `CaptureProvider`; login OAuth never requests gmail scopes; explicit research-then-decide gate; marketing never promises "connect Gmail" early |
| 3 | **Inbound-mail loop silently fails** (spam-foldered confirmations, dropped webhooks, stripped BCCs) | Capture events missing vs user-reported sends; health strip stale | Onboarding test-message verification; capture-health UI; Postmark delivery logs; idempotent ingest tolerates redelivery |
| 4 | **Tenant-isolation bug** — privacy incident with email-derived data | Any cross-user read | Scoped-manager pattern; mandatory parametrized isolation tests per endpoint; user_id denormalized on every private row; RLS earmarked for v2 |
| 5 | **Solo capacity** — M3/M4 slip pushes the second-student moment past the season | Two consecutive DoD misses | The cut list below, in order; managed services everywhere; weekly scope review against DoDs |
| 6 | **Board/scraper fragility at product stakes** | Verification layer flags a staleness spike | Ported verify layer + staleness banners (honesty is the brand); connector error alerting; "report a date" community loop later |
| 7 | **Port regressions in the state machine/ratchet** | Ported tests diverge from old behavior | Tests ported before code; founder dogfood cutover in M2 exercises real data before any outside user |
| 8 | **Seed-data parochialism** (firms/taxonomy reflect one student's HK/US IB world) | First users outside that profile find gaps | Taxonomy moved to data; v1 audience deliberately founder-adjacent; directory-add path is founder-ops, not schema change |

## What I would cut if this slips

In order — each preserves the second-student end-to-end moment:

1. Connector breadth: ship Greenhouse + Lever only; Workday post-launch; everything else stays
   seed-data refreshed weekly by the founder's existing process.
2. Digest email → in-app weekly list only (drops outbound deliverability setup).
3. Firm fit score → contact warmth score only; firm view shows a plain aggregate.
4. CSV flexibility → one rigid template import.
5. Inbound reply forwarding → BCC-outbound + manual reply logging (the ratchet still advances;
   the brief's core mitigation survives).
6. LLM residue classifier → everything ambiguous goes to needs_review; fully deterministic v1.

**Never cut:** capture instrumentation, tenant-isolation tests, self-serve deletion, the free
public calendar.

---

## Critical files for implementation

- `campaign/src/campaign/pipeline.py` — the warmth/thread-state machine to port verbatim
  (TOCTOU-safe update at `88-101`)
- `campaign/dashboard/src/netdash/gmail_enrich.py` — apply layer + stage ratchet; its findings
  contract (`141-144`) is the template for the `InteractionEvent` interface
- `campaign/src/campaign/cadence.py` — `due_actions` decision tree powering the weekly list
- `recruiting-radar/dashboard/src/dashboard/sources.py` — the connector library to lift
- `campaign/src/campaign/db.py` — schema + upsert semantics to translate to Postgres
