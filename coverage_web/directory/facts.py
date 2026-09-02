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

# A number near "GPA" is a hard cutoff UNLESS the surrounding words hedge it
# ("preferred"/"desired"/"ideally") -- the same distinction extract_languages
# already draws with _LANG_SOFT. Without this gate, "3.2 minimum GPA
# preferred" (BofA) and "Ideally you'll have a GPA of 3.2" (Barclays) render
# as an identical, unqualified "GPA 3.2" chip next to a genuine hard floor
# like Brookfield's "Minimum 3.0 Cumulative GPA" -- indistinguishable to a
# student deciding whether a 3.1 disqualifies them. Confirmed live: BofA,
# Citi, Barclays, MUFG, Nomura, PwC all state a soft-worded GPA this way.
#
# The window is short and sentence-bounded on both sides (whichever comes
# first: a literal period, or the char cap) because the hedge word has to be
# READING the GPA clause, not just sitting somewhere nearby in the same
# scraped wall of bullet text. A wide unbounded window produced false
# positives verified live: Morgan Stanley id=1857 ("minimum cumulative GPA
# of 3.0 Track record of academic excellence...") and Oliver Wyman id=524
# ("Minimum GPA of 3.5 Interest in the CAS...") both carry a "preferred"
# hundreds of characters away, in an unrelated bullet, once the HTML's list
# punctuation is stripped -- a wide window mistook a hard floor for a soft
# one. Conversely a pure period-boundary check without the char cap missed
# nothing extra, but a pure char-cap without the period boundary produced
# false positives of its own: BofA id=1795 has an unrelated "Preferred
# majors" 129 characters before its actual (unhedged... no, hedged via the
# word directly AFTER it) GPA number, and Barclays id=1247/1246 has "a GPA
# of 3.2 or above." immediately followed, past its own sentence-ending
# period, by an unrelated "Ideally, you would also have an interest in
# working..." only 18 characters later. Only the AND of both bounds (period
# OR char cap, whichever is closer) cleared every row checked, including
# these decoys, with zero misclassifications across all 114 open GPA facts.
_GPA_SOFT = re.compile(r"\b(?:preferred|desired|ideally|prefer)\b", re.IGNORECASE)
_GPA_SOFT_BEFORE = 35
_GPA_SOFT_AFTER = 25

# A DUAL-SCALE statement restates one bar twice, once per grading scale a
# school might use -- HSBC's own template writes "a minimum GPA of 4.0/5.0
# or 3.2/4.0". The forward `_GPA` pattern above finds "GPA of" and grabs the
# NEAREST decimal, which for this shape is 4.0 -- the numerator of the
# 5.0-scale reading, not the 4.0-scale bar (3.2) the chip is actually
# rendered under: `views.py`'s chip prints bare "GPA {value}" with no
# denominator, on the assumption every value already sits on the standard
# 4.0 scale. Confirmed live on 2 open HSBC postings sharing this template:
# the chip read "GPA 4.0" -- a near-perfect-GPA bar -- for a posting whose
# stated 4.0-scale floor is 3.2, the ordinary difference between "top of
# your class" and "solidly good," misleading a student off a role they
# clear. Matched immediately after the forward pattern's own capture (not
# as an independent scan), so it only ever fires on a number `_GPA` already
# treated as the candidate cutoff.
_GPA_DUAL_SCALE = re.compile(
    r"\s*/\s*(\d\.\d{1,2})\s*or\s*(\d\.\d{1,2})\s*/\s*(\d\.\d{1,2})", re.IGNORECASE)


def _gpa_is_hedged(text: str, start: int, end: int) -> bool:
    before_period = text.rfind(".", 0, start)
    before_period = 0 if before_period == -1 else before_period + 1
    before = max(before_period, start - _GPA_SOFT_BEFORE)
    after_period = text.find(".", end)
    after_period = len(text) if after_period == -1 else after_period
    after = min(after_period, end + _GPA_SOFT_AFTER)
    return bool(_GPA_SOFT.search(text[before:after]))


