"""Matching an application-confirmation email to the role it confirms.

WHY THIS IS ITS OWN MODULE
--------------------------
The My Applications page was a pipeline nobody used — not even the founder,
whose account tracked zero roles. The reason is that it competed with the
ATS's own tracking and lost: a student who has just spent twenty minutes on a
Workday form is not going to come here and click Save, then later drag a card
to "Applied". The only version of this that earns its place is one where the
board already knows, because the confirmation email arrived.

That makes MATCHING the whole feature, and matching is where it can quietly
lie: "Goldman Sachs" plus "Summer Analyst" could be four different postings.
So the rules live here, pure and tested, rather than inline in a command.

THE RULE, IN ONE LINE: advance a role only when there is exactly ONE
believable candidate. Otherwise report it and let the human decide.

An application wrongly marked submitted is worse than one not marked at all —
it makes the student think a form is done that isn't, and the deadline passes
while the board says they are covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that carry no distinguishing signal between two postings at the same
# firm. "Summer Analyst Program 2027" and "Summer Analyst Programme 2027" are
# the same role; "Investment Banking" vs "Global Markets" is what separates
# them, and that is what survives this filter.
_NOISE = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "to", "at", "in", "on",
    "program", "programme", "internship", "intern", "summer", "analyst",
    "graduate", "scheme", "opportunity", "role", "position", "job",
    "application", "full", "time", "fulltime", "part", "student",
})

_WORD = re.compile(r"[a-z0-9&]+")

# Overlap a title pair must clear to count as the same posting, and the margin
# the winner must beat the runner-up by.
#
# Both were raised after a live miss. At 0.34 a real Point72 confirmation for
# "Point72 Academy 2026 Spring Sessions - US" matched "Point72 Academy
# 2026-2027 Investment Analyst Program for Experienced Professionals - US" —
# a different programme for a different audience — because the four shared
# words were `point72`, `academy`, `2026`, `us` and nothing that separated the
# two roles overlapped at all.
#
# `boilerplate()` below cannot save that case: Point72 has 232 open postings
# and "point72" appears in only 15 of them, so no majority filter sees it. The
# honest conclusion is that a PARTIAL TITLE CANNOT reliably pick one of 232,
# and the matcher should say so. 0.6 means most of the distinguishing words
# agree; the real same-posting pair
# ("2027 Investment Banking Summer Analyst — New York" vs "Investment Banking
# Summer Analyst Program - 2027") scores exactly that.
MIN_TITLE_SCORE = 0.6

# A near-tie is not a winner. Two postings that both score well are precisely
# the pair a student would want to pick between themselves.
MIN_TITLE_MARGIN = 0.15


def tokens(title: str) -> frozenset[str]:
    """Distinguishing words in a posting title, lowercased."""
    return frozenset(
        w for w in _WORD.findall((title or "").lower())
        if w not in _NOISE and len(w) > 1
    )


def title_score(a: str, b: str) -> float:
    """Jaccard overlap of two titles' distinguishing words, 0.0-1.0.

    Both empty scores 0.0, NOT 1.0: two titles made entirely of noise words
    ("Summer Analyst Program" twice) carry no evidence that they are the same
    posting, and treating "no information" as a perfect match is exactly how
    an auto-matcher marks the wrong role submitted.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True)
class Match:
    """The outcome of one confirmation email.

    `opportunity` is None whenever the human has to decide; `reason` always
    says which rule fired, so the sync's report can name it rather than
    saying "skipped".
    """
    opportunity: object | None
    reason: str

    @property
    def matched(self) -> bool:
        return self.opportunity is not None


def boilerplate(titles) -> frozenset[str]:
    """Words this firm puts in most of its postings, which therefore separate
    none of them.

    The static noise list above cannot know these: they are the FIRM's own
    vocabulary — its name, its programme brand, the cycle year, the region
    code. Caught on live data, and it was not a near miss. A Point72
    confirmation for "Point72 Academy 2026 Spring Sessions - US" matched
    "Point72 Academy 2026-2027 Investment Analyst Program for Experienced
    Professionals - US" at 40%, and every one of the four shared words was
    `point72`, `academy`, `2026`, `us`. Nothing that distinguished the two
    roles overlapped at all — the score was measuring the firm, not the job.
    """
    titles = list(titles)
    if len(titles) < 2:
        return frozenset()
    counts: dict[str, int] = {}
    for t in titles:
        for w in tokens(t):
            counts[w] = counts.get(w, 0) + 1
    threshold = max(2, (len(titles) + 1) // 2)
    return frozenset(w for w, n in counts.items() if n >= threshold)


def match_application(candidates, title: str, *, tracked_ids=()) -> Match:
    """Pick the one posting a confirmation refers to, or refuse.

    `candidates` are the firm's open postings (the caller scopes them; this
    function never queries). `tracked_ids` are the ids the user already has
    on their board — a role they saved is far likelier to be the one they
    then applied to, so it breaks a tie the title alone cannot.
    """
    candidates = list(candidates)
    if not candidates:
        return Match(None, "no open role at this firm to match")

    # Words shared by most of this firm's postings are dropped BEFORE scoring,
    # so the score measures the job rather than the firm.
    common = boilerplate([c.title for c in candidates])

    def score(a: str, b: str) -> float:
        ta, tb = tokens(a) - common, tokens(b) - common
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    # A title match, when the titles carry enough signal to be sure.
    scored = sorted(
        ((score(title, c.title), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= MIN_TITLE_SCORE and best_score - runner_up >= MIN_TITLE_MARGIN:
        return Match(best, f"title match ({best_score:.0%})")
    if best_score >= MIN_TITLE_SCORE:
        # Two postings that both score well is the dangerous case, not a lucky
        # one: it is exactly the pair a student would want to pick between.
        return Match(None, f"{sum(1 for sc, _ in scored if sc >= MIN_TITLE_SCORE)} "
                           f"roles match that title about equally well")

    # No usable title. One saved role at this firm is a believable answer;
    # more than one is not.
    saved_here = [c for c in candidates if c.id in set(tracked_ids)]
    if len(saved_here) == 1:
        return Match(saved_here[0], "the only role you had saved at this firm")

    if len(candidates) == 1:
        return Match(candidates[0], "the firm's only open role")

    return Match(None, f"{len(candidates)} open roles here and the title did not match one")


# Stage vocabulary, strongest last. Auto-detection may only ever move a row
# UP to `submitted`; a role already at interview or offer knows more than a
# confirmation email does, and "closed" is a decision the user made.
STAGE_ORDER = ("saved", "submitted", "interview", "offer", "closed")


def may_advance(current: str) -> bool:
    """Whether a detected submission may overwrite `current`."""
    now = (current or "saved").strip().lower()
    if now not in STAGE_ORDER:
        return False
    return STAGE_ORDER.index(now) < STAGE_ORDER.index("submitted")
