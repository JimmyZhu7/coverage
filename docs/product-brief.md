# Coverage — Product Brief

*Captured 2026-07-23. Source: founder brief + competitive research.
Competitive table and shipped-surface names refreshed 2026-07-25.*

> **Naming note (2026-07-25):** this brief was written when the public surface
> was a heat-mapped cycle **calendar**. That page has been retired; the public
> surface is now the **Opportunities feed** at `/opportunities/` — the same free
> data, ranked by deadline then freshness instead of laid out as a grid.
> Per-firm cycle timelines survive on each firm's detail page. Read every
> "calendar" below as "feed".

## Thesis

Everyone tracks deadlines. Nobody tracks the relationship.

The Trackr, Handshake, and spreadsheets all answer *when* things open and close.
None answer *who* to network with, *when* to follow up, or *where* a given student
actually has a realistic shot — which is the part that decides outcomes in this
recruiting process.

## Core loop

1. A personalized recruitment timeline, by vertical (consulting, finance, tech).
2. A Gmail-hooked networking CRM that scans sent/received mail.
3. An AI layer that scores contacts and firms by estimated chance of success, and
   feeds that back into what the timeline tells you to prioritize this week.

## Technical foundation

A working single-user system already covers a large share of this. See
`docs/existing-system.md` for the verified architecture map. In summary:

- **`recruiting-radar/`** — deterministic ATS scraping (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday, Oracle Recruiting Cloud, plus company-specific
  endpoints for HSBC, Amazon, Tencent, ByteDance), a markdown tracker, a Flask
  dashboard with standardized opportunity cards, a verification layer that flags
  stale/changed/closed listings without an LLM call per row, and a self-improving
  scan pipeline that logs what worked and feeds those lessons into the next brief.
- **`campaign/`** — SQLite CRM, cadence engine, research/intel layer, weekly
  operating rhythm.
- **`campaign/dashboard/` (netdash)** — networking dashboard including Gmail
  enrichment.

Deterministic scraping was chosen over LLM web-search for both cost and accuracy:
official APIs return ground truth instead of a model's best guess. A shared-cache,
one-fetch-serves-all-users architecture is the scaling plan.

*Legal note:* hiQ Labs v. LinkedIn establishes that scraping public data is
CFAA-legal. ToS and tort risk still argue for staying reasonable on request volume.

**The public opportunities feed is free and never paywalled.** Trust with this
audience depends on the tracker being real, not a lead magnet.

## Competitive landscape

| Competitor | Model | Price | Notes |
|---|---|---|---|
| The Trackr | Free + Premium add-on | **$40 one-time (US), £30 (UK)** | alerts/community, not personalization. Price confirmed 2026-07-25 — it lands within $1 of the Season Pass below, which is the strongest external validation the pricing has |
| OffCycle | One-time unlock, explicitly "no subscription" | $9.99 unlocks | closest analog; wins goodwill by rejecting subscriptions. **Has since pivoted toward networking** — 434 firms refreshed hourly, plus verified HR contact emails and AI-assisted outreach for 10,000+ boutiques |
| NextCoffee.ai | AI coffee-chat assistant | not established | **Nearest thing to Coverage's thesis found so far** (2026-07-25): prioritizes contacts by response signal, drafts notes, sends from Gmail, follows up. Attacks the *send* side; Coverage's claim is the *sustain* side. Needs a proper teardown before the capture UX is frozen |
| Adventis | Free tracker as lead magnet | $295–495 course | one-time, 2yr access |
| RecruitU | Marketplace | free to students | monetizes employers, not students |
| WSO Academy | Off-menu sales call | ~$7,000 | one-time mentorship + tracker bundle |
| Wall Street Prep / BIWS | Course purchase | ~$425, installment plan | one-time |

**Key finding:** no competitor in this niche runs a subscription duration ladder.
Everyone sells either a flat one-time unlock or a single high-ticket program. A
duration-tiered subscription is white space, not an established norm — worth doing
deliberately rather than by default.

## Pricing

- **Free forever** — full opportunities feed, CRM capped at 50 contacts, basic fit score.
- **Season Pass — $69 per ~6-month cycle** (repriced 2026-08-20, see
  `docs/founder-decisions-2026-08-20.md` §2c: the credit grant behind this
  tier needs $69/cycle to clear the 15-credits-per-dollar margin floor; $39
  was 45% full-burn margin, below it) —
  unlimited contacts, full fit-score history, staleness automation, instant alerts.
  The "cycle" is a fixed 6-month window from activation, not a literal calendar
  season: actual high-intensity recruiting runs ~5–6 months even though the full arc
  (EB open through MBB) spans 10–12. No monthly option alongside it: a
  cheaper recurring price would cannibalise the cycle commit (§2c).
- **Team workspace — $299/year flat, up to 25 members** (26–60: $599/year);
  first 5 founding clubs free for year one, not free forever (repriced
  2026-08-20, see founder-decisions §1) — shared firm tiers, a club-wide
  coverage board, and officer-managed seats. Every member is a Free
  account; Pro seats are $69/cycle each, club-billed at the same price an
  individual pays, never pooled or discounted.

## Brand voice

**"An instrument, not a movement."**

The one documented brand failure in this space was hype and community energy — a WSO
commenter called a competitor "overmarketed garbage." This audience trusts its own
spreadsheet more than any pitch. Borrow insider vocabulary ("who has coverage on this
account") rather than marketing language.

## Known open risk

Everything that differentiates Coverage from a plain tracker depends on captured
email activity. If students don't log or BCC their outreach, Coverage quietly
degrades into The Trackr with worse deadline coverage.

Identified mitigations:

- Import contacts from an existing spreadsheet on day one, so the CRM never starts empty.
- Pre-fill a BCC tracking address on every compose.
- Reward capture within the same session via visible warmth-state movement.
- Gate a "40% activity" health check by month three, to catch a starving layer early
  rather than at month twelve.
