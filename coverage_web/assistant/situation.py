"""The situation snapshot: what changed in data Coverage already scrapes,
for one student, since the last time they looked.

WHY THIS EXISTS: `directory.models.OpportunityChange` has logged every
deadline move and every posting close/reopen since it landed — written four
times a day by `ingest.py`, `reverify` and `refresh` — and until this module
nothing anywhere read it back. Meanwhile `assistant/brief.py` (the daily
sentence on the Today page) only ever looks at the cadence queue, which
knows nothing about the board moving under a student's feet. This is the
"notice" half of an advisor that used to only ever answer when asked: pure
code, no model, that turns rows the scraper already wrote into a typed
"what changed" snapshot.

EXACTLY TWO EVENT TYPES, on purpose — decay and calendar events are
already surfaced elsewhere on Today (the cadence queue and the Schedule
rail respectively), and this module is not the place to re-derive either:

  - `deadline_moved`  — a role the student TRACKS had its stated deadline
    change.
  - `new_role_at_known_firm` — a fresh posting appeared at a firm the
    student already has a foothold at (a contact there, or a tiered
    target).

Both are things the student can DO something about today: re-plan a window
that moved, or apply to a posting that opened. That is the test an event
kind has to pass to earn a slot in a three-card strip on the page a student
opens every morning. "Changed" and "worth telling them" are not the same
predicate, and this module used to conflate them.

WHY THERE IS NO `role_closed` KIND (removed 2026-09-02)
-------------------------------------------------------
A third kind used to report that a role the student tracks had been
confirmed closed by the scraper, and it LED the flat list below on the
argument that a close is terminal news. It is written down here rather
than silently filtered because P4 forbids invisible filters, and because
the next person to look at `OpportunityChange` will ask why the close rows
are not read back.

Four things killed it, in the order they matter:

1. THERE IS NOTHING TO DO ABOUT IT. Every other thing on Today names an
   act: message this person, apply to this posting, hit this deadline. A
   posting that already shut names none. It is an obituary, and it was
   printing above the openings.
2. THE FOUNDER SAID SO, about the only instance his account has produced.
   Screenshotted 2026-09-02: "Bank of America closed Bank of America
   Campus Insight Forum: The Power to Lead - Fall 2026" beside "Bank of
   America opened Global Capital Markets Summer Analyst - 2027 - Hong
   Kong". His words: "why is it telling me the programs I applied for has
   closed, I don't care". Note APPLIED — his `UserOpportunity` row on that
   forum is `applied_status="submitted"`, which is what retires the
   obvious rescue ("keep the kind, but only where they applied"). The one
   population it would have preserved is the one population that
   complained. Measured, not assumed.
3. THE FACT IS ALREADY MARKED, on the surface that owns tracked roles and
   with better copy. `directory.views._posting_closed_note` marks every
   tracked closed row on the pipeline permanently, and splits the sentence
   on exactly the axis this kind could not: "Your application still
   stands" for a submitted row, "It's no longer accepting applications"
   for a saved one. Nothing is dropped; P4's mark lives there. Repeating
   it as a headline card is P5's "one definition per fact" broken in the
   direction of noise, and the advisor keeps its own copy too
   (`assistant.tools.get_my_pipeline` carries `posting_closed` per row).
4. IT SCALES WRONG. Board-wide over the seven days to 2026-09-02: 3870 of
   27356 postings flipped to closed (14.1%) against 178 deadline moves
   (0.65%) — closes outrun moves 22 to 1. Ranked first and capped at
   `MAX_PER_TYPE`, closes fill all three rendered slots on their own for
   any student tracking more than ~20 roles. The founder's eight tracked
   rows are the only reason the damage stopped at one card.

If a close ever needs to reach Today again, it belongs beside the role on
the pipeline or in the digest, not as a headline in a strip whose whole
job is to name what to do next.

THE TENANT RULE, same as `assistant/tools.py`
------------------------------------------------
`Opportunity` and `OpportunityChange` are SHARED-zone models (plain
`.objects`, the whole board, not one student's) — but every filter that
decides WHICH opportunities are even in play runs through this student's
own tenant-scoped rows first: `UserOpportunity.objects.for_user(user)` for
what they track, `crm.models.Contact.objects.for_user(user)` and
`crm.models.UserFirm.objects.for_user(user)` for which firms they know.
`assistant/tests/test_isolation.py` greps this whole package for the
unscoped manager name, so it must never appear here.

NEVER RAISES, same posture as `assistant/brief.py`
------------------------------------------------------
`build_situation` wraps its entire body in a try/except and returns an
empty snapshot on any failure. This is pure DB reads with no LLM call, so
the failure surface is much smaller than the brief's — but the contract
this module owes the Today page is identical: a bug here must degrade to
"no cards today", never a 500.

NOT CACHED. Recomputed on every full-page Today load, deliberately — see
the caller in `crm/today.py`. Every query here is bounded by the student's
own tenant-scoped row counts (their tracked roles, their contacts, their
tiered firms), the same cost shape as `_next_deadlines` (crm/today.py),
which already runs uncached on every render. Caching
would buy nothing but a day-old snapshot on a page whose whole point is
"what's true right now", for a query cheap enough that dogfood is the
right way to find out if that judgement call was wrong.
"""

