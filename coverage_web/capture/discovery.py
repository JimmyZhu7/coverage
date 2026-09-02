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
from datetime import timezone as dt_timezone

from django.utils import timezone

from capture import inbound
from capture.models import ContactProposal
from capture.providers import normalize_email
from crm import recruitment, services as crm_services
from crm.models import Contact, Touch
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
    """The user's OWN institutional email domains — their account email's,
    any connected mailbox's, and any school address they stated in Settings.
    Freemail excluded from all three.

    WHY: verified on the founder's real mailbox (2026-08-22, read-only): the
    two would-be junk proposals the other gates could not stop were both
    genuine, threaded, personal replies from HIS OWN school's staff — the
    housing customer-service desk and a university investigator. Someone
    writing from the student's own institution is a campus relationship (an
    RA, an advisor, an office), not a networking discovery; a professor worth
    tracking can always be added by hand. Deterministic, and only as an
    EXCLUSION — no message is ever promoted by this.

    WHY THE THIRD SOURCE (2026-08-25). The two sources this started with are
    the two the product happens to store, not the two that answer the
    question, and on the person the gate was WRITTEN FOR it knew neither.
    The founder signs in as `zhujimmy123@gmail.com` — freemail, correctly
    dropped here — and has no `GmailConnection` at all, because Gmail Live is
    not set up. Yet every one of the ~50 coffee-chat requests in his sent
    mail goes out from his `usc.edu` alias. So `usc.edu` was in nobody's
    exclusion set, and a threaded reply from USC staff was stopped only by
    the incidental "no firm match and no reply pointer" rule — which a
    threaded reply is exactly the exception to. The gate could not see the
    domain it exists for.

    `accounts.User.school_emails` closes that: a stated fact, visible and
    correctable in Settings (Profile), needing no mail sent and no mailbox
    connected. See that field for why it is stated rather than derived from
    `school` or learned from sent mail.

    SUBDOMAINS: this returns the base domains only; the caller matches
    exact-or-subdomain, so `housing.usc.edu` is covered by `usc.edu` without
    anything being enumerated here."""
    from capture.models import GmailConnection

    domains = set()
    for address in (
        [getattr(user, "email", "")]
        + list(getattr(user, "school_emails", None) or [])
        + list(
            GmailConnection.all_objects.filter(user=user).values_list(
                "gmail_address", flat=True
            )
        )
    ):
        address = (address or "").strip().lower()
        if "@" in address:
            domain = address.rsplit("@", 1)[-1]
            # THE FREEMAIL GUARD IS LOAD-BEARING FOR ALL THREE SOURCES. A
            # student who types a personal address into the Settings field
            # must not blank out every alum replying from that provider —
            # the same reason a gmail.com account email excludes nothing.
            # The form refuses freemail up front; this is the backstop for
            # rows written before it, or by any other path.
            if domain and domain not in _FREEMAIL_DOMAINS:
                domains.add(domain)
    return domains

# Display-name separators after which a role/affiliation tends to ride:
# "Jane Doe, Campus Recruiting", "Jane Doe | Goldman Sachs",
# "Jane Doe (she/her) - Talent Acquisition", and — the shape Barclays'
# address book actually emits — "Liu , Lily : International Corporate
# Banking". Split on the first, keep both halves. Parsing punctuation the
# sender typed, never inferring from prose.
_NAME_SPLIT_RE = re.compile(r"\s*(?:,|\||–|—|:|-{1,2})\s+")
# Letter runs inside a mailbox localpart: `lily.liu` -> {lily, liu},
# `christine.j.hwang` -> {christine, hwang} (the single `j` is dropped, the
# same way a middle initial is set aside everywhere else in this module).
_LOCALPART_WORD_RE = re.compile(r"[a-z]{2,}")
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


def _localpart_words(email: str) -> frozenset[str]:
    localpart, _ = _mailbox_parts(email)
    return frozenset(_LOCALPART_WORD_RE.findall(localpart))


