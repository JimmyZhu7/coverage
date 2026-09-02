"""The pre-send mismatch guard: a draft that names the wrong bank or the
wrong person must not get a Copy button.

THE SENTENCE THIS EXISTS FOR. A bulge-bracket banker, asked what actually
sinks a networking email: "I get 10+ resumes a season with the wrong bank or
wrong name (or both)", and wrong-name or wrong-bank "narrows the field down
way more than it should" (`research-outreach-mechanics.md §5b`, Grade A). The
same file's §9.3 draws the product conclusion: a merge-field or bulk-template
drafting system WITHOUT this guard is a machine for producing that error class
at scale, which is why item 34 of the do-not-build register forbids the former
until the latter ships. Independently, a recruiter reported receiving twelve
identical cold emails in one week (`research-nontarget-access.md §6`, Grade A).

WHY IT IS CHEAP AND WHY THAT MATTERS. Every other quality question about a
draft needs a model. This one does not: the recipient's firm and name are
already on the row, the draft is already parsed, and a string search settles
it. `assistant.drafts` already keeps two guards on exactly that argument (a
bracketed placeholder, a body over its word cap), and this is the third and
the only one with a counted failure behind it.

PURE, AND DELIBERATELY SO. No Django import, no database, no model call. It
takes the draft's subject and body and one `recipient` dict, and returns a
reason or None. That is what lets `drafts.flag_reason` call it and the chat
page's JavaScript mirror reimplement it against the SAME fixtures — the
existing index-pairing requirement means the two implementations must agree
segment for segment or a card's log-touch chip lands on the wrong person.

CONSERVATIVE IN ONE DIRECTION ONLY. A false negative costs one draft that
should have been demoted and was not; the student still reads every word. A
false positive costs the Copy button on a correct draft, which is the more
expensive error because it is invisible in the other direction — the student
concludes the feature is broken. So a firm name only counts as a mismatch when
it appears as a WORD, the recipient's own firm and its aliases are subtracted
first, and a bare first name is never enough to convict on its own.
"""

from __future__ import annotations

import re

FLAG_MISMATCH = "mismatch"

# Firms whose names a cold networking draft plausibly gets wrong, with the
# spellings a student or a model actually types. Not the directory table:
# this module is pure and the check has to run identically in the browser, so
# the list is data both sides can hold. It does not need to be exhaustive to
# be useful — it needs to cover the firms a student is writing to, which is
# the same short head of the distribution the founder's own 44-person blast
# hit (Citi, Goldman, J.P. Morgan, Morgan Stanley).
#
# Key is the canonical name; the tuple is every spelling that means it,
# lower-cased. A draft is only convicted when it names a firm in this list
# that is NOT the recipient's.
FIRM_ALIASES: dict[str, tuple[str, ...]] = {
    "Goldman Sachs": ("goldman sachs", "goldman", "gs"),
    "Morgan Stanley": ("morgan stanley",),
    "J.P. Morgan": ("j.p. morgan", "jp morgan", "jpmorgan", "jpm"),
    "Bank of America": ("bank of america", "bofa", "merrill lynch", "merrill"),
    "Citi": ("citigroup", "citi"),
    "Barclays": ("barclays",),
    "UBS": ("ubs",),
    "Credit Suisse": ("credit suisse",),
    "Deutsche Bank": ("deutsche bank", "deutsche"),
    "HSBC": ("hsbc",),
    "Nomura": ("nomura",),
    "Jefferies": ("jefferies",),
    "Lazard": ("lazard",),
    "Evercore": ("evercore",),
    "Centerview": ("centerview",),
    "Moelis": ("moelis",),
    "PJT Partners": ("pjt partners", "pjt"),
    "Perella Weinberg": ("perella weinberg", "perella"),
    "Rothschild": ("rothschild",),
    "Houlihan Lokey": ("houlihan lokey", "houlihan"),
    "Guggenheim": ("guggenheim",),
    "Blackstone": ("blackstone",),
    "KKR": ("kkr",),
    "Apollo": ("apollo global", "apollo"),
    "Carlyle": ("carlyle",),
    "McKinsey": ("mckinsey",),
    "BCG": ("boston consulting group", "bcg"),
    "Bain": ("bain & company", "bain and company", "bain"),
    "BlackRock": ("blackrock",),
    "PIMCO": ("pimco",),
    "Jane Street": ("jane street",),
    "Citadel": ("citadel securities", "citadel"),
    "RBC": ("rbc", "royal bank of canada"),
    "Wells Fargo": ("wells fargo",),
}

