"""LLM access for the app: deadline extraction over cached posting text (this
module's original, primary purpose), sponsorship extraction (the same
grounded-quote shape, added for Decision 3's AI pass — see below), plus
`complete_text()`, a thin shared client any other feature (e.g.
`crm.ai_brief`'s coffee-chat briefs) can call rather than each hand-rolling
its own Anthropic API plumbing. One configuration surface, one retry/timeout
policy, one place that goes dark when `ANTHROPIC_API_KEY` is unset.

DEADLINE EXTRACTION, specifically

WHY THIS EXISTS
---------------
Deadline coverage on the live board is 7% (142 of 1,949 open campus rows) —
not because most postings are silent about their deadline, but because the
regex gate in `classify._DEADLINE_KEY` only fires on a closed list of English
phrasings within 80 characters of a fully-specified date. A posting that says
"Priority consideration will be given to applications submitted before the
end of September" or states its date in a structured table the regex window
never reaches is invisible to it. Round 9's own audit found and fixed one such
phrasing gap ("application window is open until <date>") one regex at a time
— that pattern doesn't scale to the long tail.

This module is that long tail's answer: hand the SAME cached `detail_text`
every regex extractor already reads to an LLM, with one narrow question
("does this text state an application deadline, and if so, what exact
sentence says so") instead of a pattern list to maintain by hand.

THE GROUNDING RULE, non-negotiable
-----------------------------------
An LLM that free-associates a plausible-sounding date is worse than one that
says nothing — Coverage's whole pitch is that a chip's evidence is checkable.
So the model is required to return the EXACT sentence it read the date from,
and `_grounded()` verifies that quote is a real substring of the source text
before ANY of its answer is trusted. A quote that doesn't match, an
unparseable date, or a request failure all resolve to "no answer" — the same
posture `_parse_deadline` already takes for a malformed provider field. This
function never invents a deadline; it only finds one already sitting in text
this app has already fetched and stored.

CONFIGURATION
-------------
Reads `ANTHROPIC_API_KEY` from Django settings. Blank/unset (the default,
including in every test and every environment until Jimmy adds a real key)
means `is_configured()` is False and every extraction call below returns
`None` immediately — no network attempt, no crash, same posture as this
project's other optional integrations (EMAIL_URL, SENTRY_DSN, the OAuth
providers in accounts/forms.py): the app boots and every test passes with
nothing configured, and the feature activates the moment a real key lands.

COST
----
Each call sends one cached page's text (already truncated to `MAX_TEXT` by
enrich_postings, so a few thousand characters) to a small, cheap model. This
is priced per API call — real money once a key is added — which is exactly
why this stays a distinct, --limit-able command (`extract_deadlines_ai`) a
human runs deliberately, not something wired into the free `extract_facts`
sweep that already runs on every deploy.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from django.conf import settings

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0
MAX_INPUT_CHARS = 6000  # a posting's cached detail_text is already capped well under this


class AIExtractError(Exception):
    """Raised after exhausting retries on a transient API failure. Wraps the
    last underlying exception, mirroring coverage_connectors.http.FetchError's
    shape so callers can log/skip uniformly."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(f"Anthropic API call failed: {cause}")


@dataclass(frozen=True)
class DeadlineGuess:
    value: str          # ISO date, YYYY-MM-DD — parsed, not merely regex-shaped
    phrase: str          # the exact sentence the model quoted, verified as a substring of the input
    confidence: float    # 0.5 -- see module docstring; deliberately below the 0.6 regex tier


def is_configured() -> bool:
    return bool(getattr(settings, "ANTHROPIC_API_KEY", "") or "")


def _post_json(payload: dict, *, timeout: float, retries: int) -> dict:
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — deliberate plain HTTPS client
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # A 4xx (bad key, bad request, rate limit) will not resolve by
            # retrying identically — surface it immediately, same posture as
            # coverage_connectors.http's unretried 404.
            if e.code < 500:
                raise AIExtractError(e) from e
            last_error = e
        except Exception as e:  # noqa: BLE001 — network/timeout/etc, all retryable
            last_error = e
        if attempt < retries:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise AIExtractError(last_error)


