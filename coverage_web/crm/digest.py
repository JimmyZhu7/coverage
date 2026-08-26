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

    closing = _closing_this_week(user, today=today)
    actions, actions_overflow = _who_to_ping(user)
    if not closing and not actions:
        return None

    return {
        "today": today,
        "closing": closing,
        "actions": actions,
        "actions_overflow": actions_overflow,
        "picks": _new_for_you(user, today=today),
    }


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
    (`_today_sort_key`: class, then cadence priority, then tier, then
    longest-silent-first) so a student who reads only the digest and only
    ever opens Today would see the same names in the same order either way.

    Returns (shown, overflow) — `overflow` is a count, never a silently
    truncated list, matching Today's own capped-lane convention."""
    from crm.today import _build_actions, _today_class, _today_sort_key

    raw_actions, _contacts = _build_actions(user)
    pingable = [a for a in raw_actions if _today_class(a) != 4]
    pingable.sort(key=_today_sort_key)
    shown = pingable[:MAX_ACTIONS]
    overflow = max(0, len(pingable) - len(shown))
    return shown, overflow


def _new_for_you(user, *, today: date) -> list[dict]:
    """Up to `MAX_PICKS` open roles from `directory.recommend.recommend`,
    scored against the same survey-derived profile and firm/warmth signals
    Opportunities' Picked-for-you rail builds, over the same open campus
    board minus whatever the student has already tracked or dismissed — a
    role already on their board is not news, however well it would have
    scored.

    Returns [] whenever `recommend()` would: an empty survey profile, or
    nothing clearing its score bar. Blocked roles (the posting's own text
    excludes this student) are passed through to `recommend()` rather than
    filtered here, so the exclusion rule lives in exactly one place."""
    from analytics.models import UserOpportunity
    from crm import campaigns
    from crm.models import Contact, UserFirm
    from directory.classify import TARGET_BUCKETS
    from directory.dupes import fold_duplicates
    from directory.models import Opportunity
    from directory.recommend import Candidate, Profile, recommend
    from directory.views import _eligibility, _eligibility_profile, _pick_card

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
        return []

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
    candidates = [
        Candidate.from_opportunity(
            o,
            blocked=bool((lambda v: v and v["blocking"])(_eligibility(o, elig_profile))),
        )
        for o in fold_duplicates(list(open_qs))[0]
    ]
    recs = recommend(profile, candidates, limit=MAX_PICKS, today=today)
    picks = []
    for r in recs:
        card = _pick_card(r)
        # The card's own `reasons` list is kept (for a future richer render),
        # but the email templates want the one-line join the Opportunities
        # page's chips already spell out — `Recommendation.why` is exactly
        # that string, computed once, here, rather than re-joined in the
        # template.
        card["why"] = r.why
        picks.append(card)
    return picks