from __future__ import annotations

from datetime import date as _date, timedelta

from django.db.models import Min
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.models import Contact, UserFirm
from directory.classify import TARGET_BUCKETS
from directory.deadlines import is_posting_closed
from directory.dupes import fold_duplicates
from directory.models import Opportunity, OpportunityChange
from directory.recommend import role_matches_level, role_matches_regions, role_matches_tracks
from directory.views import _eligibility, _eligibility_profile, deadline_provenance

# `directory.recommend.role_level(title)` — the rung a posting's own title
# names (mba / phd / experienced / ...) — is being added alongside this
# module and may not exist yet. Guarded so this file imports either way:
# with it, `_new_role_events` drops the roles an undergraduate cannot
# apply to; without it, behaviour is exactly what it was. The Today strip
# on the founder's own account carried a PIMCO "PhD Summer Intern" and an
# RBC "IB Summer Associate" as news for a class-of-2029 undergrad, and
# `role_matches_level` (bucket + derived class year) cannot see either:
# both are `internship`-bucket rows whose only tell is in the title.
try:
    from directory.recommend import role_level as _role_level
except ImportError:  # pragma: no cover - exercised by monkeypatching below
    _role_level = None

# The levels a student at `_UNDERGRAD_LEVELS` is never news for. Matched
# case-insensitively against whatever `role_level` returns.
_ADVANCED_LEVELS = frozenset({"mba", "phd", "experienced"})
# `User.study_level` values that mean "undergraduate" — including the blank
# that every account carries until the field exists and is answered. Blank
# reads as undergrad on purpose: this product's students are overwhelmingly
# undergraduates, and a PhD posting reaching a student who never said they
# were a PhD is the measured failure, not the hypothetical one.
_UNDERGRAD_LEVELS = frozenset({"", "undergrad", "undergraduate"})

# How far back counts as "recent". Until 2026-08-31 this matched Today's
# own now-retired `crm.today._new_at_your_firms` window (`since =
# timezone.now() - timedelta(days=7)`) — that surface answered the exact
# same "what's new since last look" question for a different slice of the
# same data, and a student reading both should
# never be told two different definitions of "recent" on one page.
RECENT_DAYS = 7

# How many newest rows the new-role scan reads before filtering. Wide on
# purpose: everything that narrows this list (track, region, level,
# eligibility, one-per-firm) runs in Python underneath, so a slice sized
# to the CAP starves them. See the note at the fold below.
_CANDIDATE_ROWS = 400

# Per-event-type cap. A student tracking hundreds of roles must never run
# an effectively-unbounded query here; five of any one kind is already more
# than a daily card can show, and the flat `events` list below caps the
# rendered total further still.
MAX_PER_TYPE = 5

# The whole snapshot's cap, across all three types combined. Looser than
# what actually renders (the Today page shows at most 3 cards, matching the
# brief's own card cap) — callers that want more raw signal than the card
# strip shows (e.g. a future digest) can still have it, up to this ceiling.
MAX_TOTAL_EVENTS = 10

_EMPTY: dict = {
    "deadline_moved": [],
    "new_role_at_known_firm": [],
    "events": [],
}


