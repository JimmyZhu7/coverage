"""Site-wide text standardization filters.

The scraped data arrives in every casing a careers site can produce —
"OLIVER WYMAN - INTERN CONSULTANT – 2026 – NETHERLANDS", "international
Corporate Tax Associate", "Fund Finance (TRECO), Associate ". The stored rows
stay raw (the honesty rule: we never rewrite evidence), and presentation is
standardized here at render time instead: firm names, job titles, and person
names pass through `smart_title` wherever they appear.

`smart_title` is Title Case with three domain-aware guards a naive
`str.title()` gets wrong:

1. **Mixed-case tokens are preserved.** "PwC", "BofA", "McKinsey", "iCIMS"
   already encode their own branding — re-casing them is vandalism.
2. **Short all-caps tokens are acronyms** ("TPG", "RBC", "M&A", "IBD", "HK")
   and stay all-caps; long all-caps tokens are shouting ("NETHERLANDS") and
   get title-cased. The cut is 4 letters, plus a small whitelist for longer
   acronyms ("EMEA", "APAC", "NASDAQ").
3. **Minor words stay lowercase** mid-phrase ("Head of Diversity", "Women in
   Banking") but are capitalized as the first or last word — standard
   title-case convention rather than capitalize-every-word, which reads
   amateur in a finance product. A colon, semicolon, or standalone dash/pipe
   RESTARTS that convention, because the word after one opens a new clause
   ("Insight Forum: The Power to Lead", not "...: the Power to Lead").

Hyphen and slash compounds are cased per part ("off-cycle" → "Off-Cycle",
"m/f/d" → "M/F/D"). Whitespace is collapsed, which also trims the stray
trailing spaces real postings ship with.
"""

from __future__ import annotations

import hashlib
import re

from django import template
from django.utils.timesince import timesince as _timesince

register = template.Library()

# Words that stay lowercase unless first or last in the phrase. Includes the
# common Romance-language connectives that show up in global boards'
# postings ("Associate de Auditoría Financiera") — and, on the second row,
# the Germanic/Scandinavian ones, which were missing while their Romance
# equivalents were here from the start. The asymmetry showed up in two
# places at once:
#
#   'Ebba af Klercker'                  -> 'Ebba Af Klercker'
#   'Trainee in der Steuerberatung'     -> 'Trainee in Der Steuerberatung'
#   'Berlin, Unter den Linden 13-15'    -> 'Berlin, Unter Den Linden 13-15'
#
# The first is the founder's own contact row, and the exact name
# `crm.models.ContactMerge` cites as the duplicate-merge feature's
# motivating example, so it renders on the Settings > Duplicate Contacts
# card. The other two are live scraped rows: twelve German-language PwC/EY
# titles carry "in der", and three Deutsche Bank locations carry a street
# name built on "den"/"der" — all of them written lowercase by a source
# that knows its own orthography, and all of them recapitalized here.
_MINOR = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "into", "nor",
    "of", "on", "or", "per", "so", "the", "to", "via", "vs", "with",
    "de", "del", "della", "di", "da", "du", "des", "la", "le", "el", "y", "e",
    # Germanic and Nordic nobiliary particles, added 2026-09-01. The founder's
    # own board carries "Ebba af Klercker" -- the very row `ContactMerge`'s
    # docstring cites as the case the merge feature was built for -- and it
    # rendered "Ebba Af Klercker" on the Decisions card because "af" was not
    # here.
    #
    # BLAST RADIUS, measured rather than assumed. A first pass of this comment
    # claimed "no Firm name contains these, so only that one contact changes",
    # which was true of firm names and wrong about everything else: this filter
    # also runs over Opportunity titles and locations, and German and Danish
    # postings carry these words constantly. Re-checked against live rows, the
    # other hits are all improvements rather than regressions --
    # "Berlin, Unter den Linden 13-15" is a real street that read "Unter Den
    # Linden" before, and "Trainee in der Steuerberatung" now reads as German
    # rather than as title case applied to German. Kept for that reason, not
    # because the change was narrow.
    #
    # A LEADING particle is still capitalised (the `force_cap` branch below
    # runs first), so a firm like "Van Lanschot" keeps its capital V.
    "af", "van", "von", "der", "den", "ter",
}