def _dual_scale_value(text: str, value: float, pos: int) -> float:
    """`value` (already captured, sitting right before position `pos`), or
    the 4.0-scale alternative when what follows is a dual-scale disjunction
    stating the SAME bar again on a different denominator. Only ever
    prefers a fraction whose OWN denominator is 4.0 -- when neither half of
    the disjunction runs on that scale, there is no 4.0-scale reading to
    prefer, and `value` (the forward match's own answer) stands."""
    m = _GPA_DUAL_SCALE.match(text, pos)
    if not m:
        return value
    first_denom, second_num, second_denom = m.groups()
    if first_denom == "4.0":
        return value  # already the 4.0-scale numerator
    if second_denom == "4.0":
        try:
            return float(second_num)
        except ValueError:
            return value
    return value


def extract_gpa(text: str) -> dict | None:
    # Reversed FIRST: a number sitting immediately before the word ("a 3.5
    # GPA") is always the requirement, never the scale.
    for rx in (_GPA_REVERSED, _GPA):
        for m in rx.finditer(text):
            gap = m.group(1) if rx is _GPA else ""
            if gap and _GPA_SCALE.search(gap):
                continue
            num_group = 2 if rx is _GPA else 1
            try:
                value = float(m.group(num_group))
            except ValueError:
                continue
            value = _dual_scale_value(text, value, m.end(num_group))
            # 4.5 rather than 4.0: some schools run a 4.3 scale, and a value
            # above that is a version number or a rating, not a grade bar.
            if 1.0 <= value <= 4.5:
                # One decimal always: a cutoff rendered "3" reads as a
                # rounded-off 3-point-something rather than the 3.0 it is.
                return {"value": f"{value:.1f}".rstrip("0").rstrip(".") + (
                    "" if value % 1 else ".0"),
                    "hedge": _gpa_is_hedged(text, m.start(), m.end()),
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
    #
    # The negative lookahead excludes "graduat\w*" when it is itself the
    # NOUN "graduate" naming the post-internship full-time PROGRAMME, not
    # the applicant's own graduation. SIG's own template invites successful
    # interns "to join our full-time graduate programme in either September
    # 2027, January 2028 or August 2028" -- a statement about when the
    # OFFERED job starts, not about when the applicant graduates -- and the
    # bare `graduat\w*` gate matched the word "graduate" inside "graduate
    # programme"/"graduate scheme" identically to a genuine "planning to
    # graduate in 2027". Confirmed live on 12 of SIG's 25 grad-chip rows
    # (48%), several producing a value flatly contradicted by the row's own
    # true graduation cue elsewhere in the same posting.
    #
    # `\b` pinned right after `\w*`, before the lookahead: without it, a
    # greedy `\w*` that fails the lookahead at the full word ("graduate")
    # backtracks to a SHORTER match ("graduat") where the lookahead trivially
    # passes (the very next character, "e", isn't whitespace, so the
    # forbidden pattern can't even start) -- silently defeating the exclusion
    # this fix exists for. Forcing a real word boundary before the lookahead
    # runs means the only match Python's backtracking can settle on is the
    # WHOLE word, so the lookahead actually sees what follows it.
    #
    # The lookahead's leading gap is `[\s:;,-]*` (any MIX of whitespace and
    # light punctuation, zero or more), not the plain `\s+` a first pass
    # reaches for: SIG's own template also writes "Soon-to-be or recent
    # graduate : Our programme starts in early September 2026" -- a colon
    # between "graduate" and "programme" that `\s+` cannot cross, so the
    # forbidden pattern was never found and the exclusion silently never
    # fired. `\s+` requires a whitespace character; a colon is neither
    # whitespace nor `\w`, so the lookahead's own forbidden-pattern search
    # dead-ended one character in. Confirmed live on SIG id=9171, whose
    # extractor read the programme's OWN start year (2026) off this sentence
    # as if it were the applicant's stated graduation year.
    #
    # The second alternative is the body's own "Class of 2028". The TITLE's
    # "Class of" is `classify.extract_class_year`'s job and lands in
    # `Opportunity.class_year`; this is the same statement made in prose,
    # which that extractor never sees. Measured live: 32 open campus rows say
    # "Class of 20XX" in the body and 17 of them carried neither a title year
    # nor a grad fact -- Oliver Wyman's "Eligibility : Class of 2028 grads
    # only", SIG's "Class of 2027 or 2028 :", Apollo's "Pursuing a BA or BS
    # (Class of 2028)" -- so the verdict and the scorer were blind to a
    # sentence that says exactly who the role is for. The lookbehinds refuse
    # a programme noun directly before "Class": Houlihan Lokey writes "The
    # position is for a Summer Analyst Class of 2027 (i.e. Graduate Class of
    # 2028 Financial Analyst)", where the first "Class of" names the INTAKE
    # cohort and only the second names the graduating class.
    r"(?:(?:graduat\w*\b(?![\s:;,\-–—]*(?:\w+\s+){0,2}(?:programme|program|scheme)\b)"
    r"|degree completion)[^.\n]{0,%d}?((?:19|20)\d{2})"
    r"|(?<![\w-])(?<!analyst\s)(?<!associate\s)(?<!intern\s)(?<!interns\s)"
    r"class\s+of\s+((?:19|20)\d{2}))" % _NEAR, re.IGNORECASE)

