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
   amateur in a finance product.

Hyphen and slash compounds are cased per part ("off-cycle" → "Off-Cycle",
"m/f/d" → "M/F/D"). Whitespace is collapsed, which also trims the stray
trailing spaces real postings ship with.
"""

from __future__ import annotations

import hashlib
import re

from django import template

register = template.Library()

# Words that stay lowercase unless first or last in the phrase. Includes the
# common Romance-language connectives that show up in global boards'
# postings ("Associate de Auditoría Financiera").
_MINOR = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
    "on", "or", "per", "so", "the", "to", "via", "vs", "with",
    "de", "del", "della", "di", "da", "du", "des", "la", "le", "el", "y", "e",
}

# All-caps tokens longer than the 4-letter acronym cut that are still
# genuinely acronyms/brands, not shouting.
_ACRONYMS = {"EMEA", "APAC", "LATAM", "NYSE", "NASDAQ", "FICC", "BRICS"}

_SPLIT_RE = re.compile(r"(\s+)")
_SUB_SEP = re.compile(r"([-/])")


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
        # whatever its length — preserve it.
        if len(letters) <= 4 or letters in _ACRONYMS or atom.startswith("("):
            return atom
        return atom[:1] + atom[1:].lower() if atom[:1].isalpha() else _recap(atom)
    if not letters.islower() and not letters.istitle():
        return atom  # mixed case: PwC, BofA, McKinsey, iCIMS — their branding
    return _recap(atom)


def _recap(atom: str) -> str:
    """Uppercase the first letter of `atom`, lowercase the rest."""
    for i, ch in enumerate(atom):
        if ch.isalpha():
            return atom[:i] + ch.upper() + atom[i + 1:].lower()
    return atom


def _case_word(word: str, *, force_cap: bool) -> str:
    """Case one whitespace-delimited word, handling -/ compounds and the
    minor-word rule."""
    if not force_cap and _letters(word).lower() in _MINOR:
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
        _case_word(w, force_cap=(i == 0 or i == last)) for i, w in enumerate(words)
    )


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
