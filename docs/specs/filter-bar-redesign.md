# Opportunities filter bar — redesign spec

*Architected 2026-07-30 (Fable). Implementation spec; build top-to-bottom from §5.*

## 1. What's wrong today

Six controls, equal weight: Role Type, Programme Year, Region, Track, Companies, Search.

- **Role Type is three questions in one `<select>`.** "Which campus role?" (insight 46 /
  internship 639 / entry-level 201) is a facet. "Other / Experienced (3,456)" is 80% of the
  corpus and 0% of the audience — a *mode*, not an option. "Everything We Scraped (4,342)" is
  an escape hatch. A dropdown gives all six equal billing and hides the page's most important
  scoping decision behind a closed control.
- **The default's most important fact is buried.** The "3,456 non-campus roles hidden" line
  renders at the *bottom* of `_results.html`, below 56 firm cards.
- **Region silently drops a third of the inventory.** 297 of 886 campus roles have no region.
  Pick "Hong Kong" and those 297 vanish with no trace.
- **Counts are inconsistent and go stale.** Only 2 of 6 controls show counts. Worse: the bar
  sits *outside* `#cov-results`, so after any htmx change the counts freeze at page-load
  values. `views.py` carefully cross-filters facets per request, but those honest numbers only
  reach the user on a full page load. This is an over-claiming bug of exactly the class this
  codebase keeps fixing.
- **Deadline urgency is nearly absent** (22 of 886 dated). Nothing in the bar may be built on it.

Ground truth: 4,342 open · other 3,456 / internship 639 / entry_level 201 / insight 46 ·
campus 886 · campus with deadline 22 · campus with cohort 362 · campus region:
blank 297 | eu 230 | us 170 | sg 72 | hk 71 | cn 30 | jp 16.

## 2. Research findings

**The Trackr** — programme type is **segmented tabs**, not a dropdown. Region is
pre-partitioned navigation (separate UK/US/EU trackers), not a filter. Cycle year is the
tracker's identity. Few tiny controls otherwise; sponsorship is a first-class checkbox.
**No experienced roles exist anywhere in the product. No counts anywhere.**

**OffCycle** — corpus scoped before filtering exists; their equivalent of our 3,456 simply
isn't in the product. Header stat strip + freshness stamp. One row of dropdown pills with
**per-option counts inside every dropdown** (Levels: Internship 2303, Graduate 381…). Subset
signalled as "X of Y positions". "Internships only" — the commonest cut — is promoted to a
top-level toggle.

**Simplify** — popover filter pills showing *active-selection* counts ("Job Type (2)"), not
result counts. Job Type has **Internship + Full-Time pre-checked by default** for the student
flow: the irrelevant majority is handled by defaulting the filter on. Progressive disclosure
via "More filters (3)".

**Built In** — dropdown pills, no counts, everything collapses behind one "All Filters" at
narrow widths.

**Handshake** (docs) — quick job-type buttons at top level; everything else behind "All
Filters", including **Work Authorization**. Student-specific dimension is eligibility
matching.

**Otta / WTTJ** — the anti-filter pole: onboarding preferences → curated matches. Coverage
already has this organ (the "Picked for you" bar) and it correctly lives outside the filters.

**RecruitU — UNVERIFIED.** App is login-gated; marketing pages only. Their events carry
class-year targeting, confirming class year matters to this audience, but no claim about
their filter UI should rest on this.

Conclusions: (1) the two products built for *this* audience put programme type at the top
level; generalist boards bury experience level because for them it's one facet of dozens —
for a campus product it's *the* facet. (2) Specialists **exclude** the irrelevant majority
from the corpus rather than offering it as a sibling option. (3) There is no universal count
convention — which makes live per-option counts a differentiator, not a violation. (4) Every
student product defaults to student roles and signals the subset. (5) 4–6 visible controls,
rest behind "More filters". (6) Dimensions we don't model: visa sponsorship, eligibility
matching, CV requirements.

## 3. Principles

1. **Scope is not a filter.** Campus-vs-everything is a mode; the three buckets are a facet.
2. **Counts are a promise:** a number means "pick this and you'll see exactly this many, under
   your other filters". Show them where we can keep that promise, never where we can't, and
   never let them go stale.
