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
``chat_scheduled_at``), ``chat_completed``, ``evidence_quote``, plus two that
exist only to explain a needs_review verdict: ``forwarded`` and
``bounce_language`` (delivery-failure wording with no MTA signal behind it).

The load-bearing asymmetry, stated once here because three functions below
depend on it: **the only verdict that discards a message is a bounce**, since
it alone yields ``touch_kind=None`` with ``needs_review=False``, which the
pipeline records as ``applied`` and forgets. So every soft, human-writable
signal — delivery-failure wording in a body, a forwarded-message marker —
routes to ``needs_review`` instead. A wrong needs_review costs one click; a
wrong bounce costs the whole message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from capture.providers import ParsedInbound

# Bumped whenever the deterministic rules below change in a way that could make
# the same raw email classify differently. Stored on every CaptureEvent
# (``extraction_version``) so a re-run/backfill is auditable and events carry
# the ruleset that produced them.
#
# det-v2 (2026-07-27): three rule changes that all reclassify real mail —
# body-only delivery-failure wording no longer convicts as a bounce, the
# bare-clock-time scheduling fallback is gone and the quoted thread is
# stripped before the scheduling scan, and forwards route to needs_review.
# Events written under det-v1 keep that stamp, which is the whole point: a
# `chat_scheduled` from last week is legible as a product of the old ruleset
# rather than an inexplicable disagreement with today's.
EXTRACTION_VERSION = "det-v2"

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
# CORROBORATING ONLY — never a bounce verdict on their own. Every phrase here
# is ordinary English that a human writes on purpose: a banker answering "our
# sophomore programme does not exist this year, but happy to chat" trips
# "does not exist"; "your CV wasn't delivered to the team until Monday" trips
# "wasn't delivered to". Because the bounce branch is FIRST in `classify` and
# returns touch_kind=None, a body-only match used to mark the event `applied`
# and stop — no touch, no contact, no trace. A real reply was silently deleted
# on the strength of a phrase. These tokens now only corroborate a structural
# signal (see `_structural_bounce_evidence`) or, alone, route to needs_review.
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


def _structural_bounce_evidence(parsed: "ParsedInbound") -> str:
    """The hard, machine-generated delivery-failure signals, or "" for none.

    "Structural" means the MTA said so in a place a human doesn't type: the
    envelope sender, an RFC 3464 report content-type, the X-Failed-Recipients
    header, or an attached ``message/delivery-status`` part. The subject-token
    list stays here with them — those are whole-subject bounce phrases an MTA
    writes ("Undeliverable:", "Delivery Status Notification (Failure)"), not
    prose fragments a person can stumble into mid-sentence, and Exchange
    bounces are sometimes the only thing that carries one.

    Returns the matched evidence already formatted for `evidence_quote`, so
    the caller never has to re-derive which signal fired.
    """
    from_addr = normalize_email(parsed.from_email)
    for tok in _BOUNCE_FROM_TOKENS:
        if tok in from_addr:
            return f"sender: {tok}"

    # RFC 3464 delivery status reports.
    if "report-type=delivery-status" in (parsed.content_type or "").lower():
        return "content-type: report-type=delivery-status"
    if parsed.header("X-Failed-Recipients"):
        return "header: X-Failed-Recipients"
    if any(
        (a.content_type or "").lower().startswith("message/delivery-status")
        for a in parsed.attachments
    ):
        return "attachment: message/delivery-status"

    subject = (parsed.subject or "").lower()
    for tok in _BOUNCE_SUBJECT_TOKENS:
        if tok in subject:
            return f"subject: {tok}"
    return ""


def bounce_language(parsed: "ParsedInbound") -> str:
    """The first corroborating body token present, or "". Never a verdict on
    its own — see `_BOUNCE_BODY_TOKENS`."""
    body = (parsed.text_body or "").lower()
    for tok in _BOUNCE_BODY_TOKENS:
        if tok in body:
            return tok
    return ""


