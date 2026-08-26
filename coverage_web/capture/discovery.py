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
threshold for INBOUND mail, and it is why a newsletter, a job-board digest,
or a stranger cold-emailing the student never becomes a card.

THE OUTBOUND REVISION (2026-08-25), and why a documented refusal moved
-----------------------------------------------------------------------
This module used to refuse every outreach-only finding outright: "an
outreach-only finding is the user's own sent mail to someone they chose not
to track — not this feature's call to make." That sentence assumed the
student adds people to Coverage BEFORE emailing them. Measured on the
founder's own mailbox (read-only, 2026-08-25), the assumption is false: he
works in Gmail, and in two days he sent ~50 personalised coffee-chat
requests to bankers at directory firms — of which Coverage captured exactly
the two who replied within minutes. The other people he deliberately chose
to build a relationship with had no card, no follow-up clock, no presence.
The product's core loop did not run on the founder's own recruiting.

So an outreach-only finding is now a candidate — under a bar deliberately
HIGHER than the inbound one, because the counterparty has done nothing yet
and the only evidence is the user's own act:

  - The recipient must be at a `Firm.domains` address. No threaded-reply
    escape hatch here: writing to an alum on gmail is real, but "sent one
    email to a personal address" is exactly the "anyone he ever emailed"
    shape this module must never propose from. A directory-firm address is
    the deterministic line between "outreach into the industry he tracks"
    and "mail he happened to send".
  - Everything the inbound ladder refuses stays refused: bulk, bounces,
    no-reply and role-account localparts, ESP/ATS domains, the user's own
    institution.
  - A recipient whose address BOUNCED anywhere in the same batch is
    refused: the send provably did not reach a person (see `BatchContext`).
  - A merge-shaped send never proposes. Two guards, because campaign
    detection needs outbound touches in the database and an all-unmatched
    merge writes none: (a) more than `MERGE_RECIPIENT_LIMIT` distinct
    recipients sharing one normalized subject in one batch is a mail merge
    (his real coffee-chat bursts personalise the subject per person, so
    they pass); (b) a subject whose signature matches a DETECTED `Campaign`
    proposes only if the user has classified that campaign as their own
    recruiting. Note the deliberate asymmetry with the inbound rule below:
    an inbound REPLY from an unanswered campaign still proposes (a human
    engaged; suppression there needs the user's explicit "not my
    recruiting"), while outreach-only evidence from an unanswered campaign
    is exactly the mass send the open question is about, so it waits for
    the answer.

What an accepted outreach proposal creates is also smaller: one `outreach`
touch through the same `crm.services.log_touch` ratchet — which moves
neither warmth nor thread_state, so the contact lands cold with the
follow-up clock running from the send's own date. No warmth is fabricated
from the user's own enthusiasm; the ratchet holds.

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
# A pending outreach-evidence proposal whose person then wrote back: the row
# was upgraded in place to carry the stronger evidence. Not a new proposal —
# the unique (user, email) row is the same row — but not nothing either, so
# the caller's counters can say it happened.
UPGRADED = "upgraded"

# More distinct recipients than this sharing one normalized subject inside a
# single batch is a mail merge, not personal outreach. Three, not one: the
# founder's genuine bursts personalise the subject per person ("USC |
# <connection> | <firm> - ..."), but he does occasionally send the same
# subject to two or three people at one firm, and a real merge is an order
# of magnitude past this (the ICC panel send was 201).
MERGE_RECIPIENT_LIMIT = 3

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


class BatchContext:
    """What one finding cannot know about the batch it arrived in.

    Two of the outbound refusals are facts about the WHOLE batch, not about
    one finding: whether this recipient's address bounced (the bounce is its
    own separate finding, usually seconds behind the send), and whether this
    subject went to enough distinct recipients to be a merge. Built once per
    `apply_findings` batch and handed down; a caller running a single
    finding (tests, mostly) may pass none, and both guards simply don't
    fire — the campaign-signature guard still does, because that one reads
    the database.

    A bounce in a LATER batch is a known, accepted gap: nothing here
    dismisses an already-pending proposal, because no automated path writes
    proposal status — that rule outranks the tidiness. In practice bounces
    land seconds after the send and share its batch (both real bounces in
    the founder's 2026-08-24 burst did).
    """

    def __init__(self, findings: list[dict]):
        from collections import Counter

        from crm.campaigns import normalize_subject

        self.bounced_emails: set[str] = {
            normalize_email(str(f.get("email") or ""))
            for f in findings
            if f.get("bounced") and f.get("email")
        }
        counts: Counter[str] = Counter()
        seen: set[tuple[str, str]] = set()
        for f in findings:
            if not f.get("outreach_sent") or f.get("replied"):
                continue
            email = normalize_email(str(f.get("email") or ""))
            signature = normalize_subject(str(f.get("subject") or ""))
            if email and signature and (signature, email) not in seen:
                seen.add((signature, email))
                counts[signature] += 1
        self.outbound_subject_recipients: dict[str, int] = dict(counts)


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


def _evidence_kind(finding: dict, *, outbound_only: bool = False) -> str:
    """The strongest touch kind the evidence honestly supports.

    An outreach-only finding is ALWAYS `outreach` — even when it carries a
    `chat_status` of "scheduled" off a calendar invite the user themselves
    attached. An invite the user sent an unknown person is still only the
    user's own act; logging `chat_scheduled` off it would gift warmth
    `replied` to somebody who has never typed a word. (For matched
    contacts the pipeline does count an outbound .ics as a scheduled chat;
    a proposal is a stricter surface.)
    """
    if outbound_only:
        return "outreach"
    chat_status = str(finding.get("chat_status", "none") or "none").strip().lower()
    if chat_status == "completed":
        return "chat"
    if chat_status == "scheduled":
        return "chat_scheduled"
    return "reply_received"


def _evidence_line(
    finding: dict, *, firm_matched: bool, outbound_only: bool = False
) -> str:
    """One line of why, subject at most — never a body, never a snippet.
    (`finding["evidence"]` on the live path is a snippet; a proposal card is
    a new surface and keeps to §10's stricter reading.)"""
    subject = (finding.get("subject") or "").strip()
    if outbound_only:
        base = "You wrote to them"
    elif finding.get("threaded_reply"):
        base = "Replied to your email"
    elif firm_matched:
        base = "Wrote to you from a firm address"
    else:
        base = "Wrote to you"
    return (f"{base}: {subject}" if subject else base)[:300]


def _parse_occurred_at(finding: dict):
    """The finding's own timestamp as an aware datetime, or None. Naive
    strings are anchored to UTC, same as `capture.gmail._finding_occurred_at`."""
    raw = (finding.get("occurred_at") or "").strip()
    if not raw:
        return None
    from django.utils.dateparse import parse_datetime

    when = parse_datetime(raw)
    if when is not None and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.utc)
    return when


