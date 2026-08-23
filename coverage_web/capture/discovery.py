"""Contact auto-discovery — judge, PROPOSE, and only ever create on a tap.

WHY THIS MODULE EXISTS, AND WHY ONLY NOW
-----------------------------------------
The founder's original ask was "any networking email automatically appears in
Coverage". Two documents deliberately refused to build that:

- `gmail_live.py` (docstring point 2) excluded new-contact creation from the
  live path because it "write-creates data on every unknown sender".
- `capture_discover` insisted discovery have "its own door", refuse to
  resurrect archived contacts, and refuse to fabricate warmth.

Both were right, and this module changes neither rule. What changed is that a
judgment layer now exists (`capture.inbound`'s bulk-vs-genuine classifier,
`crm.relevance`'s recruiter test), so an unknown sender can be JUDGED before
anything is written — and what gets written is a `ContactProposal`, not a
Contact. Nothing here touches Contact or Touch until the user accepts, and a
dismissed proposal is remembered forever (unique on user+email), so the same
stranger is never re-proposed weekly.

THE JUDGMENT CHAIN, cheapest first, deterministic only (no LLM, same rule as
the rest of this pipeline):

1. Bulk verdict (`capture.inbound`, already applied upstream — the finding
   carries `bulk`) -> never propose from a blast.
2. A sender no human is behind, or not a discovery: no-reply-style localparts
   (`inbound`'s own test), role-account localparts (careers@, info@ — a
   mailbox, not a person), a known ESP/transactional sending domain, or the
   user's OWN institution's domain (their school's housing desk replying is
   a campus relationship, not a networking find — see
   `_own_institution_domains`) -> stop.
3. The sender's domain matches a `Firm.domains` entry in the directory ->
   strong signal. (Firms DO store email domains — `Firm.domains` is a real
   ArrayField, 83 of 127 firms populated on live data — so matching is
   direct, exact-or-subdomain, no derivation needed.)
4. Otherwise, a real human replying in a genuine thread (`In-Reply-To` /
   `References` of something the user sent — `capture.inbound`'s
   `threaded_reply`, carried on the finding) is still a candidate: the user
   emailed them first, from outside Coverage.

No firm match and no reply pointer -> not a candidate. That is the whole
threshold, and it is why a newsletter, a job-board digest, or a stranger
cold-emailing the student never becomes a card.

RULES HONORED FROM THE TWO REFUSALS ABOVE:
- An ARCHIVED contact matching the sender is never proposed and never
  resurrected — reported as skipped, exactly `capture_discover`'s posture.
- Warmth is never fabricated: a proposal records the strongest touch kind its
  evidence actually supports, and `accept` logs that one touch through
  `crm.services.log_touch` — the same ratchet every other source uses — so
  the cadence engine sees true history. There is no path to a warm contact
  without real evidence.
- Recruiting-looking senders are still proposed (they are worth tracking),
  carrying `recruiting_hint` so accept sets `Contact.recruiting_contact` and
  the queue never proposes coffee to them (`crm.relevance`).

WHAT THE CARD HAS TO SAY, AND WHY IT NOW SAYS IT. A proposal exists because
somebody replied to something the user sent. Which thing they replied to is
the whole decision, and the card used to fold it into one run-on evidence
sentence or, when the header was empty, drop it entirely. The founder is also
his club's outreach lead: a reply to "Fall 2026 ICC Alumni Digital Panel
Outreach" and a reply to genuine networking outreach produced the same card,
and the campaign gate below only catches the first when the campaign was
DETECTED — which needs the outbound sends in the database. So the thread
subject is stored on its own (`ContactProposal.thread_subject`, stripped of
reply prefixes by `display_subject`) and rendered as its own line. When there
is no subject to show, the card says the reply was a reply and says nothing
about a subject — see `templates/crm/_cockpit.html`. Nothing is invented.

AND DISMISS IS NO LONGER A TRAPDOOR. The never-re-propose guarantee is about
the SCAN, not about the user: `consider_finding` still refuses on the mere
existence of a row for that address whatever its status, and no automatic path
writes `pending`. `restore` (below) is the only way back, reached from the
Undo strip on Today and the Dismissed card in Settings, and it reconciles
against the same match rule `accept` uses so a person added by another door in
the meantime is never duplicated.
"""

