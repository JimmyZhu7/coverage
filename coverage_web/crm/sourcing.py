"""Who to find at a firm you have no way into yet.

The CRM starts empty and stays empty unless the student already owns a
spreadsheet of names. Every other part of the Network board assumes the
contacts exist: the Coverage Gaps strip can rank a firm "No contacts ·
0/2 advocates · exp 12" and then offer exactly one verb for it, "Add",
which is the app asking the student for the answer instead of helping
with it.

This module is the cheapest honest help available without an import, a
scraper, or a data vendor: ROLE ARCHETYPES worth reaching for on the
student's own tracks, plus a prefilled LinkedIn people-search link for
each. We hand over a query, not a person. Nothing is fetched, nothing is
imported, and no claim is made that these people exist at this firm.
Every row's link is a plain `linkedin.com/search/results/people/?keywords=`
URL of the kind a student could type themselves; the value is that they
do not have to know what to type.

PURE, in the same sense as `crm.coverage`: no database, no clock, no
network. It reads `firm.name` and two fields off the user (`tracks`,
`school`) and returns plain dicts. That is what makes the archetype table
below assertable in a unit test rather than eyeballed on a page.

THE SHAPE OF ONE ANSWER
-----------------------
Three rows per firm, always three, so the panel has a predictable size:

  * the student's tracks pick the archetypes, ROUND ROBIN across tracks
    rather than depth-first, so a student running both IB and S&T sees
    one of each instead of three IB rows and nothing about markets;
  * `User.school`, when set, claims the last row for the alumni search,
    because a shared school is the single highest-yield cold open a
    student has and it costs one extra keyword to hand over;
  * no tracks set (a student who skipped that step in onboarding) falls
    back to a generic trio rather than to nothing. "We do not know your
    track" is not a reason to withhold "find an analyst two years in".

WHY THESE ARCHETYPES
--------------------
Each one is a seat that (a) exists at essentially every firm on the
board, (b) is searchable by title on LinkedIn without inside knowledge,
and (c) has a real reason to reply to a student. Junior seats reply
because they were there last year; programme owners reply because
answering students is the job; one senior seat per track exists because
a name needs someone able to push it. Deliberately NOT a scored or
learned list: it is six short tables a student can read, disagree with,
and ignore.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlencode

# LinkedIn's public people search. Plain link, no API, no token, no
# scraping: the student lands on LinkedIn's own results page, logged in
# as themselves, and Coverage never sees what is there.
LINKEDIN_PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/"

# What the panel says out loud, once, above the rows. Kept here rather
# than in the template so the promise and the code that builds the links
# live in the same file.
DISCLOSURE = "Suggestions, not a list of people. Each one opens a LinkedIn search."

# key -> (label, why, extra keywords beyond the firm name)
#
# `label` names the seat. `why` says what that seat is good for, in one
# short line. `terms` is what gets appended to the quoted firm name in
# the search box, so it must be words that actually appear in LinkedIn
# headlines, not internal jargon.
Archetype = tuple[str, str, str]

TRACK_ARCHETYPES: dict[str, tuple[Archetype, ...]] = {
    "ib": (
        ("Analyst, one or two years in",
         "Closest to your seat. Replies most, knows this year's process.",
         "investment banking analyst"),
        ("Associate who runs the summer programme",
         "Sees the intern shortlist before it is a shortlist.",
         "investment banking associate"),
        ("VP in the group you want",
         "Senior enough to push a name, junior enough to answer.",
         "investment banking vice president"),
    ),
    "st": (
        ("Analyst on a flow desk",
         "The exact seat you are applying to. Short calls, early mornings.",
         "sales and trading analyst"),
        ("Trader who did the rotation",
         "Knows which desks actually take interns.",
         "trader"),
        ("Markets campus recruiter",
         "Owns the timeline you are currently guessing at.",
         "campus recruiting markets"),
    ),
    "pe": (
        ("Pre-MBA associate",
         "Came through banking two years ago. Remembers the jump.",
         "private equity associate"),
        ("Analyst on the credit side",
         "Smaller teams, fewer students asking, higher reply rate.",
         "credit analyst"),
        ("VP who hires off-cycle",
         "Off-cycle runs on referrals, not portals.",
         "private equity vice president"),
    ),
    "am": (
        ("Investment analyst on a fund you can name",
         "Naming the fund is half the conversation.",
         "investment analyst"),
        ("Client-facing associate",
         "Distribution seats take more grads than the funds do.",
         "client relationship associate"),
        ("Graduate programme alum, two years out",
         "Did the rotation you are applying to.",
         "graduate programme analyst"),
    ),
    "consulting": (
        ("Business analyst in their first two years",
         "Case partner and referral in one person.",
         "business analyst"),
        ("Consultant who runs case workshops",
         "Coaching you is literally part of their job.",
         "consultant"),
        ("Campus recruiting captain",
         "Named on the campus team. Your pipeline is their remit.",
         "campus recruiting"),
    ),
    "corp-strat": (
        ("Strategy manager in the group",
         "Small teams. One manager decides most of the hire.",
         "corporate strategy manager"),
        ("Senior analyst who came from consulting",
         "Knows both doors in and usually enjoys explaining the switch.",
         "strategy analyst"),
        ("Rotational programme grad",
         "The programme is the front door. They took it.",
         "rotational program analyst"),
    ),
}

# Used when the student has set no tracks. Not a seventh track: the same
# three seats every firm on the board has, phrased without a desk name.
GENERIC_ARCHETYPES: tuple[Archetype, ...] = (
    ("Analyst, one or two years in",
     "Closest to your seat. Replies most, remembers the process.",
     "analyst"),
    ("Campus recruiter",
     "Owns the timeline you are currently guessing at.",
     "campus recruiting"),
    ("Associate who ran last summer's interns",
     "Sees the intern shortlist before it is a shortlist.",
     "associate"),
)

ALUMNI_WHY = "Shared school is the easiest cold open you will get."

# Three rows. Enough to be a plan, few enough to read inside a card.
DEFAULT_LIMIT = 3


def _tracks_of(user) -> list[str]:
    """The user's tracks, filtered to ones this module has a table for and
    de-duplicated, in the order the user set them. `tracks` is a
    free-form ArrayField, so an unknown or repeated value is data, not a
    crash."""
    seen: list[str] = []
    for t in (getattr(user, "tracks", None) or []):
        if t in TRACK_ARCHETYPES and t not in seen:
            seen.append(t)
    return seen


def _round_robin(tracks: Iterable[str]) -> list[tuple[str, Archetype]]:
    """One archetype from each track, then the next from each, and so on.

    Depth-first would give a dual-track student three IB rows and nothing
    about markets, which is the opposite of what having two tracks means.
    """
    tracks = list(tracks)
    tables = [TRACK_ARCHETYPES[t] for t in tracks]
    out: list[tuple[str, Archetype]] = []
    for i in range(max((len(t) for t in tables), default=0)):
        for track, table in zip(tracks, tables):
            if i < len(table):
                out.append((track, table[i]))
    return out


def linkedin_search_url(*terms: str) -> str:
    """A LinkedIn people search for `terms`, joined with spaces and
    URL-encoded as one `keywords` value.

    `urlencode` is doing real work here, not ceremony: firm names on this
    board include "Rothschild & Co" and "S&P Global", and an unencoded
    `&` would truncate the query string at the ampersand and search for
    "Rothschild" alone. Quoting is the caller's job (see `_row`)."""
    keywords = " ".join(t for t in terms if t)
    return f"{LINKEDIN_PEOPLE_SEARCH}?{urlencode({'keywords': keywords})}"


