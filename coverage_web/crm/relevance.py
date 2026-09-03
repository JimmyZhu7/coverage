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
   search, history and every export (they come off the Network board too, but
   that is `crm.views.contact_list`'s decision, not this module's), and the
   inbound override above still applies to them unchanged — a panelist who writes in
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
board, same as `assistant/situation.py`),
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
from datetime import date, timedelta

from django.utils import timezone

from directory import estimates
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity
from directory.open_runs import CYCLE_OBSERVATION_MIN_SAMPLE

from .models import UserFirm
from .utils import FIRM_DATE_LABELS, confirmed_firm_dates


# ---------------------------------------------------------------------------
# 1. Relevance — who may generate a daily action.
# ---------------------------------------------------------------------------
# The reasons a contact is allowed in the queue, most load-bearing first.
REL_TIERED = "tiered"        # at a firm on the student's own target list
REL_SCHOOL = "school"        # shares the student's school, any employer
REL_INBOUND = "inbound"      # neither, but they wrote and are still waiting
REL_NONE = None

# A verdict on ONE CARD, not on a contact: this person is fine, this ASK is
# not available at this firm. See `apply_only` below.
REL_APPLY_ONLY = "apply_only"

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
    if contact.get("recruitment_hidden"):
        # THE PERSON THEMSELVES IS NOT PART OF THE USER'S RECRUITING — the
        # founder's 2026-08-25 rule ("any unrelated should not show up"),
        # decided by `crm.recruitment.contact_verdict` off the row's own text
        # and carried in as one bool for the same purity reason as
        # `campaign_excluded` above. Judged BEFORE the school tie below on
        # purpose: the blanket school exemption was an earlier deliberate
        # decision and the founder has overridden it — a WRIT 150 professor
        # shares his school and still has nothing to do with recruiting, so
        # REL_SCHOOL is only ever reached by people the recruitment rule kept.
        #
        # The inbound override is the campaign gate's, unchanged and for the
        # same reason: a professor who wrote and is still waiting is owed one
        # reply, and a rule that swallowed it would be teaching a bad habit.
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
#   - a bare "recruiting" / "recruitment" AS A SUBSTRING: banking analysts
#     routinely carry campus-recruiting duty in their title ("IB Analyst,
#     campus recruiting captain"). The `campus recruit` entry below catches the
#     actual recruiter titles without catching the analyst, because a
#     recruiter's title leads with the function and an analyst's leads with the
#     seat. The one carve-out is `_WHOLE_ROLE_RECRUITING_RE` below: a role that
#     is NOTHING BUT the word names the function alone and has no seat this
#     rejection could be protecting.
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

# The same function, written in Chinese. Every marker above is an English
# word bounded by `\b`, and `\b` cannot find a seam inside CJK text: Python
# treats every han character as a word character, so "校园招聘经理" (Campus
# Recruiting Manager) has no boundary around 招聘 and the English pattern
# reports False. That is the module's own failure mode, inverted — a
# recruiter who signs their mail in Chinese reads as an ordinary contact and
# `CHAT_PROPOSING_ACTIONS` proposes a coffee chat to the gatekeeper.
#
# It matters here specifically: the first agency channel serves Chinese
# international students, so this is the cohort, not an edge case.
#
# Substring matching, no boundaries, because CJK does not delimit words.
# The set stays deliberately narrow and mirrors the English doctrine above:
# 招聘 (recruitment) is the stem inside 校园招聘 / 招聘经理 / 招聘专员, 校招 is
# its campus abbreviation, 猎头 is a headhunter, 招募 is to recruit. Bare
# 人力资源 (HR) is left OUT on purpose, exactly as bare "hr" is above — the
# doctrine holds that an HR professional is a normal networking contact and
# only "hr coordinator" names the recruiting function.
_RECRUITING_ROLE_CJK_MARKERS: tuple[str, ...] = (
    "招聘",
    "校招",
    "猎头",
    "招募",
)
_RECRUITING_ROLE_CJK_RE = re.compile("|".join(_RECRUITING_ROLE_CJK_MARKERS))

# The one exception to the bare-"recruiting" rejection above, and it is
# deliberately narrower than the thing that was rejected. The rejection was
# about SUBSTRINGS: "IB Analyst, campus recruiting captain" contains the word
# but names a banker, and a false positive there silences a real banker's
# coffee chat invisibly. That reasoning stands untouched — a bare "recruiting"
# still matches nothing as a substring.
#
# This matches only when the function is the WHOLE role string. Measured on
# the founder's own 2026-08-23 full-history refresh: ten campus recruiters at
# Bain, BCG, PwC and KPMG arrived with role exactly "Recruiting", because
# `capture.discovery.split_display_name` read it off how they sign their own
# mail — "Keith Bevans, Recruiting". A sender whose signature names the
# function and nothing else has told you their seat IS the function; the
# banker the rejection protects signs "Jane Doe, IB Analyst" and carries any
# recruiting duty inside a longer string, which this anchored match can never
# reach. No ends-with or leads-with variant, on purpose: "IB Analyst, campus
# recruiting" ends with the word and is still a banker.
_WHOLE_ROLE_RECRUITING_RE = re.compile(
    r"^\s*(?:recruiting|recruitment)\s*$", re.IGNORECASE
)


