"""Cadence engine — the weekly-priority decision tree, ported from the
`campaign` project's `cadence.py` (`due_actions`) and `tasks.py` (the
backward planner), plus the `cadence.yaml` rule parameters as module
defaults.

Ported behavior (semantics unchanged from the SQLite/YAML original — only
the storage adapter changed, per docs/build-plan.md §4's port table):

  - `due_actions()`'s fixed 7-branch decision tree, in the exact same
    order, returning at most one action per contact:
      1. chat_done + no thank-you since the LATEST chat, and
         that chat is within `thank_you_expires_after_days`    -> thank_you
      2. chat_scheduled stale > 4 business days                -> confirm_chat
      3. warm contact at a firm whose CONFIRMED app_close is
         within `pre_deadline_reping_days`, REGION-SCOPED       -> reping
      4. parked / quiet                                         -> (skip)
      5. advocate idle >= advocate_touch_min_weeks             -> maintain
     5a. chatted with an OPEN PROMISE on record (they said they
         would do something), promised >= promised_followup_after_days
         days ago                                    -> promised_followup
         (NOT ported — see the 2026-09-02 C6 divergence below)
     5b. chatted (any thread_state except 'replied', which
         branch 7 owns), idle >= chatted_touch_min_weeks      -> keep_warm
         (NOT ported — see the 2026-07-30 C1 / 2026-08-22 C1b
         divergences below)
      6. cold / no_reply: 0 outbound -> first_outreach; 1 outbound and
         idle >= followup window -> follow_up (the ONLY one — see
         `max_cold_touches`'s comment), unless the silence has passed
         `followup_expires_after_business_days`, in which case -> park
         (see the 2026-09-01 divergence); else park once max_cold
         reached and idle >= park window
      7. replied + idle >= 3 business days                      -> advance
         (warmth set widened past the port — see the C3 divergence)
    Sorted by (priority, firm tier, firm name).

    NOTE on that sort: it is the PORTED display order and stays here
    untouched, golden fixtures and all. The Today page deliberately
    re-sorts what it renders in its own view layer (`crm.views._TODAY_CLASS`)
    because "who do I contact right now" is a different question from "what
    does the engine consider urgent"; nothing about that belongs in here.

  - The second-chat thank-you fix and the "chat_done contacts re-enter the
    cadence once thanked" fix (both come free with the ported branch 1).

  - DIVERGENCE from the original (deliberate, 2026-07-25): branch 1 now
    EXPIRES. The original prompted for a thank-you indefinitely; here the
    prompt stops after `thank_you_expires_after_days` and the contact falls
    through to the rest of the cadence. See that parameter's comment for why.

  - Region scoping (branch 3): an HK app_close never re-pings a US contact
    at the same firm, and vice versa. A contact whose region is unknown keeps
    the both-regions fallback: it matches on the soonest close date across any
    region for the firm. A contact explicitly OUTSIDE both markets (region
    "other", added 2026-08-25 with the third Network bucket) matches no us/hk
    close at all — that is knowledge, not ignorance, so the conservative
    fallback would be wrong for them.

  - DIVERGENCE from the original (deliberate, 2026-07-25, tightened
    2026-07-27): a contact's region is read from an EXPLICIT `region` key and
    from nothing else. The original had no region column and inferred it by
    substring-matching "hk" inside the free-text `source` — which is a
    provenance string, so every contact whose source didn't happen to say
    "hk" (including every hand-added one, source "manual") silently read as
    US: re-pinged against US deadlines, skipped for HK ones.

    The 07-25 change kept that inference as a fallback for blank rows, on the
    theory that legacy rows should keep the meaning they had. That was wrong,
    and the live data is what showed it. `infer_region` returns a region for
    ANY non-empty string, so the fallback answered confidently for all 51
    blank-region contacts — 19 of them "us" purely because their provenance
    text read "Gmail USC discovery". The documented "unknown" path, which
    three other artifacts are built around (`crm.models.Contact.region`'s
    comment, migration `crm/0005_backfill_contact_region`, and
    `crm.views._in_scope`'s firm fallback), was therefore unreachable: a
    blank region never once produced None. Worse, the wrong-but-confident
    answer SUPPRESSES branch 3 — a contact read as "us" is skipped for an HK
    close — costing the pre-deadline re-ping, the highest-value nudge in the
    engine, on exactly the rows that carry the least information.

    So the fallback is now retired from the read path. Blank means unknown,
    unknown takes the both-regions fallback, and a contact whose region
    matters gets it set explicitly. `infer_region` itself stays in the module
    (see its docstring) as the record of the old rule and as the tool for a
    one-time backfill, should the founder want those guesses written into the
    column where they can be seen and corrected.

  - DIVERGENCE from the original (deliberate, 2026-07-27, REVERTED
    2026-07-28): branch 6 briefly staged the follow-up gap — a longer
    `second_followup_after_business_days` window before follow-up #2 and any
    later one, on the reasoning that a second unanswered note is evidence and
    the right response is to back off rather than keep the same beat. The
    owner's actual call is simpler and stricter: don't send a second
    follow-up at all. One unanswered follow-up is enough evidence on its own
    — park it rather than try again on a longer leash. So there is no
    "later one" for a second window to govern; `max_cold_touches` is now
    capped at 2 in `crm.views.TUNABLE_CADENCE_PARAMS` (one outreach note, one
    follow-up, then park) precisely so that branch can't be reached, and the
    staging key was removed rather than left dormant: a configuration knob
    that LOOKS reachable is worse than no knob at all, because a future
    reader has to rediscover that it can't fire before trusting that it
    doesn't.

  - DIVERGENCE from the original (deliberate, 2026-09-01): the ONE
    follow-up EXPIRES. Branch 6 used to offer it forever: a contact with a
    single outbound note and no reply read `follow_up` at 27 business days
    exactly as at 6, and the only expiry anywhere in the tree was the
    thank-you's. Measured on the founder's live queue 2026-09-01: 44 of 44
    cards were "follow up", every one on a note sent 27 business days
    earlier. A stranger re-appearing five weeks after one unanswered email
    is not a follow-up, it is a second cold open wearing a follow-up's
    label, and the research is blunt about it — two or three follow-ups at
    most, then stop; "just let it go". So once the silence since the first
    note passes `followup_expires_after_business_days` (default 15, three
    working weeks) the branch emits `park` with the reason "First note went
    unanswered N weeks ago. Park it, or re-open with a new reason." Same
    shape as branch 1's `thank_you_expires_after_days`: the same strict `>`
    on the window, the same reading of an undated touch as expired, and the
    same argument — a courtesy with a shelf life, past which the right move
    is a fresh reason to talk and not a belated nudge. Tunable from
    Settings (`crm.today.TUNABLE_CADENCE_PARAMS`, 5-60).

  - DIVERGENCE from the original (deliberate, 2026-07-30, "C2"): every idle
    clock now reads the latest REAL touch — every kind EXCEPT
    `manual_override`. The original had no such rows at all (that kind was
    invented here for `set_state`'s audit trail, see pipeline.py), so "the
    last touch" and "the last real touch" named the same set there and the
    distinction could not arise. Here it does, and reading it wrong was
    actively silencing people: promoting a contact to advocate writes a
    `manual_override` row, which branch 5 then read as a fresh touch and
    restarted the 4-week advocate clock — measured on the founder's data,
    both of his advocates were silent for exactly this reason, their clocks
    reset by their own promotion. A bookkeeping row is the system writing to
    itself; it is not evidence that a relationship was maintained, so it
    must not reset a relationship clock. Branches 2, 5, 5b, 6 and 7 all read
    the real-touch clock. Branch 1's thank-you scan and branch 3's re-ping
    scan are untouched: both already filter to specific kinds.

  - DIVERGENCE from the original (deliberate, 2026-07-30, "C1"): a new
    branch 5b, `keep_warm`, for a contact who has actually had the chat
    (warmth `chatted`, thread_state `chat_done`). The ported tree had no
    case for them at all: branch 1 stops asking for a thank-you once the
    note is sent or the window expires, and branches 4-7 all test something
    else, so a chatted contact fell out of the cadence entirely and was
    never surfaced again. Measured on the founder's data, that dead end was
    hiding his 14 warmest non-advocate relationships — the people who
    actually met him — while 29 strangers who ignored one cold email filled
    the queue. The fix is the same shape as the advocate branch: a
    keep-in-touch clock (`chatted_touch_min_weeks`) anchored to the last
    real touch, prompting for a genuine reason to talk rather than a
    belated courtesy. It sits AFTER branch 5 so advocates keep their own,
    slower cadence, and BEFORE branch 6 so it can never be mistaken for
    cold outreach.

  - DIVERGENCE from the original (deliberate, 2026-08-22, "C1b"): branch 5b's
    gate is now WARMTH plus one carve-out, not warmth AND thread_state. C1
    above wrote it as `warmth == "chatted" AND thread_state == "chat_done"`,
    which closed the dead end for the contacts who happened to carry both and
    left a narrower version of the same dead end open for everyone else.

    The two columns drift, by design and in practice. Warmth is a ratchet that
    only ever climbs WARMTH_RANK; thread_state has no such guard outside its
    terminal `advocate` case, and pipeline.py's own docstring records that a
    late-arriving touch can move it BACKWARD. `set_state` and the CSV import
    can each write one column without the other. So `chatted` with a
    thread_state of anything but `chat_done` is an ordinary state, not a
    corruption — and a contact in it matched NO branch at all: 6 tests warmth
    'cold', 7 tests thread_state 'replied', and 1-5 had all fallen through.
    Measured on the founder's account the day this was written, 13 of his 23
    chatted contacts sat outside `chat_done`; all 13 happened to be `parked`
    (branch 4's deliberate exit, still respected), so the leak cost him
    nothing THAT day — but the hole was one `set_state` away from swallowing a
    tier-1 relationship, and a branch that only covers the states its author
    happened to test is not a closed dead end.

    `replied` is the one carve-out: branch 7 owns it and has something
    strictly better to say ("they replied — propose a chat"), the same
    precedence branch 5 already takes over branch 7 for advocates.

  - DIVERGENCE from the original (deliberate, 2026-07-30, "C3"): branch 7's
    warmth set gained `chatted` (was `("replied", "cold")`, now
    `("replied", "cold", "chatted")`). A contact who has chatted and then
    replies again lands in thread_state `replied` with warmth already
    ratcheted up to `chatted`, and the ported branch's warmth test excluded
    exactly that combination — so re-engaging AFTER a chat made a contact
    less visible than never having chatted at all. `advocate` stays out on
    purpose: branch 5 owns advocates and returns before this branch runs.

  - DIVERGENCE from the original (deliberate, 2026-08-27, "C4"): branch 1 no
    longer prompts for a thank-you when the chat is in the FUTURE. The original
    could not hit this — it read `datetime.now()` inside the branch, and a
    stored chat timestamp ahead of the wall clock was not a shape its callers
    produced. Here `as_of` is caller-supplied, and a `chat` touch dated after
    it arrives from a calendar-sourced capture, from a hand-logged chat entered
    with tomorrow's date, and from any caller whose `as_of` clock runs behind
    the touch's. The branch fired anyway: `hrs` came out negative, `-720 > 168`
    is False so nothing looked expired, and the queue produced a priority-1
    `thank_you` reading "chat done -720h ago — send thank-you (within 24h)".
    TOUCH_TRANSITIONS' own comment in pipeline.py already names this failure
    and defends it by convention (use `chat_scheduled` for a future chat);
    this is the same rule enforced rather than requested.

  - DIVERGENCE from the original (deliberate, 2026-08-27, "C5"): the action
    sort gained a fourth, final key — the contact id — so the returned order is
    a TOTAL order rather than three keys plus `list.sort`'s stability over
    whatever sequence the caller iterated. Determinism is this module's
    headline property (see the note at the end of this docstring) and it was
    only true up to ties: six contacts tying on (priority, tier, firm_name),
    shuffled 50 times, produced 48 distinct outputs, and the web layer's fetch
    carries no ORDER BY. Nothing the ported keys already separated moves.

  - DIVERGENCE from the original (deliberate, 2026-09-02, "C6"): branch 5a,
    the post-chat promised-action follow-up. The original had exactly one
    post-chat clock and this port had two (advocate, chatted); neither
    distinguished "we had a nice chat" from "they said they would introduce
    me to somebody". The sources make that the ONLY cadence split they
    support: cold with no reply is about two weeks, post-chat WITH A PROMISED
    ACTION is about one week and is the one interval anybody counted (70 to
    80% reply), post-chat with nothing promised is six weeks or more and
    event-triggered (`research-networking-norms.md §8d`). So the split is on
    relationship state, not on who the contact is — there is deliberately no
    seniority, track or region term here, because §8a and §8f of the same
    file looked for one and found none.

    `chatted_touch_min_weeks` is UNTOUCHED at either end. It already sits at
    the aggressive end of the evidenced range (§1d), the non-target sources
    do not contradict it (`research-nontarget-access.md` Verdict §4), and it
    is the founder's own recorded dial. Branch 5a does not shorten it; it
    fires on a DIFFERENT FACT (an open promise) that the keep-warm clock
    cannot see, and a contact with no promise reaches 5b at exactly the day
    it always did.

  - `tasks_from_change()`: the backward planner. Fires ONLY on
    `confirmed_official` changes (rumor / reported never spawn a task).
    `app_open` -> advocates-in-place task 14d before; `app_close` ->
    re-ping 14d before + submit 5d before; `insight_deadline` -> apply 7d
    before. The <= 3-day in-place-update rule lives in
    `plan_task_write()`, which decides whether a freshly planned task is a
    duplicate of an existing one, an in-place date update, or genuinely
    new — the exact `db.upsert_task` semantics, lifted off the DB.

This module is PURE: unlike `pipeline.py` (which writes to a DB and takes a
connection), the cadence engine only READS and COMPUTES. Every function
takes plain Python data structures (lists of contact / touch / firm_date
dicts, a firm-metadata mapping) and an explicit as-of `datetime`, and
returns plain dicts. It imports nothing from Django, psycopg, or the
network. The web layer fetches the rows (scoped to one `user_id`) and hands
them in; this module never issues a query and never learns a tenant id —
tenancy was enforced at fetch time.

Determinism: no wall-clock read anywhere. The original `due_actions` called
`datetime.now()` inside its thank-you "hours since chat" math; here that
single clock read becomes the caller-supplied `as_of`, so the same inputs
always produce byte-identical output (a golden-fixture-testable property the
original lacked).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Rule parameters — ported verbatim from campaign/cadence.yaml (9 lines, no
# code there). Module-level defaults; every function accepts a `params`
# mapping that overrides individual keys, so the web layer can carry global
# defaults here and still let a value be tuned without a code change.
# ---------------------------------------------------------------------------
CADENCE_DEFAULTS: dict[str, int] = {
    # Gap before the ONE follow-up a cold, never-replied contact gets. Business
    # days since the last touch, set to clear a full calendar week — which
    # takes some arithmetic: `business_days_since` counts Mon-Fri only, so 5
    # business days is exactly 7 calendar days. A window of 5 therefore sits ON
    # one week, not beyond it — the boundary case, and the wrong side of the
    # owner's "more than a week". 6 is the smallest window that always clears
    # it (8 calendar days from a Monday touch, 10 from a Friday one, since the
    # gap then swallows two weekends).
    "followup_after_business_days": 6,   # gap before the (only) follow-up
    "park_after_business_days": 10,      # after max_cold_touches, no reply this long -> park
    # ...and the one follow-up itself STOPS being offered once the silence
    # since the first note passes this many business days. The sibling of
    # `thank_you_expires_after_days` below, and the same argument: a courtesy
    # with a shelf life. Nudging a stranger the week after a note is a
    # follow-up; nudging them five weeks later is a second cold open wearing
    # a follow-up's label, and the research says stop well before that
    # ("2-3 follow-ups max, then just let it go"). Past the window branch 6
    # emits `park` instead, with the reason naming the weeks, so the student
    # either lets it go or comes back with a NEW reason rather than the old
    # note re-sent. 15 is three working weeks: clear of the default follow-up
    # window (6) by enough that a student who missed the card for a fortnight
    # still gets it, and short of the point where the note is a memory.
    # Measured on the founder's queue 2026-09-01: 44 of 44 cards were
    # follow-ups on a 27-business-day-old note; at 15 every one of them
    # parks. Strict `>`, like the thank-you window, so a thread on day 15
    # exactly still gets its follow-up.
    "followup_expires_after_business_days": 15,
    # Deliberately capped at 2 in crm.views.TUNABLE_CADENCE_PARAMS: one
    # outreach note, one follow-up, then park — never a second follow-up. See
    # that constant's comment for why this is enforced structurally rather
    # than left as a preference.
    "max_cold_touches": 2,               # initial note + 1 follow-up, then park
    "thank_you_within_hours": 24,        # after any chat
    # ...but the prompt STOPS after this many days. A thank-you note is a
    # courtesy with a shelf life: sent the next morning it lands well, sent
    # three weeks later it reads as an afterthought and draws attention to the
    # silence. Past this window the right move is a fresh reason to talk, not a
    # belated thanks, so the contact falls through to the rest of the cadence
    # instead of being nagged forever. Surfaced by the founder cutover, which
    # replayed months of historical chats and produced a wall of stale
    # thank-you prompts on day one.
    "thank_you_expires_after_days": 7,
    "advocate_touch_min_weeks": 4,       # keep advocates warm every 4-6 weeks
    "advocate_touch_max_weeks": 6,
    # Branch 5b's keep-warm clock for contacts who have HAD the chat. Tighter
    # than the advocate pair on purpose: an advocate is a settled relationship
    # you are maintaining, a freshly-chatted contact is a live referral
    # candidate mid-cycle, and the gap between "we spoke" and "who was that
    # again" is shorter than a month. `chatted_touch_max_weeks` is display
    # only — it renders the range in the reason string and gates nothing —
    # exactly like `advocate_touch_max_weeks`. Being display-only is also why
    # neither `_max_` key is tunable, and why the pair can end up crossed;
    # `_target_window` below is the one place that has to cope with it.
    "chatted_touch_min_weeks": 3,        # keep chatted contacts warm every 3-5 weeks
    "chatted_touch_max_weeks": 5,
    # Branch 5a's clock: how long a PROMISE sits before the queue asks the
    # student to chase it. Calendar days, measured from the day the promise
    # was recorded, not from the last touch — the promise is the fact, so it
    # is the thing that gets aged.
    #
    # WHAT 7 ENCODES. This is the one post-chat interval anybody in the
    # corpus actually counted: a contact who left a chat having promised
    # something replies at 70 to 80% when chased at about a week, and the
    # sources split cadence on relationship state and on nothing else — cold
    # with no reply about two weeks, promised-action about one week, nothing
    # promised six weeks or more and event-triggered
    # (`research-networking-norms.md §8d`).
    #
    # WHY THAT MAGNITUDE AND NOT SHORTER. The one-week-after-a-cold-note
    # number that circulates in this domain is a business-to-business sales
    # import with no evidential basis (§1b, Grade D) and is explicitly not
    # what this is: the difference is that a promise creates a legitimate
    # reason to write, and the week is the interval on which the promiser
    # still remembers making it. Shorter reads as chasing a favour; longer
    # and the intro has gone cold in the promiser's inbox.
    #
    # WHY IT IS A PRODUCT CONSTANT AND NOT A SETTINGS KNOB. Every key in
    # `crm.today.TUNABLE_CADENCE_PARAMS` is a preference about how hard the
    # student wants to be pushed. This is not a preference — it is how long
    # a person remembers a promise — and the Settings page's own cadence
    # diagram narrates the tunable set as a sentence, so a seventh spinner
    # would have to claim a taste question this is not.
    #
    # WHAT WOULD CHANGE IT: a counted reply-rate curve against days-to-chase
    # on real Coverage sends. There is none yet; the 70 to 80% figure is a
    # single practitioner count, so this number is evidence-shaped, not
    # measured here.
    "promised_followup_after_days": 7,
    "pre_deadline_reping_days": 14,      # re-ping warm contacts when a CONFIRMED app_close is this near
    # `stale_thread_days: 21` used to sit here, carried over from
    # campaign/cadence.yaml and labelled "kept for parity; used by status-style
    # reports, not due_actions". Removed 2026-09-01: grepped the whole repo and
    # NOTHING reads it — not this module, not `crm`, not the digest, not a
    # template, not a test. There are no status-style reports. It was a knob
    # that looked tunable, sat in the public defaults bundle, and could not
    # change any output.
    #
    # This module already states the rule it was breaking, in the 2026-07-28
    # reverted-divergence note: "a configuration knob that LOOKS reachable is
    # worse than no knob at all, because a future reader has to rediscover that
    # it can't fire before trusting that it doesn't." Applied to its own
    # defaults rather than only to the one that prompted it.
    "advocate_target": 2,                # advocates-in-place yardstick (from profile.advocate_target)
}

# Confirmed-buckets vocabulary — the only confidence value that may spawn a
# task or drive a re-ping, matching confidence.py's domain-cap ceiling.
CONFIRMED = "confirmed_official"

# Warmth values that count as "warm enough to re-ping" (branch 3).
_WARM = ("replied", "chatted", "advocate")

# Outbound touch kinds — what counts as "you reached out" for the cold-cadence
# touch count (branch 6).
_OUTBOUND_KINDS = ("outreach", "follow_up")

# The audit row `pipeline.set_state` writes when someone corrects a contact's
# state by hand. Deliberately kept out of `pipeline.TOUCH_TRANSITIONS` there,
# and excluded from every idle clock here (see the C2 DIVERGENCE note): it
# records that the SYSTEM wrote something down, never that the relationship
# was touched. Named rather than inlined so the one concept has one spelling
# across the module.
_MANUAL_OVERRIDE_KIND = "manual_override"

# Kinds that must never reset a relationship's idle clock. Both are rows the
# system wrote about itself rather than evidence that a relationship was
# maintained — the C2 DIVERGENCE note above is the full argument, and it holds
# identically for both members:
#
#   - `manual_override`: the audit row for a hand correction (C2's original
#     case — promoting a contact to advocate was restarting the advocate clock
#     by its own promotion, silencing both of the founder's advocates).
#   - `bulk_received`: an inbound blast — a programme invite, a newsletter, an
#     automated notice — that `capture.inbound` judged from message headers.
#     Added 2026-08-22 alongside that classifier. Without this, a mass mailshot
#     from a firm would read as "this relationship was maintained" and push the
#     person back down the queue for another few weeks, which is precisely the
#     false-evidence problem the classifier exists to end. It is recorded and
#     visible on the contact; it is simply not a touch.
_CLOCK_SILENT_KINDS = frozenset({_MANUAL_OVERRIDE_KIND, "bulk_received"})


def _merged_params(params: Mapping[str, Any] | None) -> dict[str, int]:
    merged = dict(CADENCE_DEFAULTS)
    if params:
        merged.update(params)
    return merged


def _target_window(min_weeks: int, max_weeks: int) -> str:
    """The "every N–M weeks" clause the two keep-warm branches print.

    ONE HELPER BECAUSE THE INVERSION HAS TWO DOORS. Each keep-warm clock is a
    pair: a `_min_weeks` that GATES the branch and a `_max_weeks` that is
    display only (see CADENCE_DEFAULTS). Only the min half of each pair is in
    `crm.today.TUNABLE_CADENCE_PARAMS`, so a student who widens the min past
    the product's fixed max leaves the two crossed and the copy renders a
    range that counts backwards.

    THE LIVE CASE, not a hypothetical: the founder has
    `chatted_touch_min_weeks = 6` against a fixed max of 5, so branch 5b was
    one `keep_warm` action away from printing "target every 6–5 weeks". The
    advocate pair has the identical hole — tune `advocate_touch_min_weeks` to
    8 and you get "8–6" — and its copy was ALREADY fixed once, on 2026-08
    ("a hardcoded 4–6 used to keep printing after the min was tuned away"),
    which moved the min into the string and left the max behind. Half a fix
    on one branch, the same bug untouched on the other; both are closed here.

    A crossed or equal pair collapses to the single number that is true. The
    min is what the engine actually enforces; the max never gated anything, so
    dropping it costs the sentence nothing it was entitled to say. The
    tempting alternative — widen the max to `min + 2` to preserve the
    default's two-week spread — is rejected on this module's own standing
    rule: it would print a number the student never set and the engine never
    uses, which is a confident guess dressed as a setting.

    Three options were considered for this and the other two rejected in the
    same spirit. Clamping `max` up to `min` at merge time yields "every 6–6
    weeks", a range with no width, which is this same guess with worse
    grammar. Adding `*_touch_max_weeks` to `TUNABLE_CADENCE_PARAMS` would put
    a knob on the Settings page that changes copy and nothing else — the
    "a knob that LOOKS reachable is worse than no knob at all" rule that
    `stale_thread_days` was deleted under, arriving from the other direction.
    """
    if max_weeks > min_weeks:
        return f"every {min_weeks}–{max_weeks} weeks"
    return f"every {min_weeks} week{'' if min_weeks == 1 else 's'}"


def infer_region(source: str | None) -> str | None:
    """'hk' if the contact's free-text `source` mentions HK, else 'us' — the
    same two-way call the original `netdash.data.infer_region` made. A
    missing/empty source returns None rather than guessing 'us', so branch 3
    can stay conservative (match either region) when region truly can't be
    determined. Ported from campaign/src/campaign/region.py.

    RETIRED FROM THE READ PATH (2026-07-27) — `contact_region` no longer calls
    this, and nothing else in the engine does either. It is kept because it is
    the only written record of how region used to be decided, and because a
    ONE-TIME data migration is the right place to use it if the founder ever
    wants the old inference materialised into the `region` column, where a
    human can see it and correct it. Running it on every read is what made it
    harmful: `source` is a provenance string, not a region, so this returns a
    confident answer for any non-empty input and the "unknown" case it
    documents was unreachable in practice. See `contact_region`."""
    if not source:
        return None
    return "hk" if "hk" in source.lower() else "us"


def contact_region(contact: Mapping[str, Any]) -> str | None:
    """The region a contact belongs to, or None when it genuinely isn't known.

    Only the explicit `region` key answers. Blank means UNKNOWN, and unknown
    is a real answer here — branch 3 handles it by matching the soonest close
    across any region for the firm, which is the conservative behaviour the
    whole design documents. "other" (known to be outside both us and hk) is
    returned verbatim: it names no per-region close bucket, so branch 3
    scopes such a contact to no us/hk deadline — deliberately distinct from
    the unknown fallback.

    `infer_region(source)` is deliberately NOT consulted (see that function).
    While it was, this returned a confident region 100% of the time and the
    unknown path below could never be taken."""
    return (contact.get("region") or "").strip().lower() or None


def business_days_since(then: date, now: date) -> int:
    """Whole business days (Mon-Fri) strictly after `then` up to and
    including `now`. Ported verbatim from the original cadence.py."""
    if then >= now:
        return 0
    n, cur = 0, then
    while cur < now:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _as_dt(value: Any) -> datetime | None:
    """Coerce a touch/firm_date timestamp to a tz-aware UTC datetime.

    Accepts a `datetime` (naive assumed UTC), a `date`, or an ISO-8601
    string (the original stored touch timestamps as 'YYYY-MM-DD HH:MM'
    strings; Postgres hands back real datetimes — both are tolerated here so
    fixtures and real rows behave identically). Returns None if unparseable,
    mirroring the original's defensive `_touch_date`/`_hours_since` (which
    returned None on a bad parse rather than raising).

    Tries `fromisoformat` FIRST, before the `strptime` fallback formats.
    `fromisoformat` understands a trailing UTC offset ("+08:00"); the
    `strptime` formats below do not, and — because `text[: len(fmt) + 2]`
    truncates the string to roughly the format's own length before parsing
    — silently chop the offset off rather than raising, so a
    "2026-07-27 09:00:00+08:00" timestamp used to parse as 09:00 UTC: an
    unflagged 8-hour shift. Trying `fromisoformat` first parses the offset
    correctly whenever it's present; the `strptime` loop remains only as a
    fallback for stricter/older textual variants `fromisoformat` rejects."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        text = text.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    return None