def _row(key: str, label: str, why: str, firm_name: str, terms: str) -> dict:
    # The firm name is phrase-quoted so "Bank of America" does not match
    # every person in America; the title terms stay loose because
    # headlines vary ("IBD Analyst", "Analyst, Investment Banking").
    query = f'"{firm_name}" {terms}'.strip()
    return {
        "key": key,
        "label": label,
        "why": why,
        "query": query,
        "linkedin_url": linkedin_search_url(query),
    }


def suggestions_for(firm: Any, user: Any, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Who to look for at `firm`, for this `user`. See the module docstring.

    Args:
        firm: anything carrying a `name` (a `directory.models.Firm`, or a
            mapping, so tests do not need a database row).
        user: anything carrying `tracks` (list of `TRACK_LABELS` keys) and
            `school` (string). Both optional; both degrade to a sensible
            answer rather than to an empty panel.
        limit: how many rows. The alumni row, when there is a school,
            takes one of these slots rather than being extra.

    Returns:
        A list of dicts with `key`, `label`, `why`, `query` and
        `linkedin_url`. Empty ONLY when the firm has no name, since a
        keyword search with no firm in it is not a suggestion about this
        firm at all.
    """
    firm_name = (
        (firm.get("name") if isinstance(firm, dict) else getattr(firm, "name", ""))
        or ""
    ).strip()
    if not firm_name or limit < 1:
        return []

    school = (getattr(user, "school", "") or "").strip()
    tracks = _tracks_of(user)

    # The school row is one of the three, not a fourth: the panel's size
    # is the promise, and a student with a school set has one fewer thing
    # to guess about than a student without one.
    archetype_slots = limit - 1 if school else limit

    rows: list[dict] = []
    if tracks:
        picks = _round_robin(tracks)[:archetype_slots]
        rows = [
            _row(f"{track}-{i}", label, why, firm_name, terms)
            for i, (track, (label, why, terms)) in enumerate(picks)
        ]
    else:
        rows = [
            _row(f"any-{i}", label, why, firm_name, terms)
            for i, (label, why, terms) in enumerate(
                GENERIC_ARCHETYPES[:archetype_slots]
            )
        ]

    if school:
        rows.append(
            {
                "key": "alumni",
                "label": f"Someone from {school}",
                "why": ALUMNI_WHY,
                "query": f'"{firm_name}" "{school}"',
                "linkedin_url": linkedin_search_url(f'"{firm_name}" "{school}"'),
            }
        )
    return rows
