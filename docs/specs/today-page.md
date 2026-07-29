# Today page — audit and redesign spec

*Authored 2026-07-30. Scope: `/app/` (crm.views.week, `_cockpit.html`, `_act_card.html`)
and the cadence branches that feed it. Audience: the builder implementing this; assumes
docs/product-brief.md and docs/build-plan.md are read. All numbers below were measured
against the founder account (137 contacts, 131 touches, 69 tiered firms) on 2026-07-29/30.*

The thesis this page must serve: "Everyone tracks deadlines. Nobody tracks the
relationship." Today is where the relationship layer pays off. It must answer, in one
glance: **who do I contact, why, right now** — for a student with ~6 hours a week.

---

## Part 1 — Audit

Findings ranked by how much they hurt the student. F1–F4 are the product-shaped ones;
F5–F10 are sharp edges.

### F1 (worst): the queue tracks everything except the relationship

Measured queue: 36 actions — 29 `follow_up`, 6 `advance`, 1 `thank_you`.

Meanwhile the 21 warmest contacts in the CRM produce almost nothing:

| warmth / thread_state | count | actions produced | why silent |
|---|---|---|---|
| chatted / chat_done | 14 | 1 (thank_you, chat was today) | 13 fell through: 12 chatted **11 days ago**, their thank-you prompt expired at day 7 (`thank_you_expires_after_days`), and cadence branches 4–7 have no case for `chatted` — dead end flagged CRITICAL in the earlier domain audit |
| advocate / advocate | 2 | 0 | last "touch" is a `manual_override` **audit row** written 4d ago when they were promoted — a bookkeeping row reset the advocate idle clock (branch 5 reads the last touch of *any* kind) |
| replied / chat_scheduled | 5 | 0 | correct that they aren't nagged (chat is upcoming), but the page shows *nothing* about them — no "coming up" visibility, and we store no chat datetime |

So the people who replied, met, or advocated are invisible, while 29 strangers who
ignored one cold email fill the screen. Worse, the 7 warm actions that *do* exist sort
**last**: all 36 actions share priority 1, the sort is `(priority, tier, firm_name)`,
and the warm contacts sit at tier-3/unranked firms (Jefferies, "USC" school contacts).
Verified on the live page: positions 1–29 are cold follow-ups at Citi → Goldman → HSBC
→ J.P. Morgan → Morgan Stanley → UBS → Moelis; the six "Propose a chat" cards and the
thank-you are positions 30–36, below the fold. A replied human at USC outranks nothing;
a cold non-replier at Citi outranks everyone.

**Answer to audit Q3 (what the silent 20 are owed):** a chatted contact is owed a
periodic real touch (an update, an article, a question — the same "fresh reason to
talk" the thank-you-expiry docstring already names) on a keep-warm clock like the
advocate one. An upcoming-chat contact is owed *visibility* (prep, not a nag). An
advocate is owed the existing 4-week maintain — which works, but must stop being reset
by audit rows. Concrete rules in Part 3C.

**Answer to audit Q4 (ordering):** `(priority, tier, firm_name)` is not defensible for
a page whose job is relationship progress. Six of eight kinds share priority 1, so the
real sort is firm alphabet. Warmth/momentum must dominate tier; tier should break ties
inside a class; longest-silent should beat firm name. Concrete key in Part 3B.

### F2: batch in, batch out — the queue has no concept of a day

All 29 follow-ups sit at 8–9 business days silent; 27 at exactly 9, because 31 cold
emails went out on 2026-07-16 in one sitting (outreach by day: 07-16: 31 · 07-19: 3 ·
07-22: 13 · 07-24: 41 · 07-25: 8). The engine is faithful: a batch in produces a batch
out, forever, in waves. The 41 sent on 07-24 come due as a second ~40-card wall around
08-05, and after `park_after_business_days` the same cohorts return as a park wall
(83 cold/no_reply contacts are in the pipeline for it).

**Answer to audit Q1/Q2:** a flat list of 36 is the wrong shape. A student with 6
hours a week can do 3–5 real touches a day; a wall of 36 identical cards converts to
either mass one-click "Sent" spam (dishonest data, burned contacts) or abandonment
(back to the spreadsheet). The queue needs a daily plan sized from `weekly_touch_goal`,
with the remainder *visible but held* — the exact pattern every cold-email tool uses
(daily send caps) and Linear uses for cycles (capacity + honest rollover). Formulas in
Part 3B. Held-back items must be shown as a count with a reason, never silently
dropped — this audience checks the product against its own spreadsheet.

### F3: the card never says why

`_build_actions` computes and even prose-polishes `a["reason"]` via `_sentenceize`,
and `_act_card.html` **never renders it**. The card shows firm, warmth chip, name,
role, and a verb badge. "No reply 9 business days after touch 1" — the single piece of
context that makes the ask legitimate — is thrown away. A queue that gives orders
without reasons reads as arbitrary to an audience that already trusts its spreadsheet
more than any pitch.

Also missing from the card: when you last touched them and how, whether their firm has
a confirmed deadline near, and whether an opener draft exists (Compose silently sends
an empty body when `opener` is blank).

### F4: the pace ring measures rows, not work

This week's ring reads 9/14. Composition, measured: 6 `chat_scheduled` + 2
`reply_received` + 1 `chat`. **Zero outbound notes were sent**, and two of the nine
are the *other person's* action (inbound replies). `manual_override` audit rows (state
corrections, promotions) also count. The ring can fill while the user does nothing —
the same class of over-claim as the "New" badge that meant "we imported it". The goal
`weekly_touch_goal` exists and is honest; the numerator is not.