def _as_date(value: Any) -> date | None:
    dt = _as_dt(value)
    return dt.date() if dt is not None else None


def _touch_dt(t: Mapping[str, Any]) -> datetime | None:
    return _as_dt(t.get("ts"))


def _firm_meta(firms: Mapping | Iterable[Mapping] | None) -> dict[Any, dict]:
    """Normalize the `firms` argument to {firm_id: {"name", "tier"}}.

    Accepts either a mapping keyed by firm id, or an iterable of firm dicts
    (each carrying `id` or `firm_id`). Tier lives on the per-user
    `user_firms` join in the multi-tenant schema, so the caller is expected
    to fold it in before handing firms here; a missing tier defaults to 3
    and a missing name to the id, exactly as the original
    `config.firms()`-backed lookups defaulted.

    `crm.UserFirm.tier` is a nullable column, and `crm.views.set_firm_tier`
    deliberately writes `tier=None` when a firm is dragged to the
    "Unranked" lane — a real, on-the-record value, not an absent key. So the
    None-coercion below is required in addition to `dict.get`'s default:
    `f.get("tier", 3)` only substitutes 3 when the key is MISSING, and would
    otherwise hand back a bare `None` that later breaks a `sort()` comparing
    tiers to ints (see `due_actions`).

    `_coerce_tier` generalises that same guard: `None` was the shape that
    actually shipped a TypeError, but it is not the only non-comparable value
    this argument can carry. The mapping is caller-built (`crm.today` folds
    `UserFirm.tier` in by hand), and a tier arriving as the STRING "1" — from a
    CSV import, a JSON round-trip, an admin edit, a form value that skipped
    coercion — crashes `due_actions` the same way and with the same message,
    just one type over. Fixing the reported shape and leaving its siblings is
    how the same bug ships twice."""
    out: dict[Any, dict] = {}
    if firms is None:
        return out
    if isinstance(firms, Mapping):
        items = firms.items()
    else:
        items = ((f.get("id", f.get("firm_id")), f) for f in firms)
    for fid, f in items:
        out[fid] = {"name": f.get("name", fid), "tier": _coerce_tier(f.get("tier"))}
    return out


