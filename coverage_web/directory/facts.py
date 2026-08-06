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
    r"graduat\w*[^.\n]{0,%d}?((?:19|20)\d{2})(?:\s*(?:-|–|—|to|and|through)\s*"
    r"(?:\w+\s+)?((?:19|20)\d{2}))?" % _NEAR, re.IGNORECASE)


def extract_grad_years(text: str) -> dict | None:
    m = _GRAD.search(text)
    if not m:
        return None
    years = [y for y in (m.group(1), m.group(2)) if y]
    # A posting talking about graduation in 2019 is describing its own history.
    years = [y for y in years if 2024 <= int(y) <= 2035]
    if not years:
        return None
    label = years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}"
    return {"value": label, "years": years,
            "phrase": _sentence(text, m.start(), m.end())}


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


def extract_languages(text: str) -> dict | None:
    found, phrase = [], ""
    for m in _LANG_REQ.finditer(text):
        lang = (m.group(1) or m.group(2) or "").title()
        if _LANG_SOFT.search(text[m.end():m.end() + 40]):
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


EXTRACTORS = {
    "pay": extract_pay,
    "grad": extract_grad_years,
    "language": extract_languages,
    "gpa": extract_gpa,
    "cover_letter": extract_cover_letter,
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
    "what you'll do", "what you will do", "what you'll need",
    "what we are looking for", "what we're looking for", "who can apply",
    "your role", "your team", "responsibilities", "key responsibilities",
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