# Every further year the same statement names, read from the end of the
# previous one: "between December 2027 and June 2028" is one connector and
# one more year; Jefferies' "Expected graduation between December 2027 – June
# 2028 and December 2028 – June 2029" is three. Anchored with `.match` at the
# exact end of the last year so it can only ever continue the statement it
# is already inside, never wander into the next sentence.
_GRAD_MORE = re.compile(
    r"\s*(?:-|–|—|to|and|through|or|,|/)\s*"
    r"(?:[\w'/]+\s+){0,3}?((?:19|20)\d{2})", re.IGNORECASE)

# An OPEN upper bound. A firm that writes "graduating in 2028 or later" has
# named a floor and no ceiling, and storing that as the closed window [2028,
# 2028] told every later class the role was not for them -- measured live,
# 34 open campus rows (Optiver, Baird, Bank of America, Wells Fargo, PJT,
# Deutsche Bank, Société Générale, Morgan Stanley) blocked a 2029 student
# on a sentence that includes them. The cue sits in one of two
# places: AFTER the last year ("2028 or later", "December 2028 onwards",
# "2028 and beyond", "2028 or subsequent years") or BEFORE a year, as the
# gap that leads into it ("graduating in 2028 or AFTER 2027 September" --
# Optiver's OR-list, where "after 2027 September" is the open half;
# PJT's "graduation NO EARLIER THAN June 2026"). "later"/"after" need their
# conjunction so that "graduate in 2027 after completing a four-year degree"
# is not read as a window; "no later than" is a ceiling, not a floor, and
# neither pattern matches it.
_OPEN_HIGH_AFTER = re.compile(
    r"\s*(?:(?:or|and)\s+(?:later|after|beyond|subsequent|thereafter)"
    r"|(?:(?:or|and)\s+)?onwards?)\b", re.IGNORECASE)
_OPEN_HIGH_BEFORE = re.compile(
    r"\b(?:after|(?:no|not)\s+earlier\s+than|no\s+sooner\s+than)\s+"
    r"(?:[\w'/]+\s+){0,2}$", re.IGNORECASE)

# The plausible graduation-year window. A posting talking about graduation
# in 2019 is describing its own history, and a year past the ceiling is a
# requisition number, not a class. Same window `classify._YEAR` draws for
# titles. GRAD_YEAR_MAX is also the horizon an OPEN window is enumerated up
# to -- see `extract_grad_years`.
GRAD_YEAR_MIN, GRAD_YEAR_MAX = 2024, 2035