def _coerce_tier(tier: Any) -> int | float:
    """A tier that can be compared against other tiers in `due_actions`' sort.

    `None` (the "Unranked" lane `crm.views.set_firm_tier` writes deliberately)
    and anything non-numeric fall back to 3, the same default a firm missing
    from the metadata mapping already gets. `bool` is excluded explicitly
    because it is an `int` subclass in Python and `True` would otherwise sort
    as tier 1."""
    if isinstance(tier, bool) or not isinstance(tier, (int, float)):
        return 3
    return tier


def _closing_soon(
    firm_dates: Iterable[Mapping[str, Any]], today: date, reping_days: int
) -> dict[Any, dict[str | None, date]]:
    """firm_id -> {region: soonest confirmed app_close within the window}.

    Ported from the original's closing_soon build. Only `confirmed_official`
    app_close dates within `reping_days` qualify (the original relied on
    `kb.confirmed_deadlines()` already filtering to confirmed; here the
    confidence check is explicit on each firm_date row). Keyed by region so
    branch 3 can scope the re-ping to the contact's own region.

    A date already in the past is dropped, not just one too far in the
    future: without this, a stale `confirmed_official` app_close that has
    already come and gone would still occupy the per-region `min()` bucket
    below and could beat out (and hide) a genuinely upcoming close for the
    same firm/region, on top of firing a priority-0 re-ping for a deadline
    that's already over."""
    out: dict[Any, dict[str | None, date]] = {}
    for e in firm_dates:
        if e.get("event_kind") != "app_close":
            continue
        if e.get("confidence") != CONFIRMED:
            continue
        d = _as_date(e.get("date"))
        if d is None or d < today or (d - today).days > reping_days:
            continue
        fid = e.get("firm_id")
        # Case-normalized only — NOT collapsed with the blank/None fallback,
        # which is a separate, deliberate behavior (see contact_region) that
        # needs a product decision before it changes.
        region = e.get("region")
        region = region.lower() if isinstance(region, str) else region
        bucket = out.setdefault(fid, {})
        if region not in bucket or d < bucket[region]:
            bucket[region] = d
    return out


