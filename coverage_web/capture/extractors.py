"""Deterministic signal extractors for the capture pipeline (docs/build-plan.md
§5).

This module is the "resist parsing cleverness" line the build plan draws in bold
(§10, Risk 3): every function here is a **pure, deterministic** classifier over
email headers/subject/attachments. There is **no LLM anywhere in this file**, by
design. When the deterministic rules cannot confidently classify a message
(auto-replies, an unparseable sender, a scheduling-shaped body we can't pin to a
tense), the pipeline routes the event to ``status='needs_review'`` — a one-click
human confirmation queue — instead of guessing. That residue path is where an
LLM classifier *would* live in a later version (§5's "residue → LLM"); v1 ships
it as a human queue and nothing else.

The public entry point is :func:`classify`, which takes a
``providers.ParsedInbound`` plus the recipient user's own email and returns a
:class:`Classification` — a typed bundle of ``direction``, a ``signals`` dict
(the versioned, typed payload §5 requires; "never raw text into scoring"), an
optional ``touch_kind`` to apply, and a ``needs_review`` flag with a reason.

Signal vocabulary (matches §5's ``InteractionEvent.signals``):
``outreach_sent``, ``replied``, ``bounced``, ``chat_scheduled`` (+
``chat_scheduled_at``), ``chat_completed``, ``evidence_quote``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from capture.providers import ParsedInbound

# Bumped whenever the deterministic rules below change in a way that could make
# the same raw email classify differently. Stored on every CaptureEvent
# (``extraction_version``) so a re-run/backfill is auditable and events carry
# the ruleset that produced them.
EXTRACTION_VERSION = "det-v1"

# Touch kinds this module may emit, all drawn from
# coverage_domain.pipeline.TOUCH_TRANSITIONS (the ported ratchet). We never
# invent a kind the state machine doesn't know.
KIND_OUTREACH = "outreach"
KIND_REPLY = "reply_received"
KIND_CHAT_SCHEDULED = "chat_scheduled"
KIND_CHAT = "chat"


@dataclass
class Classification:
    """The deterministic verdict for one inbound email."""

    direction: str  # "outbound" | "inbound"
    signals: dict = field(default_factory=dict)
    touch_kind: Optional[str] = None  # None => no warmth touch (e.g. a bounce)
    needs_review: bool = False
    review_reason: str = ""


# --------------------------------------------------------------------------- #
# Address helpers
# --------------------------------------------------------------------------- #

def normalize_email(addr: str) -> str:
    """Lower-case + strip. Enough for equality matching; we deliberately do not
    do Gmail dot/plus canonicalisation (a source of false merges)."""
    return (addr or "").strip().lower()


def normalize_name(name: str) -> str:
    """Collapse whitespace + case-fold for name-based contact matching."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


# --------------------------------------------------------------------------- #
# Direction — "outbound if the user is the From/sender, inbound otherwise"
# --------------------------------------------------------------------------- #

def detect_direction(parsed: "ParsedInbound", user_email: str) -> str:
    """§5: direction from headers. Outbound when the recipient user is the
    sender (they BCC'd their own outbound mail); inbound otherwise (they
    forwarded a reply they received). Defaults to inbound when the sender is
    absent/unparseable — the ambiguity is then surfaced separately by
    :func:`_direction_is_uncertain` so the event lands in needs_review rather
    than silently ratcheting a contact.
    """
    if parsed.from_email and normalize_email(parsed.from_email) == normalize_email(user_email):
        return "outbound"
    return "inbound"


def _direction_is_uncertain(parsed: "ParsedInbound") -> bool:
    """True when we could not read a usable sender at all — we then can't trust
    the inbound/outbound call, so the event goes to needs_review."""
    return not parsed.from_email


# --------------------------------------------------------------------------- #
# Bounce detection — delivery-failure patterns / mailer-daemon
# --------------------------------------------------------------------------- #

_BOUNCE_FROM_TOKENS = (
    "mailer-daemon",
    "postmaster",
    "mail delivery subsystem",
    "maildelivery",
)
_BOUNCE_SUBJECT_TOKENS = (
    "delivery status notification",
    "undelivered mail returned",
    "undeliverable",
    "delivery failure",
    "returned mail",
    "failure notice",
    "mail delivery failed",
    "could not be delivered",
    "address not found",
)
_BOUNCE_BODY_TOKENS = (
    "550 5.1.1",
    "recipient address rejected",
    "user unknown",
    "no such user",
    "does not exist",
    "message could not be delivered",
    "wasn't delivered to",
    "was not delivered",
)