def extract_grad_years(text: str) -> dict | None:
    """The graduation window a description states, or None.

    {"value": "2027–2028", "years": ["2027", "2028"], "phrase": ...}

    `years` is every year the statement names, sorted and de-duplicated, so
    `min`/`max` are the window's bounds and two windows in one sentence
    (Jefferies' "December 2027 – June 2028 and December 2028 – June 2029")
    come back as their union. An open-ended statement ("2028 or later")
    additionally carries `open_high: True`, its `value` reads "2028+", and
    `years` runs from the floor to GRAD_YEAR_MAX: the horizon is the
    product's, not the posting's, and the flag is what says so. It is
    enumerated rather than left at the floor alone because the ranking
    module reads the years and nothing else (`recommend.Candidate.
    grad_years`), and a window that stops at its floor reads there as a
    single year -- which is precisely the closed-window reading this flag
    exists to retract.
    """
    # Every match, not just the first: when a description mentions a year
    # outside the plausible window early ("our graduate scheme, running since
    # 2019...") the REAL statement is usually still ahead, and returning None
    # on the first miss threw it away.
    for m in _GRAD.finditer(text):
        year_group = 1 if m.group(1) else 2
        # (year, start-of-gap-before-it, end) for each year the statement
        # names. The gap is what an open-bound cue sits in when it leads into
        # a year rather than trailing the last one.
        captured = [(m.group(year_group), m.start(), m.end(year_group))]
        end = m.end(year_group)
        while True:
            more = _GRAD_MORE.match(text, end)
            if not more:
                break
            captured.append((more.group(1), end, more.end(1)))
            end = more.end(1)
        # `$` in the BEFORE pattern binds to `endpos`, the year's own start,
        # so the cue has to lead straight into the year it opens.
        open_high = any(_OPEN_HIGH_BEFORE.search(text, gap_start, y_end - len(y))
                        for y, gap_start, y_end in captured)
        trailing = _OPEN_HIGH_AFTER.match(text, end)
        if trailing:
            open_high, end = True, trailing.end()
        years = sorted({y for y, _, _ in captured
                        if GRAD_YEAR_MIN <= int(y) <= GRAD_YEAR_MAX}, key=int)
        if not years:
            continue
        # Sorted before the label is built, not just taken in the order the
        # regex captured them: "or" joins an OR-list as often as it joins a
        # range (RBC's "graduating in Spring 2028 or December 2027"), and the
        # connector regex above matches "or" identically to "-"/"to"/"and",
        # so a later-year-first OR-list produced years=['2028','2027'] and a
        # rendered chip reading "Grad 2028–2027" -- a backwards range for
        # what is actually "2028, or already graduated by Dec 2027".
        # Confirmed live on 6 rows across Optiver and RBC Capital Markets.
        fact = {"phrase": _sentence(text, m.start(), end)}
        if open_high:
            lo = int(years[0])
            fact.update(value=f"{lo}+", open_high=True,
                        years=[str(y) for y in range(lo, GRAD_YEAR_MAX + 1)])
        else:
            fact.update(value=years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}",
                        years=years)
        return fact
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
#
# `n't\b`, not just `\bnot\b`: PwC Canada's own template runs "We'd love it
# even more if you're bilingual in English and French, however this ISN'T A
# REQUIREMENT" on every one of its co-op postings -- a contraction, which
# `\bnot\b` cannot see (there is no standalone word "not" in "isn't"), so
# the language survived as an unqualified "French" chip on a posting that
# explicitly disclaims it. `n't\b` alone (no leading `\b`, since the
# apostrophe already breaks the preceding word) matches "isn't"/"doesn't"/
# "aren't"/"wasn't"/"weren't"/"won't"/"wouldn't"/"can't" the same way.
# "requirement" (the noun) is added to the target list for the same
# posting: "required" (the adjective this list already carried) does not
# match inside "requirement" -- the two words share a prefix, not a
# substring relationship -- so the gate silently never fired on a sentence
# that states the noun. Confirmed live: 42 of 214 open `language` facts (PwC
# Canada, every co-op/intern track) carried this exact false "French"
# requirement before this fix.
_LANG_NEGATED = re.compile(
    r"(?:\bnot\b|n't\b)[^.\n]{0,20}?"
    r"(?:required|requirement|essential|necessary|mandatory|needed|a must)\b",
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


# A negation sitting ahead of, or INSIDE, the span a YES-pattern matched --
# "we do NOT REQUIRE a transcript" (ahead of "cover letter"/"transcript"
# entirely), "a cover letter ISN'T required" (between the anchor phrase and
# the trigger word, inside the match's own non-greedy filler). Both
# `_COVER_NO` and `_TRANSCRIPT_NO` below only ever look for a negation
# stated in full AFTER the anchor phrase ("cover letter ... not required"),
# so neither shape is visible to them: the first is textually earlier than
# where they start looking, and the second is spelled as a contraction
# ("isn't") their literal "not required" text can't match either. The same
# blind spot `_LANG_NEGATED` was fixed for on `extract_languages` — a
# negation window has to span the match itself, not just the text after it.
# `n't\b` catches the contraction family ("doesn't", "isn't", "aren't") the
# same way `_LANG_NEGATED` does. No live posting has tripped either gap for
# these two extractors yet (unlike the identical shape on `language`,
# confirmed live at scale on PwC Canada) — closed pre-emptively, on the
# evidence that this exact shape recurs across extractors in this module
# rather than staying contained to the one it was first found on.
_NEGATED_NEARBY = re.compile(r"(?:\bnot\b|n't\b)", re.IGNORECASE)
_NEGATED_NEARBY_BEFORE = 25


def _requirement_negated(text: str, start: int, end: int) -> bool:
    """True when a negation sits in the span from just before `start` through
    `end` — covering both a negation stated ahead of the match and one
    folded inside it."""
    return bool(_NEGATED_NEARBY.search(
        text[max(0, start - _NEGATED_NEARBY_BEFORE):end]))


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
    if not m or _requirement_negated(text, m.start(), m.end()):
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
#
# The cents on the comma-group alternative are not decoration: Raymond James
# posts "$70,304.00-$80,500.00", and without `(?:\.\d{2})?` the pattern read
# "$70,304" and then failed to find a separator before ".00-$80,500.00", so a
# perfectly ordinary annual range extracted nothing at all.
_MONEY = r"\d{2,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}(?:\.\d{2})?"
_UNIT = r"per hour|hourly|an hour|/\s?hour|per year|annually|per annum|/\s?year|a year"

_PAY = re.compile(
    r"\$\s?(" + _MONEY + r")\s*"
    r"(?:-|–|—|to)\s*\$?\s?(" + _MONEY + r")"
    r"([^.\n]{0,40}?(?:" + _UNIT + r"))?",
    re.IGNORECASE)

# The 189 postings that state ONE figure. `_PAY` above requires a range,
# because US pay-transparency law requires one and the boards that comply post
# one — but 189 of the 2,700 open campus rows carrying detail text on
# 2026-09-01 state a single number instead and got nothing: DRW's "annual base
# salary ... is $90,000", Jump's "$250,000 per year", CIBC's "$36.00 per hour",
# PIMCO's "Salary: $ 205,000.00", BMO's "$80,000 CAD". That is more rows than
# the range pattern finds in total, and every one of them is the firm stating
# its pay as plainly as a range does.
#
# TWO WAYS TO QUALIFY, and a bare dollar figure qualifies by neither. Either a
# pay LABEL sits within 40 characters in front of it ("base salary is
# $90,000"), or a UNIT follows it ("$250,000 per year"). Without one of the
# two, "$5,000 scholarship", "$2,000,000 raised" and "a $50 gift card" all
# read as compensation. The magnitude guards in `extract_pay` then apply
# exactly as they do to a range, so the unit can never be guessed wrong.
#
# `low == high`, ALWAYS. A single figure is a single figure; the product does
# not manufacture a band around it, and `_rate`/`$Nk` already render a
# degenerate pair as one number.
_PAY_LABEL = (r"salary|salaries|base pay|basic pay|base salary|pay rate|"
              r"hourly rate|rate of pay|compensation|pay range|remuneration")
#: "... base salary for this role is $90,000". Group 1 the figure, group 2 the
#: unit phrase if the sentence also carries one.
_PAY_LABELLED = re.compile(
    r"(?:" + _PAY_LABEL + r")[^.\n]{0,40}?\$\s?(" + _MONEY + r")"
    r"([^.\n]{0,12}?(?:" + _UNIT + r"))?",
    re.IGNORECASE)
#: "$250,000 per year", "$36.00 per hour". Same two groups, unit required.
_PAY_UNITED = re.compile(
    r"\$\s?(" + _MONEY + r")\s*([^.\n]{0,12}?(?:" + _UNIT + r"))",
    re.IGNORECASE)
#: A CEILING the range path never needed and the single path does. A range
#: takes two figures and a separator to fire; one labelled figure fires on a
#: sentence like "total compensation to our clients exceeded $50,000,000",
#: where the label is real, the number is real, and the pay is somebody else's.
#: The highest campus figure on the board is Jump's $250,000, so a million is
#: an order of magnitude of headroom and still an order of magnitude below the
#: deal sizes these descriptions quote.
_PAY_ANNUAL_MAX = 1_000_000


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



# A multi-location posting lists one rate per city under a single "Pay
# Range" heading, and the FIRST match in document order is not any kind of
# "primary" figure -- it is simply whichever city the board happened to
# print first. Returning on that first match silently drops every other
# city's rate from the same block, including a higher one: Wells Fargo id
# 5079 states "Charlotte, NC: $33.66/Hour ... Minneapolis, MN:
# $37.02/Hour" and stored only $33.66, with the Minneapolis figure never
# looked at again. All matches sharing the anchor's hourly/annual
# classification and sitting within this many characters of it are merged
# into one low-to-high range instead -- bounded so a real second pay
# statement much later in a long posting (a signing bonus, a different
# role's rate) is not pulled into the same figure. Wide enough for the
# widest block observed live (Wells Fargo, 6 cities, ~230 chars).
_PAY_BLOCK_SPAN = 500

# The number is USD unless the posting says otherwise -- and several TD
# Securities Canada postings do, right after the range ("$52,700 - $74,400
# CAD"). Dropping that code entirely (the previous behaviour) rendered a
# CAD figure as an indistinguishable bare "$" chip next to genuinely-USD
# chips from other firms on the same board. Checked live: the code always
# sits within a few characters of the matched range, never buried in
# unrelated prose further away.
_CURRENCY = re.compile(r"\b(USD|CAD|GBP|EUR|AUD|SGD|HKD|BMD|NZD|CHF|JPY)\b")

# Some boards run a posting's own text straight into a footer block listing
# OTHER open roles ("Open positions HR Business Partner (12-month contract)
# Sydney Experienced..."), with no period or newline separating it from the
# posting's own prose -- so _sentence's boundary search runs straight through
# into it. Confirmed live: Optiver id=2815 ("Graduate FPGA Engineer (2027
# Start - Chicago)") correctly extracts $200k as its own stated base salary
# (the sentence right before the match reads "This is a good-faith estimate
# of the base pay scale for this position") but the returned phrase ran on
# past it into "Open positions HR Business Partner (12-month contract)
# Sydney Experienced...", three unrelated Sydney roles that have nothing to
# do with this posting or its pay. The value is unaffected; only the quoted
# evidence needs trimming at the footer's own boundary.
_OTHER_POSTINGS_FOOTER = re.compile(r"\bOpen positions\b", re.IGNORECASE)


def _trim_footer(phrase: str) -> str:
    m = _OTHER_POSTINGS_FOOTER.search(phrase)
    if not m or m.start() == 0:
        return phrase
    trimmed = phrase[:m.start()].rstrip()
    return trimmed if trimmed else phrase


def _pay_candidates(text: str) -> list:
    """Every (match, low, high, hourly) the RANGE pattern justifies."""
    out = []
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
        elif low < 10_000:
            continue
        out.append((m, low, high, hourly))
    return out


def _pay_single(text: str):
    """The first SINGLE stated figure, as the same 4-tuple with low == high.

    A strict fallback, run only when the range pattern found nothing, and it
    returns ONE match rather than merging nearby ones the way the range path
    does. The merge exists because a multi-city posting states one range per
    city under a single heading; singles have no such guarantee, and merging
    "$90,000 base salary" with a "$10,000 signing bonus" 200 characters later
    would invent a "$10k-$90k range" that no firm wrote. One figure, one
    number, `low == high`.
    """
    best = None
    for rx in (_PAY_LABELLED, _PAY_UNITED):
        for m in rx.finditer(text):
            v = _money(m.group(1))
            if v is None:
                continue
            tail = (m.group(2) or "").lower()
            # The unit comes from the posting's own words where it gave any,
            # and from magnitude where it did not — the same two-step the
            # range path uses, so a single figure can never be classified by a
            # rule the range beside it would not have used.
            hourly = "hour" in tail if tail else v < 500
            if hourly:
                if v > 500:
                    continue
            elif not 10_000 <= v <= _PAY_ANNUAL_MAX:
                continue
            if best is None or m.start() < best[0].start():
                best = (m, v, v, hourly)
            break
    return best


def extract_pay(text: str) -> dict | None:
    candidates = _pay_candidates(text)
    if not candidates:
        single = _pay_single(text)
        if single is None:
            return None
        candidates = [single]

    anchor = candidates[0]
    hourly = anchor[3]
    group = [c for c in candidates
             if c[3] == hourly and c[0].start() - anchor[0].start() <= _PAY_BLOCK_SPAN]
    low = min(c[1] for c in group)
    high = max(c[2] for c in group)

    if hourly:
        value = (f"{_rate(low)}/hr" if low == high
                 else f"{_rate(low)}–{_rate(high)}/hr")
    else:
        value = (f"${int(low) // 1000}k" if low == high
                 else f"${int(low) // 1000}k–${int(high) // 1000}k")

    currency = None
    for m, *_rest in group:
        found = _CURRENCY.search(text[max(0, m.start() - 10): m.end() + 20])
        if found:
            currency = found.group(1).upper()
            break
    if currency and currency != "USD":
        value = f"{value} {currency}"

    span_start = min(c[0].start() for c in group)
    span_end = max(c[0].end() for c in group)
    return {"value": value, "low": low, "high": high,
            "unit": "hour" if hourly else "year",
            "currency": currency,
            "phrase": _trim_footer(_sentence(text, span_start, span_end))}


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
    # A stage in its own right, not just noise ahead of "or recent
    # graduate": SIG's own template states eligibility as "a current
    # college student or recent graduate", and before this entry existed
    # "current college student" matched nothing, so only the OR-detector
    # below could recognise it — a bare, un-OR'd "current student" mention
    # deserves the same chip a bare "final-year student" gets.
    (r"current(?:\s+(?:college|university))?\s+student", "Current student"),
    (r"recent graduate", "Recent graduate"),
    (r"first[- ]year student", "First year"),
)
_STUDY_RX = [(re.compile(p, re.IGNORECASE), label) for p, label in _STUDY_STAGE]