# Punctuation that ends the clause before it, so the word AFTER it is a first
# word for title-case purposes. Without this, force_cap applied only to index 0
# and the final index OF THE WHOLE STRING, so a title that restarts mid-way
# came out with a lowercase word opening its second half: "Bank of America
# Campus Insight Forum: the Power to Lead", "2026 Women Who Lead: an Insight
# into Banking", "Senior Premier Banker - la Cienega Corridor", "APAC Virtual
# Recruitment Event | a Career with Bank of America", "Business Manager | S3 |
# t&o | Milton Keynes".
_CLAUSE_BREAKS = {"-", "–", "—", "|", "·"}


def _restarts_a_clause(prev: str) -> bool:
    return prev.endswith((":", ";")) or prev in _CLAUSE_BREAKS


def _is_minor(word: str) -> bool:
    """Whether the minor-word rule applies to this whitespace-delimited token.

    The rule is about ONE word, so the letters that spell the particle have to
    come from one atom. `_letters()` measures the whole token with the
    separators stripped out, which lets a hyphen compound spell a particle
    across the gap: the building code "A-12F" measured as the two letters
    "AF", matched the Swedish particle added above, and three live 'Honhui
    A-12F' rows rendered 'Honhui a-12f'.

    Deliberately narrower than "reject every compound". A particle really does
    arrive welded to punctuation on one side — the "/or" of "English and /or
    French", the "des-" of "Rivière- des- Prairies", the "in-" of "Audit in-
    Charge" — and each of those is still a single lettered atom, so each still
    gets the rule. Only a token with letters on BOTH sides of a separator is
    disqualified, because that shape is a code, never a word.
    """
    atoms = [a for a in _SUB_SEP.split(word) if _letters(a)]
    return len(atoms) == 1 and _letters(atoms[0]).lower() in _MINOR

# All-caps tokens longer than the 4-letter acronym cut that are still
# genuinely acronyms/brands, not shouting.
_ACRONYMS = {"EMEA", "APAC", "LATAM", "NYSE", "NASDAQ", "FICC", "BRICS"}

# Acronyms recognized regardless of how the DB happens to have stored their
# case. Contact.firm_text in particular is free text a student typed, so the
# same acronym shows up as "USC" from one contact and "usc" from another —
# the acronym is a fact about the token, not about which casing landed in
# the DB. Deliberately small and manually curated (not "any short lowercase
# token"): most short lowercase words are ordinary words ("the", "her",
# firm names like "sap"), and defaulting them to all-caps would be its own
# mis-casing bug.
_ACRONYMS_ANY_CASE = _ACRONYMS | {"USC"}

_SPLIT_RE = re.compile(r"(\s+)")
_SUB_SEP = re.compile(r"([-/])")

# An all-caps letter run with a symbol stitched into the middle of it
# ("E*TRADE", "AT&T") is stylized branding, not shouting — but `_letters()`
# strips the symbol before measuring length, so "E*TRADE" measured only
# "ETRADE" (6 letters), sailed past the 4-letter acronym cutoff, and was
# recapped to "E*trade". A symbol embedded between two letters is itself
# the signal that this is an intentional brand mark, independent of how
# many letters surround it.
_EMBEDDED_SYMBOL = re.compile(r"[A-Za-z][*&][A-Za-z]")


def _letters(token: str) -> str:
    return "".join(ch for ch in token if ch.isalpha())


def _case_atom(atom: str) -> str:
    """Case one indivisible word (no spaces, hyphens, or slashes)."""
    letters = _letters(atom)
    if not letters:
        return atom  # "2026", "—", "&"
    if letters.isupper():
        # Acronym vs shouting: short stays, long gets title-cased. A
        # parenthesized all-caps token is a ticker/fund code ("(TRECO)")
        # whatever its length — preserve it, as is a symbol-stitched brand
        # mark like "E*TRADE".
        if (len(letters) <= 4 or letters in _ACRONYMS
                or _EMBEDDED_SYMBOL.search(atom) or atom.startswith("(")):
            return atom
        return atom[:1] + atom[1:].lower() if atom[:1].isalpha() else _recap(atom)
    if not letters.islower() and not letters.istitle():
        return atom  # mixed case: PwC, BofA, McKinsey, iCIMS — their branding
    if letters.islower() and letters.upper() in _ACRONYMS_ANY_CASE:
        return letters.upper()
    return _recap(atom)


