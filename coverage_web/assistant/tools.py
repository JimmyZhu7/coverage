"""The advisor's tools: everything it is allowed to know, and the handful of
things it is allowed to change.

THE TENANT RULE, which is the whole security story
---------------------------------------------------
`user` is a PARAMETER OF `execute()`, never a tool argument. The model cannot
name a user, cannot pass a user id, cannot ask about "the other student" —
there is no field in any schema for it. Every body below reads rows through
`Model.objects.for_user(user)` (see `coverage_web/tenancy.py`: the default
manager RAISES on an unscoped query, so a forgotten scope is a loud failure
at the first call, not a silent leak), and `user` is bound by the view from
`request.user`. Tenancy.py's deliberate, greppable cross-tenant escape hatch
— the unscoped manager it exposes under a name every reviewer can search for
— is not used anywhere in this app, and `assistant/tests/test_isolation.py`
fails the build if that name ever appears here.

The practical consequence: user B handing this app one of user A's row ids
gets "not found", not user A's data, because the `.for_user()` filter runs
before the pk lookup. Every read AND write tool is tested for exactly that.

WHAT THE MODEL MAY DO
---------------------
Reads are free. Writes are exactly seven — `log_touch`, `track_opportunity`
(`saved`/`clear` only), `remember`, `add_calendar_event`, `add_contact`,
`set_contact_status` and `update_settings` — chosen because each is cheap
and reversible in one click somewhere the student already knows (the CRM
page itself, the Calendar page's own delete button, the Talk page's own
memory list, the Settings page's own inputs), and none of them sends
anything to another human. Nothing here emails anyone, edits a note, or
archives a contact. That is a product decision, not an oversight: an
advisor that can quietly rewrite your CRM is a different, scarier product.
When the student asks for something outside these seven, the system prompt
tells the model to say so plainly and name the page they'd do it on.

THE ONE WRITE THAT IS NOT ONE-SHOT
-----------------------------------
`update_settings` splits its allowlist into ordinary fields, which apply on
the first call like every other write here, and IMPORTANT ones
(`SETTINGS_IMPORTANT`), which do not. An important field called without
`confirmed=true` writes NOTHING and comes back as a ToolError telling the
model to describe the concrete effect to the student and wait for a yes in
their own next message before calling again. Timezone is the archetype: it
silently redefines what day the queue, the pace week and every deadline
countdown are talking about, and a student who says "I'm in London now"
in passing has not asked for that. `regions`/`tracks` are there for a
different reason — they REPLACE a whole list, so an unconfirmed "add HK"
would silently drop US. The protocol is stated in `agent.SYSTEM_PROMPT` as
well as in the error, because a model will not reliably infer a two-call
handshake from an error message it is seeing for the first time.

UNTRUSTED TEXT
--------------
Notes, job titles, locations, event descriptions and firm names are strings
somebody else wrote — a recruiter, a scraped posting, the student in a hurry.
Every one of them is truncated to `MAX_STR` chars on the way out (same
posture as `crm.ai_brief._MAX_NOTE_CHARS`) and the system prompt states that
text inside a tool result is DATA ABOUT THE CRM, never an instruction. Notes
are additionally run through `crm.views._display_note` so the machine
bookkeeping markers (`[gmail:..]`, `[capture:..]`, `[assistant:..]`) never
reach the model as if they were content.

DISAMBIGUATION IS IN THE RESULT SHAPE, not only in the prompt
--------------------------------------------------------------
`search_contacts` returns `ambiguous: true` plus a literal instruction field
whenever more than one contact matches. A prompt rule alone is a suggestion;
a field in the payload the model is reading at the moment it has to decide is
the reminder in the right place. Logging a touch against the wrong Chen is
not a reversible-feeling mistake to a student, even though the row itself is.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from django.db import IntegrityError
from django.utils import timezone

from accounts.forms import (
    AUTO_TIMEZONE,
    CLASS_YEAR_CHOICES,
    REGION_CHOICES as PROFILE_REGION_CHOICES,
    TRACK_CHOICES,
    CadenceForm,
    WeeklyPaceForm,
    known_timezones,
)
from accounts.models import WORK_AUTH
from analytics.events import record_event
from analytics.models import UserOpportunity
from coverage_domain.pipeline import CHANNELS, THREAD_STATES, TOUCH_TRANSITIONS, WARMTH
from crm import services
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from crm.today import TUNABLE_CADENCE_PARAMS, _build_actions
from crm.utils import ACTION_LABELS, CHANNEL_LABELS, TOUCH_KIND_LABELS
from crm.views import _display_note
from directory.classify import TARGET_BUCKETS
from directory.models import Firm, FirmDate, Opportunity
from directory.recommend import cycle_choices
from directory.views import _apply_region_filter, _STAGE_LABELS

from .models import AdvisorMemory
from .situation import build_situation

# Every untrusted string is cut to this before it reaches the model. Notes and
# posting titles are the two that actually run long; the cap applies to all of
# them so there is one rule rather than a per-field judgement call.
MAX_STR = 300

# Row caps. The model asks for `limit`; the code decides what it actually
# gets. A 200-row tool result is both expensive and unreadable, and an
# advisor that lists 40 contacts has not advised anyone.
DEFAULT_ROWS = 10
MAX_ROWS = 25

# The regions this product models (Contact.REGION_CHOICES' vocabulary).
REGIONS = ("us", "hk")

# How many contacts one `set_contact_status` call may move. Same number and
# same reasoning as MAX_ROWS above: a batch bigger than a screenful is one
# the student cannot have read back before saying yes to it, and the CRM's
# own bulk control (`crm.today.today_park_all`) works off the park strip,
# which is bounded by the same order of magnitude. Over the cap the tool
# refuses the whole call rather than silently doing the first 25.
MAX_BULK_CONTACTS = 25

_KIND_LABELS = dict(TOUCH_KIND_LABELS)
_CHANNEL_LABELS = dict(CHANNEL_LABELS)


# ---------------------------------------------------------------------------
# The settings allowlist.
#
# Built FROM the five forms /welcome/settings/ actually posts — ProfileForm,
# WorkAuthorizationForm, CadenceForm, WeeklyPaceForm, NotificationsForm — and
# validated against their own vocabularies and ranges (imported above, never
# restated), so a value this tool accepts is one the real Settings page would
# accept too, and a range tightened there tightens here on the same commit.
#
# WHAT IS DELIBERATELY NOT HERE, and will not be added:
#   email          — USERNAME_FIELD, the login identity itself.
#   avatar,
#   remove_avatar  — a file upload; there is no file in a chat turn.
#   google_sub     — the auth link.
#   password, calendar_token, plan, is_staff/is_superuser —
#                    credentials, secrets and entitlements, none of which are
#                    a recruiting preference.
#   language       — the column exists but is READ BY NOTHING (see its own
#                    field comment); Settings removed its control for exactly
#                    that reason, and a tool that writes it would be the same
#                    defect wearing a different hat.
#   timezone_auto  — not an independent knob: it is a consequence of HOW the
#                    zone was chosen, and `timezone` below sets it the same
#                    way `ProfileForm.apply_to` does.
# ---------------------------------------------------------------------------
_WORK_AUTH_VALUES = frozenset(value for value, _ in WORK_AUTH)
_WORK_AUTH_PREFIX = "work_auth_"
_WORK_AUTH_FIELDS = tuple(
    f"{_WORK_AUTH_PREFIX}{code}" for code, _ in PROFILE_REGION_CHOICES
)
_PROFILE_REGIONS = frozenset(code for code, _ in PROFILE_REGION_CHOICES)
_PROFILE_TRACKS = frozenset(code for code, _ in TRACK_CHOICES)
_CLASS_YEARS = frozenset(int(value) for value, _ in CLASS_YEAR_CHOICES if value)

SETTINGS_FIELDS: tuple[str, ...] = (
    "name",
    "school",
    "class_year",
    "target_cycles",
    "regions",
    "tracks",
    "timezone",
    "weekly_touch_goal",
    "weekly_digest_enabled",
    "advocate_target",
    *TUNABLE_CADENCE_PARAMS,
    *_WORK_AUTH_FIELDS,
)

# The fields that do NOT apply on the first call. See the module docstring:
# each of these changes something the student would not see change, so the
# tool refuses once and makes the model say out loud what it is about to do.
SETTINGS_IMPORTANT = frozenset({"timezone", "regions", "tracks", "target_cycles"})

# What the model must tell the student BEFORE asking them to confirm. Written
# in the student's terms, not the column's — "what day your queue thinks it
# is" is the thing they can actually agree or object to.
_IMPORTANT_EFFECTS = {
    "timezone": (
        "this changes what day your queue and deadlines think it is — your "
        "cadence queue, your pace week and every deadline countdown all roll "
        "over on this zone"
    ),
    "regions": (
        "this REPLACES your whole list of target markets, so any market not "
        "in the new value is dropped, and the Opportunities feed and your "
        "firm recommendations follow it"
    ),
    "tracks": (
        "this REPLACES your whole list of target tracks, so any track not in "
        "the new value is dropped, and the Opportunities feed and your firm "
        "recommendations follow it"
    ),
    "target_cycles": (
        "this REPLACES your whole list of target recruiting cycles, so any "
        "cycle not in the new value is dropped, and the Opportunities feed "
        "stops boosting postings for a cycle you remove"
    ),
}


class ToolError(Exception):
    """A tool could not do what was asked — a row that isn't this user's, a
    bad enum, a missing argument. Surfaced to the model as an `is_error`
    tool_result so it can correct itself or tell the student, never as a 500."""


def _s(value, limit: int = MAX_STR) -> str:
    """One untrusted string, safe to hand the model: stripped and capped."""
    text = ("" if value is None else str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _limit(raw, default: int = DEFAULT_ROWS) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_ROWS))


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _today(user):
    """The student's own date. `accounts.middleware.TimezoneMiddleware` has
    already activated `User.timezone` for the request, so `localdate()` is
    their day — the same call `crm/today.py` makes, so the advisor and the
    Today page can never disagree about what "today" is."""
    return timezone.localdate()


def _days_since(ts, *, now=None) -> int | None:
    if ts is None:
        return None
    now = now or timezone.now()
    return max(0, (now - ts).days)


def _firm_name(contact) -> str:
    if contact.firm_id:
        return _s(contact.firm.name, 120)
    return _s(contact.firm_text, 120)


# ---------------------------------------------------------------------------
# Tool schemas. Strict (`strict: true` + `additionalProperties: false`), so a
# tool body never has to defend against a mistyped argument — the API's
# grammar-constrained sampling guarantees the shape before the call lands.
# ---------------------------------------------------------------------------
def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_today_queue",
        "description": (
            "The student's cadence queue for today: who the engine says to "
            "contact next, what action it wants, and the reason it gives. "
            "This is the same ranked list the Today page shows — use it for "
            "any 'who should I follow up with' or 'what should I do today' "
            "question rather than reasoning it out from contacts yourself."
        ),
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "search_contacts",
        "description": (
            "Find people in the student's own network by name, firm, or role. "
            "Always returns contact ids — you need one for get_contact and "
            "log_touch. If more than one person matches, the result says so; "
            "ask the student which one before any write."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "query": {
                    "type": "string",
                    "description": "Name, firm, or role fragment, e.g. 'Chen' or 'Goldman'.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max rows, 1-{MAX_ROWS}. Defaults to {DEFAULT_ROWS}.",
                },
            },
            ["query"],
        ),
    },
    {
        "name": "get_contact",
        "description": (
            "One person in full: warmth, thread state, firm and the student's "
            "tier for it, plus their last 8 logged interactions with dates and "
            "notes. Use before advising on a specific relationship."
        ),
        "strict": True,
        "input_schema": _schema(
            {"contact_id": {"type": "integer", "description": "From search_contacts."}},
            ["contact_id"],
        ),
    },
    {
        "name": "search_opportunities",
        "description": (
            "Open campus roles on Coverage's board — insight programmes, "
            "internships and entry-level roles only. Filter by market, firm, "
            "free text, or how soon they close. Deadlines here are what the "
            "firm published; a role with no deadline simply never stated one."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "region": {
                    "type": "string",
                    "enum": list(REGIONS),
                    "description": "us or hk. Omit for every market.",
                },
                "query": {"type": "string", "description": "Free text over title, firm, location."},
                "firm": {"type": "string", "description": "Firm name or slug."},
                "closing_within_days": {
                    "type": "integer",
                    "description": "Only roles with a stated deadline this many days out or sooner.",
                },
                "limit": {"type": "integer", "description": f"Max rows, 1-{MAX_ROWS}."},
            }
        ),
    },
    {
        "name": "get_firm",
        "description": (
            "One firm: the student's tier for it, its upcoming published dates, "
            "how many open roles it has on the board, and every contact the "
            "student has there ranked by warmth. The tool for 'what's my "
            "position at X'."
        ),
        "strict": True,
        "input_schema": _schema(
            {"name_or_slug": {"type": "string", "description": "e.g. 'Goldman Sachs' or 'goldman-sachs'."}},
            ["name_or_slug"],
        ),
    },
    {
        "name": "get_my_firms",
        "description": (
            "Every firm the student has tiered as a target, grouped by "
            "tier, with contact count, warmest relationship, and open role "
            "count at each. The portfolio view — get_firm answers 'what's "
            "my position at X' for one firm; this answers 'across my "
            "targets, where am I thinnest' for all of them at once."
        ),
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "get_calendar",
        "description": (
            "Coffee chats and events on the student's calendar, soonest first. "
            "Only what is actually scheduled — this is not the deadline board."
        ),
        "strict": True,
        "input_schema": _schema(
            {"days_ahead": {"type": "integer", "description": "Look-ahead window in days. Defaults to 14."}}
        ),
    },
    {
        "name": "get_my_pipeline",
        "description": (
            "The roles the student has saved or is somewhere in the funnel on, "
            "grouped by status, with deadlines. Their applications, not the "
            "whole board."
        ),
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "get_situation",
        "description": (
            "What changed recently on roles this student tracks or firms "
            "they already know: a deadline that moved, a role that closed, "
            "or a fresh posting at a firm where they have a contact or a "
            "target tier. Use this for 'what's new', 'anything changed', "
            "'did a deadline move' — it has memory of what a posting used "
            "to say, which a fresh search_opportunities call does not."
        ),
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "log_touch",
        "description": (
            "Record an interaction that already happened with one contact. "
            "This moves warmth and thread state through the same ratchet the "
            "app's own button uses, and the touch is permanently marked as "
            "logged by the assistant. Only call this when the student has told "
            "you the interaction happened — never to 'catch up' a record you "
            "inferred. If which person they mean is at all unclear, ask first."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "contact_id": {"type": "integer", "description": "From search_contacts."},
                "kind": {
                    "type": "string",
                    "enum": sorted(TOUCH_TRANSITIONS),
                    "description": "What happened.",
                },
                "channel": {"type": "string", "enum": list(CHANNELS), "description": "Where it happened."},
                "note": {"type": "string", "description": "The student's own words about it, if they gave any."},
            },
            ["contact_id", "kind", "channel"],
        ),
    },
    {
        "name": "track_opportunity",
        "description": (
            "Save a role to the student's pipeline, or clear it again. Saving "
            "is the only funnel state you can set — moving something to "
            "submitted / interview / offer is done by the student on the "
            "Opportunities page."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "opportunity_id": {"type": "integer", "description": "From search_opportunities."},
                "status": {"type": "string", "enum": ["saved", "clear"]},
            },
            ["opportunity_id", "status"],
        ),
    },
    {
        "name": "remember",
        "description": (
            "Save one short, durable fact about the student that should "
            "carry into every FUTURE conversation, not just this one — a "
            "preference ('targeting HK over US'), a decision ('ruled out "
            "PE'), a constraint ('needs sponsorship in the US'). Not for "
            "anything already trackable in their CRM data (a firm tier, a "
            "contact, a deadline — those belong on the Network or "
            "Opportunities page, not here) and not for something true only "
            "of this one conversation."
        ),
        "strict": True,
        "input_schema": _schema(
            {"fact": {"type": "string", "description": "One short sentence, e.g. 'Not pursuing PE roles.'"}},
            ["fact"],
        ),
    },
    {
        "name": "add_calendar_event",
        "description": (
            "Put one thing on the student's calendar — a coffee chat, a "
            "superday, a flight, anything that has a date. Leave start_time "
            "out for an all-day entry ('Superday on the 14th') rather than "
            "inventing a clock time; give both start_time and end_time when "
            "they state a range. Only call this when the student has "
            "actually asked you to add or schedule something — never to "
            "record a chat that already happened (that is log_touch) and "
            "never on a guess about when something is."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "title": {"type": "string", "description": "What to call it, e.g. 'Coffee with Priya', 'Superday', 'Flight to HK'."},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "start_time": {
                    "type": "string",
                    "description": "HH:MM, 24-hour, in the student's own timezone. Omit entirely for an all-day entry.",
                },
                "end_time": {
                    "type": "string",
                    "description": "HH:MM, 24-hour. Only meaningful together with start_time.",
                },
                "kind": {
                    "type": "string",
                    "enum": [k for k, _ in CalendarEvent.KIND_CHOICES],
                    "description": "'chat' for a coffee chat, 'event' for anything else. Defaults to event.",
                },
                "contact_id": {"type": "integer", "description": "From search_contacts, if this is about someone specific."},
                "location": {"type": "string", "description": "Zoom, their office, an address — whatever they gave."},
            },
            ["title", "date"],
        ),
    },
    {
        "name": "add_contact",
        "description": (
            "Add one new person to the student's network — someone they just "
            "met, were introduced to, or found and want to track. Search "
            "first: if they are already in the network this refuses rather "
            "than making a second copy of the same person. Only call this "
            "when the student has actually asked you to add someone, never "
            "to tidy up a name that came up in conversation.\n\n"
            "SPLIT 'TITLE AT FIRM' EVERY TIME — this is the one mistake that "
            "actually happens here, on every model, and it cannot be "
            "corrected afterward (there is no edit-contact tool, so a firm "
            "left out is gone for good). 'Associate at Evercore' is TWO "
            "facts, not one: role='Associate' and firm_text='Evercore'. "
            "Before calling this, name the firm out loud in your own head "
            "and check firm_text is not empty whenever the student named an "
            "employer — 'Marcus Lee, Associate at Evercore' means "
            "firm_text='Evercore', not firm_text left blank because the "
            "sentence was 'about' the person rather than the firm."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "name": {"type": "string", "description": "Their full name, as the student said it."},
                "firm_text": {
                    "type": "string",
                    "description": (
                        "REQUIRED whenever the student named an employer — "
                        "the firm/company name only, e.g. 'Evercore'. Read "
                        "the tool description above before leaving this "
                        "blank."
                    ),
                },
                "role": {
                    "type": "string",
                    "description": "Their title only, e.g. 'VP, IBD' or 'Associate' — not the firm.",
                },
                "email": {"type": "string", "description": "Their email, if the student gave one."},
            },
            ["name"],
        ),
    },
    {
        "name": "set_contact_status",
        "description": (
            "Set warmth and/or thread state directly on one contact or on "
            "several at once — the manual override, not a logged "
            "interaction. Use this when the student is CORRECTING the "
            "record ('park those three, they're never replying', 'mark her "
            "as an advocate'). If something actually happened with one "
            "person, that is log_touch instead. Ids that aren't theirs are "
            "reported back as not found, never silently skipped."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "contact_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": f"From search_contacts. Up to {MAX_BULK_CONTACTS} at a time.",
                },
                "thread_state": {
                    "type": "string",
                    "enum": list(THREAD_STATES),
                    "description": "Where the thread stands. 'parked' takes them out of the queue.",
                },
                "warmth": {
                    "type": "string",
                    "enum": list(WARMTH),
                    "description": "How warm the relationship is.",
                },
                "note": {"type": "string", "description": "Why, in the student's own words."},
            },
            ["contact_ids"],
        ),
    },
    {
        "name": "update_settings",
        "description": (
            "Change one field of the student's own settings — profile, "
            "target markets, tracks and recruiting cycles, timezone, work "
            "authorization, cadence tuning, weekly pace, email digest. One "
            "field per call. `value` is always a string: a number for "
            "numeric fields, true/false for the digest, a comma-separated "
            "list for regions, tracks and target_cycles (which REPLACE the "
            "whole list — target_cycles values are the exact labels "
            "'<year> Summer Internship'/'<year> Full-Time / Graduate'/'<year> "
            "Spring Week / Insight'/'Off-Cycle / Immediate'), and an empty "
            "string to clear a field back to its default. Some fields are "
            "important enough that the first call deliberately changes "
            "nothing and tells you to check with the student first — read "
            "that error and follow it rather than retrying."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "field": {
                    "type": "string",
                    "enum": list(SETTINGS_FIELDS),
                    "description": "Which setting to change.",
                },
                "value": {
                    "type": "string",
                    "description": "The new value as a string. Empty string clears the field.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "True only after you have told the student what an "
                        "important change does and they have said yes in "
                        "their own next message. Leave it out otherwise."
                    ),
                },
            },
            ["field", "value"],
        ),
    },
]

# Which tools write. Used for instrumentation and for the caps the loop
# enforces — nothing about a write is decided by the model.
WRITE_TOOLS = frozenset({
    "log_touch",
    "track_opportunity",
    "remember",
    "add_calendar_event",
    "add_contact",
    "set_contact_status",
    "update_settings",
})

# Same reasoning as every other row cap in this module (DEFAULT_ROWS/
# MAX_ROWS below): a handful of durable facts is the realistic case, and a
# cap this generous only ever binds if something has gone wrong — at which
# point the tool's own error tells the model to ask the student what to
# drop, rather than silently evicting the oldest one it never asked about.
MAX_MEMORIES = 20


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def _get_today_queue(user, _args) -> dict:
    """The cadence queue, unchanged. `crm.today._build_actions` already ranks,
    labels, and explains every row; re-deriving any of that here would let the
    advisor and the Today page disagree about the same student's week, which
    is worse than either being wrong on its own."""
    actions, contacts = _build_actions(user)
    rows = []
    for a in actions[:MAX_ROWS]:
        c = a["contact"]
        rows.append(
            {
                "action": a["action"],
                "action_label": ACTION_LABELS.get(a["action"], a["action"]),
                "contact_id": c["id"],
                "contact": _s(c.get("name"), 120),
                "firm": _s(c.get("firm_text") or "", 120),
                "warmth": c.get("warmth"),
                "thread_state": c.get("thread_state"),
                "reason": _s(a.get("reason")),
                "days_idle": (
                    None if a.get("last_business_days") is None else a["last_business_days"]
                ),
                "last_interaction": a.get("last_kind"),
                "firm_deadline": _iso(a.get("closes_on")),
            }
        )
    # `_build_actions` hands back firm_text, not the directory name, for rows
    # whose contact is linked to a Firm — fill those in so the model never
    # sees a blank employer for someone it can clearly name.
    firm_ids = {c.firm_id for c in contacts if c.firm_id}
    names = dict(Firm.objects.filter(id__in=firm_ids).values_list("id", "name"))
    by_id = {c.id: c for c in contacts}
    for row in rows:
        contact = by_id.get(row["contact_id"])
        if contact is not None and contact.firm_id:
            row["firm"] = _s(names.get(contact.firm_id, ""), 120)
    return {"today": _today(user).isoformat(), "total": len(actions), "queue": rows}


def _search_contacts(user, args) -> dict:
    from django.db.models import Q

    query = _s(args.get("query"), 120)
    if not query:
        raise ToolError("query is required")
    limit = _limit(args.get("limit"))

    qs = (
        Contact.objects.for_user(user)
        .filter(archived=False)
        .filter(
            Q(name__icontains=query)
            | Q(firm_text__icontains=query)
            | Q(role__icontains=query)
            | Q(firm__name__icontains=query)
        )
        .select_related("firm")
        .order_by("name")
    )
    total = qs.count()
    contacts = list(qs[:limit])

    last_touch = {}
    for t in Touch.objects.for_user(user).filter(contact_id__in=[c.id for c in contacts]):
        prev = last_touch.get(t.contact_id)
        if prev is None or t.ts > prev:
            last_touch[t.contact_id] = t.ts

    now = timezone.now()
    rows = [
        {
            "contact_id": c.id,
            "name": _s(c.name, 120),
            "firm": _firm_name(c),
            "role": _s(c.role, 120),
            "warmth": c.warmth,
            "thread_state": c.thread_state,
            "region": c.region or "unknown",
            "last_touch_days": _days_since(last_touch.get(c.id), now=now),
        }
        for c in contacts
    ]
    result = {"query": query, "total_matches": total, "shown": len(rows), "contacts": rows}
    if total > 1:
        # See the module docstring: the disambiguation rule lives in the
        # payload, not only in the system prompt.
        result["ambiguous"] = True
        result["instruction"] = (
            "More than one contact matches. Ask the student which person they "
            "mean before logging anything against one of them. Do not choose."
        )
    return result


def _get_contact(user, args) -> dict:
    contact = (
        Contact.objects.for_user(user)
        .select_related("firm")
        .filter(pk=args.get("contact_id"))
        .first()
    )
    if contact is None:
        raise ToolError("No contact with that id in this student's network.")

    tier = None
    if contact.firm_id:
        uf = UserFirm.objects.for_user(user).filter(firm_id=contact.firm_id).first()
        tier = uf.tier if uf else None

    touches = list(Touch.objects.for_user(user).filter(contact_id=contact.id)[:8])
    history = [
        {
            "date": _iso(timezone.localtime(t.ts).date()),
            "kind": _KIND_LABELS.get(t.kind, t.kind),
            "channel": _CHANNEL_LABELS.get(t.channel or "", t.channel or ""),
            "note": _s(_display_note(t.note)),
            "source": t.source,
        }
        for t in touches
    ]
    return {
        "contact_id": contact.id,
        "name": _s(contact.name, 120),
        "firm": _firm_name(contact),
        "firm_tier": tier,
        "role": _s(contact.role, 120),
        "region": contact.region or "unknown",
        "warmth": contact.warmth,
        "thread_state": contact.thread_state,
        "school_affiliation": contact.school_affiliation,
        "has_email": bool(contact.email),
        "student_note_about_them": _s(contact.angle),
        # AI-WRITTEN, and named so it can never be read as the student's own
        # words. `student_note_about_them` above is theirs (Contact.angle);
        # this is crm.ai_summary's generated recap of the same relationship,
        # kept a separate key rather than merged into that one precisely so
        # the model — and anything reading this JSON later — can always tell
        # which of the two a sentence came from. Read-only here: nothing in
        # this tool generates or writes it.
        "advisor_summary": _s(contact.ai_summary),
        "advisor_summary_written": _iso(contact.ai_summary_generated_at),
        "recent_interactions": history,
    }


def _search_opportunities(user, args) -> dict:
    from django.db.models import Q

    today = _today(user)
    limit = _limit(args.get("limit"))

    # The same scope the Opportunities feed shows: open, campus buckets only.
    qs = (
        Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
        .select_related("firm")
    )

    # Roles this student has already said are not for them stay out, exactly
    # as they do on their own board.
    dismissed = list(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=True)
        .values_list("opportunity_id", flat=True)
    )
    if dismissed:
        qs = qs.exclude(id__in=dismissed)

    region = (args.get("region") or "").strip().lower()
    if region:
        qs = _apply_region_filter(qs, region)

    query = _s(args.get("query"), 120)
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(firm__name__icontains=query)
            | Q(location__icontains=query)
        )

    firm = _s(args.get("firm"), 120)
    if firm:
        qs = qs.filter(Q(firm__name__icontains=firm) | Q(firm__slug__iexact=firm))

    within = args.get("closing_within_days")
    if isinstance(within, int) and within > 0:
        qs = qs.filter(deadline__isnull=False, deadline__gte=today, deadline__lte=today + timedelta(days=within))

    total = qs.count()
    # Stated deadlines first and soonest first, silent postings after them —
    # the feed's own ordering. Postgres sorts NULLs last on ASC, but the split
    # below says so explicitly rather than relying on the backend's default.
    rows = list(qs.order_by("deadline", "-first_seen")[:limit])
    dated = [o for o in rows if o.deadline]
    undated = [o for o in rows if not o.deadline]

    def _row(o):
        return {
            "opportunity_id": o.id,
            "title": _s(o.title, 160),
            "firm": _s(o.firm.name, 120),
            "location": _s(o.location, 120),
            "region": o.region or "unstated",
            "deadline": _iso(o.deadline),
            "days_left": (o.deadline - today).days if o.deadline else None,
            "url": _s(o.url, 300),
        }

    return {
        "today": today.isoformat(),
        "total_matches": total,
        "shown": len(rows),
        "roles": [_row(o) for o in dated + undated],
    }


def _get_firm(user, args) -> dict:
    from django.db.models import Q

    needle = _s(args.get("name_or_slug"), 120)
    if not needle:
        raise ToolError("name_or_slug is required")
    firm = (
        Firm.objects.filter(Q(slug__iexact=needle) | Q(name__icontains=needle))
        .order_by("name")
        .first()
    )
    if firm is None:
        raise ToolError(f"No firm on Coverage's board matches {needle!r}.")

    today = _today(user)
    uf = UserFirm.objects.for_user(user).filter(firm_id=firm.id).first()

    dates = [
        {
            "event": fd.event_kind,
            "cycle": fd.cycle,
            "region": fd.region or "unstated",
            "date": _iso(fd.date),
            "confidence": round(fd.confidence or 0.0, 2),
        }
        for fd in FirmDate.objects.filter(firm_id=firm.id, date__gte=today).order_by("date")[:MAX_ROWS]
    ]

    contacts = list(
        Contact.objects.for_user(user)
        .filter(archived=False, firm_id=firm.id)
        .order_by("name")[:MAX_ROWS]
    )
    warmth_rank = {"advocate": 0, "chatted": 1, "replied": 2, "cold": 3}
    contacts.sort(key=lambda c: (warmth_rank.get(c.warmth, 9), c.name))

    return {
        "firm": _s(firm.name, 120),
        "slug": firm.slug,
        "tracks": [_s(t, 40) for t in (firm.tracks or [])][:10],
        "regions": [_s(r, 40) for r in (firm.regions or [])][:10],
        "my_tier": uf.tier if uf else None,
        "is_target_firm": uf is not None,
        "open_roles": Opportunity.objects.filter(
            firm_id=firm.id, status="open", bucket__in=TARGET_BUCKETS
        ).count(),
        "upcoming_dates": dates,
        "my_contacts": [
            {
                "contact_id": c.id,
                "name": _s(c.name, 120),
                "role": _s(c.role, 120),
                "warmth": c.warmth,
                "thread_state": c.thread_state,
            }
            for c in contacts
        ],
    }


def _get_my_firms(user, _args) -> dict:
    """The portfolio view `get_firm` can't give: every tiered target at
    once, cheap enough to read in full because it's counts, not rows. A
    student asking "how am I doing at my top firms" previously forced the
    model to call get_firm once per firm it happened to guess at — this
    answers it in one call, for firms it would otherwise never think to ask
    about at all."""
    rows = list(
        UserFirm.objects.for_user(user)
        .filter(tier__isnull=False)
        .select_related("firm")
        .order_by("tier", "firm__name")[: MAX_ROWS * 3]
    )
    if not rows:
        return {"firms": [], "note": "No target firms tiered yet — set tiers on the Network page."}

    firm_ids = [r.firm_id for r in rows]
    contacts_by_firm: dict[int, list[Contact]] = {}
    for c in Contact.objects.for_user(user).filter(archived=False, firm_id__in=firm_ids):
        contacts_by_firm.setdefault(c.firm_id, []).append(c)

    open_counts: dict[int, int] = {}
    for o in Opportunity.objects.filter(
        firm_id__in=firm_ids, status="open", bucket__in=TARGET_BUCKETS
    ).values_list("firm_id", flat=True):
        open_counts[o] = open_counts.get(o, 0) + 1

    warmth_rank = {"advocate": 0, "chatted": 1, "replied": 2, "cold": 3}
    firms = []
    for r in rows[: MAX_ROWS * 2]:
        contacts = sorted(contacts_by_firm.get(r.firm_id, []), key=lambda c: warmth_rank.get(c.warmth, 9))
        firms.append(
            {
                "firm": _s(r.firm.name, 120),
                "slug": r.firm.slug,
                "tier": r.tier,
                "contact_count": len(contacts),
                "warmest_contact": contacts[0].warmth if contacts else None,
                "open_roles": open_counts.get(r.firm_id, 0),
            }
        )
    return {"firms": firms}


def _get_calendar(user, args) -> dict:
    days = args.get("days_ahead")
    days = days if isinstance(days, int) and 1 <= days <= 180 else 14
    now = timezone.now()
    events = list(
        CalendarEvent.objects.for_user(user)
        .filter(starts_at__gte=now, starts_at__lte=now + timedelta(days=days))
        .select_related("contact")
        .order_by("starts_at")[:MAX_ROWS]
    )
    return {
        "window_days": days,
        "events": [
            {
                "title": _s(e.title, 160),
                "starts_at": timezone.localtime(e.starts_at).isoformat(),
                "all_day": e.all_day,
                "kind": e.kind,
                "with_contact": _s(e.contact.name, 120) if e.contact_id else None,
                "contact_id": e.contact_id,
                "location": _s(e.location, 120),
            }
            for e in events
        ],
    }


def _get_my_pipeline(user, _args) -> dict:
    today = _today(user)
    rows = list(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=False)
        .select_related("opportunity", "opportunity__firm")
        .order_by("opportunity__deadline")[:MAX_ROWS * 2]
    )
    by_status: dict[str, list[dict]] = {}
    for uo in rows:
        o = uo.opportunity
        status = uo.applied_status or "saved"
        by_status.setdefault(status, []).append(
            {
                "opportunity_id": o.id,
                "title": _s(o.title, 160),
                "firm": _s(o.firm.name, 120),
                "deadline": _iso(o.deadline),
                "days_left": (o.deadline - today).days if o.deadline else None,
                "still_open": o.status == "open",
            }
        )
    return {"today": today.isoformat(), "total": len(rows), "by_status": by_status}


def _get_situation(user, _args) -> dict:
    """assistant.situation.build_situation, trimmed for the model: the same
    three event kinds the Today page's own cards render from (deadline_moved,
    role_closed, new_role_at_known_firm), untruncated in count (the module
    already caps each type) but with every string run through `_s()` like
    every other tool result here."""
    situation = build_situation(user)
    events = []
    for e in situation.get("events", []):
        row = {
            "kind": e.get("kind"),
            "opportunity_id": e.get("opportunity_id"),
            "title": _s(e.get("title"), 160),
            "firm": _s(e.get("firm"), 120),
            "url": _s(e.get("url"), 300),
        }
        if row["kind"] == "deadline_moved":
            row["old_deadline"] = e.get("old_value") or None
            row["new_deadline"] = e.get("new_value") or None
        elif row["kind"] == "new_role_at_known_firm":
            row["location"] = _s(e.get("location"), 120)
        events.append(row)
    return {"total": len(events), "events": events}


# ---------------------------------------------------------------------------
# Write tools — both cheap, both reversible, both auto-applied.
# ---------------------------------------------------------------------------
def _log_touch(user, args, *, message_id: str) -> dict:
    """One interaction, through `crm.services.log_touch` — the single write
    path for a touch, which runs the reviewed ratchet in
    `coverage_domain.pipeline`. Validated exactly as `crm.views.log_touch`
    validates the button: kind in TOUCH_TRANSITIONS, channel in CHANNELS.

    `source="assistant"` and the `[assistant:<id>]` note prefix are both
    permanent: a student looking at their own history six weeks from now can
    tell which touches a model logged for them, and `crm.views._display_note`
    strips the marker so the prefix never becomes something they have to read.
    """
    contact = Contact.objects.for_user(user).filter(pk=args.get("contact_id")).first()
    if contact is None:
        raise ToolError("No contact with that id in this student's network.")

    kind = (args.get("kind") or "").strip()
    channel = (args.get("channel") or "").strip()
    if kind not in TOUCH_TRANSITIONS:
        raise ToolError(f"Unknown interaction kind {kind!r}.")
    if channel not in CHANNELS:
        raise ToolError(f"Unknown channel {channel!r}.")

    note = _s(args.get("note"), MAX_STR)
    marked = f"[assistant:{message_id}] " + note if note else f"[assistant:{message_id}]"

    before = (contact.warmth, contact.thread_state)
    services.log_touch(user.id, contact.id, kind, channel, marked, source="assistant")
    contact.refresh_from_db()

    return {
        "logged": True,
        "contact_id": contact.id,
        "contact": _s(contact.name, 120),
        "kind": _KIND_LABELS.get(kind, kind),
        "channel": _CHANNEL_LABELS.get(channel, channel),
        "warmth_before": before[0],
        "warmth_after": contact.warmth,
        "thread_state_before": before[1],
        "thread_state_after": contact.thread_state,
        "undo": "The student can correct this on the contact's page in Coverage.",
    }


def _track_opportunity(user, args) -> dict:
    """Save or clear one role. Deliberately does NOT upsert through the
    unscoped manager the way `directory.views.track_opportunity` does — that
    manager's name is the app-wide marker for "this query is not
    tenant-scoped", and it must never appear in this package (see the module
    docstring and test_isolation.py). A scoped lookup plus a plain model
    constructor is the same upsert with the isolation guarantee intact."""
    status = (args.get("status") or "").strip().lower()
    if status not in ("saved", "clear"):
        raise ToolError("status must be 'saved' or 'clear'.")

    opp = Opportunity.objects.select_related("firm").filter(pk=args.get("opportunity_id")).first()
    if opp is None:
        raise ToolError("No open role on Coverage's board has that id.")

    existing = UserOpportunity.objects.for_user(user).filter(opportunity_id=opp.id).first()

    if status == "clear":
        if existing is not None:
            existing.delete()
        return {
            "cleared": True,
            "opportunity_id": opp.id,
            "title": _s(opp.title, 160),
            "firm": _s(opp.firm.name, 120),
        }

    # Already past "saved" — submitted / interview / offer / closed. Saving on
    # top of that would blank the funnel state the student set themselves, so
    # the write is refused and the model is told why. This is the same guard
    # `templates/directory/_track_control.html` makes structurally, by
    # rendering a read-only chip instead of a Save button once a role is in
    # the funnel "so a stray feed click can't un-apply someone" — a stray tool
    # call is the same mistake arriving through a different door.
    if existing is not None and existing.applied_status:
        stage = existing.applied_status
        return {
            "saved": False,
            "already_tracked": True,
            "opportunity_id": opp.id,
            "title": _s(opp.title, 160),
            "firm": _s(opp.firm.name, 120),
            "current_status": stage,
            "current_status_label": _STAGE_LABELS.get(stage, stage),
            "instruction": (
                "This role is already further along than saved, so nothing was "
                "changed. Tell the student where it stands rather than saying "
                "you saved it. Funnel stages are theirs to move, on the My "
                "Applications page."
            ),
        }

    if existing is None:
        try:
            existing = UserOpportunity(user=user, opportunity=opp)
            existing.save()
        except IntegrityError:
            # Two saves of the same role racing each other; the constraint did
            # its job, so just adopt the row that won.
            existing = UserOpportunity.objects.for_user(user).filter(opportunity_id=opp.id).first()
            if existing is None:
                raise ToolError("Could not save that role.")
    existing.applied_status = ""
    existing.dismissed = False
    existing.save(update_fields=["applied_status", "dismissed"])
    return {
        "saved": True,
        "opportunity_id": opp.id,
        "title": _s(opp.title, 160),
        "firm": _s(opp.firm.name, 120),
        "deadline": _iso(opp.deadline),
        "undo": "The student can unsave this from the Opportunities page.",
    }


def _remember(user, args) -> dict:
    """Store one durable fact — read back into every future conversation's
    preamble (agent.build_preamble), not returned as a tool result the
    student would have to notice. See AdvisorMemory's own docstring for
    why this is a safe, uncapped-confirmation write like log_touch."""
    text = _s(args.get("fact"), 200)
    if not text:
        raise ToolError("fact is required")
    if AdvisorMemory.objects.for_user(user).count() >= MAX_MEMORIES:
        raise ToolError(
            f"Already remembering {MAX_MEMORIES} things, the most this page keeps. "
            "Ask the student which one to forget on the Talk page, or just answer "
            "this one without remembering it."
        )
    AdvisorMemory(user=user, text=text).save()
    return {"remembered": text}


def _parse_clock(raw, field: str) -> dt_time | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return dt_time.fromisoformat(raw)
    except ValueError:
        raise ToolError(f"{field} must be HH:MM (24-hour).")


def _add_calendar_event(user, args) -> dict:
    """Add one row to `CalendarEvent`, the same 'day plus an optional clock
    time' shape the student's own quick-add form uses
    (`crm.forms.CalendarEventForm`) — a blank time means a genuine all-day
    entry, never an invented one. An explicit end time is the one thing this
    can do that the hand form still cannot (it has no end-time field at
    all); everything else mirrors the form's own validation so a
    model-added event and a hand-typed one behave identically."""
    title = _s(args.get("title"), 255)
    if not title:
        raise ToolError("title is required.")

    raw_date = (args.get("date") or "").strip()
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        raise ToolError("date must be YYYY-MM-DD.")

    start_time = _parse_clock(args.get("start_time"), "start_time")
    end_time = _parse_clock(args.get("end_time"), "end_time")
    if end_time and not start_time:
        raise ToolError("end_time needs a start_time to be the end OF.")

    # Interpreted in the student's own timezone, same as the hand form's
    # clean() — TimezoneMiddleware has already activated it for this
    # request, so "6pm" means 6pm where they are, not on the server.
    tz = timezone.get_current_timezone()
    starts_at = timezone.make_aware(datetime.combine(day, start_time or dt_time.min), tz)
    ends_at = None
    if end_time:
        ends_at = timezone.make_aware(datetime.combine(day, end_time), tz)
        if ends_at <= starts_at:
            raise ToolError("end_time must be after start_time.")

    kind = (args.get("kind") or CalendarEvent.KIND_EVENT).strip()
    if kind not in dict(CalendarEvent.KIND_CHOICES):
        raise ToolError(f"kind must be one of {sorted(dict(CalendarEvent.KIND_CHOICES))}.")

    contact = None
    contact_id = args.get("contact_id")
    if contact_id:
        contact = Contact.objects.for_user(user).filter(pk=contact_id).first()
        if contact is None:
            raise ToolError("No contact with that id in this student's network.")

    event = CalendarEvent(
        user=user,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=start_time is None,
        kind=kind,
        source=CalendarEvent.SOURCE_MANUAL,
        contact=contact,
        location=_s(args.get("location"), 255),
    )
    event.save()

    return {
        "added": True,
        "id": event.id,
        "title": event.title,
        "starts_at": timezone.localtime(event.starts_at).isoformat(),
        "ends_at": timezone.localtime(event.ends_at).isoformat() if event.ends_at else None,
        "all_day": event.all_day,
        "kind": event.kind,
    }


def _add_contact(user, args) -> dict:
    """Hand-add one contact, the same write `crm.views.contact_new` makes:
    `Contact(...)` with `user` and `source` set, saved through the tenant
    manager's own model, plus the `contact_added` product event that view
    records — so a person the advisor added counts in the activation funnel
    exactly like one typed into the form.

    Validation is `full_clean()` against the model rather than a hand-rolled
    copy of it, so the email and every max_length are the ones
    `crm.forms.ContactForm` enforces.

    ONLY name/firm_text/role/email ARE OFFERED, not the ContactForm's full
    eleven fields. Two reasons, not one: this mirrors the CRM's own
    `?quick=1` fast path on `contact_new` ("ten seconds: who, where, one
    line so you remember them, everything else can wait" — `firm_id`,
    `linkedin`, `school`, `region`, `angle` and `notes` are all hidden
    behind that same page's `{% if not quick %}`). And the schema has a
    hard ceiling: `TOOL_SCHEMAS` combined may carry at most 24 OPTIONAL
    parameters across every tool before Anthropic's grammar compiler
    refuses the whole request outright (measured live: 26 offered, every
    single turn 400'd — the entire advisor was down, not just this tool).
    Four fields is what a name said out loud in one breath actually
    carries; the rest is exactly the friction the quick-add path already
    decided a first capture doesn't need.

    A same-name contact already in the network is refused rather than
    duplicated: "add Jane Chen" said about someone already tracked is the
    common case, and a second Jane Chen splits a history in two — the same
    fork `crm.views`' archive comment describes, arriving through a
    different door. The refusal names the existing id so the model can talk
    about the person the student already has.
    """
    from django.core.exceptions import ValidationError

    name = _s(args.get("name"), 255)
    if not name:
        raise ToolError("name is required.")

    existing = (
        Contact.objects.for_user(user)
        .filter(archived=False, name__iexact=name)
        .first()
    )
    if existing is not None:
        return {
            "added": False,
            "already_exists": True,
            "contact_id": existing.id,
            "name": _s(existing.name, 120),
            "firm": _firm_name(existing),
            "instruction": (
                "Someone with this name is already in the student's network, "
                "so nothing was added. Tell them that and say where the "
                "relationship stands rather than saying you added anyone. If "
                "it is genuinely a different person with the same name, they "
                "can add them on the Network page."
            ),
        }

    contact = Contact(
        user=user,
        name=name,
        firm_text=_s(args.get("firm_text"), 255),
        role=_s(args.get("role"), 255),
        email=_s(args.get("email"), 254),
        source="assistant",
    )
    try:
        contact.full_clean()
    except ValidationError as e:
        # One field at a time, in the model's own words — "Enter a valid email
        # address." is exactly what the student would have seen on the form.
        first = next(iter(e.message_dict.items()))
        raise ToolError(f"{first[0]}: {first[1][0]}") from None
    contact.save()
    record_event("contact_added", user=user, source="assistant")

    return {
        "added": True,
        "contact_id": contact.id,
        "name": contact.name,
        "firm": _firm_name(contact),
        "role": contact.role,
        "region": contact.region or "unknown",
        "undo": "The student can edit or archive this on the contact's page in Coverage.",
    }


def _set_contact_status(user, args, *, message_id: str = "") -> dict:
    """Move warmth and/or thread_state on up to `MAX_BULK_CONTACTS` contacts,
    one `services.set_contact_state` call each.

    A LOOP over the audited override, never a bulk `.update()` — the same
    call and the same reasoning as `crm.today.today_park_all`: that path
    inserts its own `manual_override` touch per contact, so the history has
    no gap, and it is the one thing allowed to move `thread_state` off the
    terminal `advocate` state. A bulk UPDATE would change a dozen
    relationships with nothing on the record saying who did it.

    An id that does not resolve through `.for_user` — another student's, or
    one that never existed — is collected into `not_found` and reported
    back, not silently dropped and not allowed to fail the other 24. The
    model needs that list to answer honestly instead of saying "done".
    """
    raw_ids = args.get("contact_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ToolError("contact_ids must be a non-empty list of contact ids.")
    if len(raw_ids) > MAX_BULK_CONTACTS:
        raise ToolError(
            f"That is {len(raw_ids)} contacts; {MAX_BULK_CONTACTS} is the most "
            "one call may change. Ask the student which ones matter most, or "
            "do it in smaller batches they can actually check."
        )

    thread_state = (args.get("thread_state") or "").strip()
    warmth = (args.get("warmth") or "").strip()
    if not thread_state and not warmth:
        raise ToolError("Give a thread_state, a warmth, or both — otherwise there is nothing to set.")
    if thread_state and thread_state not in THREAD_STATES:
        raise ToolError(f"Unknown thread_state {thread_state!r}.")
    if warmth and warmth not in WARMTH:
        raise ToolError(f"Unknown warmth {warmth!r}.")

    note = _s(args.get("note"), MAX_STR)
    # Same permanent marker as `_log_touch`: the override's own audit touch
    # says which assistant message moved this contact, and
    # `crm.views._display_note` strips the marker so the student never reads it.
    marked = f"[assistant:{message_id}] " + note if note else f"[assistant:{message_id}]"

    # Ordered, de-duplicated: "park 12, 12, 9" is one park each, and the
    # result reports the ids in the order the model asked for them.
    wanted: list[int] = []
    for raw in raw_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            raise ToolError(f"{raw!r} is not a contact id.")
        if cid not in wanted:
            wanted.append(cid)

    owned = {
        c.id: c
        for c in Contact.objects.for_user(user).filter(pk__in=wanted).select_related("firm")
    }

    changed, not_found = [], []
    for cid in wanted:
        contact = owned.get(cid)
        if contact is None:
            not_found.append(cid)
            continue
        before = (contact.warmth, contact.thread_state)
        services.set_contact_state(
            user.id,
            contact.id,
            warmth=warmth or None,
            thread_state=thread_state or None,
            note=marked,
        )
        contact.refresh_from_db()
        changed.append(
            {
                "contact_id": contact.id,
                "name": _s(contact.name, 120),
                "firm": _firm_name(contact),
                "warmth_before": before[0],
                "warmth_after": contact.warmth,
                "thread_state_before": before[1],
                "thread_state_after": contact.thread_state,
            }
        )

    result = {
        "changed_count": len(changed),
        "changed": changed,
        "not_found": not_found,
        "undo": "The student can set any of these back on the contact's own page.",
    }
    if not_found:
        result["instruction"] = (
            "Some of those ids are not in this student's network, so nothing "
            "was changed for them. Say how many you actually moved and do not "
            "claim the rest."
        )
    return result


# ---------------------------------------------------------------------------
# update_settings
# ---------------------------------------------------------------------------
def _blank(raw) -> bool:
    return not str(raw or "").strip()


def _int_setting(raw, field: str, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a whole number between {low} and {high}.") from None
    if not low <= value <= high:
        raise ToolError(f"{field} must be between {low} and {high}.")
    return value


def _csv_setting(raw, field: str, allowed: frozenset[str], *, lower: bool = True) -> list[str]:
    """Comma-separated list -> deduped, validated tokens.

    `lower=False` for `target_cycles`: unlike regions/tracks' short lowercase
    codes ("hk", "ib"), a cycle value IS its own display label
    ("2028 Summer Internship") — lowercasing it would validate fine against a
    lowercased `allowed` set but store a value nothing else in the product
    (the settings page, `cycle_choices()`, the scorer's `parse_target_cycle`)
    would ever recognise back."""
    tokens, seen = [], set()
    for part in str(raw or "").split(","):
        token = part.strip()
        token = token.lower() if lower else token
        if not token or token in seen:
            continue
        if token not in allowed:
            raise ToolError(f"{token!r} is not a {field} this product tracks. Pick from {sorted(allowed)}.")
        seen.add(token)
        tokens.append(token)
    return tokens


_TRUTHY = {"true", "yes", "on", "1"}
_FALSY = {"false", "no", "off", "0"}


def _bool_setting(raw, field: str) -> bool:
    token = str(raw or "").strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    raise ToolError(f"{field} must be true or false.")


def _setting_display(user, field: str) -> str:
    """One field's current value, as a plain string — used for the before/after
    the model reports back, so a student can see what it actually moved."""
    if field in TUNABLE_CADENCE_PARAMS:
        return str((user.cadence_params or {}).get(field, "") or "")
    if field == "advocate_target":
        return str((user.assets or {}).get("advocate_target", "") or "")
    if field.startswith(_WORK_AUTH_PREFIX):
        code = field[len(_WORK_AUTH_PREFIX):]
        return str((user.work_authorization or {}).get(code, "") or "")
    if field == "weekly_digest_enabled":
        return "false" if user.weekly_digest_opt_out else "true"
    if field in ("regions", "tracks", "target_cycles"):
        return ", ".join(getattr(user, field, None) or [])
    if field == "timezone":
        return "auto" if user.timezone_auto else (user.timezone or "")
    return str(getattr(user, field, "") or "")


def _apply_setting(user, field: str, raw) -> None:
    """Write one field. Each branch mirrors the `apply_to` of the form that
    owns that field on /welcome/settings/ — including the clearing rules,
    which are not incidental: a blank cadence input REMOVES the override so
    the product default applies, rather than storing a zero the engine then
    ignores (see `accounts.forms.CadenceForm`'s own docstring)."""
    if field in ("name", "school"):
        setattr(user, field, _s(raw, 255))
        user.save(update_fields=[field])

    elif field == "class_year":
        if _blank(raw):
            user.class_year = None
        else:
            year = _int_setting(raw, "class_year", min(_CLASS_YEARS), max(_CLASS_YEARS))
            if year not in _CLASS_YEARS:
                raise ToolError(f"class_year must be one of {sorted(_CLASS_YEARS)}.")
            user.class_year = year
        user.save(update_fields=["class_year"])

    elif field == "target_cycles":
        # Computed per call, not cached at module level: `cycle_choices()`
        # anchors to the current year, and this tool can live in a
        # long-running worker process the same as `ProfileForm.__init__`.
        known = frozenset(code for code, _ in cycle_choices() if code)
        user.target_cycles = _csv_setting(raw, "target cycle", known, lower=False)
        user.save(update_fields=["target_cycles"])

    elif field == "regions":
        user.regions = _csv_setting(raw, "region", _PROFILE_REGIONS)
        user.save(update_fields=["regions"])

    elif field == "tracks":
        user.tracks = _csv_setting(raw, "track", _PROFILE_TRACKS)
        user.save(update_fields=["tracks"])

    elif field == "timezone":
        value = str(raw or "").strip()
        if value.lower() in ("auto", AUTO_TIMEZONE):
            # Same contract as ProfileForm.apply_to: auto turns following back
            # on and leaves the stored zone alone, so the student keeps a
            # correct day until the browser next reports one.
            user.timezone_auto = True
        else:
            if value and value not in known_timezones():
                raise ToolError(
                    "That isn't a timezone this system knows. Give an IANA name "
                    "like 'Asia/Hong_Kong', or 'auto' to follow their device."
                )
            user.timezone_auto = False
            user.timezone = value
        user.save(update_fields=["timezone", "timezone_auto"])

    elif field == "weekly_touch_goal":
        user.weekly_touch_goal = (
            None if _blank(raw)
            else _int_setting(raw, "weekly_touch_goal", 1, WeeklyPaceForm.MAX_GOAL)
        )
        user.save(update_fields=["weekly_touch_goal"])

    elif field == "weekly_digest_enabled":
        # The column is named the OPPOSITE of the setting, on purpose — see
        # NotificationsForm's docstring. This is the same one translation.
        user.weekly_digest_opt_out = not _bool_setting(raw, "weekly_digest_enabled")
        user.save(update_fields=["weekly_digest_opt_out"])

    elif field == "advocate_target":
        low, high = CadenceForm.ADVOCATE_TARGET_RANGE
        assets = dict(user.assets or {})
        if _blank(raw):
            assets.pop("advocate_target", None)
        else:
            assets["advocate_target"] = _int_setting(raw, "advocate_target", low, high)
        user.assets = assets
        user.save(update_fields=["assets"])

    elif field in TUNABLE_CADENCE_PARAMS:
        low, high = TUNABLE_CADENCE_PARAMS[field]
        params = dict(user.cadence_params or {})
        if _blank(raw):
            params.pop(field, None)
        else:
            params[field] = _int_setting(raw, field, low, high)
        user.cadence_params = params
        user.save(update_fields=["cadence_params"])

    elif field.startswith(_WORK_AUTH_PREFIX):
        code = field[len(_WORK_AUTH_PREFIX):]
        value = str(raw or "").strip().lower()
        if value and value not in _WORK_AUTH_VALUES:
            raise ToolError(f"{field} must be one of {sorted(_WORK_AUTH_VALUES)}, or empty to clear it.")
        auth = dict(user.work_authorization or {})
        if value:
            auth[code] = value
        else:
            auth.pop(code, None)
        user.work_authorization = auth
        user.save(update_fields=["work_authorization"])

    else:  # pragma: no cover — the allowlist check above already caught this
        raise ToolError(f"{field} is not a setting the advisor can change.")


def _update_settings(user, args) -> dict:
    """Change one settings field.

    Two tiers, and the split is the whole point (see the module docstring).
    An ordinary field applies on the first call like every other write in
    this file. An important one does not: without `confirmed=true` this
    writes NOTHING and raises, and the error is the instruction — say what
    the change actually does, wait for the student's own yes, then call
    again. One tool rather than a propose/confirm pair because a `confirm`
    tool would have to re-take the field and the value anyway (nothing here
    stores a pending proposal), which makes it a second copy of this
    function's allowlist and validation under a different name — and two
    copies of an allowlist is how one of them drifts.
    """
    field = (args.get("field") or "").strip()
    if field not in SETTINGS_FIELDS:
        raise ToolError(
            f"{field!r} is not a setting the advisor can change. Changeable "
            f"fields: {', '.join(SETTINGS_FIELDS)}. Anything else — their "
            "email, their password, their profile picture — is theirs to "
            "change on the Settings page."
        )

    raw = args.get("value")
    if raw is None:
        raise ToolError("value is required. Use an empty string to clear a field.")

    if field in SETTINGS_IMPORTANT and not args.get("confirmed"):
        raise ToolError(
            f"NOT CHANGED, and this is not a failure. {field} is important: "
            f"{_IMPORTANT_EFFECTS[field]}. Do not call this tool again yet. "
            "In your own next reply, tell the student in plain words what "
            f"this change does and what it moves from ({_setting_display(user, field) or 'not set'}) "
            f"to ({str(raw).strip() or 'cleared'}), and ask them to confirm. "
            "Only if they say yes in their own next message, call "
            "update_settings again with the same field and value plus "
            "confirmed=true."
        )

    before = _setting_display(user, field)
    _apply_setting(user, field, raw)
    user.refresh_from_db()
    after = _setting_display(user, field)

    return {
        "updated": True,
        "field": field,
        "value_before": before,
        "value_after": after,
        "confirmed": bool(args.get("confirmed")),
        "undo": "The student can change this back on the Settings page.",
    }


_HANDLERS = {
    "get_today_queue": _get_today_queue,
    "search_contacts": _search_contacts,
    "get_contact": _get_contact,
    "search_opportunities": _search_opportunities,
    "get_firm": _get_firm,
    "get_my_firms": _get_my_firms,
    "get_calendar": _get_calendar,
    "get_my_pipeline": _get_my_pipeline,
    "get_situation": _get_situation,
    "track_opportunity": _track_opportunity,
    "remember": _remember,
    "add_calendar_event": _add_calendar_event,
    "add_contact": _add_contact,
    "update_settings": _update_settings,
}

# The two writes that stamp the assistant message id into the row they leave
# behind, so a student reading their own history six weeks from now can tell
# which touches a model wrote for them. Dispatched separately only because
# they take an argument the model never supplies.
_MESSAGE_ID_HANDLERS = {
    "log_touch": _log_touch,
    "set_contact_status": _set_contact_status,
}


def execute(user, name: str, tool_input: dict | None, message_id: str = "") -> tuple[str, bool]:
    """Run one tool for `user` and return `(json_text, is_error)`.

    `user` comes from the view's `request.user` closure — it is NEVER read
    off `tool_input`, and no schema in `TOOL_SCHEMAS` has a field for it.

    Never raises: an unknown tool, a bad argument, a row belonging to someone
    else, or an unexpected exception all come back as an `is_error` result the
    model can read and respond to. A tool that can 500 the request would make
    one malformed argument cost the student their whole message.
    """
    args = tool_input if isinstance(tool_input, dict) else {}
    try:
        stamped = _MESSAGE_ID_HANDLERS.get(name)
        if stamped is not None:
            payload = stamped(user, args, message_id=message_id)
        else:
            handler = _HANDLERS.get(name)
            if handler is None:
                raise ToolError(f"Unknown tool {name!r}.")
            payload = handler(user, args)
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except Exception as e:  # noqa: BLE001 — one bad tool call must not cost the turn
        return json.dumps({"error": f"That lookup failed: {type(e).__name__}."}), True
    return json.dumps(payload, default=str), False