def is_recruiting_role(role: str | None) -> bool:
    """Does this free-text role name a recruiting-process function?

    Text only — this is the fallback consulted when nobody has answered the
    question on the contact itself. `is_recruiting_contact` is the one to call.
    """
    if not role:
        return False
    if _WHOLE_ROLE_RECRUITING_RE.match(role):
        return True
    if _RECRUITING_ROLE_CJK_RE.search(role):
        return True
    return bool(_RECRUITING_ROLE_RE.search(role))


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
# 2b. Apply-only firms — where the process is a test, not a conversation.
# ---------------------------------------------------------------------------
# `Firm.recruiting_style == "assessment"`, spelled here so this module keeps
# its no-model-imports posture (the same convention `crm/coverage.py` and
# `crm/sourcing.py` already hold to for the identical constant).
ASSESSMENT = "assessment"

# The two cold NETWORKING asks. Everything else a card can be — an owed reply,
# a thank-you, a confirm-chat, a re-ping against a real deadline — is either
# answering somebody or acting on a date, and neither of those is networking.
_APPLY_ONLY_ACTIONS = frozenset({"first_outreach", "follow_up"})

# The card copy. It says what the firm's process IS and stops.
#
# WHAT IT MAY NEVER SAY, and this is a limit the source itself sets: no
# evidence anywhere shows networking is counterproductive at these firms
# (`research-st-quant.md` Q3 notes this explicitly). Jane Street's own FAQ
# declines one-to-one chats by policy and Citadel Securities' campus funnel is
# entirely competitions and events — that is a fact about how the firm hires,
# not a warning about the student's behaviour, and the copy has to stay on the
# right side of that line.
APPLY_ONLY_LABEL = "Apply"
# "…so put the time there instead of into a first note" closed this until
# 2026-09-02, when the badge above the sentence is the word Apply and the
# clause was that badge in fourteen words. Both surviving clauses are facts
# about the firm's process, which is all this copy was ever allowed to be.
APPLY_ONLY_REASON = (
    "This firm hires by assessment. The application and the test are the process."
)


def apply_only(action: dict) -> bool:
    """True when this card is a cold networking ask at an assessment firm.

    Gated on the ACTION and not on the contact, which is the whole design.
    Marking the person would silence every card they can produce; marking the
    ask silences exactly the one the firm's process has no room for and leaves
    the rest untouched. Concretely, at the same firm and on the same person:
    an owed reply still fires, a thank-you inside its window still fires, a
    `confirm_chat` on a chat that is already booked still fires, and a re-ping
    against a confirmed close still fires. Answering somebody who wrote to you
    is not networking, and `contact_relevance`'s own inbound override already
    makes that argument.

    Reads `recruiting_style` off the contact dict, which
    `crm.today._build_actions` carries in from the firm row alongside `tier`,
    so this stays a pure function testable with no database.
    """
    if action.get("owed_reply"):
        return False
    if action.get("action") not in _APPLY_ONLY_ACTIONS:
        return False
    contact = action.get("contact") or {}
    if (contact.get("warmth") or "") != "cold":
        return False
    return (contact.get("recruiting_style") or "") == ASSESSMENT


# ---------------------------------------------------------------------------
# 2c. Seniority — how far up a cold ask may reach.
# ---------------------------------------------------------------------------
# Matched case-insensitively against `Contact.role`, highest rung first
# because the titles nest ("Managing Director" contains "Director", "Associate
# Director" is a director). Same conservative doctrine as
# `_RECRUITING_ROLE_MARKERS` above: every pattern names a rank, never a word
# that merely co-occurs with one.
#
# `\bMD\b` is in and `\bED\b` is not, and that asymmetry is deliberate. "MD"
# in a `Contact.role` on a finance board is a managing director; "ED" is a
# two-letter string that appears inside nothing useful and names an executive
# director only in a handful of European banks' conventions, so it is left to
# the spelled-out form. Erring here costs half a point on a cold card's rank,
# never a silenced card, which is why the list can afford to be short.
_SENIORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("md", r"\bmanaging director\b|\bmd\b|\bpartner\b|\bhead of\b"
           r"|\bchair(?:man|woman|person)?\b|\bchief \w+ officer\b"),
    ("director", r"\bexecutive director\b|\bdirector\b"),
    ("vp", r"\bvice[- ]president\b|\bvp\b|\bsvp\b|\bevp\b"),
    ("associate", r"\bassociate\b"),
    ("analyst", r"\banalyst\b"),
)
_SENIORITY_RES = tuple((name, re.compile(p, re.IGNORECASE))
                       for name, p in _SENIORITY_PATTERNS)

# The rungs a cold note should not be aimed at.
SENIOR_RUNGS = frozenset({"director", "md"})


def seniority(role: str | None) -> str:
    """The rung `role` names, or "" when the text names none.

    "" is the overwhelmingly common answer and the one that must cost
    nothing: 137 of the founder's 265 live rows have no role text at all, and
    99 of 226 in the audit's own count. A blank role is not a junior contact
    and not a senior one; it is a contact whose seniority nobody has recorded,
    and every rule downstream degrades to 1.0 on it (P3).
    """
    text = (role or "").strip()
    if not text:
        return ""
    for name, rx in _SENIORITY_RES:
        if rx.search(text):
            return name
    return ""


# ---------------------------------------------------------------------------
# 3. What is happening now at a firm.
# ---------------------------------------------------------------------------
# How far out a dated event still counts as a reason to write TODAY. Wider than
# the engine's `pre_deadline_reping_days` (14) on purpose: branch 3 fires an
# urgent, priority-0 re-ping and has to be tight, while this is the softer
# question "is there anything worth mentioning". Six weeks is roughly the point
# past which a student can do nothing about a date yet.
OPENING_HORIZON_DAYS = 45

