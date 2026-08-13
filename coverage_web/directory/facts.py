"""What a job description says about applying, extracted from its own words.

`enrich_postings` left 854 full descriptions in `Opportunity.raw["detail_text"]`
and read exactly two things out of them: the deadline and the sponsorship line.
Everything else a student needs in order to decide whether a posting is worth
an hour was sitting in that same text, unread — the graduation years it will
accept, the language it demands, the GPA bar, whether a cover letter is
required, which video interview or test comes next, what it pays.

Every extractor here obeys the same contract as the deadline extractors in
classify.py, and it is the contract that makes this safe to show:

- A KEYWORD GATE first, then the value. A bare number in a posting is usually
  a headcount or a year; a bare "2028" is usually a programme year. The gate is
  what stops a plausible number becoming a wrong chip.
- SILENCE IS AN ANSWER, and its answer is "we don't know". Nothing here ever
  infers a fact from the firm, the title, or a sibling posting. A missing GPA
  means the posting did not state one, never "no GPA requirement".
- The RAW PHRASE travels with the value. Every fact carries the sentence it
  came from, so the page can show its evidence on hover and a wrong chip can
  be traced to the line that produced it rather than argued about.

Facts land in `raw["facts"]`, not in columns, deliberately: they are derived
data with no consumer outside the feed, re-derivable at any time from text we
already hold, and a schema that changes as often as this one should not be a
migration each time.
"""

from __future__ import annotations

import html
import re

# A window after the keyword. Long enough for "a minimum GPA of 3.5", short
# enough that the next sentence's number cannot be captured by mistake.
_NEAR = 60


# A full stop with a digit on either side is a decimal point, not the end of a
# sentence. Treating it as one made the evidence for "GPA 3.0 out of 4.0" open
# mid-number — "0 GPA out of 4.0" — which reads as a parsing failure even
# where the value is right. Observed on Citi's summer analyst postings.
_SENTENCE_END = re.compile(r"(?<!\d)\.(?!\d)")


def _boundary_before(text: str, pos: int) -> int:
    ends = [m.start() for m in _SENTENCE_END.finditer(text, 0, pos)]
    return ends[-1] if ends else -1


def _boundary_after(text: str, pos: int) -> int:
    m = _SENTENCE_END.search(text, pos)
    return m.start() if m else -1


def _sentence(text: str, start: int, end: int) -> str:
    """The phrase a match sits in, trimmed AROUND the match.

    Trimming from the left of the sentence is the obvious version and it is
    wrong: these descriptions run to thousands of characters with sparse
    punctuation, so a "sentence" is often 600 chars long and the first 180 of
    them do not contain the match. The evidence then shows a passage that does
    not mention the fact it is supposed to prove, which is worse than no
    evidence at all — it reads as a mis-extraction even when the value is
    right. The window is anchored on the match and grows outward.
    """
    left = max(_boundary_before(text, start), text.rfind("\n", 0, start)) + 1
    right = min((i for i in (_boundary_after(text, end), text.find("\n", end))
                 if i != -1), default=len(text))
    cut_left = cut_right = False
    if right - left > 180:
        pad = (180 - (end - start)) // 2
        new_left, new_right = max(left, start - max(pad, 20)), min(right, end + max(pad, 20))
        cut_left, cut_right = new_left > left, new_right < right
        left, right = new_left, new_right
    out = re.sub(r"\s+", " ", text[left:right]).strip()
    # Whole words at both ends. Cutting a long passage to a fixed width lands
    # mid-word about half the time ("ver world class operational services"),
    # which reads as corrupted evidence rather than as a quotation.
    if cut_left and " " in out:
        out = "…" + out.split(" ", 1)[1]
    if cut_right and " " in out:
        out = out.rsplit(" ", 1)[0] + "…"
    return out[:180]


