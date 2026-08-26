"""Region inference for the backfill command — and ONLY for the backfill.

The read path never calls this. That is the settled design, learned the hard
way: `coverage_domain.cadence.infer_region` used to answer region questions on
every read, and because it returned a confident us/hk for ANY non-empty
provenance string, "unknown" became unreachable and hand-added HK contacts
were silently re-pinged against US deadlines. Its retirement notice invites
exactly one successor: a one-time materialisation into the `region` column,
"where a human can see it and correct it". This module is that successor —
called by `backfill_contact_regions`, shown in a dry run, reviewed before a
single row is written, and never allowed to touch a region a human set.

Two rules it must never break:

  - POSITIVE EVIDENCE ONLY. The old inference's actual bug was its confident
    default ('us' for anything that didn't say "hk"). Every signal here fires
    on something that names a place; no signal, no answer, row stays blank.
  - "Unknown" and "other" are different facts. "other" is only ever proposed
    from evidence that names a place outside both markets — absence of a us/hk
    match is not that evidence.

Signals, strongest first (the order `infer_region` tries them):

  1. A touch subject's leading market tag. The founder's HK outreach subjects
     literally open with the market: "HK Jul 29–31 | Nomura | IBD - ...".
     That prefix is an explicit statement of which market the conversation
     was about — the strongest evidence a row can carry. Older touches have
     subject="" (the column post-dates them); they simply say nothing, and
     the signal degrades to the tiers below rather than guessing.
  2. Email domain. A .hk address places the person in Hong Kong; a usc.edu
     address places them on campus in Los Angeles; a country TLD from an
     explicit outside-both-markets list (.sg, .uk, .jp, ...) places them
     elsewhere. .cn is deliberately NOT on that list: mainland domains serve
     Hong Kong desks (his own data has an HK conversation on
     @blackstone.com.cn), so a .cn address is evidence of Greater China, not
     of "somewhere else".
  3. USC affiliation in the row itself: "USC" in the role text ("USC alum,
     finance professional") or a firm recorded as usc. USC is in Los Angeles.
     Only role/firm fields count — the HK cohort's subjects also say "USC
     Student Coffee Chat Request", which is about who is ASKING, not where
     the other person sits.
  4. The provenance campaign's own name: "Apollo HK campaign" states the
     market it targeted; "Gmail USC discovery" states the campus. Weakest
     tier because a campaign statement is about the batch, not the person —
     his data holds 8 rows sourced "Apollo direct search - HK" that a human
     later marked us — which is exactly why this runs under a reviewed
     dry run and never over a value someone set.
  5. The firm's footprint, when unambiguous: exactly one deadline market
     (the same rule `Contact.default_region_from_firm` applies at save), or
     a footprint entirely outside both markets -> other. "apac" is treated
     as evidence of nothing (it contains Hong Kong).
"""

from __future__ import annotations

import re

from crm.models import Contact

# Leading market tag on a touch subject: "HK Jul 29–31 | ..." / "US ... | ...".
# Anchored to the START and followed by a break character — "HSBC" must not
# read as HK, and a mid-subject "USC" is not a market tag.
_SUBJECT_TAG = re.compile(r"^\s*(HK|US)\b[\s|,:–—-]", re.IGNORECASE)

# Country TLDs that place an address unambiguously outside both deadline
# markets. Deliberately short and explicit — every entry is a country this
# product does not model, with no Greater-China ambiguity. No .cn (see module
# docstring), no .com/.net/anything global.
_OTHER_TLDS = (
    ".sg", ".uk", ".co.uk", ".jp", ".au", ".in", ".kr", ".fr", ".de",
    ".ch", ".ca", ".ae",
)

_USC_WORD = re.compile(r"\busc\b", re.IGNORECASE)
_HK_WORD = re.compile(r"\bhk\b|\bhong\s*kong\b", re.IGNORECASE)


def _from_subjects(subjects) -> tuple[str, str] | None:
    for s in subjects or ():
        m = _SUBJECT_TAG.match(s or "")
        if m:
            return m.group(1).lower(), f"touch subject {s.strip()[:40]!r}"
    return None


def _from_email(email: str) -> tuple[str, str] | None:
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain or "@" not in (email or ""):
        return None
    if domain == "hk" or domain.endswith(".hk"):
        return "hk", f"email domain {domain}"
    if domain == "usc.edu" or domain.endswith(".usc.edu"):
        return "us", f"email domain {domain}"
    for tld in _OTHER_TLDS:
        if domain.endswith(tld):
            return "other", f"email domain {domain} ({tld} is outside both markets)"
    return None


def _from_role_or_firm(role: str, firm_name: str, firm_text: str) -> tuple[str, str] | None:
    if _USC_WORD.search(role or ""):
        return "us", f"role says USC ({(role or '').strip()[:40]!r})"
    for name in (firm_name, firm_text):
        if (name or "").strip().lower() == "usc":
            return "us", "firm recorded as usc"
    return None


def _from_source(source: str) -> tuple[str, str] | None:
    if _HK_WORD.search(source or ""):
        return "hk", f"campaign source names HK ({(source or '').strip()[:40]!r})"
    if _USC_WORD.search(source or ""):
        return "us", f"campaign source names USC ({(source or '').strip()[:40]!r})"
    return None


def _from_firm_regions(firm_regions) -> tuple[str, str] | None:
    regions = {(r or "").strip().lower() for r in (firm_regions or [])}
    regions.discard("")
    if not regions:
        return None
    markets = regions & Contact.DEADLINE_MARKETS
    if len(markets) == 1 and regions == markets:
        (only,) = markets
        return only, f"firm's only market is {only}"
    # Entirely outside both markets — but only codes that genuinely exclude
    # them: "apac" contains Hong Kong, so it disqualifies the whole signal.
    outside = {"sg", "eu", "cn", "jp", "other"}
    if not markets and regions <= outside:
        return "other", f"firm's footprint is outside both markets ({sorted(regions)})"
    return None


def infer_region(
    *,
    role: str = "",
    firm_name: str = "",
    firm_text: str = "",
    firm_regions=None,
    email: str = "",
    source: str = "",
    touch_subjects=(),
) -> tuple[str, str] | None:
    """(region, reason) from the strongest signal present, or None when the
    row carries no positive evidence — in which case the honest answer is
    still blank, and the caller must leave it blank."""
    for attempt in (
        _from_subjects(touch_subjects),
        _from_email(email),
        _from_role_or_firm(role, firm_name, firm_text),
        _from_source(source),
        _from_firm_regions(firm_regions),
    ):
        if attempt is not None:
            return attempt
    return None


def infer_for_contact(contact, touch_subjects) -> tuple[str, str] | None:
    """`infer_region` over a Contact row. `touch_subjects` is passed in, not
    queried here, so the command batches one query for all rows."""
    return infer_region(
        role=contact.role,
        firm_name=contact.firm.name if contact.firm_id else "",
        firm_text=contact.firm_text,
        firm_regions=contact.firm.regions if contact.firm_id else None,
        email=contact.email,
        source=contact.source,
        touch_subjects=touch_subjects,
    )