# "New" means the same seven days `assistant.situation.RECENT_DAYS` means
# by it (the sibling surface that replaced `crm.today._new_at_your_firms`,
# retired 2026-08-31) — a student reading two cards on one page must never
# be told two definitions of recent.
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
         float and filtered through `crm.utils.confirmed_firm_dates`, the same
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
    # `confirmed_firm_dates()`, not a local confidence test. This reader spent
    # a release checking `_confidence_label(...) == "confirmed_official"` and
    # nothing else — the seventh copy of a bug six other CRM readers were
    # fixed for. Confidence alone is half the bar: a row can be certain about
    # a MONTH ("~ Sep 2027", precision "estimated", confidence 1.0) and that
    # is not something to hang a day-level countdown on. The helper holds both
    # halves, so a future third condition lands here once.
    for fd in (confirmed_firm_dates()
               .filter(firm_id__in=firm_ids, date__gte=today, date__lte=horizon)
               .order_by("-date")):
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
    # Filtered ONCE and walked twice. `_relevant_to_student` is a pure
    # function of `rows` — four in-memory passes plus an
    # `_eligibility_profile(user)` build — and calling it a second time for
    # the new-role pass below recomputed all of it to get the same list back.
    # The two loops must stay two loops (every deadline at a firm outranks
    # every new posting at it, whatever order the board returns them in), but
    # they can share the work.
    relevant = _relevant_to_student(user, rows)

    for o in relevant:
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
    for o in relevant:
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
    exact four filters `assistant.situation._new_role_events` already
    runs, and a fifth opinion
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

# ---------------------------------------------------------------------------
# 4b. Affinity — what a shared school is actually worth (2026-09-01).
# ---------------------------------------------------------------------------
# `school_affiliation` used to be an ADMISSION flag and nothing else: it got
# a contact past the relevance gate (REL_SCHOOL) and then changed their ev
# by exactly nothing at a tiered firm — measured 8.64 vs 8.64 for an alumnus
# and a stranger at the same tier-1 bank — while at a non-tiered firm
# `_SCHOOL_WEIGHT` (0.9) sat BELOW the stranger-at-an-unranked-firm weight
# (1.2). The one thing the networking research says is worth something
# scored as nothing, or as less than a stranger.
#
# WHAT IT IS WORTH, MEASURED RATHER THAN FOLKLORE. On a counted log of 93
# cold emails, alumni replied at 43% against 34% for strangers: a lift of
# about 1.3x, NOT the 4-6x the "alumni always reply" folklore claims. That
# is the multiplier for the bare flag, and it is small on purpose.
# SPECIFICITY BEATS THE FLAG: the same research puts a high-school-directory
# approach at 85%+ replies against ~25% for a bare college tie, so a NAMED
# tie — a club, a programme, a hometown, a prior employer, a mutual — earns
# 1.6. Two steps, not a curve, because two is what the evidence supports.
#
# A MULTIPLIER ON STRENGTH, NEVER ON THE CLASS LADDER. `crm.today`'s
# `_today_class` is untouched, so a cold alumnus with the best tie on the
# board still sits below a stranger who actually wrote back — asserted in
# tests, because it is the whole reason this is a multiplier and not a rank.
# Degrades to 1.0 with no flag and no text.
_AFFINITY_NONE = 1.0
_AFFINITY_SCHOOL = 1.3
_AFFINITY_SPECIFIC = 1.6

# Phrases that NAME A TIE between two people, matched case-insensitively
# against the contact's `school`, `angle` and `notes` — never `role`
# ("Analyst Programme" in a title is a job, not a bond). Same doctrine as
# `_RECRUITING_ROLE_MARKERS` above and `crm.recruitment`'s person markers:
# every entry is something a person writes when they mean "we have this in
# common", never a word that merely co-occurs with one. Rejected on purpose:
# a bare school name (that is the flag, not a specific tie), a bare
# "program"/"programme" (every posting has one), "finance" (a topic), and
# bare "MBA"/"scholar" (a credential the contact holds, not one shared).
_SPECIFIC_TIE_MARKERS: tuple[str, ...] = (
    r"\b(?:club|society|fraternity|sorority)\b",
    r"\b(?:same|my|our) (?:program(?:me)?|cohort|class|year|dorm|team|desk"
    r"|major|professor|section|high school|hometown)\b",
    r"\bhometown\b|\bgrew up (?:in|together|near)\b|\bhigh school\b",
    r"\b(?:worked|interned) (?:together|with me|alongside)\b",
    r"\b(?:prior|previous|former|old) (?:employer|colleague|co-?worker|manager"
    r"|boss|teammate)\b",
    r"\b(?:classmate|roommate|teammate|mentor|mentee)\b",
    r"\b(?:referred|introduced) (?:me|by|via|through)\b"
    r"|\bintro(?:duction)? (?:from|via|through)\b",
    r"\bmutual (?:friend|contact|connection)\b",
)
_SPECIFIC_TIE_RE = re.compile("|".join(_SPECIFIC_TIE_MARKERS), re.IGNORECASE)

# The contact-dict keys the tie search reads. `school` is always on the
# queue's dict; `angle` and `notes` are read WHEN PRESENT — `crm.today.
# _build_actions` withholds `angle` from the dict on purpose (it once leaked
# into a draft), so on the live queue the text lift arrives through `school`
# and through the student's own affiliations, and a caller that wants the
# notes scanned adds the keys. Reading, never writing: nothing here can put
# the text anywhere a draft would see it.
_AFFINITY_TEXT_KEYS = ("school", "angle", "notes")