# Page furniture that came along for the ride. `enrich_postings` reads
# Workday through its JSON API and gets the description alone, but a plain
# HTML board gives up its whole page: 153 of 854 stored descriptions open with
# a cookie-consent notice and a navigation bar before reaching a word about
# the job. The drawer rendered that as the posting, which is the firm's cookie
# policy wearing a job's title.
#
# Cutting at the LAST marker found in the opening stretch, not the first, is
# what makes this safe on a page where the banner and the nav both appear:
# everything up to the deepest piece of chrome is chrome.
_CHROME_MARKERS = (
    "read more about our cookie policy", "disable non-essential cookies",
    "accept all cookies", "i accept the cookie policy", "skip to content",
    "toggle navigation", "browse all programs", "login | register",
    "cookie preferences", "manage cookies",
)


def _strip_chrome(text: str) -> str:
    head = text[:2500].lower()
    cut = 0
    for marker in _CHROME_MARKERS:
        i = head.rfind(marker)
        if i != -1:
            cut = max(cut, i + len(marker))
    if not cut:
        return text
    rest = text[cut:].lstrip(" -->|\n\t\xa0")
    # Never trade a real description for an empty one: if the cut leaves
    # almost nothing, the markers matched something that wasn't chrome.
    return rest if len(rest) > 200 else text


def _clean(text: str) -> str:
    """Entity-decoded, whitespace-collapsed, chrome-free text.

    The detail pages were stored as fetched, so `&#160;` and `&amp;` are
    literal in the cache. Left alone they land inside the fixed-width windows
    every pattern below uses (six characters spent on one space), which
    silently shortens the reach of every gate.
    """
    return _strip_chrome(re.sub(r"[ \t\xa0]+", " ", html.unescape(text)))


# --- GPA -------------------------------------------------------------------
# Gated on the word itself. The scale check ("<= 4.5") is the second gate:
# "GPA" near "3.5" is a cutoff, "GPA" near "2027" is a coincidence.
# The gap between the keyword and the number is captured, because it is what
# tells a cutoff from a SCALE. "a minimum 3.0 GPA out of 4.0" contains both
# numbers and the forward pattern reaches the wrong one first: the page said
# 3.0 and the card said "GPA 4.0", which is not a stricter reading of the
# posting but a different claim from the one it made. Observed live on Citi.
_GPA = re.compile(r"\bG\.?P\.?A\.?\b([^.\n]{0,%d}?)(\d\.\d{1,2})" % _NEAR, re.IGNORECASE)
_GPA_REVERSED = re.compile(r"(\d\.\d{1,2})[^.\n]{0,20}?\bG\.?P\.?A\.?\b", re.IGNORECASE)
# What sits between "GPA" and a denominator, never between "GPA" and a cutoff.
_GPA_SCALE = re.compile(r"(?:out\s+of|on\s+an?|scale|max(?:imum)?|/)\s*$", re.IGNORECASE)