### F5: one-click "Sent" over-claims

"Sent" logs an outreach/follow_up touch in one click, with no compose having
happened. It *invites* fabricated data: click "Sent" on 29 cards and the CRM believes
29 follow-ups went out, warmth clocks reset, and the queue is "clear". The honest flow
is compose-first, log-second, and the log verb should say what it is ("Log it" — an
attestation you did the thing, possibly outside Coverage), not "Sent".

### F6: the three lanes are fake

`_cockpit_lane` maps priority 0 → Overdue, 1 → Due Now, 2+ → Keep Warm. Six of eight
action kinds are priority 1, so in practice everything lands in one giant "Due Now"
lane (live page: Due Now 36, other lanes absent). The lane names also lie ("Keep
Warm" is actually "priority ≥ 2", which is `maintain` and `park` — parking is not
keeping warm). Lanes should be semantic (what kind of work), not a priority-number
echo.

### F7: page real estate is spent on the wrong layer

Above the fold at 1440×800: hero card (~190px), then four directory stat cards (Open
Now / Closing Soon / Tracked Live / Application Funnel — the funnel reads 0 › 0 › 0).
The actual queue starts below the fold. Those stats are the *commodity* layer
(deadlines); Today is the *moat* layer (the relationship). They belong on the page but
not above the queue. The stat count-up animation also renders wrong numbers for ~2s
(screenshots captured 80, 152, and 886 for the same "Open Now" figure).

### F8: Snooze/Skip hide the whole contact, including deadline work

`snoozed_until` filters the contact out of `_build_actions` entirely. Snoozing a
follow-up card also suppresses a priority-0 pre-deadline `reping` that might fire
inside the snooze window — the highest-value nudge in the engine, silently eaten.

### F9: no bulk affordance for the coming park wall

When the 07-16 and 07-24 cohorts age past `park_after_business_days`, dozens of
priority-3 "Park it" cards will land at once. One-by-one parking of 30 cards is
make-work; it needs a grouped strip with a bulk action.

### F10: small honesty/paper cuts

- "5 more **touchs** to hit your weekly goal" — `touch{{ pace.remaining|pluralize }}`
  yields "touchs"; needs `pluralize:"es"`.
- Ada Lovelace appears twice (Debrief card + Send thank-you card) for the same chat —
  legitimate (two different asks) but uncoordinated; the cards should acknowledge each
  other or merge.
- Recent Activity timestamps render as "1 hour, 3 minutes" (`timesince` default two
  units) — noisy; one unit is enough.
- "USC" renders in the firm slot for school contacts — it's `firm_text`, fine, but the
  card gives no hint these are alumni rather than bankers.

**Answer to audit Q6 (what's on the page that shouldn't be / missing):** shouldn't be
above the queue: the four stat cards, the 0/0/0 funnel, the count-up animation.
Missing entirely: reasons on cards (F3), last-touch context, a daily cap with an
honest remainder (F2), the warm states (F1), upcoming-chats visibility, deadline chips
on cards, bulk park, an end-of-day "done" state, and any connection between the queue
and the pace ring (the ring should fill as the plan clears).