def due_actions(
    contacts: Iterable[Mapping[str, Any]],
    touches: Iterable[Mapping[str, Any]],
    firm_dates: Iterable[Mapping[str, Any]] | None = None,
    *,
    as_of: datetime,
    firms: Mapping | Iterable[Mapping] | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[dict]:
    """All cadence-due actions for one user's contacts, ranked.

    Pure port of campaign `cadence.due_actions`. The caller fetches the
    user's non-archived `contacts`, all their `touches`, the relevant shared
    `firm_dates`, and a `firms` metadata mapping (name + per-user tier),
    then passes them in. Nothing here reads a database or a wall clock.

    Args:
        contacts: contact dicts. Keys used: `id`, `firm_id` (or `firm`),
            `firm_text` (display fallback when firm_id is None), `warmth`,
            `thread_state`, `region` ("us" / "hk" / "other" for known to
            be outside both markets / blank-or-absent for unknown — the
            ONLY key consulted for region; see `contact_region`),
            `archived` (optional; truthy rows are skipped),
            `chat_scheduled_at` (optional; see below).
        touches: touch dicts across those contacts. Keys used: `contact_id`,
            `ts`, `kind`.
        firm_dates: shared firm_date dicts. Keys used: `firm_id`,
            `event_kind`, `region`, `date`, `confidence`.
        as_of: the as-of instant. `today = as_of.date()` drives all
            business-day math; `as_of` itself drives the thank-you
            "hours since chat" calculation (the original's one
            `datetime.now()` read).

    CALENDAR-DAY MATH IS DECIDED BY THE ZONE ITS INPUTS ARRIVE IN. `today`
    is `as_of.date()` and every touch date is `_as_date(t["ts"]).date()`, so
    two timestamps naming the same instant in different zones can land on
    different calendar days and the same event can be counted twice over.
    Measured on the founder's live account 2026-08-31: `as_of` arrived as a
    local (America/Los_Angeles) instant while touch rows arrived as UTC, so a
    touch stored 2026-08-24 01:37Z (2026-08-23 18:37 local) was read as Aug 24
    here and Aug 23 by the web layer's own ledger line, and one Today card
    printed "5 business days" in the engine's sentence and "6 business days
    ago" in the row directly beneath it. The engine cannot detect the skew —
    both values are valid aware datetimes — so the contract is on the caller:
    hand `as_of` AND every `ts` in the SAME zone, the user's own. See
    `crm.utils._touch_dicts`, which is where that conversion now happens for
    every caller at once.

    ONE MORE OPTIONAL CONTACT KEY. `chat_scheduled_at` is the real start time
    of a chat this contact has on the books, or absent when none is known
    (the usual case — Coverage only learns a chat time from an .ics DTSTART).
    Branch 2 is the only reader: with it the confirm-chat card may name the
    day, and a time still in the future suppresses the card entirely; without
    it the card says how long the thread has been quiet and names no day at
    all. Same zone contract as `ts` above.
        firms: firm metadata (see `_firm_meta`).
        params: overrides for `CADENCE_DEFAULTS`.

    Returns:
        A list of action dicts, each:
            {"contact", "action", "reason", "priority", "tier",
             "firm_name", "firm_known", "ctx"}
        `contact` is the input dict as-is (the web layer needs it). `ctx`
        carries the raw numbers the reason string renders, so a UI can build
        its own phrasing without re-parsing `reason`. Sorted by
        (priority, tier, firm_name) — priority 0 is most urgent.
    """
    p = _merged_params(params)
    followup_bd = int(p["followup_after_business_days"])
    followup_expiry_bd = int(p["followup_expires_after_business_days"])
    park_bd = int(p["park_after_business_days"])
    max_cold = int(p["max_cold_touches"])
    ty_hours = int(p["thank_you_within_hours"])
    ty_expiry_days = int(p["thank_you_expires_after_days"])
    adv_min_weeks = int(p["advocate_touch_min_weeks"])
    adv_min_days = adv_min_weeks * 7
    adv_max_weeks = int(p["advocate_touch_max_weeks"])
    chat_min_weeks = int(p["chatted_touch_min_weeks"])
    chat_min_days = chat_min_weeks * 7
    chat_max_weeks = int(p["chatted_touch_max_weeks"])
    promised_days = int(p["promised_followup_after_days"])
    reping_days = int(p["pre_deadline_reping_days"])

    today = as_of.date()
    meta = _firm_meta(firms)
    closing_soon = _closing_soon(firm_dates or (), today, reping_days)

    # Group touches by contact once, sorted ascending by timestamp.
    by_contact: dict[Any, list[Mapping[str, Any]]] = {}
    for t in touches:
        by_contact.setdefault(t.get("contact_id"), []).append(t)
    for lst in by_contact.values():
        lst.sort(key=lambda t: (_touch_dt(t) or datetime.min.replace(tzinfo=timezone.utc)))

    actions: list[dict] = []
    for c in contacts:
        if c.get("archived"):
            continue
        cid = c.get("id")
        ctouches = by_contact.get(cid, [])
        firm_id = c.get("firm_id", c.get("firm"))
        # A hand-added contact can genuinely have no firm at all (no firm_id,
        # no firm_text) — capture_discover.py writes exactly that when the
        # source record named no employer. The old terminal fallback was the
        # literal string "?", which templates render verbatim as a bare
        # question mark next to the contact's name instead of a label.
        # "No firm listed" is that same kind of terminal placeholder, and it
        # has the identical problem one level up: nothing distinguishes it
        # from a real employer name once it reaches the template, so it
        # rendered in the exact same slot/styling as ACCRACARE or ENDPOINT.
        # `firm_known` travels alongside it so the template can style the
        # placeholder differently, the way `is-school` already does for a
        # university sitting in the same slot.
        firm_known = bool(
            meta.get(firm_id, {}).get("name") or c.get("firm_text") or firm_id
        )
        firm_name = meta.get(firm_id, {}).get("name") or c.get("firm_text") or firm_id or "No firm listed"
        # Same coercion as _firm_meta (a `.get(..., 3)` default alone would not
        # catch an explicit `tier=None` from an "Unranked" drag — that used to
        # raise a TypeError comparing None to int in the `actions.sort()`
        # below). Shared helper rather than a second copy of the rule, so the
        # two places that must agree about what a tier is cannot drift.
        tier = _coerce_tier(meta.get(firm_id, {}).get("tier"))

        # The idle clock reads the last REAL touch, skipping the audit rows
        # `set_state` writes (C2). `ctouches` is already sorted ascending, so
        # filtering preserves the order and the last element is still the
        # most recent. Branch 1 and branch 3 keep reading `ctouches` — they
        # scan for specific kinds ('chat'/'thank_you' and 'reping'), which a
        # manual_override row can never be.
        real_touches = [t for t in ctouches if t.get("kind") not in _CLOCK_SILENT_KINDS]
        last = real_touches[-1] if real_touches else None
        lt_date = _as_date(last.get("ts")) if last else None

        def add(action: str, reason: str, prio: int, **ctx: Any) -> None:
            actions.append({
                "contact": c, "action": action, "reason": reason,
                "priority": prio, "tier": tier, "firm_name": firm_name,
                "firm_known": firm_known, "ctx": ctx,
            })

        thread_state = c.get("thread_state")
        warmth = c.get("warmth")

        # 1. thank-you within `ty_hours` of a chat, scoped to the LATEST chat
        #    touch. Once thanked, the contact FALLS THROUGH to the reping /
        #    maintain cadence below instead of dropping out forever.
        if thread_state == "chat_done":
            chats = [t for t in ctouches if t.get("kind") == "chat"]
            latest_chat = chats[-1] if chats else None
            chat_dt = _touch_dt(latest_chat) if latest_chat else None
            thanked = False
            for t in ctouches:
                if t.get("kind") != "thank_you":
                    continue
                t_dt = _touch_dt(t)
                if chat_dt is None or (t_dt is not None and t_dt >= chat_dt):
                    thanked = True
                    break
            hrs = None
            if chat_dt is not None:
                hrs = (as_of - chat_dt).total_seconds() / 3600
            # The window has closed: too late for the note to read as a thanks.
            # Deliberately checked BEFORE `thanked`, so an expired prompt and a
            # sent one behave identically from here on — both fall through.
            #
            # An undatable chat (chat_dt is None — reachable via the
            # import/reconciliation path, which can write a `chat` touch
            # without a parseable `ts`) counts as expired too: if you can't
            # date the chat, you can't claim the thank-you is still timely,
            # and treating it as "not yet expired" made `hrs is None` stay
            # False forever, pinning an immortal daily thank-you prompt.
            expired = chat_dt is None or (hrs is not None and hrs > ty_expiry_days * 24)
            # The chat has not happened yet. This module's own TOUCH_TRANSITIONS
            # comment already names the failure ("a reply that only
            # confirms/schedules a FUTURE chat must use `chat_scheduled`, or
            # due_actions will wrongly report a not-yet-happened chat as overdue
            # for a thank-you") and then defends against it by convention alone.
            # Convention is not a guard: a `chat` touch dated ahead of `as_of` is
            # reachable from a calendar-sourced capture, a hand-logged chat
            # entered with tomorrow's date, and — routinely — from any caller
            # whose `as_of` is behind the touch's own clock (see the UTC-vs-local
            # skew the web layer currently has). Measured before this line
            # existed: a chat 30 days out produced a priority-1 `thank_you`
            # reading "chat done -720h ago — send thank-you (within 24h)", i.e.
            # the engine asking a student to thank somebody for a conversation
            # that has not occurred, and printing a negative age to do it.
            #
            # NOT clamped to zero, and the difference matters: clamping would
            # still fire the prompt, just with an honest-looking "0h ago". The
            # thank-you is not merely mis-worded here, it is not owed at all, so
            # the branch says nothing and the contact falls through to the rest
            # of the cadence exactly as an expired or already-thanked one does.
            # Nothing downstream then fires either (branch 5b's own clock reads
            # the same future touch as negative days idle), which is correct:
            # the chat is tomorrow and there is nothing to do about it today.
            not_yet = hrs is not None and hrs < 0
            if not thanked and not expired and not not_yet:
                overdue = hrs is not None and hrs > ty_hours
                add(
                    "thank_you",
                    "chat done"
                    + (f" {hrs:.0f}h ago" if hrs is not None else "")
                    + " — send thank-you"
                    + (" (OVERDUE)" if overdue else f" (within {ty_hours}h)"),
                    0 if overdue else 1,
                    hours=hrs, overdue=overdue, window_hours=ty_hours,
                )
                continue
            # Thanked for the latest chat, or the window has closed — fall
            # through to the reping / maintain cadence below either way.

        # 2. a chat being arranged that has gone quiet > 4 business days.
        if thread_state == "chat_scheduled":
            # WHAT `bd` MEASURES, AND WHAT THE COPY USED TO CLAIM IT MEANT.
            # `bd` is business days since the LAST TOUCH — since Coverage last
            # logged anything about this contact. The sentence here used to
            # render it as "chat was scheduled {bd} business days ago", which
            # is a different fact, and on the common path not even a related
            # one: the last touch IS the `chat_scheduled` touch (the moment a
            # mailbox scan read scheduling-ish language), so the card dated a
            # booking off a clock that only knows when we last wrote something
            # down.
            #
            # LIVE CASE, founder's account 2026-08-31 (Youqi Chen). Her contact
            # notes read "Replied to your email: coffee in HK, offered same-day
            # meetup" — she OFFERED, he never confirmed, nothing was booked,
            # and she carries zero CalendarEvents. The card still read "chat
            # was scheduled 6 business days ago": a booking that never existed,
            # on a date nobody ever named. Same class of defect this codebase
            # has fixed twice already — "first seen" printed as "posted", and a
            # "New" badge that only meant "we imported it". State what the data
            # entails and nothing else.
            #
            # `chat_scheduled_at` IS USUALLY ABSENT, and that is the designed
            # state, not a degraded one. `capture.gmail._upsert_scheduled_chat`
            # creates a `CalendarEvent` only when the finding carried a real
            # .ics DTSTART, and says so in its own docstring: "A finding with
            # no time is not an error — most are — it simply makes no event."
            # `crm.models.CalendarEvent` records that before it existed "we do
            # not store a chat datetime anywhere". So the branch has three
            # sentences, not one with a hole in it: one for a time we hold, one
            # for a silence we can date, one for a contact we cannot date at
            # all. Only the first is allowed to name a day.
            #
            # The key is threaded in from the Django side through the contact
            # dict (`crm.today._build_actions`), because this package is pure
            # and cannot reach `crm.CalendarEvent` itself.
            sched = _as_dt(c.get("chat_scheduled_at"))
            # A chat still ahead of us is not stale, and "did it happen?" is
            # the wrong question to ask about it. Same guard and same reasoning
            # as branch 1's `not_yet` above: the engine must not ask a student
            # to confirm a conversation it can see has not occurred. Reachable
            # whenever the booked time is further out than the silence window —
            # a chat set three weeks ahead, then nothing logged for a week.
            if sched is not None and sched > as_of:
                continue
            # `bd is None` (no dateable touch on record) is treated the same
            # as "definitely stale" — the branch still needs to surface
            # SOMETHING, but the reason text below says so honestly instead
            # of rendering a 999-day sentinel as if it were a real count.
            bd = business_days_since(lt_date, today) if lt_date else None
            if bd is None or bd > 4:
                if sched is not None:
                    # The only branch that may state a scheduling date,
                    # because it is the only one holding one. ISO here on
                    # purpose: `crm.today._prose_dates` rewrites it into the
                    # same "Aug 24" the rest of the card speaks.
                    reason = (
                        f"chat was scheduled for {sched.date().isoformat()} — "
                        "did it happen? log the chat or reschedule"
                    )
                elif bd is None:
                    reason = (
                        "a chat was being arranged and no touches are on "
                        "record — did it happen? log the chat or reschedule"
                    )
                else:
                    # No day is named, because none is held. What IS known is
                    # how long Coverage has gone without logging anything, and
                    # the sentence says exactly that and stops.
                    reason = (
                        f"a chat was being arranged, nothing logged in {bd} "
                        "business days — did it happen? log the chat or "
                        "reschedule"
                    )
                add(
                    "confirm_chat", reason, 1,
                    # `business_days` IS AND ONLY IS "business days since the
                    # last real touch". It is NOT the age of the booking, and
                    # when `scheduled_on` is set the two are different facts
                    # sitting in one dict: a chat booked for Aug 24 whose
                    # booking email is the last thing on record has a
                    # `business_days` measured off the EMAIL. The sentence
                    # above is careful about this (it names a day only from
                    # `scheduled_on`, and prints `bd` only in the branch where
                    # no day is held); `ctx` is the machine-readable half of
                    # the same promise, so the name has to carry the same
                    # care. Verified 2026-09-01: no template and no view reads
                    # this key today, so nothing renders the mismatch — the
                    # comment is here so the first reader who wants to cannot
                    # do it by accident.
                    business_days=bd,
                    # None whenever no real time is on record, which is the
                    # normal case. A UI reading `ctx` gets the same three-way
                    # answer the sentence does rather than having to parse it.
                    scheduled_on=sched.date().isoformat() if sched else None,
                )
            continue

        # 3. pre-deadline re-ping for warm contacts at closing firms, scoped
        #    to the contact's own region.
        by_region = closing_soon.get(firm_id)
        close: date | None = None
        if by_region:
            region = contact_region(c)
            if region is None:
                close = min(by_region.values())
            elif region in by_region:
                close = by_region[region]
        if close is not None and warmth in _WARM:
            window_start = close - timedelta(days=reping_days)
            already = any(
                t.get("kind") == "reping"
                and (_as_date(t.get("ts")) or date.min) >= window_start
                for t in ctouches
            )
            if not already:
                add(
                    "reping",
                    f"{firm_name} app closes {close.isoformat()} (confirmed) — "
                    "re-ping before you submit",
                    0, close_date=close.isoformat(),
                )
                continue

        # 4. parked / quiet -> skip.
        if thread_state in ("parked", "quiet"):
            continue

        # 5. advocate idle >= advocate_touch_min_weeks -> maintain.
        if warmth == "advocate":
            days = (today - lt_date).days if lt_date else None
            if days is None or days >= adv_min_days:
                # The range in the copy is rendered from the params, not
                # hardcoded — only `advocate_touch_min_weeks` gates this
                # branch and it IS user-tunable from Settings; a hardcoded
                # "4–6" used to keep printing even after the min was tuned
                # away from the default, e.g. to 8. `_target_window` is what
                # keeps the OTHER half of that pair honest: the max is not
                # tunable, so tuning the min past it used to render a range
                # that counts backwards.
                window = _target_window(adv_min_weeks, adv_max_weeks)
                reason = (
                    "advocate — no dateable touch on record "
                    f"(target {window})"
                    if days is None else
                    f"advocate — last touch {days}d ago (target {window})"
                )
                add("maintain", reason, 2, days_since=days, target_min_weeks=adv_min_weeks)
            continue

        # 5b (C1, NOT ported — see the module docstring). The chatted dead
        # end: you had the conversation, the thank-you is sent or expired,
        # and the ported tree had nothing else to say about this person ever
        # again. Same shape as branch 5 above, on its own tighter clock.
        #
        # The gate is WARMTH, with one thread_state carved out (C1b — see the
        # module docstring). It used to be `warmth == "chatted" AND
        # thread_state == "chat_done"`, and that second term is what made this
        # branch a partial fix rather than a whole one: warmth is a RATCHET and
        # thread_state is not, so the two drift apart routinely (an import or a
        # `set_state` can write either alone, and pipeline.py documents
        # thread_state moving BACKWARD off a late-arriving touch). A contact
        # who came out of that drift as chatted/no_reply matched branch 5b's
        # first term and failed its second, and then matched nothing else
        # either: branch 6 needs warmth 'cold', branch 7 needs thread_state
        # 'replied'. They left the tree entirely — the exact dead end C1 was
        # written to close, reopened one thread_state over.
        #
        # `replied` is excluded because branch 7 OWNS it and says something
        # strictly better ("they replied — propose a chat" beats "send an
        # update"); that is the same reason branch 5 returns before 7 for
        # advocates. Everything else that can still be live at this point
        # (no_reply, chat_done, advocate) belongs here.
        #
        # Unconditional `continue`, like branch 5: a chatted contact who isn't
        # due yet has no further branch that could match (6 needs cold, 7 needs
        # thread_state 'replied', which this branch has already excluded), so
        # falling through would only walk two tests that can't fire.
        if warmth == "chatted" and thread_state != "replied":
            # 5a (C6). THE PROMISE, WHICH THE KEEP-WARM CLOCK CANNOT SEE.
            # `promised_action` is a short phrase naming what this person said
            # they would do ("an intro to Jane Doe"), and it is present ONLY
            # while the promise is still open — the caller stops passing it the
            # moment the student sends anything that chases it. Absent or blank
            # on every contact by default, which is why every golden fixture is
            # byte-identical and why a student who has never written a debrief
            # never meets this branch at all (P3).
            #
            # Aged off `promised_action_at`, the day the promise was RECORDED,
            # not off `lt_date`. The two diverge as soon as the student does
            # the thing the cadence already asked for: sending the thank-you
            # resets the last-touch clock while the promise keeps aging, and
            # this module has now been bitten twice by rendering one clock as
            # a different fact (the confirm-chat card, 2026-08-31; the
            # keep-warm card, 2026-09-01). The sentence says what it measures.
            #
            # `continue` unconditionally, exactly as 5b does: a chatted contact
            # with an open promise has no later branch that could match (6
            # needs cold, 7 needs thread_state 'replied', already excluded
            # above), so falling through would only walk two tests that cannot
            # fire — and would let a not-yet-due promise be answered by the
            # slower keep-warm card, which is the wrong ask about the same
            # person.
            promised = str(c.get("promised_action") or "").strip()
            if promised:
                p_date = _as_date(c.get("promised_action_at"))
                p_days = (today - p_date).days if p_date else None
                if p_days is None or p_days >= promised_days:
                    reason = (
                        f"they offered {promised}, no date on record — chase it"
                        if p_days is None else
                        f"they offered {promised} {p_days}d ago — chase it "
                        f"(target {promised_days} days)"
                    )
                    add(
                        "promised_followup", reason, 1,
                        promised=promised, days_since=p_days,
                        target_days=promised_days,
                    )
                continue
            days = (today - lt_date).days if lt_date else None
            if days is None or days >= chat_min_days:
                # Range rendered from the params, never hardcoded — the same
                # rule (and the same bug) as the advocate branch above.
                #
                # "LAST TOUCH {days}d ago", NOT "chatted {days}d ago" (fixed
                # 2026-09-01). `days` is `(today - lt_date).days` where
                # `lt_date` is the latest REAL touch of ANY kind — the same
                # clock branch 5 reads and words correctly one branch up. It is
                # not the age of the chat, and the two diverge as soon as the
                # student does the thing the cadence just told them to do:
                # sending the thank-you resets `lt_date` and shortens the
                # number while the conversation keeps aging.
                #
                # MEASURED on the founder's live account 2026-09-01: 6 of his
                # 24 chatted contacts carry a latest real touch that is not the
                # chat, and for 4 of them the two numbers differ outright —
                # Patina Chu would have read "chatted 24d ago" about a chat 31
                # days old, Amy Zhou "chatted 29d ago" against 35, James Bai
                # "chatted 41d ago" against 46.
                #
                # Same defect class as the confirm-chat card fixed 2026-08-31
                # ("chat was scheduled 6 business days ago" off a last-touch
                # clock): a real number rendered as a different fact. The fix
                # is the same one — say what the number measures and stop. The
                # warmth lead stays, because `warmth == "chatted"` IS true; it
                # is only the clock that had the wrong noun on it.
                #
                # The window clause goes through `_target_window` for the
                # reason spelled out there: `chatted_touch_max_weeks` is not
                # tunable and the founder's own min is 6, so the literal
                # "{min}–{max}" this used to interpolate rendered "6–5".
                window = _target_window(chat_min_weeks, chat_max_weeks)
                reason = (
                    "chatted — no dateable touch on record — send an update or a "
                    f"question (target {window})"
                    if days is None else
                    f"chatted — last touch {days}d ago — send an update or a "
                    f"question (target {window})"
                )
                add("keep_warm", reason, 2, days_since=days, target_min_weeks=chat_min_weeks)
            continue

        # 6. cold / no_reply cadence.
        if warmth == "cold" and thread_state == "no_reply":
            outbound = sum(1 for t in ctouches if t.get("kind") in _OUTBOUND_KINDS)
            if outbound == 0:
                add("first_outreach", "added but never contacted — send the first note", 1)
                continue
            # `bd is None` means outbound touches exist but none carry a
            # dateable ts — treated as "definitely stale enough" for both
            # thresholds below, same reasoning as branch 2, so the branch
            # still fires but never prints the old 999-day sentinel as a
            # real number.
            bd = business_days_since(lt_date, today) if lt_date else None
            # Park is checked first. `max_cold_touches` is capped at 2 in
            # crm.views.TUNABLE_CADENCE_PARAMS (one outreach note, one
            # follow-up), so `outbound >= max_cold` here always means "the
            # one follow-up already went out and still got no reply" — there
            # is no second follow-up to stage a later window for.
            if outbound >= max_cold and (bd is None or bd >= park_bd):
                reason = (
                    f"{outbound} touches, no reply, no dateable touch on record — park it"
                    if bd is None else
                    f"{outbound} touches, no reply, {bd} business days silent — park it"
                )
                add("park", reason, 3, outbound=outbound, business_days=bd)
            elif outbound < max_cold and (bd is None or bd >= followup_bd):
                # The follow-up has a shelf life (2026-09-01 divergence, see
                # the module docstring and `followup_expires_after_business_
                # days`). Past it the silence is not a follow-up's, it is a
                # dead thread's, and the honest card is "park it, or come
                # back with a new reason". `bd is None` — outbound touches
                # with no dateable ts — is expired here too, exactly as
                # branch 1 reads an undated chat and as the park test above
                # reads it ("definitely stale enough"): an undated note must
                # not sit in the queue forever either. Strict `>` like the
                # thank-you window, so day 15 exactly still gets its
                # follow-up.
                if bd is None or bd > followup_expiry_bd:
                    # Whole weeks, never below one: `bd` counts business days,
                    # five to a week, and "0 weeks ago" would be false on a
                    # thread that has by construction outlived the window.
                    weeks = max(1, round(bd / 5)) if bd is not None else None
                    reason = (
                        "First note went unanswered, no dateable touch on "
                        "record. Park it, or re-open with a new reason."
                        if weeks is None else
                        f"First note went unanswered {weeks} week"
                        f"{'' if weeks == 1 else 's'} ago. Park it, or "
                        "re-open with a new reason."
                    )
                    add(
                        "park", reason, 3, outbound=outbound, business_days=bd,
                        expired=True, expiry_business_days=followup_expiry_bd,
                        weeks_silent=weeks,
                    )
                    continue
                reason = (
                    f"no reply after touch {outbound}, no dateable touch on record — follow up"
                    if bd is None else
                    f"no reply {bd} business days after touch {outbound} — follow up"
                )
                add(
                    "follow_up", reason, 1, outbound=outbound, business_days=bd,
                    window_business_days=followup_bd,
                )
            continue

        # 7. replied and idle >= 3 business days -> advance (propose a chat).
        # `chatted` is in the warmth set past the port (C3): someone who
        # chatted and then wrote back is MORE engaged than a first-time
        # replier, and used to be the only one this branch ignored.
        # `advocate` is absent because branch 5 already returned for them.
        if thread_state == "replied" and warmth in ("replied", "cold", "chatted"):
            bd = business_days_since(lt_date, today) if lt_date else None
            if bd is None or bd >= 3:
                reason = (
                    "they replied — propose a 15-min chat (no dateable touch on record)"
                    if bd is None else
                    f"they replied — propose a 15-min chat (idle {bd} business days)"
                )
                add("advance", reason, 1, business_days=bd)

    # The ported three-key sort, plus one tiebreak the port did not have and
    # this module's headline property needs.
    #
    # `(priority, tier, firm_name)` is not a total order: two contacts at the
    # same firm, or at two different firms with the same name and tier, tie on
    # all three keys and fall through to `list.sort`'s stability — i.e. to the
    # order the CALLER happened to iterate `contacts` in. Measured: six tied
    # contacts shuffled 50 times produced 48 distinct output orders. The web
    # layer's fetch (`crm.today._build_actions`) carries no `ORDER BY`, so that
    # input order is whatever Postgres returns for an unordered scan — which is
    # free to change after any UPDATE, and does. The student's queue then
    # reshuffles between two loads with no data change behind it.
    #
    # `str(contact id)` rather than the raw id because ids are only required to
    # be hashable here (fixtures use ints, live rows use ints, but a caller
    # keying on a UUID or a string is not doing anything wrong), and mixing
    # types in a sort key is the exact crash `_coerce_tier` exists to prevent.
    # It is the LAST key, so it never reorders anything the ported three keys
    # already separated — it only replaces "whatever order the database felt
    # like" with a stable one.
    actions.sort(
        key=lambda a: (
            a["priority"], a["tier"], str(a["firm_name"]), str(a["contact"].get("id")),
        )
    )
    return actions


# ---------------------------------------------------------------------------
# Backward planner — ported from campaign/src/campaign/tasks.py.
# ---------------------------------------------------------------------------
# Per-event backward lead times, in days. Ported verbatim from tasks.py.
TASK_PLAN: dict[str, list[dict[str, Any]]] = {
    "app_open": [
        {"kind": "advocate_target", "lead_days": 14},
    ],
    "app_close": [
        {"kind": "reping", "lead_days": 14},
        {"kind": "submit", "lead_days": 5},
    ],
    "insight_deadline": [
        {"kind": "insight_app", "lead_days": 7},
    ],
}

# Kinds of change that can produce a task (mirrors tasks_from_change's guard).
_TASK_CHANGE_KINDS = ("new_date", "updated_date", "corroborated")


def tasks_from_change(
    change: Mapping[str, Any],
    *,
    today: date,
    firms: Mapping | Iterable[Mapping] | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Backward-plan concrete tasks from a single firm_date change.

    Pure port of campaign `tasks.tasks_from_change`. Fires ONLY on
    `confirmed_official` changes — a rumor or reported date shows up in the
    brief but never creates or moves a task on its own. A date already in the
    past (relative to `today`) produces nothing.

    Args:
        change: a change dict. Keys used: `kind` (one of new_date /
            updated_date / corroborated — anything else yields nothing),
            `confidence` (must equal `confirmed_official`), `key` (the
            firm_date key `"<firm_id>/<cycle>/<event_kind>"`), `new` (the new
            date string; only its leading date token is read), `firm_id`
            (optional; falls back to the first path segment of `key`),
            `region` (optional; an "hk" region tags the title).
        today: the as-of date (past dates are dropped against this).
        firms: firm metadata for the display name (see `_firm_meta`).
        params: overrides `CADENCE_DEFAULTS`; only `advocate_target` is read,
            for the app_open task's "{target}+ advocates" title.

    Returns:
        A list of planned-task dicts, each:
            {"kind", "firm_id", "source_key", "due", "title", "why",
             "event_kind"}
        (empty when the change doesn't qualify). `due` is an ISO date string
        `lead_days` before the event. These are proposals — persistence and
        de-duplication are `plan_task_write`'s job.
    """
    if change.get("kind") not in _TASK_CHANGE_KINDS:
        return []
    if change.get("confidence") != CONFIRMED:
        return []
    key = change.get("key")
    new = change.get("new")
    if not key or not new:
        return []
    d = _as_date(str(new).split(" ")[0])
    if d is None or d < today:
        return []

    p = _merged_params(params)
    firm_id = change.get("firm_id") or str(key).split("/")[0]
    meta = _firm_meta(firms)
    name = meta.get(firm_id, {}).get("name", firm_id)
    target = int(p["advocate_target"])
    event = str(key).split("/")[-1]
    region_tag = " (HK)" if change.get("region") == "hk" else ""

    out: list[dict] = []

    def add(kind: str, lead_days: int, title: str, why: str) -> None:
        out.append({
            "kind": kind, "firm_id": firm_id, "source_key": key,
            "event_kind": event,
            "due": (d - timedelta(days=lead_days)).isoformat(),
            "title": title, "why": why,
        })

    if event == "app_open":
        add("advocate_target", 14,
            f"Have {target}+ advocates at {name}{region_tag} before apps open {d.isoformat()}",
            f"{name} app opens {d.isoformat()} (confirmed). Advocates in place before "
            "open = referrals ready day one.")
    elif event == "app_close":
        add("reping", 14,
            f"Re-ping warm {name}{region_tag} contacts — app closes {d.isoformat()}",
            "2 weeks out: remind advocates you're applying, ask for a referral/flag.")
        add("submit", 5,
            f"Submit {name}{region_tag} application (closes {d.isoformat()})",
            "5-day buffer before the confirmed close. Rolling review — earlier beats the "
            "deadline.")
    elif event == "insight_deadline":
        add("insight_app", 7,
            f"Submit {name}{region_tag} insight/sophomore program app (deadline {d.isoformat()})",
            "Insight programs are the sophomore fast-track — confirmed deadline.")

    return out


# Lead time (days) within which a planned task's due-date shift is treated as
# an in-place update of the existing task rather than a new one. Ported from
# db.upsert_task's <=3-day rule.
TASK_INPLACE_UPDATE_DAYS = 3


def plan_task_write(
    planned: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Decide how one planned task reconciles against already-stored tasks.

    Pure port of the write half of `db.upsert_task`: a task is identified by
    its `(source_key, kind)` pair. Given the freshly `planned` task and the
    user's `existing` open tasks, this returns the write intent WITHOUT
    performing any write — the caller (web layer) executes it against the DB.

    De-dup / in-place-update rule:
      - No existing task with the same `(source_key, kind)` -> `insert`.
      - An existing one whose `due` date differs by <= 3 days (or not at all)
        -> `update_in_place` (refresh title/why/due on the SAME row; prevents
        duplicate-task spam when a confirmed date drifts by a day or two).
      - An existing one whose `due` moved by MORE than 3 days -> `reschedule`
        (a material shift; the caller may supersede the old row). This is the
        boundary the original's `<= 3` comparison drew.

    Returns:
        {"op": "insert" | "update_in_place" | "reschedule",
         "task": <planned>, "existing_id": <id or None>, "delta_days": <int>}
    """
    key = (planned.get("source_key"), planned.get("kind"))
    new_due = _as_date(planned.get("due"))
    match = None
    for e in existing or ():
        if (e.get("source_key"), e.get("kind")) == key:
            match = e
            break
    if match is None:
        return {"op": "insert", "task": dict(planned), "existing_id": None, "delta_days": None}

    old_due = _as_date(match.get("due"))
    if new_due is None or old_due is None:
        delta = None
        op = "update_in_place"
    else:
        delta = abs((new_due - old_due).days)
        op = "update_in_place" if delta <= TASK_INPLACE_UPDATE_DAYS else "reschedule"
    return {"op": op, "task": dict(planned), "existing_id": match.get("id"), "delta_days": delta}