# An ordinal suffix is not the start of a word. "745 7th Avenue" tokenizes to
# "7th", whose first LETTER is the "t" — so `_recap` uppercased it and the app
# manufactured "745 7Th Avenue", "9Th Floor", "83Rd Ave", "19Th & 1St" on 140
# open rows at render time, over DB values that were clean. It lives here
# rather than in `smart_title` because `smart_location` funnels through the
# same leaf: 122 of the 140 are locations, so a fix one level up would pass
# its own unit test and change nothing on the page.
_ORDINAL_TAIL = re.compile(r"(?:st|nd|rd|th)(?![A-Za-z])", re.IGNORECASE)


def _recap(atom: str) -> str:
    """Uppercase the first letter of `atom`, lowercase the rest — unless the
    letters are an ordinal suffix hanging off the digits in front of them,
    which belong to the number, not to a word of their own."""
    for i, ch in enumerate(atom):
        if ch.isalpha():
            if i and atom[i - 1].isdigit() and _ORDINAL_TAIL.match(atom, i):
                return atom.lower()
            return atom[:i] + ch.upper() + atom[i + 1:].lower()
    return atom


def _case_word(word: str, *, force_cap: bool) -> str:
    """Case one whitespace-delimited word, handling -/ compounds and the
    minor-word rule."""
    if not force_cap and _is_minor(word):
        return word.lower()
    return "".join(
        part if part in "-/" else _case_atom(part)
        for part in _SUB_SEP.split(word)
    )


@register.filter(name="smart_title")
def smart_title(value):
    """Standardize a firm name, job title, or person name to Title Case.
    See the module docstring for the exact rules."""
    if not value:
        return value
    words = [w for w in _SPLIT_RE.split(str(value).strip()) if w.strip()]
    if not words:
        return ""
    last = len(words) - 1
    return " ".join(
        _case_word(w, force_cap=(i == 0 or i == last
                                 or _restarts_a_clause(words[i - 1])))
        for i, w in enumerate(words)
    )


# A run of lowercase letters, the only shape this filter will ever split —
# guards against "jsmith2" (a digit in the run) and "j" alone (a single
# initial, indistinguishable from a truncated split) getting pulled apart.
_LOCAL_PART_TOKEN = re.compile(r"^[a-z]{2,}$")


