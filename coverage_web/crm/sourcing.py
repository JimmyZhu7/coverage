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
network. It reads three fields off the firm (`name`, `tracks`,
`recruiting_style`) and two off the user (`tracks`, `school`) and returns
plain dicts. That is what makes the archetype table below assertable in a
unit test rather than eyeballed on a page.

THE SHAPE OF ONE ANSWER
-----------------------
Three rows per firm, always three, so the panel has a predictable size:

  * THE FIRM'S OWN TRACKS, intersected with the student's, pick the
    archetypes (2026-09-01). Before this the student's tracks alone did,
    and the panel was measured proposing "investment banking analyst" at
    Jane Street and "sales and trading analyst" at KKR and Google — seats
    those firms do not have. 21 of the 33 `st`-tagged firms on the board
    are quant or prop shops. So: the tracks the two share, else the firm's
    (a PE shop gets PE seats whatever the student runs), else the
    student's when the firm has no tracks on file. Two shared tracks still
    ROUND ROBIN rather than depth-first, so a student running both IB and
    S&T at a bank that hires for both sees one of each;
  * `User.school`, when set, claims the last row for the alumni search,
    because a shared school is the single highest-yield cold open a
    student has and it costs one extra keyword to hand over;
  * no tracks set on either side (a student who skipped that step in
    onboarding, a firm nobody has classified), or a firm whose only
    stated tracks are ones this panel has no seats for (the nine
    `corp-strat` tech firms since D-3, MLT and SEO Career), falls back to
    a generic trio rather than to nothing. "We do not know your track" is
    not a reason to withhold "find an analyst two years in".

THE ONE FIRM THAT GETS A DIFFERENT ANSWER
-----------------------------------------
A firm whose `recruiting_style` is "assessment" (Jane Street, Citadel
Securities, SIG, Optiver, HRT — see `directory.models.Firm` for the
evidence) hires off a test, and a coffee chat is not part of its process:
Jane Street's own FAQ answers "Can I schedule a phone call or coffee?"
with "unfortunately, no". Proposing "an analyst to chat with" there is not
help, it is homework the firm has said it will not mark. Those firms get
the two rows that are true of them — the campus recruiter who runs the
process, and an alumnus for a resume referral only — and a note that says
to apply. The alumni row stays; its `why` stops promising a conversation.

WHY THESE ARCHETYPES
--------------------
Each one is a seat that (a) exists at essentially every firm on the
board, (b) is searchable by title on LinkedIn without inside knowledge,
and (c) has a real reason to reply to a student. Junior seats reply
because they were there last year; programme owners reply because
answering students is the job; one senior seat per track exists because
a name needs someone able to push it. Deliberately NOT a scored or
learned list: it is five short tables a student can read, disagree with,
and ignore — one per selectable track.
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
}
# No `corp-strat` table. The track was retired from the picker on 2026-09-02
# (D-3, `classify.RETIRED_TRACKS`): nobody can select it, so nobody can be
# handed its seats. The nine firms tagged with it keep their tag and keep a
# panel — see `_tracks_of` for what they get instead, which is the generic
# trio and not this student's banking seats.

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

# `Firm.recruiting_style` value for a test-gated process, spelled here so
# this module stays free of Django imports (same posture as `crm.coverage`).
ASSESSMENT = "assessment"

# The two honest rows for an assessment firm. Not a seventh track: these
# are the only two seats at such a firm where an email does anything, and
# each `why` says exactly how little. The alumnus row's `terms` are what
# gets searched when the student has no school on file; with a school the
# row searches the school itself (see `suggestions_for`), because that is
# the referral the row is for.
ASSESSMENT_ARCHETYPES: tuple[Archetype, ...] = (
    ("Campus recruiter (they run the process; a chat is not part of it)",
     "Ask what the assessment covers and when it runs. That is the whole conversation.",
     "campus recruiting"),
    ("Alumnus at the firm, for a resume referral only",
     "A referral gets your resume read. It does not skip the test.",
     "alumni"),
)

# What the panel says above those two rows instead of `DISCLOSURE`'s
# generic line. It links the student to the one verb that works: apply.
ASSESSMENT_NOTE = (
    "This firm does not do coffee chats. Apply, then prepare for the test."
)

# Three rows. Enough to be a plan, few enough to read inside a card.
DEFAULT_LIMIT = 3


