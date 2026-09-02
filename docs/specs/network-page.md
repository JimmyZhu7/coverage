# Network page spec

*Authored 2026-09-02 (D-14). Scope: `/app/contacts/` (`crm.views.contact_list`,
`crm/contact_list.html`, `crm/_styles.html`, `crm/_contact_search.html`) and the
modules that feed it: `crm/coverage.py`, `crm/sourcing.py`, `crm/campaigns.py`,
`crm/recruitment.py`. Audience: the next agent to change this page.*

*This describes the page AS IT IS after the 2026-09-01 pass, not as it was
audited before it. Where the CSS and this document disagree, the CSS wins and
this document is the bug, same rule `docs/design-spec.md` states for itself.
Numbers below were measured on the demo account (`demo@coverage.local`: 49
contacts, 22 target firms, 6 gap rows) on 2026-09-02.*

The thesis this page serves: **who do I know, where am I exposed.** Today
answers "what do I do now" and owns the cadence queue. Network is the standing
picture. A queue rendered here once, under different lane labels, and that
duplication is the single largest thing this page has had removed from it.

---

## Part 1. Page shape

Order, top to bottom, at every width:

1. **Page header.** `_pagehead.html` with `title="Network"` and the page's one
   navy button, "＋ Add Contact". The call site also passes `eyebrow` and `sub`;
   the include ignores both, and has since the eyebrow was dropped from every
   hero. The passed values are dead weight, not a rendered subtitle.
2. **Scope tabs** (`.subnav.scope-tabs.standalone`): All, the student's own
   regions, School. Region tabs come from `region_scopes`, which is Settings >
   Profile "Regions of Interest", so a student recruiting US only does not carry
   a Singapore tab forever. "Other countries" renders only when it holds
   somebody or the reader is standing on it. No counts (Part 3, D1).
3. **Coverage Gaps** (`.gap-strip`), when `gaps` is non-empty. One advocate
   summary line, then six ledger rows.
4. **Unplaced** (`.net-unplaced`), only on `?scope=unplaced`. Not a tab. Its one
   route in is the "Place them" link in the caveat above the contact grid, which
   renders on a region tab and only when that tab is actually showing guesses.
5. **Covered Firms** (`.net-coverage`): one panel, one section per tier, firm
   cards inside, then a five-item warmth key.
6. **Contacts** (`.net-contacts`): the search and sort toolbar, the one-shot
   park Undo strip when there is one, then the warmth ledger, then the sticky
   bulk bar.

There is no right rail and no warmth meter. The meter (`.meter-fill`, with its
`--from`/`--to` animation contract) lives on the contact detail page, not on
this board. `docs/design-spec.md` §5 described a July page at this URL and its
per-page block was deleted rather than updated, so between that deletion and
this file the CSS comments were the only record.

## Part 2. The five regions in detail

### A. Scope tabs

`scope` is a bare query parameter, empty for All. `NETWORK_SCOPE_REGIONS` is
`("hk", "us", "sg", "eu", "other")`, deliberately its own tuple rather than a
slice of `directory.classify.TRACKED_REGIONS`, so a market added there cannot
silently grow a tab nobody laid out. `school` and `unplaced` are the two scopes
that are not regions, and `UNPLACED_SCOPE` is kept out of the region tuple on
purpose: every branch that asks "is this a region tab" would answer wrong for
it.