def _parse_date(value: str | None) -> _date | None:
    """A `OpportunityChange.render_value`d date, back to a real `date` — or
    `None` for `""` (no value) or anything that isn't ISO-8601. Used so the
    card template can format a real date (`|date:"M j"`) rather than
    re-parsing a string, while the raw text survives alongside it for the
    (rare) case a stored value was never a date to begin with."""
    if not value:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        return None


def _deadline_source(opp) -> str | None:
    """Which KIND of fact the deadline on `opp` now is: "reported" for a date
    Coverage's own regex read out of the posting's prose, "stated" for one
    the board published as a field, None when there is no date at all.

    The same three-way answer `assistant.tools._deadline_source` gives every
    search / pipeline row, borrowed from `directory.views.deadline_provenance`
    the same way — restated here (three lines) rather than imported from
    `tools`, because `tools` imports this module and the import would be
    circular. `test_situation` pins the two against each other.

    WHY A MOVED DEADLINE NEEDS THIS AT ALL. Measured 2026-09-01: of 394
    `deadline_moved` change rows in the last 30 days, 354 were on prose-read
    deadlines (`confidence` 0.6) and 36 on stated ones. A regex that reads a
    different date out of a re-scraped page is not the firm changing its
    mind, and the daily brief was telling students "deadline moved from X to
    Y" with nothing to say which of the two it was."""
    if opp.deadline is None:
        return None
    return "reported" if deadline_provenance(opp) else "stated"


def _deadline_moved_events(tracked_ids: list[int], since, limit: int) -> list[dict]:
    """`OpportunityChange` rows on a tracked opportunity where the stated
    deadline itself moved, most recent first, at most one row per
    opportunity — a role that moved twice in the window is reported once,
    old-to-newest, not as two disagreeing cards.

    A posting that is CLOSED right now is skipped
    (`directory.deadlines.is_posting_closed`). A moved deadline is a promise
    about a window still open; on a dead posting it is a stale row the
    scraper has already overtaken. Reported directly, pointing at two cards
    for one Bank of America forum: one saying it closed and would not take
    applications, the other saying its deadline had moved to a date nine
    days out. Both were true rows; together they were nonsense.

    This guard is now the ONLY place the module reads a posting's live
    closed-ness, since the `role_closed` kind is gone (module docstring).
    It does not report the close; it declines to report a window on a role
    that no longer has one."""
    if not tracked_ids:
        return []
    rows = (
        OpportunityChange.objects
        .filter(field="deadline", opportunity_id__in=tracked_ids, observed_at__gte=since)
        .select_related("opportunity", "opportunity__firm")
        .order_by("-observed_at")
    )
    seen: set[int] = set()
    events: list[dict] = []
    for row in rows:
        if row.opportunity_id in seen:
            continue
        opp = row.opportunity
        if is_posting_closed(opp):
            seen.add(row.opportunity_id)
            continue
        seen.add(row.opportunity_id)
        events.append({
            "kind": "deadline_moved",
            "opportunity_id": opp.id,
            "title": opp.title,
            "firm": opp.firm.name,
            "url": opp.url,
            "old_value": row.old_value,
            "new_value": row.new_value,
            "old_date": _parse_date(row.old_value),
            "new_date": _parse_date(row.new_value),
            # Provenance of the date the role carries NOW (the `new_value`
            # side of the move). Travels with the event because neither the
            # advisor's tool payload nor the brief's prompt line has a
            # dotted underline to carry it — see `_deadline_source`.
            "deadline_source": _deadline_source(opp),
            "observed_at": row.observed_at,
        })
        if len(events) >= limit:
            break
    return events