def _inverted_reading(raw: str, email: str) -> tuple[str, str] | None:
    """`("First Last", role_tail)` when `raw` reads as a corporate
    `Last, First` display form AND THE ADDRESS SAYS SO — otherwise None.

    WHY THIS EXISTS. `_name_tokens` has always known that address books
    emit "Nunley, Vanessa N" and inverts it, and `capture.gmail`'s matcher
    gets the benefit because it hands `names_equivalent` the raw header.
    `consider_finding` does not: it runs `split_display_name` first, which
    treats the comma as the role separator it usually is and keeps only the
    left half. Measured on the founder's live mailbox (read-only,
    2026-08-28), Barclays renders one of his real contacts as
    `Liu , Lily : International Corporate Banking`, so a proposal for her
    would have been NAMED "Liu", carried the role "Lily : International
    Corporate Banking", and — because "Liu" matches no full name — proposed
    a second card for somebody already on the board.

    WHY THE ADDRESS IS THE ARBITER, and not a rule about which words look
    like names. "Nunley, Vanessa N" and "Jane Doe, Campus Recruiting" are
    the same string shape; nothing in the text separates them, and guessing
    is what this module refuses to do. The mailbox localpart is evidence
    the sender's own employer wrote: `vanessa.nunley@` says the inverted
    reading is the person, `jane.doe@` says "Campus Recruiting" is not. So
    the inversion is taken ONLY when the inverted name's words are exactly
    the localpart's words — set equality, because `last.first@` is as
    common as `first.last@`, and middle initials are set aside on both
    sides the way they are everywhere else here. No corroboration, no
    change: the parse falls back to exactly what it did before.

    TWO CONVENTIONS, BECAUSE ONE COVERS A QUARTER OF A REAL BOARD. Measured
    over the founder's 182 live contacts (read-only, 2026-08-28), 47 of them
    — Peter Hoffmann at `phoffmann@williamblair.com`, Shaunna Lu at
    `slu@liontree.com`, Somil Agarwal at `sagarwal@allenco.com` — carry an
    address that spells the surname and only the INITIAL of the given name,
    so the word-set test above can never fire for them. The second test is
    that convention read literally: the localpart, stripped of separators
    and any trailing disambiguation digits, is exactly the given name's
    first letter followed by the surname. It is the same kind of evidence,
    not a looser one — `bsingh4@jefferies.com` still refuses "Singh, Jack",
    because B is not J and which name the B stands for is not this
    function's to invent."""
    if "," not in raw:
        return None
    head, rest = raw.split(",", 1)
    head, rest = head.strip(), rest.strip()
    if not head or not rest:
        return None
    pieces = _NAME_SPLIT_RE.split(rest, maxsplit=1)
    given = pieces[0].strip()
    tail = pieces[1].strip() if len(pieces) > 1 else ""
    if not given:
        return None
    candidate = f"{given} {head}".replace(",", " ")
    name_words = tuple(t for t in _name_tokens(candidate) if len(t) > 1)
    if len(name_words) < 2:
        return None

    words = _localpart_words(email)
    corroborated = len(words) >= 2 and frozenset(name_words) == words
    if not corroborated:
        # The run-together conventions, where the localpart carries no
        # separator for the word-set test to split on. Every form here is an
        # EXACT concatenation — nothing is truncated, abbreviated or guessed
        # at, so `ebbakler@` still refuses "af Klercker, Ebba" and
        # `travchen@` still refuses "Chen, Travis".
        localpart = _mailbox_parts(email)[0]
        compact = "".join(
            ch for ch in localpart if ch.isalpha() or ch.isdigit()
        ).rstrip("0123456789")
        surname = "".join(t for t in _name_tokens(head) if len(t) > 1)
        first = next((t for t in _name_tokens(given) if len(t) > 1), "")
        corroborated = bool(surname and first and compact in {
            f"{first[0]}{surname}",   # phoffmann@williamblair.com
            f"{surname}{first[0]}",   # hoffmannp@
            f"{first}{surname}",      # jerryleung@cmbi.com.hk
            f"{surname}{first}",      # leungjerry@
        })
    if not corroborated:
        return None
    return " ".join(candidate.split()), tail


def split_display_name(raw: str, *, email: str = "") -> tuple[str, str]:
    """(person_name, role_hint) off a From: display name. Only ever splits on
    punctuation the sender typed; a plain "Jane Doe" comes back whole with no
    hint. Pronoun parentheticals are dropped from both halves.

    `email` is optional corroboration, never a source of name text: when the
    sender's own mailbox confirms the string is a `Last, First` inversion
    rather than a name-then-role split, the person's name comes back whole
    instead of as a bare surname. See `_inverted_reading`."""
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
    inverted = _inverted_reading(raw, email) if email else None
    if inverted is not None:
        name, tail = inverted
    else:
        pieces = _NAME_SPLIT_RE.split(raw, maxsplit=1)
        name = pieces[0].strip()
        tail = pieces[1].strip() if len(pieces) > 1 else ""
    if tail:
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


# --------------------------------------------------------------------------- #
# Identity — the one opinion on "who is this already"
#
# `_match_existing` below is the SINGLE matcher every proposal-shaped door
# uses (`consider_finding`, `accept`, `restore`, mailfacts' referral gate,
# autopilot's created-or-matched bookkeeping, `capture_discover`). The
# helpers here are its rungs plus one deliberately WEAKER rung
# (`duplicate_evidence`) that never merges anything by itself: it only ever
# words a suggestion for `crm.merge`'s tap-gated duplicate cards. Two rungs
# of one ladder, in one module, so a third opinion about who is a duplicate
# cannot grow — the same rule that pulled `restore` onto `_match_existing`
# in the first place.
#
# NEITHER RUNG EVER PICKS BETWEEN CANDIDATES. When two rows answer to one
# name (or one routing form), `_match_existing` returns None and the
# caller's own door — a proposal card, a new contact, a restored card —
# puts the question to the user. That is the whole ladder in one sentence:
# conclusive evidence acts, suggestive evidence offers, ambiguous evidence
# abstains, and nothing guesses.
#
# `capture.gmail._match_contact` is the one exception, and deliberately so:
# it has a genuinely different contract (active-only, raises on ambiguity
# rather than abstaining, so the sync run can REPORT the drift) that
# `_match_existing` cannot express. Rather than
# grow a third opinion about WHO COUNTS AS A MATCH, it imports the same two
# conclusive rungs — `routing_variant` and `names_equivalent` — and applies
# its own scoping/ambiguity rules on top. The identity rules live here;
# only the "what do I do with an ambiguous or archived hit" policy differs.
# --------------------------------------------------------------------------- #