# A posting states an OR-eligibility disjunction ("current college student
# OR recent graduate", "final-year student or recent graduate") far more
# often than it states a single stage — SIG, PwC, IMC Trading, Optiver and
# Carlyle all use exactly this shape to say they welcome BOTH groups. The
# single-stage loop below used to `return` on the first regex that matched
# anywhere in the text, so a disjunction always collapsed to whichever named
# group's pattern happened to run first in _STUDY_STAGE, silently dropping
# the other half of the posting's own eligibility statement — "current
# college student or recent graduate" became just "Recent graduate", which
# reads as excluding the current students the posting explicitly welcomes.
# Confirmed live on 16 open rows across 6 firms (SIG ids 8935/9042/9017/9016,
# PwC ids 15845/15423/15417/15408/10088/6570, IMC Trading id 2980, Optiver
# ids 2798/2797/2796, Carlyle id 688).
#
# Checked ahead of the single-stage loop, and built from the SAME phrase
# list, so a disjunction is recognised as one match spanning both named
# groups rather than two independent single-stage matches racing each
# other.
_STUDY_PHRASE = "(?:%s)" % "|".join(p for p, _ in _STUDY_STAGE)
_STUDY_OR = re.compile(
    r"(%s)\s*(?:/|,)?\s*or\s*(%s)" % (_STUDY_PHRASE, _STUDY_PHRASE),
    re.IGNORECASE,
)