def extract_gpa(text: str) -> dict | None:
    # Reversed FIRST: a number sitting immediately before the word ("a 3.5
    # GPA") is always the requirement, never the scale.
    for rx in (_GPA_REVERSED, _GPA):
        for m in rx.finditer(text):
            gap = m.group(1) if rx is _GPA else ""
            if gap and _GPA_SCALE.search(gap):
                continue
            try:
                value = float(m.group(2) if rx is _GPA else m.group(1))
            except ValueError:
                continue
            # 4.5 rather than 4.0: some schools run a 4.3 scale, and a value
            # above that is a version number or a rating, not a grade bar.
            if 1.0 <= value <= 4.5:
                # One decimal always: a cutoff rendered "3" reads as a
                # rounded-off 3-point-something rather than the 3.0 it is.
                return {"value": f"{value:.1f}".rstrip("0").rstrip(".") + (
                    "" if value % 1 else ".0"),
                    "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Graduation window -----------------------------------------------------
# `class_year` already holds a stated "Class of 2028" from the TITLE. This is
# the same question asked of the body, where firms state ranges ("graduating
# between December 2027 and June 2028") that a single year cannot express.
_GRAD = re.compile(
    # "or" joins ranges as often as "to" does — SIG writes "graduate in the
    # winter of 2028 or the spring of 2029" — and the second year can sit a
    # few words past its connector ("or the spring of 2029"), not just one.
    # "degree completion" is the same statement without the word: Deutsche
    # Bank writes "Bachelor's degree completion: 2026 or later".
    r"(?:graduat\w*|degree completion)[^.\n]{0,%d}?((?:19|20)\d{2})"
    r"(?:\s*(?:-|–|—|to|and|through|or)\s*"
    r"(?:[\w']+\s+){0,3}?((?:19|20)\d{2}))?" % _NEAR, re.IGNORECASE)


def extract_grad_years(text: str) -> dict | None:
    # Every match, not just the first: when a description mentions a year
    # outside the plausible window early ("our graduate scheme, running since
    # 2019...") the REAL statement is usually still ahead, and returning None
    # on the first miss threw it away.
    for m in _GRAD.finditer(text):
        years = [y for y in (m.group(1), m.group(2)) if y]
        # A posting talking about graduation in 2019 is describing its own
        # history.
        years = [y for y in years if 2024 <= int(y) <= 2035]
        if not years:
            continue
        label = years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}"
        return {"value": label, "years": years,
                "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Programme start year ---------------------------------------------------
# The intake year, when the posting states it in the body rather than the
# title. `cohort` already reads "Summer Analyst Program - 2027" from titles;
# this is the same fact stated as prose or as a start-date label — HSBC's
# "Start Date and Duration: Tue Jun 01, 2027", Crédit Agricole's "Date prévue
# de prise de fonction 01/06/2026" (44 live rows say their year ONLY there),
# SocGen's "starting in July 2026", Deutsche Bank Italy's "Inizio Internship:
# Settembre 2026".
#
_MONTH = (r"january|february|march|april|may|june|july|august|september|"
          r"october|november|december")
# Anchors only, never a bare year: the same descriptions carry "© 2026",
# requisition numbers ("Référence 2026-110866"), page-update dates and event
# RSVP dates, none of which say when the job starts. Two near-misses are
# excluded by name: "begins" is NOT an anchor because IMC's rejection
# boilerplate says "reapply when the next recruitment season begins in
# September 2027" — the next season's start, not this posting's — and
# "commenc\w+" must not swallow "commencement", which is a graduation
# ceremony.
_START_RXS = (
    # Label forms: the value is the posting's own structured field. The
    # tempered window on "start date" refuses to cross a second "date" label:
    # SocGen writes "Start date Immediately Publication date 2026/05/20", and
    # an untempered window read the publication date as the start.
    re.compile(r"start date(?:(?!date)[^.\n]){0,40}?((?:19|20)\d{2})",
               re.IGNORECASE),
    # An insight event's stated date is when the thing takes place — the same
    # fact Citi labels "Event Start Date" (already read by the anchor above);
    # BofA writes "Event Date September 14, 2026" without the word.
    re.compile(r"event date(?:(?!date)[^.\n]){0,40}?((?:19|20)\d{2})",
               re.IGNORECASE),
    re.compile(r"prise de fonction[^.\n]{0,20}?\d{2}/\d{2}/((?:19|20)\d{2})",
               re.IGNORECASE),
    re.compile(r"inizio[^.\n]{0,40}?((?:19|20)\d{2})", re.IGNORECASE),
    # Prose forms, anchored on a verb that can only describe this role.
    re.compile(r"starting(?: in| from)?(?:\s+[\w/]+){0,2}?\s+((?:19|20)\d{2})",
               re.IGNORECASE),
    re.compile(r"commenc(?!ement)\w*(?:\s+[\w/]+){0,4}?\s+((?:19|20)\d{2})",
               re.IGNORECASE),
    # A month-to-month range, months named on both sides. No verb needed:
    # "from January 2027 - August 2027" (MUFG) and "during June and July
    # 2027" (McKinsey) are programme periods, and no boilerplate — copyright
    # lines, requisition numbers, page dates — takes this shape.
    re.compile(r"from\s+(?:%s)\s+((?:19|20)\d{2})\s*(?:-|–|—|to|until)\s*(?:%s)"
               % (_MONTH, _MONTH), re.IGNORECASE),
    re.compile(r"during\s+(?:%s)\s+(?:and|or|to|-|–)\s*(?:%s)\s+((?:19|20)\d{2})"
               % (_MONTH, _MONTH), re.IGNORECASE),
)

# What a start year must NOT be. "offer": BofA's 2027 Summer Analyst
# postings all say strong interns "may be offered full time employment to
# commence in July 2028" — the year a DIFFERENT job would begin, one summer
# after this one. "academic year": Morgan Stanley Japan's eligibility clause
# ("studying abroad outside of Japan during the academic year from June
# 2026...") dates the applicant's studies, not the programme. Either phrase
# shortly before the match disqualifies it.
_START_OFFER_GUARD = re.compile(
    r"(?:offer\w*|academic year)[^.\n]{0,60}$", re.IGNORECASE)


def extract_start_year(text: str) -> dict | None:
    for rx in _START_RXS:
        for m in rx.finditer(text):
            year = m.group(1)
            # Same plausibility window as the graduation fact: a start date
            # in the past is the page's history, not an intake.
            if not 2024 <= int(year) <= 2035:
                continue
            if _START_OFFER_GUARD.search(text[max(0, m.start() - 80):m.start()]):
                continue
            return {"value": year, "years": [year],
                    "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Language --------------------------------------------------------------
# Only the languages this product's markets actually gate on, and only when
# the posting frames one as a requirement. "Mandarin" appearing in a list of
# nice-to-haves is not a wall; "fluency in Mandarin required" is.
_LANGS = ("mandarin", "cantonese", "japanese", "korean", "german", "french",
          "spanish", "italian", "dutch", "portuguese", "arabic")
_LANG_REQ = re.compile(
    r"(?:fluen\w+|proficien\w+|native|business[- ]level|command of|speak|"
    r"written and spoken|bilingual)[^.\n]{0,%d}?\b(%s)\b|"
    r"\b(%s)\b[^.\n]{0,40}?(?:required|is a must|essential|mandatory|"
    r"fluency|proficiency)" % (_NEAR, "|".join(_LANGS), "|".join(_LANGS)),
    re.IGNORECASE)


# "Fluency in English is essential and a fluency in German is a strong
# preference" matches every requirement pattern above and is not a
# requirement. The guard reads the words AFTER the match, where the
# hedge always sits, and drops the language when it finds one. Observed on
# live data: PwC (French "is a plus") and BofA (German, "strong preference")
# both rendered as hard language walls.
_LANG_SOFT = re.compile(
    r"\b(?:is |are )?(?:a |an )?(?:strong |added |distinct )?"
    r"(?:plus|preference|preferred|preferable|advantage|advantageous|"
    r"desirable|beneficial|bonus|nice to have|welcome[d]?|valued|"
    r"an asset|would help)\b", re.IGNORECASE)

# A flat-out NEGATIVE statement, which _LANG_SOFT cannot see because it only
# looks forward from where the match ENDS -- and a negation like these three
# sits INSIDE the match itself, between the language name and the
# requirement keyword that made `_LANG_REQ` fire in the first place, not
# after it. Confirmed live on Morgan Stanley id=1911 ("Proficiency in German
# is not required" -- the requirement keyword IS the negated word, so the
# match ends right where the negation starts and the old forward-only guard
# never got a chance to run), Morgan Stanley id=1878 ("Knowledge of German is
# beneficial but not essential" -- "essential" is what triggered the match,
# and "not" sits directly before it, inside the matched span) and Blackstone
# id=348 ("...advantageous but not required" -- same shape). All three
# stored `facts.language` with the negation quoted as its own `phrase`,
# rendering as a blocking "German needed" chip on postings that explicitly
# say German is not required. Checked over a window spanning the match
# itself (not just the text after it) so it catches negations wherever they
# fall relative to the keyword that triggered the match.
_LANG_NEGATED = re.compile(
    r"\bnot\b[^.\n]{0,20}?(?:required|essential|necessary|mandatory|needed|a must)\b",
    re.IGNORECASE)


def extract_languages(text: str) -> dict | None:
    found, phrase = [], ""
    for m in _LANG_REQ.finditer(text):
        lang = (m.group(1) or m.group(2) or "").title()
        window = text[max(0, m.start() - 20):m.end() + 40]
        if _LANG_SOFT.search(text[m.end():m.end() + 40]) or _LANG_NEGATED.search(window):
            continue
        if lang and lang not in found:
            found.append(lang)
            phrase = phrase or _sentence(text, m.start(), m.end())
    if not found:
        return None
    return {"value": " · ".join(found[:2]), "langs": found, "phrase": phrase}


# --- Cover letter ----------------------------------------------------------
# Two gates, because "cover letter" alone appears in "a cover letter is
# optional" just as often as in the sentence that demands one.
_COVER_YES = re.compile(
    r"cover letter[^.\n]{0,%d}?(?:is )?(?:required|must|mandatory)|"
    r"(?:required|must submit|please submit|include)[^.\n]{0,%d}?cover letter"
    % (_NEAR, _NEAR), re.IGNORECASE)
_COVER_NO = re.compile(r"cover letter[^.\n]{0,30}?(?:not required|optional)",
                       re.IGNORECASE)


def extract_cover_letter(text: str) -> dict | None:
    if _COVER_NO.search(text):
        return None
    m = _COVER_YES.search(text)
    if not m:
        return None
    return {"value": "Cover letter", "phrase": _sentence(text, m.start(), m.end())}


# --- Assessments -----------------------------------------------------------
# Named products only. "Online assessment" is generic enough to appear in
# boilerplate about the process in general; a brand name is a fact about what
# you will actually be asked to sit.
_ASSESSMENTS = {
    "hirevue": "HireVue",
    "pymetrics": "Pymetrics",
    "codesignal": "CodeSignal",
    "hackerrank": "HackerRank",
    "sonru": "Sonru",
    "cut-e": "cut-e",
    "shl": "SHL",
    # "video interview" WAS in this list and came straight back out: every
    # Bank of America page carries an accessibility line offering help with
    # "your application or video interview", so the generic phrase tagged the
    # boilerplate rather than the process. Named products only, as stated.
    "numerical reasoning": "Numerical test",
    "situational judgement": "Situational judgement",
    "situational judgment": "Situational judgement",
}


def extract_assessment(text: str) -> dict | None:
    low = text.lower()
    for needle, label in _ASSESSMENTS.items():
        i = low.find(needle)
        if i != -1:
            return {"value": label, "phrase": _sentence(text, i, i + len(needle))}
    return None


# --- Pay -------------------------------------------------------------------
# US pay-transparency law is why this exists at all: firms posting into NY,
# CA, CO and WA must state a range, and no campus board shows it. Hourly and
# annual both appear; the unit travels with the number so the chip can never
# read "$45" and mean a year.
_PAY = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d{2})?)\s*"
    r"(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d{2})?)"
    r"([^.\n]{0,40}?(?:per hour|hourly|an hour|per year|annually|per annum))?",
    re.IGNORECASE)


