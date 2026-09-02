"""Is this inbound message actually a REPLY to me, or a blast that happens
to be addressed at me?

WHY THIS MODULE EXISTS
----------------------
`gmail_live._classify_message` used to answer that question with one line:
anything inbound that isn't a bounce is `replied: True`. That single default
is what put Caroline Baenen (Manager, Talent Acquisition, West Monroe) at
warmth `replied` / thread_state `replied` on the founder's live account on
the strength of a mass programme invitation — "As a participant in West
Monroe's Sophomore Series, we'd love to invite you to an exclusive
conversation..." — a message written to a list, not to him. The CRM read
that as "this person answered you", the Today queue read the warmth, and
proposed he ask a stranger for a coffee chat. In the founder's words:
"emails that weren't even designed to be a coffee chat, you're proposing to
me to do a coffee chat with, and that's not okay."

DETERMINISTIC ONLY — the same rule the rest of this pipeline follows
--------------------------------------------------------------------
`gmail_live.py`'s module docstring is explicit that it does no LLM
classification, and this module keeps that promise. Every signal below is
either an RFC-defined header that machine-generated and list mail is
*supposed* to carry (RFC 2369 `List-*`, RFC 2919 `List-Id`, RFC 3834
`Auto-Submitted`, RFC 8058 `List-Unsubscribe-Post`, the de-facto
`Precedence:`), a sender address a human cannot be reached at
(`no-reply@`), or the message's own recipient shape. Free, exact, no
network call beyond the message Gmail already handed us, and it never
guesses from prose.

THE ESCAPE HATCH, and why it is `In-Reply-To`
----------------------------------------------
Erring wrong in the "bulk" direction is worse than erring wrong in the
"genuine" direction: mislabelling a real banker's reply silences a real
relationship, while mislabelling a blast as a reply produces noise the user
can see and ignore. So a message that carries `In-Reply-To` or `References`
is treated as a genuine reply even when it also carries list headers — that
is the case of a recruiter typing a real answer from a corporate system
that stamps `List-Unsubscribe` on everything it sends.

That works because the two populations separate cleanly on this header:
every mail client sets `In-Reply-To` when a human hits Reply, and a
mail-merge blast is a fresh message that sets neither. `threadId != id`
(Gmail's own threading) is deliberately NOT used as the escape hatch: Gmail
will thread a second blast under the first one on subject alone, so it
would hand every recurring newsletter a free pass.

Two signals override the escape hatch anyway, because they describe mail
that is machine-generated *within* a thread:
  - `Auto-Submitted:` anything but `no` (RFC 3834) — an out-of-office
    responder is in-thread and is not a person answering you.
  - a `no-reply@` sender — nobody is reading the other end.

"A MACHINE SENT IT" IS NOT "A LIST GOT IT" (2026-09-01)
--------------------------------------------------------
The verdict above is deliberately blunt about `Auto-Submitted`, and that
blunt reading threw away the single most valuable message in the mailbox.
Google Calendar stamps `Auto-Submitted: auto-replied` on the mail it sends
when someone accepts an invitation, so every "Accepted: Coffee Chat @ ..."
a counterparty ever sent the founder was classified as bulk and reached
`apply_findings` with `chat_status: "none"` — five of the six `review`
MailFacts on his live account (read-only, 2026-09-01) are exactly that
message, unread. The verdict is still correct as a verdict: no PERSON typed
that mail. What was missing is that the message carries a structured `.ics`
stating, in iTIP's own vocabulary, that a named person accepted an invite
the student themselves organised — evidence, not prose.

So the fix is not to soften this test. `capture.gmail_live._ics_rsvp` reads
the invite and overrides the verdict on the strength of the `.ics`, and
`machine_only` below is the one fact it needs back from here: whether the
bulk verdict rests only on "software composed this" or also on "software
sent this to a list". The West Monroe blast the module exists to stop is a
list send and stays bulk under every rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses

# --------------------------------------------------------------------------- #
# The kind an inbound-but-bulk message is recorded as.
#
# Imported from coverage_domain so there is exactly one spelling of it, and
# so this module's callers never have to know whether the ratchet knows
# about it. `TOUCH_TRANSITIONS[BULK_RECEIVED_KIND]` is `(None, None)`: the
# touch is logged, the message is kept, and warmth/thread_state do not move.
# --------------------------------------------------------------------------- #
from coverage_domain.pipeline import BULK_RECEIVED_KIND  # noqa: F401  (re-export)

# A localpart nobody reads. Deliberately narrow — `info@`, `careers@` and
# `recruiting@` are all addresses a human genuinely answers from, and
# matching those would silence exactly the relationships this product
# exists to track.
_NOREPLY_LOCALPART_RE = re.compile(
    r"^(?:no[._-]?reply|do[._-]?not[._-]?reply|donotreply|noreply|"
    r"bounce[sd]?|mailer[._-]?daemon|postmaster|notifications?|automated)"
    r"(?:[._+-].*)?$",
    re.IGNORECASE,
)

# RFC 2369 / RFC 2919. The presence of ANY of these means the message was
# sent by list software. `List-Unsubscribe` is handled separately below: it
# is the one header that legitimate one-to-one corporate mail systems also
# stamp on personal messages, so it is not on its own decisive.
_LIST_HEADERS = ("list-id", "list-post", "list-archive", "list-help", "list-owner")

# RFC 8058 pairs with List-Unsubscribe; both are marketing-shaped rather
# than list-shaped, so both live in the "strong but overridable" tier.
_UNSUBSCRIBE_HEADERS = ("list-unsubscribe", "list-unsubscribe-post")

# Bulk-sender / campaign identifiers. `Feedback-ID` is what Google's own
# Postmaster Tools asks bulk senders to stamp; the rest are the major ESPs'
# per-campaign ids. None of these ever appear on a message a person typed.
_CAMPAIGN_HEADERS = (
    "feedback-id",
    "x-campaign-id",
    "x-campaignid",
    "x-mailgun-sid",
    "x-sg-eid",
    "x-mandrill-user",
    "x-marketing-campaign",
    "x-mailchimp-campaign-id",
    "x-sfmc-stack",
)

# De-facto (never standardised, universally honoured): a sender declaring
# its own mail low-priority/automated.
_BULK_PRECEDENCE = {"bulk", "list", "junk"}

# Auto-reply subject prefixes, for the responders that carry no RFC 3834
# header at all. Deliberately anchored to the START of the subject and kept
# to the stock strings mail systems themselves prepend ("Automatic reply:"
# is Exchange's, "Out of Office" is the older Outlook's) — a human writing
# "following up on the auto reply I got" must never match. Verified against
# the founder's live mailbox (2026-08-24, read-only): Exchange's
# "Automatic reply: USC | Redwood | Allen & Company - ..." is exactly this
# shape (that one ALSO carried `Auto-Submitted: auto-generated`; this regex
# is for the systems that don't).
_AUTOREPLY_SUBJECT_RE = re.compile(
    r"^\s*(?:automatic reply|auto[- ]?reply|autoreply|auto response|"
    r"out of (?:the )?office(?:\s+auto(?:-| )?reply)?|ooo)\b"
    r"(?:\s*[:\-]|\s*$)",
    re.IGNORECASE,
)

# How many people have to be on To:+Cc: before the message stops looking
# like it was written to one person. Six, not two: a genuine intro email
# ("Jimmy, meet Sarah; Sarah, meet Jimmy") legitimately carries three or
# four, and a real thread accumulates a few more as people are looped in.
BULK_RECIPIENT_COUNT = 6


@dataclass(frozen=True)
class InboundVerdict:
    """What one inbound message is, and why.

    `reasons` is never empty when `is_bulk` is True — it is what the touch
    note records and what the reclassification report prints, so a
    demotion is always explainable to the person whose CRM it changed.
    """

    is_bulk: bool
    reasons: tuple[str, ...] = ()
    # True when the message carries an RFC reply pointer (`In-Reply-To` or
    # `References`), i.e. someone hit Reply on something. Reported even for
    # a bulk verdict, because it is the fact the override rules turn on.
    threaded_reply: bool = False
    # True when the account owner is actually on To:/Cc: (or the headers
    # named nobody at all, which proves nothing either way and defaults
    # open). A reply pointer says "someone hit Reply"; it does NOT say they
    # hit Reply on something the user sent — a colleague replying-all into
    # a thread the user was only ever Bcc'd or list-delivered into carries
    # In-Reply-To just the same. Verified on the founder's live mailbox
    # (2026-08-25, read-only): a West Monroe coordinator's "RE:" follow-up
    # to their own mass invite threaded onto the first blast and named only
    # the firm's own people on To:/Cc: — a reply, and not to him. Tier 3
    # already treats "not on To:/Cc:" as half a bulk signal; this reports
    # the same fact on its own so a stricter surface (contact discovery)
    # can make it decisive without re-parsing headers.
    addressed_to_user: bool = True
    # True when the message declares ITSELF machine-answered: `Auto-Submitted:`
    # anything but `no` (RFC 3834), an `X-Autoreply`/`X-Autorespond` header, or
    # a stock auto-reply subject prefix ("Automatic reply:"). Strictly narrower
    # than `is_bulk` — a newsletter is bulk and not this — and reported so the
    # auto-reply reader (`capture.mailfacts`) can gate on "the counterparty's
    # OWN mailbox answered by machine" without re-parsing headers. An
    # auto-reply is the one bulk message whose BODY is worth reading: it is
    # where "no longer with the firm", "please contact X at Y" and "back on
    # September 2" live. Deliberately LAST in the field list: existing call
    # sites construct this positionally.
    auto_submitted: bool = False
    # True when the ONLY thing that made this message bulk is that a machine
    # composed it (`Auto-Submitted:`, `X-Autoreply`, a stock auto-reply
    # subject) — no list headers, no unsubscribe headers, no campaign id, no
    # blast-shaped recipient list. It is the difference between "software
    # sent this" and "software sent this TO A LIST", and only the first one
    # describes a calendar server relaying one person's invite to one other
    # person. `capture.gmail_live` reads it to decide whether an iTIP
    # `METHOD:REQUEST` may still be read as a chat someone proposed: a
    # programme blast carries `List-Unsubscribe` (the live West Monroe case
    # this module was written around) and so is never machine-only, while
    # Google Calendar's own invitation mail carries `Auto-Submitted` and
    # nothing else. Bulk-only: False whenever `is_bulk` is False.
    machine_only: bool = False

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


def _headers_map(message: dict) -> dict[str, str]:
    """Every header on the message, lower-cased name -> value. Last value
    wins for a repeated header, which is fine: every rule below only ever
    asks "is this present" or tests one canonical value."""
    out: dict[str, str] = {}
    for header in message.get("payload", {}).get("headers", []) or []:
        name = (header.get("name") or "").strip().lower()
        if name:
            out[name] = (header.get("value") or "").strip()
    return out


def looks_like_noreply(address: str) -> bool:
    """True for an address no human reads. Matches the LOCALPART only —
    matching the domain would catch every message from, say,
    `noreply-mail.example.com`'s legitimate users."""
    address = (address or "").strip().lower()
    if "@" not in address:
        return False
    localpart = address.split("@", 1)[0]
    return bool(_NOREPLY_LOCALPART_RE.match(localpart))