def _firm_field(firm: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a `directory.models.Firm` or a mapping standing in
    for one, so tests need no database row."""
    if isinstance(firm, dict):
        return firm.get(key, default)
    return getattr(firm, key, default)


def _known_tracks(values: Iterable[str] | None) -> list[str]:
    """`values` filtered to tracks this module has a table for and
    de-duplicated, in the order given. Both `User.tracks` and `Firm.tracks`
    are free-form ArrayFields, so an unknown or repeated value is data, not
    a crash."""
    seen: list[str] = []
    for t in (values or []):
        if t in TRACK_ARCHETYPES and t not in seen:
            seen.append(t)
    return seen


def _tracks_of(user, firm: Any = None) -> list[str]:
    """The tracks the archetypes are drawn from, for this user AT this firm.

    The firm's tracks are the honest constraint: a seat the firm does not
    have is not a suggestion. So the answer is the tracks the two sides
    share, in the order the STUDENT set them (that order is a preference
    and it is theirs); failing any overlap, the firm's own tracks (a PE
    shop gets PE seats whatever the student runs — measured, "sales and
    trading analyst" at KKR); failing tracks on the firm at all, the
    student's, exactly as before this module read the firm. Empty only
    when neither side has a track this module knows.

    "NO TRACKS ON FILE" AND "TRACKS THIS MODULE HAS NO TABLE FOR" ARE NOT
    THE SAME FIRM. A firm nobody has classified is unknown, and the
    student's own tracks are the best available guess for it. A firm that
    states `['corp-strat']` or `['pipeline']` has been classified, and
    stated something this panel has no seats for — retired (D-3) or never
    an employer at all (MLT, SEO Career). Falling back to the student's
    tracks there would put "investment banking analyst" in front of Google
    and MLT, which is the exact defect the firm-tracks read fixed on
    2026-09-01. Those firms get an empty list, which `suggestions_for`
    renders as the generic trio: an analyst two years in exists at all of
    them, and a bulge-bracket desk does not."""
    user_tracks = _known_tracks(getattr(user, "tracks", None))
    stated = [t for t in (_firm_field(firm, "tracks") or []) if t]
    firm_tracks = _known_tracks(stated)
    shared = [t for t in user_tracks if t in firm_tracks]
    if shared or firm_tracks:
        return shared or firm_tracks
    return [] if stated else user_tracks


def panel_note(firm: Any) -> str:
    """The one line the panel prints above its rows for `firm`: the
    assessment note for a test-gated firm, `DISCLOSURE` for everyone else.
    The template reads `sourcing_note` per card; this is what the view
    puts there."""
    if (_firm_field(firm, "recruiting_style") or "") == ASSESSMENT:
        return ASSESSMENT_NOTE
    return DISCLOSURE


def _round_robin(tracks: Iterable[str]) -> list[tuple[str, int, Archetype]]:
    """One archetype from each track, then the next from each, and so on.

    Depth-first would give a dual-track student three IB rows and nothing
    about markets, which is the opposite of what having two tracks means.

    Carries each archetype's index WITHIN ITS OWN TRACK, not its position
    in the interleaved output, because that index becomes the row's
    analytics key: "st-0" has to mean the same seat in every student's
    funnel, whether markets was their first track or their second.
    """
    tracks = list(tracks)
    tables = [TRACK_ARCHETYPES[t] for t in tracks]
    out: list[tuple[str, int, Archetype]] = []
    for i in range(max((len(t) for t in tables), default=0)):
        for track, table in zip(tracks, tables):
            if i < len(table):
                out.append((track, i, table[i]))
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
            mapping, so tests do not need a database row). `tracks` and
            `recruiting_style` are read too when present; absent, the
            answer is the student's-tracks one this module always gave.
        user: anything carrying `tracks` (list of `TRACK_LABELS` keys) and
            `school` (string). Both optional; both degrade to a sensible
            answer rather than to an empty panel.
        limit: how many rows. The alumni row, when there is a school,
            takes one of these slots rather than being extra.

    Returns:
        A list of dicts with `key`, `label`, `why`, `query` and
        `linkedin_url`. Empty ONLY when the firm has no name, since a
        keyword search with no firm in it is not a suggestion about this
        firm at all. An assessment firm gets exactly two rows (see the
        module docstring) — two honest rows beat three padded ones.
    """
    firm_name = (_firm_field(firm, "name") or "").strip()
    if not firm_name or limit < 1:
        return []

    school = (getattr(user, "school", "") or "").strip()

    if (_firm_field(firm, "recruiting_style") or "") == ASSESSMENT:
        recruiter, alumnus = ASSESSMENT_ARCHETYPES
        rows = [_row("assess-0", recruiter[0], recruiter[1], firm_name, recruiter[2])]
        label, why, terms = alumnus
        if school:
            # Keyed "alumni" like every other school row, so the analytics
            # funnel keeps one name for "the school search" across firms;
            # the label and `why` are the referral-only ones.
            query = f'"{firm_name}" "{school}"'
            rows.append({
                "key": "alumni",
                "label": label,
                "why": why,
                "query": query,
                "linkedin_url": linkedin_search_url(query),
            })
        else:
            rows.append(_row("assess-1", label, why, firm_name, terms))
        return rows[:limit]

    tracks = _tracks_of(user, firm)

    # The school row is one of the three, not a fourth: the panel's size
    # is the promise, and a student with a school set has one fewer thing
    # to guess about than a student without one.
    archetype_slots = limit - 1 if school else limit

    rows: list[dict] = []
    if tracks:
        rows = [
            _row(f"{track}-{i}", label, why, firm_name, terms)
            for track, i, (label, why, terms) in _round_robin(tracks)[:archetype_slots]
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