_DEADLINE_PROMPT = """You are reading the text of a job/internship posting. Answer ONLY the question below, from ONLY the text given -- never from general knowledge about the employer or role.

QUESTION: Does this text state an application DEADLINE (the date by which a candidate must submit their application)? This is NOT a start date, an event date, a posted-on date, or a rolling/ongoing basis statement.

If yes, respond with EXACTLY this JSON shape and nothing else:
{"deadline_iso": "YYYY-MM-DD", "quote": "<the exact sentence from the text stating the deadline, copied verbatim, unmodified>"}

If the text does not clearly state a hard application deadline, respond with EXACTLY:
{"deadline_iso": null, "quote": null}

The "quote" field, when present, MUST be an exact, verbatim substring of the text below -- do not paraphrase, summarize, or fix typos in it. If you cannot quote it exactly, answer null instead of guessing.

TEXT:
\"\"\"
{text}
\"\"\"
"""


def _extract_response_text(api_response: dict) -> str:
    blocks = api_response.get("content") or []
    for block in blocks:
        if block.get("type") == "text":
            return block.get("text") or ""
    return ""


def _parsed_object(raw: str) -> dict:
    """The model's reply as the JSON OBJECT every prompt here asks for, or an
    empty dict. Never raises.

    THE BUG THIS REPLACES, which all four grounded extractors had in the same
    shape: each one did `json.loads(raw)` inside a `try` catching
    `(ValueError, TypeError)`, then read `parsed.get(...)` OUTSIDE it. That
    covers a reply that is not JSON, and misses the reply that is PERFECTLY
    VALID JSON but is not an object -- and `null` is the single most likely
    such reply, because every prompt in this module spends a paragraph
    teaching the model to answer with nulls. `json.loads("null")` is `None`,
    `None.get` is an `AttributeError`, and it is raised from a line no
    `except` in this module covers.

    Where that landed mattered more than that it happened. The two board-side
    passes run under management commands that catch `AIExtractError` per row,
    so one `null` ended a paid batch partway through with a stack trace. The
    two mail-side passes run INSIDE a mailbox sync and swallow
    `AIExtractError` precisely so a bad row cannot stop the sync -- an
    `AttributeError` from three lines further down walked straight past that
    and took the sync with it, in a function whose docstring promises "never
    raises". `[]`, `42` and `"a string"` all do the same thing one type over.

    Also unwraps a code fence: models produce one despite the instruction not
    to, and refusing it would throw away a real answer over its packaging.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso_day(value) -> str | None:
    """A model's answer as a bare ISO day in this century, or None.

    Two checks, and both are load-bearing. The `fullmatch` is the CENTURY
    gate -- it is what keeps "1926-10-30", "26-10-30" and a datetime with a
    time on it out. `date.fromisoformat` is the CALENDAR gate: "2026-02-30"
    and "2026-13-01" both satisfy the regex and neither is a day that exists.

    The `isinstance` comes FIRST, because `value` is whatever JSON the model
    emitted: `re.fullmatch` raises `TypeError` on a bool or an int, and
    `True` / `20261030` are both replies a model can produce for a field it
    was told answers with a date or a null.
    """
    if not isinstance(value, str):
        return None
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


#: A quote shorter than this is not a citation, whatever it matches. Every
#: prompt in this module asks for "the exact sentence" that states the fact,
#: and a substring check alone cannot tell a sentence from a fragment: the
#: single character "." is a verbatim substring of almost every posting ever
#: written, and so is the bare year "2027" of any posting that mentions one.
#: Without a floor, a model that answered {"deadline_iso": "2027-01-15",
#: "quote": "2027"} would clear the grounding gate on a page that says
#: "2027 Summer Analyst Program" and nothing about a deadline at all -- a
#: fabricated date wearing a real substring, which is the exact failure the
#: grounding rule exists to make impossible.
#:
#: Twelve characters, measured after whitespace normalization, is below the
#: shortest real deadline sentence this app has seen ("Closes 30/10/2026" is
#: seventeen) and above every fragment a model can reach for by accident. It
#: only ever TIGHTENS the gate: a quote rejected here would have been trusted
#: before, never the other way round.
_MIN_QUOTE_CHARS = 12


# A scraped posting's `detail_text` routinely carries real Unicode
# typography -- an HTML entity like `&#8217;` or `&mdash;` decoded once at
# fetch time, never re-encoded -- and a model asked to quote it "verbatim"
# reliably straightens that punctuation back to ASCII: curly quotes to
# straight ones, an em/en dash to a hyphen, the single ellipsis character to
# three periods. That is not a paraphrase and not a fabricated sentence --
# the words, their order, and their meaning are all exactly the source's --
# but a bare substring check sees a changed character and rejects a true,
# correctly-cited answer the same as it would reject an invented one.
#
# This table is the fix the module's own rule calls for: normalize how the
# comparison reads the SAME mark written two ways, never loosen what counts
# as a match. It only ever widens what compares EQUAL, exactly like the
# whitespace collapse below it -- a quote this table would have rejected
# before still gets rejected; nothing here can turn a paraphrase or an
# invented sentence into a match.
_TYPOGRAPHIC_EQUIVALENTS = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "′": "'",  # quotes -> '
    "“": '"', "”": '"', "″": '"',                # quotes -> "
    "–": "-", "—": "-", "‒": "-",                # dashes -> -
    "…": "...",                                            # ellipsis -> ...
})

# The other half of the em/en dash case: a model just as often reaches for
# the ASCII-art convention (two or three hyphens) as it does a single
# straight one. Collapsing a run of 2+ hyphens to one, after the table
# above has already turned any real dash character into a hyphen too, means
# a real em dash, an en dash, "--" and "---" all normalize to the exact same
# single "-" -- one mark, four spellings a model reaches for when asked to
# copy it in plain ASCII.
_DASH_RUN_RE = re.compile(r"-{2,}")


def _grounded(quote: str | None, source: str) -> bool:
    """The model's quote must appear verbatim in the source text, and must be
    long enough to BE a quote. Whitespace collapse and the typographic
    normalization above are the only ones allowed -- a model that lightly
    reflows a paragraph, straightens a curly quote, or writes a dash as
    "--" while copying it is still quoting it; anything else (a paraphrase,
    a fabricated sentence) will not match."""
    # A non-STRING quote is not a quote, and refusing it here rather than
    # letting `re.sub` decide is the difference between "no answer" and a
    # `TypeError` raised out of a mailbox sync: a model can emit
    # `"quote": ["a", "b"]` or `"quote": 42` for a field it was told holds a
    # sentence, and a non-empty list clears the falsiness check above.
    if not quote or not isinstance(quote, str):
        return False

    def norm(s: str) -> str:
        s = s.translate(_TYPOGRAPHIC_EQUIVALENTS)
        s = _DASH_RUN_RE.sub("-", s)
        return re.sub(r"\s+", " ", s).strip()

    cleaned = norm(quote)
    if len(cleaned) < _MIN_QUOTE_CHARS:
        return False
    return cleaned in norm(source)


def extract_deadline_ai(
    text: str | None,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> DeadlineGuess | None:
    """One posting's cached text in, a grounded deadline guess out (or
    `None`) -- never raises for a "no answer" case, only for a genuine API
    failure (`AIExtractError`), so callers can choose to skip-and-log rather
    than crash a batch run on one bad row."""
    if not is_configured():
        return None
    t = (text or "").strip()
    if not t:
        return None
    t = t[:MAX_INPUT_CHARS]

    # A plain substring replace, not str.format(): the prompt's own JSON
    # examples are full of literal `{`/`}` characters that .format() would
    # try (and fail) to parse as placeholders.
    prompt = _DEADLINE_PROMPT.replace("{text}", t)
    response = _post_json(
        {
            "model": model,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
        retries=retries,
    )
    parsed = _parsed_object(_extract_response_text(response))

    # `_iso_day`, not a bare regex: `DeadlineGuess.value` is documented as
    # "ISO date, YYYY-MM-DD" and its caller assigns it straight to a
    # `DateField`, so a date-SHAPED string naming no real day is exactly the
    # "unparseable date" the module docstring already says resolves to no
    # answer. Validating at the SOURCE makes that sentence true for the next
    # caller too, rather than making each of them re-earn it.
    deadline_iso = _iso_day(parsed.get("deadline_iso"))
    quote = parsed.get("quote")
    if not deadline_iso or not quote:
        return None
    if not _grounded(quote, t):
        return None

    # 0.5, not the regex tier's 0.6: this is a second-pass, LLM-derived
    # answer over the same "reported, not provider-authoritative" class of
    # evidence -- kept distinguishable so a future audit can tell which
    # mechanism produced a given deadline without re-deriving it from raw.
    return DeadlineGuess(value=deadline_iso, phrase=quote, confidence=0.5)


# ---------------------------------------------------------------------------
# SPONSORSHIP EXTRACTION — Decision 3's AI pass
# (docs/founder-decisions-2026-08-20.md), the last of the four steps.
#
# By the time this pass would run, steps 1-3 have already recovered every
# answer classify.extract_sponsorship's regex (the Workday structured field
# plus the missed phrasings) and directory.sponsorship's firm-policy
# fallback can find for free. What's left is real prose the regex genuinely
# cannot parse — the same "long tail" argument as the deadline pass above,
# and the same grounding rule: the model must quote the EXACT sentence its
# answer came from, verified as a real substring of the source text, or the
# answer is rejected. This never invents a sponsorship stance; it only reads
# one already sitting in text this app has already fetched.
#
# Scope, deliberately narrow (see extract_sponsorship_ai management command):
# only rows where BOTH the posting and the firm's own policy are still
# silent, and only rows whose text contains a sponsorship-adjacent keyword —
# a row with neither could not possibly have an answer for the model to find,
# and sending it anyway would only burn a paid call for a guaranteed "no
# answer" (1,358 of the pre-regex 2,304 unknown rows, per Decision 3).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SponsorshipGuess:
    value: str           # "yes" or "no" — never "unknown"; see extract_sponsorship_ai
    phrase: str           # the exact sentence the model quoted, verified as a substring of the input
    confidence: float     # 0.5 — same second-pass tier as DeadlineGuess.confidence


_SPONSORSHIP_PROMPT = """You are reading the text of a job/internship posting. Answer ONLY the question below, from ONLY the text given -- never from general knowledge about the employer.

