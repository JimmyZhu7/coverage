"""The reversal: what happens to `declared` rows when the declaration moves.

`Contact.resolve_region`'s tier 2 files a contact by the student's own
declared markets, and it is allowed to do that for exactly one reason — when
the US is the only market you recruit in, "this person is a US contact" is
entailed by a stated fact rather than guessed from one. The day the student
adds Hong Kong in Settings, that premise is gone. Every row written under it
is now a claim nothing supports, sitting silently in a region tab and
scoping the cadence engine's pre-deadline re-ping.

So those rows are unplaced, and the student is told — plainly, with the
number, in the same breath as the change that caused it. A hundred and forty
three contacts quietly changing state is the same class of bug as a wrong
region: nothing on the screen gives anyone a reason to look.

WHAT IS NOT TOUCHED, and why: rows sourced "user" (a person typed that) and
rows sourced "firm" (a single-market firm entails it regardless of what the
student is targeting). Neither premise moved. `region_source` is what makes
this distinction possible at all — without it the three kinds of placed row
are indistinguishable and the only safe reversal would be none.
"""

from __future__ import annotations

from dataclasses import dataclass

from directory.classify import REGION_LABELS

from .models import Contact


def declared_market(regions) -> str:
    """The single deadline market these declared regions entail, or "".

    ALWAYS intersects `Contact.DEADLINE_MARKETS` first: Settings speaks a
    six-value vocabulary (`directory.classify.TRACKED_REGIONS`) and
    `Contact.region` speaks three. `['us', 'sg']` entails the US; `['sg']`
    alone entails nothing at all and must never be rounded to "other".
    """
    markets = {
        (r or "").strip().lower() for r in (regions or [])
    } & Contact.DEADLINE_MARKETS
    return next(iter(markets)) if len(markets) == 1 else ""


@dataclass(frozen=True)
class Unplaced:
    """What the reversal did, and the sentence that says so."""

    count: int
    was: str
    added: tuple[str, ...]

    @property
    def message(self) -> str:
        if not self.count:
            return ""
        added = _join([REGION_LABELS.get(r, r.upper()) for r in self.added])
        was = REGION_LABELS.get(self.was, self.was.upper())
        s = "" if self.count == 1 else "s"
        were = "was" if self.count == 1 else "were"
        return (
            f"You added {added}. {self.count} contact{s} {were} filed as "
            f"{was} because that was your only market. They're unplaced "
            f"again — the Unplaced tab on Network is where you place them."
        )


def _join(labels: list[str]) -> str:
    if len(labels) <= 1:
        return labels[0] if labels else "a market"
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


def unplace_declared_regions(user, *, previous_regions=None) -> Unplaced:
    """Blank `region`/`region_source` on this user's tier-2 rows.

    Call after the new regions are saved. `previous_regions` is what the
    student declared BEFORE the change — it names the market those rows were
    filed as, which is the one thing the row itself can no longer say once it
    has been blanked.

    A plain `.update()`, deliberately: it does not go through `save()`, so
    resolution cannot immediately re-place the rows it just cleared. They
    re-resolve on their own next save, which is the right moment — by then
    the declaration is settled.
    """
    was = declared_market(previous_regions)
    added = tuple(
        r for r in (user.regions or [])
        if r not in set(previous_regions or [])
    )
    count = Contact.all_objects.filter(
        user=user, region_source=Contact.REGION_SOURCE_DECLARED,
    ).update(region="", region_source="")
    return Unplaced(count=count, was=was, added=added)