# Aliases sorted longest-first inside each firm so "bank of america" is tested
# before "bofa" could ever matter, and compiled once. `\b` on both sides is
# what keeps "gs" out of "things" and "bain" out of "Bainbridge"; the dots in
# "j.p. morgan" are escaped by `re.escape`.
_FIRM_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (
        canonical,
        re.compile(
            "|".join(
                rf"\b{re.escape(a)}\b"
                for a in sorted(aliases, key=len, reverse=True)
            ),
            re.IGNORECASE,
        ),
    )
    for canonical, aliases in FIRM_ALIASES.items()
)

# A greeting line's name: "Hi Dana," / "Hello Ms. Reed," / "Dear Dana Reed —".
# Only the greeting is checked, never the body, because a body legitimately
# names other people ("Priya suggested I reach out") and convicting on that
# would demote exactly the drafts with a real referral in them, which are the
# best drafts the product produces.
_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|dear|good morning|good afternoon)[ \t]+"
    r"(?:mr\.?|ms\.?|mrs\.?|dr\.?|prof\.?)?[ \t]*"
    r"(?P<name>[A-Z][\w'\-]+(?:[ \t]+[A-Z][\w'\-]+)?)",
    re.IGNORECASE | re.MULTILINE,
)


def _firms_named(text: str) -> set[str]:
    return {canonical for canonical, rx in _FIRM_RES if rx.search(text)}


def _recipient_firm_aliases(firm: str) -> set[str]:
    """Every canonical firm the recipient's own firm text could BE.

    The recipient's firm arrives as free text (`Contact.firm_text`, or a
    directory firm's name), so "J.P. Morgan", "JPMorgan Chase" and "jpm" all
    have to subtract the same canonical name. A firm text that matches nothing
    in the table subtracts nothing, which is the safe direction: the draft is
    then only convicted if it names some OTHER firm outright.
    """
    return _firms_named(firm or "")


def _greeting_names(text: str) -> list[str]:
    return [m.group("name").strip() for m in _GREETING_RE.finditer(text or "")]


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^\w'\-]+", (name or "").lower()) if len(t) > 1}


def mismatch_reason(subject: str, body: str, recipient: dict | None) -> str | None:
    """Why this draft names the wrong recipient, or None.

    `recipient` is `{"name": ..., "firm": ...}` for the contact the draft is
    addressed to, or None when the fence never named a valid contact. None
    means NO CHECK: the guard has nothing to compare against, and inventing a
    recipient to compare against would be worse than not checking (P1). That
    is also what makes this additive — every existing caller that passes
    nothing gets exactly today's behaviour (P3).

    Returns a sentence naming what is wrong, because the card is demoted to
    prose and the student has to be told which of the two it was. A demotion
    with no reason is the invisible filter P4 forbids.
    """
    if not recipient:
        return None
    text = f"{subject or ''}\n{body or ''}"

    own = _recipient_firm_aliases(str(recipient.get("firm") or ""))
    named = _firms_named(text)
    wrong_firms = sorted(named - own)
    if wrong_firms:
        firm_text = str(recipient.get("firm") or "").strip()
        where = f" who is at {firm_text}" if firm_text else ""
        return (
            f"This draft names {wrong_firms[0]}, but it is addressed to "
            f"{recipient.get('name') or 'this contact'}{where}."
        )

    # The greeting, and only the greeting. A first name that appears anywhere
    # in the recipient's own name clears it — "Hi Dana" for Dana Reed, "Hi Ms.
    # Reed" for Dana Reed — so a draft is convicted only when the greeting
    # names somebody with no token in common with the recipient at all.
    own_tokens = _name_tokens(str(recipient.get("name") or ""))
    if not own_tokens:
        return None
    for greeted in _greeting_names(text):
        if not _name_tokens(greeted) & own_tokens:
            return (
                f"This draft opens to {greeted}, but it is addressed to "
                f"{recipient.get('name')}."
            )
    return None
