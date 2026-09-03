# Network page spec

*Authored 2026-09-02 (D-14). Scope: `/app/contacts/` (`crm.views.contact_list`,
`crm/contact_list.html`, `crm/_styles.html`, `crm/_contact_search.html`) and the
modules that feed it: `crm/coverage.py`, `crm/sourcing.py`, `crm/campaigns.py`,
`crm/recruitment.py`. Audience: the next agent to change this page.*

*Revised 2026-09-02 (evening): the Coverage Gaps strip this file described in
full was deleted and its status moved onto the firm cards as a "CG" tag. Where
this document and the code disagree, the code wins and this document is the bug,
same rule `docs/design-spec.md` states for itself. Numbers below were measured
on the demo account (`demo@coverage.local`: 49 contacts, 23 firm cards) and on
the founder's own board (54 firm cards) on 2026-09-02, read-only.*

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
3. **Unplaced** (`.net-unplaced`), only on `?scope=unplaced`. Not a tab. Its one
   route in is the "Place them" link in the caveat above the contact grid, which
   renders on a region tab and only when that tab is actually showing guesses.
4. **Covered Firms** (`.net-coverage`): one panel, one section per tier, firm
   cards inside, then a six-item key.
5. **Contacts** (`.net-contacts`): the search and sort toolbar, the one-shot
   park Undo strip when there is one, then the warmth ledger, then the sticky
   bulk bar.

There is no Coverage Gaps strip. One stood between the scope tabs and Covered
Firms until 2026-09-02 and is deleted (Part 3, D11). Nothing may take its
place: a second list of firms above a board that already lists every firm is
the seam that got it removed.

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

### B. Coverage exposure, and the CG tag

There is no strip. `coverage.rank_gaps` still ranks EVERY tiered firm with a
gap, worst first, and what the page renders off it is one mark: firms whose
exposure clears `coverage.CG_EXPOSURE_MIN` wear a "CG" pill on their own card
in Covered Firms.

Exposure is `tier_weight × gap_points + deadline_bonus`, where `gap_points` is
the ladder rung after `track_fit` halves it for an off-track firm and zeroes it
for an assessment firm. Covered firms are dropped: at or above the advocate
target is not a gap.

**The bar is 8, written as `TIER_WEIGHT[2] × GAP_POINTS[NO_CONTACTS]`.** It
reads "a firm you ranked, that you have no way into": both of the formula's
heaviest terms have to fire. Measured 2026-09-02 by rendering the board at each
candidate bar and counting the cards that would wear the tag:

| bar | founder, 54 cards | demo, 23 cards |
| --- | --- | --- |
| any gap at all | 54 (100%) | 21 (91%) |
| exposure ≥ 4 | 44 (81%) | 20 (86%) |
| exposure ≥ 6 | 28 (51%) | 18 (78%) |
| **exposure ≥ 8** | **11 (20%)** | **6 (26%)** |
| exposure ≥ 9 | 7 (13%) | 6 (26%) |
| exposure ≥ 12 | 1 (2%) | 6 (26%) |

Every one of the founder's 54 tiered firms carries some gap, because he has
zero advocates anywhere, so "any gap" is his default state and a tag on the
default state is furniture. Every bar under 8 still tags half the board or
more; 12 tags one card out of 54. At 8 the tag lands on eleven banks on his own
tracks: one with nobody (3 × 4 = 12), six he has emailed with no reply (3 × 3 =
9), and four tier-2 banks with nobody (2 × 4 = 8). On the demo board it selects
exactly the six firms the old strip drew, name for name.

What it deliberately does not tag: the eleven PE, AM and consulting shops he
tiered aspirationally and knows nobody at, because `track_fit` already halved
them and tagging them would re-open the defect that multiplier was added to
close; tier-3 firms, because tier 3 is the student's own statement that they
matter least; and any firm where somebody has replied. A firm that HAS an
advocate can never be tagged: `BELOW_TARGET` is 1 point, so its ceiling is
3 × 1 + 3 = 6.