A region tab shows contacts whose region is confirmed plus contacts whose
region is a guess from their firm. It says so: `unconfirmed_total` drives the
caveat above the contact grid ("11 of these have no region set. Shown on a
guess. Place them"), and that line is the entire nag budget for the unplaced
pool. It is zero on All and on School, so those scopes carry no caveat and no
route into the Unplaced tool.

### B. Coverage Gaps

The worst six tiered firms, worst first, from `coverage.rank_gaps` with
`limit=6`. Exposure is `tier_weight × gap_points + deadline_bonus`, where
`gap_points` is the ladder rung after `track_fit` halves it for an off-track
firm and zeroes it for an assessment firm. Covered firms are dropped.

Row anatomy, five grid columns that line up across all six rows:

```
| firm name | T1  6d to close | No advocate | Who to find ↓ | Add Contact |
```

- **The face carries no number.** Not the exposure score, not the rank, not the
  advocate fraction, not the open-role count. All of it is in the row's own
  `title=`, spelled out with the word "exposure". Pinned by
  `crm/tests/test_coverage_gaps.py::test_the_gap_strip_shows_no_number_on_its_face`.
- **Tier is both a colour and a text tag.** `.gap-t1/2/3` sets the left edge and
  `.gap-tier-tag` prints "T1". Colour alone is the failure mode this rule was
  written to avoid, so the tag does not come off.
- **Ties are broken by order, not by text.** Same tier and same gap state score
  identically, which is the formula being honest. `rank_gaps` then sorts on open
  campus roles. Open roles never enter the exposure formula: hiring volume is
  not a coverage gap.
- **"Who to find"** is a native `<details>` so it opens with no JavaScript. The
  panel is absolutely positioned and anchored `right: 0`, under the row's action
  end. Three role archetypes from `crm/sourcing.py`, each a prefilled LinkedIn
  search. `sourcing.DISCLOSURE` states that we hand over a query, not a person.
  The script does two things on top: close the others when one opens, and POST
  to `crm:sourcing_event` fire and forget.
- **The verb is "Add Contact", or "Apply" at an assessment firm.** An assessment
  firm's own FAQ declines the coffee chat, so prompting for a contact there
  would send a student to manufacture a relationship the firm has said does not
  move the process. `verb_reason` carries the why in `title=`.

`advocate_summary.line` sits above the rows: one number, the one the research
says predicts outcomes, said once instead of 54 times in 54 tooltips.

### C. Unplaced

Only on `?scope=unplaced`, where `contacts` is already filtered to the unplaced,
so the heading's `contact_total` is the unplaced count.

Grouped by firm, because people at one firm's Hong Kong desk arrived on one
thread and a student remembers them as a group. Per group: a "Select all"
button, the market chips that firm actually recruits in (from
`Contact.firm_markets`, not a fixed three), and the contacts as checkboxes.
`REGION_BULK_LABELS` supplies the verb labels once, for both the per-group chips
and the sticky bar, so there is no second copy to drift.

"Select all" and the chips are `hidden` in markup and revealed by script. With
JavaScript off the checkboxes and the three-verb bar are the whole control, and
the POST is identical either way.

Ignoring this tool forever breaks nothing. A blank region keeps the cadence
engine's both-markets fallback and the contact goes on appearing in its firm's
tabs marked unconfirmed. That is the designed steady state.

### D. Covered Firms

One `.net-panel`, one `.tier-section` per tier, `tier_sections` from the view.
"Unranked" is built only when something already sits there.

**Firm card anatomy:**

```
Bank of America        [SP]  [12d]
[========== warmth bar ==========]  [●●]
＋ Add a contact
```

- **Head row:** firm name, the "SP" sponsorship pill, and a red mono countdown
  for a CONFIRMED close inside 30 days, from `crm.utils.confirmed_firm_dates`.
  Rumours never draw one. The card says "12d" where the gap strip says "12d to
  close", because a 190px card shares this line with the firm's name; the
  sentence is in `close_title`.
- **The warmth bar renders on every card,** including a firm with zero contacts,
  which draws a flat grey track because `.firm-bar`'s own background is the cold
  colour. `bar_title` says "No contacts yet" for that case, so the one thing
  every other bar states in words is still stated.
- **Advocate sockets render only when there is a fill to show.** Every firm
  starts at zero advocates, so a permanent pair of empty dots was the most
  repeated element on the board. The "0 of N" case moved into the bar's
  `title=`.
- **No count badges.** "48 Open" and "1 Act Now" came off the card. Neither is
  lost: both still decide which firm sorts first inside its tier, open roles are
  the Opportunities feed's whole job, and every contact behind "Act Now" is
  named on Today.
- **One verb per card,** `.fc-act-link`, hidden once the firm is covered. A
  covered firm is a status, not a task.

**Re-tiering is drag only, on this page.** Cards are `draggable="true"`, the
lanes listen for `dragstart`/`dragover`/`drop`, and the drop POSTs to
`crm:set_firm_tier`. HTML5 drag never fires on a touchscreen. The per-card
`<select>` that covered touch and keyboard was removed 2026-08-31 at the
founder's call, so this is a gap on this page and not a gap in the product:
Settings > Target Firms carries an independent picker (`.tf-tier`). Every hint
on this page names that route rather than telling a phone to drag. Pinned by
`crm/tests/test_firm_tier_controls.py`.

The move is animated. Each card carries an inline `view-transition-name`, and
`setTier` wraps the reparent in `startViewTransition`. Reduced motion skips the
transition rather than zeroing its duration: the honest reading of the
preference is "do not start one".

**Each tier grid is capped to two rows by script,** measured off the rendered
layout rather than computed from tokens, because cards in one tier differ in
real height. A tier that already fits stays uncapped, with no tabindex and no
scroller. A capped grid becomes a labelled scroll region. The panel itself does
not scroll; only the grid inside it does.

Empty lane: "No firms on this tier." plus a link to Settings > Target Firms,
because that is where the absence is actually resolved.

The five-item key at the foot of the panel reads its labels from
`warmth_labels`, derived from `_WARMTH_SECTIONS`, so it cannot disagree with the
section headings a scroll below it.

### E. Contacts

**Toolbar** (`crm/_contact_search.html`, shared with Archived Contacts): a
search box and a five-option sort. Zero server round trips. Everything a row
matches or sorts on is baked into its own `data-*` attributes at render time, so
the script never re-parses rendered, title-cased text. Searching opens the
groups that have matches and restores the reader's own open/closed state when
the query clears.

**Park Undo** appears only on the render immediately after a bulk park. The view
POPs `PARK_UNDO_SESSION_KEY`, so a refresh is not still offering to reverse a
tap from ten minutes ago. It is its own `<form>`, above and outside the grid's
form, because nesting forms is invalid HTML.

**The warmth ledger.** Five rows in one bordered surface, hairline separated,
not five cards. Fixed order: "Emailed, Replied", "Chatted", "Advocate",
"Emailed, No Reply", "Not Contacted Yet". Not sorted by size: with five fixed categories a
reader benefits more from "Replied is always first" than from a leaderboard that
reshuffles.

- A row holding nobody is skipped by the template. `sections` from the view
  still lists all five keys and still sums to `contact_total` exactly, which
  `test_the_warmth_sections_account_for_every_contact_the_header_counts` pins
  against `resp.context["sections"]` directly.
- The dominant category is marked by weight, not by a badge: each summary's
  background fills left to right by its real share of `contact_total`, with a
  64px fade after the share point so the tint does not cut a hard vertical line
  through the label.
- **The first rendered row opens.** `{% cycle 'open' '' '' '' '' %}`, not
  `forloop.first`, and the difference is the point: `forloop` counts all five
  keys including the empty ones the template skips, so `forloop.first` would put
  `open` on a bucket that renders nothing and leave the whole page closed.

**Contact card anatomy** (`_contact_card` in `crm/views.py` builds the dict):

```
[x] (GH)  Grace Huang  [PARKED] [F] [T2] [HK]
          J.P. Morgan · IB Analyst
          9d since last touch          Log Touch   Edit
```

- The checkbox is the explicit target, not a wrapping `<label>`: the card is
  already full of its own click targets.
- The avatar is a staleness ring. `stale_pct` is days since last touch over the
  contact's own cadence window, capped at 1.0, and never-touched reads full.
  `stale_tint` is decided in Python (`due` / `warming` / `fresh`), not by a CSS
  substring hack that cannot tell 8% from 80%.
- Pills: Parked, gender initial, tier, region. Each renders only when its value
  exists. Parked is muted rather than coloured, because park is a decision the
  student made on purpose.
- Firm and role on one line, role compressed by `smart_role`, the untouched
  original one hover away in `title=`.
- **No description line and no email address on the card.** 120 of 182 bullets
  on the founder's live board were provenance noise the capture pipeline wrote
  for itself, and the clamp cut the useful ones mid sentence. Both fields render
  in full on the contact detail page.
- **Log Touch is a ghost button, Edit is plain.** They are not equals, and
  neither is navy (Part 4).

**Bulk bar.** Sticky, `hidden` until something is ticked. Three verbs: Snooze
3d, Stop following up, Archive, plus Clear. Shift-click extends a range. Archive
and park both confirm with the live count, read fresh from the checked boxes at
submit time so the dialog can never name a number bigger than what posts. There
is deliberately no delete: see `_BULK_VERBS` for why the product has no
hard-delete path for a contact at all.

## Part 3. Decisions, not accidents

These are the things a next agent will otherwise "fix". Each was decided, and
reversing one needs new evidence, not a fresh reading of the old spec.

**D1. The scope tabs carry no counts.** Removed 2026-08-31 on the founder's
direct call. The label alone is what you click, and the count is still one click
away on whichever board it names. Do not restore `.scope-count` here.

**D2. The warmth bars are unlabelled.** No percentage, no fraction, no legend
row inside the card. The bar's own `title=` gives the same colours in words with
real counts for that firm, which is more specific than a legend ever was, and it
costs a row only on the card being hovered. The key at the foot of the panel is
the one permanent explanation, and it exists because every card draws a bar now.

**D3. No number on a gap card's face.** Tried twice, cut twice. "exposure 12"
was a term that means something else in finance and an integer only readable
against its neighbours. "ranked 1 of 6" told the reader a position they could
already see, with a denominator that moved as the strip's length moved.

**D4. Tier counts came off the tier labels too,** same day and same call. The
`aria-label` on a capped grid still reads the tier name by element rather than
by raw `textContent`, so a label cannot silently regain the "Tier 1 20 firms"
ambiguity if a count is ever added back.

**D5. The board's meta strip is gone.** Three stacked bands above the board, an
unplaced ask, four off-board links and two "N hidden" caveats, none of which was
a contact. Every guarantee survives, relocated: the unplaced ask is made twice
lower on this page, and the four off-board ledgers hang off Settings > Your
Data. What is not allowed back is a permanently rendered line for a state that
is allowed, or a hidden-count sentence above the first contact card.

**D6. The cadence queue does not render here.** "Contacts Needing Action" and
Today's cockpit rendered the same `_build_actions` output under different lane
labels. The queue's one home is Today.

**D7. Hidden people are counted, not disappeared.** Campaign-excluded and
not-recruitment contacts are removed before the scope filter and before every
count, so the tier board, the gap strip, `contact_total` and the warmth sections
all derive from one list. They are kept, counted and reachable. A board that
quietly shrinks is a bug this repository has now fixed three times.

**D8. Zero-state rows do not render.** "Other countries" with nobody in it, a
warmth row with nobody in it, and the archived/parked/hidden links with nothing
behind them are all a standing reproach for a state the product allows. The one
exception is standing on the tab already: answering "London" for your first
Other contact must not make the tab you are looking at vanish.

## Part 4. What the 2026-09-01 pass changed

Recorded here so the spec starts true, and so a reader can tell a fresh decision
from an old one.

1. **The gap strip became a ledger.** Six 150px boxes in a six-across grid
   became six rows in one bordered surface. Three card shapes shared this board
   and this was one of them. The columns are placed explicitly, so a row missing
   its sourcing panel cannot slide its button one column left.
2. **The first non-empty warmth row opens by default.** Collapsing all five
   (2026-08-31) meant a page called Network opened showing zero people. The
   objection had been to the page arriving several screens tall, not to it
   showing anybody. Four rows stay closed.
3. **Log Touch became a ghost button.** Measured on the demo board: 49 contact
   cards, 49 navy fills, 98 buttons on one page, and the page's own "＋ Add
   Contact" as a fiftieth navy button with nothing to distinguish it. `.act-ghost`
   is the treatment Today's queue already uses for a per-row verb, so the two
   boards now agree on what a row-level action looks like. Navy is reserved for
   the page-level action, and exactly one navy button renders.
4. **The 10px pressable text was lifted to `--fs-xs` with a 32px floor.**
   `.fc-act-link` (a card's only verb, measured 247x16px on a phone),
   `.gap-tier-tag`, `.gap-due-tag`, `.gap-act` and the "Who to find" toggle.
   `--fs-nano`'s own token comment calls 10px the floor and reserves it for
   uppercase badge labels; on this page only the "SP" pill still qualifies.
5. **The Covered Firms panel stopped nesting its own scroller.** It was fixed at
   606px, its content measured 606px, and it never scrolled while the capped
   grid inside it did. The Unplaced panel keeps its cap, because it has no inner
   one.

All five are pinned in `crm/tests/test_ui_pass_2026_09_01.py`.

## Part 5. Honesty rules

What this page must not imply.

1. **A guessed region is never shown as a confirmed one.** A region tab that
   includes guesses says how many, with a link to place them.
2. **Nothing on this page infers "other countries".** A human says "London" by
   hand, through the Unplaced tool, or it stays unknown.
3. **A count and its board agree.** Every number on this page derives from the
   one filtered contact list, never from a second query written beside it.
4. **A removed number is not a hidden number.** Every count taken off a face is
   either in a `title=` on the element it describes, or it is doing work in a
   sort, and this document says which.
5. **The card never asserts a chat, a send or a state the user did not enter.**
   Log Touch is a link to the contact page, not a one-click attestation.
6. **Bulk verbs name their reach and their way back.** Archive and park confirm
   with the live count; park also leaves a one-shot Undo.
7. **Colour is never the only signal.** Tier is a colour and a tag, warmth is a
   dot and a word, sponsorship is a pill with text.
8. **Progressive enhancement is a promise, not a nicety.** Every control this
   page reveals with script has a working no-script equivalent that posts to the
   same endpoint. "Select all", the per-firm chips and both bulk bars are all in
   this class.

## Part 6. Responsive and accessibility

- **700px:** the gap row's five columns reflow. The firm name and the verb keep
  their line; tier, state and "Who to find" stack under them. Still one surface.
- **900px:** `.net-panel` drops its height cap and grows with its content.
- **560px:** the search and sort toolbar stacks. Watch the flex-basis when
  touching it: `flex: 1 1 260px` reads against the main axis, and in a column it
  once told the search field to start 260px tall.
- Tier grids re-measure their two-row cap on resize, on `animationend` after the
  `.kin-reveal` entrance, and on `visibilitychange`, because a background tab
  never plays the entrance animation and so never fires the recompute.
- A capped grid gets `tabindex="0"`, `role="region"` and a label naming the tier
  and the firm count.
- Every checkbox carries an `aria-label` with the contact's name; the gap strip's
  "Add Contact" links carry an `aria-label` with the firm's name, because the
  visible text is deliberately the same six times over.
- `prefers-reduced-motion: reduce` flattens the bar growth, the card entrance
  and the retier transition. §17 of the stylesheet is the single override.
- The warmth summary is the whole disclosure control, not a triangle at its end:
  cursor, hover tint and the chevron glued to the count all say so together.

## Part 7. Data contract

Context keys the template reads: `scope`, `region_scopes`, `all_total`,
`school_total`, `unplaced_scope`, `unplaced_groups`, `region_verb_labels`,
`gaps`, `advocate_summary`, `sourcing_note`, `tier_sections`, `firm_total`,
`sections`, `contact_total`, `unconfirmed_total`, `warmth_labels`, `park_undo`.

`unplaced_total` is in the context and nothing renders it. It stays as the
assertion surface for `crm/tests/test_region_resolution.py`, which reads "how
many contacts still have no region" off the page's own computation rather than
re-deriving it.

Endpoints this page posts to: `crm:contacts_bulk` (both forms),
`crm:contacts_park_undo`, `crm:set_firm_tier`, `crm:sourcing_event`.

Vocabulary has one source. `_WARMTH_SECTIONS` in `crm/views.py` is the only map
from a stored warmth slug to the words a student reads, `_warmth_labels()`
derives the legend from it, and `smart_title` cases the data. Nothing on this
page hand-cases a firm name, a person's name or a warmth label.

## Part 8. Known dead weight

Recorded rather than fixed, because a spec that quietly omits these is how they
survive.

1. `contact_list.html` passes `eyebrow` and `sub` to `_pagehead.html`, which
   ignores both.
2. `.fc-tier` still has a full CSS block, hover, focus and `@media (hover: none)`
   rules in `crm/_styles.html`, and `test_ui_pass_2026_09_01.py` still asserts
   its size and radius, but no template renders it. It went with the per-card
   picker on 2026-08-31. Removing the CSS means retiring that assertion in the
   same change.

## Open uncertainties

1. Whether drag-only re-tiering is acceptable long term. Settings > Target Firms
   covers touch and keyboard for the product, but this board is where a student
   is looking when they form the opinion.
2. Whether the two-row tier cap is the right number. It was chosen so the panel
   fits, not measured against how far students actually scroll.
3. Whether the first-row-open rule should follow the reader instead of the fixed
   order. Replied is first and most actionable today; on a board where Replied
   is one person and Emailed No Reply is ninety, the useful row may be the one
   with the work in it.
4. Whether the warmth share fill reads as a measurement to anyone. It carries a
   real number and claims none, which is the intent, and nobody has been asked.
