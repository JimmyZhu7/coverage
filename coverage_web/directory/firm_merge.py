"""Merge `Firm` rows that share the same name — the firm-level counterpart
to `dupes.py`'s posting-level dedup.

WHY THIS IS A SEPARATE MODULE FROM `dupes.py`
----------------------------------------------
`dupes.py`'s Class B ("several postings, one job") is a GUESS: two
same-titled requisitions at one firm may be one posting filed twice, or two
genuinely independent openings with independent lifecycles — the module's
own docstring is explicit that merging those would destroy real data, so
`fold_duplicates` only ever hides the copy at display time and never writes.

A `Firm` name collision is not that kind of guess. "TD Securities" and "TD
Securities" are not two firms that happen to share a name the way two
requisitions happen to share a title — a proper-noun employer name is, for
Coverage's seeded/scraped set, an identifier, not descriptive text. The live
collision (ids 199 and 207) was not two real firms; it was `_FirmResolver`
losing track of "TD Securities" — one seeded row (`boards.py`'s only `"td"`
board config resolves postings to the name "TD Securities") and one row a
`test_feed_honesty.py` fixture minted directly against the dev DB on
2026-08-15 (`Firm.objects.create(slug="td-closed", name="TD Securities")`).
Merging is the correct fix here, not a hedge against one.

WHAT COUNTS AS CANONICAL. The lowest-id row in the group — the oldest,
i.e. whichever was created first. `ingest._FirmResolver.resolve()` (see its
own docstring) now resolves every future name collision the same way, so
this keeps the merge and the resolver's ongoing behaviour pointed at the
same row rather than fighting each other.

WHAT A MERGE PRESERVES. Same policy `dedupe_opportunities.py`'s docstring
already describes for its own (still-unimplemented) posting-level merge:
the survivor keeps the earliest `first_seen`, the most recent verification
timestamps, a stated deadline/location over a missing one, and `open` over
`closed`. Per-user tracking rows (`UserOpportunity`, `UserFirm`) move across
and combine on collision rather than one silently overwriting the other,
using the same "furthest-along wins" rule `applications.STAGE_ORDER`
already encodes for auto-detected submissions.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count
from django.db.models.functions import Lower

from analytics.models import UserOpportunity
from crm.models import UserFirm

from .applications import STAGE_ORDER
from .models import EmailPatternStats, Firm, FirmDate, Opportunity


def find_duplicate_firm_groups() -> list[list[Firm]]:
    """Every set of 2+ `Firm` rows sharing a case-insensitive name, each
    group ordered oldest (canonical) first."""
    dupe_names = (
        Firm.objects.annotate(name_lower=Lower("name"))
        .values("name_lower")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
        .values_list("name_lower", flat=True)
    )
    groups = []
    for name_lower in dupe_names:
        firms = list(
            Firm.objects.annotate(name_lower=Lower("name"))
            .filter(name_lower=name_lower)
            .order_by("id")
        )
        groups.append(firms)
    return groups


def _stage_rank(status: str) -> int:
    status = (status or "saved").strip().lower()
    return STAGE_ORDER.index(status) if status in STAGE_ORDER else 0


def _merge_user_opportunity(keep: UserOpportunity, lose: UserOpportunity) -> None:
    """Combine two per-user tracking rows for postings that turned out to be
    the same one, keeping whichever field is "furthest along" or earliest —
    never silently dropping a user's own recorded state."""
    if _stage_rank(lose.applied_status) > _stage_rank(keep.applied_status):
        keep.applied_status = lose.applied_status
    if lose.applied_at and (keep.applied_at is None or lose.applied_at < keep.applied_at):
        keep.applied_at = lose.applied_at
    keep.interview_dates = sorted(set(keep.interview_dates or []) | set(lose.interview_dates or []))
    keep.dismissed = keep.dismissed and lose.dismissed
    keep.save()
    lose.delete()


def _reparent_user_opportunities(old_opp: Opportunity, new_opp: Opportunity) -> None:
    for uo in UserOpportunity.all_objects.filter(opportunity=old_opp):
        existing = UserOpportunity.all_objects.filter(user_id=uo.user_id, opportunity=new_opp).first()
        if existing is None:
            uo.opportunity = new_opp
            uo.save(update_fields=["opportunity"])
        else:
            _merge_user_opportunity(existing, uo)


