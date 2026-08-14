"""Deadline extraction over cached posting text, using an LLM as a second
pass behind the regex extractors in `classify.py` and `facts.py`.

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
    value: str          # ISO date, YYYY-MM-DD
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


def _grounded(quote: str | None, source: str) -> bool:
    """The model's quote must appear verbatim in the source text. Whitespace
    is the only normalization allowed -- a model that lightly reflows a
    paragraph while copying it should not lose an otherwise-real quote, but
    anything else (a paraphrase, a fabricated sentence) will not match."""
    if not quote:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return norm(quote) in norm(source)


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
    raw = _extract_response_text(response).strip()
    # Models occasionally wrap JSON in a code fence despite instructions;
    # strip one if present rather than failing the whole row over it.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None

    deadline_iso = parsed.get("deadline_iso")
    quote = parsed.get("quote")
    if not deadline_iso or not quote:
        return None
    if not _grounded(quote, t):
        return None
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", deadline_iso or ""):
        return None

    # 0.5, not the regex tier's 0.6: this is a second-pass, LLM-derived
    # answer over the same "reported, not provider-authoritative" class of
    # evidence -- kept distinguishable so a future audit can tell which
    # mechanism produced a given deadline without re-deriving it from raw.
    return DeadlineGuess(value=deadline_iso, phrase=quote, confidence=0.5)
