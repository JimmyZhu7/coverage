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
one of two things: a sender on a known ATS domain, or a message the bulk
classifier already flagged (or a no-reply sender) whose subject is about an
application. A genuine human reply is neither, and never reaches the rest of
this module.

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
    get in through the subject gate instead)."""
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
# `rejected` and `offer` are read from the subject AND the snippet, because
# they are body-shaped: the classic rejection subject is the neutral "Your
# application to X". The phrases below are correspondingly strict — every one
# of them is a sentence that CANNOT occur in a confirmation. In particular a
# bare "unfortunately" is not on this list: confirmations say "unfortunately
# we cannot respond to every applicant", and closing a live application on
# that word would be the worst mistake this module could make.
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
#   video_interview -> interview. An on-demand HireVue is an interview in the
#   only sense the funnel means: the firm has moved you into its interview
#   process. No under-claim needed.
TARGET_STATUS = {
    ApplicationEvent.APPLIED: "submitted",
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
EVENT_LABELS = {
    ApplicationEvent.APPLIED: ("Application received", "Mark applied"),
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


def _snippet_of(finding: dict) -> str:
    """The little preview text Gmail hands back, when the finding carries
    one. Never stored anywhere — read here, and (when the AI layer is on)
    sent to the model to ground a quote against. §10 stands: what lands in
    the database is the subject."""
    return (finding.get("snippet") or "")[:600]


def detect(finding: dict, *, allow_ai: bool = True) -> Detection:
    """Classify one finding as an application-status message, or not.

    Cheap tests first, and the expensive one only ever last:
      1. Is the sender an ATS/assessment/video vendor, or is this a bulk or
         no-reply message whose subject is about an application? If neither,
         stop — no gate, no phrases, no model.
      2. Do the phrase lists type it? If so, done, free.
      3. Otherwise, and only otherwise, ask the model (if one is configured).
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

    # -- gate ---------------------------------------------------------- #
    if kind is None:
        if not (machine and about_application):
            return Detection()
        reasons = ("automated sender, application subject",)
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
    body_text = f"{subject}\n{snippet}"
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
        return Detection(True, best, "rules", reasons + (f"phrasing says {best}",))

    # -- the long tail ------------------------------------------------- #
    if allow_ai:
        from directory import ai_extract

        guess = ai_extract.extract_application_event_ai(subject, snippet)
        if guess is not None and guess.value in TARGET_STATUS:
            return Detection(
                True, guess.value, "ai", reasons + ("classified by AI",)
            )
    return Detection(True, None, "rules", reasons + ("kind unclear",))


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

    detection = detect(finding, allow_ai=allow_ai)
    if not detection.gated:
        return Outcome()

    subject = (finding.get("subject") or "").strip()
    if detection.event_type is None:
        return Outcome(
            UNRESOLVED,
            f"application mail we could not type: {subject[:120]}",
            detection.reasons,
        )

    resolver = resolver or Resolver(user)
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
    if not _advances(user, opportunity, target):
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
        )
    return Outcome(
        PROPOSED,
        f"{firm.name} — {opportunity.title}: {event_label(detection.event_type)} "
        f"({match.reason}) — proposed for your confirm",
        detection.reasons,
    )


def _occurred_at(finding: dict):
    raw = (finding.get("occurred_at") or "").strip()
    if not raw:
        return None
    from django.utils.dateparse import parse_datetime

    when = parse_datetime(raw)
    if when is not None and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.utc)
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