# A study-stage phrase can name who is SPEAKING at an event rather than who
# may apply to it — Bank of America's own insight-event template runs
# "you'll hear from... senior leaders, recent graduates, as well as our
# recruitment team" and "gain firsthand insights from recent graduates about
# life at Bank of America", both of which named recent grads as panelists,
# not attendees. Confirmed live on 9 open BofA rows (5634, 5376, 5379, 5380,
# 5381, 5382, 18061, 4741, 1839): every one produced a false "Recent
# graduate" eligibility chip sourced from exactly this speaker-bio sentence
# shape, none of them an eligibility statement.
#
# The gate looks BACKWARD from the match for a "hear from"/"insights from"
# governing verb within the same clause — the two confirmed shapes both put
# that verb ahead of the stage phrase, sometimes with a run of appositive
# nouns between them ("hear from, and have the opportunity to ask questions
# to, our senior leaders, recent graduates,"), which is why the window is
# wide rather than an adjacent-word check.
_STUDY_SPEAKER_CONTEXT = re.compile(
    r"(?:hear from|gain[^.\n]{0,40}?insights?\s+from|insights?\s+from|"
    r"panel(?:ists)?\s+(?:of|include)|speakers?\s+(?:include|:))",
    re.IGNORECASE,
)
_STUDY_CONTEXT_WINDOW = 150


