# Coverage Gauntlet — durable state

This file is the loop's memory. Every agent in a gauntlet round reads a
summary of it before working; the round's final Record step rewrites it.
It exists because eleven rounds of hand-copying context between one-off
scripts is exactly the "context rot" failure a loop's state is supposed to
prevent. Keep it distilled: this is a working card, not a changelog — git
history holds the full story.

## Round counter

next_round: 12
(Rounds 1-10 were data/content/layout sweeps; round 11 was the dedicated
visual-design round. All ran 2026-08-14/15.)

## Objective (standing)

Coverage tells students the truth: every deadline, status, label, and fact
chip on the live product matches what the firm's own page says today, every
word earns its place, and the layout/motion craft holds the bar set by the
benchmark products (Linear, Ramp, Mercury — craft standards, not their
look; Coverage's paper-ledger identity is deliberate and stays).

## Metric (standing)

A finding counts only with a repro a stranger can run (a query, a bounded
fetch, or a URL plus exactly what renders there) and survives a
fresh-context skeptic. A fix counts only when the recheck reproduces the
original defect's absence LIVE (browser-measured for UI, live DB/source
re-run for data), not when its tests pass.

## Boundaries (standing)

- Live DB is READ-ONLY to auditors/skeptics. Data corrections ship as
  dry-run-by-default management commands; --apply runs need the founder or
  the main session, never an agent's own initiative.
- NEVER the generic WebFetch tool for external pages — it hung two agents
  35-46 min each on jobs.ubs.com (Taleo). Always
  coverage_connectors.http.fetch_text/fetch_json (15s timeout, 2 retries)
  via python -c. Unfetchable = unverifiable, not a finding.
- Politeness: 1.5s between external fetches, stay within stated budgets.
- Bot-walled, do not re-report: Evercore/Jefferies/Morgan Stanley/Nomura
  (tal.net tenants); unreachable: Citadel/DE Shaw/Two Sigma/Balyasny.
- No deploys, no spending, no credentials, no auto-run of paid AI commands
  (extract_deadlines_ai costs money per call once a key exists).
- Builders work in isolated worktrees, merge agent brings branches to main
  sequentially, full suite gates the merge. Isolated test DBs are suffixed
  per area (coverage_audit_tmp_test_<area>).
- A worktree branches from a possibly-stale base: before skipping a
  finding as "does not reproduce", verify against CURRENT MAIN — round
  11's contrast builder skipped a real, confirmed dark-mode contrast bug
  this exact way and it had to be re-fixed by hand afterward.
- A fix touching a cached raw field isn't done until the affected live
  rows get a SCOPED backfill (or the skip is disclosed loudly).

## Live carve-outs (files other sessions own RIGHT NOW — do not touch)

- coverage_connectors/coverage_connectors/talnet.py (Event Date leaking
  into deadline_dates — separate session, unlanded)
- coverage_web/crm/views.py _contact_card day-counting (third instance of
  the timedelta-floor vs calendar-diff bug — separate session, unlanded)
- coverage_web/directory/applications.py, coverage_web/directory/dupes.py,
  coverage_web/directory/management/commands/dedupe_opportunities.py
  (uncommitted edits sitting on main from a parallel session)
Remove entries from this list when their fixes appear in git log.

## Fixed mechanisms — do not re-report

Data layer (rounds 1-10): deadline-refresh gaps incl. PAST_DEADLINE stale
re-check now on the real 6h cron cadence; reverify.py now persists revised
deadlines (was frozen-at-first-capture for every provider);
workday.verify() now returns BMO's stated deadline and classify_url caps
job_path to two segments (trailing /apply made verify 422); Phenom's
frozen list-payload snippet no longer outranks the live Workday API;
enrich_postings no longer mistakes its own cached copy (or extract_facts'
derived dict) for a board payload, and has --ids for scoped backfills;
Goldman GraphQL, JPM Oracle requisition-search, KKR + Jane Street
Greenhouse custom domains, HSBC sitemap title/location (og:title,
plain-text Location: label over truncated microdata, postal-code strip)
all wired; mckinsey fetch()/verify() null-docs crash; identity-duplicate
folding (tal.net two-pool requisitions) wired into feed, My Applications,
calendar, and firm pages; bag-of-words title key in the report-only
dedupe listing; classification: study-stage speaker-bio gate,
duration range low bound kept, "application window is open until" and
similar phrasings; content: raw enums (bucket slugs, app_close),
smart_title fixes (E*TRADE, USC, ordinals, colon restarts), location joins
and capitalization, Fit Score self-contradictions, firm-detail campus
scoping with folded-consistent counts, stat strip real firm count,
"No firm listed" styling, debug prefixes stripped from history, calendar
plurals, cadence-diagram defaults read from CADENCE_DEFAULTS.