def _merge_opportunity_pair(keep: Opportunity, lose: Opportunity) -> None:
    """`keep` and `lose` share a (firm, url) once reparented onto the same
    canonical firm — the exact posting, filed twice. Fold `lose`'s facts
    onto `keep` per the policy `dedupe_opportunities.py` documents, move any
    tracking rows across, then delete `lose`."""
    changed = []
    if lose.first_seen and (keep.first_seen is None or lose.first_seen < keep.first_seen):
        keep.first_seen = lose.first_seen
        changed.append("first_seen")
    for field in ("last_verified", "last_checked", "deadline_checked_at"):
        lv, kv = getattr(lose, field), getattr(keep, field)
        if lv and (kv is None or lv > kv):
            setattr(keep, field, lv)
            changed.append(field)
    if not keep.deadline and lose.deadline:
        keep.deadline = lose.deadline
        keep.deadline_precision = lose.deadline_precision
        keep.confidence = max(keep.confidence, lose.confidence)
        changed.append("deadline")
    if not keep.location and lose.location:
        keep.location = lose.location
        changed.append("location")
    if keep.status != "open" and lose.status == "open":
        keep.status = "open"
        keep.closed_at = None
        changed.append("status")
    if changed:
        keep.save()

    _reparent_user_opportunities(lose, keep)
    lose.delete()


def _merge_firm_dates(canonical: Firm, duplicate: Firm) -> tuple[int, int]:
    moved = merged = 0
    for fd in FirmDate.objects.filter(firm=duplicate):
        # Must match `uniq_firm_dates_firm_cycle_track_region_event` column for
        # column: this lookup is what decides between reparenting the row and
        # folding it into an existing one, so a key narrower than the
        # constraint's reparents a row the constraint then rejects. `track`
        # joined that key in migration 0014.
        existing = FirmDate.objects.filter(
            firm=canonical, cycle=fd.cycle, track=fd.track, region=fd.region,
            event_kind=fd.event_kind,
        ).first()
        if existing is None:
            fd.firm = canonical
            fd.save(update_fields=["firm"])
            moved += 1
        else:
            # Keep whichever reading is more confident; either way the
            # duplicate's history is folded in rather than lost.
            if fd.confidence > existing.confidence:
                existing.date, existing.precision = fd.date, fd.precision
                existing.confidence, existing.source_url = fd.confidence, fd.source_url
                existing.found_on = fd.found_on
            existing.history = (existing.history or []) + (fd.history or [])
            existing.save()
            fd.delete()
            merged += 1
    return moved, merged


def _merge_user_firms(canonical: Firm, duplicate: Firm) -> tuple[int, int]:
    moved = merged = 0
    for uf in UserFirm.all_objects.filter(firm=duplicate):
        existing = UserFirm.all_objects.filter(user_id=uf.user_id, firm=canonical).first()
        if existing is None:
            uf.firm = canonical
            uf.save(update_fields=["firm"])
            moved += 1
        else:
            if existing.tier is None and uf.tier is not None:
                existing.tier = uf.tier
            if not existing.status and uf.status:
                existing.status = uf.status
            existing.save()
            uf.delete()
            merged += 1
    return moved, merged


def _merge_email_pattern_stats(canonical: Firm, duplicate: Firm) -> bool:
    dup_stats = EmailPatternStats.objects.filter(firm=duplicate).first()
    if dup_stats is None:
        return False
    canon_stats, _ = EmailPatternStats.objects.get_or_create(firm=canonical)
    canon_stats.delivered += dup_stats.delivered
    canon_stats.bounced += dup_stats.bounced
    canon_stats.save()
    dup_stats.delete()
    return True


def merge_firms(canonical: Firm, duplicate: Firm) -> dict[str, Any]:
    """Reparent every row under `duplicate` onto `canonical`, then delete
    `duplicate`. Idempotent-ish per call — meant to be run once per
    (canonical, duplicate) pair inside a transaction (see the management
    command), never on a schedule."""
    stats: dict[str, Any] = {
        "duplicate_id": duplicate.id, "duplicate_slug": duplicate.slug,
        "opportunities_moved": 0, "opportunities_merged": 0,
    }

    for opp in list(Opportunity.objects.filter(firm=duplicate)):
        existing = Opportunity.objects.filter(firm=canonical, url=opp.url).first()
        if existing is None:
            opp.firm = canonical
            opp.save(update_fields=["firm"])
            stats["opportunities_moved"] += 1
        else:
            _merge_opportunity_pair(existing, opp)
            stats["opportunities_merged"] += 1

    stats["firm_dates_moved"], stats["firm_dates_merged"] = _merge_firm_dates(canonical, duplicate)
    stats["user_firms_moved"], stats["user_firms_merged"] = _merge_user_firms(canonical, duplicate)
    stats["email_pattern_stats_merged"] = _merge_email_pattern_stats(canonical, duplicate)

    duplicate.delete()
    return stats