def specific_tie(contact: dict, user=None, affiliations=()) -> str | None:
    """The phrase that names a specific tie with this contact, or None.

    Two sources. The marker list above, matched against the contact's own
    text; and the student's `affiliations` — a list of free-text strings on
    the User (a club, a programme, a hometown) that another change is adding
    concurrently, read with `getattr` so this works before the column
    exists and with no user at all. An affiliation counts when the contact's
    text contains it, case-insensitively; three characters is the floor so
    "PE" cannot match inside "open".
    """
    text = " ".join(
        str(contact.get(k) or "") for k in _AFFINITY_TEXT_KEYS
    ).strip()
    if not text:
        return None
    m = _SPECIFIC_TIE_RE.search(text)
    if m:
        return m.group(0)
    lowered = text.lower()
    # `affiliations` wins when given: the queue passes the list itself
    # rather than the User, because `_gate_and_rank` takes derived facts,
    # not query paths (see its docstring).
    for aff in (affiliations or getattr(user, "affiliations", None) or []):
        aff = str(aff or "").strip()
        if len(aff) >= 3 and aff.lower() in lowered:
            return aff
    return None


def affinity(contact: dict, user=None, affiliations=()) -> float:
    """1.6 for a named tie, 1.3 for the bare school flag, 1.0 otherwise."""
    if specific_tie(contact, user, affiliations):
        return _AFFINITY_SPECIFIC
    if contact.get("school_affiliation"):
        return _AFFINITY_SCHOOL
    return _AFFINITY_NONE


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

# THE COLD ASK'S SENIORITY CEILING (WS-CRM-10).
#
# WHAT IT ENCODES. Referrals flow downward, and a cold note aimed above the
# level that fields them is not a stronger version of the same move, it is a
# weaker one. A bulge-bracket VP described referring nearly every networking
# email he received to an analyst, and the flat statement from the same
# corpus is "No MD at a BB ever would have responded to a networking
# pre-analyst if they weren't already in process"
# (`research-networking-norms.md §2b`, Grade A). The ceiling is a function of
# connection strength rather than of the contact alone: cold goes to analysts
# and associates, an alum can go to any level (`§2d`, Grade B).
#
# WHY 0.5 AND NOT 0. The evidence says low-probability, not impossible, and
# nothing in this codebase may silence a card invisibly (P4). A half is a
# demotion a student can see and override by writing the note anyway; a zero
# would be a hidden filter wearing a weight's clothes. It is also one
# multiplier on one axis, deliberately: the sources discuss track and
# seniority at length and none of them conditions CADENCE on either
# (`§8a`, `§8f`), so splitting this into a matrix would be inventing four
# more numbers nobody measured.
#
# WHY IT LIFTS FOR AN ALUM OR A WARM THREAD. That is §2d's rule verbatim, and
# it is the reason this is a multiplier on the COLD ask specifically: a
# shared school or a reply already received is exactly the connection the
# source says raises the ceiling.
#
# WHAT WOULD CHANGE IT: a counted reply rate by rung on real Coverage sends.
# There is none; §2b is a practitioner account, strong on the mechanism and
# silent on the magnitude, so 0.5 is a direction with a size attached rather
# than a measurement.
_SENIOR_COLD_PENALTY = 0.5
# The floor: a keep-warm nudge with no event behind it. Not zero, because a
# genuinely long silence with an advocate is still worth something; low enough
# that anything real outranks it.
_NOW_NOTHING = 0.4


def expected_value(action: dict, user=None, affiliations=()) -> float:
    """Relevance x relationship strength x affinity x whether something real
    is happening.

    Multiplicative, not additive, and that is the substance of the ranking
    rather than a detail of it. Added, a pile of small facts about a stranger
    can outweigh one big fact about the person who would vouch for you.
    Multiplied, a zero-ish term stays zero-ish: a cold contact at a firm the
    student does not target cannot climb, however long they have been silent.

    `affinity` (section 4b) rides on strength: the measured ~1.3x for a
    shared school, 1.6 for a named tie, 1.0 for neither. `user` is optional
    and only feeds the student's own `affiliations` into the tie search;
    `crm.today._gate_and_rank` does not pass one yet, so the live queue gets
    the flag lift and the marker lift, and the affiliation lift is one
    argument away.

    Reads only keys `crm.today._build_actions` has already attached, so the
    scorer stays a pure function of the action dict and is testable without a
    database.
    """
    contact = action.get("contact") or {}
    rel = relevance_weight(action.get("relevance"), action.get("relevance_tier"))
    strength = _RELATIONSHIP_WEIGHT.get(contact.get("warmth"), 0.8) * affinity(contact, user, affiliations)

    if action.get("owed_reply"):
        now = _NOW_INBOUND
    elif action.get("closes_on") or action.get("priority") == 0:
        now = _NOW_DEADLINE
    elif action.get("action") == "thank_you":
        now = _NOW_THANK_YOU
    else:
        # The action's OWN claim on today, before anything at the firm is
        # consulted.
        if action.get("action") == "advance":
            now = _NOW_ADVANCE
        elif action.get("action") in ("first_outreach", "follow_up", "park",
                                      "confirm_chat"):
            now = _NOW_COLD_DUE
        else:
            now = _NOW_NOTHING
        # An opening RAISES the claim; it can never lower it. This used to be
        # an `elif` branch ahead of the action's own weight, which made the
        # opening REPLACE it — so an `advance` card at a firm with a role
        # posted this week scored `_OPENING_WEIGHT[new_role]` (1.6) while the
        # identical card at a firm with nothing happening scored `_NOW_ADVANCE`
        # (1.8). Finding a reason to write demoted the card below the one with
        # no reason at all, which inverts the whole point of `firm_openings`:
        # the module docstring's rule 3 is that a nudge WITH a trigger behind
        # it outranks one without. `max` is the fix and states the rule
        # directly, so a future weight cannot re-create the inversion by
        # sitting a tenth below an action baseline.
        opening = action.get("opening")
        if opening:
            now = max(now, _OPENING_WEIGHT.get(opening["kind"], _NOW_NOTHING))

    return round(rel * strength * now * senior_cold_factor(action), 4)