Design (round 11): btn-danger:hover tokenized via --danger-ink (was
1.9:1 in dark), --line-strong-as-text middot separators -> --ink-3 (three
instances), drawer close is a stroke icon, small SVGs converge on
stroke-width 1.5, four hardcoded px sizes -> tokens, settings stagger
indices sequential, network action-queue and opportunities firm columns
use shared kin-reveal, net-mini tap target 24px.

## Refuted / dead ends — do not re-litigate

- Word-order dedup folding on the live feed: NO safe corroborating signal
  exists (no group shares req ids; Brookfield's anagram pair is two real
  jobs). Report-only listing keeps the bag-of-words key. Dead end.
- Static (non-pulsing) urgent-deadline dot: deliberate, documented in CSS.
- Firm-count differences across feed/firm-detail/network: deliberate
  scope differences, now labeled on-page. Folding firm-detail's count
  would hide ~994 experienced rows across 56 firms.
- My Applications lenses are cross-sections, not a partition; the page
  says so. Not a bug.
- "Rolling" chip coexisting with a stated deadline: the quoted evidence
  names the date; correct as designed.
- Stripping the literal "(OVERDUE)" token: state is carried as priority,
  which routes the card to the critical lane. Not a bug.
- Insight-event "Event Date" rendered as a visible quotation: deliberate.
- Round 11 refuted: locations-summary truncation, stat-number display
  sizes (deliberate display-scale values), cadence-rail 9px marker
  (documented design), marketing mobile nav hiding links.

## Open leads (verified-adjacent, uncapped or low; fair game next round)

- Landing firm-avatar hue badge (round 11, unverified, low).
- Avatar fallback font-size exact-micro match (round 11, unverified, low).
- Calendar strip counts an applications-OPEN milestone as a deadline and
  reads "0 events" on a month with an Insight Forum entry (round 9, low).
- Cycle-date confidence stated twice ("RUMORED · CONFIDENCE MEDIUM
  (0.6)") — raw model float on the page (round 9, low).
- Calendar "Remove" control is a 45x14 tap target vs the 24px minimum the
  rest of the product now holds (round 9, low).
- HSBC Sheffield insight programme renders twice on /firms/hsbc/ (real
  fold-eligible pair on a page that deliberately doesn't fold; a
  provider_identity-scoped fold there may be safe — investigate, don't
  assume).

## Failed approaches (do not repeat the same move on the same problem)

- Fixing WebFetch hangs per-critic instead of in shared rules (recurred
  immediately in a different critic).
- Blanket word-order folding on the live feed (hid a real Brookfield
  role; reverted same day).
- Counting scope-sentence totals on the raw queryset while rendering
  folded rows (numbers contradicted each other on the same page).
- Declaring a fix done from its diff/tests alone: three separate rounds
  found code-correct fixes invisible live because cached rows were never
  backfilled.

## Design benchmark standards (for the design lens)

Extracted from Linear/Ramp/Mercury and confirmed against Coverage's own
system: one accent doing real work; every spacing/size value on the token
scale (no orphan px); motion restrained, meaningful, uniformly staggered,
reduced-motion respected; contrast measured (4.5:1 text) on the rendered
page in BOTH themes including composited tints; tap targets 24px+;
icons one stroke-width (1.5), stroked in currentColor. Coverage's
paper-ledger identity, Fraunces/Instrument Sans/Spline Sans Mono stack,
and pill-chip grammar are settled identity — audit execution against the
standards, never propose a rebrand.
