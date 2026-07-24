# Existing System — Verified Architecture Map

*Source: direct source reads of `~/Claude/Projects/Recruitment Opportunities/`, 2026-07-23.*
*All claims cited to file:line. This supersedes assumptions in `product-brief.md`.*

Two sibling projects, wired together into one single-user product:

- **`campaign/`** — SQLite CRM, cadence engine, research/intel knowledge base. 2,749 LOC.
- **`campaign/dashboard/` (package `netdash`)** — CRM library + Gmail *apply* layer. 1,755 LOC.
  Its own UI was retired 2026-07-22; `app.py` is now a 36-line redirect shim to port 5057.
- **`recruiting-radar/dashboard/`** — the real Flask UI + all scraping. 7,445 LOC.
- **`recruiting-radar/handshake-scan/`** — separate Playwright project. 190 LOC.

**Naming trap:** the package under `campaign/dashboard/` is `netdash`, not `campaign`.

---

## 1. The Gmail situation — read this first

**There is no OAuth anywhere in either repository.** No Google API client library, no IMAP,
no POP. Confirmed by grep across both trees and both lockfiles.

`netdash/gmail_enrich.py` (623 lines) is explicitly *"a pure apply layer"* that
*"never talks to Gmail itself"* (`gmail_enrich.py:1-6`). It consumes a list of
already-produced JSON findings. The actual mailbox reading happens **outside both repos**,
in a scheduled Claude task using the Gmail MCP connector — referenced from six places in
the source but never defined in-tree.

So per-user Gmail access is **build-from-scratch**, not port. What *does* exist, and is
genuinely valuable, is everything downstream of the read:

- `apply_enrichment()` (`gmail_enrich.py:138-350`) — backfills discovered emails, archives
  bounced addresses (soft-delete, never hard), logs touches through the shared pipeline.
- A **stage ratchet**, not a seen/unseen flag: `reply_received(1) < chat_scheduled(2) < chat(3)`
  per Gmail thread (`_thread_stage_rank`, `353-376`). One thread legitimately progresses
  through all three stages without a new thread ID; a flat dedup check was a real production
  bug — *"a chat that actually happened was swallowed and reported as 'already logged, skipped'"*
  (`198-201`).
- `apply_usc_discovery()` (`437-533`) — inserts new contacts from mail-derived findings with
  exact-match dedup, and reports rather than silently forking when it matches an archived contact.
- `record_pattern_confidence()` (`544-623`) — feeds `email_pattern_stats`. Live: 31+ sends
  across 11 firms of real bounce/delivery history.
- 14 regression tests targeting exactly these edge cases.

---

## 2. Data model (`campaign/src/campaign/db.py:9-63`)

SQLite, schema applied idempotently on connect via `CREATE TABLE IF NOT EXISTS` plus
hand-rolled additive migrations guarded by `PRAGMA table_info`.

| Table | Columns |
|---|---|
| `contacts` | `id, name, firm, role, email, linkedin, source, warmth, thread_state, angle, notes, school_affiliation, gender, created, archived` |
| `touches` | `id, contact_id→contacts(id), ts, channel, kind, note` |
| `tasks` | `id, title, why, due, kind, firm, source_key, status, created` |
| `runs` | `id, ts, items, findings, changes, status, report, reason` |
| `email_pattern_stats` | `firm (PK), delivered_count, bounced_count, last_updated` |

**No uniqueness constraint on contacts.** Dedup is application-layer only, on the write path
(`cli.py:144-159`, `actions.py:204-211`), bypassable with `--force`. **Firms are not a table** —
`contacts.firm` holds a plain string id from `firms.yaml`, no referential integrity.

### The warmth state machine — the most reusable IP here

Two parallel states, table-driven via `TOUCH_TRANSITIONS` (`pipeline.py:14-30`):

- **`warmth`** — ordinal ratchet `cold(0) → replied(1) → chatted(2) → advocate(3)`. Only ever
  moves up.
- **`thread_state`** — `no_reply | replied | chat_scheduled | chat_done | advocate | quiet | parked`.

Touch kinds: `outreach, follow_up, reply_received, chat, chat_scheduled, thank_you, maintain, reping`.
Channels: `email, linkedin, coffee_chat, call, event, other`.