from __future__ import annotations

import re

from django.utils import timezone

from capture import inbound
from capture.models import ContactProposal
from capture.providers import normalize_email, normalize_name
from crm import services as crm_services
from crm.models import Contact
from crm.relevance import is_recruiting_role
from directory.models import Firm

# Outcomes `consider_finding` reports back to the caller's counters.
PROPOSED = "proposed"
ARCHIVED_MATCH = "archived_match"

# Localparts that name a mailbox rather than a person. A proposal is an offer
# to track a HUMAN, so these never qualify — distinct from
# `inbound.looks_like_noreply`, which deliberately keeps careers@/recruiting@
# as addresses a human answers FROM (true for classifying a message; still not
# a person to put on a networking board).
_ROLE_ACCOUNT_LOCALPART_RE = re.compile(
    r"^(?:info|hello|contact|team|support|help|admin|office|"
    r"careers?|jobs?|recruiting|recruitment|talent(?:acquisition)?|hr|"
    r"campus|internships?|apply|applications?|admissions?|"
    r"events?|rsvp|invitations?|programs?|programmes?|"
    r"news(?:letter)?s?|updates?|digest|alerts?|"
    r"marketing|sales|press|media|community|membership|alumni)"
    r"(?:[._+-].*)?$",
    re.IGNORECASE,
)

# Sending domains that are mail INFRASTRUCTURE, not an employer: ESPs,
# transaction mailers, job boards' notification senders. Suffix-matched, so
# `bounce.mailchimpapp.net` and `mailchimpapp.net` both hit. Deliberately
# short and unambiguous — a miss here still has to get past the firm-domain /
# threaded-reply gate, so this list only needs to catch senders that could
# otherwise carry a reply pointer (e.g. a calendaring bot).
_TRANSACTIONAL_DOMAIN_SUFFIXES = (
    "mailchimpapp.net",
    "mcsv.net",
    "rsgsv.net",
    "sendgrid.net",
    "mailgun.org",
    "mailgun.net",
    "amazonses.com",
    "sparkpostmail.com",
    "postmarkapp.com",
    "mandrillapp.com",
    "hubspotemail.net",
    "hs-send.com",
    "exacttarget.com",
    "marketo.com",
    "mktomail.com",
    "salesforce.com",
    "cmail19.com",
    "cmail20.com",
    "createsend.com",
    "customeriomail.com",
    "email.calendly.com",
    "linkedin.com",
    "bounce.linkedin.com",
    "indeed.com",
    "indeedemail.com",
    "glassdoor.com",
    "handshake.com",
    "joinhandshake.com",
    "greenhouse.io",
    "greenhousemail.io",
    "lever.co",
    "hire.lever.co",
    "myworkday.com",
    "icims.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "successfactors.com",
    "brevo.com",
    "sendinblue.com",
    "substack.com",
    "beehiiv.com",
    "mailerlite.com",
    "hirevue.com",
    "qemailserver.com",
    "ccsend.com",
    "mailchimpapp.com",
)

# Domains where an address IS a person, not an institution — the own-domain
# exclusion below must never fire for these, or a mailbox connected at
# gmail.com would exclude every alum on personal email.
_FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "qq.com", "163.com", "126.com",
})