3. **Missing data is an option, not an omission.**
4. **The bar states what it excludes, in its own voice, without paywall theatre.**

## 4. The new bar

```
ROLE TYPE  (segmented, full row)
[ All Campus (886) ][ Insight (46) ][ Internship (639) ][ Entry-Level (201) ]

PROGRAMME YEAR   REGION          TRACK          COMPANIES   SEARCH
[Any Year (886)] [Any Region ▾]  [Any Track ▾]  [All ▾]     [______]   Clear
 intake hint

Showing campus roles only · 3,456 experienced roles hidden · Show everything
```

Row 1 answers "what is this page"; row 2 narrows it. The subset sentence moves from below 56
firm cards to the top of `#cov-results`.

### A. Role Type — segmented control

Replace `<select name="role">` with a radio group styled as pills: `<fieldset>` +
visually-hidden `<input type="radio" name="role">` + `<label>`. Values unchanged (`""`,
`insight`, `internship`, `entry_level`). Real form inputs with the same name ⇒ htmx `change`
submission and URL round-tripping work with **zero new JS**, identical serialization.

- `All Campus (886)` = value `""`, default checked.
- Reuse existing `role-*` colour families for the **selected** state only; unselected stay
  `--surface`/`--line`. Selected also gets weight + focus ring — never colour alone.
- **`other` and `all` are REMOVED from the segmented control.** Reachable only via the subset
  sentence's link and `?role=` deep links.

**Conditional fifth segment (deep-link honesty).** When the request carries `role=all` or
`role=other`, render a muted fifth segment (`role-other` grey), checked, labelled
`Everything (4,342)` / `Other / Experienced (3,456)`. This solves two things: the bar honestly
represents its state, **and** the form still serializes `role` when the user next changes
Region or Search. With no checked radio the next htmx GET would silently reset the mode to
campus — a real bug a naive build ships.

### B. Who earns a place

| Control | Verdict | Rationale |
|---|---|---|
| Role Type | **Promote** to row 1, segmented | The page's scope decision. |
| Programme Year | Keep, row 2, hint intact | Cycle is core to the audience. NOT promoted: 59% unstated, so a year tab bar would overstate the data. |
| Region | Keep, row 2, **+counts, +unstated option** | Useful (589/886) but must stop hiding the 297. |
| Track | Keep, row 2, **+counts** | Distinct dimension (firm vertical vs role kind) — don't re-merge. |
| Companies | Keep, row 2 | The CRM tie-in. Existing "N selected" stays. |
| Search | Keep, rightmost | High value over 886 scraped titles. Open-ended ⇒ no counts. |
| Provider (`?provider=`) | **Stays URL-only** | No student thinks in ATS providers. |
| Sort control | **Do not add** | Would order 22 of 886 rows and imply deadline coverage we lack. |

**Progressive disclosure:** ≥768px everything visible. Below 768px row 2 (except Search)
collapses behind one `Filters (n)` disclosure, n = active row-2 filters. Search and the
segmented control never hide.

### C. Counts — the consistency rule

> **Every closed-vocabulary control shows a live per-option count; open-ended and identity
> controls show none.**

- **Counts:** Role segments, Programme Year, **Region (new)**, **Track (new)**. Computed in
  the pass `_facets` already makes. Must obey the existing cross-filter posture: computed
  against every *other* active filter, never their own. Track counts may sum above the total
  (multi-track firms) — same documented posture as `_year_facet`'s overlap comment.
- **No counts:** Search (open text), Companies button (identity).
- **Staleness fix (REQUIRED).** Each count renders in `<span id="cnt-…">`; `_results.html`
  appends matching `<span hx-swap-oob="innerHTML" id="cnt-…">` with fresh numbers. OOB-swap
  **bare spans only** — never inputs or the form — so focus and select state survive while the
  numbers stay true. This is the difference between "counts that don't lie" being a code
  comment and being a product behaviour.

### D. Honesty rules — what each control must NOT imply

