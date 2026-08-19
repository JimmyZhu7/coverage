# Founder decisions, 2026-08-20

Three calls made on Jimmy's behalf ("use fable to decide for me for decisions
only you can make"). Inputs: today's PM review and customer walkthrough,
`docs/credit-system-plan.md`, `docs/pricing-rebalance-plan.md`,
`templates/core/pricing.html`, `settings/base.py::CREDIT_PLANS`, and live
counts pulled from the local directory DB while writing this.

Constraints respected: the Pro trial mechanics (`PRO_TRIAL_DAYS`,
`PRO_TRIAL_TRIGGER`, the "Notify me when Pro opens" waitlist), the Gmail Live
plan gate (real-time watch Pro-only, Scan Now on Free), and mid-period
upgrades granting the credit difference immediately are all being built in
parallel. Nothing below decides whether they exist; it sets the numbers and
the words they run with.

Planning constants used throughout (from the credit plan, §1 and §3):
1 credit = $0.02 of model spend; a plan is healthy at **≤ 15 credits per
dollar of monthly price**, which is **≥ 70% gross margin at full burn**.

---

## 1. Team tier economics

**The call: Team is a flat club workspace on top of Free accounts, and Pro is
bought per seat at the individual price; credits are never pooled and never
discounted.**

### Why

The $249/yr "up to 25 Pro-grade seats" preview sold $1,080/yr of worst-case
model spend for $249. The mistake was bundling the one thing that has a
per-user marginal cost (credits) into the one thing that does not (shared
boards). What a club officer actually buys is the collaboration layer: shared
firm tiers, the club-wide coverage board, "which firms do our members already
reach", a shared deadline calendar, officer-managed membership. None of that
calls a model. So price that flat, and let Pro stay what it already is: an
individual's credits and sync, with the same margin whether a club or a
student pays.

Pooling was considered and rejected. A pool that clears 70% at $249/yr is
311 credits a month for the whole club (about 100 Sonnet messages across 25
people), which is too thin to call "Pro" and needs a second ledger to build.
Per-seat bands were rejected because the pricing FAQ already promises clubs
"not per-seat billing to negotiate" for the workspace, and that promise is
the right one for the collaboration layer.

### The numbers

| Item | Price | What it is | Model spend exposure | Margin |
|---|---|---|---|---|
| Team workspace | **$299 / year, flat**, up to 25 members | Shared tiers, club coverage board, shared calendar, officer-managed seats. Every member is a Free account. | Zero beyond the Free grants those members would burn anyway (25 × $1.20 × 12 = $360/yr worst case, ~$120/yr typical at 30–40% burn) | Typical: +$179/yr on Free spend alone. Worst case: −$61/yr, and only if all 25 burn every Free credit every month. |
| Pro seat (club-billed) | **$69 / cycle per seat**, same as individual | Identical to individual Pro: 180 credits/mo, Sonnet, Gmail Live | $3.60/seat/mo at full burn | 69%, same as individual Pro (Decision 2c) |
| Larger clubs | 26–60 members: **$599 / year** | Same workspace | Same shape | Same shape |
| Founding clubs | First 5 clubs: workspace free for year one | Go-to-market lever from the product brief, capped in time instead of "forever" | As above | n/a |

Why $299 and not $249: $249 was the number on the page; $299 is the number
at which the Free-credit worst case of 25 members is close to covered
(−$61 instead of −$111) with zero Pro seats sold, and the product brief's own
range for a club edition was $300–500. No finance society's budget line
moves on $50.

Why no volume discount on Pro seats: any discount below $69 on a 180-credit
grant breaks the 15 credits/$ ceiling (at $59/cycle it is 18.3/$ and a 63%
floor). The club's discount IS the workspace: one flat fee covers everyone,
and the officers buy Pro for the five people actually running outreach.
"Talk to us" above 10 Pro seats; do not publish a band.

### The pitch, one sentence

> Every member joins free; the club pays $299 a year for the shared
> coverage board, and Pro seats cost the same $69 a cycle whether the club
> buys one or twenty.

### Trade accepted

A club that wants 25 people on Pro pays $1,725/cycle, which is expensive
next to the old $249 and will lose the rare whole-club Pro deal. That deal
was the one losing money. The model sells what clubs actually use (the
shared board) and keeps Pro's economics identical everywhere, so there is
exactly one margin to watch.

### Ship

Pricing page, Team panel: **stays on the page**, "In the works" badge kept,
but the card is rewritten now so no officer ever sees the old model:

- Price line: `$299` / `per year, flat · up to 25 members`
- Includes line: `Every member on Free, plus` (not "Everything in Pro, plus")
- Bullets (four, in order): `Shared firm tiers and a club-wide coverage
  board` / `See which firms your members already reach` / `Member seats
  managed by club officers, no per-seat billing for the board` / `Pro for
  the people running outreach: $69 a cycle per seat, billed to the club`
- CTA: replace the dead "Coming Soon" with the waitlist being built:
  `Run a club? Notify me` (the same "Notify me" mechanic, tagged `team`).
- FAQ 3 answer: `Shared tiers, shared coverage, and one flat $299 a year an
  officer manages. Members are free accounts; Pro is per seat at the
  individual price, billed to the club if you like. If you run a finance
  society, we want your input before it ships.`

Docs: `docs/product-brief.md` lines 80–82 (Club Edition $300–500/yr,
"free forever") become this model (workspace $299/yr, founding clubs free
for year one). No settings change; no credit mechanics change.

---

## 2. Free vs Pro, and the trial

### 2a. Trial

**The call: 14 days, triggered by connecting Gmail, one per account.
`PRO_TRIAL_DAYS = 14`, `PRO_TRIAL_TRIGGER = "gmail_connect"`.**

What the student must witness before paying is the loop closing without
them typing: send outreach, a reply lands in Gmail, it logs itself, the
contact moves warmth, the Today queue changes. The product's own cadence
puts the first follow-up 6 business days after the first touch, and cold
replies take 2–6 business days. Seven calendar days from connect is not
long enough for a student who connects on a Tuesday and sends five emails
on Thursday to see that happen even once. Fourteen covers one full
send-reply-follow-up cycle for almost everyone.

Rejected: "until 3 replies have logged themselves" (unbounded; a student
with no outreach sits on Sonnet forever and never sees a trial end), and
cycle-aligned trials (same problem, larger). If the mechanics can carry it
cheaply, the trial-end notice should count what was witnessed: "In two
weeks Coverage logged N replies and M confirmations for you." That number
is the upsell.

Cost of 14 vs 7: bounded by the Pro monthly grant either way ($3.60 worst
case per trial, ~$1 typical); the Gmail watch itself is near free. Fine.

### 2b. What moves

**The call: nothing currently Free moves to Pro. Two Free-limits land
instead: the coffee-chat brief becomes credit-metered (1 credit, any plan),
and Free's on-demand Gmail scan is limited to once every 7 days.**

Candidates and what each would cost:

| Candidate | Decision | Why |
|---|---|---|
| Advisor taking actions (log, park, add contact) | Stays Free | It is the CRM's front door; gating it starves the moat and makes Free's advisor feel broken. Reviewer called it the best moment in the product. Adoption cost of gating: high. Conversion gain: small, since Haiku already gives good answers. |
| Coffee-chat brief | Stays Free, **1 credit per brief** | It is a user-triggered model call (`crm/ai_brief.py` via `complete_text`). Charging it is honest and makes "Free-limited" true through the pool that already exists. 60 credits a month still buys dozens. The credit plan's "deliberately not metered" list shrinks by one; the daily brief and title calls stay unmetered (product surface, not user-triggered). |
| Sonnet vs Haiku | Unchanged (Pro = stronger model) | Not the pitch. Keep it as the second bullet, not the first. |
| Gmail scan on demand (Free) | Stays Free, **once per 7 days** (`GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7`; Pro: no limit, plus real time) | The rescan cron ticks every 15 minutes, so a Free user who presses Scan Now each morning gets near-daily sync and Pro's "real time" is worth about the button. Weekly catch-up on Free is still more capture than Free has ever had (today it has none), and it makes the Pro delta a sentence: Free catches up when you ask, once a week; Pro listens all day. This is a new parameter, not in flight; flagged for veto below. |

The conversion problem was never that Free is too rich. It is that Pro's
anchor was dark and untrialable. Fix that (the gate, the trial, Gmail Live
configured), keep the aha moments free, and let the trial sell the sync.

### 2c. Pro price

**The call: keep $69 per cycle (~6 months). No monthly option at launch.**

Margin accepted: 180 credits/mo against $11.50/mo is 15.7 credits/$, a
full-burn gross margin of **68.7%**, 1.3 points under the 70% planning floor.
Typical burn (35%) is ~89%. The exact-ratio price is $72 ($12.00/mo, 15.0/$,
70.0%); $3 of margin at the theoretical ceiling is not worth a price that
reads like a rounding error. If real burn data ever says otherwise, move
`CREDIT_PRO_MONTHLY_GRANT` to 170, never the price.