def _money(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _rate(v: float) -> str:
    """An hourly figure with its cents, when it has any. Wells Fargo posts
    "$40.87 - $40.87"; dropping the cents makes a precise number look
    approximate, and collapsing the pair makes a point look like a range."""
    return f"${v:.2f}".rstrip("0").rstrip(".") if v % 1 else f"${v:.0f}"


def extract_pay(text: str) -> dict | None:
    for m in _PAY.finditer(text):
        low, high = _money(m.group(1)), _money(m.group(2))
        if low is None or high is None or high < low:
            continue
        tail = (m.group(3) or "").lower()
        hourly = "hour" in tail or high < 500
        if hourly:
            # An "hourly rate" over $500 is not an hourly rate.
            if high > 500:
                continue
            value = (f"{_rate(low)}/hr" if low == high
                     else f"{_rate(low)}–{_rate(high)}/hr")
        else:
            if low < 10_000:
                continue
            value = (f"${int(low) // 1000}k" if low == high
                     else f"${int(low) // 1000}k–${int(high) // 1000}k")
        return {"value": value, "low": low, "high": high,
                "unit": "hour" if hourly else "year",
                "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Rolling review --------------------------------------------------------
# The feed labels every undated role "Rolling", which is a claim the data does
# not support: ~600 of those postings simply never stated a date. This is the
# subset that says so out loud, and it is the only subset allowed to keep the
# word.
_ROLLING = re.compile(
    r"rolling basis|reviewed on a rolling|rolling review|"
    r"(?:apply|applications? (?:are )?(?:encouraged|reviewed))[^.\n]{0,40}?"
    r"(?:as early as possible|early as we|on a rolling)|"
    r"we encourage you to apply early|applications close once",
    re.IGNORECASE)


def extract_rolling(text: str) -> dict | None:
    m = _ROLLING.search(text)
    if not m:
        return None
    return {"value": "Rolling", "phrase": _sentence(text, m.start(), m.end())}


# --- Year of study ---------------------------------------------------------
# The market's own eligibility vocabulary, and the most common way a posting
# says who it is for outside the US. "Penultimate year" appears in 65 of 908
# open campus descriptions and was read by nothing: a student in their second
# of three years could not tell from the board whether a role was open to them.
#
# The stage names are quoted, never converted into a graduation year: a
# penultimate year means different things on a three-year UK degree and a
# four-year US one, and translating would be this module inferring a fact the
# posting did not state.
_STUDY_STAGE = (
    (r"penultimate[- ]year", "Penultimate year"),
    # "entering your final year of study" is as common as "final-year
    # student" and was falling through to the looser stage below it.
    (r"final[- ]year (?:student|of study)", "Final year"),
    (r"recent graduate", "Recent graduate"),
    (r"first[- ]year student", "First year"),
)
_STUDY_RX = [(re.compile(p, re.IGNORECASE), label) for p, label in _STUDY_STAGE]


def extract_study_stage(text: str) -> dict | None:
    for rx, label in _STUDY_RX:
        m = rx.search(text)
        if m:
            return {"value": label, "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Programme length ------------------------------------------------------
# "10 weeks" is a fact about whether a summer is spoken for, and postings
# state it far more often than they state a deadline. Gated on the programme
# words either side, so a "10 week" mention of something else in the body
# cannot become the programme's length.
_WEEKS = re.compile(
    r"\b(\d{1,2})[- ]week[s]? (?:summer )?(?:internship|programme|program|placement|analyst)|"
    r"(?:internship|programme|program|placement) (?:is |runs |lasts )?"
    r"(?:for )?(?:approximately )?(\d{1,2}) weeks",
    re.IGNORECASE)


def extract_duration(text: str) -> dict | None:
    for m in _WEEKS.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            weeks = int(raw)
        except (TypeError, ValueError):
            continue
        # A campus programme is a summer or a placement year, never one week
        # or sixty. Outside that band the number belongs to something else.
        if 2 <= weeks <= 52:
            return {"value": f"{weeks} weeks", "weeks": weeks,
                    "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Transcript ------------------------------------------------------------
# Unlike a CV, a transcript has a LEAD TIME — some registrars take days — so
# knowing before you open the form is worth its own chip. Same two-gate shape
# as the cover letter: the word alone appears in "transcripts are not required
# at this stage" just as readily as in the sentence that demands one.
_TRANSCRIPT_YES = re.compile(
    r"(?:submit|upload|provide|attach|include|require[ds]?|send)[^.\n]{0,%d}?"
    r"transcript|transcript[s]?[^.\n]{0,40}?(?:required|must be|are needed)"
    % _NEAR, re.IGNORECASE)
_TRANSCRIPT_NO = re.compile(
    r"transcript[s]?[^.\n]{0,40}?(?:not required|optional|not needed)",
    re.IGNORECASE)


def extract_transcript(text: str) -> dict | None:
    if _TRANSCRIPT_NO.search(text):
        return None
    m = _TRANSCRIPT_YES.search(text)
    if not m:
        return None
    return {"value": "Transcript", "phrase": _sentence(text, m.start(), m.end())}


EXTRACTORS = {
    "pay": extract_pay,
    "study": extract_study_stage,
    "language": extract_languages,
    "grad": extract_grad_years,
    "start": extract_start_year,
    "gpa": extract_gpa,
    "duration": extract_duration,
    "cover_letter": extract_cover_letter,
    "transcript": extract_transcript,
    "assessment": extract_assessment,
    "rolling": extract_rolling,
}


def extract_facts(text: str | None) -> dict:
    """Every fact a description states about applying, keyed by kind.

    Returns {} for empty text rather than a dict of Nones: absence of a fact
    and absence of a description are different states, and only the caller
    knows which one it is looking at.
    """
    if not text or not text.strip():
        return {}
    text = _clean(text)
    out = {}
    for kind, fn in EXTRACTORS.items():
        try:
            got = fn(text)
        except (ValueError, IndexError):
            got = None
        if got:
            out[kind] = got
    return out


# --- Reading the description at all ----------------------------------------
# The stored text is one unbroken line. Workday's own JSON delivers the
# description as HTML and `enrich_postings` strips the tags, so every <p>,
# <li> and <h3> that gave the posting its shape is gone by the time it
# reaches us — the median document is 3,825 characters with no newline in it
# and only 38 of 854 carry a bullet character.
#
# So the breaks are re-derived from the one structural signal that survived:
# these firms all write the same section headings. The list is explicit and
# short on purpose. A cleverer rule (break before any Capitalised Run) shatters
# every "Bank of America" and "New York" in the body.
_SECTIONS = (
    "job description summary", "job description", "about the team",
    "about the role", "about the program", "about the programme",
    "about you", "purpose of the role", "role overview",
    "what you'll do", "what you will do", "what you'll need",
    "what we are looking for", "what we're looking for", "who can apply",
    "your role", "your team", "responsibilities", "key responsibilities",
    # Barclays' own template, 190/133 hits sampled live across its postings
    # — "Accountabilities" (bare, or prefixed "Key") is that firm's word for
    # what every other firm here calls "Responsibilities". A third heading
    # from the same template, "<title> Expectations" (Analyst/Assistant Vice
    # President/Vice President/...), was tried and dropped: any two of its
    # title variants that share a suffix ("Vice President Expectations"
    # inside "Assistant Vice President Expectations") both independently
    # satisfy the split regex, since the shorter one's own lookbehind is
    # happy to fire right after "Assistant" — sandwiching that single word
    # into its own orphan paragraph. Not worth enumerating every seniority
    # title to chase.
    "accountabilities", "key accountabilities",
    "qualifications", "basic qualifications", "desired qualifications",
    "preferred qualifications", "requirements", "eligibility",
    "skills and qualifications", "selection process", "recruitment process",
    "our recruitment process", "how to apply", "application process",
    "program locations", "pay range", "salary range", "benefits",
    "equal opportunity", "diversity", "why join", "next steps",
)

def _heading_alt(phrase: str) -> str:
    """One heading as a pattern whose FIRST letter must be capital.

    Case matters here and a plain IGNORECASE match proved it: "Requirements"
    and "Responsibilities" are headings, but the same words occur mid-sentence
    ("...the requirements of the AMP Program are designed to...") and an
    insensitive match broke the paragraph there, leaving blocks that start
    halfway through a sentence. A heading is capitalised; prose is not.
    """
    first, rest = re.escape(phrase[0]), re.escape(phrase[1:])
    return f"{first.upper()}(?i:{rest})"


_SECTION_RX = re.compile(
    r"(?<=[a-z.,;:)])\s+(?=(?:%s)\b)" % "|".join(
        _heading_alt(s) for s in sorted(_SECTIONS, key=len, reverse=True)))
_BULLET_RX = re.compile(r"\s*[•●▪]\s*")


def paragraphs(text: str | None, *, limit: int = 4000) -> list[str]:
    """The description as readable blocks, longest-first-heading order kept.

    `limit` is a reading budget, not a storage one: past ~4,000 characters a
    posting is boilerplate about the firm rather than about the job, and the
    drawer says so and links out rather than pretending to be the posting.
    """
    if not text:
        return []
    body = _clean(text).strip()
    if len(body) > limit:
        cut = body.rfind(" ", 0, limit)
        body = body[:cut if cut > limit - 200 else limit].rstrip() + "…"
    if _BULLET_RX.search(body):
        body = _BULLET_RX.sub("\n· ", body)
    blocks = []
    for chunk in _SECTION_RX.split(body):
        chunk = chunk.strip()
        if chunk:
            blocks.append(chunk)
    return blocks