def _recipient_addresses(headers: dict[str, str]) -> list[str]:
    raw = ", ".join(v for v in (headers.get("to", ""), headers.get("cc", "")) if v)
    return [addr.lower() for _, addr in getaddresses([raw]) if addr]


def classify_inbound(own_email: str, message: dict) -> InboundVerdict:
    """Decide whether one INBOUND Gmail message is a genuine reply.

    Callers must have established the message is inbound already (this
    never looks at direction) and that it is not a bounce — a bounce has
    its own, older branch in `gmail_live._classify_message` and a different
    meaning.

    The three tiers, in the order they are applied:

    1. **Always bulk.** A `no-reply@`-style sender, `Auto-Submitted:`
       anything but `no`, `Precedence: bulk|list|junk`, or any RFC 2369 /
       2919 `List-*` header. None of these can describe a person writing to
       you, in a thread or out of one, so `In-Reply-To` does not rescue
       them.
    2. **Strong, but overridable.** `List-Unsubscribe`,
       `X-Auto-Response-Suppress`, or an ESP campaign id. Marketing shape —
       but corporate mail systems stamp some of these on genuine one-to-one
       mail too, so a message that is an actual reply keeps its status.
    3. **Weak, and only ever in pairs.** The recipient shape: the account
       owner not being on `To:`/`Cc:` at all, and more than
       `BULK_RECIPIENT_COUNT` people on them. Either alone describes plenty
       of genuine mail (a BCC'd intro, a wide thread); together they
       describe a blast. Also overridable by a real reply pointer.
    """
    headers = _headers_map(message)
    own = (own_email or "").strip().lower()

    threaded = bool(headers.get("in-reply-to") or headers.get("references"))
    all_recipients = _recipient_addresses(headers)
    # Open by default: empty To:/Cc: or an unknown own address is absence of
    # evidence, not evidence of absence.
    addressed = (not all_recipients) or (not own) or (own in all_recipients)

    # --- tier 1: nothing rescues these ------------------------------------ #
    always: list[str] = []
    # The subset of `always` that says "a machine composed this" rather than
    # "this went to a list". See `InboundVerdict.machine_only`.
    machine: list[str] = []

    _, from_addr = ("", "")
    from_pairs = getaddresses([headers.get("from", "")])
    if from_pairs:
        from_addr = from_pairs[0][1]
    if looks_like_noreply(from_addr):
        always.append(f"sender is an unattended address ({from_addr})")

    auto_submitted = headers.get("auto-submitted", "").split(";")[0].strip().lower()
    is_auto_reply = False
    if auto_submitted and auto_submitted != "no":
        always.append(f"Auto-Submitted: {auto_submitted}")
        machine.append(always[-1])
        is_auto_reply = True

    # The header-less responders. `X-Autoreply`/`X-Autorespond` are the
    # de-facto headers some vacation responders stamp instead of RFC 3834's;
    # the subject prefix covers the systems that stamp nothing at all. All
    # tier 1 for the same reason `Auto-Submitted` is: a machine answered, in
    # a thread, and it is not a person answering you — before this test an
    # OOO with no RFC header fell straight through to `replied: True` and
    # inflated warmth off a vacation responder.
    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        always.append("X-Autoreply (vacation responder)")
        machine.append(always[-1])
        is_auto_reply = True
    subject = headers.get("subject", "")
    if _AUTOREPLY_SUBJECT_RE.match(subject):
        always.append("auto-reply subject prefix")
        machine.append(always[-1])
        is_auto_reply = True

    precedence = headers.get("precedence", "").strip().lower()
    if precedence in _BULK_PRECEDENCE:
        always.append(f"Precedence: {precedence}")

    present_list = [h for h in _LIST_HEADERS if headers.get(h)]
    if present_list:
        always.append("mailing-list headers (" + ", ".join(present_list) + ")")

    # --- tier 2: strong, overridden by a genuine reply pointer ------------- #
    #
    # Computed BEFORE tier 1 returns, though it is only USED after: a message
    # can carry both an `Auto-Submitted` and a `List-Unsubscribe`, and the
    # tier-1 early return meant the list evidence was never even looked at.
    # That is the difference between "a calendar server relayed one person's
    # invite" and "a marketing system sent a calendar invite to a list", and
    # `machine_only` is exactly the fact that distinguishes them.
    strong: list[str] = []

    present_unsub = [h for h in _UNSUBSCRIBE_HEADERS if headers.get(h)]
    if present_unsub:
        strong.append("unsubscribe headers (" + ", ".join(present_unsub) + ")")

    if headers.get("x-auto-response-suppress"):
        strong.append("X-Auto-Response-Suppress (machine-generated)")

    present_campaign = [h for h in _CAMPAIGN_HEADERS if headers.get(h)]
    if present_campaign:
        strong.append("bulk-campaign headers (" + ", ".join(present_campaign) + ")")

    # --- tier 3: weak, and only ever counted in pairs ---------------------- #
    weak: list[str] = []
    recipients = all_recipients
    if recipients and own and own not in recipients:
        weak.append("you are not on To: or Cc:")
    if len(recipients) > BULK_RECIPIENT_COUNT:
        weak.append(f"{len(recipients)} people on To:/Cc:")

    if always:
        return InboundVerdict(
            True,
            tuple(always),
            threaded,
            addressed,
            auto_submitted=is_auto_reply,
            # Machine-composed and nothing else: every tier-1 reason is an
            # auto-reply signal, and no weaker tier found list, campaign or
            # blast-shaped evidence either.
            machine_only=(
                len(machine) == len(always) and not strong and len(weak) < 2
            ),
        )

    reasons = strong + (weak if len(weak) >= 2 else [])
    if reasons and not threaded:
        return InboundVerdict(True, tuple(reasons), threaded, addressed)

    return InboundVerdict(False, (), threaded, addressed)
