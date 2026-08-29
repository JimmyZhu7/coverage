"""Mail facts — read what a message STATES about a person, and act on it,
with a verbatim quote or not at all.

WHY THIS MODULE EXISTS
----------------------
The pipeline understood four kinds of mail — a reply, a bounce, a blast, and
silence — and flattened everything else to nothing. Two real costs on the
founder's own mailbox (2026-08-24, read-only), both from messages the bulk
classifier CORRECTLY refused to call replies and then threw away whole:

1. `sagarwal@allenco.com` auto-replied "Somil Agarwal is no longer with
   Allen & Company. For matters with which Somil was involved, please
   contact Salima Vahabzadeh at salima@allenco.com." Coverage kept proposing
   the departed Somil — a dead address he would tap Accept on and then chase
   for a follow-up cycle — and never saw Salima, whose name and address sat
   in the body in plain English.
2. Goldman's postmaster answered with DSN 5.2.2: "The recipient's mailbox is
   full and can't accept messages now. Please try resending your message
   later". Correctly NOT a hard bounce (the address works) — and then the
   expanded routing address the DSN named was discarded.

GROUNDED QUOTES, OR NO ACTION — the accuracy contract
------------------------------------------------------
Every action this module takes carries a verbatim sentence from the message
that justifies it, stored on the `MailFact` row and shown on the card — the
same rule `directory.ai_extract` holds for deadlines: an answer that cannot
cite its sentence is not an answer. The deterministic layer satisfies it by
construction (the quote IS the sentence the pattern matched in); the AI
layer's quote is verified as a substring before anything is trusted; and a
message that gates in but yields no quotable fact is SURFACED (a pending
`review` row) rather than acted on or silently dropped.

DETERMINISTIC FIRST. THE MODEL IS THE LONG TAIL, NEVER THE GATE.
-----------------------------------------------------------------
The gate is headers, not prose: RFC 3834 `Auto-Submitted` / `X-Autoreply` /
the stock "Automatic reply:" subject (`capture.inbound.auto_submitted`), or a
DSN the soft-bounce test already typed (`capture.gmail_live`). Inside the
gate, the phrase layer below types the stock wordings mail systems actually
send. Only a GATED message the phrases cannot type reaches
`directory.ai_extract.extract_mail_fact_ai` — dark without an API key, closed
classification, grounded quote — and even then the model only ever points at
a sentence: every email address and date is re-extracted deterministically
from the text, never taken from the model's mouth.

One human-mail exception, deliberately first-person-only: a GENUINE reply
whose sender says "my new email is X@" states a fact about the SPEAKER in
words that cannot be about a third person, so it is read too. Third-person
patterns ("X is no longer with us") are never read from human mail — a human
reply routinely QUOTES the auto-reply it is following up on.

WHERE "ACT" ENDS AND "PROPOSE" BEGINS — the line, drawn explicitly
-------------------------------------------------------------------
The house rule (and the Limited Use posture) is propose-then-confirm for
anything that creates or removes a person. This module acts automatically
exactly where the action is (a) grounded in a quote, (b) reversible by one
tap, and (c) about a FACT of the record rather than the shape of the network:

  ACTS, with a visible undo on the card:
  - departed  -> clear the dead address off the contact (the bounce block's
                 own precedent: the address moves to the notes, the person
                 stays; `prior_email` makes undo exact) and WITHDRAW the
                 departed person's pending outreach-evidence proposal (the
                 existing dismiss/restore machinery — Settings > Dismissed
                 restores it, and `undo` here does too). This is a narrow,
                 deliberate exception to "no automated path writes proposal
                 status": the alternative is proposing a person the firm's
                 own mail system just said is gone.
  - out of office (with a stated return date) -> extend `snoozed_until` to
                 the return date, so the follow-up nag arrives the day they
                 are back instead of counting their leave as silence. Only
                 ever FORWARD — a later snooze the user set stands.
  - routing / address change -> a dated note on the contact recording the
                 alternate address. The primary is NEVER overwritten — the
                 standing rule that only the user replaces an address holds.

  PROPOSES, never creates:
  - a referral ("please contact Salima at salima@...") writes a pending
    `ContactProposal` with evidence_kind "referral" — through the same
    refusal ladder discovery holds (no-reply/role-account/ESP/own-school,
    dismissed-forever memory, never resurrect archived) — and `accept` on a
    referral logs NO touch: the person has neither written nor been written
    to, so they land cold with the first-outreach prompt, which is the true
    state. A referral inside a plain OOO ("for urgent matters contact my
    assistant") is NOT proposed — temporary coverage is not a networking
    lead — and a redirect with no departure/address-change around it is
    surfaced as a pending card for the user to judge.

  NEVER, full stop: creating a Contact, archiving anyone, overwriting a
  populated email, or raising warmth. An auto-reply logs `bulk_received`
  through the existing path (warmth unmoved) and nothing here changes that.

NEVER DOUBLE-COUNTED. The referral proposal is created HERE with its own
evidence kind, and the ordinary discovery hook never sees Salima (she has
sent nothing); if she later genuinely replies, `capture.discovery`'s upgrade
path promotes the same unique (user, email) row in place. The auto-reply
itself stays a `bulk_received` touch — recorded, warmth unmoved — exactly as
before this module existed.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from capture import inbound
from capture.models import ContactProposal, MailFact
from capture.providers import normalize_email
from crm.models import Contact
from directory import ai_extract

# Outcome counters for `apply_findings`' SyncResult.
@dataclass
class Outcome:
    applied: int = 0
    surfaced: int = 0
    referrals: int = 0
    details: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The phrase layer — stock auto-responder wordings, matched per sentence so
# the matching sentence IS the quote.
# --------------------------------------------------------------------------- #

_DEPARTED_RE = re.compile(
    r"\bno longer (?:with|at|employed|works?|working)\b"
    r"|\bhas left (?:the )?(?:firm|company|bank|organi[sz]ation)\b"
    r"|\bis leaving (?:the )?(?:firm|company|bank)\b"
    r"|\bno longer an? employee\b",
    re.IGNORECASE,
)

# "please contact Salima Vahabzadeh at salima@allenco.com" — the name and the
# address, read off the sentence's own structure. The name is whatever sits
# between the verb and "at <address>"; junk names ("me", "us", a clause) are
# cleaned by `_clean_referral_name`, which falls back to the address's
# localpart rather than inventing anything.
_REDIRECT_RE = re.compile(
    r"\b(?:contact|reach out to|reach|email)\s+(?P<name>[^@]{1,60}?)\s+at\s+"
    r"(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)

_OOO_RE = re.compile(
    r"\bout of (?:the )?office\b"
    r"|\bon (?:annual |parental |maternity |paternity |medical |family |personal )?leave\b"
    r"|\baway from (?:the office|my (?:desk|email))\b"
    r"|\bon vacation\b"
    r"|\bcurrently (?:travel|traveling|travelling)\b"
    r"|\blimited access to (?:my )?e?-?mail\b",
    re.IGNORECASE,
)

# First-person only — cannot be a statement about a third person, which is
# what makes it safe to read from a genuine human reply too.
_NEW_ADDRESS_RE = re.compile(
    r"(?:my new (?:e-?mail)(?: address)? (?:is|will be)"
    r"|i(?:'ve| have) moved to"
    r"|(?:please )?(?:now )?reach me at"
    r"|please update your records to)"
    r"\s*:?\s*(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)

# Return-date phrasings. Named months ONLY — a numeric date is a guess about
# which country wrote it (`capture.appmail._due_on`'s rule, held here). The
# year MAY be absent, unlike a deadline: "returning September 2" in an OOO
# can only mean the next September 2 within the leave's own horizon, and
# `_resolve_year` refuses anything that lands more than a year out or in the
# past, so nothing is ever guessed loosely.
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_RETURN_LEAD = (
    r"(?:return(?:ing|s)?(?:\s+to\s+the\s+office)?|be\s+back|back\s+in\s+the\s+office"
    r"|back|until|through)"
)
_RETURN_RES = (
    # "returning on Monday, September 2" / "back September 2, 2026"
    re.compile(
        rf"\b{_RETURN_LEAD}\s+(?:on\s+)?(?:[A-Za-z]+day,?\s+)?"
        rf"(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:\s*,?\s+(?P<year>20\d{{2}}))?\b",
        re.IGNORECASE,
    ),
    # "back on 2 September" / "until 2nd September 2026"
    re.compile(
        rf"\b{_RETURN_LEAD}\s+(?:on\s+)?(?:[A-Za-z]+day,?\s+)?"
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_ALT})\.?"
        rf"(?:\s*,?\s+(?P<year>20\d{{2}}))?\b",
        re.IGNORECASE,
    ),
)

# A return date more than this far past the message is a misparse, not a
# leave. Tighter than a deadline's two years: nobody's vacation responder
# names a return more than a year out.
_MAX_RETURN_DAYS = 366

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# Referral "names" that are not names. Falling back to the localpart is
# honest; "Contact Me" as a proposed person is not.
_NON_NAMES = frozenset({
    "me", "us", "him", "her", "them", "someone", "the team", "our team",
    "my assistant", "my colleague", "the office", "your recruiter",
})


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]


def _grounded(quote: str, source: str) -> bool:
    """Whitespace-normalized verbatim-substring check — the same rule as
    `directory.ai_extract._grounded`, enforced here even for quotes this
    module built itself, so a refactor can never quietly store a sentence
    the message did not say.

    Also the same typographic table `directory.ai_extract._grounded` uses:
    `_detect_ai` below hands this function a quote `extract_mail_fact_ai`
    already accepted, over the identical subject+snippet text — if this
    check normalized punctuation any differently than that one, a quote the
    AI layer just verified as grounded could still be thrown away right
    here, one call later, for a reason that has nothing to do with whether
    it is a real citation."""
    if not quote:
        return False

    def norm(s: str) -> str:
        s = s.translate(ai_extract._TYPOGRAPHIC_EQUIVALENTS)
        s = ai_extract._DASH_RUN_RE.sub("-", s)
        return re.sub(r"\s+", " ", s).strip()

    return norm(quote) in norm(source)


def _quote_of(sentence: str, source: str) -> str | None:
    """A sentence as a storable quote: capped to the column, still verified
    verbatim against the source. None means no action — never a fabricated
    or trimmed-into-falsehood quote."""
    quote = (sentence or "").strip()[:500]
    return quote if _grounded(quote, source) else None


def _resolve_year(month: int, day: int, year: int | None, anchor: date) -> date | None:
    """The stated (or nearest-forward) date, refused rather than guessed
    when it cannot sit within a year after `anchor`."""
    try:
        if year is not None:
            found = date(year, month, day)
            if anchor <= found <= anchor + timedelta(days=_MAX_RETURN_DAYS):
                return found
            return None
        found = date(anchor.year, month, day)
        if found < anchor:
            found = date(anchor.year + 1, month, day)
        if anchor <= found <= anchor + timedelta(days=_MAX_RETURN_DAYS):
            return found
    except ValueError:
        return None
    return None


def _return_date(text: str, anchor: date) -> tuple[date, str] | None:
    """(return date, the sentence stating it), or None. The quote is the
    sentence the DATE lives in — that sentence, not the generic OOO marker,
    is what justifies moving a follow-up clock."""
    for sentence in _sentences(text):
        for pattern in _RETURN_RES:
            match = pattern.search(sentence)
            if match is None:
                continue
            year = match.group("year")
            found = _resolve_year(
                _MONTHS[match.group("month").lower().rstrip(".")],
                int(match.group("day")),
                int(year) if year else None,
                anchor,
            )
            if found is not None:
                return found, sentence
    return None


def _clean_referral_name(raw: str, email: str) -> str:
    name = re.sub(r"^(?:please\s+|either\s+)", "", (raw or "").strip(), flags=re.IGNORECASE)
    name = name.strip(" ,;:")
    if not name or name.lower() in _NON_NAMES or len(name.split()) > 5:
        return email.split("@", 1)[0]
    return name[:255]


# --------------------------------------------------------------------------- #
# Detection over one finding's text
# --------------------------------------------------------------------------- #

@dataclass
class Detected:
    kind: str
    quote: str
    new_email: str = ""
    new_name: str = ""
    return_on: date | None = None
    detected_by: str = "rules"


def _finding_text(finding: dict) -> str:
    """Subject + Gmail's snippet, HTML-unescaped — Gmail's snippet says
    "&amp;" where the message says "&", and a quote stored with entities in
    it is not the sentence the message wrote."""
    subject = (finding.get("subject") or "").strip()
    snippet = html.unescape((finding.get("snippet") or "").strip())
    return f"{subject}\n{snippet}".strip()


def _detect_auto(text: str) -> list[Detected]:
    """The deterministic pass over one gated auto-reply. Returns every fact
    the phrases can type, each carrying the sentence that types it."""
    found: list[Detected] = []
    sentences = _sentences(text)

    for sentence in sentences:
        if _DEPARTED_RE.search(sentence):
            quote = _quote_of(sentence, text)
            if quote:
                found.append(Detected("departed", quote))
            break

    for sentence in sentences:
        match = _REDIRECT_RE.search(sentence)
        if match:
            quote = _quote_of(sentence, text)
            email = normalize_email(match.group("email"))
            if quote and email:
                found.append(Detected(
                    "referral", quote, new_email=email,
                    new_name=_clean_referral_name(match.group("name"), email),
                ))
            break

    for sentence in sentences:
        match = _NEW_ADDRESS_RE.search(sentence)
        if match:
            quote = _quote_of(sentence, text)
            email = normalize_email(match.group("email"))
            if quote and email:
                found.append(Detected("address_change", quote, new_email=email))
            break

    if any(_OOO_RE.search(s) for s in sentences):
        marker = next(s for s in sentences if _OOO_RE.search(s))
        quote = _quote_of(marker, text)
        if quote:
            found.append(Detected("out_of_office", quote))

    return found


# --------------------------------------------------------------------------- #
# Matching people
# --------------------------------------------------------------------------- #

def _contact_for(user, email: str) -> Contact | None:
    return (
        Contact.objects.for_user(user)
        .filter(archived=False, email__iexact=email)
        .first()
    )


def _routing_match(user, routing_email: str):
    """(contact, proposal) the routing address belongs to, by the one rule a
    DSN's expansion follows: the SAME localpart on a subdomain of the stored
    address's own domain. `Noah.Bauld@ny.ibd.email.gs.com` resolves to the
    contact (or pending proposal) at `noah.bauld@gs.com` and to nobody else —
    deterministic, exact, and refusing beats guessing."""
    routing_email = normalize_email(routing_email)
    if "@" not in routing_email:
        return None, None
    localpart, routing_domain = routing_email.split("@", 1)

    def matches(stored: str) -> bool:
        stored = normalize_email(stored)
        if "@" not in stored:
            return False
        stored_local, stored_domain = stored.split("@", 1)
        return stored_local == localpart and (
            routing_domain == stored_domain
            or routing_domain.endswith("." + stored_domain)
        )

    contact = next(
        (
            c for c in Contact.objects.for_user(user).filter(
                archived=False, email__istartswith=f"{localpart}@"
            )
            if matches(c.email)
        ),
        None,
    )
    proposal = next(
        (
            p for p in ContactProposal.objects.for_user(user).filter(
                email__istartswith=f"{localpart}@"
            )
            if matches(p.email)
        ),
        None,
    )
    return contact, proposal


# --------------------------------------------------------------------------- #
# Notes — §10-clean: the FACT in Coverage's words; the quote stays on the row
# --------------------------------------------------------------------------- #

def _append_note(contact: Contact, line: str) -> str:
    """Append one dated line to the contact's notes (the same journal shape
    `capture.gmail._append_note` writes), save, and return the exact stamped
    line so undo can remove precisely it."""
    stamped = f"— {timezone.localdate():%b %d, %Y} — {line}"
    existing = (contact.notes or "").rstrip()
    contact.notes = f"{existing}\n{stamped}" if existing else stamped
    contact.save(update_fields=["notes"])
    return stamped


def _remove_note(contact: Contact, stamped_line: str) -> None:
    if not stamped_line:
        return
    notes = contact.notes or ""
    for candidate in (f"\n{stamped_line}", stamped_line):
        if candidate in notes:
            contact.notes = notes.replace(candidate, "", 1)
            contact.save(update_fields=["notes"])
            return


def _snooze_datetime(user, return_on: date) -> datetime:
    """Midnight at the START of the return day, on the user's own clock —
    the nag comes back the day they are back, not a day late and not while
    they are still away. Same timezone discipline as
    `capture.gmail._upsert_scheduled_chat`."""
    tzname = (getattr(user, "timezone", "") or "").strip()
    try:
        zone = ZoneInfo(tzname) if tzname else timezone.get_current_timezone()
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.get_current_timezone()
    return datetime(return_on.year, return_on.month, return_on.day, tzinfo=zone)


# --------------------------------------------------------------------------- #
# Fact rows
# --------------------------------------------------------------------------- #

def _existing_fact(user, about_email: str, kind: str) -> MailFact | None:
    return MailFact.objects.for_user(user).filter(
        about_email=about_email, kind=kind
    ).first()


def _create_fact(user, kind: str, *, about_email: str, finding: dict,
                 quote: str = "", **fields) -> MailFact:
    from capture.discovery import _parse_occurred_at

    return MailFact.all_objects.create(
        user=user,
        kind=kind,
        about_email=about_email[:254],
        quote=quote[:500],
        subject=(finding.get("subject") or "").strip()[:300],
        thread_id=(finding.get("thread_id") or "").strip()[:128],
        occurred_at=_parse_occurred_at(finding),
        **fields,
    )


# --------------------------------------------------------------------------- #
# The referral proposal — discovery's refusal ladder, then a pending row
# --------------------------------------------------------------------------- #

def _propose_referral(
    user, det: Detected, *, referred_by: str, firm_domains, dry_run: bool
) -> tuple[str, ContactProposal | None]:
    """Run the referred address through the same refusals discovery holds and
    write a pending referral proposal. Returns (outcome, proposal):
    "proposed", "exists" (a live contact or any prior proposal row already
    answers the question), or "refused" (the ladder said no)."""
    from capture import discovery

    email = det.new_email
    if not email or "@" not in email:
        return "refused", None
    localpart = email.split("@", 1)[0]
    if inbound.looks_like_noreply(email):
        return "refused", None
    if discovery._ROLE_ACCOUNT_LOCALPART_RE.match(localpart):
        return "refused", None
    if discovery._is_transactional_domain(email):
        return "refused", None
    sender_domain = email.rsplit("@", 1)[-1]
    if any(
        sender_domain == own or sender_domain.endswith("." + own)
        for own in discovery._own_institution_domains(user)
    ):
        return "refused", None

    # The remembered-forever dedup, exactly discovery's: any row for this
    # address — pending, accepted, or dismissed — already answers this.
    if ContactProposal.objects.for_user(user).filter(email=email).exists():
        return "exists", None
    match = discovery._match_existing(user, email, det.new_name)
    if match is not None:
        # Already in the network (or archived, which a scan never
        # resurrects). Nothing to propose either way.
        return "exists", None

    firm_domains = firm_domains or discovery.FirmDomains()
    if dry_run:
        return "proposed", None
    proposal = ContactProposal.all_objects.create(
        user=user,
        name=det.new_name[:255] or localpart,
        email=email,
        firm_id=firm_domains.match(email),
        evidence=f"Named as the person to contact for {referred_by}"[:300],
        evidence_kind="referral",
        thread_id="",
        occurred_at=None,
    )
    return "proposed", proposal


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #

def consider_finding(
    user, finding: dict, *, firm_domains=None, dry_run: bool = False,
    allow_ai: bool = True,
) -> Outcome:
    """Read one finding for stated facts and act/propose/surface per the
    module contract. Called from `capture.gmail.apply_findings` for every
    finding, before contact matching — whether the sender is in the contact
    book has nothing to do with whether their mailbox stated a fact."""
    out = Outcome()
    if not finding.get("found") or finding.get("outreach_sent") or finding.get("bounced"):
        return out

    sender = normalize_email(str(finding.get("email") or ""))[:254]
    if not sender or "@" not in sender:
        return out
    text = _finding_text(finding)

    if finding.get("soft_bounce"):
        _apply_routing(user, sender, finding, text, out, dry_run=dry_run)
        return out

    if finding.get("auto_reply"):
        detected = _detect_auto(text)
        if not detected and allow_ai:
            detected = _detect_ai(text, finding)
        if not detected:
            _surface_review(user, sender, finding, out, dry_run=dry_run)
            return out
        _apply_auto(
            user, sender, finding, text, detected, out,
            firm_domains=firm_domains, dry_run=dry_run,
        )
        return out

    # A genuine human reply: first-person address change only (see module
    # docstring on why third-person patterns are never read from human mail).
    if finding.get("replied") and not finding.get("bulk"):
        for sentence in _sentences(text):
            match = _NEW_ADDRESS_RE.search(sentence)
            if match:
                quote = _quote_of(sentence, text)
                email = normalize_email(match.group("email"))
                if quote and email and email != sender:
                    _apply_address_change(
                        user, sender, finding, quote, email, out, dry_run=dry_run
                    )
                break
    return out


def _detect_ai(text: str, finding: dict) -> list[Detected]:
    """The long tail: ask the model WHICH fact the text states and WHERE,
    then re-run the deterministic extractors over the grounded text for
    every structured datum. The model can point; it cannot dictate."""
    subject = (finding.get("subject") or "").strip()
    snippet = html.unescape((finding.get("snippet") or "").strip())
    guess = ai_extract.extract_mail_fact_ai(subject, snippet)
    if guess is None:
        return []
    quote = _quote_of(guess.phrase, text)
    if not quote:
        return []
    detected = [Detected(guess.value, quote, detected_by="ai")]
    if guess.value == "departed":
        # A departure the model read often sits beside a referral the exact
        # phrases missed — re-scan deterministically, never ask the model
        # for an address.
        for sentence in _sentences(text):
            match = _REDIRECT_RE.search(sentence)
            if match:
                rq = _quote_of(sentence, text)
                email = normalize_email(match.group("email"))
                if rq and email:
                    detected.append(Detected(
                        "referral", rq, new_email=email,
                        new_name=_clean_referral_name(match.group("name"), email),
                        detected_by="ai",
                    ))
                break
    return detected


# --------------------------------------------------------------------------- #
# Appliers
# --------------------------------------------------------------------------- #

def _apply_auto(
    user, sender: str, finding: dict, text: str, detected: list[Detected],
    out: Outcome, *, firm_domains, dry_run: bool,
) -> None:
    kinds = {d.kind: d for d in detected}
    name = (finding.get("name") or "").strip() or sender.split("@", 1)[0]

    if "departed" in kinds:
        _apply_departed(
            user, sender, name, finding, kinds["departed"], out,
            referral=kinds.get("referral"), firm_domains=firm_domains,
            dry_run=dry_run,
        )
        return
    if "address_change" in kinds:
        det = kinds["address_change"]
        if det.new_email and det.new_email != sender:
            _apply_address_change(
                user, sender, finding, det.quote, det.new_email, out,
                dry_run=dry_run, detected_by=det.detected_by,
            )
        return
    if "out_of_office" in kinds:
        _apply_ooo(user, sender, name, finding, text, kinds["out_of_office"],
                   out, dry_run=dry_run)
        return
    if "referral" in kinds:
        # A redirect with nothing around it saying WHY (no departure, no
        # address change): the honest read is "coverage while away" or
        # context we don't have — surfaced, not proposed.
        det = kinds["referral"]
        if _existing_fact(user, sender, MailFact.KIND_REFERRAL) is not None:
            return
        out.surfaced += 1
        out.details.append(
            f"{name}: auto-reply names {det.new_name} <{det.new_email}> — "
            "surfaced for your look (no departure stated, so nobody proposed)"
        )
        if not dry_run:
            _create_fact(
                user, MailFact.KIND_REFERRAL, about_email=sender,
                finding=finding, quote=det.quote, about_name=name[:255],
                new_email=det.new_email, new_name=det.new_name[:255],
                detected_by=det.detected_by,
                status=MailFact.STATUS_PENDING,
            )
        return
    _surface_review(user, sender, finding, out, dry_run=dry_run)


def _apply_departed(
    user, sender: str, name: str, finding: dict, det: Detected, out: Outcome,
    *, referral: Detected | None, firm_domains, dry_run: bool,
) -> None:
    if _existing_fact(user, sender, MailFact.KIND_DEPARTED) is None:
        contact = _contact_for(user, sender)
        proposal = ContactProposal.objects.for_user(user).filter(
            email=sender, status=ContactProposal.STATUS_PENDING,
            evidence_kind="outreach",
        ).first()

        actions: list[str] = []
        note_line = ""
        prior_email = ""
        if contact is not None and (contact.email or "").strip():
            prior_email = contact.email.strip()
            actions.append(f"{prior_email} cleared from the contact")
        if proposal is not None:
            actions.append("their pending card withdrawn")
        action_note = "; ".join(actions).capitalize() if actions else ""

        if not dry_run:
            from capture import discovery

            if contact is not None and prior_email:
                note_line = _append_note(
                    contact,
                    f"Their mailbox auto-replied that they have left the firm "
                    f"— {prior_email} cleared from the contact (kept here).",
                )
                contact.email = ""
                contact.save(update_fields=["email"])
            if proposal is not None:
                discovery.dismiss(proposal)
            _create_fact(
                user, MailFact.KIND_DEPARTED, about_email=sender,
                finding=finding, quote=det.quote, about_name=name[:255],
                detected_by=det.detected_by,
                status=(
                    MailFact.STATUS_APPLIED if actions
                    else MailFact.STATUS_PENDING
                ),
                action_note=action_note[:300],
                contact=contact,
                proposal=proposal,
                prior_email=prior_email[:254],
                note_line=note_line[:500],
            )
        if actions:
            out.applied += 1
            out.details.append(f"{name}: no longer at the firm — {'; '.join(actions)}")
        else:
            out.surfaced += 1
            out.details.append(
                f"{name}: no longer at the firm — noted (nothing on file to change)"
            )

    if referral is not None and referral.new_email and referral.new_email != sender:
        outcome, proposal = _propose_referral(
            user, referral, referred_by=f"{name}'s matters",
            firm_domains=firm_domains, dry_run=dry_run,
        )
        if outcome == "proposed":
            out.referrals += 1
            out.details.append(
                f"{referral.new_name}: named as {name}'s replacement — "
                "proposed for your confirm"
            )
            if not dry_run and _existing_fact(
                user, sender, MailFact.KIND_REFERRAL
            ) is None:
                _create_fact(
                    user, MailFact.KIND_REFERRAL, about_email=sender,
                    finding=finding, quote=referral.quote,
                    about_name=name[:255],
                    new_email=referral.new_email,
                    new_name=referral.new_name[:255],
                    detected_by=referral.detected_by,
                    status=MailFact.STATUS_APPLIED,
                    action_note=f"Proposed {referral.new_name} as a contact"[:300],
                    proposal=proposal,
                )


def _apply_address_change(
    user, sender: str, finding: dict, quote: str, new_email: str, out: Outcome,
    *, dry_run: bool, detected_by: str = "rules",
) -> None:
    if _existing_fact(user, sender, MailFact.KIND_ADDRESS_CHANGE) is not None:
        return
    name = (finding.get("name") or "").strip() or sender.split("@", 1)[0]
    contact = _contact_for(user, sender)
    applied = contact is not None
    if not dry_run:
        note_line = ""
        if contact is not None:
            note_line = _append_note(
                contact,
                f"They wrote that their new address is {new_email} — noted, "
                f"primary ({contact.email or 'blank'}) kept.",
            )
        _create_fact(
            user, MailFact.KIND_ADDRESS_CHANGE, about_email=sender,
            finding=finding, quote=quote, about_name=name[:255],
            new_email=new_email, detected_by=detected_by,
            status=MailFact.STATUS_APPLIED if applied else MailFact.STATUS_PENDING,
            action_note=(
                f"New address {new_email} noted on the contact" if applied else ""
            )[:300],
            contact=contact,
            note_line=note_line[:500],
        )
    if applied:
        out.applied += 1
        out.details.append(f"{name}: stated a new address ({new_email}) — noted")
    else:
        out.surfaced += 1
        out.details.append(
            f"{name}: stated a new address ({new_email}) — surfaced for your look"
        )


def _apply_ooo(
    user, sender: str, name: str, finding: dict, text: str, det: Detected,
    out: Outcome, *, dry_run: bool,
) -> None:
    from capture.discovery import _parse_occurred_at

    occurred = _parse_occurred_at(finding)
    anchor = timezone.localdate(occurred) if occurred else timezone.localdate()
    dated = _return_date(text, anchor)
    return_on, quote = (dated[0], dated[1]) if dated else (None, det.quote)
    if dated:
        quote = _quote_of(dated[1], text) or det.quote

    contact = _contact_for(user, sender)
    existing = _existing_fact(user, sender, MailFact.KIND_OOO)
    if existing is not None:
        # DISMISSED or UNDONE is the user's own word — "stop touching this"
        # — and it outranks any later auto-reply, the same way
        # `address_is_departed` excludes an undone departure and `dismiss`'s
        # docstring calls every dismissed row a permanent do-not-re-create
        # memory. Without this check, a card the user waved away (or a
        # snooze they explicitly undid) came back to life — status flipped
        # back to `applied` and `contact.snoozed_until` overwritten again —
        # the moment a later auto-reply from the same sender happened to
        # state a date, with no tap from the user in between.
        if existing.status in (MailFact.STATUS_DISMISSED, MailFact.STATUS_UNDONE):
            return
        # A NEW leave later on updates the one row rather than being blocked
        # forever by the dedup — but only ever forward, and only while the
        # row is still LIVE. `dismissed`/`undone` are closed states the
        # student themselves put this card into — `dismiss()` only accepts
        # `pending`/`applied` and `undo()` only acts on `applied`, both
        # refusing to touch a row already in one of those two terminal
        # states. This update branch is the one path in the module that
        # skipped that check: a later, perfectly ordinary "still out,
        # pushed my return back" auto-reply from the same sender would
        # flip `status` back to `applied` and push `contact.snoozed_until`
        # forward again — reviving a card the student had explicitly
        # closed, and moving a CRM field with no new tap behind it. See
        # `test_mailfacts.TestOooDismissedStaysDismissed`.
        if existing.status not in (MailFact.STATUS_PENDING, MailFact.STATUS_APPLIED):
            return
        if (
            return_on is not None
            and (existing.return_on is None or return_on > existing.return_on)
        ):
            if not dry_run:
                existing.return_on = return_on
                existing.quote = quote[:500]
                existing.status = MailFact.STATUS_APPLIED if contact else MailFact.STATUS_PENDING
                prior_snooze = contact.snoozed_until if contact else None
                prior_was_ours = (
                    prior_snooze is not None and prior_snooze == existing.snoozed_to
                )
                snoozed_to = _extend_snooze(user, contact, return_on) if contact else None
                if snoozed_to is not None:
                    existing.snoozed_to = snoozed_to
                    # Preserve what stood before OUR writes: if the value we
                    # are overwriting was our own previous extension, the
                    # original prior (possibly the user's own snooze) is
                    # already recorded and must not be replaced by our own
                    # intermediate value.
                    if not prior_was_ours:
                        existing.prior_snoozed_until = prior_snooze
                    existing.action_note = f"Follow-up snoozed to {return_on:%b %d, %Y}"[:300]
                existing.save(update_fields=[
                    "return_on", "quote", "status", "snoozed_to",
                    "prior_snoozed_until", "action_note",
                ])
            out.applied += 1
            out.details.append(
                f"{name}: out of office again, back {return_on:%b %d} — "
                "follow-up moved"
            )
        return

    if contact is None:
        # Nobody's clock is running for a non-contact; a card for every OOO
        # from a stranger would be noise. Nothing recorded, deliberately.
        return
    if return_on is None:
        out.surfaced += 1
        out.details.append(
            f"{name}: out of office, no readable return date — surfaced"
        )
        if not dry_run:
            _create_fact(
                user, MailFact.KIND_OOO, about_email=sender, finding=finding,
                quote=quote, about_name=name[:255],
                detected_by=det.detected_by,
                status=MailFact.STATUS_PENDING, contact=contact,
            )
        return

    if not dry_run:
        prior_snooze = contact.snoozed_until
        snoozed_to = _extend_snooze(user, contact, return_on)
        note_line = _append_note(
            contact,
            f"Out of office until {return_on:%b %d, %Y} — follow-up waits for "
            "their return.",
        )
        _create_fact(
            user, MailFact.KIND_OOO, about_email=sender, finding=finding,
            quote=quote, about_name=name[:255],
            detected_by=det.detected_by,
            return_on=return_on,
            status=MailFact.STATUS_APPLIED,
            action_note=f"Follow-up snoozed to {return_on:%b %d, %Y}"[:300],
            contact=contact,
            snoozed_to=snoozed_to,
            # What stood before the extension — a snooze the USER set to an
            # earlier date is overwritten by the forward-only move, and undo
            # must put IT back rather than clearing to None.
            prior_snoozed_until=prior_snooze if snoozed_to is not None else None,
            note_line=note_line[:500],
        )
    out.applied += 1
    out.details.append(
        f"{name}: out of office until {return_on:%b %d, %Y} — follow-up "
        "snoozed to their return, warmth untouched"
    )


def _extend_snooze(user, contact: Contact, return_on: date):
    """Move `snoozed_until` FORWARD to the return date. A later snooze the
    user set themselves stands; returns the value written, or None when the
    existing snooze already covers it."""
    target = _snooze_datetime(user, return_on)
    if contact.snoozed_until and contact.snoozed_until >= target:
        return None
    contact.snoozed_until = target
    contact.save(update_fields=["snoozed_until"])
    return target


def _apply_routing(
    user, failed_email: str, finding: dict, text: str, out: Outcome,
    *, dry_run: bool,
) -> None:
    """A soft bounce: record the DSN's expanded routing address against the
    person it belongs to. Keeps every address it finds; clears none."""
    if _existing_fact(user, failed_email, MailFact.KIND_ROUTING) is not None:
        return
    # The quote: the DSN sentence carrying its actual news. Prefer the
    # mailbox-full/deferral sentence over the boilerplate first line.
    quote = ""
    from capture.gmail_live import _SOFT_BOUNCE_RE

    for sentence in _sentences(text):
        if _SOFT_BOUNCE_RE.search(sentence):
            quote = _quote_of(sentence, text) or ""
            break
    if not quote:
        # No citable sentence, no action — surface instead.
        _surface_review(user, failed_email, finding, out, dry_run=dry_run)
        return

    # `_routing_match` covers the exact-address case too (a domain is a
    # trivial suffix of itself), so one lookup answers both "the DSN named
    # the stored address" and "the DSN named its expanded routing form".
    contact, proposal = _routing_match(user, failed_email)
    if contact is None and proposal is None:
        # Nobody on file is behind this address — nothing to record it
        # against, and a card about a stranger's full mailbox is noise.
        return
    primary = (contact.email if contact else proposal.email) or ""
    routing = failed_email if failed_email != normalize_email(primary) else ""

    name = (
        (contact.name if contact else proposal.name)
        or failed_email.split("@", 1)[0]
    )
    applied = bool(routing)
    if not dry_run:
        note_line = ""
        if contact is not None and routing:
            note_line = _append_note(
                contact,
                f"Mail to {primary} was deferred (mailbox full, not a bounce). "
                f"Their mail system routes it via {routing} — primary kept.",
            )
        _create_fact(
            user, MailFact.KIND_ROUTING, about_email=failed_email,
            finding=finding, quote=quote, about_name=str(name)[:255],
            new_email=routing[:254],
            status=MailFact.STATUS_APPLIED if applied else MailFact.STATUS_PENDING,
            action_note=(
                f"Routing address {routing} recorded; {primary} kept"
                if routing else "Delivery deferred — address kept"
            )[:300],
            contact=contact,
            proposal=proposal,
            note_line=note_line[:500],
        )
    if applied:
        out.applied += 1
        out.details.append(
            f"{name}: delivery deferred (mailbox full) — {primary} kept, "
            f"routing address {routing} recorded"
        )
    else:
        out.surfaced += 1
        out.details.append(
            f"{name}: delivery deferred (mailbox full) — address kept, "
            "surfaced for your look"
        )


def _surface_review(user, sender: str, finding: dict, out: Outcome, *, dry_run: bool) -> None:
    """The no-quote fallback: never act, never drop — a pending card."""
    if _existing_fact(user, sender, MailFact.KIND_REVIEW) is not None:
        return
    name = (finding.get("name") or "").strip() or sender.split("@", 1)[0]
    out.surfaced += 1
    out.details.append(
        f"{name}: automated reply we could not read — surfaced for your look"
    )
    if not dry_run:
        _create_fact(
            user, MailFact.KIND_REVIEW, about_email=sender, finding=finding,
            about_name=name[:255], status=MailFact.STATUS_PENDING,
        )


# --------------------------------------------------------------------------- #
# The taps: undo / dismiss
# --------------------------------------------------------------------------- #

def address_is_departed(user, email: str) -> bool:
    """Whether a departed-fact stands against this address. Read by
    `capture.gmail.apply_findings`' email-backfill block: without it, the
    very finding whose auto-reply just cleared a dead address (or any later
    scan of an older message from it) name-matches the contact, sees a blank
    email column, and helpfully refills the address the firm's own mail
    system said is gone. UNDONE is excluded on purpose — the user's undo is
    the user saying the address stands."""
    email = normalize_email(email)
    if not email:
        return False
    return MailFact.objects.for_user(user).filter(
        about_email=email, kind=MailFact.KIND_DEPARTED,
    ).exclude(status=MailFact.STATUS_UNDONE).exists()


def undo(fact: MailFact) -> None:
    """Reverse exactly what the apply did, and only where the state still
    matches what the apply wrote — a value the user changed by hand since is
    never overwritten. Idempotent: only `applied` rows undo."""
    if fact.status != MailFact.STATUS_APPLIED:
        return
    from capture import discovery

    contact = fact.contact
    if fact.kind == MailFact.KIND_DEPARTED:
        if contact is not None and fact.prior_email and not (contact.email or "").strip():
            contact.email = fact.prior_email
            contact.save(update_fields=["email"])
            _remove_note(contact, fact.note_line)
        if (
            fact.proposal is not None
            and fact.proposal.status == ContactProposal.STATUS_DISMISSED
        ):
            discovery.restore(fact.proposal)
    elif fact.kind == MailFact.KIND_OOO:
        if (
            contact is not None
            and fact.snoozed_to is not None
            and contact.snoozed_until == fact.snoozed_to
        ):
            # Restore what the apply overwrote — which is the user's own
            # earlier snooze when there was one, and None only when there
            # was nothing before. Clearing to None unconditionally
            # destroyed a snooze the user had set themselves.
            contact.snoozed_until = fact.prior_snoozed_until
            contact.save(update_fields=["snoozed_until"])
        if contact is not None:
            _remove_note(contact, fact.note_line)
    elif fact.kind in (MailFact.KIND_ROUTING, MailFact.KIND_ADDRESS_CHANGE):
        if contact is not None:
            _remove_note(contact, fact.note_line)
    elif fact.kind == MailFact.KIND_REFERRAL:
        # The referral's own undo is the proposal card's Dismiss — one
        # mechanism, not two. Nothing to reverse here.
        return
    fact.status = MailFact.STATUS_UNDONE
    fact.resolved_at = timezone.now()
    fact.save(update_fields=["status", "resolved_at"])


def dismiss(fact: MailFact) -> None:
    """Wave the card away. The row stays — it is the do-not-re-create memory
    (the unique constraint), same contract as every other capture surface."""
    if fact.status not in (MailFact.STATUS_PENDING, MailFact.STATUS_APPLIED):
        return
    fact.status = MailFact.STATUS_DISMISSED
    fact.resolved_at = timezone.now()
    fact.save(update_fields=["status", "resolved_at"])