# The label freemail providers put before the dot. An org-label match
# (`amazon` == `amazon`) is meaningless for these: hotmail.com and hotmail.es
# are separately registered mailboxes, so a shared localpart across them
# proves nothing.
_FREEMAIL_ORG_LABELS = frozenset(
    domain.split(".", 1)[0] for domain in _FREEMAIL_DOMAINS
)

# Country-code registries commonly hang real domains under a generic second
# level (cmbi.com.hk, blackstone.com.cn) — the organisation's own label sits
# one step further left.
_GENERIC_SECOND_LEVELS = frozenset({"com", "co", "org", "net", "ac", "edu", "gov"})


def _name_tokens(name: str) -> tuple[str, ...]:
    """A display name as canonical words: lowercased, diacritics folded,
    periods dropped, and a single `Last, First` comma inverted back to
    `First Last` — the form corporate address books emit ("Nunley, Vanessa
    N", "af Klercker, Ebba"). Punctuation beyond that is left exactly as
    typed: canonicalising is not guessing, and hyphenated or apostrophed
    names stay themselves."""
    import unicodedata

    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace(".", " ").strip().lower()
    if text.count(",") == 1:
        family, given = text.split(",", 1)
        text = f"{given} {family}"
    text = text.replace(",", " ")
    return tuple(text.split())


def names_equivalent(a: str, b: str) -> bool:
    """Whether two display forms read as the SAME full name. True when the
    canonical token sequences are equal, or equal once middle initials are
    set aside — and only then for names still carrying at least a first and
    a last name, so "Matt" can never claim "Matt R".

    Initials must AGREE when both sides carry them: "Vanessa A Nunley" and
    "Vanessa B Nunley" are two people until proven otherwise. And an
    initial never expands into a word — "Jinghan L" does not equal
    "Jinghan Liu", because which Liu/Lau/Lee the L was is exactly the guess
    this function exists to refuse.

    THE FIRST-AND-LAST-NAME FLOOR APPLIES TO EXACT EQUALITY TOO (fixed
    2026-08-28). The paragraph above was true of the middle-initial branch
    and false of the plain `ta == tb` branch, which happily called two
    one-word names one person: `names_equivalent("Kevin", "Kevin")` was
    True. That is not a spelling variant, it is a collision — and it was
    reachable, because `consider_finding` falls back to the mailbox
    localpart when a sender has no display name, and the founder's live
    board carries five one-word cards ("Matt" <matt@nummo.com>, "Diego",
    "Kirthi", "Daksh", "Alexis"). A second Matt writing in from any address
    would have been fused onto the first Matt's card, touches and all. One
    word is a first name, a surname, or a mailbox handle; it never
    identifies a person. Two one-word rows that really are one person still
    reach the user as a duplicate CARD (`duplicate_evidence`'s mailbox
    rung) — an offer, which is the whole point of the ladder."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    words_a = tuple(t for t in ta if len(t) > 1)
    words_b = tuple(t for t in tb if len(t) > 1)
    if len(words_a) < 2 or len(words_b) < 2:
        return False
    if ta == tb:
        return True
    initials_a = sorted(t for t in ta if len(t) == 1)
    initials_b = sorted(t for t in tb if len(t) == 1)
    if initials_a and initials_b and initials_a != initials_b:
        return False
    return words_a == words_b


def _name_shortening(a: str, b: str) -> bool:
    """Whether one display name could be a truncation of the other: every
    word of the shorter reads, in order, as the whole or the start (three
    letters minimum) of a word in the longer. "Ebba Kler" fits "Ebba af
    Klercker"; "Patina Chu" does not fit "Patina Zhu", and no edit-distance
    guess ever gets a say. Suggestive only — never a match by itself."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    short, full = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    i = 0
    for token in short:
        matched = False
        while i < len(full):
            candidate = full[i]
            i += 1
            if candidate == token or (len(token) >= 3 and candidate.startswith(token)):
                matched = True
                break
        if not matched:
            return False
    return True


def _mailbox_parts(email: str) -> tuple[str, str]:
    email = normalize_email(email)
    if "@" not in email:
        return "", ""
    localpart, domain = email.rsplit("@", 1)
    return localpart, domain