Transitions apply inside a single `UPDATE ... CASE` evaluated against live DB state, **not** an
in-Python snapshot — deliberately closing a TOCTOU race between a dashboard click and the Gmail
sync (`pipeline.py:60-101`). `advocate` is terminal; only an explicit `contact set` leaves it.

One canonical state machine (`pipeline.apply_touch()`), three front ends: CLI, web dashboard,
Gmail enrichment.

---

## 3. Scoring — the fit score does not exist

`confidence.py` scores **source-data trustworthiness**, never a person's odds at a firm.
Its own docstring: *"A forum URL can never be 'confirmed_official' no matter what the agent says"*
(`confidence.py:1-7`).

Fully deterministic, no LLM call. `domain_cap()` (`47-62`) caps confidence by URL host:
forum hosts (WSO, Reddit, Blind, Fishbowl, Discord, Quora, 1point3acres, ChaseDream,
CollegeConfidential) → `rumor`; the firm's own configured domain or any `.edu` →
`confirmed_official`; everything else → `reported`. `effective_confidence()` then clamps
whatever the research LLM *claimed* down to what the domain can support. **Code has final say
over the model** — a pattern worth carrying forward.

`status.py` is a read-only report, not a scorer.

---

## 4. Cadence engine (`cadence.py:40-186`)

Parameters live in `cadence.yaml` (9 lines, no code): `followup_after_business_days: 5`,
`park_after_business_days: 10`, `max_cold_touches: 2`, `thank_you_within_hours: 24`,
`advocate_touch_min_weeks: 4`, `advocate_touch_max_weeks: 6`, `pre_deadline_reping_days: 14`,
`stale_thread_days: 21`.

`due_actions()` evaluates a fixed 7-branch decision tree per contact, returning at most one
action + priority:

1. `chat_done` with no thank-you since the last chat → **thank_you** (scoped to latest chat only)
2. `chat_scheduled` stale >4 business days → **confirm_chat**
3. Warm contact at a firm whose confirmed `app_close` is within 14 days, **region-scoped**
   (an HK close never re-pings a US contact at the same firm) → **reping**, capped one per window
4. `parked`/`quiet` → skip
5. `advocate` idle ≥4 weeks → **maintain**
6. `cold`/`no_reply`: zero outbound → **first_outreach**; else **follow_up** at ≥5 business days;
   else **park** once 2 cold touches reached and idle ≥10 business days
7. Replied, idle ≥3 business days → **advance** (propose a chat)

Sorted by `(priority, firm tier, firm name)`.

`tasks.py:14-56` is the backward planner — fires **only** on `confirmed_official` changes
(rumor never spawns a task). `app_open` → advocates-in-place task 14d before; `app_close` →
re-ping 14d before + submit 5d before; `insight_deadline` → apply 7d before. A due-date shift
of ≤3 days updates in place rather than duplicating.

---

## 5. Scraping layer — the most portable asset

Deliberate two-tier hybrid. **Deterministic layer, zero LLM calls:**

| Provider | Mechanism |
|---|---|
| Workday | ~25 boards (Blackstone, MS, Wells Fargo, BlackRock, Barclays, DB, Citi, Nvidia…), `wday/cxs/.../jobs` JSON, paginated |
| Greenhouse | `boards-api.greenhouse.io` JSON |
| Lever | `api.lever.co` JSON |
| Oracle Recruiting Cloud | J.P. Morgan only, `recruitingCEJobRequisitions` REST |
| tal.net | BofA, Morgan Stanley — server-rendered HTML, regex-parsed |
| HSBC | its own `sitemap.xml` (career site is a JS shell) |
| Amazon / Tencent / ByteDance | each firm's public JSON search API |
| Ashby | normalizer + URL exist but **no firm wired to it** — dead capability |
| SmartRecruiters | **verification only**, no discovery board configured |

Plus Reddit RSS forum signal, InTrack HK HTML scrape, a job-count/page-hash change monitor,
and GitHub community internship-list scraping (`discovery.py`).

`prescan.py` (888 lines) is the cost-control brain: refreshes channels concurrently, computes
HOT/WARM/COLD heat per firm, then classifies every row as **RESOLVED** (deterministic layer
settled it) vs **RESIDUE** (needs an agent, tagged `needs-llm-portal`, `time-critical`,
`conflict`, `facts-missing`, `coverage-gap`). Only the residue reaches an LLM.