def senior_cold_factor(action: dict) -> float:
    """`_SENIOR_COLD_PENALTY` for a cold ask aimed above the rung that fields
    them, 1.0 for everything else. See that constant for the whole argument.

    A separate function rather than four lines inside `expected_value`
    because it is the one term in that product with a citation behind it, and
    because a caller (a card, a test, an explanation) that wants to say WHY a
    row sits where it does needs to be able to ask.
    """
    if action.get("action") not in _APPLY_ONLY_ACTIONS:
        return 1.0
    contact = action.get("contact") or {}
    # A recruiter is the one senior person a cold note is SUPPOSED to reach:
    # fielding them is the job. `is_recruiting_contact` is the existing
    # definition of that (P5) and reads the student's own answer first.
    if is_recruiting_contact(contact):
        return 1.0
    # §2d's ceiling-raisers, both of them.
    if contact.get("school_affiliation"):
        return 1.0
    if (contact.get("warmth") or "cold") != "cold":
        return 1.0
    if seniority(contact.get("role")) in SENIOR_RUNGS:
        return _SENIOR_COLD_PENALTY
    return 1.0


# ---------------------------------------------------------------------------
# 4c. Season — which move the calendar favours, derived and never named.
# ---------------------------------------------------------------------------
# THE MECHANISM, WHICH IS MODE-SWITCHING AND NOT INTENSITY. The practitioner
# rule is not "email harder in season": it is that first contact belongs in
# the low-competition window, and once the wave is on you "circle back with
# people to ask about timelines instead of reaching out for the first time"
# (`research-networking-norms.md §4a`, Grade A on the mechanism, Grade B on
# the prescription). Underneath it is a saturation fact, measured twice from
# the receiving side a year apart: inbound volume swings about tenfold, from
# roughly 10 emails a week off-peak to about 25 a day at peak (`§3a`, Grade
# A). A cold note is worth less when it lands in a pile of twenty-five.
#
# NO MONTH APPEARS ANYWHERE IN THIS FILE, and that is not a stylistic choice.
# The peak demonstrably MOVED by roughly eight months between 2021 and 2026,
# out of one half of the year and into the other (`§4d`) — the two windows are
# named in the source and deliberately not repeated here, because a month
# spelled in a comment is one careless edit away from being a month spelled in
# a condition. McKinsey's undergraduate deadline moved 3.5 months
# between consecutive cycles while its full-time deadline moved the other way,
# and any hardcoded constant is therefore wrong for at least one firm-role
# pair within twelve months (`research-consulting-forums.md §7`;
# `SYNTHESIS-PLAN.md` Part C item 6). So the season is read off Coverage's own
# board: what fraction of the firms it watches have already been observed
# opening for this cycle.
#
# TWO MODES, NOT A CURVE. The evidence supports a switch between two moves;
# it does not support a continuous intensity dial, and a curve would be five
# invented numbers wearing one measured one's clothes.
SEASON_EARLY = "early"
SEASON_CROWD = "crowd"

# The share of watched (firm, region) pairs that must have been observed
# opening before the queue calls it a crowd.
#
# WHY A HALF. It is the only threshold on this axis that does not need its own
# justification: "more of the market has started than has not" is the
# statement, and any other number would be a guess about how much of a wave
# constitutes a wave. The mechanism it stands in for is saturation, and
# saturation is about the median recipient's inbox, so the median firm is the
# right place to put the line.
#
# WHAT WOULD CHANGE IT: a measured reply-rate curve against board-open share.
# Coverage will be able to compute one from its own sends; it cannot yet.
SEASON_CROWD_SHARE = 0.5

# The weights each mode puts on the two cold moves and on the warm one.
#
# WHAT THEY ENCODE, AND WHY THEY ARE THIS SMALL. In `early`, a first note is
# worth more than a follow-up because the window is exactly when a stranger's
# note is read; in `crowd`, that inverts and the warm moves — advancing a live
# thread, a keep-warm with a real opening behind it — carry the day, which is
# §4a's "circle back instead of reaching out for the first time" stated as a
# multiplier. 1.25 and 0.8 are reciprocal to within rounding, so the mode
# reorders cards WITHIN a lane and cannot lift one lane over another: a cold
# stranger in `early` still sits below somebody who wrote back, because
# `crm.today._TODAY_CLASS` is a structural rung and this is a weight. That
# containment is the point — the tenfold saturation swing is a real effect on
# the margin, not a reason to hand a whole morning to strangers.
#
# WHAT WOULD RETIRE THE RULE: Coverage measuring its own reply rate by mode.
# If the two modes do not separate, this comes out entirely rather than being
# tuned, because its whole claim is that they do.
_SEASON_WEIGHTS: dict[str, dict[str, float]] = {
    SEASON_EARLY: {"first_outreach": 1.25, "follow_up": 0.8,
                   "advance": 1.0, "keep_warm": 1.0},
    SEASON_CROWD: {"first_outreach": 0.8, "follow_up": 1.25,
                   "advance": 1.25, "keep_warm": 1.25},
}