def _display_location(title: str, location: str) -> str:
    """The location a "new role" event card should actually print.

    Two real duplications, both seen live on the founder's own board:

    1. `location` repeating a segment against ITSELF -- the Hong Kong
       board emits the same place twice under two spellings, "HONG KONG,
       Hong Kong". Collapsed here by comparing casefolded segments, not
       by hardcoding "Hong Kong" as a special case, so the same fix covers
       any board that does this.
    2. `location` repeating the TITLE -- a posting titled "...Summer
       Associate - Houston" then followed by a card that also prints
       "Houston, Texas, United States of America" says the city twice in
       one sentence. The title is scraped evidence and is never rewritten
       (this product's own rule), so the fix is on the card's side: if the
       title already names the location's city, the card does not repeat
       it.

    Returns "" (falsy, so the template's existing `{% if e.location %}`
    already suppresses the whole clause) when there is nothing left worth
    printing.
    """
    if not location:
        return ""
    seen: set[str] = set()
    segments = []
    for part in location.split(","):
        part = part.strip()
        key = part.casefold()
        if part and key not in seen:
            seen.add(key)
            segments.append(part)
    if not segments:
        return ""
    if segments[0].casefold() in title.casefold():
        return ""
    return ", ".join(segments)


def _drop_advanced_levels(rows: list, user) -> list:
    """`rows` minus the postings whose TITLE names a rung above an
    undergraduate — MBA, PhD, experienced hire — for a student whose
    `study_level` is blank or undergraduate. Unchanged rows when
    `directory.recommend.role_level` is not importable yet, when the student
    has stated a higher level, or when the level function itself misbehaves:
    a signature change in the sibling module must cost a filter, never the
    whole situation strip (`build_situation`'s own try/except would
    otherwise blank every card over it)."""
    if _role_level is None:
        return rows
    level = str(getattr(user, "study_level", "") or "").strip().lower()
    if level not in _UNDERGRAD_LEVELS:
        return rows
    kept = []
    for o in rows:
        try:
            rung = str(_role_level(o.title) or "").strip().lower()
        except Exception:  # noqa: BLE001 — see docstring
            return rows
        if rung not in _ADVANCED_LEVELS:
            kept.append(o)
    return kept