@register.filter(name="smart_person_name")
def smart_person_name(value):
    """Turn an email-local-part-shaped `Contact.name` into a readable one:
    'jude.yoon' -> 'Jude Yoon', 'wenyu.xiong' -> 'Wenyu Xiong'.

    `capture` stores exactly what it observed, which for a contact pulled
    off a Gmail thread is sometimes nothing better than the address's local
    part — verified on the founder's own board (2026-08-31): 43 of 226
    non-archived contacts (19%) have `name == email.split("@")[0]`
    case-insensitively, and every one of them is `source="capture"`. Real
    examples: 'jude.yoon' / 'jude.yoon@citi.com', 'wenyu.xiong' /
    'wenyu.xiong@citi.com', 'dongyoon.kim' / 'dongyoon.kim@citi.com'.

    This does not touch the stored row — `Contact.name` stays exactly what
    capture observed, honest evidence rather than a guess dressed up as one.
    The fix is presentational only, the same posture `smart_title` and
    `smart_location` already take on messier fields.

    THE SHAPE, and nothing looser than it. A local part reads as
    "word[.+_-]word[.+_-]...", so the rule fires only when ALL of these
    hold at once:
      1. no whitespace anywhere — a typed name has spaces ("Youqi Chen",
         "J.P. Morgan Recruiting"); a local part never does. This alone
         rules out most real names before the rest of the checks run.
      2. at least one of the three corporate-address separators (. _ -)
         is present, splitting the string into two or more pieces.
      3. every piece is two or more lowercase letters and nothing else —
         no digits ("jsmith2"), no mixed case ("John_Smith" is left alone:
         capitalization already present is a signal a person typed it
         deliberately, not evidence to overwrite), no single-letter pieces
         (an initial split from "j.smith" cannot be told apart from a typo,
         so the whole name is left untouched rather than guessed at).

    Each surviving piece is capitalized and rejoined with a plain space:
    the separator character itself carried no meaning of its own, it was
    just what an email system accepts in a local part.

    WHAT THIS DELIBERATELY LEAVES ALONE, checked against the real 43:
    a bare single token with no separator at all ('cv', from
    'cv@citi.com') never reaches rule 2 — there is no way to tell a
    truncated local part from a two-letter nickname, so it renders
    exactly as stored. A single already-capitalized given name ('Kirthi',
    'Matt') never reaches rule 2 either, and needs no fixing: it already
    reads as a name.

    KNOWN LIMIT: an all-lowercase hyphenated compound given name typed by
    hand with no last name ('mary-jane') is indistinguishable from a
    first.last local part and comes out split ('Mary Jane'). None of the
    real 43 take this shape — every one is a firm-issued firstname.lastname
    address — so this is a documented risk rather than a measured one.
    """
    if not value:
        return value
    text = str(value).strip()
    if not text or any(ch.isspace() for ch in text):
        return value
    # A WHOLE ADDRESS, not just the local part. Capture usually stores the
    # local part alone, but two of the founder's rows hold the full string
    # ('victoria.hsu@gs.com', 'yvonne.cheng@gs.com', both source="capture"),
    # and they rendered as "Victoria.hsu@gs.com" on the firm page and in the
    # Cmd-K palette. The domain carries nothing a reader wants in a name, so
    # it is dropped and the local part goes through the identical rule below
    # -- one shape check, not a second one. Anything with more than one "@",
    # or nothing before it, is not an address and is left alone.
    #
    # BLAST RADIUS, measured the same way the local-part rule was. Swept over
    # all 267 of the founder's contacts (2026-09-01, read-only), comparing
    # this filter against the version that had no "@" branch: exactly those
    # two rows render differently, and the other 40 rewrites are the
    # bare-local-part case that already worked. No typed name moved, because
    # reusing the shape check rather than relaxing it is what keeps the
    # domain from buying the local part any leniency -- 'cv@citi.com' is
    # still one ambiguous token and still renders as stored, and a local
    # part that arrives already capitalised ('Victoria.Hsu@gs.com') is still
    # read as deliberate and left alone.
    if "@" in text:
        local, _, domain = text.partition("@")
        if not local or "@" in domain:
            return value
        text = local
    pieces = re.split(r"[._-]+", text)
    pieces = [p for p in pieces if p]
    if len(pieces) < 2:
        return value
    if not all(_LOCAL_PART_TOKEN.match(p) for p in pieces):
        return value
    return " ".join(p[:1].upper() + p[1:] for p in pieces)


# ---------------------------------------------------------------------------
# Locations
#
# A location is not a title, and `smart_title` says so itself: its docstring
# scopes it to firm names, job titles and person names. Six templates piped
# `Opportunity.location` through it anyway, so the English TITLE-CASE
# minor-word convention ("Head of Diversity") ran over place names and
# downcased whatever particle landed mid-string:
#
#   'Batesville; Des Moines'      -> 'Batesville; des Moines'   (10 open rows)
#   'Wilmington, DE, United States' -> 'Wilmington, de, ...'    (11 open rows)
#   'Milano Via Turati 25-27'     -> 'Milano via Turati 25-27'
#   '411 E Wisconsin Ave'         -> '411 e Wisconsin Ave'
#   'Gemini Building A, Prague'   -> 'Gemini Building a, Prague'
#   'Portage La Prairie'          -> 'Portage la Prairie'
#
# The fix is NOT deleting "des" from _MINOR. Five open rows carry 'Geneva Place
# des Bergues 3', where the lowercase is correct French street casing, and
# 'RIO DE JANEIRO' depends on the rule too. The rule is right; the FIELD is
# wrong for it.
#
# So a location gets its own path, and its rule is: RESPECT THE CASE THE SOURCE
# CHOSE. A particle written lowercase stays lowercase (the source knows its own
# orthography better than we do); one written capitalized stays capitalized (it
# is part of the proper name). Only where the source offers no signal at all —
# an all-caps token — does the title-case convention get to decide.
# ---------------------------------------------------------------------------
_SEGMENT_RE = re.compile(r"[,;:\-]")