QUESTION: Does this text state, as the EMPLOYER'S OWN POLICY, whether the employer will sponsor a work visa (e.g. H-1B, employment pass, work permit) for this specific role? A candidate-facing application-form question -- like "Will you now or in the future require sponsorship? * Select..." -- is NOT an answer to this question; it is something the CANDIDATE fills in, not a statement the employer makes. Only a declarative statement of the employer's own policy counts.

If the text states the employer WILL sponsor, respond with EXACTLY this JSON shape and nothing else:
{"sponsorship": "yes", "quote": "<the exact sentence from the text stating this, copied verbatim, unmodified>"}

If the text states the employer WILL NOT sponsor, respond with EXACTLY:
{"sponsorship": "no", "quote": "<the exact sentence from the text stating this, copied verbatim, unmodified>"}

If the text does not clearly state the employer's own sponsorship policy -- including if the only sponsorship-related text is a candidate application-form question -- respond with EXACTLY:
{"sponsorship": null, "quote": null}

The "quote" field, when present, MUST be an exact, verbatim substring of the text below -- do not paraphrase, summarize, or fix typos in it. If you cannot quote it exactly, answer null instead of guessing.

TEXT:
\"\"\"
{text}
\"\"\"
"""


def extract_sponsorship_ai(
    text: str | None,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> SponsorshipGuess | None:
    """One posting's cached text in, a grounded sponsorship guess out (or
    `None`) -- never raises for a "no answer" case, only for a genuine API
    failure (`AIExtractError`), same contract as `extract_deadline_ai`."""
    if not is_configured():
        return None
    t = (text or "").strip()
    if not t:
        return None
    t = t[:MAX_INPUT_CHARS]

    prompt = _SPONSORSHIP_PROMPT.replace("{text}", t)
    response = _post_json(
        {
            "model": model,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
        retries=retries,
    )
    parsed = _parsed_object(_extract_response_text(response))

    value = parsed.get("sponsorship")
    quote = parsed.get("quote")
    if value not in ("yes", "no") or not quote:
        return None
    if not _grounded(quote, t):
        return None

    return SponsorshipGuess(value=value, phrase=quote, confidence=0.5)


# ---------------------------------------------------------------------------
# APPLICATION-MAIL CLASSIFICATION — the long tail of `capture.appmail`
#
# Same argument as the two passes above, one surface over: the deterministic
# layer in `capture/appmail.py` types application mail from a list of phrases
# the ATSs send verbatim, and it will always have a tail. "We've reviewed your
# candidacy and have decided to pursue other applicants for this position" is
# a rejection in a phrasing no list had; a subject that says only "Update on
# your application" is unreadable without the body.
#
# SCOPE, and it is what makes the cost defensible: this is reached ONLY for a
# message that has already passed `appmail`'s gate (a known ATS/assessment
# vendor, or a bulk/no-reply sender with an application-shaped subject) AND
# that the phrase lists could not type. Everything else — every newsletter,
# every job alert, every human reply, and the large majority of real
# application mail — is classified for free and never reaches this function.
#
# The input is a subject line and Gmail's own short snippet, not a message
# body: a few hundred characters, an order of magnitude smaller than the
# posting text the deadline pass sends. And the grounding rule is the same —
# the quote must be a real substring of what the model was shown, or the whole
# answer is discarded. The caller uses the quote to verify and then drops it;
# §10 means an `ApplicationEvent` stores the subject and nothing else.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplicationEventGuess:
    value: str           # applied | assessment | video_interview | interview | rejected | offer
    phrase: str          # the exact sentence the model quoted, verified as a substring
    confidence: float    # 0.5 — same second-pass tier as the two guesses above


_APPLICATION_EVENT_PROMPT = """You are reading one email a student received while applying for internships. Answer ONLY from the text given -- never from general knowledge about the employer.