def _new_role_events(user, since, limit: int) -> list[dict]:
    """Fresh open postings at a firm the student already has a foothold at.

    JUDGEMENT CALL: the memo names "firms where they have contacts"
    specifically. This uses the UNION of contact firms and tiered
    (UserFirm) firms rather than contacts alone — a firm a student has
    ranked as a target but hasn't met anyone at yet is just as "known" to
    them as one where they have a contact, arguably more actionable (they
    are actively trying to break in), and excluding it would silently drop
    the single largest tiered-but-contactless firm case. Both populations
    are the student's own tenant-scoped rows either way.
    """
    contact_firm_ids = set(
        Contact.objects.for_user(user)
        .filter(archived=False, firm_id__isnull=False)
        .values_list("firm_id", flat=True)
    )
    tiered_firm_ids = set(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    firm_ids = contact_firm_ids | tiered_firm_ids
    if not firm_ids:
        return []

    # Exclude board DEBUTS: a firm whose oldest posting is itself inside the
    # window just joined Coverage's board, and every role it has would read
    # as "new" for a reason that has nothing to do with the firm actually
    # opening anything. Same fix the retired `crm.today._new_at_your_firms`
    # used to make for the identical trap.
    debut_ids = {
        row["firm_id"]
        for row in Opportunity.objects.filter(firm_id__in=firm_ids)
        .values("firm_id").annotate(oldest=Min("first_seen"))
        if row["oldest"] and row["oldest"] >= since
    }
    live_firm_ids = [f for f in firm_ids if f not in debut_ids]
    if not live_firm_ids:
        return []

    dismissed_ids = set(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=True)
        .values_list("opportunity_id", flat=True)
    )

    qs = (
        Opportunity.objects
        .filter(status="open", bucket__in=TARGET_BUCKETS, firm_id__in=live_firm_ids, first_seen__gte=since)
        .select_related("firm")
        .order_by("-first_seen")
    )
    if dismissed_ids:
        qs = qs.exclude(id__in=dismissed_ids)

    # Same repeat-listing problem the Opportunities feed already solves: a
    # board scraped twice in one week posts the same requisition twice.
    #
    # THE SLICE HAS TO CLEAR THE RELEVANCE FILTERS, NOT THE RAW ROWS. This
    # was `qs[: limit * 8]` — 24 rows — sized when the only thing between
    # here and the cap was the per-firm dedup below. The track, region,
    # level and eligibility filters were added underneath it later, and
    # they are the ones that actually decide the count: on the founder's
    # own account the window held 224 matching rows of which 11 were
    # relevant, but the 24 NEWEST happened to be entirely off-track, so
    # the strip rendered nothing at all and had been dark for weeks. A
    # newest-first slice taken before the filters is a lottery on what a
    # scraper happened to post last.
    #
    # `_CANDIDATE_ROWS` is a bounded read (ids + titles already needed in
    # memory) that is wide enough for the filters to have something to
    # work with, and the cap still happens after all of them.
    rows, _folded = fold_duplicates(list(qs[:_CANDIDATE_ROWS]))

    # RELEVANT TO WHAT THEY RECRUIT FOR. Everything above this line selects on
    # the FIRM, which is right for the firm axis and blind to the job: the
    # same bank that runs the investment bank a student tiered also posts
    # branch, audit and helpdesk reqs, and this snapshot feeds the daily
    # brief — the first thing a new student ever reads from Coverage. See
    # `role_matches_tracks` for the rule and the failure that prompted it.
    # Applied AFTER fold_duplicates so the fold still sees the full slice,
    # and it costs no query: the titles are already in memory.
    rows = [o for o in rows if role_matches_tracks(o.title, user.tracks)]

    # RELEVANT TO THE STUDENT'S OWN RUNG, read off the TITLE. `role_matches_
    # level` below judges bucket and derived class year, and a "PhD Summer
    # Intern – Quantitative Research" or an "IB Summer Associate" clears
    # both: same `internship` bucket, no derivable year. Two of the
    # founder's four new-role events on 2026-09-01 were exactly those. So
    # when `directory.recommend.role_level` exists (guarded import at the
    # top of this module) and the student is an undergraduate — or has not
    # said otherwise — the MBA / PhD / experienced-hire rows are dropped
    # here. Without `role_level` nothing changes.
    rows = _drop_advanced_levels(rows, user)

    # RELEVANT TO WHERE, AND WHEN. Same posture, two more axes: a Pune,
    # India ops role and a full-time "New Associate" programme both reached
    # a US/HK IB-track sophomore's day-one brief in the same customer
    # walk that found the track gap — right firm, wrong market, wrong rung
    # of the ladder. See `role_matches_regions` and `role_matches_level`.
    rows = [o for o in rows if role_matches_regions(o.region, user.regions)]
    rows = [
        o for o in rows
        if role_matches_level(o.bucket, o.class_year_derived,
                               user.target_cycles, user.class_year)
    ]
    # And never a role the student's own stated facts rule OUT entirely (a
    # wrong stated class year, a market that won't sponsor them) — the same
    # blocking verdict `directory.views._eligibility` already issues for
    # Picked-for-you, applied here rather than duplicated.
    elig_profile = _eligibility_profile(user)
    if elig_profile:
        rows = [
            o for o in rows
            if not (lambda v: v and v["blocking"])(_eligibility(o, elig_profile))
        ]

    # ONE PER FIRM. A firm's own campus recruiting team routinely posts a
    # whole batch of reqs the same week — CICC alone can post three roles
    # in one run — and without this, that single batch fills every slot
    # the snapshot has, which reads as "CICC, CICC, CICC" instead of
    # naming the breadth of what's actually moving. The signal this event
    # type exists to carry is WHICH firms have news, not how many reqs any
    # one of them opened; a student who wants the full list already has it
    # on the Opportunities feed for that firm. Kept to the most recent
    # posting per firm (`rows` is already newest-first).
    #
    # AND THE EVENT SAYS HOW MANY IT STANDS FOR. `folded_count` is the number
    # of OTHER fresh roles at that firm this one card is standing in for — 0
    # when the firm posted exactly one. The one-per-firm rule above is right
    # (three CICC reqs are one piece of news), but silently showing one of
    # three is the invisible filter P4 forbids: "CICC just opened a role" and
    # "CICC just opened three" are different facts about how hard that firm is
    # moving, and the second is one `Counter` over a list already in memory.
    # It costs no query.
    from collections import Counter

    per_firm = Counter(o.firm_id for o in rows)
    seen_firms: set[int] = set()
    events = []
    for o in rows:
        if o.firm_id in seen_firms:
            continue
        seen_firms.add(o.firm_id)
        events.append({
            "kind": "new_role_at_known_firm",
            "opportunity_id": o.id,
            "title": o.title,
            "firm": o.firm.name,
            "url": o.url,
            "location": _display_location(o.title, o.location),
            # HOW NEW, not just "new". Measured on the founder's three
            # situation rows, 2026-09-01: they surfaced 4.4 to 5.4 days after
            # `first_seen` (`audit-opportunities.md §C2`), so "just opened"
            # was doing work the date does not support. Carried as the raw
            # value; every reader formats it in its own units.
            "first_seen": o.first_seen,
            "folded_count": per_firm[o.firm_id] - 1,
        })
        if len(events) >= limit:
            break
    return events