---

## Part 2 — Research

How the best "what do I do today" surfaces handle a big backlog + small daily
capacity + mixed urgency:

- **Superhuman (split inbox, inbox zero).** The inbox is treated as a task list split
  into intentional sections; triage is a three-way verb — today, another day (a
  scheduled reminder), done. The endpoint is an explicit "done for the day" state, not
  an empty database. Pattern: *semantic sections + per-item defer + a reachable
  daily zero.* ([Superhuman help: Achieve Inbox Zero](https://help.superhuman.com/hc/en-us/articles/45295217605523-Achieve-Inbox-Zero),
  [Custom Split Inbox](https://help.superhuman.com/hc/en-us/articles/38458483333907-Custom-Split-Inbox))
- **HubSpot task queues.** Reps don't scroll a list; they hit "Start queue" and the
  system feeds one task at a time, next loading on completion. Queue-mode reps
  complete 30–40% more tasks per hour than list-pickers. Pattern: *the surface's job
  is to pick the next item, not to display all items.* ([LeadCRM guide to HubSpot task queues](https://www.leadcrm.io/hubspot-troubleshooting-guide/task-queues/),
  [Supered: HubSpot tasks](https://www.supered.io/blog/hubspot-tasks/))
- **Dex (personal CRM).** Per-contact keep-in-touch frequency (monthly, quarterly,
  etc.); the reminder is anchored to the last logged interaction and pushes forward
  automatically when you log a new one. This is the closest analogue to warmth decay
  and is exactly the missing `chatted` branch. ([Dex: Keep-in-Touch guide](https://getdex.com/docs/workflows/keep-in-touch))
- **Clay.earth.** "Reconnect cadence" per contact; surfaces people "going cold" —
  people you used to talk to who've gone quiet. Same shape: recency-of-relationship
  drives the nudge, not campaign mechanics. ([Muncly Clay review](https://muncly.com/clay-earth-review-is-this-an-end-game-personal-crm/))
- **Cold-email tooling (lemlist/Instantly).** The industry answer to "batch in" is a
  hard **daily send cap** (~30–40/day per inbox, ramped), with the tool spreading a
  batch over days automatically; the remainder is visibly "scheduled", not hidden.
  Direct precedent for capping surfaced follow-ups per day regardless of how many are
  technically due. ([lemlist: sending limits](http://help.lemlist.com/en/articles/4508367-understand-sending-limits-in-lemlist),
  [lemwarm deliverability guide](https://www.lemwarm.com/blog/how-to-boost-your-email-deliverability))
- **Todoist.** Daily goal (default 5), streaks, and overdue pressure — but crucially
  the goal counts *completions by the user*. Small default numbers are deliberate.
  ([Todoist: Introduction to Karma](https://www.todoist.com/help/articles/introduction-to-karma-OgWkWy))
- **Things / 1-3-5 style planning.** A daily list of ~3 significant items is the size
  that feels doable; lists that are structurally impossible to finish read as personal
  failure and get abandoned. ([get-alfred: the 1-3-5 rule](https://get-alfred.ai/blog/1-3-5-rule),
  [Becoming Minimalist: 3-item list](https://www.becomingminimalist.com/to-do/))
- **Linear cycles.** Capacity is derived from what actually got done recently, not
  hope; unfinished work rolls over *visibly*, and the 2026 "Cycle Autopilot" rolls
  high-priority items automatically while demoting low-priority ones — "when partial
  work stays visible instead of being buried, your delivery forecasts stay honest."
  ([Linear docs: Cycles](https://linear.app/docs/use-cycles),
  [Work Management Hub on Linear cycles](https://workmanagementhub.com/linear-cycles-sprint-planning-guide-2026/))
- **Salesloft Rhythm.** Signals (engagement, intent) are converted into a *prioritized*
  action queue per rep — momentum ranks the queue, not alphabet. ([HubSpot blog: Outreach vs Salesloft](https://blog.hubspot.com/sales/outreach-vs-salesloft))
- **Recruiting trackers (Simplify, Teal, Huntr).** All offer follow-up reminders and
  kanban stages, none solves the daily-capacity problem — their "today" is a deadline
  list. This is white space, consistent with the product brief's competitive read.
  ([Simplify job tracker](https://simplify.jobs/job-application-tracker),
  [Teal job tracker](https://www.tealhq.com/tools/job-tracker))

**Who solves "200 people to keep warm and 30 minutes"?** Nobody fully. Dex/Clay solve
the *clock* (per-relationship cadence anchored to last interaction). Cold-email tools
solve the *cap* (fixed daily quota, spread automatically, remainder visible as
scheduled). HubSpot/Superhuman solve the *session* (feed one at a time toward an
explicit done-state). The standard remainder pattern across all of them: **show the
count, name the policy holding it back, and let the user open the full list** —
never delete, never silently hide.

---

## Part 3 — Design spec

### A. Page shape

Order, top to bottom (all widths):

1. **Header strip** (compact, replaces the current tall hero): date eyebrow, "Today",
   and a one-line generated summary that is the page's thesis sentence, e.g.
   "4 to send · 1 chat to write down · 9/14 this week." Numbers link to their
   sections. No count-up animation.
2. **Debrief lane** (unchanged position and behavior — it already expires honestly).
3. **Today's plan** — the capped queue (policy in B), grouped into three semantic
   lanes (only render non-empty ones):
   - **Don't lose these** — time-critical: `reping`, overdue `thank_you`,
     `confirm_chat`. Never capped (see B).
   - **Move it forward** — momentum: `advance`, fresh `thank_you`, `keep_warm`
     (new, C), `maintain`.
   - **Cold follow-ups · N of M today** — `follow_up`, `first_outreach`, capacity-
     filled. Header carries the honest fraction.
4. **Up next** — one collapsed row: "27 more queued · pacing out at 4 a day ·
   Show all". Expanding renders the full remaining list (same cards, no quick-log
   buttons on held items is NOT required — they're real actions, just not today's).
   When >5 `park` actions exist, a separate strip: "31 gone quiet · Park all" with
   per-item undo via the existing archived/parked recovery surfaces.
5. **Stat ribbon** — the four directory stats compressed to one slim row (number +
   label, no cards), placed *below* the queue. Funnel hidden while 0/0/0
   ("Nothing submitted yet" one-liner instead). These are links out to
   Opportunities/Network, not the point of the page.
6. **Right rail** (≥1024px; stacks below main content otherwise):
   - **This week** pace ring (numerator fixed per E1).
   - **Coming up** (new): chats scheduled and not yet stale — "Chat with Grace Hopper ·
     set up 1d ago". Sourced from contacts in `chat_scheduled` whose staleness is
     ≤ 4 business days (the complement of cadence branch 2). Copy must say when it
     was *set up*, never when the chat *is* — we don't store the chat datetime (E4).
   - **Recent activity** (unchanged, but single-unit `timesince`).

**Zero states** (three distinct, current template conflates two):
- No contacts: current copy stands ("No contacts yet…").
- Plan cleared, queue non-empty: **"Done for today."** + "N more are pacing out over
  the coming days · Show all" + the pace ring delta ("that's 4 of 14 this week").
  This state is the product working, not an empty page — style it as a win.
- Queue truly empty: current "You're all caught up." copy stands.

### B. Queue policy — capacity, ordering, remainder

**Capacity (view layer, not the engine).** `cadence.due_actions` stays pure and
returns everything; Today *selects*. In `crm/views.py`:

```python
TODAY_PLAN_MIN = 3          # never plan fewer (momentum floor)
TODAY_PLAN_MAX = 12         # never plan more (6h/week reality ceiling)

def _daily_cap(user, done_outbound_this_week, today) -> int:
    goal = user.weekly_touch_goal or WEEKLY_TOUCH_GOAL
    workdays_left = # Mon–Fri days from `today` through Sunday, min 1
    remaining = max(0, goal - done_outbound_this_week)
    return max(TODAY_PLAN_MIN, min(TODAY_PLAN_MAX, ceil(remaining / workdays_left)))
```

Founder example: goal 14, 0 outbound done, Wednesday → 3 workdays left →
`ceil(14/3)` = 5 today. Behind on Friday → cap rises toward 12, self-pacing like
Linear capacity. The cap keys off the *existing* `weekly_touch_goal` (already
user-tunable in Settings) — no new setting, no second knob to drift out of sync.
`done_outbound_this_week` uses the same corrected numerator as the pace ring (E1) so
the plan and the ring can never disagree.

**Selection and ordering.** Replace the display order (view layer only — the engine's
ported `(priority, tier, firm_name)` sort and its golden fixtures stay untouched)
with:

```python
_TODAY_CLASS = {  # lower = shown first
    "reping": 0, "confirm_chat": 0,          # time-critical
    "thank_you": 1, "advance": 1,            # momentum: they gave you something
    "keep_warm": 2, "maintain": 2,           # warm upkeep (keep_warm is new, §C)
    "first_outreach": 3, "follow_up": 3,     # cold
    "park": 4,                               # bulk strip, not the plan
}
# sort key: (class, cadence priority, tier, -idle_business_days, firm_name)
```

The inversion that matters: **momentum beats tier**. A replied contact at unranked
USC now outranks a cold non-replier at Citi — the thesis, enforced in the sort key.
Within a class, tier then longest-silent-first; firm name only as the final stable
tiebreak (audit Q4).

Fill rule:
1. Class 0 items are **always** shown, all of them, even past the cap (a confirmed
   deadline or a dying chat thread is never hidden — E-rule 8 also exempts them from
   snooze).
2. Remaining slots fill from class 1 → 2 → 3 in sort order until `daily_cap`.
3. Class 4 (`park`) never occupies plan slots; it renders as the bulk strip.

**Remainder.** Everything selected-out goes to "Up next": a count, the pacing
sentence naming the policy ("pacing out at 5 a day"), and Show all. Nothing is
deleted, nothing silently vanishes; tomorrow the oldest-silent items fill first, so a
07-16 batch drains in FIFO order over ~6 weekdays.

**Tunables.** No new `TUNABLE_CADENCE_PARAMS` keys for capacity — the cap derives
from `weekly_touch_goal`. New cadence key (C) is whitelisted:
`chatted_touch_min_weeks: (1, 52)`.

### C. New cadence branches (coverage_domain/cadence.py)

All three changes need: a DIVERGENCE entry in the module docstring (same dated style
as the existing ones), golden-fixture tests, and updated branch numbering comments.

**C1. `keep_warm` — the chatted dead end (the CRITICAL from the domain audit).**

New params in `CADENCE_DEFAULTS`:

```python
"chatted_touch_min_weeks": 3,   # keep chatted contacts warm every 3–5 weeks
"chatted_touch_max_weeks": 5,   # display range only, like the advocate pair
```

New branch, inserted **between branch 5 (advocate maintain) and branch 6 (cold
cadence)** — i.e. it is reached only after branch 1 fell through (thanked or
expired), branch 3 declined (no confirmed close near), branch 4 declined (not
parked/quiet):

```
state:      warmth == "chatted" AND thread_state == "chat_done"
condition:  idle >= chatted_touch_min_weeks * 7 days since last REAL touch
            (or no dateable touch on record — same convention as branches 2/5/6)
->  action   "keep_warm"
    priority 2
    reason   f"chatted {days}d ago — send an update or a question
             (target every {min}–{max} weeks)"
    ctx      days_since, target_min_weeks
```

Reason copy follows the advocate branch's rule: render the range from params, never
hardcode. Web layer additions: `ACTION_LABELS["keep_warm"] = "Keep warm"`,
`_ACTION_TOUCH["keep_warm"] = "maintain"` (logs an existing kind;
`TOUCH_TRANSITIONS["maintain"] == (None, None)` so no pipeline change and the state
ratchet is untouched). Effect on live data: the 12 contacts chatted on 07-18 surface
from ~08-08, staggered by the daily cap — not another wall.

*Why 3 weeks, not the advocate 4:* a chatted contact is a live referral candidate
mid-cycle; Dex's tightest common cadence is monthly, and the thank-you docstring
already frames post-chat contact as needing "a fresh reason to talk". **Genuine
uncertainty: 3 vs 4 weeks is a founder call; the param is whitelisted precisely so
the founder can tune it against his own cycle. Default to 3 unless he says
otherwise.**

**C2. Idle clocks ignore audit rows.**

```
Define REAL_TOUCH_KINDS = every kind except "manual_override".
lt_date / idle math in branches 2, 5, C1, 6, 7 reads the latest REAL touch.
Branch 1's thank-you scan and branch 3's reping scan are unchanged
(they filter by specific kinds already).
```

DIVERGENCE note: the original had no `manual_override` rows (that kind was invented
for `set_state`'s audit trail, per pipeline.py), so "any touch" and "any real touch"
were the same set there; here a promotion or state correction must not reset a
relationship clock. This un-silences the 2 advocates whose 4-week clock was restarted
by their own promotion row.

**C3. Branch 7 widened to chatted contacts who reply again.**

```
old: thread_state == "replied" and warmth in ("replied", "cold")
new: thread_state == "replied" and warmth in ("replied", "cold", "chatted")
```

A chatted contact who re-engages currently falls into no branch at all (warmth is
ratcheted to "chatted" but branch 7 only matched lower warmth). Advocates stay
excluded — branch 5 owns them. DIVERGENCE note + fixture.

**C4. Upcoming chats are NOT a cadence branch.** `replied/chat_scheduled` inside the
4-business-day window stays silent in the engine on purpose; the view-layer "Coming
up" rail (A6) provides the visibility. Rationale: the engine emits *actions*, and
"a chat exists" is not an action; also we store no chat datetime, so any engine
output would have to guess (E4).

### D. Card design

Card anatomy (all lanes, `min-height` only — this codebase has shipped fixed-height
clipping twice):

```
┌──────────────────────────────────────┐
│ FIRM/SCHOOL          [warmth chip]   │  existing
│ Name (link)                          │  existing
│ Role                                 │  existing
│ [Action label]  [Closes Aug 8]       │  label existing; deadline chip new
│ reason line                          │  NEW — render a.reason (F3)
│ Last: Followed up · 9 bd ago         │  NEW — from last real touch
│ [Compose]  [Log it]  [They replied]  │  reworked verbs
│            Snooze · Skip             │  existing ghosts
└──────────────────────────────────────┘
```

- **Reason line** (`.act-reason`, `--fs-xs`, `--ink-2`): the `_sentenceize`d string
  that already exists. One line, wrap allowed, no truncation mid-claim.
- **Last-touch line**: "Last: {kind label} · {n} business days ago", from the latest
  real touch; "No touches on record" when none. This is the card's evidence.
- **Deadline chip**: rendered only when the firm has a `confirmed_official`
  `app_close` within `pre_deadline_reping_days`, scoped by the contact's region with
  the same unknown-region fallback as branch 3 (reuse `_closing_soon`'s output —
  compute once in `_build_actions`, don't re-derive). Confirmed-only, matching the
  engine's bar: rumors never make a chip.
- **Verbs** (audit Q5):
  - Primary = **Compose** (mailto with opener body + BCC capture) whenever `email`
    exists. Compose-first is the honest flow; it also feeds the capture funnel,
    which is the product's stated open risk.
  - Secondary = **"Log it"** (renamed from "Sent" — an attestation that the thing
    happened, possibly outside Coverage; `title` text: "Record that you did this").
    Same POST as today (`today_act` `sent` + kind).
  - **"They replied"** (renamed from "Reply" — it logs `reply_received`, i.e. an
    event about *them*; "Reply" reads as "compose a reply" and mislogs).
  - `confirm_chat` cards: primary is **"Log the chat"** (links to the log-touch flow
    with kind=chat preselected), secondary "Reschedule" (logs `chat_scheduled`).
    Current mapping (one-click `sent` with kind `chat`) silently asserts a chat
    happened — too big a claim for one click; keep it a two-step.
  - `park` cards keep their dedicated verb (already correct — state change, no fake
    touch).
  - When `opener` is blank, Compose gets a small "no draft yet" hint chip instead of
    silently opening an empty email; clicking through is still allowed.
- **Accessibility**: every button carries an aria-label including the contact name
  ("Log follow-up to Ethan Gao") — the page renders 30+ identical button texts
  otherwise. Cards remain `article` with the name as the heading.
- School contacts (firm_id NULL, `firm_text` like "USC"): chip style variant so the
  slot doesn't impersonate an employer ("USC · alum").

### E. Honesty rules — what the page must NOT imply

1. **The pace ring counts only the user's own work.** Numerator = touches this week
   with kind in `{outreach, follow_up, thank_you, reping, maintain, chat,
   chat_scheduled}`. `reply_received` (their action) and `manual_override` (audit
   row) never count. Uncertainty flag: whether `chat_scheduled` counts is arguable
   (it is user work — scheduling — but cheap); default IN, revisit with data.
2. **No count without its denominator.** A capped lane header must read "N of M
   today"; "Up next" must carry the exact held-back count and the pacing policy in
   words. Never render a total that mixes shown and hidden.
3. **Held is not gone.** "Show all" must always expand to the complete queue; the
   cap is a pacing device, not a filter. (The audience will diff this page against
   their spreadsheet. Let them.)
4. **Never claim a chat time we don't store.** "Coming up" says "set up {n}d ago",
   never "chat tomorrow". If a chat datetime field ships later, this rule retires.
5. **No auto-logging from Compose.** Clicking Compose must never write a touch — a
   mailto is not a send. Only "Log it" / "They replied" / debrief flows write.
6. **Never a second follow-up.** The cap/ordering work must not resurrect held
   follow-ups into extra sends: a held `follow_up` surfacing days later is still
   follow-up #1-and-only for that contact (`max_cold_touches` stays capped at 2).
7. **Numbers don't animate through false values.** Drop the count-up, or animate
   only over <300ms from 0 in a way that can't be screenshotted as data.
8. **Priority-0 exempts snooze.** Apply `snoozed_until` filtering *after* the engine
   runs, dropping actions for snoozed contacts **except** class-0 (`reping`,
   `confirm_chat`). Implementation: stop filtering snoozed contacts out of the
   engine's input in `_build_actions`; filter the action list instead.
9. **Fix "touchs"** → `pluralize:"es"`, and the empty-plan state must say "Done for
   today", never "You're all caught up" while "Up next" is non-empty.
10. **The debrief/thank-you pair acknowledges itself.** When one contact holds both
    cards, the thank-you card gains one line: "Debrief first. It writes your
    thank-you for you." (or simply order the debrief card first in its lane, which
    the layout already does). Per the copy style, UI strings carry no em dashes;
    engine reason fragments may keep them because `_sentenceize` converts them to
    sentence breaks before render.

### F. Responsive + accessibility (375 / 768 / 1024 / 1440)

- **375**: single column; act-grid 1-up; stat ribbon becomes a 2×2 compact grid or
  horizontal scroll with `overflow-x: auto` on its own container (never the page);
  rail stacks below the queue (pace ring first); quick-action buttons full-width row,
  wrap to two rows before shrinking below 44×44pt tap targets.
- **768**: act-grid 2-up; rail still stacked below; header strip and lanes full
  width.
- **1024**: cockpit goes two-column (main + rail) as today; act-grid 2-up.
- **1440**: act-grid 3-up max (never 4 — the reason line needs measure); page
  container keeps its existing `page--wide` max-width.
- Cards: `min-height`, never `height` (repeat of the standing constraint — two
  shipped clipping bugs).
- Lanes are `section`s with real headings (already true); add `aria-live="polite"`
  on the lane-count spans so htmx swaps announce ("Cold follow-ups, 2 of 29 today").
- The pace ring keeps `aria-hidden` on the SVG with the adjacent text as the
  accessible value; the header-strip summary line doubles as the page's
  `aria-describedby` target.
- `kin-reveal` stagger and the ring fill respect `prefers-reduced-motion: reduce`
  (no transform/opacity entrance, ring renders at final state).
- Color: warmth chips and lane dots must not be the only signal — each carries its
  text label already; keep it that way. Contrast for `--ink-3` on `--paper` at
  `--fs-micro` must clear WCAG AA for the new fraction/pacing copy; if not, use
  `--ink-2`.
- Template hygiene: any multi-line template comment uses `{% comment %}` (guards:
  `accounts/tests/test_template_comments.py`, `directory/tests/test_styles_block.py`).

### G. Ordered implementation checklist

Each step ships green on its own; order minimizes rework.

1. **Fix the pace numerator** (E1) + the "touchs" typo (E9). Pure view/template;
   add a view test asserting `reply_received`/`manual_override` don't count.
2. **Render the reason + last-touch lines on `_act_card.html`** (D). `a["reason"]`
   already exists; add last-real-touch to the action dicts in `_build_actions`.
3. **Rename verbs** ("Sent" → "Log it", "Reply" → "They replied") + aria-labels +
   `confirm_chat` two-step (D). No backend change except the confirm_chat mapping.
4. **cadence.py: C2 real-touch clocks**, docstring DIVERGENCE, fixtures. (Do C2
   before C1 so the new branch is born on the correct clock.)
5. **cadence.py: C1 `keep_warm` branch** + params + docstring + fixtures; whitelist
   `chatted_touch_min_weeks` in `TUNABLE_CADENCE_PARAMS`; add label + touch-kind
   mappings; surface the new param in Settings (it must never be a knob the engine
   ignores — that bug class already shipped once).
6. **cadence.py: C3 branch-7 widening** + docstring + fixture.
7. **View-layer ordering + daily cap + lane regrouping** (B): `_TODAY_CLASS`,
   `_daily_cap`, semantic lanes, class-0 always-shown rule, snooze exemption (E8 —
   move the snooze filter from queryset to action list).
8. **"Up next" collapsed remainder + Show all** (B), lane-header fractions (E2).
9. **Park strip with bulk park** (F9): one POST looping `services.set_contact_state`
   per contact (never a bulk UPDATE — the ratchet stays the only writer).
10. **Page reshuffle** (A): compact header strip with summary line, stat ribbon
    below queue, funnel zero-state, kill count-up (E7).
11. **"Coming up" rail card** (A6/C4) + single-unit activity timestamps.
12. **Zero states** (A): "Done for today" vs "all caught up" split.
13. **Responsive/a11y pass** (F) at all four widths; `prefers-reduced-motion`.
14. **Honesty sweep against E** as a release gate: walk E1–E10 with the founder
    dataset and screenshot each claim.

### H. MUST / SHOULD / LATER

**MUST** (the page is dishonest or thesis-breaking without these):
- E1 pace-ring numerator fix; E9 typo.
- F3 fix: reason + last-touch on cards (steps 2).
- C1 `keep_warm` branch (the CRITICAL dead end — the warmest 13 people invisible).
- C2 real-touch clocks; C3 branch-7 widening.
- B daily cap + momentum-over-tier ordering + honest "Up next" remainder.
- Semantic lanes replacing the priority-echo lanes (F6).
- Verb honesty: "Log it" / "They replied" / confirm_chat two-step (F5).
- E8 snooze exemption for class-0 actions.

**SHOULD** (clear wins, not blocking):
- Page reshuffle: header strip, stat ribbon demoted, funnel zero-state, no count-up.
- "Coming up" rail; "Done for today" state; deadline chips on cards.
- Park strip + bulk park (needed before the 07-24 cohort ages out, ~mid-August).
- aria-labels, aria-live counts, reduced-motion.
- Debrief/thank-you pairing line (E10); school-contact chip variant.

**LATER** (real, but not this pass):
- A chat datetime field on touches (retires E4's constraint; enables true "chat
  tomorrow" prep cards and calendar links).
- HubSpot-style focus mode ("Start my 5") feeding one card at a time.
- Per-contact custom keep-in-touch cadence (Dex-style override of the global
  `chatted_touch_min_weeks`).
- Streak mechanics on top of the pace ring (Todoist-style weekly streaks) — only
  after the numerator has been honest for a while.
- Opener-drafting assistance on Compose for cards with no draft.

### Open uncertainties (flagged, not papered over)

1. `chatted_touch_min_weeks` default: 3 vs 4 weeks — founder call (C1).
2. Whether `chat_scheduled` counts toward the pace numerator (E1) — default yes.
3. `TODAY_PLAN_MIN`/`MAX` (3/12) are reasoned defaults, not measured ones; revisit
   after two weeks of founder dogfood against the actual clear-rate.
4. Whether "Up next" should allow acting on held cards directly (currently: yes,
   cards are fully functional when expanded — pacing guides, never blocks). If
   founder data shows Show-all-then-mass-log behavior, reconsider.
5. The 6 `advance` contacts include 5 school alumni; if the founder treats alumni
   chats as a separate motion from banker outreach, a fourth lane ("School") may be
   warranted — no evidence yet, so not specced.