QUESTION: Is this email an automated APPLICATION-STATUS message about an application the student already submitted, and if so which kind?

The kinds, and what each one means:
- "applied": the employer confirms it received an application.
- "assessment": an invitation to complete an online/coding/numerical assessment or test (HackerRank, Codility, CodeSignal, SHL, pymetrics...).
- "video_interview": an invitation to record a one-way or on-demand video interview (HireVue, Spark Hire...).
- "interview": an invitation to, or the scheduling of, an interview with people -- including an assessment centre or superday.
- "rejected": the employer states the student is not being taken forward.
- "offer": the employer offers the role.

Respond with EXACTLY this JSON shape and nothing else:
{"event": "<one of the six kinds above>", "quote": "<the exact sentence from the text that shows this, copied verbatim, unmodified>"}

If the email is NOT an application-status message about this student's own application -- a job alert, a marketing email, a newsletter, an event invitation, a request that they apply, or anything ambiguous -- respond with EXACTLY:
{"event": null, "quote": null}

The "quote" field, when present, MUST be an exact, verbatim substring of the text below -- do not paraphrase, summarize, or fix typos in it. If you cannot quote it exactly, answer null instead of guessing.

TEXT:
\"\"\"
{text}
\"\"\"
"""

_APPLICATION_EVENT_KINDS = frozenset({
    "applied", "assessment", "video_interview", "interview", "rejected", "offer",
})


def extract_application_event_ai(
    subject: str | None,
    snippet: str | None = "",
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> ApplicationEventGuess | None:
    """One gated message's subject + snippet in, a grounded event kind out
    (or `None`). Never raises for a "no answer" case; an API failure is
    swallowed rather than surfaced, because this runs inside a mailbox sync
    whose job is to keep going — an unclassified message is already a
    supported outcome (`capture.appmail` reports it as unresolved)."""
    if not is_configured():
        return None
    text = "\n".join(part for part in [(subject or "").strip(), (snippet or "").strip()] if part)
    if not text:
        return None
    text = text[:MAX_INPUT_CHARS]

    prompt = _APPLICATION_EVENT_PROMPT.replace("{text}", text)
    try:
        response = _post_json(
            {
                "model": model,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
            retries=retries,
        )
    except AIExtractError:
        return None
    parsed = _parsed_object(_extract_response_text(response))

    value = parsed.get("event")
    quote = parsed.get("quote")
    if value not in _APPLICATION_EVENT_KINDS or not quote:
        return None
    if not _grounded(quote, text):
        return None
    return ApplicationEventGuess(value=value, phrase=quote, confidence=0.5)


# ---------------------------------------------------------------------------
# AUTO-REPLY FACT CLASSIFICATION — the long tail of `capture.mailfacts`
#
# Same argument one more time: the deterministic layer in capture/mailfacts.py
# types auto-replies from the stock phrasings mail systems actually send ("is
# no longer with", "please contact X at Y", "out of the office ... returning
# September 2"), and it will always have a tail — "Somil's coverage has been
# assumed by Salima" states a departure and a referral in words no list had.
#
# SCOPE: reached ONLY for a message that already declared ITSELF machine-
# answered (RFC 3834 Auto-Submitted / X-Autoreply / the stock subject prefix
# — `capture.inbound`'s `auto_submitted`) AND that the phrase layer could not
# type. Human mail never reaches this function, and neither does the great
# majority of auto-replies, which the free layer types first.
#
# The classification is CLOSED — three fact kinds or null — and the grounding
# rule is identical to every pass above: the quote must be a verbatim
# substring of what the model was shown or the whole answer is discarded.
# Uniquely here the quote is not thrown away after verification: it is the
# justification `capture.models.MailFact` stores and shows, because an
# automated action whose reason cannot be shown is an action the user cannot
# argue with. Structured data (the referral's address, the return date) is
# NEVER taken from the model — `capture.mailfacts` re-extracts it
# deterministically from the grounded text, so the model can only ever point
# at a sentence, not invent an email address.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MailFactGuess:
    value: str           # departed | out_of_office | address_change
    phrase: str          # the exact sentence quoted, verified as a substring
    confidence: float    # 0.5 — same second-pass tier as every guess above


_MAIL_FACT_PROMPT = """You are reading one AUTOMATED reply (an auto-responder) a student received after emailing a professional. Answer ONLY from the text given -- never from general knowledge.