def build_situation(user) -> dict:
    """This student's "what changed" snapshot: up to `MAX_PER_TYPE` events
    of each of the two kinds, plus a flat `events` list (both kinds merged,
    capped at `MAX_TOTAL_EVENTS`, most-urgent-first) for anything that just
    wants a short list to render or summarize.

    Ordering of the flat list is a judgement call, stated once here. Both
    surviving kinds are actionable, so urgency alone decides between them: a
    MOVED deadline is a window closing on a role the student already tracks
    and already wants, so missing it costs them something they had; a NEW
    role is upside they did not have before — real, and the less
    time-pressured of the two. So `deadline_moved` leads, then
    `new_role_at_known_firm`, each internally most-recent-first.

    Until 2026-09-02 a third kind, `role_closed`, led this list on the
    argument that a close is terminal news. The module docstring records why
    it is gone; the short version is that terminal is not the same as
    actionable, and it was pushing the openings down the page.

    That same order is also the PRECEDENCE for the flat list's one-card-per-
    role rule: a role appearing in both kinds is reported once, by the more
    urgent kind that claims it. The per-kind lists are left whole — a caller
    asking for `deadline_moved` wants every move, not the ones that survived
    a merge — so only `events` is deduplicated.

    ONE CARD PER FIRM WAS CONSIDERED AND REJECTED. The founder's screenshot
    read as noise partly because both its cards named Bank of America, and
    folding by firm would have collapsed them. But the doubling was a close
    beside an opening, and with the close gone the remaining pair at one
    firm is a moved window plus a fresh posting: two different roles, two
    different acts, both worth doing. Folding those would suppress the
    actionable half to fix a problem the removal already fixed.
    `_new_role_events` still keeps one row per firm within its own kind
    (three CICC reqs are one piece of news), and that stays.

    Never raises — see the module docstring. Any failure returns the same
    empty shape a student with nothing to report gets, so a bug here can
    never turn into a broken Today page."""
    try:
        now = timezone.now()
        since = now - timedelta(days=RECENT_DAYS)

        tracked_ids = list(
            UserOpportunity.objects.for_user(user)
            .filter(dismissed=False)
            .values_list("opportunity_id", flat=True)
        )

        deadline_moved = _deadline_moved_events(tracked_ids, since, MAX_PER_TYPE)
        new_roles = _new_role_events(user, since, MAX_PER_TYPE)

        # ONE CARD PER ROLE. Each helper above dedupes inside its own kind
        # and neither can see the other, so a single role could surface twice
        # with two different sentences — which is exactly what shipped once: a
        # Bank of America forum reported as closed AND as having moved its
        # deadline to a date in the future, side by side.
        #
        # The precedence is the list order, argued in the docstring above: a
        # window that moved outranks a role that merely appeared. Fixing only
        # one pair at its source (see `_deadline_moved_events`) would leave
        # the same trap set for the next event kind added here, so the
        # guarantee lives at the merge where it can be stated once — and it
        # stays even now that the pair that prompted it is down to two kinds.
        events: list[dict] = []
        claimed: set[int] = set()
        for event in deadline_moved + new_roles:
            opportunity_id = event.get("opportunity_id")
            if opportunity_id in claimed:
                continue
            claimed.add(opportunity_id)
            events.append(event)
            if len(events) >= MAX_TOTAL_EVENTS:
                break

        return {
            "deadline_moved": deadline_moved,
            "new_role_at_known_firm": new_roles,
            "events": events,
        }
    except Exception:  # noqa: BLE001 — never break the Today page over this
        return dict(_EMPTY)
