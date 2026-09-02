# Credit system plan — metering Coverage's AI surfaces

Status, corrected 2026-09-02: **most of this is built.** The header said
"design for review, nothing built. No code or migrations exist for this yet"
while §10's own changelog, four screens down, said the top-up packs were built
and inert — two sentences in one document disagreeing about the same code
(`todo-mined.md §4`). What exists today: `billing/models.py::CreditLedger` and
its migrations, `billing/credits.py` (the per-plan grants, the daily burst,
`spend`), `billing/stripe_gateway.py`'s two one-time packs, and the Credits
card on Settings. What does not: a subscription, and any charge for Pro
itself. §10's "Stripe-shaped but inert" is still the accurate sentence for the
top-ups; the deploy has no Stripe keys and no registered webhook, so nothing
can be bought.

> **Changelog — v4, 2026-08-20 (Pro repriced to $69/cycle; coffee-chat
> brief metered).** Founder decision (`docs/founder-decisions-2026-08-20.md`
> §2c, §2b): Pro's monthly-equivalent price moves from **$12/month** to
> **$69 per ~6-month cycle (≈ $11.50/mo)**, no monthly option alongside it.
> The 180-credit grant is unchanged, so full-burn margin moves from the
> exact-ratio 70% to **68.7%** (1.3 points under the planning floor,
> accepted rather than round up to $72). Every "$12/month" below that named
> Pro's price is updated to the cycle price; the two one-time top-up packs
> in §10 ($5→60, $12→160) are unrelated to this change and untouched. Also:
> the coffee-chat brief (`crm/ai_brief.py`) joins §1's per-action table at
> 1 credit — it was never actually covered by the "daily brief" line in that
> list (that line names `assistant/brief.py`'s auto-generated `DailyBrief`,
> a different feature); this doc now says so explicitly to remove the
> ambiguity.
>
> **Changelog — v3, 2026-08-19 (pay-as-you-go top-ups, Stripe-shaped but
> inert).** Built `billing/stripe_gateway.py`: two one-time credit packs
> ($5→60, $12→160 — both priced against §3's governing ratio, see new §10)
> on top of the monthly grant this doc already describes. Stripe is NOT
> configured on this deploy yet (`STRIPE_SECRET_KEY` /
> `STRIPE_WEBHOOK_SECRET` are both blank) — every entry point gates on
> `stripe_gateway.is_configured()`, the exact pattern `capture/
> gmail_live.py::is_configured()` already established, so the whole
> feature builds, migrates, and passes its test suite with zero Stripe
> keys present and renders on Settings as a disabled "Coming soon" block
> rather than disappearing. See §10 for the full design. Unchanged:
> everything above about the monthly grant, chat/rescan metering, and the
> ledger itself.
>
> **Changelog — v2, 2026-08-19 (priced for margin).** v1 validated *cost* but
> set no Pro price; its grants were upside-down at any price a student would
> pay (1,500 Pro credits ≈ $30 of model spend — a $10/mo Pro that got fully
> used would lose $20/user). v2 sets **Pro at $12/month** and cuts grants to
> **Free 60 / Pro 180** so a fully-burned Pro month still carries **70%**
> gross margin on model spend and a typical month **~90%**. New: §2
> competitive landscape (prices researched live 2026-08-19), §3 margin
> arithmetic. Burst guards rescaled to match (Free 15/day, Pro 45/day).
> Unchanged from v1: the credit unit ($0.02 anchor), per-action credit costs
> (1 / 3 / 1-per-10-threads), ledger design, lazy grants, rollover-at-2x,
> both enforcement points, and everything out of scope.

## The problem

Coverage now has two AI surfaces a user can spend real money through:

1. **Talk to Coverage** (assistant/) — one student message triggers up to
   `MAX_ROUNDS` (8) API round-trips on the plan's model. Today this is capped
   by a per-plan *daily message count* (Free: 15 Haiku messages, Pro: 60
   Sonnet — `assistant/plans.py`, enforced at the top of `agent.run_turn` /
   `stream_turn` by counting `assistant_message_sent` ProductEvents since
   local midnight).
2. **Historical Gmail rescan** (capture/, being built now) — the "Scan Now"
   button's deterministic pass is free, but its residue classifier sends up
   to 100 threads per rescan to Haiku. Nothing meters this at all.

Two independent caps in two apps is two things to keep honest. One credit
pool that both surfaces draw from replaces the daily-message mechanic and
covers the rescan on the same ledger. Neither surface is unlimited on any
plan, free or paid.

## Real cost per action (the numbers everything else is built on)

Current API pricing: Haiku 4.5 is $1.00/M input tokens, $5.00/M output.
Sonnet is $3.00/M in, $15.00/M out (intro pricing of $2/$10 runs through
2026-08-31; plan on the full price).

**One chat message, Free (Haiku).** The cached system-plus-tools prefix is
~4.7k tokens (measured live, see agent.py's PROMPT CACHING note). A typical
turn is 2–3 rounds. Billed pieces: one 1h-TTL cache write (~4.7k × 2x =
$0.009, once per idle hour, not per message), ~10k fresh input across rounds
($0.010), ~15k cached reads at 0.1x ($0.0015), ~2k output ($0.010).

> One Haiku chat message ≈ **$0.005–$0.03**. Plan on **$0.02**.

**One chat message, Pro (Sonnet).** Same token shape at 3x the price:
≈ **$0.015–$0.09**, plan on **$0.06**. A PDF attachment (billed per page)
can push a single turn higher — one more reason attachments already stub
out of the replay window after one turn.

**One residue thread, rescan (Haiku).** Classification prompt ~1.5k input +
~80 output ≈ **$0.002 per thread**. A maxed-out 100-thread residue pass is
therefore about **$0.20 — twenty cents**. The 100-thread cap is doing its
job: even the worst-case rescan costs less than three Sonnet chat messages.
The founder's worry about "spending a lot per scan" does not survive the
arithmetic; the thing that actually needs metering is *frequency* (a user
pressing Scan Now ten times a day), which credits handle.

## 1. The credit unit

**1 credit ≈ $0.02 of model spend ≈ one Haiku advisor message.** Costs per
action, set from the real ratios above, not round numbers for their own sake:

| Action | Credits | Real cost | Why |
|---|---|---|---|
| Chat message on Haiku (Free's model) | 1 | ~$0.02 | The anchor unit |
| Chat message on Sonnet (Pro's model) | 3 | ~$0.06 | Honest 3x price ratio |
| Rescan residue classification | 1 per 10 threads, rounded up | ~$0.02 per 10 | Full 100-thread pass = 10 credits ≈ $0.20 |
| Deterministic rescan pass, CSV reply-detection | 0 | $0 | No model call, never charged |
| Coffee-chat brief (`crm/ai_brief.py`) | 1 | ~$0.02 | Added 2026-08-20 (founder-decisions §2b). Same anchor as a Haiku chat message; user-triggered via a POST button, so it is metered rather than joining the not-metered list below. Charged once per successful generation, never on a failed/unconfigured call. |

Notes:

- The chat debit is **per student message**, not per API round — the
  student can't see or control rounds, so charging by round would be
  charging for the implementation. `MAX_ROUNDS` and `MAX_TOOL_CALLS` stay
  exactly as they are: they are per-turn spend governors inside one unit of
  charge, orthogonal to credits.
- The rescan charge is on threads **actually sent to Haiku**, not the cap.
  A rescan whose deterministic pass leaves 12 uncertain threads costs 2
  credits, not 10. Charged in one ledger row at the end of the pass with
  `props={"threads": n}`.
- Deliberately **not** metered (decision, not oversight): the conversation
  title call (`_TITLE_MODEL`, ~$0.0001), the daily brief
  (`assistant/brief.py`'s `DailyBrief` — an auto-generated product surface,
  NOT `crm/ai_brief.py`'s user-triggered coffee-chat brief, which is now
  metered above), `crm/ai_summary`, `extract_deadlines_ai`, and
  `extract_sponsorship_ai` (added 2026-08-20 —
  docs/founder-decisions-2026-08-20.md, Decision 3's AI pass over the
  sponsorship residue). The first three are bookkeeping calls already
  gated (once per day, MIN_TOUCHES, POST-behind-a-button) and each costs a
  fraction of a cent; the last two are founder-run management commands,
  never wired into a cron or a user-triggered path. If any of them ever
  becomes user-triggered at volume, it joins the table — the ledger's
  `kind` field is built for that.

## 2. Competitive landscape & pricing

Prices pulled live 2026-08-19. Pricing pages move constantly — re-verify
before anything ships publicly.

### Credit-metered AI tools (the mechanic this plan copies)

| Tool | Plan | Price/mo | Monthly grant | What a credit buys | Implied economics |
|---|---|---|---|---|---|
| Manus | Pro entry | $20 | 4,000 credits | task compute; one deep-research task burns 900–1,000 | ~4 heavy or ~20 medium tasks; est. 5–10x over compute |
| Lovable | Pro | $25 | 100 credits | ~1 agent message/edit | **$0.25 per agent message** vs est. $0.03–0.10 raw → ~3–8x |
| Bolt.new | Pro | $20 | 13M tokens | raw model tokens | full burn ≈ $40+ of Sonnet-class inference → under water at 100% use; priced on breakage |
| v0 (Vercel) | Premium | $20 | $20 of credits | metered tokens at v0's own marked-up token rates | markup lives in the token rate, not the grant |
| Gamma | Plus | $8–10 | 1,000 credits | 10-slide deck ≈ 50, AI image 2–40 | ~$0.40/deck vs pennies of inference → ~10x; rollover capped at 2x |
| ElevenLabs | Starter | $6 | 30,000 credits (~30 min TTS) | 1 credit ≈ 1 character | ~$0.20/min vs <$0.02 raw → ~10x |
| Midjourney | Basic | $10 | 3.3 fast GPU-hrs (~200–250 images) | GPU seconds | ~1.5–3x over GPU cost at full burn; relies on <100% utilization |

What the credit-metered market actually does:

- **Nobody passes compute through 1:1.** The norm for credit systems is a
  ~3.3x markup at full utilization (≈66–70% gross margin) and far more at
  typical utilization; 50–70% gross margin is the accepted floor for
  AI-native products (industry pricing write-ups: digitalapplied.com,
  getmonetizely.com).
- **Monthly pools with a rollover cap are standard.** Gamma caps rollover at
  exactly the 2x this plan already chose (§7). Manus expires monthly credits
  outright.
- **A few VC-scale tools price below full-burn cost** (Bolt's 13M tokens
  cost more to serve than $20 if fully used) and bank on breakage. Not an
  option for a bootstrapped app — Coverage must be margin-positive even if
  every user burns everything.

### What Coverage's audience already pays

| Tool | Sticker/mo | Cheapest committed/mo |
|---|---|---|
| Huntr Pro | $40 | ~$27 (6-month) |
| Simplify+ | $39.99 | ~$30 (quarterly) |
| LinkedIn Premium Career | $29.99–39.99 | ~$20–24 (annual) |
| Teal+ | $29 | ~$26 (quarterly) |
| Careerflow Premium | $23.99 | ~$14.40 (annual) |
| ChatGPT Plus / Claude Pro | $20 | $20 |
| Gamma Plus | ~$10 | $8 (annual) |

Sources: manus.im pricing via lindy.ai/blog/manus-ai-pricing and
nocode.mba/articles/manus-ai-pricing; nocode.mba/articles/lovable-pricing;
nocode.mba/articles/bolt-pricing-2026; v0.app/pricing;
flowith.io/blog/gamma-app-pricing-2026-free-vs-plus-vs-pro;
flexprice.io/blog/elevenlabs-pricing-breakdown; eesel.ai/blog/midjourney-pricing;
jobhire.ai/blog/simplify-jobs-review; blog.loopcv.pro/teal-hq-review;
premium.linkedin.com/careers/career + scrupp.com/blog/linkedin-premium-cost;
huntr.co/pricing; jobsolv.com/directory/careerflow;
sentisight.ai/ai-price-comparison-gemini-chatgpt-claude-grok.

**Read on the market.** Students in an active recruiting cycle demonstrably
pay $24–40/month for career tools, and $20 is the mental slot for "my one AI
subscription." Coverage should not fight ChatGPT for the $20 slot — the chat
advisor is the surface most substitutable by ChatGPT — and does not need $30
career-tool pricing to clear healthy margin. **$69 per ~6-month cycle
(≈ $11.50/mo)** sits under every comp that matters, reads as an add-on
*next to* a ChatGPT subscription rather than instead of it, and supports a
68.7% full-burn margin (§3) — 1.3 points under the 70% floor, accepted in
favor of a cycle price that reads like a real number rather than the
exact-ratio $72 (founder-decisions-2026-08-20.md §2c).

## 3. Margin analysis — deriving the price and the grants

**The v1 problem, stated plainly.** v1 granted Pro 1,500 credits ≈ $30 of
model spend and deliberately set no price. At any plausible price that is
upside down:

| Pro price | Margin at full burn (1,500 cr = $30 spend) | Margin at typical burn (35% ≈ $10.50) |
|---|---|---|
| $8/mo | −275% | −31% |
| $10/mo | −200% | −5% |
| $15/mo | −100% | +30% |

Even the best cell is far below the 50–70% floor; one fully-used Pro month
at $10 loses $20. The grants were sized for free dogfooding, not for revenue.

**The constraint that prices everything.** At the planning cost of
$0.02/credit, "≥70% gross margin even at full burn" means full-burn model
spend ≤ 30% of price — i.e. **grant ≤ 15 credits per dollar of monthly
price**. So: $12 → 180 credits, $15 → 225, $20 → 300. §2c's founder decision
(2026-08-20) keeps the 180-credit grant but reprices to a **$69/cycle**
(≈ $11.50/mo) commit rather than a monthly $12 — the exact-ratio price at
180 credits would be $72/mo-equivalent (15.0 credits/$, 70.0% margin); $69
runs the ratio to 15.7 credits/$, **1.3 points under the 70% floor**,
accepted because "$3 of margin at the theoretical ceiling is not worth a
price that reads like a rounding error."

**Chosen numbers: Pro $69 per ~6-month cycle (≈ $11.50/mo), 180 credits. Free 60 credits.**

| Scenario | Credits burned | Model spend | Gross margin on $11.50/mo |
|---|---|---|---|
| Full burn (hard ceiling) | 180 | $3.60 | **68.7%** |
| Heavy month (60%) | 108 | $2.16 | 81% |
| Typical SaaS utilization (30–40%) | 54–72 | $1.08–1.44 | **87–91%** |
| Light month (15%) | 27 | $0.54 | 95% |

Markup framing: full burn is a 3.3x markup over model cost — exactly the
industry norm for credit systems (§2); typical usage is ~9–10x, inside the
band the credit-metered comps land in. And $0.02/credit is the conservative
planning number (measured Haiku turns run $0.005–0.03), so every margin
above is a floor, not an average.

**Free is a bounded loss-leader.** 60 credits caps a Free user at
**$1.20/month** of model spend, worst case; typical is ~$0.40. A maxed-out
free user costs less per *year* than one month of Pro revenue.

**Is 180 credits market-fair?** It buys 60 Sonnet advisor messages a month
(or 180 Haiku-priced actions, or mixes with 10-credit full rescans). That is
$0.20 per Sonnet message versus Lovable's $0.25 per agent message at twice
the monthly price — and it comes with the rescans and the CRM around it. The
v1 grant was set before a price existed; once $69/cycle is real, 180 is what
honest unit economics buys. If more generosity is ever wanted, move the
grant (`CREDIT_PRO_MONTHLY_GRANT` down to ~170, per founder-decisions §2c),
never the price.

## 4. Allocation per plan

**Monthly grant, not a daily reset.** A rescan is bursty by nature — a user
connects Gmail, scans, imports a CSV, scans again, then does nothing for two
weeks. A daily allowance punishes exactly that shape; a monthly pool fits it.
It also fits the current stage: founder dogfooding plus a handful of testers,
where light users should never feel a wall.

| Plan | Price | Monthly grant | Buys (examples) | Worst-case model cost / user / month |
|---|---|---|---|---|
| Free | $0 | **60 credits** | 60 Haiku messages, or 30 + 3 full rescans | **$1.20** |
| Pro | **$69/cycle (≈ $11.50/mo)** | **180 credits** | 60 Sonnet messages, or 45 + 4 full rescans | **$3.60** |

Sanity against the status quo: the old caps' theoretical ceiling was 450
Haiku messages/month on Free (~$13.50) and 1,800 Sonnet messages/month on
Pro (~$162). v1 of this plan cut that to $6 / $30 but was still sized for
free dogfooding; v2 cuts it to $1.20 / $3.60 because the grants are now
priced against real revenue (§3) — every credit granted is a credit the
cycle price has to cover even if fully burned. The monthly pool still fits the
product's actual shape better than a daily reset: rescans are bursty, and a
big scan-and-chat day after connecting Gmail draws from the month, not from
an arbitrary daily wall.

**Daily burst guard (recommended, one query).** A monthly pool means a
runaway loop or a shared password can burn a month in an hour. Add a
per-day debit ceiling — Free 15 credits/day, Pro 45 (15 Sonnet messages, or
10 plus a full rescan — a legitimately heavy day fits) — checked in the same
place as the balance, implemented as a `Sum` over today's spend rows. This
is what *subsumes* the old daily cap rather than deleting the idea: the
daily number stops being the product limit and becomes abuse protection,
set high enough that a legitimate heavy day never touches it.

Both grants and per-action costs live in settings, env-overridable, exactly
like `ASSISTANT_PLANS` does today:

```python
CREDIT_PLANS = {
    "free": {"monthly_grant": 60,  "message_cost": 1, "daily_burst": 15},
    "pro":  {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
}
CREDIT_RESCAN_THREADS_PER_CREDIT = 10
```

## 5. Where it lives in the codebase

**A new `billing` app.** Not `assistant` (the rescan is capture's surface),
not `accounts` (whose models.py already carries the plan field and a promise
that "a billing webhook writes this tomorrow" — that webhook's home is this
app). When Stripe arrives, its webhook handler, and nothing else, moves in
next to code that already exists.

### Data model: an append-only ledger, no balance column

This codebase already has the convention: `analytics.ProductEvent` is an
immutable append-only record and `record_event` is its single write path.
Credits follow it. A running-balance column is a second copy of the truth
that can drift from its own history; at 60 users, `Sum("delta")` over an
indexed per-user ledger is microseconds, and the ledger *is* the audit
trail — every admin adjustment, every grant, every spend is a row somebody
can read back.

```python
# billing/models.py
class CreditLedger(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    delta = models.IntegerField()            # + for grants/adjustments, - for spends
    kind = models.CharField(max_length=32)   # "grant" | "spend_chat" | "spend_rescan" | "adjust"
    period = models.CharField(max_length=7, blank=True, default="")  # "2026-08" on grant rows
    props = models.JSONField(default=dict, blank=True)  # {"model": ..., "threads": 37}
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "credit_ledger"
        indexes = [models.Index(fields=["user", "created"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "period"],
                condition=Q(kind="grant"),
                name="uniq_monthly_grant",
            )
        ]
```

### The API (billing/credits.py)

```python
def balance(user) -> int          # ensure_monthly_grant(), then Sum("delta")
def ensure_monthly_grant(user)    # lazy + idempotent, see below
def can_spend(user, cost) -> bool # balance > 0 and today's spend < daily_burst
def spend(user, cost, kind, **props) -> None  # one negative ledger row
```

**Grants are lazy, no cron.** The first `balance()` call in a new month
writes that month's grant row; the unique constraint makes a concurrent
double-write a harmless IntegrityError to swallow. This is the same
"activates when touched" posture as everything else in the app — no
scheduled job to forget to deploy.

**Concurrency:** wrap check-and-spend in `transaction.atomic()` with
`select_for_update` on the user row. One line, and worth it even pre-launch
because the streaming path holds requests open long enough for a
double-submit to be realistic.

### Enforcement point 1: the chat loop (assistant/agent.py)

Replace the `messages_sent_today(user) >= cap` block at the top of
`run_turn` and `stream_turn` with `billing.credits.can_spend(user, cost)`,
where `cost` comes from `CREDIT_PLANS[plan]["message_cost"]`. The debit goes
exactly where `record_event("assistant_message_sent")` already fires —
**after round 0 returns successfully** — preserving the existing fix that a
student is never charged for a request the API failed to answer. The
`assistant_message_sent` event itself stays (it feeds analytics); only the
cap stops reading it. `daily_cap()` and `messages_sent_today()` retire from
the enforcement path.

The notice keeps `ChatMessage.NOTICE_CAPPED` (templates and tests keep
working) with rewritten copy — see §6.

### Enforcement point 2: the rescan (capture/)

In the Scan Now flow being built in the parallel worktree, at the seam
between the deterministic pass and the Haiku residue loop:

1. Deterministic pass runs unconditionally — it is free and stays free.
2. Before the residue loop: `affordable = min(len(residue), balance * 10)`
   (also clamped by the daily burst). Classify that many threads.
3. One `spend(user, ceil(classified / 10), "spend_rescan", threads=classified)`
   at the end.

A zero balance never blocks the scan button — it degrades the scan to
deterministic-only and says so in the results summary. That matches the
feature's own design: the AI pass was always the optional second opinion on
threads the free pass couldn't call.

Coordination note: this touches the other agent's code at exactly one seam
(the residue loop's entry). The credit module lands first; the rescan calls
two functions from it.

## 6. What happens at zero credits

**Hard stop before a turn starts, never mid-turn.** The balance is checked
once, before round 0. A turn in flight always finishes all its rounds — the
per-message debit model makes this automatic, since rounds within a turn are
never individually charged. A student mid-conversation loses nothing they
have already asked.

**Overdraw edge (Pro):** a Pro user with 1–2 credits left is allowed the
turn and debited the full 3; the ledger goes slightly negative and the next
grant absorbs it. Displayed balance floors at zero. The alternative
(requiring the full 3 up front) strands 1–2 credits every month for no
benefit; being ~$0.04 generous is cheaper than the support conversation.

**The copy is honest, in the app's existing voice** — the same posture as
`client.py`'s "isn't switched on yet" and the current cap notice ("No link
and no upsell button: there is nothing to buy yet"):

- Chat, Free at zero: *"That's the last of this month's credits on the Free
  plan. They refill on 1 September — Today, Network and Opportunities are
  all still there. Pro comes with three times the credits and a stronger
  model."*
- Chat, Pro at zero: same first two sentences, no upsell line.
- Rescan, partial: *"Scanned everything. 87 threads needed a closer look;
  credits covered 40 of them — the rest stay unclassified until credits
  refill."*

**Show the meter.** A quiet "214 credits" line in the chat composer and on
Settings (one `Sum` query). A limit a user can see coming is a product
decision; one that ambushes them is a bug report. This is the only UI work
in the plan beyond the notice copy.

**Refunds net out of the burst guard and the monthly usage line, not just
the balance.** `billing/credits.py::refund` (assistant/agent.py's fairness
rule: never charged for a turn the student didn't get an answer to, even
when the charge already landed on round 0 and only a later round failed)
writes its own `KIND_REFUND` row, and `daily_spent`/`month_usage` net a
same-window refund against the spend row it reverses. A refunded turn is
defined to be, from the student's side, exactly as if it never happened —
not "the balance came back but the turn still counts against today's
allowance and this month's usage total."

This was a real decision, not the obvious shape: round 0 genuinely did
reach the API and cost real money, so there's a case that the daily burst
guard — an abuse backstop against a runaway loop or a shared password,
not a billing mechanism — should count it regardless of refund. That
argument loses to this module's own stated fairness rule, which draws no
such exception, and to what not netting actually does in practice: a run
of turns that all fail after round 0 (an outage, a flaky patch of
tool-call network errors) can burn a student's entire `daily_burst` and
lock them out for the rest of the day while their balance sits untouched
— and the zero-credit notice (`_credit_block_notice`) would then tell them
"your credits for the month are still there," which reads as dishonest
when they got zero answers. Settings would show the same kind of
mismatch: a full balance next to a nonzero "used N this month." The real
per-round API cost this counter-argument cares about is tracked
independently anyway, in `assistant/agent.py::_log_usage`'s
`assistant_usage` event — untouched by a refund, and the right place to
look for actual model spend, as distinct from what a student's allowance
should reflect.

## 7. Refill and rollover

- **Refill:** full grant on the 1st of each month, user's own timezone
  (`timezone.localdate()` — the same "today" every other feature uses),
  written lazily as above.
- **Rollover:** unused credits carry over, capped at one extra month:
  at grant time, `grant_row.delta = min(monthly_grant, 2 * monthly_grant - current_balance)`
  (clamped ≥ 0), so balance never exceeds 2x the monthly grant. This is the
  "generous while dogfooding" posture: a light month isn't punished, but a
  dormant account can't quietly bank a year of spend and detonate it in one
  week. One clamp at grant time, no expiry job. (Gamma caps rollover at the
  same 2x — the mechanic is market-standard, §2.) A Pro balance maxes at 360
  credits, so the worst deferred exposure is $7.20 — well inside the margin.

## 8. Explicitly out of scope

- **A Stripe-billed Pro subscription.** `user.plan` still flips by hand in
  admin ("admin IS the billing system for now" — accounts/admin.py); Stripe
  is only wired up for the one-time top-up packs (§10), not for charging
  the $69/cycle Pro price itself or writing `user.plan` from a webhook. That
  remains exactly the shape this section originally described: `user.plan`
  admin-set, grants derived from it, no subscription checkout built.
- Metering the un-metered calls listed in §1.
- Per-user overrides beyond admin adjustment rows.

Pay-as-you-go top-ups (one-time packs, not a subscription) ARE now built —
see §10 — but Stripe itself is not yet configured on this deploy, so the
feature is inert (disabled "Coming soon" UI, checkout redirects with a
clean message, webhook 400s) until `STRIPE_SECRET_KEY` /
`STRIPE_WEBHOOK_SECRET` are set.

## 9. Cost math at projected scale

50 active Free users + 10 Pro users. Pro revenue at $69/cycle
(≈ $11.50/mo): **$115/month.**

| Scenario | Assumption | Monthly model spend | Against $115 revenue |
|---|---|---|---|
| Likely early reality | Free ~30 credits used ($0.60); Pro ~65 credits ($1.30) | **~$43** | 63% margin, free fleet included |
| Quiet month | Half that engagement | **~$22** | 81% margin |
| Hard ceiling | Every user exhausts every credit | 50 × $1.20 + 10 × $3.60 = **$96** | still $19 positive |
| v1 grants' ceiling (for comparison) | Full burn on 300/1,500 grants | 50 × $6 + 10 × $30 = **$600** | −$480 at the v1-era $12/mo this row is illustrating |
| Old caps' ceiling (for comparison) | Everyone maxes the daily caps | ~50 × $13.50 + 10 × $162 ≈ **$2,300** | — |

The headline: with v2 numbers, ten Pro subscriptions cover the model bill
for the entire 60-user fleet even in the theoretical worst case where every
user, free and paid, burns every credit.

The rescan line item specifically: if all 60 users ran two maxed-out
100-thread rescans a month, that is 120 × $0.20 ≈ **$26/month total**. The
scan the founder was worried about is the cheapest thing on the menu; the
chat, with its 8-round loops and Sonnet tier, is where the money is, and
the credit ratios (1 : 3 : 1-per-10-threads) price that truthfully.

## 10. Pay-as-you-go top-ups (v3, `billing/stripe_gateway.py`)

A second way to get credits, alongside the monthly grant §4 describes:
one-time packs a student buys outright, on top of whatever their plan
already grants. Not a subscription and not a replacement for the plan
system — Pro is still admin-set (§8) — just a release valve for someone who
burns a month's grant early and wants more now rather than waiting for
the 1st.

**The two packs, priced against §3's governing ratio** (grant ≤ 15 credits
per dollar, to hold ≥ 70% margin at full burn, $0.02/credit planning cost):

| Pack | Price | Credits | Credits/$ | Margin at full burn |
|---|---|---|---|---|
| Small | $5.00 | 60 | 12 | ~76% |
| Large | $12.00 | 160 | 13.3 | ~73% |

Both sit under the 15-credits-per-dollar ceiling with room to spare —
priced a little more conservatively than the monthly grant itself (which
runs the ceiling close to its limit at 15/$) because a one-time purchase
has no rollover cap to bound worst-case exposure the way §7's 2x clamp
does for the monthly pool. Price stored in cents (`price_cents`) throughout
`stripe_gateway.py`, never as a float dollar amount.

**Checkout, without any Stripe Dashboard configuration.**
`create_checkout_session(user, pack_key, success_url, cancel_url)` builds a
Stripe Checkout Session (`mode="payment"`) using ad-hoc `price_data` rather
than a pre-created Stripe Price ID — the founder hasn't set those up, and
dynamic `price_data` means this never needs to. `user.id` and `pack_key`
travel in the session's `metadata`, which is how the webhook — running
with no request context, no signed-in user, nothing but the verified
payload — knows what to grant and to whom.

**The `is_configured()` gate — copied directly from `capture/
gmail_live.py`'s pattern.** `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`
are both blank by default (`settings/base.py`); `stripe_gateway.
is_configured()` is true only when both are set. Every entry point respects
it:

- **Settings page:** the "Buy more credits" block is always rendered
  (never hidden — the founder should be able to see the feature exists),
  but its buttons render `disabled` and the copy reads "Coming soon" when
  `not is_configured()`.
- **`POST /billing/checkout/<pack_key>/`:** redirects back to Settings with
  a plain message ("Credit top-ups aren't available yet") instead of
  attempting a Stripe call, when not configured.
- **`POST /billing/webhook/`:** returns 400 immediately when not
  configured, since there is no webhook secret to verify a signature
  against anyway — and this path is never actually hit in that state,
  because there's no webhook URL registered with Stripe yet to call it.
- **`create_checkout_session` itself** raises `StripeGatewayError` if
  called while unconfigured — callers must check first, same discipline as
  every other call site in this app.

The whole app builds, migrates, and passes its test suite with zero Stripe
keys present (`billing/tests/test_stripe_gateway.py`); nothing here needs
real Stripe credentials to develop or test against.

**Idempotency — Stripe redelivers webhooks; grants must not double.**
Stripe's own delivery guarantee is at-least-once, so
`handle_webhook_event` must tolerate the identical event arriving twice
(a retry after a slow 200, or two near-simultaneous deliveries racing each
other). Guarded by a new model, `ProcessedStripeEvent` — deliberately
*not* a `PrivateModel` like `CreditLedger`, because a webhook event carries
no request-time tenant context to scope against; it's a plain model with a
global `unique=True` on `stripe_event_id`. The handler does
`get_or_create(stripe_event_id=...)` inside `transaction.atomic()` *before*
calling `credits.grant_purchase`, so:

- A sequential redelivery finds the row already exists and returns without
  granting again.
- Two concurrent deliveries race on the row's unique constraint the same
  way `ensure_monthly_grant` (§4) races two concurrent grant checks — one
  wins, one gets an `IntegrityError`-shaped conflict and backs off, and
  either way exactly one grant lands. Covered by a real
  `TransactionTestCase` test with multiple threads hitting the same event
  ID concurrently, not just a sequential-duplicate happy path.

**The ledger row.** `credits.grant_purchase(user, pack_key, stripe_event_id)`
writes one positive `CreditLedger` row: `kind="purchase"` (a new `kind`,
distinct from `"grant"` — a purchase is money spent, a grant is the free
monthly allowance, and keeping them apart matters for any future revenue
reporting), `delta` = the pack's credit amount, `props={"pack": ...,
"stripe_event_id": ..., "price_cents": ...}` for the audit trail. No new
concurrency primitive needed beyond the `ProcessedStripeEvent` guard above
— by the time `grant_purchase` runs, the event is already known-unprocessed.

## Build order (when approved)

1. `billing` app: model, migration, `credits.py`, admin, tests.
2. Settings: `CREDIT_PLANS` + rescan constant; drop `daily_cap` from
   `ASSISTANT_PLANS` (model selection stays there untouched).
3. Swap the cap block in `run_turn`/`stream_turn`; rewrite the capped
   notice; update assistant tests.
4. Hook the rescan residue loop (coordinate with the worktree building it).
5. Composer/Settings credit display.
6. Update accounts/admin.py's plan description text and the pricing page's
   Pro list — including the $69/cycle price (§2–3), shown even while
   nothing collects it.
7. Meter the coffee-chat brief (`crm/ai_brief.py`, §1): `can_spend`/`spend`
   around `generate_coffee_chat_brief`, new `spend_brief` kind.

Verify per-app (`pytest coverage_web/billing coverage_web/assistant
coverage_web/capture`) — the full-suite run has a known pre-existing
connection-leak issue that mass-fails unrelated tests.