def _org_label(domain: str) -> str:
    """The label that names the organisation in a mail domain: `amazon` for
    amazon.com AND amazon.es, `cmbi` for cmbi.com.hk, `gs` for
    ny.ibd.email.gs.com. A heuristic, used only as ONE suggestive signal
    alongside an identical mailbox name — never sufficient on its own, and
    never consulted for freemail labels (see _FREEMAIL_ORG_LABELS)."""
    parts = [p for p in (domain or "").lower().split(".") if p]
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in _GENERIC_SECOND_LEVELS and len(parts[-1]) <= 3:
        return parts[-3]
    return parts[-2]


def _personal_localpart(email: str) -> bool:
    """A localpart that plausibly names one human: four characters or more,
    not a role account, not a no-reply. Two-letter initials ("jl@") are
    refused — two people share initials far too easily to hang identity on
    them."""
    localpart, _ = _mailbox_parts(email)
    if len(localpart) < 4:
        return False
    if _ROLE_ACCOUNT_LOCALPART_RE.match(localpart):
        return False
    return not inbound.looks_like_noreply(email)


def routing_variant(email_a: str, email_b: str) -> bool:
    """The same mailbox seen through the firm's own mail routing: identical
    localpart, one domain an internal extension of the other. Goldman's DSN
    machinery reports `noah.bauld@ny.ibd.email.gs.com` for the person whose
    address is `noah.bauld@gs.com` — one namespace, one mailbox, rewritten
    in transit. Role and no-reply localparts are refused: a shared
    `recruiting@` across subdomains is infrastructure, not a person.

    Public (no leading underscore) because `capture.gmail._match_contact`
    shares this rung too — see that function's docstring.

    THE LOCALPART FLOOR IS `_personal_localpart`'s, NOT A LOOSER ONE (fixed
    2026-08-28). This rung used to refuse only role and no-reply
    localparts, so `jl@bnpparibas.com` and `jl@asia.bnpparibas.com` fused
    CONCLUSIVELY — while `test_identity.py`'s
    `test_two_letter_localparts_prove_nothing` asserts the SUGGESTIVE rung
    refuses that exact pair as proving nothing. A ladder whose conclusive
    rung acts on evidence its suggestive rung will not even mention is
    upside down, and `jl@bnpparibas.com` ("Jinghan L") is a live row on the
    founder's board. `_personal_localpart` is the one place the "four
    characters or more, two people share initials far too easily" rule is
    written down, so this rung now asks it rather than restating half of it.

    Freemail is refused outright for the same reason `_FREEMAIL_ORG_LABELS`
    exists: a subdomain of a mail provider is still the provider's
    namespace, never one employer's internal routing."""
    la, da = _mailbox_parts(email_a)
    lb, db = _mailbox_parts(email_b)
    if not la or la != lb or da == db:
        return False
    if not _personal_localpart(email_a):
        return False
    if _org_label(da) in _FREEMAIL_ORG_LABELS or _org_label(db) in _FREEMAIL_ORG_LABELS:
        return False
    return da.endswith("." + db) or db.endswith("." + da)