def consider_finding(
    user, finding: dict, *, firm_domains: FirmDomains | None = None,
    dry_run: bool = False, batch: BatchContext | None = None,
) -> str | None:
    """Run the judgment chain over one UNMATCHED finding. Returns `PROPOSED`
    when a proposal was created (or would be, under `dry_run`), `UPGRADED`
    when a pending outreach-evidence proposal for this address was upgraded
    in place by stronger inbound evidence, `ARCHIVED_MATCH` when the sender
    is an archived contact (reported, never resurrected), and None when the
    finding simply isn't a candidate.

    Callers reach this from `apply_findings`' `skipped_unmatched` branch —
    the one place both capture paths (the live listener and every
    backfill/rescan) already agree means "a real message from someone not in
    Coverage". Writes ONE row at most, and only ever a `ContactProposal`.

    `batch` carries the two facts one finding cannot know alone (same-batch
    bounces, same-batch subject fan-out) — see `BatchContext`.
    """
    # -- the ladder of refusals, cheapest first ----------------------------- #
    if finding.get("bulk") or finding.get("bounced"):
        return None
    # Two kinds of evidence, two bars — see the module docstring's outbound
    # revision. Inbound: the counterparty did something (replied, or a chat
    # got scheduled/completed). Outbound-only: the user deliberately wrote
    # to this person and nothing has come back yet — a candidate since
    # 2026-08-25, under the stricter firm-domain-only bar below.
    outbound_only = bool(finding.get("outreach_sent")) and not finding.get("replied")
    inbound_evidence = not outbound_only and (
        finding.get("replied")
        or str(finding.get("chat_status", "none")).lower() in ("scheduled", "completed")
    )
    if not (inbound_evidence or outbound_only):
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
    if outbound_only:
        # Outbound bar: a directory-firm address, nothing less. No
        # threaded-reply escape (there is no reply), no personal domains —
        # see the module docstring for why this is the line between "his
        # recruiting" and "anyone he ever emailed".
        if firm_id is None:
            return None
        if batch is not None and email in batch.bounced_emails:
            # The send provably never reached a person. The bounce finding
            # itself is refused above; this refuses the SEND it bounced off.
            return None
    elif firm_id is None and not finding.get("threaded_reply"):
        return None
    elif finding.get("addressed_to_user") is False:
        # A reply pointer proves someone hit Reply; only To:/Cc: proves it
        # was aimed at the user. A reply-all into a thread the user was
        # Bcc'd or list-delivered into (a coordinator's "RE:" on a mass
        # invite, live case 2026-08-25) threads without ever addressing
        # them. Explicit False only — a finding that doesn't carry the fact
        # (every finding written before it existed) behaves as it always
        # did.
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
    signature = ""
    if subject:
        from crm.campaigns import normalize_subject

        signature = normalize_subject(subject)
    if signature:
        from crm.models import Campaign

        if inbound_evidence:
            # Only an explicit `other` answer suppresses a REPLY — an
            # undetected or unanswered campaign changes nothing here, the
            # same rule `crm/campaigns.py` holds for the queue. A human
            # engaged; the user's explicit word is what un-persons them.
            if Campaign.objects.for_user(user).filter(
                signature=signature, kind=Campaign.KIND_OTHER
            ).exists():
                return None
        else:
            # Outbound is the OTHER side of the same asymmetry: the only
            # evidence is the send, and a send that groups into a detected
            # campaign is a mass send unless the user has said that
            # campaign IS their recruiting. `other` refuses, and so does
            # `unclassified` — the open question is about this exact mail.
            if Campaign.objects.for_user(user).filter(
                signature=signature
            ).exclude(kind=Campaign.KIND_RECRUITING).exists():
                return None
    if outbound_only and signature and batch is not None:
        # The in-batch half of the merge guard, for the send whose campaign
        # cannot have been detected yet: an all-unmatched merge writes no
        # outbound touches, so `crm.campaigns.detect` never sees it. The
        # fan-out in front of us is evidence enough.
        if batch.outbound_subject_recipients.get(signature, 0) > MERGE_RECIPIENT_LIMIT:
            return None

    # -- existing rows: never duplicate, never resurrect -------------------- #
    existing = ContactProposal.objects.for_user(user).filter(email=email).first()
    if existing is not None:
        # THE ONE WRITE TO AN EXISTING ROW, and it only ever moves upward: a
        # PENDING outreach-evidence proposal whose person has now actually
        # written back gets its evidence upgraded in place. Without this,
        # batch order decides what the card says — the sent mail is scanned
        # before the reply that answered it, so the weaker row would both
        # mis-describe the person ("You wrote to them" about somebody who
        # replied) and make `accept` log the weaker touch, losing the reply
        # forever (the finding is consumed; the contact doesn't exist yet
        # for the ladder to catch it later). Dismissed and accepted rows
        # are untouched — dismissal stays permanent, and nothing here
        # changes status, ever.
        if (
            existing.status == ContactProposal.STATUS_PENDING
            and existing.evidence_kind == "outreach"
            and inbound_evidence
        ):
            if not dry_run:
                existing.evidence_kind = _evidence_kind(finding)
                existing.evidence = _evidence_line(
                    finding, firm_matched=firm_id is not None
                )
                existing.threaded_reply = bool(finding.get("threaded_reply"))
                existing.thread_subject = (
                    display_subject(subject)
                    if finding.get("threaded_reply") else ""
                )
                thread_id = (finding.get("thread_id") or "").strip()[:128]
                if thread_id:
                    existing.thread_id = thread_id
                occurred_at = _parse_occurred_at(finding)
                if occurred_at is not None:
                    existing.occurred_at = occurred_at
                if not existing.role_hint:
                    raw_name = (finding.get("name") or "").strip()
                    _, role_hint = split_display_name(raw_name)
                    if role_hint:
                        existing.role_hint = role_hint
                        existing.recruiting_hint = is_recruiting_role(role_hint)
                existing.save(update_fields=[
                    "evidence_kind", "evidence", "threaded_reply",
                    "thread_subject", "thread_id", "occurred_at",
                    "role_hint", "recruiting_hint",
                ])
            return UPGRADED
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
        ContactProposal.all_objects.create(
            user=user,
            name=name[:255],
            email=email,
            firm_id=firm_id,
            role_hint=role_hint,
            recruiting_hint=is_recruiting_role(role_hint),
            evidence=_evidence_line(
                finding, firm_matched=firm_id is not None,
                outbound_only=outbound_only,
            ),
            evidence_kind=_evidence_kind(finding, outbound_only=outbound_only),
            # For a genuine threaded reply ("Replied to: …") and for the
            # user's own outreach ("You reached out: …") — the two cases
            # where the thread's subject is the sentence's object. Blank for
            # a firm-domain first contact, where "Replied to" would be a
            # false sentence and the evidence line already says what shape
            # that message was.
            thread_subject=(
                display_subject(subject)
                if (finding.get("threaded_reply") or outbound_only) else ""
            ),
            threaded_reply=bool(finding.get("threaded_reply")),
            thread_id=(finding.get("thread_id") or "").strip()[:128],
            occurred_at=_parse_occurred_at(finding),
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
            (
                "Found in your sent mail"
                if proposal.evidence_kind == "outreach"
                else "Found in your inbox"
            )
            + f" · {timezone.localdate():%b %d, %Y}"
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