def season_mode(user, today=None) -> str | None:
    """`"early"`, `"crowd"`, or None when the board cannot say.

    Reads the measured opening activity for the firms the student targets,
    through `directory.estimates.observations_for` — the one observation
    reader (P5) — and asks a single question: of the (firm, region) pairs
    Coverage watches for this student, what share have already been observed
    opening postings for the target buckets?

    None is a real and common answer, and it degrades to exactly today's
    behaviour: a student with no tiered firms, or whose firms have no
    observation clearing the sample floor, gets `_SEASON_WEIGHTS` applied not
    at all and the queue order they had before this function existed (P3).
    """
    del today  # the board's own state answers this; no calendar is consulted
    firm_ids = set(tiered_firm_tiers(user))
    if not firm_ids:
        return None
    rows = estimates.observations_for(firm_ids)
    if not rows:
        return None
    watched = 0
    opened = 0
    for (_firm_id, _region), obs in rows.items():
        # A row that has never cleared the sample floor on either side is not
        # evidence of a quiet market, it is evidence of a board Coverage has
        # barely watched. Counting it as "not yet open" would read a thin
        # sample as an early season.
        if (obs.opened_count < CYCLE_OBSERVATION_MIN_SAMPLE
                and obs.closed_count < CYCLE_OBSERVATION_MIN_SAMPLE
                and not obs.currently_open_count):
            continue
        watched += 1
        if obs.opened_count >= CYCLE_OBSERVATION_MIN_SAMPLE:
            opened += 1
    if not watched:
        return None
    return SEASON_CROWD if opened / watched >= SEASON_CROWD_SHARE else SEASON_EARLY


def season_factor(action: dict, mode: str | None) -> float:
    """The mode's weight for this card's action, or 1.0.

    Applied by `crm.today._gate_and_rank` alongside `ev` rather than inside
    `expected_value`, because the mode is a fact about the student's whole
    board and `expected_value` is deliberately a pure function of one action
    dict with no route back to the database.
    """
    if not mode:
        return 1.0
    return _SEASON_WEIGHTS.get(mode, {}).get(action.get("action"), 1.0)


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
# `UserFirm.tier`. The warmth is `Contact.warmth`, which the pipeline ratchet
# only ever moves off a real touch. The dates are a confirmed `FirmDate` or an
# `Opportunity.deadline`. Nothing here rounds a maybe into a statement — see
# `assistant/brief.py` for the bug that taught this codebase the cost of a
# surface that asserts the inverse of the student's own data.
#
# THE SECOND COMPLAINT, 2026-09-02, about the keep-warm card and then about
# all of them: "refine and concise info presented here, short, concise, clean
# and informative." The 2026-08-31 pass above bought its honesty with words,
# and by tonight the card was saying the same fact three ways. On the
# founder's own KEEP WARM card:
#
#     KEEP WARM                                  <- the badge, and the verb
#     chatted                                    <- the warmth chip
#     Tier 1 target, and you have already had the conversation.
#     A role there closes Sep 30.
#     Last: Chat happened · 24 business days ago  <- the footer
#
# "you have already had the conversation" is nine words for `warmth ==
# "chatted"`, which the chip states in one and the footer restates underneath.
#
# THE RULE THIS SECTION NOW HOLDS, and it is `test_rail_copy_2026_09_02.py`'s
# rule one surface over: KEEP EVERY FACT, CUT EVERY RESTATEMENT. A sentence
# may not say what the badge above it says, what the warmth chip beside it
# says, what the deadline chip beside it says, or what the footer below it
# says. What is left is the part of the card only this sentence can carry, and
# where nothing is left the sentence gets shorter rather than padded. The one
# fact that left the face is the warmth GLOSS, and it moved to the chip's own
# `title` (`WARMTH_NOTE`) rather than being deleted — the same "cut the
# explanation, keep the fact, put it in a title" move the rail pass made.
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

# THE WARMTH GLOSS, and the card's `title` for the chip that already prints
# the bare value beside the contact's name.
#
# These two strings used to be a clause inside the keep-warm sentence
# (`_STRENGTH_CLAUSE`: "and they would vouch for you", "and you have already
# had the conversation"). They earned their place in 2026-08-31's pass, when
# the card said "Advocate." and nothing else and the founder called the
# prompts hollow — a stored warmth value is vocabulary, not a reason.
#
# They are still that gloss, and the fact has not moved off the card: it moved
# off the FACE. `_act_card.html` renders `contact.warmth` as a chip two lines
# above the sentence, so on the face the sentence was glossing a word the
# reader could already see, in nine words, directly above a footer saying
# "Chat happened". A `title` on the chip is where a fact about what a label
# MEANS belongs on this page — the same place the rail put "around the close"
# and "the good window" the same night.
#
# All four warmths, not just the two a keep-warm card can hold: the chip is
# drawn on every card in the queue, and a chip that explains itself on two
# cards and not on the other four is worse than one that never did.
WARMTH_NOTE = {
    "cold": "No reply from them yet.",
    "replied": "They have written back.",
    "chatted": "You have already had the conversation.",
    "advocate": "They would vouch for you.",
}