def duplicate_evidence(a, b) -> str:
    """One sentence of why two existing contact ROWS might be one person, or
    "" when the evidence does not clear the bar. The suggestive rung of the
    identity ladder: everything it returns is an OFFER for a tap
    (`crm.merge`), never a merge — a false merge fuses two people's
    histories with no clean undo, a false split costs one duplicate card.

    `a` and `b` are Contact-shaped (`.name`, `.email`, `.firm_id`).

    WHAT CLEARS THE BAR, and what never does:
    - An identical personal mailbox name at two RELATED domains (one a
      subdomain of the other, both registered to one directory firm, or one
      org label across country TLDs: ebbakler@amazon.com next to
      ebbakler@amazon.es) — provided the display names could be the same
      person. Live proof: the founder's rows 707/706 are one AWS account
      manager tracked as two people.
    - The identical full name where the addresses do not contradict it:
      the SAME employer domain, same firm, related domains, or one row with
      no address at all.
    - A shared employer alone NEVER suffices, with or without a shared
      surname: two analysts at one bank routinely share both. Different
      full names at unrelated domains never suffice either — a namesake at
      another firm is two people until a human says otherwise.

    THE SAME-DOMAIN CASE WAS THE HOLE (fixed 2026-08-28). "Relatedness" was
    computed only for `da != db`, so the most obvious duplicate shape there
    is — one name, two addresses at ONE employer domain
    (john.smith@gs.com next to j.smith@gs.com) — produced nothing unless
    both rows also carried the same `firm_id` FK. A row whose employer
    never resolved to a directory firm keeps `firm_id` NULL and lives in
    `firm_text`, which is exactly the row a CSV import or a hand-add
    produces, so the pair that most needed asking about was the pair that
    never got asked. Freemail is excluded: two "Jane Doe"s at gmail.com are
    two mailboxes at one provider, not two cards for one colleague."""
    la, da = _mailbox_parts(getattr(a, "email", "") or "")
    lb, db = _mailbox_parts(getattr(b, "email", "") or "")
    same_firm = (
        getattr(a, "firm_id", None) is not None
        and getattr(a, "firm_id", None) == getattr(b, "firm_id", None)
    )
    subdomain_related = bool(
        da and db and da != db and (da.endswith("." + db) or db.endswith("." + da))
    )
    label_a, label_b = _org_label(da), _org_label(db)
    same_employer_domain = bool(
        da and db and da == db and da not in _FREEMAIL_DOMAINS
    )
    org_related = bool(
        da
        and db
        and (
            same_employer_domain
            or (
                da != db
                and (
                    subdomain_related
                    or same_firm
                    or (
                        label_a
                        and label_a == label_b
                        and label_a not in _FREEMAIL_ORG_LABELS
                    )
                )
            )
        )
    )

    same_name = names_equivalent(getattr(a, "name", ""), getattr(b, "name", ""))
    if same_name:
        if la and lb:
            if org_related:
                return (
                    f"Same name at related addresses, {la}@{da} and {lb}@{db}."
                )
            if same_firm:
                return "Same name, and both cards sit at the same firm."
            # Same full name at two unrelated employers: a namesake until a
            # human says otherwise. Refused.
            return ""
        if not la and not lb:
            return "Two cards with the same name and no email address on either."
        with_addr = a if la else b
        return (
            f"Same name. One card has no email address; the other is "
            f"{(with_addr.email or '').strip().lower()}."
        )

    # The mailbox rung: an identical personal localpart at related domains,
    # for names that could be one person's long and short forms.
    if (
        la
        and la == lb
        and da != db
        and _personal_localpart(a.email)
        and org_related
        and _name_shortening(a.name, b.name)
    ):
        detail = "both registered to the same firm" if same_firm else "one organisation"
        return (
            f"Same mailbox name, {la}, at {da} and {db} ({detail}), and the "
            f"two display names read as one person."
        )
    return ""


def _names_two_directory_firms(
    email_a: str, email_b: str, firm_domains: "FirmDomains | None" = None
) -> bool:
    """Whether these two addresses sit at two DIFFERENT directory firms.

    The one contradiction the name rung is allowed to act on, and it is a
    fact rather than a guess: both addresses resolve, by `Firm.domains`, to
    a firm the directory knows, and the two firms are not the same one. A
    "Xiang Li" at cicc.com.cn and a "Xiang Li" at ubs.com are two people
    for exactly the reason `duplicate_evidence` already refuses that pair
    (`test_same_name_at_unrelated_firms_is_a_namesake`).

    Deliberately NOT "the domains are unrelated". An alum replying from
    their personal gmail is the single most common shape in a student's
    board, `duplicate_evidence` offers no card for a same-name pair split
    across an employer and a freemail address, and refusing the name rung
    there would mint a permanent duplicate with no path back. Two tracked
    EMPLOYERS is a different claim, and the only one the directory can
    actually make."""
    if not email_a or not email_b:
        return False
    firm_domains = firm_domains or FirmDomains()
    a = firm_domains.match(email_a)
    b = firm_domains.match(email_b)
    return a is not None and b is not None and a != b


def _match_existing(
    user, email: str, name: str, *, firm_domains: "FirmDomains | None" = None
) -> Contact | None:
    """The contact this address-or-name already is, across EVERY row
    including archived ones — lifted out of `consider_finding`/`accept` so
    `restore` (and every other door) reconciles by the same rule instead of
    growing a third opinion about who is a duplicate.

    Three rungs, strongest first, each conclusive on its own:
    1. The exact address (the strong key).
    2. The same address through the firm's own routing — identical
       localpart, one domain an internal extension of the other (Goldman's
       `ny.ibd.email.gs.com` for `gs.com`). See `routing_variant`.
    3. The same full name (`names_equivalent`): corporate `Last, First`
       inversion and a dropped-or-added middle initial recognised, nothing
       looser. A truncated name, a shared surname, or a shared employer is
       NEVER a match here — that evidence is suggestive at best, and the
       suggestive rung (`duplicate_evidence`) only ever proposes a merge
       for a tap, never performs one.

    AND IT ABSTAINS RATHER THAN PICKS (fixed 2026-08-28). The weak rungs
    used to end in `next(...)`, so when two rows answered to one name this
    returned whichever the queryset yielded first — under
    `Contact.Meta.ordering = ["-created"]`, the most recently added.
    `capture.gmail._match_contact` calls that exact behaviour out as the
    bug it was written to fix ("silently picking the first one found would
    land the touch on whichever row the queryset happened to yield first")
    and raises `AmbiguousContactError` instead; this function, which is the
    one every proposal door uses, still did the old thing. Two "Michael
    Chen" cards and one reply from a third address landed the reply on the
    newer card.

    Abstaining is safe in all four callers precisely because none of them
    treats None as "nothing to do": `consider_finding` writes a PROPOSAL (a
    card the user taps), `accept` creates a new contact, `restore` puts the
    card back, `mailfacts` skips its referral gate. Every one of those is a
    false SPLIT — one extra card, and `crm.merge` is built to offer it back
    — where the old behaviour was a false MERGE, which fuses two people's
    histories with no clean undo. That is rule one of this module.

    The name rung additionally refuses when the two addresses name two
    different directory firms — see `_names_two_directory_firms`."""
    rows = list(Contact.objects.for_user(user))
    email = normalize_email(email)
    if email:
        exact = next(
            (c for c in rows if normalize_email(c.email or "") == email), None
        )
        if exact is not None:
            # Two rows on ONE address are two cards for one mailbox, so
            # picking either states nothing false; the duplicate panel is
            # where that pair gets resolved.
            return exact
        routed = [c for c in rows if c.email and routing_variant(email, c.email)]
        if len(routed) == 1:
            return routed[0]
        if routed:
            return None
    named = [c for c in rows if names_equivalent(c.name, name)]
    if len(named) != 1:
        return None
    if _names_two_directory_firms(email, named[0].email or "", firm_domains):
        return None
    return named[0]