def detect_bounce(parsed: "ParsedInbound") -> bool:
    """Deterministic delivery-failure detection. A bounce is a hard signal
    (mailer-daemon sender, a delivery-status report content-type, or a
    failure-notice subject) — never inferred from soft language."""
    from_addr = normalize_email(parsed.from_email)
    if any(tok in from_addr for tok in _BOUNCE_FROM_TOKENS):
        return True

    # RFC 3464 delivery status reports.
    if "report-type=delivery-status" in (parsed.content_type or "").lower():
        return True
    if parsed.header("X-Failed-Recipients"):
        return True
    if any(
        (a.content_type or "").lower().startswith("message/delivery-status")
        for a in parsed.attachments
    ):
        return True

    subject = (parsed.subject or "").lower()
    if any(tok in subject for tok in _BOUNCE_SUBJECT_TOKENS):
        return True

    body = (parsed.text_body or "").lower()
    if any(tok in body for tok in _BOUNCE_BODY_TOKENS):
        return True
    return False


def _bounce_evidence(parsed: "ParsedInbound") -> str:
    subject = (parsed.subject or "").lower()
    for tok in _BOUNCE_SUBJECT_TOKENS:
        if tok in subject:
            return f"subject: {tok}"
    from_addr = normalize_email(parsed.from_email)
    for tok in _BOUNCE_FROM_TOKENS:
        if tok in from_addr:
            return f"sender: {tok}"
    return "delivery-failure pattern"


# --------------------------------------------------------------------------- #
# Auto-reply / out-of-office — a machine reply is NOT a human reply
# --------------------------------------------------------------------------- #

_AUTOREPLY_SUBJECT_TOKENS = (
    "out of office",
    "out of the office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "away from my desk",
    "on vacation",
    "on leave",
)