def _location_codes(text: str) -> set[str]:
    """All-caps tokens that occupy a whole delimited segment, i.e. sit in the
    "City, ST" slot rather than inside a phrase: the DE of 'Wilmington, DE,
    United States', the NY of 'NY - 375 - 18', the ON of 'ON-81 Bay Street'.

    Positional rather than a list of known codes, because the SAME token means
    different things in different places: "DE" alone between commas is
    Delaware, but the "DE" inside an undelimited run ('VILLE DE QUEBEC') is the
    Romance particle, and lowercasing that one is correct.
    """
    codes = set()

    def _code(token: str) -> str:
        """The code a lone token spells, or "". Digits are allowed alongside
        the letters — 'RO03' and 'BG1' are building codes, not words."""
        letters = _letters(token)
        if (token.isalnum() and letters.isupper() and 1 <= len(letters) <= 3):
            return letters
        return ""

    for segment in _SEGMENT_RE.split(text):
        codes.add(_code(segment.strip()))
    # ...and the trailing token, which is the state/territory slot whether or
    # not a comma announces it: 'WASHINGTON DC', 'NYC (1285)'. Measured from
    # the last word that HAS letters, so a trailing '(1285)' does not hide it.
    for word in reversed([w for w in _SPLIT_RE.split(text) if _letters(w)]):
        codes.add(_code(word.strip()))
        break
    codes.discard("")
    return codes


def _case_word_location(word: str, *, codes: set[str], shouting: bool,
                        force_cap: bool) -> str:
    letters = _letters(word)
    if letters and _is_minor(word) and not force_cap:
        if letters in codes:
            return word          # a state/country code, not a particle
        if letters.islower() or letters.istitle():
            return word          # the source made the call; it is not ours to undo
        # An ALL-CAPS particle carries no case signal of its own, so the
        # title-case convention decides: 'VILLE DE QUEBEC' -> 'Ville de Quebec'.
        return word.lower()
    if shouting and letters and letters not in codes:
        # Nothing in the whole string is lowercase, so every token is shouting
        # rather than an acronym. Fold it first so `_case_atom` recaps it
        # instead of preserving it: 'RIO DE JANEIRO' -> 'Rio de Janeiro'.
        word = word.lower()
    return "".join(
        part if part in "-/" else _case_atom(part)
        for part in _SUB_SEP.split(word)
    )


# A clause boundary in a role string: a comma, an opening paren, an em/en
# dash, or a spaced hyphen. Never a bare hyphen — that would split
# "on-campus" or "junior/senior" mid-word, which are single tokens with a
# hyphen in them, not two clauses.
_ROLE_CLAUSE_RE = re.compile(r"\s*[,(]|\s+[—–]\s*|\s+-\s+")

# Long-form -> the abbreviation this product already uses natively
# elsewhere for the same concept. Not invented: `ib`/`st`/`pe`/`am` are
# directory.classify.TRACK_LABELS' own track codes, and "IB"/"PE"/"DCM"/
# "TMT"/"VP"/"FX" already appear unabbreviated nowhere else in the real
# role data — students who typed a role themselves already default to the
# short form; only the odd one out spelled it out. Word-boundary matched,
# case-sensitive on the written form, applied BEFORE the clause/word cap
# so an abbreviation can free up room for the words that follow it.
_ROLE_ABBREVIATIONS = [
    (re.compile(r"\bInvestment Banking\b"), "IB"),
    (re.compile(r"\bPrivate Equity\b"), "PE"),
    (re.compile(r"\bAsset Management\b"), "AM"),
    (re.compile(r"\bSales (?:&|and) Trading\b"), "S&T"),
    (re.compile(r"\bTechnology\b"), "Tech"),
]


