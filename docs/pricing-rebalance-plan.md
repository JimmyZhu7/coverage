# Pricing rebalance plan — trimming Free, selling Pro

Status: spec for implementation. Template and view changes only unless a
section says otherwise. Written 2026-08-19 against the live
`coverage_web/templates/core/pricing.html`, `core/views.py::pricing()`,
`billing/credits.py`, `settings/base.py::CREDIT_PLANS`, and
`docs/credit-system-plan.md`.

> **Changelog — v1, 2026-08-19.** The founder's read on the current page:
> "free tier has too much features." He is right about the story, not the
> count. Free currently lists ten checked bullets including Gmail Live and
> the AI advisor, while Pro's whole pitch is "the same, but more" — the
> page reads as Free already having everything and Pro being a tip jar.
> This plan moves the automation axis (real-time Gmail sync) to Pro,
> reframes the advisor as visibly limited on Free, makes the refresh-speed
> and archive-depth gaps explicit, and adds an icon-based comparison table
> so the upgrade path is legible at a glance.

## 1. The principle behind every move

From the product-shape decision on record: **listings are the wedge,
networking CRM is the moat.** The wedge (the opportunity board, filters,
full-posting deadlines, eligibility and sponsorship answers) is what gets a
student to sign up. It stays in Free, generous, with live counts. What goes
to Pro is the *more-of-it* axis once a student is hooked:

- **Automation** (things Coverage does without being asked): real-time
  Gmail sync, Calendar sync, hourly refresh.
- **Depth** (heavier AI use): a stronger model and three times the credit
  pool.
- **History**: the multi-cycle archive.
- **Reach**: LinkedIn import.

This matches what the researched comps do (credit-system-plan §2): Simplify+
and Teal+ keep the job board free and gate auto-tracking, AI volume, and
integrations; LinkedIn Premium sells depth on a free network. Nobody gates
the board itself.

It also matches where Coverage's own money goes. The board costs the same
to serve whether one student or fifty read it. The AI surfaces and inbox
sync are the metered, per-user costs — the credit system exists precisely
because of them — so they are the honest paid axis.

## 2. The moves, one by one

### Move 1 — Gmail Live (real-time) goes to Pro; on-demand scan stays Free

**What changes.** Free loses the bullet "Gmail Live: connect Gmail and it
logs itself." Pro gains real-time Gmail sync, merged with the Calendar
bullet it already carried. Free keeps the ability to connect Gmail and run
Scan Now on demand (the deterministic pass is free by design; the AI
residue pass is already credit-metered on both plans).

**Why.** Three reasons stack:

1. The current page is incoherent: Free lists "Gmail Live" while Pro lists
   "Optional Gmail and Calendar sync" as an addition. A visitor cannot
   tell what the difference is. Resolving the conflict toward Pro gives
   the paid tier its anchor feature.
2. Manual-free / automatic-paid is the cleanest upgrade axis in this
   category, and it is honest about cost: a standing Gmail watch plus
   continuous classification is exactly the per-user recurring spend the
   credit system was built to bound.
3. Gmail Live is not configured on any deploy (`capture/gmail_live.py::
   is_configured()` is false everywhere; Google Cloud setup is still
   pending per docs/gmail-live-setup.md). No user has this today, so the
   page is repositioning a forward-looking claim, not clawing back a live
   feature.

**Enforceability.** NEEDS CODE before Gmail Live goes live publicly — today
nothing gates it by plan. See §7.

### Move 2 — The advisor stays on Free, but visibly limited; Pro is 3x

**What changes.** Free's advisor bullet gains its real monthly ceiling
(the daily one alone undersells the limit). Pro's bullet claims the two
things that are true and enforced today: a stronger model, and three times
the monthly credit pool.