# THE BENCH'S copy, and from 2026-09-02 only the bench's. It was the act
# card's too (`keep_warm_reason` interpolated it as "and {clause}") until that
# card's own warmth chip took the job over; the bench strip in
# `templates/crm/_cockpit.html` renders no warmth chip and no ledger row, so
# the clause is still the only place a parked contact's warmth is stated
# there. Kept as its own constant rather than folded into `WARMTH_NOTE`
# because the two are different sentences for different surfaces: this one is
# a mid-sentence clause with no subject and no full stop, and `WARMTH_NOTE` is
# a standalone sentence for a tooltip.
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


def why_this_person(action: dict) -> str:
    """"Tier 1 target", "Same school" — why this contact is allowed to cost a
    slot at all, in one noun phrase and no verb.

    Extracted from `keep_warm_reason` on 2026-09-02 so `card_reason`'s
    first-outreach branch can say it too. That card had been saying "Added but
    never contacted. Send the first note.", which is the badge and the footer
    read back; the lead is the one thing about a stranger the card knows and
    had never printed.
    """
    contact = action.get("contact") or {}
    relevance = action.get("relevance")
    tier = action.get("relevance_tier")
    if contact.get("campaign_excluded"):
        return _CAMPAIGN_LEAD
    if relevance == REL_TIERED:
        return f"Tier {tier} target" if tier in _TIER_WEIGHT else "On your target list"
    return _LEAD_BY_RELEVANCE.get(relevance, "Warm contact")


def keep_warm_reason(action: dict) -> str:
    """The rewritten reason for a `maintain` / `keep_warm` card.

    TWO CLAUSES UNTIL 2026-09-02, now one plus whatever is live: the warmth
    half ("and you have already had the conversation") was the chip's own
    value spelled out, and it moved to the chip's `title` (`WARMTH_NOTE`).
    What is left is the lead — which is the half the chip CANNOT say, because
    the tier and the school tie are facts about the student's list and not
    about the contact's temperature — and the opening, which is what makes
    today the day.
    """
    contact = action.get("contact") or {}
    opening = action.get("opening")
    clause = _opening_clause(opening)

    if is_recruiting_contact(contact):
        # No "keep in touch" framing for somebody whose relationship to the
        # student IS the recruiting process. The opening is the entire reason
        # this card exists, so it leads; without one the card is not drawn at
        # all (see `crm.today`). "They are your recruiting contact there" lost
        # its subject and its second "there" — the clause in front of it
        # already said where, and the qualifier is what changes the ask.
        return f"{clause} Your recruiting contact.".strip()

    first = f"{why_this_person(action)}."
    return f"{first} {clause}".strip() if clause else first


# The one sentence a recruiting contact's inbound message earns. Says what NOT
# to do — the card it replaces said "propose a 15-min chat" to a
# talent-acquisition manager whose "reply" was a mass programme invite.
#
# IT USED TO OPEN "They wrote to you. Answer the note." (2026-09-02: cut).
# Both halves are printed elsewhere on the same card and neither is printed
# anywhere else: the badge reads REPLY, and the footer under the sentence
# reads "They replied · 14 business days". The qualifier is the only clause
# here that changes what the student writes, so it is the only clause left.
RECRUITING_REPLY_REASON = "Recruiting contact, not a coffee chat."
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
CAMPAIGN_REPLY_REASON = "From a send that was not your recruiting."
CAMPAIGN_REPLY_LABEL = "Reply"

# And the same one sentence for a recruitment-hidden contact's inbound
# message (`crm/recruitment.py`) — the professor who writes with a real
# question gets an answer, never a recruiting ask: the inbound override
# grants exactly one thing, and the engine's own action (a re-ping, a chat
# proposal) would be a recruiting move aimed at somebody the rule just said
# is not part of the user's recruiting.
UNRELATED_REPLY_REASON = "Not part of your recruiting."
UNRELATED_REPLY_LABEL = "Reply"


# ---------------------------------------------------------------------------
# 5b. Every OTHER card's sentence (2026-09-02).
# ---------------------------------------------------------------------------
# The founder asked for the keep-warm card's diet and then for it everywhere:
# "This is Today's page under Move it forward but do it for all cards like
# Move it forward."
#
# WHY THE REWRITE IS HERE AND NOT IN THE ENGINE. `coverage_domain.cadence`
# writes a reason for every branch, and its own contract says `ctx` "carries
# the raw numbers the reason string renders, so a UI can build its own
# phrasing without re-parsing `reason`". That is the door this walks through:
# every clause below is built from `ctx`, never scraped back out of the
# engine's prose, so the sentence and the number that produced it cannot
# disagree. The engine keeps its fragments and its golden fixtures; this
# module keeps the copy, exactly as `crm.today._sentenceize` and `_age_in_days`
# already do one layer down.
#
# WHAT EACH BRANCH IS ALLOWED TO SAY is decided by what the card already
# renders around the sentence (`templates/crm/_act_card.html`):
#
#     the badge          `a.label` — the verb. FOLLOW UP, FIRST OUTREACH, REPLY
#     the warmth chip    `contact.warmth`, glossed in its title (WARMTH_NOTE)
#     the deadline chip  "Closes Sep 30", confirmed FirmDates only
#     the footer         the latest real touch and its age in business days
#     the buttons        Done · They replied · Compose · Snooze · Skip · Park it
#
# A clause that repeats any of those is cut. Nothing else is.
_FOLLOWUP_FIRST = "No reply to your first note."