def _own_institution_domains(user) -> set[str]:
    """The user's OWN institutional email domains — their account email's and
    any connected mailbox's, freemail excluded.

    WHY: verified on the founder's real mailbox (2026-08-22, read-only): the
    two would-be junk proposals the other gates could not stop were both
    genuine, threaded, personal replies from HIS OWN school's staff — the
    housing customer-service desk and a university investigator. Someone
    writing from the student's own institution is a campus relationship (an
    RA, an advisor, an office), not a networking discovery; a professor worth
    tracking can always be added by hand. Deterministic, and only as an
    EXCLUSION — no message is ever promoted by this."""
    from capture.models import GmailConnection

    domains = set()
    for address in [getattr(user, "email", "")] + list(
        GmailConnection.all_objects.filter(user=user).values_list(
            "gmail_address", flat=True
        )
    ):
        address = (address or "").strip().lower()
        if "@" in address:
            domain = address.rsplit("@", 1)[-1]
            if domain and domain not in _FREEMAIL_DOMAINS:
                domains.add(domain)
    return domains

# Display-name separators after which a role/affiliation tends to ride:
# "Jane Doe, Campus Recruiting", "Jane Doe | Goldman Sachs",
# "Jane Doe (she/her) - Talent Acquisition". Split on the first, keep both
# halves. Parsing punctuation the sender typed, never inferring from prose.
_NAME_SPLIT_RE = re.compile(r"\s*(?:,|\||–|—|-{1,2})\s+")
_PARENTHETICAL_RE = re.compile(r"\s*\(([^)]*)\)\s*")
# Pronoun parentheticals are identity, not role — dropped, never hinted.
_PRONOUN_RE = re.compile(r"^(?:she|he|they)\b", re.IGNORECASE)


class FirmDomains:
    """Lazy `{domain: firm_id}` map over `Firm.domains`, built at most once
    per batch. Shared-zone read (no user column on firms), reached only to
    label a message the user's own mailbox produced — same posture as every
    other directory read in the capture pipeline."""

    def __init__(self):
        self._map: dict[str, int] | None = None

    def _load(self) -> dict[str, int]:
        if self._map is None:
            self._map = {}
            for firm_id, domains in Firm.objects.exclude(domains=[]).values_list(
                "id", "domains"
            ):
                for domain in domains or []:
                    domain = (domain or "").strip().lower().lstrip("@")
                    if domain:
                        self._map[domain] = firm_id
        return self._map

    def match(self, email: str) -> int | None:
        """The firm whose registered domain the address belongs to, or None.
        Exact match first, then parent-domain (mail.jpmorgan.com matches a
        registered jpmorgan.com) — never the reverse, so a registered
        subdomain can't claim the whole parent."""
        domain = (email or "").rsplit("@", 1)[-1].strip().lower()
        if not domain or "@" not in (email or ""):
            return None
        mapping = self._load()
        if domain in mapping:
            return mapping[domain]
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            parent = ".".join(parts[i:])
            if parent in mapping:
                return mapping[parent]
        return None


def _is_transactional_domain(email: str) -> bool:
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return True
    return any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in _TRANSACTIONAL_DOMAIN_SUFFIXES
    )