def _study_label(phrase: str) -> str | None:
    """Which _STUDY_STAGE label a phrase captured by _STUDY_OR belongs to.

    The compound regex proves the SHAPE ("stage or stage") but, because both
    halves share one alternation, not which alternative matched which side —
    this re-tests the short captured span against each individual pattern to
    find out.
    """
    stripped = phrase.strip()
    for rx, label in _STUDY_RX:
        if rx.fullmatch(stripped):
            return label
    return None


def extract_study_stage(text: str) -> dict | None:
    for m in _STUDY_OR.finditer(text):
        window = text[max(0, m.start() - _STUDY_CONTEXT_WINDOW):m.start()]
        if _STUDY_SPEAKER_CONTEXT.search(window):
            continue
        label_a, label_b = _study_label(m.group(1)), _study_label(m.group(2))
        if not label_a or not label_b:
            continue
        if label_a == label_b:
            value = label_a
        else:
            # Lower-cased mid-sentence, same as any second clause after
            # "or" — the label vocabulary is Title Case only because each
            # one can also stand alone as a chip's own leading word.
            value = f"{label_a} or {label_b[0].lower()}{label_b[1:]}"
        return {"value": value, "phrase": _sentence(text, m.start(), m.end())}
    for rx, label in _STUDY_RX:
        for m in rx.finditer(text):
            window = text[max(0, m.start() - _STUDY_CONTEXT_WINDOW):m.start()]
            if _STUDY_SPEAKER_CONTEXT.search(window):
                continue
            return {"value": label, "phrase": _sentence(text, m.start(), m.end())}
    return None