def _no_reply(outbound) -> str:
    """"No reply to your first note." / "No reply after 3 notes."

    BEFORE: "No reply 7 business days after touch 1. Follow up." Three facts,
    two of them printed twice — "7 business days" is the footer's own number
    in the footer's own unit, and "Follow up" is the badge. "touch 1" is the
    one fact only this sentence held, and it is the one that decides whether
    the student pushes again or parks: a first note unanswered is normal, a
    third is a verdict.

    It says "note" rather than "touch" because the ledger row below it says
    "Reached out" and the button says Compose; "touch" is the schema's word
    for the row, not the student's word for the email.
    """
    if not isinstance(outbound, int) or outbound < 1:
        # No count on the dict is not a licence to invent one. The silence
        # itself is still true and the footer still carries the age.
        return "No reply yet."
    return _FOLLOWUP_FIRST if outbound == 1 else f"No reply after {outbound} notes."


def card_reason(action: dict) -> str:
    """The card sentence for every action `keep_warm_reason` does not own.

    Returns the engine's own (already prose-polished) sentence untouched for
    any action this does not recognise, so a new cadence branch renders the
    way it did before anybody edited this file rather than rendering nothing.
    """
    ctx = action.get("ctx") or {}
    kind = action.get("action")
    engine = (action.get("reason") or "").strip()

    if kind == "first_outreach":
        # BEFORE: "Added but never contacted. Send the first note." The first
        # half is the footer ("No touches yet"), the second is the badge
        # (FIRST OUTREACH), and between them the card said nothing about the
        # person. `why_this_person` is what it should have been saying: on a
        # cold stranger the tier or the school tie IS the reason to spend the
        # morning on them. Reachable values are the tiered and school leads
        # only — `contact_relevance` needs `owed_reply` for REL_INBOUND, and a
        # contact with no touches on record is owed nothing.
        return f"{why_this_person(action)}."

    if kind == "follow_up":
        return _no_reply(ctx.get("outbound"))

    if kind == "park":
        if ctx.get("expired"):
            # The expired-follow-up park. BEFORE: "First note went unanswered
            # 5 weeks ago. Park it, or re-open with a new reason." The weeks
            # were the footer's business days in a second unit — the exact
            # two-registers-for-one-fact problem the rail pass ended — and
            # "Park it" is the badge and the primary button. What is left is
            # the thing the card cannot otherwise say: why this is a park and
            # not the follow-up the same silence would have earned last week.
            return "Too late to follow up. Re-open only with a new reason."
        # The cap-reached park. Same sentence the follow-up card gets, because
        # it is the same fact; the badge is what differs, and the badge is
        # where the difference belongs.
        return _no_reply(ctx.get("outbound"))

    if kind == "advance":
        # BEFORE: "They replied. Propose a 15-min chat." The first sentence is
        # the footer ("They replied · 14 business days"), the second is the
        # badge (Propose a chat). The size of the ask is the whole of what the
        # badge leaves out, and it is the half that gets the chat agreed.
        return "Ask for 15 minutes."

    if kind == "reping":
        # BEFORE: "Barclays app closes Aug 30. Re-ping before you submit."
        # The firm is the card's own identity line, the date is the
        # `Closes Aug 30` chip beside the badge (built from the SAME
        # `_closing_soon` index this branch fired on, so the chip is there
        # whenever this card is), and "re-ping" is the badge. The order of
        # operations is what nothing else states.
        return "Before you submit the application."

    if kind == "thank_you":
        # BEFORE: "Chat done 2d ago. Send thank-you." — the footer's fact in
        # calendar days beside the footer's business days, then the badge.
        # And the window that justifies the whole card never reached the
        # screen at all: the engine writes it as "(within 24h)" / "(OVERDUE)"
        # and `crm.today._sentenceize` strips every parenthetical. So this
        # branch cuts two restatements and promotes the fact they were hiding.
        hours = ctx.get("window_hours")
        if not isinstance(hours, int):
            return engine
        if ctx.get("overdue"):
            return f"Overdue past the {hours} hour window."
        return f"Within {hours} hours of the chat."

    if kind == "confirm_chat":
        # BEFORE: "Chat was scheduled for Aug 24. Did it happen? Log the chat
        # or reschedule." The question and the two options are the card's own
        # two buttons, which is as literal as a restatement gets. The booked
        # day is the fact, and where none is held the card says the state and
        # stops — the silence itself is already in the footer.
        booked = ctx.get("scheduled_on")
        if booked:
            try:
                return f"A chat was set for {_on_date(date.fromisoformat(booked))}."
            except (TypeError, ValueError):
                return engine
        return "A chat was being arranged."

    if kind == "promised_followup":
        # BEFORE: "They offered a referral 5d ago. Chase it." — "Chase it" is
        # the badge ("Chase the offer"). The offer and its age both stay: the
        # age here is measured off `promised_action_at`, which is a DIFFERENT
        # event from the footer's last touch, so the two numbers are two facts
        # and not one fact twice.
        promised = str(ctx.get("promised") or "").strip()
        if not promised:
            return engine
        days = ctx.get("days_since")
        if isinstance(days, int):
            return f"They offered {promised} {days}d ago."
        return f"They offered {promised}."

    return engine