def split_display_name(raw: str) -> tuple[str, str]:
    """(person_name, role_hint) off a From: display name. Only ever splits on
    punctuation the sender typed; a plain "Jane Doe" comes back whole with no
    hint. Pronoun parentheticals are dropped from both halves."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    hint = ""
    match = _PARENTHETICAL_RE.search(raw)
    if match:
        inner = match.group(1).strip()
        raw = _PARENTHETICAL_RE.sub(" ", raw).strip()
        if inner and not _PRONOUN_RE.match(inner):
            hint = inner
    pieces = _NAME_SPLIT_RE.split(raw, maxsplit=1)
    name = pieces[0].strip()
    if len(pieces) > 1 and pieces[1].strip():
        tail = pieces[1].strip()
        hint = f"{tail} {hint}".strip() if hint else tail
    return name or raw, hint[:255]


def display_subject(raw: str | None) -> str:
    """A Subject header made fit to READ on a card: reply/forward prefixes
    stripped (all of them — "Re: Fwd: Re:" is one thread, not three), inner
    whitespace collapsed, nothing else touched.

    Deliberately NOT `crm.campaigns.normalize_subject`: that one lowercases
    and drops every digit and symbol because it is a grouping KEY, and
    "fall  icc alumni digital panel outreach" is not a line to show a human.
    This shares only the prefix regex with it, so the two agree about what a
    prefix is.
    """
    from crm.campaigns import _REPLY_PREFIX_RE

    text = (raw or "").strip()
    while True:
        stripped = _REPLY_PREFIX_RE.sub("", text, count=1).strip()
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split())[:255]


def _match_existing(user, email: str, name: str) -> Contact | None:
    """The contact this address-or-name already is, across EVERY row
    including archived ones. Email first (the strong key), then normalized
    name (the weak one) — the exact order and scope `capture_discover` uses,
    lifted out of `consider_finding`/`accept` so `restore` reconciles by the
    same rule instead of growing a third opinion about who is a duplicate."""
    everyone = Contact.objects.for_user(user)
    match = everyone.filter(email__iexact=email).first()
    if match is None:
        target = normalize_name(name)
        match = next((c for c in everyone if normalize_name(c.name) == target), None)
    return match


def _evidence_kind(finding: dict) -> str:
    chat_status = str(finding.get("chat_status", "none") or "none").strip().lower()
    if chat_status == "completed":
        return "chat"
    if chat_status == "scheduled":
        return "chat_scheduled"
    return "reply_received"


def _evidence_line(finding: dict, *, firm_matched: bool) -> str:
    """One line of why, subject at most — never a body, never a snippet.
    (`finding["evidence"]` on the live path is a snippet; a proposal card is
    a new surface and keeps to §10's stricter reading.)"""
    subject = (finding.get("subject") or "").strip()
    if finding.get("threaded_reply"):
        base = "Replied to your email"
    elif firm_matched:
        base = "Wrote to you from a firm address"
    else:
        base = "Wrote to you"
    return (f"{base}: {subject}" if subject else base)[:300]


def consider_finding(
    user, finding: dict, *, firm_domains: FirmDomains | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run the judgment chain over one UNMATCHED finding. Returns `PROPOSED`
    when a proposal was created (or would be, under `dry_run`),
    `ARCHIVED_MATCH` when the sender is an archived contact (reported, never
    resurrected), and None when the finding simply isn't a candidate.

    Callers reach this from `apply_findings`' `skipped_unmatched` branch —
    the one place both capture paths (the live listener and every
    backfill/rescan) already agree means "a real message from someone not in
    Coverage". Writes ONE row at most, and only ever a `ContactProposal`.
    """
    # -- the ladder of refusals, cheapest first ----------------------------- #
    if finding.get("bulk") or finding.get("bounced"):
        return None
    # Inbound evidence only: a proposal answers "someone wrote to you".
    # An outreach-only finding is the user's own sent mail to someone they
    # chose not to track — not this feature's call to make.
    if not (
        finding.get("replied")
        or str(finding.get("chat_status", "none")).lower() in ("scheduled", "completed")
    ):
        return None

    email = normalize_email(str(finding.get("email") or ""))[:254]
    if not email or "@" not in email:
        return None
    localpart = email.split("@", 1)[0]
    if inbound.looks_like_noreply(email) or _ROLE_ACCOUNT_LOCALPART_RE.match(localpart):
        return None
    if _is_transactional_domain(email):
        return None
    sender_domain = email.rsplit("@", 1)[-1]
    own_domains = _own_institution_domains(user)
    if any(
        sender_domain == own or sender_domain.endswith("." + own)
        for own in own_domains
    ):
        return None

    firm_domains = firm_domains or FirmDomains()
    firm_id = firm_domains.match(email)
    if firm_id is None and not finding.get("threaded_reply"):
        return None

    # CAMPAIGN-AWARE SUPPRESSION. A reply to a mail merge the user has already
    # said was NOT their recruiting must not become a "Found in your inbox"
    # card: the subject the reply carries is the campaign's own signature, and
    # the user has answered the question about that send once. Without this, a
    # merge recipient who was never in Coverage (the outbound predates the
    # Gmail connection, say) gets proposed off their reply, and accepting them
    # quietly re-imports a person from a send the user explicitly declassified.
    # Only an explicit `other` answer suppresses — an undetected or unanswered
    # campaign changes nothing here, the same rule `crm/campaigns.py` holds for
    # the queue. Deterministic: one signature lookup, no guessing from prose.
    subject = (finding.get("subject") or "").strip()
    if subject:
        from crm.campaigns import normalize_subject
        from crm.models import Campaign

        signature = normalize_subject(subject)
        if signature and Campaign.objects.for_user(user).filter(
            signature=signature, kind=Campaign.KIND_OTHER
        ).exists():
            return None

    # -- existing rows: never duplicate, never resurrect -------------------- #
    if ContactProposal.objects.for_user(user).filter(email=email).exists():
        return None

    raw_name = (finding.get("name") or "").strip() or localpart
    name, role_hint = split_display_name(raw_name)

    match = _match_existing(user, email, name)
    if match is not None:
        if match.archived:
            # capture_discover's rule, kept exactly: archiving was a
            # deliberate user action, and a scan seeing this person again is
            # not consent to undo it. Reported, not proposed.
            return ARCHIVED_MATCH
        # A live contact matched by name with a different address — the
        # alternate-email note in apply_findings is the right home for that
        # fact, not a duplicate-person proposal.
        return None

    if not dry_run:
        occurred_at = None
        raw_ts = (finding.get("occurred_at") or "").strip()
        if raw_ts:
            from django.utils.dateparse import parse_datetime

            occurred_at = parse_datetime(raw_ts)
            if occurred_at is not None and timezone.is_naive(occurred_at):
                occurred_at = timezone.make_aware(occurred_at, timezone.utc)
        ContactProposal.all_objects.create(
            user=user,
            name=name[:255],
            email=email,
            firm_id=firm_id,
            role_hint=role_hint,
            recruiting_hint=is_recruiting_role(role_hint),
            evidence=_evidence_line(finding, firm_matched=firm_id is not None),
            evidence_kind=_evidence_kind(finding),
            # Only for a genuine threaded reply: the card labels this line
            # "Replied to", and that sentence is false for a firm-domain
            # first contact. Blank there, and the evidence line already says
            # what shape THAT message was.
            thread_subject=(
                display_subject(subject) if finding.get("threaded_reply") else ""
            ),
            threaded_reply=bool(finding.get("threaded_reply")),
            thread_id=(finding.get("thread_id") or "").strip()[:128],
            occurred_at=occurred_at,
        )
    return PROPOSED


# --------------------------------------------------------------------------- #
# The tap: accept / dismiss
# --------------------------------------------------------------------------- #

def accept(proposal: ContactProposal) -> Contact | None:
    """Create the contact this proposal describes — the same creation
    contract `capture_discover` holds: match-before-create (email, then
    normalized name, against every row including archived), never resurrect
    an archived match, and warmth strictly earned (one touch, the evidence's
    own kind, through the normal `crm.services.log_touch` ratchet with the
    thread marker, so later capture runs dedup against it like any other
    touch).

    Returns the contact (created, or the live one that already existed), or
    None when the match was archived — in which case the proposal is
    dismissed rather than left asking forever, and unarchiving stays a
    by-hand decision.

    Idempotent: a proposal past `pending` returns its recorded contact and
    writes nothing.
    """
    if proposal.status != ContactProposal.STATUS_PENDING:
        return proposal.contact

    user = proposal.user
    match = _match_existing(user, proposal.email, proposal.name)

    if match is not None and match.archived:
        _resolve(proposal, ContactProposal.STATUS_DISMISSED)
        return None
    if match is not None:
        proposal.contact = match
        _resolve(proposal, ContactProposal.STATUS_ACCEPTED, extra=["contact"])
        return match

    contact = Contact(
        user=user,
        name=proposal.name,
        email=proposal.email,
        firm=proposal.firm,
        role=proposal.role_hint,
        source="capture",
        # Three-state honesty (see the field's own comment): True only when
        # the role hint says recruiting; otherwise left NULL — "nobody has
        # said" — so the role-text fallback keeps working if a role is
        # filled in later.
        recruiting_contact=True if proposal.recruiting_hint else None,
        notes=(
            f"Found in your inbox · {timezone.localdate():%b %d, %Y}"
            + (f"\n{proposal.evidence}" if proposal.evidence else "")
        ),
    )
    contact.save()

    # Real evidence -> a real touch, never invented: every proposal exists
    # BECAUSE of a genuine inbound message, so the touch is that message's
    # own kind at that message's own time. The ratchet moves warmth exactly
    # as far as the evidence carries it and no further.
    marker = f"[gmail:{proposal.thread_id}] " if proposal.thread_id else ""
    now = proposal.occurred_at
    if now is not None:
        now = min(now, timezone.now())
    crm_services.log_touch(
        user.id, contact.id, proposal.evidence_kind, "email",
        note=f"{marker}{proposal.evidence}".strip() or None,
        now=now,
        source="capture",
    )

    proposal.contact = contact
    _resolve(proposal, ContactProposal.STATUS_ACCEPTED, extra=["contact"])
    return contact


def dismiss(proposal: ContactProposal) -> None:
    """Hide. The row stays — it IS the do-not-re-propose memory, and no scan
    will ever ask about this address again. `restore` below is the way back,
    and only a person can reach it."""
    if proposal.status != ContactProposal.STATUS_PENDING:
        return
    _resolve(proposal, ContactProposal.STATUS_DISMISSED)


# What `restore` did, for the caller's message. The user tapped a button and
# is owed a sentence saying what actually happened, which is not always
# "it's back".
RESTORED = "restored"
ALREADY_A_CONTACT = "already_a_contact"
RESTORE_ARCHIVED = "archived"
RESTORE_NOOP = "noop"


def restore(proposal: ContactProposal) -> tuple[str, Contact | None]:
    """Undo a dismissal: put the row back to `pending` so the card returns to
    the Today lane. Returns `(outcome, contact)`.

    WHY THIS IS NOT A ONE-LINE STATUS FLIP. Dismissal is permanent by design,
    so the gap between the dismiss and the restore can be arbitrarily long,
    and in that gap the person may have entered Coverage by another door —
    hand-added, imported, or accepted from a different proposal. Flipping the
    row back to `pending` there would put a "Not in your network" card on the
    Today page for somebody who demonstrably IS in the network, and one tap on
    Add would then run `accept`, which would match them and… do nothing, after
    asking. So this reconciles against the SAME match rule accept and
    consider_finding use (`_match_existing`: email, then normalized name,
    across every row including archived):

    - Live contact already exists -> the proposal's question is answered, not
      pending. Recorded as `accepted` against that contact, exactly the state
      `accept`'s own already-a-contact branch writes. No duplicate, no card.
    - The match is ARCHIVED -> stays dismissed. Archiving was a deliberate
      user action and `capture_discover`'s rule has always been that a later
      pass is not consent to undo it; unarchiving stays a by-hand decision on
      the contact's own page.
    - No match -> `pending`, `resolved_at` cleared. The card comes back.

    Idempotent: a row that is not `dismissed` is left exactly as it is.
    """
    if proposal.status != ContactProposal.STATUS_DISMISSED:
        return RESTORE_NOOP, proposal.contact

    match = _match_existing(proposal.user, proposal.email, proposal.name)
    if match is not None:
        if match.archived:
            return RESTORE_ARCHIVED, None
        proposal.contact = match
        _resolve(proposal, ContactProposal.STATUS_ACCEPTED, extra=["contact"])
        return ALREADY_A_CONTACT, match

    proposal.status = ContactProposal.STATUS_PENDING
    proposal.resolved_at = None
    proposal.save(update_fields=["status", "resolved_at"])
    return RESTORED, None


def _resolve(proposal: ContactProposal, status: str, *, extra: list[str] | None = None) -> None:
    proposal.status = status
    proposal.resolved_at = timezone.now()
    proposal.save(update_fields=["status", "resolved_at", *(extra or [])])
