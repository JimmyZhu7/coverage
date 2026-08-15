# Coverage Gauntlet — durable state

This file is the loop's memory. Every agent in a gauntlet round reads a
summary of it before working; the round's final Record step rewrites it.
It exists because eleven rounds of hand-copying context between one-off
scripts is exactly the "context rot" failure a loop's state is supposed to
prevent. Keep it distilled: this is a working card, not a changelog — git
history holds the full story.

## Round counter

next_round: 14
(Rounds 1-10 were data/content/layout sweeps; round 11 was the dedicated
visual-design round; round 12 mixed data/classification/content/design
fixes and added the loop's first integration-critic pass; round 13 mixed
deadline/dedup/status/content/design fixes. All ran 2026-08-14/15/16.)

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
- A branch that touches a carved-out file cannot be merged, full stop —
  round 12 lost two real fixes (dupes.py) to this; re-attempt once the
  carve-out clears rather than forcing the merge.

## Live carve-outs (files other sessions own RIGHT NOW — do not touch)

- coverage_web/directory/applications.py, coverage_web/directory/dupes.py,
  coverage_web/directory/management/commands/dedupe_opportunities.py
  (uncommitted edits sitting on main from a parallel session — still
  present as of round 13's close, unchanged since round 12). This is
  still what blocks worktree-wf_b418567e-46b-25's two dedup fixes
  (f9d148e, 433497c) from merging — confirmed round 13 too
  (`git merge-base --is-ancestor` on both: neither is an ancestor of
  HEAD); merge that branch once this carve-out clears.
Remove entries from this list when their fixes appear in git log (or the
uncommitted edits are simply gone from `git status`).

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

Round 12: reverify.py's staleness cutoff no longer starves on
last_checked (code fix on main; migration + live backfill still pending,
see open leads). Classification: extract_study_stage / _fact_chips no
longer collapse an OR-eligibility disjunction ("current student OR recent
graduate") into a single wall chip — both eligibility groups render;
CONFIRMED backfilled live (extract_facts, 31 rows, run directly outside
the loop after its own recheck showed the code fix hadn't propagated to
cached raw['facts'] — see "Loop self-corrections" below for why the loop
missed this one itself). Content: firm-page Cycle Dates timeline no longer shows Applications
Close before Open on hsbc/ubs/ms/jpm; Today funnel labelled
"Applied › Interviewing" instead of raw DB stage keys; firm-detail
eyebrow prints region/track labels instead of raw slugs (wording only —
see open lead on the underlying data); firm rows and feed cards agree on
empty-location wording; calendar month tally no longer counts an
applications-open milestone as a deadline (calendar only — see open lead
on Today's rail). Design: feed card's three per-card controls reach the
44px touch floor (card only — see open lead on the rest of the page).

Round 13: reverify gains a scoped `--ids` backfill flag and now keys
staleness on deadline_checked_at (migration applies cleanly, dry-run vs.
BMO's live Workday endpoint confirmed correct, 9/9 tests pass, merged to
main) — but the actual write-mode run for opportunity #9446 (or any other
stale row) was never executed from this worktree, deliberately, because
its workday.py connector diverges 269 lines from main's; see open leads
for the live-data gap this leaves. Dedup: Save/Saved button now targets
`closest .track` instead of an id selector, so the duplicate
`id="track-{{r.id}}"` shared between a role's "Picked for you" and
firm-column copies (real, confirmed) can no longer make a click swap the
wrong card's DOM; the duplicate id itself is kept, now documented as an
inert labeling hook only. Status: the role-description drawer
(/opportunities/<id>/read/) now checks Opportunity.status — a
status="closed" row gets an honest "This posting is closed" caution and a
demoted "View the closed posting" link instead of the live-open caution
and CTA. Content: sitemap connector splits a recognized trailing US
state+ZIP or UK postcode off HSBC titles into location (narrow pattern,
"never guess" rule intact for anything ambiguous — code fix merged,
covers 4 of 23 empty-location HSBC rows, dry-run confirmed correct, see
open leads for the pending --apply); My Applications' "Rolling" lens no
longer claims undated roles are "reviewed as they arrive" (matches the
feed's own retraction); firm page's "Warmest here:" line no longer
renders when it just restates the contact list's own first row; Settings'
"pe" track label now matches the Opportunities filter's "Private Equity /
Credit" (Corporate Strategy's separate structural gap is unrelated, still
open). Design: the JS-injected custom-select caret (.csel-caret, every
progressively-enhanced <select> in the product) converges on
stroke-width 1.5, closing the one outlier round 11's icon sweep missed
because it's built via string concatenation, not a static inline <svg>.

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
- Round 12: "SIG firm label is a mismatched employer" (River's Edge
  Insurance Solutions LLC IS a Susquehanna/SIG business per its own
  disclosure page; the counter-statistic offered as evidence was false —
  refuted on both prongs).
- Round 12 design decision: closing Oracle postings on search-absence
  alone was considered and rejected — this exact connector already
  caused a false closure trusting absence (JPM 4731, see oracle.py
  docstring) and its keyword-search endpoint can't distinguish "gone"
  from "malformed response" at that call site. The scoped remedy is
  surfacing the uncertainty (see oracle-jpm-opp6788 in open leads, whose
  UI fix landed but doesn't fire yet), not a stronger auto-close signal.

## Open leads (verified-adjacent, uncapped or low; fair game next round)

- oracle-jpm-opp6788-dead-behind-open, still broken (high): the
  "unconfirmed" mark (commit cbecbf1, on main) doesn't fire — live GET
  /opportunities/6788/read/ renders the apply link with zero caution, and
  no row in the live dataset appears to trigger it either. Needs a
  live-data audit of `_unconfirmed_note()`'s last_checked>last_verified
  condition, not another template pass.
- gs-cohort-fold-hides-2027-postings, unmerged (high): fix f9d148e lives
  only on worktree-wf_b418567e-46b-25, blocked from merging by the
  dupes.py carve-out. Live repro still holds: /firms/gs/?role=all shows 0
  hits for 170880_GS_CAMPUS (2027 cohort).
- sig-sponsorship-fact-lost-in-fold-tiebreak, unmerged (high): fix
  433497c, same branch/blocker as above. Live repro still holds:
  /firms/sig/?role=all shows 0 hits for jobs/11084 (sponsorship=yes).
- reverify-write-mode-backfill-pending (medium; was "reverify's
  full-board backfill still pending", now narrower): the mechanism is
  code-complete (`manage.py reverify --ids <id>` merged, dry-run vs.
  BMO's live Workday endpoint confirmed correct, migration graph
  reconciled with main) but no write-mode run has actually happened —
  Opportunity #9446 (BMO/Associate/Mississauga) still shows the header
  badge vs. body-text self-contradiction live on
  /opportunities/9446/read/ (DB deadline stuck at 2026-07-24 vs. the
  firm's live-stated 2026-09-04), and deadline_checked_at is still NULL
  catalog-wide since it didn't exist before round 12. Run
  `manage.py reverify --ids 9446 --apply` (and eventually a deliberate
  full-board pass) from a checkout with current connector code — not
  from a stale worktree, per this round's disclosed skip.
- today-deadlines-rail-counts-openings (high): still unfixed — this
  round's builder confirmed the worktree predates the feature entirely
  (crm/today.py, crm/calendar_views.py, crm/utils.py, and the rail's
  _cockpit.html markup don't exist there) and instead worked out the
  exact fix for direct application to main: move _OPENING_EVENTS /
  _firm_date_kind() into crm/utils.py so today.py and calendar_views.py
  share one definition; add "kind" to _next_deadlines' row dict and gate
  "urgent" on kind == "deadline" so an opening can't earn the red
  is-urgent treatment; add an `is-opening` dot class in _cockpit.html and
  a matching `.activity-dot.is-opening { background: var(--ok) }` rule in
  _styles.html.
- firm-eyebrow-two-market-seed (high): e8a8616 relabelled the slugs but
  Firm.regions/tracks is still a us/hk-only seed while the same firm
  pages list roles in London/Paris/Tokyo/Singapore/The Hague — the
  eyebrow's underlying claim, not just its wording, is wrong.
- contact-history-manual-override-debug-note (medium): confirmed live —
  manual-override history entries still leak an internal debug note.
  Round 1-10's fix only stripped the raw `column=value` audit prefix
  (the "worse half"); a follow-on rewrite (scrub_manual_override_notes,
  commit e97d7ae, pre-existing on main) was proposed for the remaining
  jargon-laden notes but was never wired in anywhere live — and per this
  round's confirmation, would not actually fix the problem even if it
  were.
- settings-corp-strat-track-structurally-unreachable (medium; split out
  of the now-fixed settings-track-vocabulary-second-copy): "Corporate
  Strategy" isn't just mislabeled, it's unreachable under any query — all
  9 firms tagged tracks=['corp-strat'] have zero live Opportunity rows,
  so the (sound) hide-zero-count facet rule permanently hides it. Data-
  coverage gap (no live postings sourced for those 9 firms under this
  track), not a content fix — needs real connector coverage, not a copy
  change.
- network-open-count-drifts-from-opportunities-and-firm-detail (medium,
  integration finding): Network's per-firm "OPEN" role count silently
  disagrees with the same firm's count on the Opportunities firm-card
  carousel and on the firm's own detail page, for several firms at once,
  live, same session — while other firms agree exactly across all three,
  ruling out a uniform intentional scope difference.
- no-deadline-three-phrasings (medium): the null-deadline rename to
  "No date posted" landed in the feed template only; the shared helper
  other surfaces call still returns the old string, so one role reads
  three different ways across surfaces.
- touch-floor-stopped-at-the-card (medium): round 12's 44px fix reached
  only the role card; Opportunities' filter console and escape-hatch
  links on the same page are still 14-34px on a coarse pointer.
- eligibility-lens-dies-at-the-seam (medium): the "Your year" eligibility
  verdict only renders on the Opportunities feed; firm pages and My
  Applications show the identical postings unlabelled.
- region-facet-count-overstates-results (medium): Opportunities region
  facet counts are computed pre-fold while the headline count is
  post-fold, so selecting a region under-delivers vs. its own count.
- push-toggle-13px-target (medium): Notifications' "Deadline Alerts / On
  This Device" row has no `for=` wiring to its label, unlike "Weekly
  Email Digest" right above it (301x24 hit area) — effectively untappable
  by its visible label.
- hsbc-sitemap-title-location (medium, narrowed): code fix merged this
  round (_split_trailing_postal_code, US state+ZIP or UK postcode only,
  "never guess" rule intact for anything ambiguous) and a dry-run backfill
  command confirms it correctly covers 4 of the 23 empty-location HSBC
  rows (ids 17423, 4743, 1617, 1618) — --apply not yet run. The remaining
  ~19 rows are genuinely ambiguous slugs (bare numeric codes, truncated
  fragments) and stay empty by design, not a bug.
- landing-monogram-contrast (rounds 11+12, low): BlackRock's "BL" badge
  measures 4.32:1 at 11px bold against the product's 4.5:1 floor — a
  fixed-lightness hue picker is the mechanism.
- Avatar fallback font-size exact-micro match (round 11, low).
- cycle-date-confidence-stated-twice (rounds 9+12, low): the rumored
  cycle-date pill states confidence as both a word and the raw internal
  float, directly under a lede that already promises confidence shown.
- Calendar "Remove" control is a 45x14 tap target vs. the 24px minimum
  the rest of the product now holds (round 9, low).
- HSBC Sheffield insight programme renders twice on /firms/hsbc/ (round
  9, low) — real fold-eligible pair on a page that deliberately doesn't
  fold; a provider_identity-scoped fold there may be safe — investigate,
  don't assume.
- transcript-phrase-is-application-form-chrome (low): the "Transcript"
  fact chip's hover-evidence phrase is sourced from Greenhouse
  application-form field labels, not the posting's own prose.
- stale-display-type-tokens (low): --fs-hero (34px) and its top-of-scale
  neighbor are annotated for surfaces they don't render on; zero live
  usages.
- nav-peephole-on-phone (low): the primary nav on phone width collapses
  to a 174px scroller holding 538px of links — four of five destinations
  sit off-screen and the visible edge clips a label mid-word.

## Failed approaches (do not repeat the same move on the same problem)

- Fixing WebFetch hangs per-critic instead of in shared rules (recurred
  immediately in a different critic).
- Blanket word-order folding on the live feed (hid a real Brookfield
  role; reverted same day).
- Counting scope-sentence totals on the raw queryset while rendering
  folded rows (numbers contradicted each other on the same page).
- Declaring a fix done from its diff/tests alone: four separate rounds
  (most recently round 12's oracle-jpm-opp6788 mark, which landed,
  passed its tests, and still doesn't fire on any live row) found
  code-correct fixes invisible or inert live. Always live-recheck before
  filing as fixed.
- Trusting a builder's own `fixed[].key` verbatim when writing recheck/
  Record bookkeeping: round 12's classification builder reported
  `{"key": "1", ...}` instead of the finding's real key
  (study-stage-or-disjunction-mislabeled-as-wall). The recheck still
  caught it live (still_reproduces: true), but the mismatched key meant
  Record's own key-matching missed it — the main session had to notice
  the gap by hand, re-verify (facts.py's fix WAS correct; only the
  cached raw['facts']['study'] on 193 rows was stale), and backfill via
  extract_facts directly. Fixed for next round: builders must echo the
  ORIGINAL finding key verbatim in every fixed[]/skipped[] entry — do
  not paraphrase or renumber it. If the Record step ever finds a
  recheck entry whose key doesn't match any confirmed finding, treat
  that as its own high-severity open lead, not a row to drop silently.
  Recurred in round 13 in a milder form: the recheck key
  "reverify-ids-scoped-backfill-mechanism" (an internal mechanism name
  cited inside a fix-skip disclosure) didn't match the confirmed
  finding's own key (bmo-associate-9446-deadline-drift-42-days) — this
  time traceable because the disclosure explicitly cross-referenced it
  ("see fixed[] key reverify-ids-scoped-backfill-mechanism"), but Record
  still had to do that tracing by hand rather than a straight key match.
  Same rule applies going forward: never assume an unmatched recheck key
  means nothing to record.

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
