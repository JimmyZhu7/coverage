"""Weekly digest assembly: what closes this week, who to ping, and (as a
bonus, when it has something real to say) what's new for you — Coverage's
retention loop, the reason a student who isn't actively browsing still opens
the app.

WHY THIS MODULE IS PURE ASSEMBLY, NOT A FOURTH FORMULA. This codebase has
already shipped the "second competing formula" bug twice on data this exact
digest touches: `crm/today.py` and `crm/debrief.py` disagreeing about how
many days had passed since a chat (fixed in f44f238, see debrief.py's own
docstring for the shared convention that came out of it), and a third,
still-open copy of the same class of bug in `crm/views.py`'s `_contact_card`.
A digest that re-derived "closing soon" or "needs a follow-up" from scratch
would be a fourth. So every date rule and every cadence rule below is a call
into the module that already owns it — this file contains no date math beyond
"today" itself, no cadence priority, and no fit-scoring:

  * "closing this week"  -> `directory.views._tracked_rows` (the exact same
    fold/dedup My Applications renders from) + `directory.deadlines.
    is_closing_soon` (the same window the Today dashboard's stat card and My
    Applications' Closing Soon lens both already use).
  * "who to ping"         -> `crm.today._build_actions` (the cadence queue
    Today itself renders, already dressed with a label, a reason sentence,
    and a mailto compose link).
  * "new for you"         -> `directory.recommend.recommend` (the same
    scorer behind Opportunities' Picked-for-you rail), gated the same way
    that rail is: an empty survey profile gets nothing, never a padded list.

WHY "CLOSING THIS WEEK" USES THE APP'S OWN WINDOW, NOT A HARDCODED 7 DAYS.
`directory.deadlines.CLOSING_SOON_DAYS` (10 days as of writing — "roughly
this and next week", per that module's own comment) is already the ONE
definition of urgency every other surface in the product uses. Cutting the
digest's own copy at exactly 7 would make it disagree with My Applications
and the Today dashboard the first time either of those got a real deadline
in days 8-10 — telling a student in one place that a role is closing soon and
in another (this email) that it isn't. Reusing the constant means the digest
can only ever agree.

WHY DEBRIEF PROMPTS (`crm.debrief.pending`) ARE NOT IN HERE. They were read
and considered. A pending debrief is a different kind of ask — "write down
what you remember" — from every action this module surfaces, which are all
some flavor of "say something to someone". `crm.today`'s queue is the
existing, tested answer to "who needs action this week"; folding a second,
structurally different list into "who to ping" would blur what the section
promises. Left as a clean seam for a future pass (see `send_weekly_digest`'s
module docstring) rather than bolted on here.

WHAT "NOTHING TO REPORT" MEANS, EXACTLY: zero roles closing this week AND
zero pingable cadence actions. Recommended picks never rescue an otherwise-
empty digest — a "3 roles you might like, nothing else going on" email is a
cold-start prompt, not a retention nudge, and this feature's whole premise is
that Coverage already knows something happened in the student's world this
week. See `assemble_digest`'s return contract below.
"""

from __future__ import annotations

import re
from datetime import date

from django.utils import timezone

# A digest is read in an inbox, worked from a page. Past this many rows the
# email becomes the thing it's trying to save the student from (a wall of
# text), so the rest are named only by count, with a link back to Today for
# the rest of the queue — the same "never a bare capped number" rule Today's
# own held-back lane follows (crm/today.py's `capped` flag).
MAX_ACTIONS = 8

# `directory.recommend.recommend`'s own DEFAULT_LIMIT is tuned for a browsing
# page with room to scroll; an email earns a much smaller, higher-bar slice.
MAX_PICKS = 4


