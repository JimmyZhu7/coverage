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
2. A sender no human is behind: no-reply-style localparts (`inbound`'s own
   test), role-account localparts (careers@, info@ — a mailbox, not a
   person), or a known ESP/transactional sending domain -> stop.
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
)

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

    firm_domains = firm_domains or FirmDomains()
    firm_id = firm_domains.match(email)
    if firm_id is None and not finding.get("threaded_reply"):
        return None

    # -- existing rows: never duplicate, never resurrect -------------------- #
    if ContactProposal.objects.for_user(user).filter(email=email).exists():
        return None

    raw_name = (finding.get("name") or "").strip() or localpart
    name, role_hint = split_display_name(raw_name)

    everyone = Contact.objects.for_user(user)
    match = everyone.filter(email__iexact=email).first()
    if match is None:
        target = normalize_name(name)
        match = next(
            (c for c in everyone if normalize_name(c.name) == target), None
        )
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
    everyone = Contact.objects.for_user(user)
    match = everyone.filter(email__iexact=proposal.email).first()
    if match is None:
        target = normalize_name(proposal.name)
        match = next(
            (c for c in everyone if normalize_name(c.name) == target), None
        )

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
    """Hide forever. The row stays — it IS the do-not-re-propose memory."""
    if proposal.status != ContactProposal.STATUS_PENDING:
        return
    _resolve(proposal, ContactProposal.STATUS_DISMISSED)


def _resolve(proposal: ContactProposal, status: str, *, extra: list[str] | None = None) -> None:
    proposal.status = status
    proposal.resolved_at = timezone.now()
    proposal.save(update_fields=["status", "resolved_at", *(extra or [])])
