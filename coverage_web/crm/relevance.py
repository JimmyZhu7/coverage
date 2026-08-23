"""Who the daily queue is allowed to talk about, in what order, and with what
ask. The VIEW layer's half of the cadence.

WHY THIS IS NOT IN THE ENGINE. `coverage_domain.cadence`'s own docstring draws
the line: the engine answers "what does the cadence consider urgent", the Today
page answers "who do I contact right now", and view concerns do not belong in
the engine. Everything in this module is the second question. It reads
`crm.UserFirm` (the student's tiered targets), `Contact.school_affiliation`,
`Contact.role` and the shared `directory` board — none of which the engine
takes, imports, or should. The engine stays pure, takes plain dicts, and keeps
its golden fixtures.

THE THREE JOBS, and the founder dogfood that forced each:

1. RELEVANCE (`contact_relevance`). Measured on the founder's own 156-contact
   account, his 16-item queue held 14 people at firms he does not target:
   AccraCare, Paramount, Endpoint, McMaster, WorkWhile, E*TRADE, and one with
   no employer on file. Eight of the sixteen were the same sentence — "no reply
   13 business days after touch 1" — about those same people. The engine knew
   his 54 tiered firms all along and was never asked. His call: a contact at a
   tiered firm, or any contact who shares his school, may generate a daily
   action. Everyone else stays in the contact book, on the Network page, in
   search, in every export — and never produces a daily action.

   ONE OVERRIDE: somebody who actually WROTE to him and is still waiting on an
   answer surfaces whatever their employer. Answering a person who wrote to you
   is basic courtesy and costs one reply; letting a relevance rule swallow it
   would be the tool teaching a bad habit.

   ONE PRIOR QUESTION, added later and asked FIRST: was this outreach even his
   job search? He is also "Associate of External Outreach" for USC's
   International Consulting Club and mail-merged 201 alumni — Delta, Humana,
   Tacori, WME, a law firm — asking them to speak on a club panel. Coverage
   read all of it as his recruiting network. `crm/campaigns.py` detects the
   bulk send and asks him one question about it; contacts whose relationship
   with him STARTED in a send he says was not his recruiting arrive here with
   `campaign_excluded` set and get no daily action. They keep the contact book,
   the Network board, search, history and every export, and the inbound
   override above still applies to them unchanged — a panelist who writes in
   with a real question is still a person who wrote to him.

2. THE ASK (`is_recruiting_role`). A campus recruiter and a banker are not the
   same relationship, and the queue was proposing coffee chats to both. Two
   live rows on 2026-08-22: a "Manager, Talent Acquisition" whose mass
   programme invite had been logged as a reply, and a "Campus recruiting
   manager (Deloitte, national)" who had ALREADY made the introduction and
   handed him on to the USC recruiter. Both got "they replied, propose a 15-min
   chat". The founder: "emails that weren't even designed to be a coffee chat,
   you're proposing to me to do a coffee chat with, and that's not okay."

3. WHAT IS HAPPENING NOW (`firm_openings`) and WHAT THAT IS WORTH
   (`expected_value`). "Advocate. Last touch 34d ago." is a stopwatch, not a
   reason to email a human being. The product decision is that keep-warm
   becomes rare and reason-seeking: where something real is happening at their
   firm, say what it is; where nothing is, the nudge is quieter and lower
   priority than anything with a trigger behind it.

TENANCY. Every query here is `.for_user(user)`-scoped on the private models.
`directory.Opportunity` / `FirmDate` are shared-zone by design (the whole
board, same as `assistant/situation.py` and `crm.today._new_at_your_firms`),
but they are only ever reached through firm ids that came out of this
student's own tenant-scoped rows.

NO INVENTED FACTS. Every clause any reason string here can produce is read off
a row: a confirmed `FirmDate`, an `Opportunity.deadline`, an
`Opportunity.first_seen`, the contact's own warmth, the user's own tier. There
is no "probably", no rounding a maybe into a statement. See
`assistant/brief.py` for what this codebase learned the hard way about a
surface that asserts things the student's data does not say.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone

from directory.classify import TARGET_BUCKETS
from directory.models import FirmDate, Opportunity

from .models import UserFirm
from .utils import FIRM_DATE_LABELS, _confidence_label


# ---------------------------------------------------------------------------
# 1. Relevance — who may generate a daily action.
# ---------------------------------------------------------------------------
# The reasons a contact is allowed in the queue, most load-bearing first.
REL_TIERED = "tiered"        # at a firm on the student's own target list
REL_SCHOOL = "school"        # shares the student's school, any employer
REL_INBOUND = "inbound"      # neither, but they wrote and are still waiting
REL_NONE = None

# Relevance weight per reason, used by `expected_value`. A tier is the
# student's own ranking of how much a firm matters, so it is read straight off
# `UserFirm.tier` rather than flattened. `None` is the "Unranked" lane
# `crm.views.set_firm_tier` writes deliberately — a real answer ("on my list,
# not ranked"), weaker than tier 3 but stronger than a stranger.
_TIER_WEIGHT: dict[object, float] = {1: 3.0, 2: 2.2, 3: 1.6}
_TIER_UNRANKED_WEIGHT = 1.2
_SCHOOL_WEIGHT = 0.9
# Deliberately the lowest weight in the table and deliberately not zero. An
# inbound-only contact has earned exactly one thing — an answer — and should
# sit below every relevant person while still being visible.
_INBOUND_WEIGHT = 0.5

# Touch kinds that mean THEY moved, not you. Same set `crm.today` uses for the
# pace ring's honesty correction (`_INBOUND_TOUCH_KINDS`), and for the same
# reason: `chat_scheduled` is written by the capture pipeline off a RECEIVED
# message proposing a time, so it is their action and it is owed an answer.
INBOUND_TOUCH_KINDS = frozenset({"reply_received", "chat_scheduled"})


def tiered_firm_tiers(user) -> dict[int, object]:
    """`{firm_id: tier}` for this student's target list. `tier` may be None
    (the Unranked lane), which is why membership is tested with `in` and never
    by truthiness of the value."""
    return {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(user)
        if uf.firm_id
    }


def contact_relevance(contact: dict, tiers: dict[int, object], *, owed_reply: bool):
    """Why this contact may appear in the queue, or `REL_NONE`.

    `contact` is the plain dict `crm.today._build_actions` hands the engine, so
    this works identically on a live row and on a fixture.

    Order matters. The campaign gate is FIRST because it answers a different
    and prior question from the other three: they ask "is this person worth a
    daily action", it asks "was this even my job". A club panelist at a tier-1
    bank is a tiered contact by every test below and still does not belong in a
    recruiting queue — the founder emailed him wearing a different hat, and no
    amount of tier makes that outreach his job search. Then a tiered firm is
    the strongest claim, a school tie the next, and the owed-reply override is
    last because it grants the narrowest thing.
    """
    if contact.get("campaign_excluded"):
        # `crm.campaigns.excluded_contact_ids` has already applied the whole
        # rule (classified `other`, relationship originated there, not exempted
        # by hand). One bool arrives here so this stays a pure function of the
        # dict and keeps working on a fixture with no database behind it.
        #
        # THE INBOUND OVERRIDE STILL WINS, and it is the same one the module
        # docstring's rule 1 describes rather than a second mechanism bolted
        # on: somebody who actually wrote to you and is still waiting on an
        # answer surfaces whatever else is true about them. A panelist who
        # sends a real question deserves a reply; a tool that swallowed it
        # because the send it arrived on was club admin would be teaching a bad
        # habit with better bookkeeping. It costs one reply and it is the whole
        # reason this returns REL_INBOUND instead of REL_NONE.
        return REL_INBOUND if owed_reply else REL_NONE
    if contact.get("firm_id") in tiers:
        return REL_TIERED
    if contact.get("school_affiliation"):
        return REL_SCHOOL
    if owed_reply:
        return REL_INBOUND
    return REL_NONE


def relevance_weight(relevance, tier) -> float:
    if relevance == REL_TIERED:
        return _TIER_WEIGHT.get(tier, _TIER_UNRANKED_WEIGHT)
    if relevance == REL_SCHOOL:
        return _SCHOOL_WEIGHT
    return _INBOUND_WEIGHT


# ---------------------------------------------------------------------------
# 2. The ask — who must never be asked for a coffee chat.
# ---------------------------------------------------------------------------
# Unambiguous recruiting-function markers, matched case-insensitively against
# `Contact.role`.
#
# CONSERVATIVE BY CONSTRUCTION, because the two errors are not symmetric. A
# false negative costs one inappropriate prompt the student can ignore. A false
# positive silences a real banker's coffee chat — the single highest-value
# action this product produces — and does it invisibly, which is far worse. So
# every entry here has to be a phrase that names the recruiting FUNCTION, never
# a word that merely co-occurs with it.
#
# Rejected on purpose, and worth recording so they are not "helpfully" added
# later:
#   - a bare "hr" / "human resources": the founder has a USC alum whose role
#     reads "USC alum, HR/people professional". That person is a normal
#     networking contact who happens to work in HR, not a gatekeeper standing
#     between him and a job. The distinction this list draws is the person's
#     relationship to HIM, not their department.
#   - a bare "recruiting" / "recruitment": banking analysts routinely carry
#     campus-recruiting duty in their title ("IB Analyst, campus recruiting
#     captain"). The `campus recruit` entry below catches the actual recruiter
#     titles without catching the analyst, because a recruiter's title leads
#     with the function and an analyst's leads with the seat.
#   - "professor", "advisor", "faculty": a professor is a perfectly good coffee
#     chat. They are not part of the recruiting process.
_RECRUITING_ROLE_MARKERS: tuple[str, ...] = (
    r"talent acquisition",
    r"campus recruit\w*",
    r"university recruit\w*",
    r"college recruit\w*",
    r"graduate recruit\w*",
    r"global recruiting",
    r"recruiting (?:manager|lead|coordinator|specialist|partner|director)",
    r"recruitment (?:manager|lead|coordinator|specialist|partner|director)",
    r"recruiter",
    r"sourcer",
    r"hr coordinator",
    r"(?:program|programme|event|events) coordinator",
)
_RECRUITING_ROLE_RE = re.compile(
    r"\b(?:" + "|".join(_RECRUITING_ROLE_MARKERS) + r")\b", re.IGNORECASE
)


def is_recruiting_role(role: str | None) -> bool:
    """Does this free-text role name a recruiting-process function?

    Text only — this is the fallback consulted when nobody has answered the
    question on the contact itself. `is_recruiting_contact` is the one to call.
    """
    return bool(role and _RECRUITING_ROLE_RE.search(role))


def is_recruiting_contact(contact: dict) -> bool:
    """The student's own answer if they gave one, the role text otherwise.

    `recruiting_contact` is nullable and NULL means unanswered, so `is None` is
    the test and not falsiness: an explicit False is a user saying "no, this
    one is a real networking contact" and has to beat the text.
    """
    explicit = contact.get("recruiting_contact")
    if explicit is not None:
        return bool(explicit)
    return is_recruiting_role(contact.get("role"))


# Cadence actions whose whole content is "ask this person for a conversation".
# These are the ones a recruiting contact must never receive.
CHAT_PROPOSING_ACTIONS = frozenset({"advance", "keep_warm", "maintain"})


# ---------------------------------------------------------------------------
# 3. What is happening now at a firm.
# ---------------------------------------------------------------------------
# How far out a dated event still counts as a reason to write TODAY. Wider than
# the engine's `pre_deadline_reping_days` (14) on purpose: branch 3 fires an
# urgent, priority-0 re-ping and has to be tight, while this is the softer
# question "is there anything worth mentioning". Six weeks is roughly the point
# past which a student can do nothing about a date yet.
OPENING_HORIZON_DAYS = 45

# "New" means the same seven days `crm.today._new_at_your_firms` and
# `assistant.situation.RECENT_DAYS` already mean by it. A student reading two
# cards on one page must never be told two definitions of recent.
OPENING_NEW_DAYS = 7

# Opening kinds, strongest first. A confirmed firm date is the firm itself
# saying so; a role deadline is the board saying so; a new posting is the
# weakest of the three because it is upside rather than a clock.
OPENING_FIRM_DATE = "firm_date"
OPENING_ROLE_DEADLINE = "role_deadline"
OPENING_NEW_ROLE = "new_role"

_OPENING_WEIGHT = {
    OPENING_FIRM_DATE: 2.4,
    OPENING_ROLE_DEADLINE: 2.0,
    OPENING_NEW_ROLE: 1.6,
}


def firm_openings(user, firm_ids, today=None) -> dict[int, dict]:
    """`{firm_id: opening}` — the one best live reason to write to somebody at
    each of these firms, or no entry at all when there is none.

    Three sources, in the order of authority above:

      1. A CONFIRMED `FirmDate` inside the horizon. `confidence` is stored as a
         float and read back through `crm.utils._confidence_label`, the same
         adapter the cadence engine's own re-ping branch goes through, so a
         rumour can never become a countdown here either.
      2. The soonest deadline on an open campus role at that firm that this
         student could actually apply for.
      3. A role at that firm that opened in the last `OPENING_NEW_DAYS`.

    "Could actually apply for" is not a loose test, and it is deliberately the
    SAME test the Opportunities feed and the daily brief already apply
    (`directory.recommend` plus `directory.views._eligibility`): the student's
    tracks, their markets, their rung of the ladder, and no role their own
    stated facts rule out. Without it this reads a bank's helpdesk and audit
    reqs as a reason to email an investment banker. `role_matches_tracks` is an
    allowlist and will often return nothing — which is the honest answer, and
    the card that would have said something is simply not drawn.

    Returns plain dicts so the caller can render them without another query:
    `{"kind", "date", "days", "label", "title"}`. `date`/`days`/`title` are
    None where the kind does not carry them.
    """
    firm_ids = [f for f in firm_ids if f]
    if not firm_ids:
        return {}
    today = today or timezone.localdate()
    horizon = today + timedelta(days=OPENING_HORIZON_DAYS)
    since = timezone.now() - timedelta(days=OPENING_NEW_DAYS)

    out: dict[int, dict] = {}

    # 1. Confirmed firm dates. Ordered latest-first so the last write per firm
    #    is the soonest — the same batching pattern `_chat_prep` and
    #    `_seed_firm_dates` use to keep this to one query.
    for fd in (FirmDate.objects
               .filter(firm_id__in=firm_ids, date__gte=today, date__lte=horizon)
               .order_by("-date")):
        if _confidence_label(fd.confidence) != "confirmed_official":
            continue
        out[fd.firm_id] = {
            "kind": OPENING_FIRM_DATE,
            "date": fd.date,
            "days": (fd.date - today).days,
            "label": FIRM_DATE_LABELS.get(
                fd.event_kind, fd.event_kind.replace("_", " ")),
            "title": None,
        }

    # 2 + 3. The board. One query for both, narrowed at the database rather
    #        than in Python: only rows that are either closing inside the
    #        horizon or newly seen can produce an opening at all.
    from django.db.models import Q

    rows = list(
        Opportunity.objects
        .filter(firm_id__in=firm_ids, status="open", bucket__in=TARGET_BUCKETS)
        .filter(Q(deadline__gte=today, deadline__lte=horizon)
                | Q(first_seen__gte=since))
        .select_related("firm")
    )
    for o in _relevant_to_student(user, rows):
        if o.firm_id in out:
            continue
        if o.deadline and today <= o.deadline <= horizon:
            out[o.firm_id] = {
                "kind": OPENING_ROLE_DEADLINE,
                "date": o.deadline,
                "days": (o.deadline - today).days,
                "label": "Applications close",
                "title": o.title,
            }
    for o in _relevant_to_student(user, rows):
        if o.firm_id in out:
            continue
        if o.first_seen and o.first_seen >= since:
            out[o.firm_id] = {
                "kind": OPENING_NEW_ROLE,
                "date": None,
                "days": None,
                "label": "New role",
                "title": o.title,
            }
    return out


def _relevant_to_student(user, rows):
    """The subset of `rows` this student could actually apply for, soonest
    deadline first.

    Borrowed wholesale from `directory` rather than re-derived: these are the
    exact four filters `crm.today._new_at_your_firms` and
    `assistant.situation._new_role_events` already run, and a fifth opinion
    about what counts as "a role for me" is how two pages start disagreeing.
    All four run in memory over rows already loaded, so they cost no query.
    """
    from directory.recommend import (
        role_matches_level,
        role_matches_regions,
        role_matches_tracks,
    )
    from directory.views import _eligibility, _eligibility_profile

    out = [o for o in rows if role_matches_tracks(o.title, user.tracks)]
    out = [o for o in out if role_matches_regions(o.region, user.regions)]
    out = [
        o for o in out
        if role_matches_level(o.bucket, o.class_year_derived,
                              user.target_cycles, user.class_year)
    ]
    profile = _eligibility_profile(user)
    if profile:
        out = [
            o for o in out
            if not (lambda v: v and v["blocking"])(_eligibility(o, profile))
        ]
    out.sort(key=lambda o: (o.deadline is None, o.deadline or timezone.localdate()))
    return out


# ---------------------------------------------------------------------------
# 4. Expected value — the order the day is actually worked in.
# ---------------------------------------------------------------------------
# What makes NOW the moment, per action. Multiplied against relevance and
# relationship strength, so a weak trigger on a tier-1 advocate still beats a
# strong one on a stranger and vice versa — which is the whole point of a
# product where "who" and "why now" are both real inputs.
_RELATIONSHIP_WEIGHT = {"advocate": 3.0, "chatted": 2.4, "replied": 1.6, "cold": 0.8}

# A confirmed close the engine itself called priority 0, and an unanswered
# inbound message, are the two things that are unambiguously happening right
# now. A thank-you window is nearly as live. Everything below them is a clock
# the product chose rather than an event the world produced.
_NOW_INBOUND = 3.0
_NOW_DEADLINE = 3.0
_NOW_THANK_YOU = 2.6
# A live thread the student has already answered, sitting quiet past the
# engine's three-business-day window. Real — somebody wrote back at some point
# and the conversation has not landed anywhere yet — but not the same claim on
# today as a message still sitting unanswered.
_NOW_ADVANCE = 1.8
_NOW_COLD_DUE = 1.0
# The floor: a keep-warm nudge with no event behind it. Not zero, because a
# genuinely long silence with an advocate is still worth something; low enough
# that anything real outranks it.
_NOW_NOTHING = 0.4


def expected_value(action: dict) -> float:
    """Relevance x relationship strength x whether something real is happening.

    Multiplicative, not additive, and that is the substance of the ranking
    rather than a detail of it. Added, a pile of small facts about a stranger
    can outweigh one big fact about the person who would vouch for you.
    Multiplied, a zero-ish term stays zero-ish: a cold contact at a firm the
    student does not target cannot climb, however long they have been silent.

    Reads only keys `crm.today._build_actions` has already attached, so the
    scorer stays a pure function of the action dict and is testable without a
    database.
    """
    contact = action.get("contact") or {}
    rel = relevance_weight(action.get("relevance"), action.get("relevance_tier"))
    strength = _RELATIONSHIP_WEIGHT.get(contact.get("warmth"), 0.8)

    if action.get("owed_reply"):
        now = _NOW_INBOUND
    elif action.get("closes_on") or action.get("priority") == 0:
        now = _NOW_DEADLINE
    elif action.get("action") == "thank_you":
        now = _NOW_THANK_YOU
    elif action.get("opening"):
        now = _OPENING_WEIGHT.get(action["opening"]["kind"], _NOW_NOTHING)
    elif action.get("action") == "advance":
        now = _NOW_ADVANCE
    elif action.get("action") in ("first_outreach", "follow_up", "park", "confirm_chat"):
        now = _NOW_COLD_DUE
    else:
        now = _NOW_NOTHING

    return round(rel * strength * now, 4)


# ---------------------------------------------------------------------------
# 5. The sentence on the card.
# ---------------------------------------------------------------------------
# THE COMPLAINT THIS ANSWERS, in the founder's words: the prompts "feel hollow"
# and ask him to contact people who "might not offer me value". The card that
# drew it read "Advocate. Last touch 34d ago." — two facts, neither of which is
# a reason to write to a person. A day count is the system's bookkeeping; the
# student is being asked to spend social capital.
#
# So a keep-warm reason now says two things and only two: why this person is
# worth the email, and what makes today the day. Where the second has no
# answer in the data, it is left unsaid rather than filled with a clock, and
# the card goes to "Up next" instead of today's plan (see
# `crm.today._cockpit_context`).
#
# EVERY CLAUSE BELOW IS READ OFF A ROW. The tier is the student's own
# `UserFirm.tier`. "You have already had the conversation" is `warmth ==
# 'chatted'`, which the pipeline ratchet only ever writes off a real `chat`
# touch. The dates are a confirmed `FirmDate` or an `Opportunity.deadline`.
# Nothing here rounds a maybe into a statement — see `assistant/brief.py` for
# the bug that taught this codebase the cost of a surface that asserts the
# inverse of the student's own data.
_LEAD_BY_RELEVANCE = {
    REL_SCHOOL: "Same school",
    REL_INBOUND: "Not a target firm",
}
# A campaign contact who wrote in also comes back as REL_INBOUND, and for them
# "Not a target firm" would be a claim nobody checked: the ICC panel merge
# reached bankers at J.P. Morgan and BNP Paribas, who are very much on the
# founder's target list. The reason they are here is the campaign, not their
# employer, so the lead says that instead of asserting the opposite of his own
# tier list. See `crm/campaigns.py` for the send this describes.
_CAMPAIGN_LEAD = "From one of your campaigns"
_STRENGTH_CLAUSE = {
    "advocate": "they would vouch for you",
    "chatted": "you have already had the conversation",
}


def _on_date(value) -> str:
    """"Oct 30" — spelled out here rather than with `%-d`, whose zero-padding
    suppression is a platform extension and not available everywhere Python
    runs."""
    return f"{value.strftime('%b')} {value.day}"


# How long a role title may be before the card names it. `firm_openings` reads
# `Opportunity.title` straight off the board, and a scraped title is not always
# a title: bank career sites publish things like "2027 Global Markets Summer
# Analyst Program - Hong Kong - Sales and Trading - Requisition 24081". Naming
# that is worse than not naming it — it buries the date the sentence exists to
# deliver and wraps the card to four lines. Past this, the untitled wording is
# the better sentence, so it falls back rather than truncating: a title cut
# mid-phrase reads like a bug, and "a role there" was never wrong.
_MAX_ROLE_TITLE_CHARS = 48


def _role_clause(opening: dict) -> str:
    """"The IB summer analyst role closes Oct 30." — or the untitled wording.

    `firm_openings` already fetches the title and this used to discard it,
    which made a card that had looked at the board read like one that had run a
    query. Same discipline as everything else in this module: the title is read
    off an `Opportunity` row, never composed, and where there isn't one the
    sentence says less rather than inventing more.
    """
    title = (opening.get("title") or "").strip()
    if not title or len(title) > _MAX_ROLE_TITLE_CHARS:
        return f"A role there closes {_on_date(opening['date'])}."
    # The sentence around the title is sentence case; the title itself is left
    # exactly as the firm published it. Case-folding it would be the one thing
    # in this module that alters a fact rather than reporting it — "IB Summer
    # Analyst" becomes "iB Summer Analyst" or "ib summer analyst", and "J.P.
    # Morgan Markets Analyst" becomes nonsense. A role title is a name.
    return f"The {title} role closes {_on_date(opening['date'])}."


def _opening_clause(opening: dict | None) -> str:
    if not opening:
        return ""
    kind = opening["kind"]
    if kind == OPENING_FIRM_DATE:
        return f"{opening['label']} {_on_date(opening['date'])}."
    if kind == OPENING_ROLE_DEADLINE:
        return _role_clause(opening)
    if kind == OPENING_NEW_ROLE:
        return "A role you could apply for opened there this week."
    return ""


def keep_warm_reason(action: dict) -> str:
    """The rewritten reason for a `maintain` / `keep_warm` card."""
    contact = action.get("contact") or {}
    relevance = action.get("relevance")
    tier = action.get("relevance_tier")
    opening = action.get("opening")

    if contact.get("campaign_excluded"):
        lead = _CAMPAIGN_LEAD
    elif relevance == REL_TIERED:
        lead = f"Tier {tier} target" if tier in _TIER_WEIGHT else "On your target list"
    else:
        lead = _LEAD_BY_RELEVANCE.get(relevance, "Warm contact")

    if is_recruiting_contact(contact):
        # No "keep in touch" framing for somebody whose relationship to the
        # student IS the recruiting process. The opening is the entire reason
        # this card exists, so it leads; without one the card is not drawn at
        # all (see `crm.today`).
        clause = _opening_clause(opening)
        return f"{clause} They are your recruiting contact there.".strip()

    strength = _STRENGTH_CLAUSE.get(contact.get("warmth"))
    first = f"{lead}, and {strength}." if strength else f"{lead}."
    clause = _opening_clause(opening)
    return f"{first} {clause}".strip() if clause else first


# The one sentence a recruiting contact's inbound message earns. Says what to
# do and, just as importantly, what not to do — the card it replaces said
# "propose a 15-min chat" to a talent-acquisition manager whose "reply" was a
# mass programme invite.
RECRUITING_REPLY_REASON = (
    "They wrote to you. Answer the note. Recruiting contact, not a coffee chat."
)
RECRUITING_REPLY_LABEL = "Reply"
RECRUITING_KEEP_WARM_LABEL = "Check in"

# The same one sentence for a campaign-excluded contact's inbound message —
# and it exists for the same reason. Measured on the audit account
# (2026-08-23): Nick Tehle, whose only relationship with the founder is the
# ICC panel merge, replied about the panel; the moment the campaign was
# classified "not my recruiting" his card should have become this — instead
# the engine's own action survived the gate untouched, and the card read
# "J.P. Morgan app closes 2026-08-30. Re-ping before you submit." A person
# the user has SAID is not their recruiting must never be handed a recruiting
# ask; the inbound override grants exactly one thing (see rule 1 above), and
# this is the sentence for it.
CAMPAIGN_REPLY_REASON = (
    "They wrote to you. Answer the note. From a send that was not your recruiting."
)
CAMPAIGN_REPLY_LABEL = "Reply"