def detect_bounce(parsed: "ParsedInbound") -> bool:
    """Deterministic delivery-failure detection. A bounce is a hard signal
    (mailer-daemon sender, a delivery-status report content-type, an
    X-Failed-Recipients header, an attached delivery-status part, or a
    failure-notice subject) — never inferred from soft language. The
    module used to promise exactly this one line above code that did the
    opposite; the body-token scan now lives in `bounce_language` and can
    only corroborate, because the cost of a false positive here is a
    deleted human reply."""
    return bool(_structural_bounce_evidence(parsed))


def _bounce_evidence(parsed: "ParsedInbound") -> str:
    """Name the signal(s) that actually fired, so the audit trail says which.

    Previously this re-checked only subject and sender and fell through to a
    generic "delivery-failure pattern" for every other route — which is what
    an X-Failed-Recipients bounce and a DSN-attachment bounce both recorded,
    i.e. the two most authoritative cases were the two least legible ones."""
    structural = _structural_bounce_evidence(parsed)
    token = bounce_language(parsed)
    if structural and token:
        return f"{structural} + body: {token}"
    if structural:
        return structural
    return f"body: {token}" if token else "delivery-failure pattern"


_DSN_FINAL_RECIPIENT_RE = re.compile(r"Final-Recipient:\s*rfc822;\s*(\S+)", re.IGNORECASE)
_HEADER_TO_RE = re.compile(r"^To:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _dsn_final_recipient(text: str) -> str:
    """Pull the failed address out of an RFC 3464 delivery-status report's
    ``Final-Recipient: rfc822; user@example.com`` field."""
    match = _DSN_FINAL_RECIPIENT_RE.search(text or "")
    return match.group(1).strip().rstrip(";,") if match else ""


def _original_to(text: str) -> str:
    """The first address off a ``To:`` header line in an attached original
    message (the bounced ``message/rfc822`` attachment some MTAs include)."""
    match = _HEADER_TO_RE.search(text or "")
    if not match:
        return ""
    addrs = getaddresses([match.group(1)])
    return addrs[0][1].strip() if addrs and addrs[0][1] else ""


def bounced_recipient(parsed: "ParsedInbound") -> str:
    """Which address bounced, for a message :func:`detect_bounce` already
    flagged. A bounce's ``From`` is the sender that matters least — it's
    mailer-daemon/postmaster, not the person whose address failed — so this
    recovers the ADDRESS THAT ACTUALLY BOUNCED instead, in order of how
    authoritative the source is:

      1. ``X-Failed-Recipients`` — most inbound-email providers (incl.
         Postmark) set this directly to the failed address.
      2. The RFC 3464 delivery-status report's ``Final-Recipient`` field,
         if a ``message/delivery-status`` part is attached.
      3. The ``To:`` header of the original bounced message, if the MTA
         attached it (``message/rfc822``).

    Returns "" when none of these are present. Callers must treat that as
    "we know it bounced but not for whom", not as a reason to guess — this
    function never falls back to the mailer-daemon's own address."""
    failed = (parsed.header("X-Failed-Recipients") or "").strip()
    if failed:
        # Some MTAs report a comma-separated list; the first address is the
        # one that headed the report.
        return failed.split(",")[0].strip()

    for att in parsed.attachments:
        if (att.content_type or "").lower().startswith("message/delivery-status"):
            final = _dsn_final_recipient(att.decoded_text())
            if final:
                return final

    for att in parsed.attachments:
        ctype = (att.content_type or "").lower()
        if ctype.startswith("message/rfc822") or ctype.startswith("text/rfc822-headers"):
            to_addr = _original_to(att.decoded_text())
            if to_addr:
                return to_addr

    return ""


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
# Forwards — a message the student RECEIVED and re-sent to capture.
# --------------------------------------------------------------------------- #
# The envelope of a forward describes the STUDENT, not the correspondence
# inside it: From is the student (so `detect_direction` reads "outbound"),
# and there is frequently no non-self recipient at all (so `_counterparty`
# returns ("", "")). Left alone that used to manufacture a contact literally
# named "Unknown contact" and log an `outreach` touch against it — recording
# that the student SENT mail when they in fact RECEIVED one, once per forward.
#
# Reading the real correspondent out of the forwarded block means parsing
# body-embedded headers, which is a feature with its own failure modes (every
# client formats the block differently, and a mis-parse writes a touch onto
# the wrong person). So v1 detects the shape and routes it to needs_review,
# where one click puts it on the right contact. Under-serving a forward costs
# a click; guessing at one corrupts the CRM silently.
_FORWARD_SUBJECT_RE = re.compile(r"(?:^|\s)fwd?\s*:", re.IGNORECASE)
_FORWARD_MARKER_RE = re.compile(r"-{2,}\s*forwarded message\s*-{2,}", re.IGNORECASE)


def detect_forward(parsed: "ParsedInbound") -> str:
    """Evidence that this message is a forward, or "" when it isn't.

    Three shapes, any of which is enough: a ``Fwd:``/``FW:`` subject prefix
    (matched anywhere, so a ``Re: Fwd:`` chain still counts), the
    ``---------- Forwarded message ----------`` separator every major client
    writes, or an attached ``message/rfc822`` part (the "forward as
    attachment" option).
    """
    subject = parsed.subject or ""
    match = _FORWARD_SUBJECT_RE.search(subject)
    if match:
        return f"subject: {match.group(0).strip()}"
    if _FORWARD_MARKER_RE.search(parsed.text_body or ""):
        return "body: forwarded-message separator"
    for att in parsed.attachments:
        if (att.content_type or "").lower().startswith("message/rfc822"):
            return "attachment: message/rfc822"
    return ""


# --------------------------------------------------------------------------- #
# Scheduling — .ics calendar attachment (tense from DTSTART), else an explicit
# scheduling phrase. Text language can't tell past from future reliably, so
# text-only scheduling defaults to the SAFE forward stage (chat_scheduled),
# never "chat happened".
# --------------------------------------------------------------------------- #

_DTSTART_RE = re.compile(
    r"^DTSTART[^:]*:\s*"
    r"(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})"
    r"(?:T(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})(?P<z>Z)?)?",
    re.MULTILINE,
)

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


# Where the quoted thread begins. Gmail writes "On Mon, Jul 20, 2026 at 9:12 AM
# Jane Banker <jane@bank.example> wrote:"; Outlook writes an
# "-----Original Message-----" rule; every client also prefixes the quoted
# lines with ">".
_QUOTE_HEADER_RE = re.compile(
    r"^\s*(?:On\b.*\bwrote:|-{2,}\s*original message\s*-{2,})\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Everything above the quoted thread, with ``>``-quoted lines removed.

    Load-bearing for scheduling, not cosmetic. `text_body` carries the WHOLE
    thread, and every threaded reply on earth opens its quote with a clock
    time — "On Mon, Jul 20, 2026 at 9:12 AM Jane wrote:". Scanning that for
    scheduling language meant the student's own quoted words, and the
    machinery of quoting itself, voted on how to classify the counterparty's
    new sentence.
    """
    if not text:
        return ""
    match = _QUOTE_HEADER_RE.search(text)
    if match:
        text = text[: match.start()]
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
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
    otherwise an EXPLICIT phrase from `_SCHEDULE_PHRASES`, which can only ever
    imply a *future* chat (chat_scheduled), never a completed one. Nothing
    else counts.

    There used to be a third rung: a bare clock-time regex over
    ``subject + text_body``. It fired on essentially every threaded reply,
    because `text_body` includes the quoted thread and every quote header
    carries a time ("… at 9:12 AM … wrote:"). So a plain "happy to help" reply
    classified `chat_scheduled` instead of `reply_received`, which suppressed
    the correct "they replied — propose a chat" nudge and then, days later,
    asked "did the chat happen?" about a chat nobody ever scheduled.

    Both halves of the fix matter and neither is sufficient alone: the quote
    is stripped first (`strip_quoted`) so no scan ever reads the thread's own
    plumbing, and the bare-time rung is gone outright, because "3pm" in a
    live sentence is as often "I'm in a meeting until 3pm" as it is a
    proposal. The trade is deliberate and asymmetric: under-calling to
    `reply_received` costs one cadence stage the user can advance by hand,
    while over-calling fabricates a meeting and then prompts a thank-you for
    a conversation that never happened.
    """
    ics = _find_ics(parsed)
    if ics is not None:
        dtstart = _ics_dtstart(ics.decoded_text())
        if dtstart is not None:
            if dtstart <= reference:
                return _Scheduling("completed", dtstart, f"ics DTSTART {dtstart.isoformat()} (past)")
            return _Scheduling("scheduled", dtstart, f"ics DTSTART {dtstart.isoformat()} (future)")
        # An .ics with no readable DTSTART still evidences a meeting artifact.
        return _Scheduling("scheduled", None, f"calendar attachment {ics.name or 'invite.ics'}")

    body = f"{parsed.subject or ''}\n{strip_quoted(parsed.text_body or '')}"
    lowered = body.lower()
    for phrase in _SCHEDULE_PHRASES:
        if phrase in lowered:
            return _Scheduling("scheduled", None, f"phrase: {phrase}")
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

    # 1. Bounce — a hard, unambiguous delivery failure, and the ONLY verdict
    #    here that ends the message's life (touch_kind=None with
    #    needs_review=False => the pipeline marks it applied and stops). That
    #    is why `detect_bounce` now takes only structural signals: everything
    #    a human could have typed on purpose falls to branch 5 instead.
    #
    #    No warmth touch: a bounce is not interaction progress. It also
    #    performs no contact mutation from this path — the legacy system
    #    archived the bounced address, and the Gmail-findings path's
    #    equivalent has since been downgraded to clearing the email (see
    #    capture/gmail.py). Archiving is a user action with a UI and an undo;
    #    no automated path takes it.
    if detect_bounce(parsed):
        signals["bounced"] = True
        signals["evidence_quote"] = _bounce_evidence(parsed)
        # WHICH address bounced used to be discarded entirely here (the
        # sender is mailer-daemon, not the failed recipient, and nothing
        # downstream ever looked past the boolean). Recording it doesn't
        # change what happens to the Contact (still touch_kind=None, still
        # no ratchet write below) — it just stops throwing the fact away.
        failed_recipient = bounced_recipient(parsed)
        if failed_recipient:
            signals["failed_recipient"] = failed_recipient
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

    # 3. An OUTBOUND forward => the envelope describes the student, not the
    #    exchange inside it => human review. Deliberately BEFORE the auto-reply
    #    check: such a forward's direction and counterparty are both
    #    untrustworthy, which makes it the more fundamental reason we can't act,
    #    and every later branch reads one or the other.
    #
    #    Scoped to outbound on purpose. A `Re: Fwd:` chain is a completely
    #    ordinary shape for a genuine reply — a banker forwards you to a
    #    colleague and the colleague answers with the prefix still in the
    #    subject — and there the envelope IS the exchange: the sender is the
    #    real counterparty. Reviewing those taxed the commonest good case to
    #    guard a bad one that is already covered twice over: a forward whose
    #    prefix was edited off is still outbound with no non-self recipient, so
    #    `_counterparty` comes back empty and `resolve_contact`'s
    #    name-and-email guard refuses to invent a contact for it.
    forward_evidence = detect_forward(parsed) if direction == "outbound" else ""
    if forward_evidence:
        signals["forwarded"] = True
        signals["evidence_quote"] = forward_evidence
        return Classification(
            direction=direction, signals=signals, touch_kind=None,
            needs_review=True, review_reason="forward_unparsed",
        )

    # 4. Auto-reply / OOO => looks like a reply but isn't one => human review.
    if detect_autoreply(parsed):
        signals["evidence_quote"] = (parsed.subject or "")[:200]
        return Classification(
            direction=direction, signals=signals, touch_kind=None,
            needs_review=True, review_reason="auto_reply",
        )

    # 5. Bounce LANGUAGE with no structural signal behind it. This is the
    #    branch that used to not exist: the body scan sat inside
    #    `detect_bounce`, so a human sentence containing "does not exist" was
    #    classified a bounce, given no touch, and marked applied — deleted in
    #    all but name. It is genuinely ambiguous (a student really might
    #    forward a bounce whose headers the provider stripped), and ambiguous
    #    is what the review queue is for.
    token = bounce_language(parsed)
    if token:
        signals["bounce_language"] = token
        signals["evidence_quote"] = f"body: {token}"
        return Classification(
            direction=direction, signals=signals, touch_kind=None,
            needs_review=True, review_reason="unconfirmed_bounce",
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
