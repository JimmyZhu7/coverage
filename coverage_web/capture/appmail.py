"""Application mail — read the ATS, PROPOSE a pipeline move, never make one.

WHY THIS MODULE EXISTS
----------------------
`directory/management/commands/capture_applications.py` already argued the
case and it has not changed: My Applications was a pipeline nobody used —
the founder's own account tracked zero roles — because it competed with the
ATS's own tracking and lost. Nobody who has just finished a twenty-minute
Workday form comes back to click Save and later drag a card to "Applied".
The evidence lands in the mailbox instead, and Coverage already reads that
mailbox.

Two things that command could not do, and this module does:

1. **It had no detector.** Its findings came from a human running an agent
   over the inbox and handing it a JSON file. Everything below is the
   deterministic replacement — ATS sender domains and the phrases those
   systems actually send — running on the same inbound stream
   `capture.inbound` already classifies.
2. **It had no gate.** It wrote `UserOpportunity.applied_status` directly.
   Here, detection writes an `ApplicationEvent` — pending, user-confirmable
   — and only `accept()` below ever touches the pipeline. That is the
   Limited Use posture the whole B2B plan depends on: mail read on a
   student's behalf may propose, and only the student's tap may change
   their record.

COMPOSING WITH `capture.inbound`, NOT FIGHTING IT
-------------------------------------------------
Application mail IS bulk mail. It comes from `no-reply@`, it carries
`List-Unsubscribe` and campaign ids, and `classify_inbound` correctly calls
it bulk — which is precisely what makes it findable. So the gate below wants
one of three things: a sender on a known ATS domain; a message the bulk
classifier already flagged (or a no-reply sender) whose SUBJECT is about an
application; or an unattended address on a firm's OWN domain, where the
subject may be silent and the body carries the signal. A genuine human reply
is none of the three, and never reaches the rest of this module.

WHY THE THIRD ARM EXISTS — a real miss, 2026-08-24
---------------------------------------------------
`noreply@campuscareers.bofa.com` sent "Bank of America Action Required:
Indicate Your Top Choices for Campus Insight Forum". Body: "Bank of America
Application Update / Congratulations on advancing in the Campus Insight Forum
process! To move forward, you must complete the Program Preference Survey by
August 30, 2026 at 11:59 PM EST."

Two arms both refused it. `campuscareers.bofa.com` is not an ATS vendor, and
the subject carries no application word — the words are all in the body. So
an advancement notice from the bank's own campus-recruiting system, naming a
hard deadline six days out, was filed as bulk noise. That is precisely the
failure this module exists to prevent.

THE ARM IS THE SENDER PLUS THE WORD, NOT THE SENDER ALONE. It would have been
easier to gate on the sender by itself: an unattended address on a domain the
directory registers for a firm is a strong signal on its own. It is also
exactly the address a bank mails its marketing from, and gating on it alone
would hand every "an evening with our traders" blast to the paid model for a
guaranteed "not an application event". So the arm keeps the application-word
test and only changes its SCOPE: for a firm's own unattended address, the
word may come from the body. Nothing else about the gate moves, and marketing
with no application word anywhere is refused exactly as before.

A gate is not a verdict, either way. Everything that gets through still has to
be TYPED by the phrase layer below, and a message the phrases cannot type
produces a report line and no row.

THE DETERMINISTIC LAYER IS THE PRODUCT. THE LLM IS THE LONG TAIL.
------------------------------------------------------------------
`_detect` is exact-match work: a suffix test on the sender's domain and a
list of phrases these systems send verbatim, in the millions. It costs
nothing, runs on every message, and classifies the great majority. Only a
message that PASSES the gate and that the phrases cannot type is handed to
`directory.ai_extract.extract_application_event_ai` — which is itself dark
unless `ANTHROPIC_API_KEY` is configured, exactly like every other AI path
in this codebase. No key, no calls, and the feature still works.

The grounding rule is inherited whole from `ai_extract`: the model must quote
the sentence it read its answer from, verified as a real substring of the
text it was given, or the answer is discarded. And unlike the deadline
extractor, the quote is used to VERIFY and then thrown away — §10's "no
email bodies" rule means what an `ApplicationEvent` stores is the SUBJECT,
at most, the same reading `capture.discovery._evidence_line` takes.

WHAT IT REFUSES TO DO, inherited from `directory.applications`
--------------------------------------------------------------
- Guess between two believable roles. `match_application` advances only when
  exactly one candidate is credible; everything else is counted and reported
  and never carded. An application wrongly marked submitted is worse than
  one not marked at all — it tells a student a form is done that isn't.
- Move a row backwards, or sideways into a stage the mail doesn't support.
  See `TARGET_STATUS` for the mapping and the one place it deliberately
  under-claims.
- Ask twice. One row per (user, role, event kind) — the ATS's own reminder
  about an application it already confirmed resolves to the same triple.
- Infer a date. `_due_on` reads a deadline only where the mail STATES one, in
  words, with a year. "By Friday", "within 5 days", "08/30" — all refused.

THE ONE THING A STAGE CANNOT SAY
--------------------------------
`_advances` exists because a card that would change nothing is noise: the
ATS re-confirming an application the board already has is exactly the mail a
student should not be asked about twice. But the BofA message above changes
no stage at all — the founder had already marked that role submitted by hand
— and it is still the most important message in the batch, because it names
a dated thing he must DO. So the suppression tests the stage AND the date:
an event carrying a deadline the board has no way of knowing says something
the stage does not, and is carded even when the stage stands still.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone as dt_timezone

from django.utils import timezone

from capture import inbound
from capture.models import ApplicationEvent

# Outcomes `consider_finding` reports to the caller's counters.
PROPOSED = "proposed"
UNRESOLVED = "unresolved"
ALREADY_AHEAD = "already_ahead"


# --------------------------------------------------------------------------- #
# The gate: who sends application mail
# --------------------------------------------------------------------------- #
#
# Suffix-matched, so `us.greenhouse-mail.io` and `mail.smartrecruiters.com`
# both hit their parent. These are the systems, not the employers — a firm
# that mails from its own domain is caught by the subject gate instead.
#
# Deliberately overlapping with `capture.discovery._TRANSACTIONAL_DOMAIN_
# SUFFIXES` rather than importing it: that list exists to REFUSE (a sender
# that is infrastructure is not a person to track), this one exists to ADMIT
# (a sender that is an ATS is exactly whose mail we want to read), and the
# two lists will drift apart as this one grows assessment vendors that list
# has no reason to know about.
_ATS_DOMAINS = (
    # Applicant tracking systems
    "greenhouse.io", "greenhouse-mail.io", "greenhousemail.io",
    "myworkday.com", "myworkdayjobs.com", "myworkdaysite.com", "workday.com",
    "lever.co", "hire.lever.co",
    "icims.com",
    "smartrecruiters.com", "smartrecruiters.net",
    "ashbyhq.com",
    "successfactors.com", "successfactors.eu", "sapsf.com",
    "taleo.net", "taleo.com",
    "brassring.com", "kenexa.com",
    "tal.net", "oleeo.com",
    "avature.net",
    "jobvite.com",
    "workable.com",
    "breezy.hr",
    "teamtailor.com",
    "eightfold.ai",
    "phenompeople.com",
    "paradox.ai",
    "recruitics.com",
    "gr.hs-sites.com",
)

# Vendors whose ENTIRE business is one stage of the funnel. A message from
# them is that stage's invite by sender alone — the subject is a bonus.
_ASSESSMENT_DOMAINS = (
    "hackerrank.com", "hackerrankforwork.com",
    "codility.com",
    "codesignal.com",
    "karat.io",
    "pymetrics.com",
    "shl.com", "shldirect.com",
    "cut-e.com", "aon.com",
    "plum.io",
    "modernhire.com",
    "testgorilla.com",
)
_VIDEO_INTERVIEW_DOMAINS = (
    "hirevue.com",
    "sparkhire.com",
    "willo.video",
)

# The word that makes a subject about an application at all. Required even
# for an ATS sender: Greenhouse and Workday also send job alerts and
# newsletters, and a gate that admitted those would hand every one of them
# to the paid model for a guaranteed "not an application event".
_APPLICATION_WORD_RE = re.compile(
    r"\b(?:applicat\w*|apply|applying|applied|candidacy|candidate|"
    r"interview\w*|assessment|offer|recruit\w*|hiring)\b",
    re.IGNORECASE,
)


def _domain_of(email: str) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower()


def _suffix_match(domain: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        if domain == suffix or domain.endswith("." + suffix):
            return suffix
    return None


def sender_kind(email: str) -> str | None:
    """Which kind of system this address belongs to: "assessment", "video",
    "ats", or None for everything else (including a firm's own domain —
    plenty of banks mail confirmations from `careers@firm.com`, and those
    get in through the subject gate, or through `firm_own_sender` below)."""
    domain = _domain_of(email)
    if not domain:
        return None
    if _suffix_match(domain, _ASSESSMENT_DOMAINS):
        return "assessment"
    if _suffix_match(domain, _VIDEO_INTERVIEW_DOMAINS):
        return "video"
    if _suffix_match(domain, _ATS_DOMAINS):
        return "ats"
    return None


def firm_own_sender(email: str, firm_domains=None) -> bool:
    """Is this an unattended address on a domain the DIRECTORY already
    registers for a firm?

    Both halves matter and neither is enough alone. `looks_like_noreply` is
    `capture.inbound`'s own localpart test — the same one that makes a
    message bulk — so a human at the bank is never this. And the domain test
    is `capture.discovery.FirmDomains`, reused rather than re-derived: it
    already resolves a subdomain to its registered parent (`mail.jpmorgan.com`
    to a registered `jpmorgan.com`, and `campuscareers.bofa.com` to `bofa.com`)
    and never the reverse, so a registered subdomain cannot claim a parent it
    was not given.

    Deliberately NOT a hardcoded list of campus-recruiting hostnames. Every
    bank spells its own differently (`campuscareers.`, `campus.`, `recruiting.`,
    `earlycareers.`) and a list of them would be permanently one bank behind.
    The directory's firm domains are already maintained for other reasons and
    are the same fact stated once.
    """
    if not inbound.looks_like_noreply(email):
        return False
    if firm_domains is None:
        from capture.discovery import FirmDomains

        firm_domains = FirmDomains()
    return firm_domains.match(email) is not None


# --------------------------------------------------------------------------- #
# The phrase layer
# --------------------------------------------------------------------------- #
#
# SCOPE MATTERS, and it is the whole safety design here.
#
# `applied`, `assessment`, `video_interview` and `interview` are read from the
# SUBJECT only. Those are subject-shaped events — the ATS puts them in the
# subject line because that is what the mail is for — and reading them out of
# body prose is how "if you progress, you will be invited to an online
# assessment" turns a confirmation into an interview invite.
#
# `rejected`, `offer` and `advanced` are read from the subject AND the
# snippet, because they are body-shaped: the classic rejection subject is the
# neutral "Your application to X", and the BofA advancement notice's subject
# was "Action Required: Indicate Your Top Choices". The phrases below are
# correspondingly strict — every one of them is a sentence that CANNOT occur
# in a confirmation. In particular a bare "unfortunately" is not on this list:
# confirmations say "unfortunately we cannot respond to every applicant", and
# closing a live application on that word would be the worst mistake this
# module could make.
#
# WHY `advanced` IS SAFE TO READ FROM PROSE AND `interview` IS NOT. The danger
# the scope rule guards against is CONDITIONAL prose: "if you progress, you
# will be invited to an online assessment" sits in the body of half the
# confirmations ever sent, and reading it as an invite turns a confirmation
# into an interview. The `advanced` phrases below are second-person
# assertions in the past or present about THIS candidate, and — this is the
# part that makes the scope safe rather than merely careful — they claim
# `submitted`, the same floor every confirmation claims. A wrong read costs
# nothing a "thank you for applying" would not already have cost. A wrong
# `interview` read invents a stage nobody reached, which is why that one stays
# in the subject.
_SUBJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (ApplicationEvent.APPLIED, r"thank(?:s| you) for applying"),
    (ApplicationEvent.APPLIED, r"thank(?:s| you) for your application"),
    (ApplicationEvent.APPLIED, r"we(?:'ve| have)? received your application"),
    (ApplicationEvent.APPLIED, r"application (?:received|submitted|confirmation|complete)"),
    (ApplicationEvent.APPLIED, r"your (?:recent |online )?application (?:to|for|has been|was)\b"),
    (ApplicationEvent.APPLIED, r"confirmation of your application"),

    (ApplicationEvent.ASSESSMENT, r"\b(?:online|coding|technical|numerical|verbal|situational) assessment\b"),
    (ApplicationEvent.ASSESSMENT, r"\bassessment (?:invitation|invite|link|required)\b"),
    (ApplicationEvent.ASSESSMENT, r"invitation to (?:complete|take)\b"),
    (ApplicationEvent.ASSESSMENT, r"complete (?:your|the|an) (?:online )?(?:assessment|test|exercise)"),
    (ApplicationEvent.ASSESSMENT, r"\b(?:hackerrank|codility|codesignal|pymetrics)\b"),

    (ApplicationEvent.VIDEO_INTERVIEW, r"\bhirevue\b"),
    (ApplicationEvent.VIDEO_INTERVIEW, r"\b(?:on[- ]?demand|video|digital|recorded) interview\b"),

    (ApplicationEvent.INTERVIEW, r"\binterview (?:invitation|invite|request)\b"),
    (ApplicationEvent.INTERVIEW, r"invit(?:ation|e|ing) (?:you )?to (?:an? )?interview"),
    (ApplicationEvent.INTERVIEW, r"schedule your interview"),
    (ApplicationEvent.INTERVIEW, r"your interview (?:is|has been) (?:scheduled|confirmed)"),
    (ApplicationEvent.INTERVIEW, r"assessment cent(?:er|re)"),
    (ApplicationEvent.INTERVIEW, r"\bsuper ?day\b"),
    (ApplicationEvent.INTERVIEW, r"\bfinal round\b"),
)

_BODY_PATTERNS: tuple[tuple[str, str], ...] = (
    # Advancement. Every one of these names the candidate and a completed
    # move; none of them can be read off a conditional. "Congratulations" is
    # never on its own — banks congratulate students on being admitted to a
    # mailing list.
    (ApplicationEvent.ADVANCED,
     r"congratulations on (?:advancing|progressing|moving forward|being selected)"),
    (ApplicationEvent.ADVANCED,
     r"you (?:have |'ve )?(?:been )?(?:advanced|progressed|moved forward) "
     r"(?:to|in|into)\b"),
    (ApplicationEvent.ADVANCED,
     r"advanc(?:ed|ing) to the next (?:stage|round|step|phase)"),
    (ApplicationEvent.ADVANCED,
     r"(?:selected|chosen) to (?:move forward|continue|advance)\b"),
    (ApplicationEvent.ADVANCED,
     r"pleased to (?:inform|share|tell) you that you have (?:advanced|progressed)"),

    (ApplicationEvent.REJECTED, r"regret to inform"),
    (ApplicationEvent.REJECTED, r"not (?:be )?(?:moving|going) forward with your\b"),
    (ApplicationEvent.REJECTED, r"(?:will |have decided )?not (?:to )?(?:be )?(?:progress|proceed|advanc)\w*\s+(?:with )?your\b"),
    (ApplicationEvent.REJECTED, r"no longer under consideration"),
    (ApplicationEvent.REJECTED, r"(?:were|was) not selected"),
    (ApplicationEvent.REJECTED, r"unsuccessful (?:on this occasion|at this (?:time|stage))"),
    (ApplicationEvent.REJECTED, r"(?:move|moving|proceed(?:ing)?) forward with other candidates"),
    (ApplicationEvent.REJECTED, r"will not be (?:moving|progressing|proceeding)"),
    (ApplicationEvent.REJECTED, r"decided not to (?:move|proceed|progress)"),

    (ApplicationEvent.OFFER, r"pleased to offer you"),
    (ApplicationEvent.OFFER, r"offer of (?:employment|an? internship)"),
    (ApplicationEvent.OFFER, r"your offer letter"),
    (ApplicationEvent.OFFER, r"we (?:would like|are delighted) to offer"),
)

_SUBJECT_RES = tuple((kind, re.compile(p, re.IGNORECASE)) for kind, p in _SUBJECT_PATTERNS)
_BODY_RES = tuple((kind, re.compile(p, re.IGNORECASE)) for kind, p in _BODY_PATTERNS)

# Highest wins when more than one fires. An ATS mail that mentions both
# applying and an assessment IS the assessment invite; a mail that both
# thanks you for applying and regrets to inform you is the rejection.
_PRECEDENCE = (
    ApplicationEvent.APPLIED,
    # Above `applied` (it says more than "we got it") and below every named
    # stage (it says less than "here is your assessment"). A mail that both
    # congratulates you on advancing and invites you to a HireVue IS the
    # HireVue invite.
    ApplicationEvent.ADVANCED,
    ApplicationEvent.ASSESSMENT,
    ApplicationEvent.VIDEO_INTERVIEW,
    ApplicationEvent.INTERVIEW,
    ApplicationEvent.REJECTED,
    ApplicationEvent.OFFER,
)


# --------------------------------------------------------------------------- #
# Event kind -> what it may do to the pipeline
# --------------------------------------------------------------------------- #
#
# The pipeline's vocabulary is `directory.views._TRACK_STATES`:
# saved / submitted / interview / offer / closed, and it is read by My
# Applications, the weekly digest, the calendar, the assistant and firm
# merge. It is deliberately NOT extended here — a sixth state would ripple
# through five surfaces to describe one email — so two of the six event
# kinds map onto a coarser stage than their own name, and one of those is a
# deliberate under-claim:
#
#   assessment -> submitted. A HackerRank invite is real forward motion, and
#   "Interviewing" would still be a lie: nobody has interviewed anybody. What
#   it DOES prove beyond doubt is that the application was submitted, so that
#   is what it claims. The precise fact survives on the `ApplicationEvent`
#   row itself, which is kept after accept and is in the data export.
#
#   advanced -> submitted, and this is the second deliberate under-claim.
#   "Congratulations on advancing in the Campus Insight Forum process" is real
#   forward motion and it is NOT an interview: nobody has interviewed anybody,
#   and the mail's own next step was a preference survey. It is not a rejection
#   and it is obviously not an offer. What it proves beyond argument is that
#   the application is live and was submitted, so `submitted` is what it
#   claims — the same argument, for the same reason, as `assessment` above.
#   The precise fact survives on the ApplicationEvent row and in the export;
#   what does NOT survive an over-claim is a student's trust in a board that
#   told them they were interviewing when they were filling in a form.
#
#   video_interview -> interview. An on-demand HireVue is an interview in the
#   only sense the funnel means: the firm has moved you into its interview
#   process. No under-claim needed.
TARGET_STATUS = {
    ApplicationEvent.APPLIED: "submitted",
    ApplicationEvent.ADVANCED: "submitted",
    ApplicationEvent.ASSESSMENT: "submitted",
    ApplicationEvent.VIDEO_INTERVIEW: "interview",
    ApplicationEvent.INTERVIEW: "interview",
    ApplicationEvent.OFFER: "offer",
    # "closed" is the funnel's terminal state, shown as "Done" — and
    # `directory.views` says out loud why it exists: the overwhelmingly
    # common terminal outcome is a rejection, which never produces an offer.
    ApplicationEvent.REJECTED: "closed",
}

# What the CARD says. Sentence case, no em dashes, and — for the one event
# nobody wants to read twice — no repetition of the word the email already
# used. `Not moving forward` states the fact; `Mark done` is an action about
# a list, not a verdict about a person.
#
# `Next step required` rather than the mirror-image `Moving forward`: that
# would sit one word away from `Not moving forward` in the same lane, and the
# two cards a student most needs to tell apart at a glance are exactly those
# two. It also says the useful half — there is something to DO — which is the
# whole reason this card exists when the stage has not moved.
EVENT_LABELS = {
    ApplicationEvent.APPLIED: ("Application received", "Mark applied"),
    ApplicationEvent.ADVANCED: ("Next step required", "Mark applied"),
    ApplicationEvent.ASSESSMENT: ("Assessment invite", "Mark applied"),
    ApplicationEvent.VIDEO_INTERVIEW: ("Video interview invite", "Move to interviewing"),
    ApplicationEvent.INTERVIEW: ("Interview invite", "Move to interviewing"),
    ApplicationEvent.REJECTED: ("Not moving forward", "Mark done"),
    ApplicationEvent.OFFER: ("Offer", "Move to offer"),
}


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, (event_type, "Update"))[0]


def action_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, (event_type, "Update"))[1]


# --------------------------------------------------------------------------- #
# The dated requirement
# --------------------------------------------------------------------------- #
#
# The single most valuable line in the message that started all this was not
# the status at all: it was "by August 30, 2026 at 11:59 PM EST". A stage a
# student can read off their own board; a date they cannot act on after it
# passes is the thing a CRM is for.
#
# READ, NEVER INFERRED — the same confirmed-vs-rumoured discipline
# `directory.models.Opportunity.deadline_precision` draws over postings:
#
#   - Named months only. `August 30, 2026` and `30 August 2026` are read;
#     `08/30/2026` is not, because a numeric date is a guess about which
#     country wrote it and a card that names the wrong day is worse than a
#     card with no day. The one thing that cannot be misread is a spelled
#     month.
#   - The year must be written. "by August 30" would need the year inferred
#     from the message date, and an inference is not a stated deadline.
#   - There must be an obligation word in front of it. "by", "before", "no
#     later than", "due". A date on its own is an event date, a start date, a
#     posted-on date — this module has no way to tell which, so it reads none
#     of them.
#   - The date may not predate the message. A deadline behind the mail that
#     announced it is a misparse, not a deadline.
#
# So "by the end of next week", "within 5 days", "by Friday", "08/30" and a
# bare "August 30, 2026" all attach nothing, and attaching nothing is the
# honest outcome. NO MODEL IS ASKED. The deterministic layer is the product
# here exactly as it is above, and §10's read-then-discard rule cuts harder
# for a date than for a status: what lands in the database is one DateField,
# never the sentence it was read from.
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_OBLIGATION = r"(?:by|before|no later than|due(?:\s+(?:by|on))?|deadline(?:\s+is)?)"

_DUE_RES = (
    # "by August 30, 2026" / "before Aug 30 2026"
    re.compile(
        rf"\b{_OBLIGATION}\s*:?\s+(?:the\s+)?"
        rf"(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"\s*,?\s+(?P<year>20\d{{2}})\b",
        re.IGNORECASE,
    ),
    # "by 30 August 2026" / "no later than 30th September, 2026"
    re.compile(
        rf"\b{_OBLIGATION}\s*:?\s+(?:the\s+)?"
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_ALT})\.?"
        rf"\s*,?\s+(?P<year>20\d{{2}})\b",
        re.IGNORECASE,
    ),
)

# Two years. A stated deadline further out than that is not a deadline this
# mail is asking anybody to meet; it is a misparse or a copyright line.
_MAX_DUE_DAYS = 730


def _due_on(text: str, sent_on: date | None) -> date | None:
    """The stated deadline in `text`, or None. See the rules above."""
    for pattern in _DUE_RES:
        match = pattern.search(text or "")
        if match is None:
            continue
        try:
            found = date(
                int(match.group("year")),
                _MONTHS[match.group("month").lower().rstrip(".")],
                int(match.group("day")),
            )
        except (ValueError, KeyError):
            continue
        floor = sent_on or timezone.localdate()
        if found < floor or found > floor + timedelta(days=_MAX_DUE_DAYS):
            continue
        return found
    return None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Detection:
    """What one message is, and how sure the cheap layer was.

    `event_type` None with `gated` True is the ONLY case that may reach the
    paid model: application mail whose phrasing the list above cannot type.
    """
    gated: bool = False
    event_type: str | None = None
    detected_by: str = "rules"
    reasons: tuple[str, ...] = ()
    # The stated deadline this mail carries, when it states one. Never set on
    # a rejection: a closed application has nothing left to be due.
    due_on: date | None = None


def _snippet_of(finding: dict) -> str:
    """The little preview text Gmail hands back, when the finding carries
    one. Never stored anywhere — read here, and (when the AI layer is on)
    sent to the model to ground a quote against. §10 stands: what lands in
    the database is the subject."""
    return (finding.get("snippet") or "")[:600]


def detect(
    finding: dict, *, allow_ai: bool = True, firm_domains=None
) -> Detection:
    """Classify one finding as an application-status message, or not.

    Cheap tests first, and the expensive one only ever last:
      1. Is the sender an ATS/assessment/video vendor; is this a bulk or
         no-reply message whose SUBJECT is about an application; or is it an
         unattended address on a firm's own domain with an application word
         anywhere in it? If none of the three, stop — no gate, no phrases,
         no model.
      2. Do the phrase lists type it? If so, done, free.
      3. Otherwise, and only otherwise, ask the model (if one is configured).

    `firm_domains` is `capture.discovery.FirmDomains`, shared across a batch
    by `Resolver`. Passing it is an optimization, not a requirement — omitted,
    the third arm builds its own. Either way the map is loaded lazily, so a
    batch with no unattended senders in it never queries for firm domains at
    all.
    """
    email = (finding.get("email") or "").strip().lower()
    subject = (finding.get("subject") or "").strip()
    snippet = _snippet_of(finding)

    # A bounce is about delivery, not about an application; the user's own
    # sent mail is not a status update about it.
    if finding.get("bounced") or finding.get("outreach_sent"):
        return Detection()

    kind = sender_kind(email)
    machine = bool(finding.get("bulk")) or inbound.looks_like_noreply(email)
    about_application = bool(_APPLICATION_WORD_RE.search(subject))
    body_text = f"{subject}\n{snippet}"

    # -- gate ---------------------------------------------------------- #
    if kind is None:
        if machine and about_application:
            reasons = ("automated sender, application subject",)
        elif firm_own_sender(email, firm_domains):
            # THE THIRD ARM (see the module docstring). The sender is the
            # firm's own unattended recruiting address, so the subject is
            # allowed to be silent — but the message still has to be about an
            # application SOMEWHERE, or every marketing blast from the same
            # address would gate in and cost a model call to say so.
            if not _APPLICATION_WORD_RE.search(body_text):
                return Detection()
            reasons = (
                f"unattended address on the firm's own domain "
                f"({_domain_of(email)})",
            )
        else:
            return Detection()
    else:
        # An ATS also sends job alerts and newsletters. Vendors whose whole
        # business is one funnel stage don't, so they skip this test.
        if kind == "ats" and not about_application:
            return Detection()
        reasons = (f"sender is a known {kind} system ({_domain_of(email)})",)

    # -- phrases ------------------------------------------------------- #
    hits: set[str] = set()
    for event_type, pattern in _SUBJECT_RES:
        if pattern.search(subject):
            hits.add(event_type)
    for event_type, pattern in _BODY_RES:
        if pattern.search(body_text):
            hits.add(event_type)

    # A vendor whose only product is one stage says that stage by existing.
    if kind == "assessment":
        hits.add(ApplicationEvent.ASSESSMENT)
    elif kind == "video":
        hits.add(ApplicationEvent.VIDEO_INTERVIEW)

    if hits:
        best = max(hits, key=_PRECEDENCE.index)
        return Detection(
            True, best, "rules", reasons + (f"phrasing says {best}",),
            _due_for(best, body_text, finding),
        )

    # -- the long tail ------------------------------------------------- #
    if allow_ai:
        from directory import ai_extract

        guess = ai_extract.extract_application_event_ai(subject, snippet)
        if guess is not None and guess.value in TARGET_STATUS:
            return Detection(
                True, guess.value, "ai", reasons + ("classified by AI",),
                _due_for(guess.value, body_text, finding),
            )
    return Detection(True, None, "rules", reasons + ("kind unclear",))


def _due_for(event_type: str, body_text: str, finding: dict) -> date | None:
    """The stated deadline, for the event kinds that can have one.

    A rejection is excluded by name, not by luck: a closed application has
    nothing left that is due, and a "respond by" sentence in a rejection is
    boilerplate about a survey nobody has to fill in.
    """
    if event_type == ApplicationEvent.REJECTED:
        return None
    when = _occurred_at(finding)
    return _due_on(body_text, timezone.localdate(when) if when else None)


# --------------------------------------------------------------------------- #
# Firm and role resolution
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r"[A-Za-z0-9&.'-]+")


class Resolver:
    """Per-batch caches for the two lookups detection needs.

    Built lazily and at most once per `apply_findings` run, the same shape
    (and for the same reason) as `capture.discovery.FirmDomains`: a batch
    with no application mail in it pays nothing at all.
    """

    def __init__(self, user):
        self.user = user
        self._names = None
        self._domains = None
        self._tracked = None

    # -- firms by name ------------------------------------------------- #
    @property
    def names(self) -> dict:
        """`accounts.services._firm_lookup`'s map, reused verbatim rather
        than re-derived: it is the same normalization the contact CSV import
        matches firm strings with (`&`/`and`/`+` collapsed, legal suffixes
        peeled), and it has been checked collision-free across the whole
        directory."""
        if self._names is None:
            from accounts.services import _firm_lookup

            self._names = _firm_lookup()
        return self._names

    @property
    def domains(self):
        from capture.discovery import FirmDomains

        if self._domains is None:
            self._domains = FirmDomains()
        return self._domains

    @property
    def tracked_ids(self) -> set[int]:
        from analytics.models import UserOpportunity

        if self._tracked is None:
            self._tracked = set(
                UserOpportunity.objects.for_user(self.user).values_list(
                    "opportunity_id", flat=True
                )
            )
        return self._tracked


def _ngram_firm(text: str, names: dict, *, max_words: int = 4):
    """The directory firm named inside `text`, or None.

    Word n-grams rather than a substring scan, because the substring form
    matches on syllables: "Citi" is inside "Citizens", and a card that says
    a student applied to the wrong bank is worse than no card. Each n-gram
    is normalized through the SAME `normalize_firm_name` the lookup was
    built with, so "J.P. Morgan" and "JPMorgan" reach the same key.

    Longest n-gram wins, so "Bain Capital" is never read as "Bain".
    """
    from accounts.services import normalize_firm_name

    words = _WORD_RE.findall(text or "")
    for size in range(min(max_words, len(words)), 0, -1):
        for start in range(0, len(words) - size + 1):
            key = normalize_firm_name(" ".join(words[start:start + size]))
            # Two characters is "HP"; one is noise from a hyphen split.
            if len(key) < 3:
                continue
            firm = names.get(key)
            if firm is not None:
                return firm, " ".join(words[start:start + size])
    return None


def resolve_firm(finding: dict, resolver: Resolver):
    """(Firm, the text it was read from) or (None, ""), cheapest first:
    the sender's own domain, then the display name, then the subject.

    Order is confidence order. A message from `campus@northbank.com` is the
    firm saying so; a firm's name inside a Greenhouse subject line is the
    firm being mentioned, which is nearly always the same thing and
    occasionally isn't.
    """
    email = (finding.get("email") or "").strip().lower()
    firm_id = resolver.domains.match(email)
    if firm_id is not None:
        from directory.models import Firm

        firm = Firm.objects.filter(pk=firm_id).first()
        if firm is not None:
            return firm, _domain_of(email)

    for text in ((finding.get("name") or ""), (finding.get("subject") or "")):
        found = _ngram_firm(text, resolver.names)
        if found is not None:
            return found
    return None, ""


# Subject boilerplate that is about the MESSAGE rather than about the role.
# Stripped before the remainder is offered as a title, so
# `match_application` scores "2027 Investment Banking Summer Analyst" rather
# than "Thank you for applying to 2027 Investment Banking Summer Analyst".
_TITLE_NOISE_RE = re.compile(
    r"(?:thank(?:s| you) for (?:applying|your application)|"
    r"we(?:'ve| have)? received your application|"
    r"application (?:received|submitted|confirmation|complete|update)|"
    r"confirmation of your application|"
    r"your (?:recent |online )?application|"
    r"invitation to (?:complete|take|interview)|"
    r"interview (?:invitation|invite|request)|"
    r"(?:online|coding|technical) assessment|"
    r"next steps?|update on|regarding|"
    r"\b(?:re|fwd)\b)",
    re.IGNORECASE,
)


def role_title(subject: str, firm_text: str) -> str:
    """The part of the subject that might name a role.

    Deliberately sloppy, and safe because of who reads it:
    `directory.applications.title_score` scores only DISTINGUISHING words
    and returns 0.0 when a title carries none, so leftover connective tissue
    contributes nothing rather than mismatching. A subject with no role in
    it at all degrades to the empty string, and `match_application` falls
    through to its "the only role you had saved at this firm" rule.
    """
    text = _TITLE_NOISE_RE.sub(" ", subject or "")
    if firm_text:
        text = re.sub(re.escape(firm_text), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\[\]{}()|:;,–—-]+", " ", text)
    text = re.sub(r"\b(?:to|for|at|the|a|an|your|our|is|has been|was)\b", " ",
                  text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()[:255]


# --------------------------------------------------------------------------- #
# The hook
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    """What one finding did, and the line the sync report prints for it."""
    result: str | None = None
    detail: str = ""
    reasons: tuple[str, ...] = field(default_factory=tuple)


def consider_finding(
    user, finding: dict, *, resolver: Resolver | None = None,
    dry_run: bool = False, allow_ai: bool = True,
) -> Outcome:
    """Run the whole chain over one finding. Writes at most one
    `ApplicationEvent`, and never anything else.

    Called from `capture.gmail.apply_findings` for EVERY finding, before
    contact matching — unlike `capture.discovery`, which hangs off the
    unmatched branch. The difference is deliberate: whether an ATS is in
    somebody's contact book has nothing to do with whether their mail says
    an application moved, and a firm whose `careers@` address a student HAS
    saved as a contact would otherwise be the one case this feature silently
    skipped.
    """
    if not finding.get("found"):
        return Outcome()

    resolver = resolver or Resolver(user)
    detection = detect(
        finding, allow_ai=allow_ai, firm_domains=resolver.domains
    )
    if not detection.gated:
        return Outcome()

    subject = (finding.get("subject") or "").strip()
    if detection.event_type is None:
        return Outcome(
            UNRESOLVED,
            f"application mail we could not type: {subject[:120]}",
            detection.reasons,
        )

    firm, firm_text = resolve_firm(finding, resolver)
    if firm is None:
        return Outcome(
            UNRESOLVED,
            f"application mail from an unknown firm: {subject[:120]}",
            detection.reasons,
        )

    from directory.applications import match_application
    from directory.models import Opportunity

    candidates = list(
        Opportunity.objects.filter(firm=firm, status="open").order_by("title")
    )
    match = match_application(
        candidates, role_title(subject, firm_text),
        tracked_ids=resolver.tracked_ids,
    )
    if not match.matched:
        return Outcome(
            UNRESOLVED,
            f"{firm.name}: {match.reason} — left for you to set by hand",
            detection.reasons,
        )
    opportunity = match.opportunity
    target = TARGET_STATUS[detection.event_type]

    # A card that would change nothing is noise. `may_advance` is the same
    # ratchet `capture_applications` uses, generalized to any target stage:
    # the row's current position already knows at least as much as this
    # email does.
    #
    # UNLESS THE MAIL CARRIES A DATE. A stage says where you are; it cannot
    # say that a preference survey is due on the 30th. The BofA advancement
    # notice landed on a role the founder had already marked submitted by
    # hand, so the stage test alone would have thrown away the deadline along
    # with the card — the same message lost twice, for a different reason the
    # second time. A deadline still ahead of today is something the board
    # genuinely does not know, so it earns the card on its own.
    if not _advances(user, opportunity, target) and not _still_due(detection):
        return Outcome(
            ALREADY_AHEAD,
            f"{firm.name} — {opportunity.title}: already at or past "
            f"{target}, nothing to propose",
            detection.reasons,
        )

    # ONE row per (user, role, kind), whatever its status — the constraint is
    # in the model, and this is the read that keeps a dismissed card from
    # ever coming back.
    existing = ApplicationEvent.objects.for_user(user).filter(
        opportunity=opportunity, event_type=detection.event_type
    ).first()
    if existing is not None:
        return Outcome()

    if not dry_run:
        ApplicationEvent.all_objects.create(
            user=user,
            opportunity=opportunity,
            firm=firm,
            firm_text=(firm_text or firm.name)[:255],
            event_type=detection.event_type,
            target_status=target,
            evidence=subject[:300],
            detected_by=detection.detected_by,
            match_reason=match.reason[:200],
            thread_id=(finding.get("thread_id") or "").strip()[:128],
            occurred_at=_occurred_at(finding),
            due_on=detection.due_on,
        )
    due = f" — due {detection.due_on.isoformat()}" if detection.due_on else ""
    return Outcome(
        PROPOSED,
        f"{firm.name} — {opportunity.title}: {event_label(detection.event_type)}"
        f"{due} ({match.reason}) — proposed for your confirm",
        detection.reasons,
    )


def _still_due(detection: Detection) -> bool:
    """Whether this detection carries a deadline that has not passed."""
    return detection.due_on is not None and detection.due_on >= timezone.localdate()


def _occurred_at(finding: dict):
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


def _rank(status: str) -> int:
    from directory.applications import STAGE_ORDER

    value = (status or "saved").strip().lower() or "saved"
    return STAGE_ORDER.index(value) if value in STAGE_ORDER else 0


def _advances(user, opportunity, target: str) -> bool:
    from analytics.models import UserOpportunity

    row = UserOpportunity.objects.for_user(user).filter(
        opportunity=opportunity
    ).first()
    current = row.applied_status if row is not None else ""
    return _rank(target) > _rank(current)


# --------------------------------------------------------------------------- #
# The tap: accept / dismiss
# --------------------------------------------------------------------------- #

def accept(event: ApplicationEvent):
    """Write the pipeline move this row describes. THE ONLY path from an
    `ApplicationEvent` to a `UserOpportunity`.

    Holds `capture_applications`' three refusals, unchanged:
      - never moves a row backwards (the rank test below);
      - never un-does the user's own "Done";
      - idempotent, so a double-tap or a re-run writes once.

    A row that is no longer an advance (the student got there first, by hand
    or through an earlier card) is still resolved as accepted — the answer
    "yes, that happened" is true, there is simply nothing left to write.
    """
    from analytics.models import UserOpportunity

    if event.status != ApplicationEvent.STATUS_PENDING:
        return None

    row, _ = UserOpportunity.all_objects.get_or_create(
        user=event.user, opportunity=event.opportunity
    )
    if _rank(event.target_status) > _rank(row.applied_status):
        row.applied_status = event.target_status
        # Un-hides a role the student had waved away in the feed. Applying
        # to something is a louder statement than dismissing it was.
        row.dismissed = False
        if row.applied_at is None:
            row.applied_at = event.occurred_at or timezone.now()
        row.save(update_fields=["applied_status", "applied_at", "dismissed"])

    _resolve(event, ApplicationEvent.STATUS_ACCEPTED)
    return row


def dismiss(event: ApplicationEvent) -> None:
    """Never ask about this role-and-event again. The row stays — it IS the
    memory, same contract as `capture.discovery.dismiss`."""
    if event.status != ApplicationEvent.STATUS_PENDING:
        return
    _resolve(event, ApplicationEvent.STATUS_DISMISSED)


def _resolve(event: ApplicationEvent, status: str) -> None:
    event.status = status
    event.resolved_at = timezone.now()
    event.save(update_fields=["status", "resolved_at"])