# --- Programme length ------------------------------------------------------
# "10 weeks" is a fact about whether a summer is spoken for, and postings
# state it far more often than they state a deadline. Gated on the programme
# words either side, so a "10 week" mention of something else in the body
# cannot become the programme's length.
#
# A stated range ("8-12 weeks internship") carries an optional low bound
# ahead of the digit group that actually sits next to "week(s)". Without it,
# the mandatory digit group anchors on whichever number is ADJACENT to
# "week(s)" -- for "8-12 weeks" that is "12", and the match starts there,
# never reaching back to capture the "8-". The chip then reads a fixed
# 12-week programme for something the posting itself states as 8-12 weeks.
# Confirmed live: McKinsey id=1946 ("During your 8-12 weeks internship...")
# and Morgan Stanley id=1852 ("This is 8-10 weeks internship program...").
_WEEKS = re.compile(
    r"\b(?:(\d{1,2})\s*[-–]\s*)?(\d{1,2})[- ]week[s]? (?:summer )?(?:internship|programme|program|placement|analyst)|"
    r"(?:internship|programme|program|placement) (?:is |runs |lasts )?"
    r"(?:for )?(?:approximately )?(?:(\d{1,2})\s*[-–]\s*)?(\d{1,2}) weeks",
    re.IGNORECASE)


def extract_duration(text: str) -> dict | None:
    for m in _WEEKS.finditer(text):
        if m.group(2):
            low_raw, high_raw = m.group(1), m.group(2)
        else:
            low_raw, high_raw = m.group(3), m.group(4)
        try:
            weeks = int(high_raw)
        except (TypeError, ValueError):
            continue
        # A campus programme is a summer or a placement year, never one week
        # or sixty. Outside that band the number belongs to something else.
        if not (2 <= weeks <= 52):
            continue
        low = None
        if low_raw is not None:
            try:
                low_val = int(low_raw)
            except (TypeError, ValueError):
                low_val = None
            if low_val is not None and 2 <= low_val <= weeks:
                low = low_val
        value = f"{low}-{weeks} weeks" if low is not None else f"{weeks} weeks"
        return {"value": value, "weeks": weeks, "low_weeks": low,
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
    if not m or _requirement_negated(text, m.start(), m.end()):
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