1. **Region must not imply completeness.** Add option `Other / Unstated (297)` (value `none`
   → `region=""`). Label it "Other / Unstated" because blank conflates "couldn't parse" with
   "market we don't track" (Sydney, Mumbai) and must not pretend they're distinguishable.
   When a concrete region is selected, `_results.html` renders: "297 campus roles have no
   tracked region and are hidden by this filter. Show them." — querystring built from the
   **live request** (the bare-`?` bug was fixed once; don't re-ship it). "Any Region" keeps
   meaning everything including unstated.
2. **Nothing in the bar may be built on deadlines.** 22 of 886. No "Closing soon" tab, toggle
   or sort. Deadline urgency stays on the per-card fuse and the stat strip.
3. **Programme Year hint ships verbatim** at every breakpoint including inside the mobile
   disclosure: "Intake year in the posting. Not a graduation year." Years render bare — never
   "Class of YYYY" (reserved for the stated-only `class-chip`).
4. **The everything mode must not pose as curation.** Plain words. No "premium", no lock
   icons, no "unlock". It's a link, it's free, it's just noisier data.
5. **Counts are live or absent.** Anything not covered by the OOB mechanism must not render.
6. **Adjacent defect, flagged NOT fixed here:** the stat strip's "864 Fresh (10d)" overstates
   freshness after bulk imports — same measurement class as the fixed "New" badge bug. Out of
   scope; do not let it get "fixed" by renaming things around it.

### E. Default state

Default stays `role=""` → 886. The subset sentence **moves to the top** of `_results.html`
(above the stat strip), quiet styling (`--ink-3`, `--fs-s`): "Showing campus roles only ·
3,456 experienced roles hidden · Show everything." It must stay *inside* `#cov-results` so its
number refreshes with filters. Delete the bottom copy — said once, where it's read before
scrolling.

### F. Responsive

- **1440 / 1024:** two rows as drawn. Row 1 `flex-wrap: nowrap` (4 pills ≈ 620px). Row 2 keeps
  current pill styling, `flex-wrap: wrap`, `align-items: flex-start` (the flex-start comment
  about the Year hint is load-bearing).
- **768:** row 1 unchanged; row 2 wraps naturally.
- **375 (≤640px):** segmented control becomes a **2×2 grid** — wrapping over horizontal
  scroll, because a scrolled-away segment hides the counts that are the point, and this file's
  firm-column lesson says hidden-by-scroll needs affordances we'd have to build. Conditional
  fifth segment spans full width. Search full-width. Year/Region/Track/Companies collapse into
  `<details class="filters-more">` whose `<summary>` reads `Filters · 2`. Controls stay
  **inside the form** — a `<details>` inside a form doesn't affect serialization and
  collapsed inputs still submit (this is why `<details>`, not conditional rendering). Server-
  render `open` when any of the four is non-default so a deep-linked filter is never invisible.
  The Year hint travels with its control into the disclosure.

### G. Accessibility

- `<fieldset class="seg">` + `<legend>Role type</legend>` styled like the current uppercase
  micro-labels — do **not** visually-hide it; it replaces the "Role Type" caption.
- Radios visually hidden with the clip-rect pattern, **not `display:none`** (which removes
  them from keyboard order). Native radio semantics give arrow-key movement and a single Tab
  stop for free — no ARIA re-implementation, no JS.
- `:focus-visible` on `input:focus-visible + label`: `outline: 2px solid var(--accent);
  outline-offset: 2px`.
- Counts are plain text inside the label ("Internship (639)") — read as-is. Do **not** move
  counts into `::after` content.
- Add `role="status"` to the stat strip's first item so "886 Open Roles" is re-announced after
  each swap. One figure, not the whole strip (which would be noisy).
- Companies popover: add Escape-to-close with focus returned to the button.
- Clear stays a real link.

### H. What NOT to change — builder hazards

1. **The Programme Year hint, verbatim.**
2. **`_apply_role_filter` semantics**, including unclassified `bucket=""` counting as `other`
   and the unrecognized-value fallthrough to campus. All six `role` URL values keep working.
3. **Facet cross-filter math** — a facet must not be crossed with its own filter.
4. **`_year_facet`'s overlap posture** (a row with both cohort and stated class year counts
   under both).