def _evidence_kind(finding: dict, *, outbound_only: bool = False) -> str:
    """The strongest touch kind the evidence honestly supports. Never a chat.

    An outreach-only finding is ALWAYS `outreach` — even when it carries a
    `chat_status` of "scheduled" off a calendar invite the user themselves
    attached. An invite the user sent an unknown person is still only the
    user's own act; logging `chat_scheduled` off it would gift warmth
    `replied` to somebody who has never typed a word. (For matched
    contacts the pipeline does count an outbound .ics as a scheduled chat;
    a proposal is a stricter surface.)

    A PROPOSAL'S CEILING IS `reply_received`, and that is the same "stricter
    surface" argument applied to the other three rungs. This used to return
    `chat_scheduled` / `chat` straight off `chat_status`, and the result was
    structurally dishonest in two ways at once:

    * A `ContactProposal` has no field for the chat's TIME. So a proposal
      accepted as `chat_scheduled` created a contact parked at
      `thread_state="chat_scheduled"` with no `CalendarEvent` behind it —
      `_upsert_scheduled_chat` runs in `apply_findings` for MATCHED contacts
      and never on this path. That is the Youqi Chen shape exactly: a daily
      "did it happen? log the chat or reschedule" card about a meeting with
      no time and no calendar entry, in a state whose only exit is a human.
      Live on the founder's account: Lily Liu (contact 765) carries a
      `chat_scheduled` touch written by this path and zero calendar events.
    * `chat` sets warmth `chatted`, which `capture_worklist.RECHECK_WARMTH`
      drops from every later re-check — so a proposal card, one tap, could
      put a person Coverage had never tracked into the one state no later run
      can revisit. Live: Tanner Kleinberg (contact 764), accepted off a
      `chat` proposal whose evidence line reads "Wrote to you from a firm
      address".

    Nothing is lost by flooring here, because the person becomes a CONTACT at
    the same moment. `reply_received` carries the marker, so the very next
    sync sees the same thread staged at rank 1, and a genuine `chat_scheduled`
    finding (rank 2) climbs past it through `apply_findings` — the path that
    writes the calendar event too. The chat arrives one run later, corroborated,
    instead of arriving now, uncorroborated and unrecoverable.
    """
    if outbound_only:
        return "outreach"
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
        # Django 5 removed `django.utils.timezone.utc` — see
        # `capture.gmail._finding_occurred_at` for the failure this caused.
        when = timezone.make_aware(when, dt_timezone.utc)
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
        # "referral" joins "outreach" here (2026-08-27): a referral proposal
        # (capture.mailfacts — "please contact Salima at salima@...") also
        # describes someone who has not yet written, so a genuine reply from
        # them is strictly stronger evidence and upgrades the same unique
        # row in place. This is also the never-double-count guarantee: the
        # reply can never mint a second card for a person a referral already
        # proposed.
        if (
            existing.status == ContactProposal.STATUS_PENDING
            and existing.evidence_kind in ("outreach", "referral")
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
                    _, role_hint = split_display_name(raw_name, email=email)
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
    name, role_hint = split_display_name(raw_name, email=email)

    # RECRUITMENT RELEVANCE, the same person-level rule the Network board
    # and the daily queue apply (`crm.recruitment` — the founder's
    # 2026-08-25 "any unrelated should not show up"). A sender whose own
    # signature names a campus or off-track seat ("Jane Doe, Account
    # Manager", "Prof. X") must not become a proposal the user then has to
    # dismiss so the board can hide the result: an unrelated person should
    # not get IN in the first place, and the two gates must agree rather
    # than fight. Deliberately the SAME function, run on exactly the
    # evidence a proposal has (the role hint) — pure text, no query — and
    # keep-biased the same way: a hint that names a track or a recruiting
    # function passes before any off-track word is consulted, and a hint
    # that says nothing refuses nobody. This complements the firm-domain
    # bar above rather than replacing it: that one asks "is this mail his
    # recruiting world", this one asks "is this PERSON".
    if role_hint and recruitment.role_hint_disqualified(role_hint):
        return None

    match = _match_existing(user, email, name, firm_domains=firm_domains)
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
# Alumni filed at their school instead of their employer
# --------------------------------------------------------------------------- #

# Words that make a free-text firm a SCHOOL rather than an employer. Generic
# and deliberately small: the specific case is answered by the student's own
# institution domains below, and this list only has to catch the shape
# ("Boston College", "NYU Stern School of Business") for anyone else.
_SCHOOL_WORDS = frozenset({
    "university", "univ", "college", "school", "institute", "academy",
    "alumni", "alum", "polytechnic",
})
_WORD_RE = re.compile(r"[a-z]+")
# Words that carry no identity and so must never be the thing two names have
# in common. Without this, a student whose school is "University of Southern
# California" contributes the token "of", and "Bank of America" then reads as
# naming their school — the exact false positive that would re-file a banker
# as an alum. Three-letter minimum for the same reason, which still admits
# every abbreviation that matters here ("usc", "mit", "nyu").
_STOPWORDS = frozenset({"the", "and", "for", "des", "der", "van", "von"})


def school_tokens(user) -> frozenset[str]:
    """The student's OWN school, as the handful of lowercase words a contact's
    free-text firm would have to contain to be naming it: every word of
    `User.school`, plus the first label of each institutional domain in
    `_own_institution_domains` (so `usc.edu` yields `usc`, which is exactly
    the string 19 of the founder's rows carry in `firm_text`).

    Reuses `_own_institution_domains` rather than re-deriving: that function
    is already the one place this codebase decides what "the student's own
    institution" means, and it already drops freemail.
    """
    tokens = set(_WORD_RE.findall((getattr(user, "school", "") or "").lower()))
    for domain in _own_institution_domains(user):
        tokens.add(domain.split(".", 1)[0])
    return frozenset(t for t in tokens if len(t) >= 3 and t not in _STOPWORDS)


def names_a_school(firm_text: str, user) -> bool:
    """Is this free-text firm a school and not an employer? True when it
    names the student's own institution, or carries a generic school word."""
    words = set(_WORD_RE.findall((firm_text or "").lower()))
    if not words:
        return False
    return bool(words & _SCHOOL_WORDS) or bool(words & school_tokens(user))


def school_firm_fields(contact, *, user=None, firm_domains: FirmDomains | None = None) -> dict:
    """THE ALUM-AT-A-FIRM REPAIR, as a dict of field -> new value (empty when
    the rule does not fire). Does not save; `accept` below and the
    `fix_school_firms` command both apply it, and the command shows it first.

    Measured 2026-09-01 on the founder's account: 19 contacts sit at free-text
    firm "usc" with `firm_id` NULL, and 7 of them write from an address
    `FirmDomains.match` resolves to a directory firm (bain.com x3, bcg.com,
    deloitte.com x2, pwc.com). Those 7 are alumni AT a firm, filed under the
    school — so they are off Firm Coverage, have no tier, no firm dates and no
    Firm Fit, and 2 of them were also victims of the write-order ratchet.
    Their employer was knowable from their own email domain the whole time.

    THE ORDER OF THE TWO FACTS IS THE POINT. `firm` is the employer;
    `school_affiliation` / `school` is the affinity. The single FK can hold
    one of them, and the row was holding the wrong one. Never overwrites a
    firm that already resolved (`firm_id` set -> nothing happens, whatever the
    address says: a resolved FK is either the directory's own answer or a
    person's, and an address is weaker evidence than both), and never fires
    without a school-looking `firm_text`, so a contact at an off-directory
    employer keeps the name the student typed.
    """
    if contact.firm_id is not None or not (contact.email or "").strip():
        return {}
    firm_text = (contact.firm_text or "").strip()
    if not firm_text or not names_a_school(firm_text, user or contact.user):
        return {}
    firm_id = (firm_domains or FirmDomains()).match(contact.email)
    if firm_id is None:
        return {}
    fields = {
        "firm_id": firm_id,
        # The employer takes the FK, so the school moves to the field that
        # holds a school — and stops being a firm name the coverage strip
        # counts. `school` is 64 chars; `firm_text` is 255.
        "firm_text": "",
        "school_affiliation": True,
    }
    if not (contact.school or "").strip():
        fields["school"] = firm_text[:64]
    return fields


# --------------------------------------------------------------------------- #
# The tap: accept / dismiss
# --------------------------------------------------------------------------- #

def accept(
    proposal: ContactProposal,
    *,
    role: str | None = None,
    region: str | None = None,
) -> Contact | None:
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

    `role` and `region`, when given, are what the student typed or picked on
    the proposal card itself (`crm.views.proposal_act`). They exist because
    the Gmail door delivers a name and an address and nothing else, and
    nothing downstream ever asked for the rest: role was blank on 136 of the
    151 capture rows on the founder's account, and 90 rows carried neither a
    role nor a region, 89 of them cold and queue-eligible. `region` is written
    with `region_source="user"` — a person just said so, and that is the one
    provenance nothing else may overwrite. Both are optional; omitted, this
    behaves exactly as it did.

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
        # The one thing an accept onto an EXISTING row may still fix: an alum
        # filed at their school whose address names their employer. Nothing
        # else about a live contact is touched — the student's own record wins
        # over anything a proposal carries.
        upgrade = school_firm_fields(match, user=user)
        if upgrade:
            for field, value in upgrade.items():
                setattr(match, field, value)
            match.save(update_fields=list(upgrade))
        proposal.contact = match
        _resolve(proposal, ContactProposal.STATUS_ACCEPTED, extra=["contact"])
        return match

    contact = Contact(
        user=user,
        name=proposal.name,
        email=proposal.email,
        firm=proposal.firm,
        role=(role or "").strip()[:255] or proposal.role_hint,
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
                else (
                    "Referred in an auto-reply"
                    if proposal.evidence_kind == "referral"
                    else "Found in your inbox"
                )
            )
            + f" · {timezone.localdate():%b %d, %Y}"
            + (f"\n{proposal.evidence}" if proposal.evidence else "")
        ),
    )
    # A region the student picked on the card, before the first save() — so
    # `resolve_region`'s tier 1 sees it and returns it unchanged rather than
    # the firm rule filling the blank first and this overwriting it after.
    # Blank stays blank: the deterministic rule is still allowed to answer
    # (and, at a both-market firm, still correctly refuses to guess).
    if region in Contact.REGION_VALUES:
        contact.region = region
        contact.region_source = Contact.REGION_SOURCE_USER
    # THE ADDRESS STILL NAMES THE EMPLOYER. `consider_finding` resolved the
    # firm when the proposal was minted; a proposal that has sat pending
    # since before that firm's `domains` were filled in carries `firm=None`
    # for no better reason than when it was written. Asked again here, at the
    # moment the row becomes real, and only to fill a blank — never to
    # second-guess a firm the proposal already carries.
    if contact.firm_id is None and contact.email:
        contact.firm_id = FirmDomains().match(contact.email)
    contact.save()

    # Real evidence -> a real touch, never invented: every proposal exists
    # BECAUSE of a genuine inbound message, so the touch is that message's
    # own kind at that message's own time. The ratchet moves warmth exactly
    # as far as the evidence carries it and no further.
    #
    # EXCEPT A REFERRAL, which logs NOTHING — and that is the honesty, not a
    # gap. The referred person (capture.mailfacts: "please contact Salima at
    # salima@...") has neither written to the user nor been written to; any
    # touch kind would state a fact that never happened. They land cold at
    # no_reply with zero touches, so the cadence engine's own first branch
    # says exactly the true thing: "added but never contacted — send the
    # first note."
    if proposal.evidence_kind != "referral":
        # ROWS WRITTEN BEFORE `_evidence_kind` STOPPED PRODUCING CHAT RUNGS
        # are still in the table (two on the founder's account, both already
        # accepted; a pending one on any account would still be waiting on a
        # tap). Reading the stored value straight back would let a proposal
        # minted last week write the state this module now refuses to write,
        # so the floor is applied HERE too, at the moment of the write,
        # rather than only where the value is chosen. See `_evidence_kind`
        # for why a proposal's ceiling is `reply_received`.
        kind = proposal.evidence_kind
        if kind in ("chat", "chat_scheduled"):
            kind = "reply_received"
        marker = f"[gmail:{proposal.thread_id}] " if proposal.thread_id else ""
        now = proposal.occurred_at
        if now is not None:
            now = min(now, timezone.now())
        result = crm_services.log_touch(
            user.id, contact.id, kind, "email",
            note=f"{marker}{proposal.evidence}".strip() or None,
            now=now,
            source="capture",
        )
        # THE SUBJECT THIS PROPOSAL HAS BEEN CARRYING ALL ALONG. A mail
        # merge's single defining fact is that N threads share one subject,
        # and `crm.campaigns.detect` groups on `Touch.subject` — but only the
        # live capture path ever stamped it, so all 136 accepted proposals on
        # the founder's account produced touches with the column blank while
        # `ContactProposal.thread_subject` sat right there holding it. Tonight
        # alone that is 39 sends the detector cannot see as one send.
        #
        # The same two-step the live path uses, and for the same reason its
        # own comment gives: `apply_touch`'s INSERT column list is part of
        # `coverage_domain`'s contract, and widening the pure engine for a
        # column only the web app cares about would put a view concern inside
        # the ratchet. It returns the row id; Django stamps the subject on.
        if proposal.thread_subject and result.touch_id:
            Touch.objects.for_user(user).filter(id=result.touch_id).update(
                subject=proposal.thread_subject[:255]
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