def assemble_digest(user, *, today: date | None = None) -> dict | None:
    """Everything one weekly digest email needs, or None when there is
    nothing worth sending.

    Returns None exactly when both `closing` and `actions` are empty — see
    this module's docstring for why picks alone never earn a send. Callers
    (the management command) treat None as "skip this user, log why, move
    on", never as an error.
    """
    today = today or timezone.localdate()

    # Inside the December blackout the email does not go out. Today itself
    # keeps showing confirmed deadlines on those days; the digest gets no
    # such carve-out, because an email landing on Dec 24 headed "who to ping"
    # is the product doing the one thing the page is telling the student not
    # to. `None` is the "skip this user" signal the command already logs. See
    # `crm.today.outreach_blackout` for the window and the evidence.
    from crm.today import BLACKOUT_HOLIDAY, outreach_blackout

    if outreach_blackout(today) == BLACKOUT_HOLIDAY:
        return None

    closing = _closing_this_week(user, today=today)
    actions, actions_overflow = _who_to_ping(user)
    if not closing and not actions:
        return None

    from accounts.unsubscribe import make_token

    picks, picks_note = _new_for_you(user, today=today)
    return {
        "today": today,
        # THE WAY OUT, IN THE EMAIL ITSELF. settings-page.md's LATER section
        # says the digest ships with "an unsubscribe link in the email that
        # writes the same flag" as the Settings toggle, and it shipped
        # without one: the footer explained why the mail arrived and offered
        # nothing to do about it, so the only exit was to find a Settings
        # page you have to be signed in to reach. The token is minted here,
        # with the rest of the row assembly, so the templates stay renderers;
        # `accounts.unsubscribe` owns what it means and what it grants.
        "unsubscribe_token": make_token(user),
        # THE ONE LINE AT THE TOP. Advocates in place and firms with nobody
        # at all are the two numbers that predict whether this student gets
        # an interview, and the digest used to open on this week's
        # deadlines instead. See `_coverage_summary`.
        "coverage": _coverage_summary(user, today=today),
        "closing": closing,
        "actions": actions,
        "actions_overflow": actions_overflow,
        "picks": picks,
        # One honest sentence when every pick is for a cycle the student is
        # NOT recruiting for, else "". See `_cycle_note`.
        "picks_note": picks_note,
        # Section-level provenance flags for the templates' "(reported)"
        # key, which prints ONCE per digest: under Closing if any closing
        # row is prose-read, else under New for you if any pick is. The
        # dict is `deadline_provenance`'s own (label + why), so the words
        # come from one place.
        "closing_reported": next((i["reported"] for i in closing if i.get("reported")), None),
        "picks_reported": next((p["reported"] for p in picks if p.get("reported")), None),
    }


_WHY_YOU_TAIL = re.compile(r"\s*—\s*you\b", re.IGNORECASE)
_WHY_EM_DASH = re.compile(r"\s*—\s*")
_WHY_YEAR_RANGE = re.compile(r"(?<=\d)–(?=\d)")


def why_line(why: str) -> str:
    """A `Recommendation.why` string, in the digest's own voice.

    THE DIGEST DOES NOT GET TO ASSUME THE SCORER'S WORDING. `directory.
    recommend` writes its reason chips for a hover tooltip on a card, where
    "For 2027-2028 grads — you" reads as an aside about the row under the
    cursor. Joined into one line in an inbox it landed as a dangling
    fragment, and the em dash in it is against house copy style (P7) besides.
    The label at the source is being reworded by the pass that owns
    `recommend.py`; this function is why that pass and this one cannot break
    each other. It normalises whatever arrives, so the digest is correct
    before, during and after that change, and stays correct if a future
    reason introduces an em dash of its own.

    Three rules, in order, each narrower than the next:

      * the "— you" tail becomes "(yours)", which is what it means and is
        the wording the source is moving to anyway;
      * any other em dash becomes a comma, because the digest writes lists
        with commas and dots, never with dashes;
      * an en dash between two digits becomes a hyphen, so "2027–2028"
        renders as one unbreakable-looking token in mail clients that treat
        an en dash as a wrap opportunity.

    Deliberately NOT a rewrite of the chip text. It changes punctuation and
    one three-character idiom; every claim the scorer made survives it. A
    digest that edited the scorer's reasons would be the second definition of
    "why this role" (P5), which is exactly what this module exists not to be.
    """
    s = str(why or "")
    s = _WHY_YOU_TAIL.sub(" (yours)", s)
    s = _WHY_EM_DASH.sub(", ", s)
    return _WHY_YEAR_RANGE.sub("-", s)