`scan_runner.py` launches headless `claude -p --output-format json --model sonnet` as a
**subprocess**, prompt read from the `recruiting-radar` skill file. That subprocess is the only
writer of `tracker.md`, and the piece that reads Gmail and handles bot-protected portals
(GS `higher.gs.com`, UBS Taleo, MBB).

`handshake-scan/` is a third paradigm: Playwright with a persistent manually-authenticated
Chromium profile. User completes SSO/Duo by hand once; expiry detected by looking for a password
input. Stores no password.

---

## 6. Stack and operational reality

- **Python 3.14.6**, `uv` 0.11.26. Cross-project deps are editable path installs.
- **Flask dev server** in both apps (`app.run(debug=False, use_reloader=False)`). No gunicorn/
  uwsgi/waitress. Port 5057 is the live UI; 5058 is the retired redirect shim.
- **214 tests, none using pytest** — plain stdlib asserts and `unittest.TestCase`, run as
  `python tests/x.py`. Coverage is on pure functions: parsing, ATS URL classification,
  disposition rules, the warmth ratchet, KB promotion. **Nothing tests the Flask routes**, and
  nothing exercises the `claude -p` subprocess end to end.
- **No auth of any kind.** Grep for session/login/password/current_user returns only Playwright's
  locator for *Handshake's* login page.
- **~15 hardcoded absolute paths** to `/Users/zhujimmy/Claude/Projects/...` across ≥8 modules.
- **Asymmetric persistence:** `campaign` has real SQLite. `recruiting-radar` has **no database** —
  its system of record is `tracker.md`, a Markdown pipe-table hand-parsed by regex, guarded by a
  single in-process `threading.Lock()`, plus ~12 flat JSON side-stores.

**Known inconsistency:** `network_link.py:9-11` claims the radar venv lacks the `campaign` package,
but `recruiting-radar/dashboard/pyproject.toml:14-15,22-23` declares `campaign` and `netdash` as
editable deps. Unresolved; both are in the repo as-is.

**`coverage.py` is a naming coincidence.** It is the firm×track×region gap engine
(`live | watching | off-cycle | gap`), unrelated to the Coverage product.

---

## 7. Reuse assessment for multi-tenant

### Port largely as-is — tenant-agnostic already
- `pipeline.py` warmth/thread-state ratchet, `confidence.py` domain caps, `kb.py` corroboration
  and promotion rules. Pure functions over rows/dicts. Add a tenant id and they transfer.
- **The entire scraping layer.** A JPMorgan Oracle board is identical for every user — this is the
  strongest candidate for a centrally-run shared connector service, which matches the intended
  one-fetch-serves-all-users design.
- `prescan.py`'s RESOLVED/RESIDUE pattern as a generic LLM cost-control mechanism.
- **Firm facts belong in a shared cross-tenant KB.** `kb/timeline_*.yaml` and `kb/firm_intel/*.md`
  are facts about firms, not about a user — every student targeting Goldman benefits from the same
  confirmed date. Do not duplicate per tenant.

### Needs real rework
- No auth, anywhere. Both apps are one process serving one person.
- **`recruiting-radar`'s persistence is the single biggest rewrite item.** `tracker.md` + a dozen
  JSON side-stores + one in-process file lock + hardcoded paths has no multi-tenant story. It needs
  to *become* a database, not have its file format ported.
- `config.root()` walks up from cwd looking for `profile.yaml` — assumes one repo = one user.
- The `claude -p` subprocess pattern shells out to a locally installed, already-logged-in CLI on the
  same machine. Under concurrent load it needs to become API calls with a real job queue.
- Gmail: from scratch (see §1).

### Must become per-user data
- `profile.yaml` — name, school, class year, cycle, outreach assets, weekly hours, personal calendar.
- `firms.yaml` / `targets.yaml` — split into a **shared master firm directory** plus a thin per-user
  "which firms, at what tier" join table.
- `prescan.py`'s `_IB_ST_FIRM_CATEGORY` taxonomy and the HK+US, Class-of-2028/2029 scope. The
  *mechanism* (heat tiers, disposition queue) is reusable; the taxonomy is one person's targets.
- `contacts`/`touches` are personal relationship data — strictly tenant-scoped, never cross-visible,
  unlike the firm KB above.