5. **`show_all_qs` built from the live querystring.**
6. **htmx form contract:** `hx-get` same URL, `hx-target="#cov-results"`,
   `hx-swap="innerHTML"`, `hx-push-url="true"`, `hx-trigger="change, keyup changed delay:350ms,
   submit"`, `<noscript>` submit, GET fallback. OOB spans are additive and must **not** widen
   the swap target to include the form.
7. **The recommend bar stays outside `#cov-results`.**
8. **CSS comments only `/* */` inside `<style>`** — a Django brace comment once killed 103 of
   185 rules; `directory/tests/test_styles_block.py` guards it.
9. **Card honesty strings:** `class-chip` renders only stated class years; the fresh badge says
   "First seen Nd ago", never "New".
10. **`YEAR_NONE` ("No Year Stated") stays first-class** — ~59% of campus rows.

## 5. Implementation checklist

**Backend — `directory/views.py`**
1. `_facets` returns per-option counts: `regions` → `[{value,label,count}]` plus a final
   `{value:"none", label:"Other / Unstated", count:<blank>}`; `tracks` likewise (document the
   multi-track overlap like `_year_facet` does). Cross-filter each facet against the *other*
   active filters. **Fallback if too costly on a public page:** compute region/track facets
   against the role-scoped set only — but then soften the count promise accordingly. Prefer
   the full fix; measure first.
2. Support `region=none` (`qs.filter(region="")`) with the existing unrecognized-value no-op.
3. Add `hidden_region` (blank-region rows excluded when a concrete region is active) and
   `show_unregioned_qs` (live querystring + `region=none`) to context.
4. `ROLE_CHOICES` stays the vocabulary source of truth; add `role_segments` /
   `role_optin_active` to context so the template doesn't re-derive bucket membership.

**Templates — `opportunities.html`**
5. Replace Role Type select with the segmented fieldset (legend, 4 radio+label pills, count
   spans, conditional muted fifth segment when `selected.role in ("other","all")`).
6. Wrap Year/Region/Track/Companies so they render as `<details class="filters-more">` at
   ≤640px and plain contents above (CSS-only: always render `open`, hide `summary` on desktop;
   a two-line inline script removes `open` under 640px only when no filter is active — no-JS
   mobile sees it open, which is correct-by-default).
7. Counted options for Region (incl. "Other / Unstated") and Track; count spans get stable ids.
8. Companies script: Escape-to-close + focus return.

**Templates — `_results.html`**
9. Move the `hidden_other` line to the **top**, above the stat strip. Remove the bottom copy.
10. Add the region honesty line when `hidden_region`.
11. Append the OOB fragment (render only on `HX-Request`; keep it in one included partial).
    Verify focus survives while typing in Search with counts updating.
12. `role="status"` on the open-roles stat item.

**Styles — `_styles.html`**
13. `.seg` pill styles; selected states from `role-*`; focus ring; 2×2 grid ≤640px; fifth
    segment full width.
14. `.filters-more` summary pill + explicit caret (copy the `.recbar-why-all` technique —
    Chrome drops the native marker under flex `summary`); hide summary >640px.
15. Keep `.filters` flex rules and the flex-start comment.

**Tests**
16. Template test: the Programme Year hint string renders (guard the load-bearing sentence).
17. View tests: `role=all` / `role=other` deep links render the conditional segment and keep
    `role` in subsequent serialization (regression for the mode-reset bug); `region=none`
    filters to blank-region rows; OOB fragment present on HX requests and absent on full
    renders; `hidden_other` appears above the stat strip.

## 6. Verification pass

Re-check every control against §D before ship. If any fails review, that control ships
**without its number** rather than with a stale one.

## 7. Unsure / deferred

- **RecruitU's listing UI is unverified** (login-gated). Research rests on Trackr, OffCycle,
  Simplify, Built In, Handshake docs, WTTJ docs.
- **Cross-filtering cost** on a public unauthenticated page — measure before choosing item 1's
  full fix vs fallback.
- **Per-firm counts in the Companies menu** — cheap, probably good, deferred for reviewability.
- **The "864 Fresh (10d)" stat** overstates freshness after bulk imports — separate fix.