Measured on the raw contact table the numbers differ, and the board is right.
The view drops archived, parked, campaign-hidden and non-recruitment people
before it counts a firm's warmths, so Société Générale is `all_cold` in a bare
query and `no_contacts` on the board, a whole ladder rung apart.

Rules the tag inherits from the strip's own arguments:

- **No number on a card's face, and none in its `title=` either.** "exposure
  12" was a term that means something else in finance; "ranked 1 of 6" restated
  a position the reader could see. The strip was allowed to keep the
  arithmetic in a hover; a card is not, because a card is not in a ranked list
  and there is nothing to compare it against.
- **The mark is readable without a hover.** "CG" is two letters. The key at
  the foot of the panel says "Coverage gap, nobody warm yet" in words a
  touchscreen can reach, and it renders whether or not any card is tagged. The
  pill's own `title=` carries the sentence: "Coverage gap. You ranked this firm
  high and nobody here is warm yet."
- **Red, in a different register from the countdown.** `--danger` is the
  founder's own call and the palette already carries it, but the deadline tag
  two elements along is bare `--danger` mono text on the same 22px row. CG is
  the soft-filled chip instead (`--danger-soft` / `--danger` / `--danger-line`),
  built exactly like the green "SP". Shape separates them before hue does, and
  the countdown stays the loudest mark on the card: a gap you can work is not
  an alarm, a closing deadline is.
- **Ties are not broken by the tag.** Same tier and same rung score
  identically, which is the formula being honest, and both cards get the same
  mark. `rank_gaps` breaks the tie on open campus roles, which still decides
  order for the weekly digest (`crm/digest.py` names `no_contact[:3]`). Open
  roles never enter the exposure formula: hiring volume is not a coverage gap.

`advocate_summary` is not rendered and is no longer in the context. Its last
surface was the deleted heading's `title=`. The function is untouched and
remains the one definition of the count.

"Who to find" is gone with the strip: three role archetypes from
`crm/sourcing.py`, each a prefilled LinkedIn search, in a `<details>` inside a
gap row. The module, the `crm:sourcing_event` endpoint and their tests all
still stand and the feature now has no surface. That is deliberate — what the
founder asked to delete was a widget — and it is an open question, not a
finished decision (Open uncertainties, 5).

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
Bank of America   [CG] [SP]  [12d]
[========== warmth bar ==========]  [●●]
＋ Add a contact
```

- **Head row:** firm name, then up to three marks, in that order. "CG" when the
  firm's exposure clears the bar (section B). "SP" when the firm sponsors. A red
  mono countdown for a CONFIRMED close inside 30 days, from
  `crm.utils.confirmed_firm_dates`; rumours never draw one. The card says "12d"
  where the deleted strip said "12d to close", because a 190px card shares this
  line with the firm's name; the sentence is in `close_title`.
- **The marks lead with coverage,** because this board is what coverage means.
  "SP" is a fact about the firm and the countdown is a fact about the calendar,
  and they keep the order they already had behind it.
- **Only the name gives.** Both pills and the countdown are `flex: none`; the
  name is `flex: 1 1 auto` and ellipsises. Measured at 1280x800: a head row
  carrying all three marks and a 44-character name is one line at 22px on a 96px
  card, identical to a card with no marks, and leaves the name 77px. No card on
  either board carries more than one mark today.
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

The six-item key at the foot of the panel reads its warmth labels from
`warmth_labels`, derived from `_WARMTH_SECTIONS`, so it cannot disagree with the
section headings a scroll below it. Four warmth dots, then the "SP" and "CG"
swatches with their words. Every mark a card can wear owes the key an entry: a
two-letter pill the key cannot explain is an abbreviation with no reading, and
the key is the only explanation a touchscreen can reach.

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

**Contact card anatomy** (`_contact_card` in `crm/views.py` builds the dict).
Four slots, every one of them a fixed height, so the same fact is at the same
offset on every card in the grid and a card with fewer facts leaves a gap
rather than sliding the rest up:

```
          padding                                            12
[x] (GH)  Grace Huang                       .cc-head         40   (the avatar)
             gap                                              4