QUESTION: Does this auto-reply state one of the following FACTS about the person the student wrote to?

- "departed": the person is no longer at the firm / has left the company.
- "out_of_office": the person is temporarily away (vacation, leave, travel) and will return.
- "address_change": the person has a new email address to be reached at.

Respond with EXACTLY this JSON shape and nothing else:
{"fact": "<one of the three kinds above>", "quote": "<the exact sentence from the text that states this, copied verbatim, unmodified>"}

If the text states none of these three facts clearly, respond with EXACTLY:
{"fact": null, "quote": null}

The "quote" field, when present, MUST be an exact, verbatim substring of the text below -- do not paraphrase, summarize, or fix typos in it. If you cannot quote it exactly, answer null instead of guessing.

TEXT:
\"\"\"
{text}
\"\"\"
"""

_MAIL_FACT_KINDS = frozenset({"departed", "out_of_office", "address_change"})


def extract_mail_fact_ai(
    subject: str | None,
    snippet: str | None = "",
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> MailFactGuess | None:
    """One gated auto-reply's subject + snippet in, a grounded fact kind out
    (or `None`). Never raises: an API failure is swallowed, because this runs
    inside a mailbox sync whose job is to keep going — an untyped auto-reply
    is already a supported outcome (`capture.mailfacts` surfaces it as a
    review card rather than acting)."""
    if not is_configured():
        return None
    text = "\n".join(part for part in [(subject or "").strip(), (snippet or "").strip()] if part)
    if not text:
        return None
    text = text[:MAX_INPUT_CHARS]

    prompt = _MAIL_FACT_PROMPT.replace("{text}", text)
    try:
        response = _post_json(
            {
                "model": model,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
            retries=retries,
        )
    except AIExtractError:
        return None
    parsed = _parsed_object(_extract_response_text(response))

    value = parsed.get("fact")
    quote = parsed.get("quote")
    if value not in _MAIL_FACT_KINDS or not quote:
        return None
    if not _grounded(quote, text):
        return None
    return MailFactGuess(value=value, phrase=quote, confidence=0.5)


DEFAULT_TEXT_MODEL = "claude-sonnet-5"


def complete_text(
    prompt: str,
    *,
    model: str = DEFAULT_TEXT_MODEL,
    max_tokens: int = 600,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> str | None:
    """A free-form completion for prose features (coffee-chat briefs, outreach
    drafts) that don't fit the single-fact grounded-quote contract above.

    Returns `None` when unconfigured or on any API failure -- callers must
    treat this the same way they'd treat "the AI feature isn't available
    right now" (show nothing, or a plain "AI brief unavailable" note), never
    as an error worth a 500. There is no grounding check here, because
    there's no single quotable fact to verify: the caller's OWN prompt is
    responsible for constraining the model to the context it's given (see
    `crm/ai_brief.py` for the pattern -- an explicit "only use the facts
    below, never invent one" instruction plus a visible "AI-drafted, check
    before you send it" label wherever this output reaches a user)."""
    if not is_configured():
        return None
    p = (prompt or "").strip()
    if not p:
        return None
    try:
        response = _post_json(
            {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": p}],
            },
            timeout=timeout,
            retries=retries,
        )
    except AIExtractError:
        return None
    text = _extract_response_text(response).strip()
    return text or None