def detect_autoreply(parsed: "ParsedInbound") -> bool:
    """Detect vacation responders / OOO / automatic replies. These are the
    canonical "ambiguous — don't ratchet, don't drop" case: they look like a
    reply but aren't one, so they go to needs_review for a human to judge."""
    auto_submitted = (parsed.header("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if (parsed.header("X-Autoreply") or "").strip():
        return True
    if (parsed.header("X-Autorespond") or "").strip():
        return True
    precedence = (parsed.header("Precedence") or "").lower()
    if precedence in {"auto_reply", "bulk", "junk"}:
        return True
    subject = (parsed.subject or "").lower()
    return any(tok in subject for tok in _AUTOREPLY_SUBJECT_TOKENS)


# --------------------------------------------------------------------------- #
# Scheduling — .ics calendar attachment (tense from DTSTART), else date/time
# language. Text language can't tell past from future reliably, so text-only
# scheduling defaults to the SAFE forward stage (chat_scheduled), never "chat
# happened".
# --------------------------------------------------------------------------- #

_DTSTART_RE = re.compile(
    r"^DTSTART[^:]*:\s*"
    r"(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})"
    r"(?:T(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})(?P<z>Z)?)?",
    re.MULTILINE,
)

# A clock time like "3pm", "3:30 PM", "15:00". Requires a time-of-day token so
# a bare date mention ("since 2019") doesn't trip scheduling.
_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)
_SCHEDULE_PHRASES = (
    "calendar invite",
    "calendar hold",
    "meeting invite",
    "let's schedule",
    "lets schedule",
    "schedule a call",
    "schedule a chat",
    "set up a call",
    "set up a chat",
    "hop on a call",
    "book a time",
    "put some time",
    "zoom link",
    "google meet",
    "teams meeting",
)


def _find_ics(parsed: "ParsedInbound"):
    for att in parsed.attachments:
        ctype = (att.content_type or "").lower()
        name = (att.name or "").lower()
        if ctype.startswith("text/calendar") or name.endswith(".ics"):
            return att
    return None


def _ics_dtstart(ics_text: str) -> Optional[datetime]:
    match = _DTSTART_RE.search(ics_text or "")
    if not match:
        return None
    g = match.groupdict()
    try:
        dt = datetime(
            int(g["Y"]), int(g["M"]), int(g["D"]),
            int(g["h"] or 0), int(g["m"] or 0), int(g["s"] or 0),
            tzinfo=timezone.utc,  # treat DTSTART as UTC for the past/future test
        )
    except (ValueError, TypeError):
        return None
    return dt


@dataclass
class _Scheduling:
    state: str  # "none" | "scheduled" | "completed"
    at: Optional[datetime] = None
    evidence: str = ""


def detect_scheduling(parsed: "ParsedInbound", reference: datetime) -> _Scheduling:
    """.ics attachment wins (its DTSTART gives a real, deterministic tense);
    otherwise fall back to date/time language, which can only ever imply a
    *future* chat (chat_scheduled), never a completed one."""
    ics = _find_ics(parsed)
    if ics is not None:
        dtstart = _ics_dtstart(ics.decoded_text())
        if dtstart is not None:
            if dtstart <= reference:
                return _Scheduling("completed", dtstart, f"ics DTSTART {dtstart.isoformat()} (past)")
            return _Scheduling("scheduled", dtstart, f"ics DTSTART {dtstart.isoformat()} (future)")
        # An .ics with no readable DTSTART still evidences a meeting artifact.
        return _Scheduling("scheduled", None, f"calendar attachment {ics.name or 'invite.ics'}")

    body = f"{parsed.subject or ''}\n{parsed.text_body or ''}"
    lowered = body.lower()
    for phrase in _SCHEDULE_PHRASES:
        if phrase in lowered:
            return _Scheduling("scheduled", None, f"phrase: {phrase}")
    time_match = _TIME_RE.search(body)
    if time_match:
        return _Scheduling("scheduled", None, f"time: {time_match.group(0).strip()}")
    return _Scheduling("none")


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #

def classify(parsed: "ParsedInbound", user_email: str, *, reference: Optional[datetime] = None) -> Classification:
    """Deterministically classify one parsed inbound email into a
    :class:`Classification`. See module docstring for the philosophy; the
    branch order below is the whole decision tree.
    """
    reference = reference or parsed.occurred_at or datetime.now(timezone.utc)
    signals: dict = {"extraction_version": EXTRACTION_VERSION}

    # 1. Bounce — a hard, unambiguous delivery failure. No warmth touch: a
    #    bounce is not interaction progress. (The legacy system also archived
    #    the bounced address; that contact mutation is intentionally a v1.x
    #    follow-up — see report — since this app only advances warmth via the
    #    ported log_touch and never edits contacts directly.)
    if detect_bounce(parsed):
        signals["bounced"] = True
        signals["evidence_quote"] = _bounce_evidence(parsed)
        return Classification(
            direction="inbound", signals=signals, touch_kind=None,
        )

    direction = detect_direction(parsed, user_email)
    signals["direction"] = direction

    # 2. Sender unreadable => we can't trust direction => human review.
    if _direction_is_uncertain(parsed):
        return Classification(
            direction=direction, signals=signals, touch_kind=None,
            needs_review=True, review_reason="unparseable_sender",
        )

    # 3. Auto-reply / OOO => looks like a reply but isn't one => human review.
    if detect_autoreply(parsed):
        signals["evidence_quote"] = (parsed.subject or "")[:200]
        return Classification(
            direction=direction, signals=signals, touch_kind=None,
            needs_review=True, review_reason="auto_reply",
        )

    sched = detect_scheduling(parsed, reference)
    if sched.at is not None:
        signals["chat_scheduled_at"] = sched.at.isoformat()
    if sched.evidence:
        signals["evidence_quote"] = sched.evidence

    if direction == "outbound":
        signals["outreach_sent"] = True
        # An outbound calendar artifact (an invite the user sent) evidences a
        # real scheduled/held meeting. Outbound *text* that merely proposes a
        # time is still just outreach — we never infer "replied"/"scheduled"
        # from the user talking to themselves.
        if sched.state == "completed":
            signals["chat_completed"] = True
            return Classification(direction, signals, KIND_CHAT)
        if sched.state == "scheduled" and _find_ics(parsed) is not None:
            signals["chat_scheduled"] = True
            return Classification(direction, signals, KIND_CHAT_SCHEDULED)
        return Classification(direction, signals, KIND_OUTREACH)

    # inbound — a real message from the counterparty.
    signals["replied"] = True
    if sched.state == "completed":
        signals["chat_completed"] = True
        return Classification(direction, signals, KIND_CHAT)
    if sched.state == "scheduled":
        signals["chat_scheduled"] = True
        return Classification(direction, signals, KIND_CHAT_SCHEDULED)
    return Classification(direction, signals, KIND_REPLY)