def _join(names: list[str]) -> str:
    """"A", "A and B", "A, B and C". No em dash, no Oxford comma — house
    copy style, same as `send_weekly_digest._subject`."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _coverage_summary(user, *, today: date) -> dict:
    """"N advocates across M target firms · K firms with no contact yet,
    starting with A, B and C" — the metric that predicts outcomes, computed
    from exactly what the Network board's Coverage Gaps strip and the
    advisor's `get_my_firms` already count, through `crm.coverage.rank_gaps`
    so the three firms named are the same three the strip would put first
    (tier 1 before tier 2, more open roles before fewer, then name).

    {} when the student has tiered nothing — there is no coverage to
    measure and the templates print no line. `app_close` is passed as None
    on purpose: the deadline bonus would need `confirmed_firm_dates` for
    every firm, and it decides ORDER among gaps, not whether a firm is a
    zero-contact gap at all, which is the only thing this line names.

    Campaign-hidden contacts (`crm.campaigns`) are excluded the same way
    `get_my_firms` and the firm page exclude them: nine club alumni at one
    bank would count it as covered when the student has no recruiting
    relationship there at all."""
    from django.db.models import Count

    from crm import campaigns, coverage
    from crm.models import Contact, UserFirm
    from directory.classify import TARGET_BUCKETS
    from directory.models import Opportunity

    rows = list(
        UserFirm.objects.for_user(user)
        .filter(tier__isnull=False)
        .select_related("firm")
    )
    if not rows:
        return {}
    firm_ids = [uf.firm_id for uf in rows]
    hidden = campaigns.excluded_contact_ids(user)
    warmths: dict[int, list[str]] = {}
    for fid, warmth in (
        Contact.objects.for_user(user)
        .filter(archived=False, firm_id__in=firm_ids)
        .exclude(id__in=hidden)
        .values_list("firm_id", "warmth")
    ):
        warmths.setdefault(fid, []).append(warmth)
    # Advocates who are NOT at a target firm — an alum at the student's own
    # school, a contact whose firm was typed rather than linked. The metric
    # is advocates AT target firms (that is what predicts an interview), but
    # a student who knows they have two advocates and reads "0 advocates"
    # reads a bug, not a gap. Measured on the founder's own account: both of
    # his advocates carry `firm_text="usc"` and no firm, so the line said 0.
    advocates_total = (
        Contact.objects.for_user(user)
        .filter(archived=False, warmth="advocate")
        .exclude(id__in=hidden)
        .count()
    )
    open_by_firm = dict(
        Opportunity.objects
        .filter(firm_id__in=firm_ids, status="open", bucket__in=TARGET_BUCKETS)
        .values_list("firm_id").annotate(n=Count("id")).values_list("firm_id", "n")
    )
    target = coverage.advocate_target(user)
    gaps = coverage.rank_gaps(
        [
            {
                "firm_id": uf.firm_id,
                "name": uf.firm.name,
                "tier": uf.tier,
                "warmths": warmths.get(uf.firm_id, []),
                "app_close": None,
                "open": open_by_firm.get(uf.firm_id, 0),
            }
            for uf in rows
        ],
        today=today,
        target=target,
        limit=len(rows),
    )
    no_contact = [g for g in gaps if g["state"] == coverage.NO_CONTACTS]
    advocates = sum(1 for ws in warmths.values() for w in ws if w == "advocate")
    elsewhere = max(0, advocates_total - advocates)
    named = [g["name"] for g in no_contact[:3]]

    line = f"{_plural(advocates, 'advocate')} across {_plural(len(rows), 'target firm')}"
    if elsewhere:
        line += f" ({elsewhere} elsewhere)"
    if not no_contact:
        line += " · a contact at every one"
    elif len(no_contact) <= len(named):
        line += f" · {_plural(len(no_contact), 'firm')} with no contact yet: {_join(named)}"
    else:
        line += (
            f" · {_plural(len(no_contact), 'firm')} with no contact yet, "
            f"starting with {_join(named)}"
        )
    return {
        "advocates": advocates,
        "advocates_elsewhere": elsewhere,
        "firms": len(rows),
        "no_contact": len(no_contact),
        "named": named,
        "line": line,
    }


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cycle_note(user, recs) -> str:
    """One sentence for the top of "New for you" when NOT ONE pick is for a
    cycle the student declared — "Nothing yet for your 2028 Summer
    Internship cycle; these are a year early" — else "".

    Derived from each pick's own bucket and intake year against
    `User.target_cycles`, through the same `parse_target_cycle` the scorer's
    cycle bonus uses, NOT from the chip text: `Recommendation.why` is a
    display string another surface owns, and the founder's four picks all
    carried a "2027 intake" chip whose tooltip said "not a fit" — printed in
    the digest as if it were a reason to apply. The chip is being reworded
    where it is made; this is the digest saying the one thing the chips
    could not, in a place that cannot drift with their wording.

    The timing suffix is only claimed about picks in the SAME programme
    bucket as a declared cycle, and only when every one of them states an
    intake year: "a year early" when each is exactly one year before the
    declared cycle of its bucket, "earlier intakes" when all are before it,
    "other intakes" otherwise. Picks in another bucket — an insight week
    beside a summer-internship cycle — are not judged: a 2027 insight
    programme is not "a year early" for a 2028 internship cycle, it is a
    different programme, so when the picks are mixed the suffix names the
    ones it is about ("the summer internship roles here are a year
    early"). A pick with no intake year gets the bare sentence — nothing
    about its timing is known to say.

    AND WHEN THE STUDENT'S OWN CYCLE OPENS, appended as a second sentence
    from `directory.views.cycle_open_estimate`. Deliberately not restated
    here: this function and the Picked column's header are two renderings of
    one fact, and the day the digest wrote its own month range is the day the
    email and the page could disagree about a date (P5). The whole sentence,
    including the word "estimated" and the firm count, arrives already built,
    and is "" whenever the corpus cannot say — in which case the note is
    exactly what it was before."""
    from directory.recommend import CYCLE_LABELS, parse_target_cycle
    from directory.views import cycle_open_estimate

    labels = [
        str(v).strip() for v in (getattr(user, "target_cycles", None) or []) if str(v).strip()
    ]
    targets = [(label, parse_target_cycle(label)) for label in labels]
    targets = [(label, cycle) for label, cycle in targets if cycle is not None]
    if not targets or not recs:
        return ""

    def _matches(c) -> bool:
        cohort = _int_or_none(c.cohort)
        return any(c.bucket == bucket and cohort == year for _, (bucket, year) in targets)

    if any(_matches(r.candidate) for r in recs):
        return ""
    note = (
        f"Nothing yet for your {_join([label for label, _ in targets])} "
        f"cycle{'' if len(targets) == 1 else 's'}"
    )

    def _with_open_estimate(text: str) -> str:
        """`text` plus the "when does mine open" sentence, if there is one.

        Applied at every path that returns a note, never at the paths that
        return "" — a student whose picks ARE in their cycle has no question
        for this sentence to answer, and answering it anyway would be the
        email volunteering a date about a cycle already underway.
        """
        opens = cycle_open_estimate(user)
        return f"{text}. {opens}" if opens else text

    target_buckets = {bucket for _, (bucket, _) in targets}
    judged = [r.candidate for r in recs if r.candidate.bucket in target_buckets]
    cohorts = [_int_or_none(c.cohort) for c in judged]
    if not judged or any(c is None for c in cohorts):
        return _with_open_estimate(note)

    def _years_for(bucket: str) -> set[int]:
        return {year for _, (b, year) in targets if b == bucket}

    if len(judged) == len(recs):
        subject = "these"
    else:
        names = sorted({CYCLE_LABELS.get(c.bucket, c.bucket).lower() for c in judged})
        subject = f"the {_join(names)} roles here"
    if all(any(cohort == y - 1 for y in _years_for(c.bucket)) for c, cohort in zip(judged, cohorts)):
        note += f"; {subject} are a year early"
    elif all(cohort < min(_years_for(c.bucket)) for c, cohort in zip(judged, cohorts)):
        note += f"; {subject} are earlier intakes"
    else:
        note += f"; {subject} are other intakes"
    return _with_open_estimate(note)


def _closing_this_week(user, *, today: date) -> list[dict]:
    """Tracked roles inside the app's one "closing soon" window, in the exact
    shape My Applications' own Closing Soon lens renders — same partition
    (`directory.views._tracked_rows`), same window test (`is_closing_soon`),
    same per-row shape (`_lens_item`, which already carries `stage_label` —
    the overlap rule's own answer to "a Saved role closing Friday is still
    just one row, correctly labeled, not duplicated or dropped")."""
    from directory.deadlines import is_closing_soon, is_posting_closed
    from directory.views import TRACK_CLOSED, _lens_item, _tracked_rows

    rows = _tracked_rows(user)
    # LIVE rows only, same rule My Applications' lenses enforce, and now the
    # same two-sided test: a finished application has no deadline urgency left
    # in it (TRACK_CLOSED, the student's own "Done"), and neither has a
    # posting the reverify pass watched the firm take down (is_posting_closed,
    # a fact about the posting, not about the student). This section is
    # headed "closing this week"; a role that closed LAST week under either
    # meaning is not that, and advertising it as such is the email telling a
    # student to hurry toward something already gone.
    live = [
        uo for uo in rows
        if (uo.applied_status or "saved") != TRACK_CLOSED
        and not is_posting_closed(uo.opportunity)
    ]
    items = [
        _lens_item(uo, today=today)
        for uo in live
        if is_closing_soon(uo.opportunity.deadline, today=today)
    ]
    items.sort(key=lambda i: (i["days_left"], i["firm_name"].lower()))
    return items


def _who_to_ping(user) -> tuple[list[dict], int]:
    """The top of the cadence queue Today itself would show, minus the one
    action kind that is never a thing to DO ('park' — a bulk strip of
    contacts to stop chasing). Sorted by Today's own ordering
    (`_today_sort_key`: class, then expected value, then cadence priority,
    then tier, then longest-silent-first) so a student who reads only the
    digest and only ever opens Today would see the same names in the same
    order either way. Imported rather than re-derived for that reason — the
    class ladder narrowed from five rungs to four on 2026-08-27 and this
    function needed no edit, which is the property worth keeping.

    Returns (shown, overflow) — `overflow` is a count, never a silently
    truncated list, matching Today's own capped-lane convention.

    NO DAILY PACE, A WEEKLY ONE INSTEAD. `pace=False` is passed for the
    reason spelled out on `_build_actions`: the daily cap discloses itself in
    a sentence about TODAY — "Citi already has 2 today, so this one is better
    tomorrow" — appended to the `reason` this email renders verbatim, and
    `sent_today` behind it is counted against digest morning. `3c9227f`
    sorted those cards last; it did not stop them being printed, and on the
    founder's queue they stayed out of the email only because 8 unpaced cards
    happened to exist (`audit-personalization-networking.md` D5).

    The rule the cap encodes is a SPACING rule, so this email applies the
    same ceiling over its own period: `FIRM_DAILY_CONTACT_CAP * 5`, five
    working days of the daily budget, which is 10 — inside the 4-to-5 people
    per group and 1-to-2 groups per bank the sources actually support
    (`research-networking-norms.md` §7c, Grade A on the ceiling). It bites
    only on a queue with more than ten cards at one firm; below that the list
    is what it was before this change, in the same order.

    SAID PLAINLY: at today's `MAX_ACTIONS` of 8 the ceiling of 10 cannot
    bind, because eight rows cannot contain eleven of anything. It is written
    anyway because the two numbers are independent — the first is "how long
    may an email be", the second is "how many people at one bank may it name"
    — and raising the first without the second is exactly how a digest turns
    into twelve Citi cards. The test pins it with `MAX_ACTIONS` raised.
    """
    from crm.today import (
        CLASS_PARK, FIRM_DAILY_CONTACT_CAP, _build_actions, _pace_firm_key,
        _today_class, _today_sort_key,
    )

    raw_actions, _contacts = _build_actions(user, pace=False)
    pingable = [a for a in raw_actions if _today_class(a) != CLASS_PARK]
    pingable.sort(key=_today_sort_key)

    # The weekly per-firm budget, spent in Today's own order so the digest and
    # the page agree about WHICH ten. `_pace_firm_key` rather than a firm id
    # of this module's own making: it is the same key the daily cap spends,
    # market-aware, and a contact with no nameable employer returns None and
    # is never budgeted at all (P5, and P3 — a student with ten or fewer cards
    # per firm sees no difference).
    weekly_cap = FIRM_DAILY_CONTACT_CAP * 5
    spent: dict = {}
    shown = []
    for a in pingable:
        if len(shown) >= MAX_ACTIONS:
            break
        key = _pace_firm_key(a["contact"])
        if key is not None:
            if spent.get(key, 0) >= weekly_cap:
                continue
            spent[key] = spent.get(key, 0) + 1
        shown.append(a)
    overflow = max(0, len(pingable) - len(shown))
    return shown, overflow


def _new_for_you(user, *, today: date) -> tuple[list[dict], str]:
    """`(picks, note)`: up to `MAX_PICKS` open roles from
    `directory.recommend.recommend`, scored against the same survey-derived
    profile and firm/warmth signals Opportunities' Picked-for-you rail
    builds, over the same open campus board minus whatever the student has
    already tracked or dismissed — a role already on their board is not
    news, however well it would have scored — plus `_cycle_note`'s one
    sentence ("" when the picks include the student's own cycle).

    Returns `([], "")` whenever `recommend()` would return nothing: an empty
    survey profile, or nothing clearing its score bar. Blocked roles (the
    posting's own text excludes this student) are passed through to
    `recommend()` rather than filtered here, so the exclusion rule lives in
    exactly one place.

    Every pick carries its deadline WITH provenance — `deadline_marker`'s
    countdown and `deadline_provenance`'s "(reported)" dict, the exact pair
    `_lens_item` gives a closing row — or the templates print no date at
    all. `_pick_card`'s bare `deadline` field is kept for the Opportunities
    page but never printed here: 96% of dated open campus rows are
    Coverage's own reading, and an email cannot underline."""
    from analytics.models import UserOpportunity
    from crm import campaigns
    from crm.models import Contact, UserFirm
    from directory.classify import TARGET_BUCKETS
    from directory.dupes import fold_duplicates
    from directory.models import Opportunity
    from directory.recommend import Candidate, Profile, recommend
    from directory.views import (
        _eligibility, _eligibility_profile, _pick_card, deadline_marker,
        deadline_provenance,
    )

    tier_by_firm: dict[int, int | None] = dict(
        UserFirm.objects.for_user(user).values_list("firm_id", "tier")
    )
    # Same warmest-per-firm collapse Opportunities computes for the profile —
    # see directory/views.py's Picked-for-you block for the identical logic
    # this mirrors (kept duplicated rather than extracted because it is four
    # lines of ORM, not a rule that can drift the way a date/cadence formula
    # can).
    # The campaign exclusion is part of the mirrored logic, not an extra:
    # an alum who answered a club panel invitation is a real reply and a fake
    # foothold, and letting it warm their bank would aim this student's weekly
    # role picks at a firm he has no recruiting relationship at. `campaigns`.
    warm_by_firm: dict[int, str] = {}
    for fid, warmth in (
        Contact.objects.for_user(user)
        .filter(archived=False, firm__isnull=False,
                warmth__in=("replied", "chatted", "advocate"))
        .exclude(id__in=campaigns.excluded_contact_ids(user))
        .values_list("firm_id", "warmth")
    ):
        rank = "warm" if warmth in ("chatted", "advocate") else "replied"
        if warm_by_firm.get(fid) != "warm":
            warm_by_firm[fid] = rank

    profile = Profile.from_user(user, tier_by_firm, warm_firms=warm_by_firm)
    if profile.is_empty:
        return [], ""

    elig_profile = _eligibility_profile(user)
    touched = set(
        UserOpportunity.all_objects.filter(user=user)
        .values_list("opportunity_id", flat=True)
    )
    open_qs = (
        Opportunity.objects
        .filter(status="open", bucket__in=TARGET_BUCKETS)
        .exclude(id__in=touched)
        .select_related("firm")
    )
    folded = fold_duplicates(list(open_qs))[0]
    by_id = {o.id: o for o in folded}
    candidates = [
        Candidate.from_opportunity(
            o,
            blocked=bool((lambda v: v and v["blocking"])(_eligibility(o, elig_profile))),
        )
        for o in folded
    ]
    recs = recommend(profile, candidates, limit=MAX_PICKS, today=today)
    picks = []
    for r in recs:
        card = _pick_card(r)
        # The card's own `reasons` list is kept (for a future richer render),
        # but the email templates want the one-line join the Opportunities
        # page's chips already spell out — `Recommendation.why` is exactly
        # that string, computed once, here, rather than re-joined in the
        # template. `why_line` is the inbox's punctuation, not a rewrite;
        # see its docstring.
        card["why"] = why_line(r.why)
        o = by_id.get(r.candidate.id)
        card["deadline_marker"] = deadline_marker(
            o.deadline if o else None, getattr(o, "deadline_precision", "") if o else "",
            today=today,
        )
        card["reported"] = deadline_provenance(o) if o else None
        picks.append(card)
    return picks, _cycle_note(user, recs)