@register.filter(name="smart_role")
def smart_role(value, max_words=4):
    """Compress a contact's freeform `role` field to the first clause,
    capped at a few words, with the product's own standard abbreviations
    applied.

    The field is typed by a student about a real person and ranges from
    already-clean ("IB Analyst") to a full sentence ("Campus recruiter
    (PwC) — self-described 'the campus recruiter and primary point of
    contact' for USC students"). Verified against all 85 distinct values
    on the founder's own board before this shipped: taking everything
    before the first clause boundary and capping the remainder at four
    words never fabricates a title, since it is always a strict prefix
    of what the student actually wrote — it just stops reading once the
    role itself has been said and the elaboration starts. Abbreviating
    "Investment Banking" to "IB" is the one exception to strict-prefix:
    it is a substitution, not a cut, and only ever swaps in a form the
    same student already used unprompted elsewhere on the same board.

    Never adds an ellipsis: a short, complete-looking phrase reads as a
    real label; "Campus recruiter..." reads as broken. The unclamped
    original stays in `title=` at every call site, one hover away, so
    nothing here is actually lost — see `.cc-firm`'s own title attribute
    in contact_list.html for the doctrine this follows.
    """
    if not value:
        return value
    text = str(value).strip()
    m = _ROLE_CLAUSE_RE.search(text)
    clause = text[: m.start()] if m else text
    for pattern, short in _ROLE_ABBREVIATIONS:
        clause = pattern.sub(short, clause)
    words = clause.split()
    if len(words) > max_words:
        words = words[:max_words]
    return " ".join(words)


@register.filter(name="smart_location")
def smart_location(value):
    """Standardize a place string. Like `smart_title`, minus the title-case
    minor-word rule, which has no business in a location. See above.

    `force_cap` on the FIRST word survives, for the same reason it exists in
    `smart_title`: a particle that OPENS a name is part of it, not a
    connective inside it — 'EL DORADO HILLS, CA', 'ON-81 Bay Street'.

    It does NOT extend to the last word, which is where this differs from
    `smart_title`. Capitalizing a title's final word is an English
    convention; a place name has no such convention, and applying it made the
    app manufacture orthography no source wrote. ISO 3166 long forms end in a
    particle — the boards send 'Seoul, Korea, Republic of' and 'Taiwan,
    Province of China' — and force-capping the tail rendered 'Republic Of',
    which is not how anyone writes it and not what the DB stores.

    KNOWN LIMIT: a fully-shouting string offers no case signal to respect, so
    'WEST DES MOINES, IA' still renders 'WEST des Moines, IA' — 'DES' there is
    indistinguishable from the 'DE' of 'RIO DE JANEIRO', which the same rule
    gets right. One open row is affected; the ten mixed-case 'Des Moines' rows
    are not.
    """
    if not value:
        return value
    text = str(value).strip()
    words = [w for w in _SPLIT_RE.split(text) if w.strip()]
    if not words:
        return ""
    codes = _location_codes(text)
    shouting = not any(ch.islower() for ch in text)
    return " ".join(
        _case_word_location(w, codes=codes, shouting=shouting,
                            force_cap=(i == 0))
        for i, w in enumerate(words)
    )


@register.filter(name="timesince1")
def timesince1(value):
    """`{{ value|timesince }}`, collapsed to its coarser unit alone.

    Django's `timesince` template filter has no way to pass `depth` — its
    one argument is a comparison time, not a depth — so every template that
    wanted `depth=1` had no filter to reach for and fell back to the plain
    `timesince` tag, which defaults to `depth=2`: two units ("1 hour, 38
    minutes ago", "5 days, 13 hours ago"). That is noise in a sentence read
    at a glance rather than studied, which is why `directory.views` already
    calls `timesince(..., depth=1)` directly in Python wherever it builds
    the string itself (`checked_ago`, the closed-posting note). This filter
    is that same call, for the templates that render `timesince` straight
    off a context value instead: the closed-posting caution
    (`_role_drawer.html`), the advisor's own timestamp
    (`assistant/_message.html`), and the Gmail Live status lines
    (`accounts/settings.html`).
    """
    if not value:
        return value
    return _timesince(value, depth=1)


@register.filter(name="firm_hue")
def firm_hue(value):
    """A firm's signature hue (0–359), derived stably from its slug/name so the
    same firm always gets the same tint. Saturation and lightness are fixed in
    CSS, so every monogram shares one muted, premium color family and only the
    hue varies. Unknown firms fall back to the brand navy's hue."""
    if not value:
        return 210
    digest = hashlib.md5(str(value).strip().lower().encode("utf-8")).hexdigest()
    return int(digest, 16) % 360