No $12/month plan alongside: a monthly option under the cycle price
cannibalises the 6-month commit that front-loads cash, and "per cycle" is
how students think about a season.

### 2d. The lead sentence

**Hero title:** `The board is free. The sync is Pro.`

**Hero sub:** `Every listing, every deadline, the queue and the board: free.
Pro connects Gmail and Calendar so replies, confirmations and interviews
log themselves.`

Why: the customer reviewer's exact objection was "the engine I used was
free". The new title names the one thing Pro is for, and the sub says what
it does in the student's own nouns.

### Pro card, rewritten to match

- Tagline: `For when the replies start coming in.`
- Price line unchanged: `$69` / `one cycle, ~6 months`
- Bullets, reordered (Gmail Live is the anchor, so it goes first):
  1. `Gmail Live: replies and confirmations log themselves, in real time.
     Calendar sync too, only if you connect it.`
  2. `Talk to Coverage on a stronger model, with sharper judgement on where
     your week should go. Three times the credits: {{ advisor_pro_grant }}
     a month.`
  3. `Hourly refresh on your Tier 1 firms, instead of every six hours`
  4. `LinkedIn contact import`
  5. `Multi-cycle archive of past seasons`
- CTA: the waitlist button being built, with note: `Try Pro free for 14
  days the moment you connect Gmail.`

Comparison table, two rows change:

| Row | Free | Pro |
|---|---|---|
| Gmail scan on demand | `Once a week` | `Any time` |
| Gmail Live: real-time, logs itself | dash | check |

### Ship / docs that must change

- `PRO_TRIAL_DAYS` default 7 → **14** in the in-flight settings.
- New setting `GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7` (env-overridable like the
  credit numbers), enforced where `capture/views.py::gmail_rescan` already
  refuses a second queued rescan; Pro and trial users bypass it.
- Coffee-chat brief: add `spend_brief` (1 credit) to the credit table;
  `crm/ai_brief.py` calls `billing.credits.can_spend/spend` like the chat
  path does.
- `docs/credit-system-plan.md`: every "$12/month" (§2 read-on-the-market,
  §3 chosen numbers and margin table, §4 allocation table, §8, §9 revenue
  line, build order 6) → "$69 per ~6-month cycle (≈ $11.50/mo)"; §3 table
  recomputed on $11.50 (full burn 68.7%, heavy 81%, typical 87–91%); §1's
  "deliberately not metered" list drops the coffee-chat brief.