[PARKED] [F] [T2] [HK]                      .cc-tags         16   (reserved)
             gap                                              4
J.P. Morgan · IB Analyst                    .cc-firm         21   (one line)
                                            (free space)
             gap                                              4
──────────────────────────────────────      .cc-foot   border-top, then 12
9d since last touch     Log Touch   Edit               margin-top: auto
          padding                                            12
```

Which puts `.cc-tags` at 57px and `.cc-firm` at 77px from the top of every card
on the board, the foot at 102px on every one of them, and the card at 179.4px at
four across (where the foot wraps) or 156.2px everywhere else. Measured on all
49 demo cards at 1280 and 375 in both colour schemes, with
`content-visibility` forced visible — leave it on and every offscreen card
reports its `contain-intrinsic-size` placeholder instead of its box, which
reads as a flat "no change" whatever you changed.

- The checkbox is the explicit target, not a wrapping `<label>`: the card is
  already full of its own click targets. It is centred by the row, not by a
  hand-computed margin.
- The avatar is a staleness ring. `stale_pct` is days since last touch over the
  contact's own cadence window, capped at 1.0, and never-touched reads full.
  `stale_tint` is decided in Python (`due` / `warming` / `fresh`), not by a CSS
  substring hack that cannot tell 8% from 80%.
- **The name has the identity row to itself,** wraps rather than truncating, and
  the row is pinned at the avatar's 40px with a 20px line-height so a two-line
  name fills it exactly instead of pushing everything below it down. The two
  longest names in the data, "Bartholomew Vanderhoeven" (204px) and "Mariela
  Jimenez-Sanchez" (181px), both wrap against 173px of room.
- **Pills get their own reserved row,** rendered whether or not the contact has
  any: Parked, gender initial, tier, region, each still conditional
  individually. Parked is muted rather than coloured, because park is a decision
  the student made on purpose. The row never wraps — the widest combination the
  data can build is 185px against 248px in the narrowest card the grid makes.
- **Firm and role, exactly one line,** as two spans that truncate
  independently. The line is a full-width row (253px at 1280, 283px at 375, up
  from the 173px it had beside the avatar). The ROLE never gives up a pixel and
  the firm is the half that ellipsises, because two people at the same firm are
  told apart by the role; the firm is guaranteed a 3.5em floor by a `max-width`
  on the role rather than a `min-width` on itself, which would pad "BCG" and
  "USC" out to the floor and hand the pixels to nobody. Role compressed by
  `smart_role`; the untouched original, full firm and full uncompressed role, is
  one hover away in `title=`.
- **No description line and no email address on the card.** 120 of 182 bullets
  on the founder's live board were provenance noise the capture pipeline wrote
  for itself, and the clamp cut the useful ones mid sentence. Both fields render
  in full on the contact detail page.
- **Log Touch is a ghost button, Edit is plain.** They are not equals, and
  neither is navy (Part 4).
- **The gaps are the hierarchy, and the seam is drawn.** The name, its pills and
  its firm line are one identity block at 4px; the card's one real division is
  the seam above the foot, which is 4px + a `--line` hairline + 12px. Flat 8px
  gaps between all four slots said nothing about which of them belong together,
  and 16px of vertical padding on four short lines put 32px of the card above
  the avatar and below the buttons. Taking those back cost the card 23px at 1280
  and 15px at 375 without moving a slot. The hairline is the dark-mode half: the
  pill row renders empty on 29 of 49 demo cards and the seam sat 20px below it,
  and bounded only by the card's own low-contrast edge the two read as one hole.
  `--line` is that same edge token, so the rule needs no separate dark treatment.
- **`margin-top: auto` is not where the slack was.** Measured, it collects 0.0px
  on all 49 cards at both widths — the foot already sits at a fixed offset
  because every slot above it is a fixed height. It stays as the guarantee for
  the case it was written for: a grid row stretched taller than its content.
- **The foot wraps to two rows at four across, and that is accepted.**
  `.cc-since`'s 7.5rem floor plus 12px plus the 149.1px button pair wants 281.1px
  against 253.5px of card. Forcing one row needs 27.6px, and the only sources are
  the floor that keeps the wrap decision uniform across the grid (widest real
  string 109px, so it cannot go below about 7rem) and the buttons' own padding
  (14px → 8px). That leaves 2.4px of headroom at 1280 and still wraps at the
  grid's 280px `minmax` floor. The two axes are spaced separately instead —
  `column-gap` 12px across, `row-gap` 4px down — so the wrapped fact and controls
  read as one footer.

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

**D3. No number on a firm card's face, or in its `title=`.** Tried twice on the
old gap card, cut twice. "exposure 12" was a term that means something else in
finance and an integer only readable against its neighbours. "ranked 1 of 6"
told the reader a position they could already see, with a denominator that moved
as the strip's length moved. The strip was allowed to keep the arithmetic in a
hover; the CG tag that inherited the rule is not, because a card is not in a
ranked list and has nothing to be compared against.

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
count, so the tier board, the exposure ranking, `contact_total` and the warmth
sections all derive from one list. That list is also what the CG bar is measured
against, and it differs from a raw contact query by a whole ladder rung at some
firms (section B). They are kept, counted and reachable. A board that
quietly shrinks is a bug this repository has now fixed three times.

**D8. Zero-state rows do not render.** "Other countries" with nobody in it, a
warmth row with nobody in it, and the archived/parked/hidden links with nothing
behind them are all a standing reproach for a state the product allows. The one
exception is standing on the tab already: answering "London" for your first
Other contact must not make the tab you are looking at vanish.

**D9. The advocate aggregate is nowhere on this page.** Removed from the face
2026-09-02 on the founder's direct call, quoting the line back word for word;
`.strip-note` went with it and nothing else on the site used the class. It then
lived in the strip heading's `title=` for a few hours, and the heading was
deleted the same evening, so the sentence has no surface at all now and
`advocate_summary` is out of the context.
`test_the_advocate_line_is_nowhere_on_the_page` pins that.
`crm.coverage.advocate_summary` is untouched and stays the one definition of the
count. Putting it back anywhere is a new decision about where an aggregate
belongs, not a restoration of this one.

**D10. Red means one thing per surface, and shape separates two reds.** The gap
row's 3px tier-coloured left edge was cut 2026-09-02 because `rank_gaps` weights
tier heaviest, so six identical `--danger` edges read as a wall rather than a
signal. The firm card now carries two `--danger` marks — the CG chip and the
close countdown — and they are distinguished by SHAPE, not hue: a soft-filled
chip against bare mono text. A third red mark on this card needs a third shape
or it does not go on.

**D11. The Coverage Gaps strip is deleted, whole.** "Delete this widget and
route its status of coverage gaps into the actual company cards." Heading,
ledger, six rows, "Who to find" dropdown, and every CSS rule that drew any of
them. `.gap-due-tag` is the one class that survived, because the firm card had
already adopted it for its countdown. What is not allowed back is a second list
of firms above a board that already lists every firm: the strip named six firms
in one place while the board named all of them in another, and the reader had to
hold six names in their head to cross-reference. `test_the_widget_is_gone_and
_left_nothing_behind` pins the deletion at both the markup and the stylesheet
level.

**D12. A firm name repeated inside the role is left alone.** "BCG · BCG contact
via USC" is the student's own text, and suppressing the repeat is a display rule
that deletes a word a person typed. Counted on the founder's board: 11 of 168
role-bearing rows repeat the firm's leading token in the visible role, and 9 of
those 11 are firm "USC" with a role like "USC junior/senior peer" — where the
strip would turn a claim about a peer AT USC into a claim about a peer. The
repetition also costs nothing now: with the firm/role line at full card width
and the role protected from shrinking, none of those 11 rows truncates at any
width the grid produces. Zero rows on the demo board are affected at all. The
untouched original stays in `title=` either way.

**D13. The firm line is one line, and the two-line clamp that briefly stood
there is retired.** The clamp was correct about its own defect — at 173px of
room the line rendered "Goldman Sachs · Campus R…", cutting one letter into the
word that carries the meaning — but a clamp that resolves to two lines on some
cards and one on others is a variable-height fact, and the card is a fixed
skeleton. The mid-word cut is answered by giving the line the card's full width
and protecting the role instead. Reversing this needs new evidence, not the old
argument: the numbers that retired it are 2 two-line firm lines before and 0
after, against 0 roles truncated on 384 real rows at every width.

## Part 4. What the 2026-09-01 pass changed

Recorded here so the spec starts true, and so a reader can tell a fresh decision
from an old one.

1. **The gap strip became a ledger.** Six 150px boxes in a six-across grid
   became six rows in one bordered surface. Three card shapes shared this board
   and this was one of them. SUPERSEDED the following evening: the strip was
   deleted outright, which takes the board to two card shapes rather than three
   and is what `test_the_board_is_down_to_two_card_shapes` now pins.
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
   uppercase badge labels; on this page only the "SP" and "CG" pills qualify,
   and three of the five elements listed here were deleted the next evening.
5. **The Covered Firms panel stopped nesting its own scroller.** It was fixed at
   606px, its content measured 606px, and it never scrolled while the capped
   grid inside it did. The Unplaced panel keeps its cap, because it has no inner
   one.

All five are pinned in `crm/tests/test_ui_pass_2026_09_01.py`.

The 2026-09-02 pass then took the gap strip twice more, both times off the
founder's eye rather than off a measurement. The first pass gave the six rows
one set of column tracks (`subgrid`), moved the flexible track off the firm
name, and quieted the "Who to find" rule from dashed `--line-strong` to dotted
`--line`. Shown that, he asked for the caption gone and said the widget still
needed work, which produced the second. Items 6 to 9 below are therefore a
record, not a live description: the strip they refined was deleted a few hours
after they landed, and their tests went with it (item 11).

6. **The tier rail came off.** Six identical `--danger` edges, one continuous
   3px bar down a 340px strip. Tier moved entirely into the tag: the word plus
   one weight step (D10).
7. **The slack became its own track.** Moving it from the name to the state
   column had relocated the hole rather than closed it — `.gap-state` was a
   739px box holding two words, so the state text was stranded mid-row. Six
   tracks now, five zones, and the fifth track empty.
8. **"Who to find" joined the facts and lost its rule.** With the gutter
   between it and the button the two stopped competing, and the resting
   underline came off in favour of the ↓ glyph. Its panel re-anchored to the
   toggle.
9. **The state phrase was promoted over the tier tag** in size, colour and
   face. They had been identical but for the tag being the heavier.
10. **The advocate caption was removed** (D9).

Then the third pass, the same evening:

11. **The strip was deleted and the CG tag replaced it** (D11, section B). Its
    eight geometry tests in `crm/tests/test_network_row_and_card_geometry.py`
    are retired with the markup they measured and replaced by two: one that the
    rules went with it, and one for the chip that carries its meaning now. Items
    6 to 9 were true of a widget that no longer renders; item 10 survives as
    D9, and the tier-rail argument survives as the shape rule in D10.

Measured at 1280px across those passes: the ledger's state text ended at x=380
with the next zone at x=1047, then a tight fact cluster ending at x=491.7 with
the button alone at x=1141.2, and now no row at all — the board opens on
Covered Firms.

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
7. **Colour is never the only signal, and a two-letter mark is not a word.**
   Warmth is a dot and a word; sponsorship and coverage are lettered pills that
   spell themselves out in a `title=` AND in the key at the foot of the panel,
   because a `title=` is unreachable on a touchscreen. The rule is that the
   words can always stand alone, not that every fact owes a hue: the deleted gap
   strip proved the second reading wrong by painting six rows the same alarming
   colour.
8. **Progressive enhancement is a promise, not a nicety.** Every control this
   page reveals with script has a working no-script equivalent that posts to the
   same endpoint. "Select all", the per-firm chips and both bulk bars are all in
   this class.

## Part 6. Responsive and accessibility

- **900px:** `.net-panel` drops its height cap and grows with its content.
- **560px:** the search and sort toolbar stacks. Watch the flex-basis when
  touching it: `flex: 1 1 260px` reads against the main axis, and in a column it
  once told the search field to start 260px tall.
- Tier grids re-measure their two-row cap on resize, on `animationend` after the
  `.kin-reveal` entrance, and on `visibilitychange`, because a background tab
  never plays the entrance animation and so never fires the recompute.
- A capped grid gets `tabindex="0"`, `role="region"` and a label naming the tier
  and the firm count.
- Every checkbox carries an `aria-label` with the contact's name. A firm card's
  verb carries one with the firm's name, because the visible text is the same on
  every card. The "SP" and "CG" swatches in the key are `aria-hidden`: the words
  beside them are the accessible content, and announcing the abbreviation twice
  helps nobody.
- `prefers-reduced-motion: reduce` flattens the bar growth, the card entrance
  and the retier transition. §17 of the stylesheet is the single override.
- The warmth summary is the whole disclosure control, not a triangle at its end:
  cursor, hover tint and the chevron glued to the count all say so together.

## Part 7. Data contract

Context keys the template reads: `scope`, `region_scopes`, `all_total`,
`school_total`, `unplaced_scope`, `unplaced_groups`, `region_verb_labels`,
`tier_sections`, `firm_total`, `sections`, `contact_total`, `unconfirmed_total`,
`warmth_labels`, `park_undo`. Each firm card in `tier_sections` carries `cg`, the
boolean the tag renders off.

Two keys are in the context and nothing iterates them, both deliberately:

- `gaps`, the full ranking. `coverage.flagged_firm_ids` reads it to build the
  tag, and it stays in the context so a test can read the arithmetic behind a
  mark rather than inferring it from a pill.
- `unplaced_total`, the assertion surface for
  `crm/tests/test_region_resolution.py`, which reads "how many contacts still
  have no region" off the page's own computation rather than re-deriving it.

Two keys were REMOVED on 2026-09-02 with the strip that rendered them:
`advocate_summary` (D9) and `sourcing_note`. Both functions still exist and are
still the one definition of what they compute.

Endpoints this page posts to: `crm:contacts_bulk` (both forms),
`crm:contacts_park_undo`, `crm:set_firm_tier`. It no longer posts to
`crm:sourcing_event`; that endpoint is live, tested, and has no caller.

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
3. `crm/sourcing.py`, the `crm:sourcing_event` endpoint and `crm/urls.py`'s route
   for it are all live and tested with no caller in the app, since 2026-09-02.
   The "Who to find" panel was their only surface and it was inside the deleted
   strip. Left standing on purpose: what was deleted is a widget, and whether
   contact sourcing deserves a surface elsewhere is a product question (Open
   uncertainties, 5), not a cleanup.
4. `.cc-school` has a CSS block in `crm/_styles.html` and no markup renders it.
   The card carries no school chip; `card.school` is still built by
   `_contact_card` and read by the search index, but nothing draws it.
5. `.contact-card` sets `contain-intrinsic-size: auto 128px` alongside
   `content-visibility: auto`, and the card is 171px to 202px tall depending on
   width. The placeholder is a floor, so the rendered cards still decide the
   grid's row height, but the number is now well under the real one and no
   longer means what its comment says it measures. It also makes the card
   unmeasurable from JavaScript: `getBoundingClientRect` on an offscreen card
   returns the placeholder, which is how a "before" and an "after" can measure
   byte-identical. Disable `content-visibility` for the measurement.

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
5. Where, if anywhere, "Who to find" goes. It died as a side effect of deleting
   the widget it lived inside, not on its own merits, and the founder has not
   been asked about it. It does not fit the firm card as-is: the panel is an
   absolutely-positioned dropdown and the card sits inside a capped, scrolling
   grid with `overflow: hidden`.
6. Whether the CG bar holds as the founder's board changes. It is a fixed
   position on the exposure scale, not a share of the board, so it will tag more
   cards as he tiers more firms and fewer as people start replying. The second
   is the point; the first is worth re-measuring after the next tiering pass.