**Why.** The advisor is moat-adjacent (it reads the user's own CRM) but it
is also the single biggest per-user cost. The credit system already
enforces the split; the page just fails to sell it. One honesty
constraint the copy must respect: because a Pro message costs 3 credits,
both plans currently work out to 15 messages a day and 60 a month. So Pro
copy sells **model quality and the credit pool**, never "more messages" —
that would be false today. (§8 offers an optional one-line settings change
that would make Pro's daily ceiling genuinely higher.)

**Enforceability.** Copy-only. `CREDIT_PLANS` enforces all of it today.

### Move 3 — Refresh cadence becomes an explicit Free limit

**What changes.** Nothing in the Free card. The comparison table (§6) gets
a "Board refresh" row: Free "Every 6 hours", Pro "Hourly on Tier 1". The
Pro card already claims this; the table makes the Free side of the same
fact visible instead of leaving it implied.

**Why.** Speed-of-refresh is a classic more-of-the-same-thing paid axis
and it costs real scraper capacity. Stating Free's cadence plainly turns
an invisible limitation into a reason to upgrade without removing
anything.

**Enforceability.** Positioning on an unlaunched capability (per-user
hourly refresh is not built). Acceptable while the Pro column carries the
"In the works" badge — this claim already sits on the live page today.

### Move 4 — Season depth becomes an explicit Free limit

**What changes.** Nothing in the Free card. The table gets a "Season
archive" row: Free "This cycle", Pro "Every cycle". The Pro card keeps its
existing "Multi-cycle archive of past seasons" bullet.

**Why.** Historical depth is the textbook hooked-user upsell: worthless on
day one, valuable by the time a student is on their second recruiting
cycle. Zero wedge damage.

**Enforceability.** Same status as Move 3: already claimed on the live Pro
card, unbuilt, covered by the "In the works" badge.

### Explicitly NOT moved

- **The whole opportunity board** (roles, filters, full-posting reads,
  eligibility, sponsorship, deadline fuses): the wedge. Stays free and
  generous, live counts intact.
- **The Today queue and the Network board**: the moat's front door. Free
  users building CRM data is the lock-in; gating it would starve the moat.
- **CSV import, export, and instant deletion**: data rights. The honesty
  section promises "export or delete everything, on any plan, at any
  time" and that promise is load-bearing for trust. Never gated.

## 3. Free card — exact copy

Tagline changes from "The full product. No card, no trial clock." to:

> "The whole board. No card, no trial clock."

Price line unchanged: `$0` / "forever, for what's listed here".

The feature list goes from ten bullets to eight. Template vars are the
existing ones plus `advisor_free_grant` (new, §5). Exact bullets, in
order:

1. `<b>{{ open_count }}</b> live campus roles across <b>{{ firm_count }}</b> firms hiring today`
   (unchanged)
2. `Filters across <b>{{ market_count }}</b> markets and six verticals`
   (unchanged)
3. `<b>{{ read_count }}</b> postings read in full, so deadlines and
   requirements come from the posting rather than the board's summary`
   (unchanged)
4. `Eligibility checked against your class year and visa, with
   <b>{{ sponsorship_count }}</b> roles answering sponsorship outright`
   (unchanged)
5. `Deadline fuses, rolling markers, and a label on every inferred date`
   (tightened from "…every date we inferred rather than were told")
6. `The Today queue and the Network board: warmth lanes, firm tiers,
   one-click logging`
   (merges the old bullets 6 and 7 into one line)
7. `Talk to Coverage: an advisor that reads your own contacts, firms and
   deadlines before it answers. {{ advisor_free_cap }} messages a day,
   {{ advisor_free_grant }} a month.`
   (adds the monthly ceiling; both numbers measured, never typed)
8. `CSV import, export, and instant deletion`
   (unchanged)

Removed outright: "Gmail Live: connect Gmail and it logs itself" (Move 1).
Free's on-demand Gmail scan appears in the comparison table only, not in
the card — the card is the pitch, the table is the inventory.

## 4. Pro card — exact copy

Badge, tagline, price all unchanged ("In the works", "For the heaviest
recruiting seasons.", `$39` / "one cycle, ~6 months" — but see §9 on the
price). "Everything in Free, plus" kept. The list goes from five bullets
to five, rewritten:

1. `Talk to Coverage on a stronger model, with sharper judgement on where
   your week should go. Three times the credits: {{ advisor_pro_grant }}
   a month.`
   (replaces "…{{ advisor_pro_cap }} messages a day", which prints 15,
   the same number Free shows — selling sameness. The credit pool is the
   true 3x.)
2. `Gmail Live: real-time sync that logs itself. Calendar sync too, only
   if you connect it.`
   (absorbs Free's old Gmail Live bullet and Pro's old "Optional Gmail
   and Calendar sync" bullet into one anchor feature)
3. `Hourly refresh on your Tier 1 firms, instead of every six hours`
   (unchanged)
4. `LinkedIn contact import`
   (unchanged)
5. `Multi-cycle archive of past seasons`
   (unchanged)

CTA block unchanged ("Coming Soon" / "Announced in the app first.").

## 5. View changes (`core/views.py::pricing()`)

Two new context keys, read from the credit system like everything else on
this page (measured, never typed):

```python
"advisor_free_grant": billing_credits.plan_config(
    SimpleNamespace(plan="free"))["monthly_grant"],
"advisor_pro_grant": billing_credits.plan_config(
    SimpleNamespace(plan="pro"))["monthly_grant"],
```

(Worth a tiny helper next to `_advisor_daily_cap` so the SimpleNamespace
trick lives in one place.) `advisor_free_cap` / `advisor_pro_cap` stay;
the Free card still uses the free daily cap, and the pro one is no longer
rendered but is harmless to keep in context.

## 6. The comparison table — full spec

### Placement and framing

Inside `.panel-ind` only, directly after the `.price-grid` div closes and
before `</div>` of the panel. The Team panel is untouched. Structure:

```html
<section class="price-compare kin-reveal" style="--i:2">
  <h2>Side by side.</h2>
  <div class="cmp-scroll">
    <table class="cmp"> … </table>
  </div>
</section>
```

`h2` styled identically to `.price-honesty h2` (same font shorthand),
centered, margin `var(--s7) 0 var(--s4)`.

### Columns

Three columns: row label, Free, Pro.

- `<thead>`: first `th` empty. Second `th` text "Free". Third `th` text
  "Pro" followed by an inline badge `<span class="cmp-badge">In the
  works</span>` styled like `.plan-badge.soon` but static (no absolute
  positioning; `font-family: var(--font-mono); font-size: var(--fs-micro);
  text-transform: uppercase; letter-spacing: 0.08em; border-radius: 999px;
  padding: 2px 8px; background: var(--stale-s); color: var(--stale-t);
  margin-left: var(--s2);`).
- The one "In the works" badge on the Pro column header is the ONLY
  in-the-works marker in the table. No per-cell "coming soon" icons: the
  whole Pro tier is unlaunched, saying it once keeps cells clean and the
  page honest.

### Cell language (exactly three states)

1. **Included** — a filled check icon, `<span class="cmp-yes"></span>`:
   a 16px circle, `background: var(--accent)` with the exact white-check
   SVG data URI already used by `.plan-features li::before` (copy that
   `background:` declaration verbatim, sized `no-repeat center / 9px`),
   `border-radius: 50%; display: inline-block; width: 16px; height: 16px;`.
   Include `<span class="sr-only">Included</span>` alongside it (or
   `role="img" aria-label="Included"`) for screen readers.
2. **Limit / value** — plain text in the cell: `font-family:
   var(--font-mono); font-size: var(--fs-xs); color: var(--ink);
   font-variant-numeric: tabular-nums;`. Used when the feature exists on
   both plans at different sizes.
3. **Not included** — `<span class="cmp-no">–</span>`: a plain en-level
   dash character, `color: var(--ink-3);`, with `aria-label="Not
   included"`. No X icons; a quiet dash is the minimal-aesthetic answer.

No emoji, no illustrative icons, no fourth state. No legend needed: check,
number, dash are self-evident.

### Rows, in order

Group header rows are a single `<td colspan="3">` styled like
`.plan-includes` (uppercase, `var(--fs-xs)`, `var(--ink-3)`, letter-spacing
0.06em) with `padding-top: var(--s4)`.

| # | Row label | Free cell | Pro cell |
|---|---|---|---|
| — | **THE BOARD** (group) | | |
| 1 | Live campus roles, every tracked firm | check | check |
| 2 | Market and vertical filters | check | check |
| 3 | Deadlines read from the posting itself | check | check |
| 4 | Eligibility and sponsorship answers | check | check |
| 5 | Board refresh | `Every 6 hours` | `Hourly on Tier 1` |
| — | **THE CRM** (group) | | |
| 6 | Today queue with one-click logging | check | check |
| 7 | Network board: warmth lanes, firm tiers | check | check |
| 8 | Gmail scan on demand | check | check |
| 9 | Gmail Live: real-time, logs itself | dash | check |
| 10 | Calendar sync | dash | check |
| 11 | LinkedIn contact import | dash | check |
| — | **THE ADVISOR** (group) | | |
| 12 | Talk to Coverage | `Fast model` | `Stronger model` |
| 13 | Credits a month, chat and Gmail AI scans | `{{ advisor_free_grant }}` | `{{ advisor_pro_grant }}` |
| — | **YOUR DATA** (group) | | |
| 14 | CSV import and export | check | check |
| 15 | Delete everything, instantly | check | check |
| 16 | Season archive | `This cycle` | `Every cycle` |

Notes for the implementer:

- Row 13's numbers come from the two new context vars (§5), never typed.
- Row 5 and row 16's strings are typed as written; they mirror the Pro
  card's own bullets, which are the source of truth for those claims.
- Row 12 deliberately says "Fast model" / "Stronger model", not model
  names: model IDs churn, the page's existing voice already says
  "stronger model", and vendor names date the page.
- Do NOT add a "messages a day" row: both plans compute to 15 today and a
  row of identical numbers sells nothing. If §8's optional burst change
  ships, add row "Daily ceiling" with `15 messages` / `30 messages`
  directly under row 13.

### Table styling

- Table: `width: 100%; max-width: 860px; margin: 0 auto;
  border-collapse: collapse;` inside `.price-compare { max-width: 860px;
  margin: 0 auto; }`.
- Row separators only, no vertical lines, no outer box: `.cmp td, .cmp th
  { border-bottom: 1px solid var(--line); }` with group-header cells and
  the header row included. Horizontal rules on a bare background reads
  premium; a fully boxed grid reads like a settings page.
- Label column: `text-align: left; font-size: var(--fs-s);
  color: var(--ink-2); padding: var(--s3) var(--s2);`.
- Free / Pro columns: `text-align: center; width: 150px;`.
- Header `th`: `font: var(--w-med) var(--fs-s) var(--font-display);
  color: var(--ink); padding: var(--s3) var(--s2);`.
- Pro column tint, echoing the featured card: give every Pro-column cell
  `background: color-mix(in srgb, var(--accent) 4%, transparent);`
  (apply via `.cmp td:last-child, .cmp th:last-child`, excluding group
  header rows which span all columns).
- Responsive: `.cmp-scroll { overflow-x: auto; }` and `.cmp { min-width:
  480px; }` so under 720px the table scrolls inside its own container
  rather than squashing.

## 7. Copy-only vs. needs-code — the honesty matrix

| Claim after rebalance | True and enforced today? | Action |
|---|---|---|
| Free advisor: fast model, 15/day, 60/month | Yes (`ASSISTANT_PLANS` + `CREDIT_PLANS`) | Copy only |
| Pro advisor: stronger model, 180 credits/month | Yes (enforced for admin-set pro users) | Copy only |
| Board, filters, eligibility, fuses, Today, Network, CSV free | Yes | Copy only |
| Gmail scan on demand on Free | Yes in code (built, credit-metered AI pass); feature awaits Google Cloud setup | Copy only |
| Gmail Live real-time = Pro only | **No.** `gmail_live.is_configured()` is an all-or-nothing env flag; nothing checks `user.plan` | **Needs code** — see below |
| Hourly Tier 1 refresh (Pro) | Not built; already claimed on live page | Covered by "In the works" badge; build before Pro launch |
| Calendar sync, LinkedIn import, multi-cycle archive (Pro) | Not built; already claimed on live page | Same as above |

**The Gmail Live gate (fast-follow code task, not part of this page
change).** Before Gmail Live is ever configured on a real deploy, plan-gate
the real-time path in `capture/gmail_live.py` and the connect flow:

- At watch registration: only call `register_watch` for `user.plan ==
  "pro"` (connect itself stays open to all plans, because Scan Now needs a
  connection). `renew_watches` skips non-pro connections; `
  process_notification` drops notifications for non-pro users defensively.
- Settings UI: the connect block shows the real-time toggle as Pro-gated
  for free users, with Scan Now available to everyone.

Sequencing makes the page honest in every state: today Gmail Live is
unconfigured everywhere, so neither tier has it and the Pro card's "In the
works" badge covers the claim; the gate must simply land in the same
release that turns Gmail Live on. If for any reason Gmail Live goes live
before the gate exists, soften Pro bullet 2 to "Gmail and Calendar sync,
only if you connect it" and return Gmail Live to the Free list — do not
ship a page that gates in copy what the code gives everyone.

## 8. Optional code change — make Pro's daily ceiling real

One-line settings change, flagged for the founder rather than assumed:
raise `CREDIT_PRO_DAILY_BURST` from 45 to 90. Pro's daily ceiling becomes
30 Sonnet messages vs Free's 15. Monthly exposure is unchanged (the
180-credit grant still bounds the month at $3.60 worst case; the burst is
an abuse guard, not a grant), so the margin math in
docs/credit-system-plan.md §3 is untouched. If shipped, add the "Daily
ceiling" table row per §6. Until then, no page copy may claim a higher
daily allowance for Pro.

## 9. Flag — the $39 cycle price vs. the governing ratio

Not this plan's call, but the numbers must be on the record. The credit
plan's governing ratio (§3 there): **grant ≤ 15 credits per dollar of
price** to hold ≥ 70% margin at full burn. The page prices Pro at $39 per
~6-month cycle ≈ $6.50/month; the enforced grant is 180 credits/month:

- 180 credits / $6.50 ≈ **27.7 credits per dollar** — nearly double the
  ceiling.
- Full-burn margin: $3.60 spend vs $6.50 revenue ≈ **45%**, below the 70%
  floor (though typical ~35% burn still yields ≈ 80%).

Options, in order of preference:

1. **Keep $39, accept the 45% full-burn floor for launch.** $39/cycle
   dramatically undercuts every comp (Huntr ~$27/mo committed, Simplify+
   ~$30/mo) and the grant is env-tunable (`CREDIT_PRO_MONTHLY_GRANT`) the
   moment real burn data says otherwise. Recommended for the pre-launch
   story.
2. Reprice the cycle at $69–72 (the doc's $12/month, billed per cycle),
   restoring the ratio exactly.
3. Cut the Pro grant to ~97/month at $39 — not recommended; it guts the
   "three times the credits" pitch.

Whichever way it lands, the pricing page and credit-system-plan.md should
stop disagreeing about Pro's price.

## 10. Supporting copy — the page around the cards

These lines currently contradict the rebalanced story and must change in
the same edit:

- **Hero title**: "Free while we earn the right to charge." →
  `"The board is free. The engine is Pro."`
- **Hero sub**: "Everything Coverage does today is included." →
  `"Every listing, every deadline, free for everyone. Pro adds sync,
  speed, and a stronger advisor."`
- **FAQ 1 answer**: drop the Gmail Live mention. New text: `"No. The feed
  is public. An account adds the CRM: the Today queue and the Network
  board."`
- **FAQ 2 answer** ("Will the free plan shrink…"): the current text
  promises "Everything listed in Free today stays in Free," which this
  rebalance would break if it had ever shipped to users. It has not (the
  page is local-only, pre-launch, zero users), so the promise resets to
  the new list — a deliberate founder call, recorded here. New text:
  `"No. This list is the floor. Free may grow; it will not shrink. Pro is
  for sync, speed, and heavier AI use, the things that cost real money to
  run."`
- **Honesty section**: unchanged. "Paid tiers will exist because syncing
  inboxes and serving campuses costs real money to run" was already the
  right sentence; after Move 1 the page finally agrees with it.
- **Team / Enterprise panel**: untouched, per scope.

Copy style check (applies to every quoted string above): no em dashes,
short declarative sentences, concrete measured numbers, no corporate
fluff. All numeric claims render from context vars except the two
cadence/archive strings in the table, which mirror the Pro card's own
bullets.

## Build order

1. `pricing()` context: add `advisor_free_grant` / `advisor_pro_grant`.
2. Rewrite the two Individual cards and supporting copy (§3, §4, §10).
3. Add the comparison table and its CSS (§6).
4. Verify: `pytest coverage_web/core` (full-suite run has the known
   pre-existing connection-leak issue; verify per-app).
5. Fast-follow (separate task, before Gmail Live configuration): the plan
   gate in `capture/gmail_live.py` (§7). Optional: the Pro burst raise
   (§8) plus its table row.
6. Founder decision: the $39 price flag (§9).