- `docs/pricing-rebalance-plan.md` §9: mark resolved ("$69/cycle, 69% floor
  accepted, 2026-08-20").
- `docs/product-brief.md` lines 75–83: Season Pass $39 → $69/cycle; drop the
  monthly secondary options.
- `templates/core/pricing.html`: hero, Pro card, two table rows, Team panel
  (Decision 1). The sign-in note "Coverage never reads your inbox"
  (`templates/account/_auth_providers.html`) now contradicts a Pro feature
  that reads it with consent; change to `Coverage never reads your inbox
  unless you connect it.`

---

## 3. Sponsorship data is thin

**The call: fix the data first with zero model spend (two deterministic
changes recover ~850 answers this week), then spend about a dollar of
Haiku on the residue, and reposition the promise on the three-state honesty
the product already speaks. Sequence: copy and regex now, firm-policy
plumbing next, AI pass last.**

### What the numbers actually are

Pulled from the local directory while writing this (2,435 open campus rows,
all 2,435 with cached posting text, mean 4,470 chars ≈ 1,150 tokens):

| Finding | Rows |
|---|---|
| Posting says yes / no / not stated | 13 / 118 / 2,304 |
| Not-stated rows whose text mentions sponsor, visa, work authorisation, right to work, H-1B, OPT or CPT | **946** |
| Of those, PwC Workday rows carrying a structured field `Available for Work Visa Sponsorship? Yes/No` that `classify.extract_sponsorship` does not read | **636** (487 No, 46 Yes, 103 blank) |
| Other phrasings the regex lists miss ("not offering sponsorship", "unable to provide sponsorship", "eligible to work in ...") | ~50 |
| Not-stated rows whose firm has a per-region answer in `Firm.sponsors` (58 firms hold data) that the pill already shows but the filter, the eligibility verdict and the onboarding preview counts all ignore | **319** |
| Not-stated rows with no keyword at all | 1,358 (they genuinely do not say; no extractor can help) |

So the honest ceiling is about 45% of rows answered, and "most postings
don't state it" stays true forever. The copy has to carry that; the data
work just makes the answered minority as large as it really is.

### The plan, in order

1. **Copy, now (zero cost).** The onboarding work-auth preview and the
   pricing bullet already count only posting-stated answers. Reframe to
   three states everywhere the number appears:
   - Onboarding preview metric: `{{ answered }} of {{ count }} answer the
     visa question` stays, but the bars become four: `Posting says yes` /
     `Posting says no` / `Firm policy known` / `Not stated`, and the footnote
     reads: `Most postings never say. Coverage shows you the ones that do,
     tells you when it is the firm's policy rather than the posting, and
     scores the rest as neutral, never as a guess.`
   - Pricing Free bullet: `Eligibility checked against your class year and
     visa, with <b>{{ sponsorship_count }}</b> roles answering sponsorship
     outright and the rest marked not stated, never guessed`.
   - Landing page: no change needed; it makes no sponsorship count claim.
2. **Regex, this week (zero model spend, +~580 answers).** Teach
   `classify.extract_sponsorship` the structured Workday field (`Available
   for Work Visa Sponsorship?\s*(Yes|No)`) and the ~50 missed phrasings.
   Stated answers go from 131 to roughly 710 (29%). Re-run over cached text
   via the existing `extract_facts` sweep; no new command.
3. **Firm policy plumbing, next (zero model spend, +319 answers, labelled).**
   `directory/views.py::_apply_sponsorship_filter`, the `visa_out`
   eligibility verdict, and `accounts/onboarding_preview.py::work_preview`
   all read `opp.sponsorship` only; `_sponsorship_tag` already falls back to
   `Firm.sponsors[region]`. Make the fallback a first-class fourth state
   (`firm_yes` / `firm_no`) that the filter includes and the preview counts,
   with a distinct pill (`Sponsors · firm policy` / `No sponsorship · firm
   policy`). Firm-level "no" is a warning chip, not a blocking verdict: a
   policy is less certain than the posting, and the product's rule is never
   to block on a guess. Answered rows reach ~1,030 (42%).
4. **AI pass, last (about $1 one-time, ~$0.50/week after).** Run a grounded
   Haiku extraction over the keyword-hit residue only (946 − 636 PwC ≈ 310
   rows today): 310 × ~1,500 input tokens ≈ 0.47M tokens ($0.47) + ~80
   output tokens each ($0.12). Same shape as `directory/ai_extract.py`'s
   deadline pass: exact-quote grounding, founder-run `--limit`-able command
   (`extract_sponsorship_ai`), never the free sweep. Expected yield 100–200
   more answers. Running it over all 2,304 not-stated rows would cost ~$4.40
   and return nothing from the 1,358 keyword-free rows, so do not.

### Trade accepted

The international student still sees "not stated" on more than half of
roles after all four steps, and competitors are not going to be beaten on
coverage; they are beaten on never lying. The bet is that "here are the
700 that say, here are the 300 where we know the firm's stance, the rest
have not said" is the sentence that segment trusts, and that trust, plus
the HK/SG/CN market coverage nobody else has, is enough to win them. The
AI spend is not the constraint; the posting text is.

### Ship / docs that must change

- `directory/classify.py`: the structured-field regex and missed phrasings.
- `directory/views.py`, `accounts/onboarding_preview.py`: the four-state
  plumbing and the firm-policy pill.
- `templates/accounts/_onboarding_preview.html`: four bars and the footnote.
- `templates/core/pricing.html`: Free bullet 4.
- `docs/credit-system-plan.md` §1: `extract_sponsorship_ai` joins the
  "founder-run, not metered" list next to `extract_deadlines_ai`.

---

## For Jimmy to veto

1. **Free Scan Now limited to once a week** (Decision 2b). New parameter,
   not in the in-flight gate work. Without it, Pro's real-time sync is worth
   roughly one button press a day.
2. **Team workspace at $299, not $249** (Decision 1). $249 works too; it
   just leaves the 25-Free-member worst case at −$111 instead of −$61.
3. **Trial at 14 days, not the 7 the mechanics currently default to**
   (Decision 2a). Seven is fine if the trial-end notice can extend once
   when zero replies were logged; fourteen is the simpler rule.
