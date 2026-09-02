"""Duplicate cards — suggest, merge on a tap, undo, all through the ledger.

THE DIVISION OF LABOUR. `capture.discovery` owns the identity ladder: its
`_match_existing` is the one CONCLUSIVE opinion (exact address, routing
variant, equivalent full name) every capture door uses, and its
`duplicate_evidence` is the one SUGGESTIVE rung — evidence strong enough to
ask about, never strong enough to act on. This module owns what happens
around a suggestion: finding the pairs among the user's existing rows,
performing the merge the user tapped, and reversing it. No matching logic
lives here — a third opinion about who is a duplicate must not grow.

WHY SUGGESTIONS ARE COMPUTED LIVE AND NEVER STORED. A stored suggestion can
go stale three ways (either contact edited, archived, or deleted) and every
staleness is a wrong card, so Settings computes it on render. It stays cheap
enough to do that because the scan is BLOCKED rather than exhaustive: rows
are grouped by the two equalities the suggestive rung requires (same
multi-word name, or same mailbox localpart) and only rows sharing one are
compared. Unblocked it was every unordered pair — the founder's 306 rows
made 45,844 comparisons on every GET, and 600 rows would have made 180,000.
See `_blocking_keys`. Only ANSWERS persist, as
`crm.models.ContactMerge` rows — and any answer (merged, undone, rejected)
suppresses the pair forever, the same remembered-forever contract a
dismissed ContactProposal holds.

THE MERGE ITSELF is deliberately small, because every write it makes is a
write undo must reverse:
- touches move from duplicate to primary (ids recorded verbatim). This is
  the one sanctioned UPDATE on `touches` rows in application code: it
  re-points which card of the SAME person holds the history, and never
  edits what any touch says or when it happened.
- blank fields on the primary are filled from the duplicate (before/after
  recorded).
- the duplicate's address lands on the primary as a note line (recorded so
  undo can strip that line and no other) — the same alternate-address-as-
  note posture `capture.mailfacts` holds for routing addresses.
- the duplicate is archived, not deleted. Its row is the undo's raw
  material and the reason a later scan recognises the address instead of
  re-proposing the person.

Warmth and thread_state are pipeline property and this module NEVER writes
them: the primary keeps its own earned state. The scan puts the row with
more history on the "keep" side precisely so the stronger warmth is the one
that survives.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from crm.models import Contact, ContactMerge, Touch

# Rendering guard, same posture as the Settings dismissed-proposals cap: a
# scan that somehow suggests more than this many pairs is telling us about
# import hygiene, not about people, and the page should not drown.
MAX_SUGGESTIONS = 25

# The fields a merge may copy onto the primary WHEN BLANK there. Email is
# deliberately absent: the primary's address is the one its history was
# earned on, and the duplicate's address is recorded as the note line
# instead — an address swap is a hand decision on the contact page.
_FILLABLE_FIELDS = ("role", "firm_id", "region", "linkedin", "school", "firm_text")


@dataclass
class MergeCandidate:
    """One suggested pair, primary (keep) first."""

    primary: Contact
    duplicate: Contact
    evidence: str


def _touch_counts(user, contact_ids: list[int]) -> dict[int, int]:
    counts: dict[int, int] = {cid: 0 for cid in contact_ids}
    rows = (
        Touch.objects.for_user(user)
        .filter(contact_id__in=contact_ids)
        .values_list("contact_id", flat=True)
    )
    for cid in rows:
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def _answered_pairs(user) -> set[frozenset[int]]:
    """Every pair the user has already answered, whatever the answer was.
    Merged, undone and rejected all suppress: each is the user's word."""
    return {
        frozenset((p, d))
        for p, d in ContactMerge.objects.for_user(user).values_list(
            "primary_id", "duplicate_id"
        )
    }


def _blocking_keys(contact) -> set:
    """The keys under which this row can possibly be somebody's duplicate.

    A BLOCKING KEY, not an opinion. `discovery.duplicate_evidence` — the one
    suggestive rung, and still the only thing that decides a pair — returns
    something non-empty in exactly two shapes, and both of them are an
    equality:

      the name rung     requires `names_equivalent(a.name, b.name)`, which
                        requires the two canonical token sequences to carry
                        the SAME multi-letter words in the same order (see
                        `names_equivalent`: `ta == tb`, or `words_a ==
                        words_b` once agreeing initials are set aside, and
                        never fewer than two words a side).
      the mailbox rung  requires `la and la == lb` — an identical, non-empty
                        mailbox localpart.

    Everything else those two branches consult (domain relatedness, shared
    firm, name shortening, personal-localpart) only ever NARROWS what they
    already agree on. So two rows sharing neither key cannot be a pair, and
    comparing them is wasted work.

    The keys are computed BY `discovery`'s own canonicalisers rather than
    re-derived here, which is the module docstring's rule: no matching logic
    lives in this file, and a blocking key that spelled "same name" its own
    way would be exactly the third opinion that rule forbids. If the rung
    ever widens beyond these two equalities the blocking goes blind, so the
    necessary condition is pinned by a test that runs the full O(n²) scan
    against the blocked one over a matrix of contact shapes
    (`crm/tests/test_merge.py`, `test_blocking_finds_every_pair_the_full_scan_finds`).
    """
    from capture import discovery

    keys = set()
    words = tuple(
        t for t in discovery._name_tokens(getattr(contact, "name", "") or "")
        if len(t) > 1
    )
    if len(words) >= 2:
        keys.add(("name", words))
    localpart, _ = discovery._mailbox_parts(getattr(contact, "email", "") or "")
    if localpart:
        keys.add(("mailbox", localpart))
    return keys


def candidate_pairs(user) -> list[MergeCandidate]:
    """Every pair of this user's contact rows the suggestive rung would call
    one person, primary-first, minus pairs already answered. Read-only.

    Pairs where BOTH rows are archived are skipped — two cards the user has
    already put away ask for no decision. One archived row still suggests:
    the live card's history is split from something real.

    BLOCKED, NOT SCANNED. This ran `duplicate_evidence` over every unordered
    pair, which is O(n²) in a page render: measured on the founder's live
    account 2026-09-01, 45,844 comparisons and 597 ms of Python on every GET
    of Settings, to reach one suggestive pair he had already answered. Rows
    are grouped by `_blocking_keys` first and only rows sharing a key are
    compared — the keys being the two equalities the rung itself requires,
    so the blocked scan cannot miss a pair the full scan would have found.
    Same account, blocked: 19 ms and 0 comparisons, and the one pair the full
    scan can see (rows 707/706, the AWS account manager tracked twice) shares
    the `("mailbox", "ebbakler")` key, so it is still reachable the moment
    the answer that suppresses it is undone.

    The candidate index pairs are then walked in the ORIGINAL `(i, j)` order,
    not bucket by bucket, because `MAX_SUGGESTIONS` truncates: a different
    visit order would return a different 25 to a page whose whole promise is
    that the same board renders the same cards."""
    rows = list(Contact.objects.for_user(user))
    answered = _answered_pairs(user)
    counts = _touch_counts(user, [c.id for c in rows])
    from capture import discovery

    buckets: dict[tuple, list[int]] = {}
    for idx, row in enumerate(rows):
        for key in _blocking_keys(row):
            buckets.setdefault(key, []).append(idx)

    # Indices land in each bucket ascending, so `(members[a], members[b])` is
    # already `(i, j)` with `i < j` — the same orientation the old nested loop
    # produced. A row can share both keys with another, hence the set.
    candidates: set[tuple[int, int]] = set()
    for members in buckets.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                candidates.add((members[a], members[b]))

    out: list[MergeCandidate] = []
    for i, j in sorted(candidates):
        a, b = rows[i], rows[j]
        if a.archived and b.archived:
            continue
        if frozenset((a.id, b.id)) in answered:
            continue
        evidence = discovery.duplicate_evidence(a, b)
        if not evidence:
            continue
        primary, duplicate = _pick_primary(a, b, counts)
        out.append(MergeCandidate(primary, duplicate, evidence))
        if len(out) >= MAX_SUGGESTIONS:
            return out
    return out


def _pick_primary(a: Contact, b: Contact, counts: dict[int, int]) -> tuple[Contact, Contact]:
    """Keep the row with the most history: more touches, then the live one
    over the archived one, then the older row. The keeper's warmth is the
    one that survives (see module docstring), so history depth is the only
    honest tiebreak."""
    ka = (counts.get(a.id, 0), 0 if a.archived else 1, -a.id)
    kb = (counts.get(b.id, 0), 0 if b.archived else 1, -b.id)
    return (a, b) if ka >= kb else (b, a)


def suggestion_for(user, primary_id: int, duplicate_id: int) -> MergeCandidate | None:
    """The still-standing suggestion for this exact pair, or None. The POST
    that performs a merge re-derives the evidence rather than trusting a
    form field: if the pair no longer clears the suggestive bar (a row was
    edited, archived twice over, or already answered), the tap refuses."""
    for cand in candidate_pairs(user):
        if {cand.primary.id, cand.duplicate.id} == {primary_id, duplicate_id}:
            return cand
    return None


def merge(user, primary: Contact, duplicate: Contact, evidence: str = "") -> ContactMerge:
    """Fold `duplicate` into `primary`, exactly as the ledger describes, and
    return the ledger row. Caller guarantees both rows belong to `user` and
    the pair was suggested (see `suggestion_for`)."""
    with transaction.atomic():
        moved = list(
            Touch.objects.for_user(user)
            .filter(contact=duplicate)
            .values_list("id", flat=True)
        )
        if moved:
            # The one sanctioned touches UPDATE — see module docstring.
            Touch.objects.for_user(user).filter(id__in=moved).update(contact=primary)

        field_changes: dict[str, dict] = {}
        update_fields: list[str] = []
        for field in _FILLABLE_FIELDS:
            before = getattr(primary, field)
            after = getattr(duplicate, field)
            if (before in (None, "")) and (after not in (None, "")):
                setattr(primary, field, after)
                field_changes[field] = {"before": before, "after": after}
                update_fields.append("firm" if field == "firm_id" else field)

        note_line = ""
        dup_email = (duplicate.email or "").strip().lower()
        if dup_email and dup_email != (primary.email or "").strip().lower():
            note_line = (
                f"Also reachable at {dup_email} "
                f"(merged duplicate card, {timezone.localdate():%b %d, %Y})"
            )[:500]
            primary.notes = (
                f"{primary.notes}\n{note_line}" if primary.notes else note_line
            )
            update_fields.append("notes")
        if update_fields:
            primary.save(update_fields=update_fields)

        was_archived = duplicate.archived
        if not was_archived:
            duplicate.archived = True
            duplicate.save(update_fields=["archived"])

        return ContactMerge.all_objects.create(
            user=user,
            primary=primary,
            duplicate=duplicate,
            evidence=evidence[:1000],
            status=ContactMerge.STATUS_MERGED,
            moved_touch_ids=[int(pk) for pk in moved],
            field_changes=field_changes,
            note_line=note_line,
            duplicate_was_archived=was_archived,
            resolved_at=timezone.now(),
        )


def reject(user, a: Contact, b: Contact, evidence: str = "") -> ContactMerge:
    """Record "different people". Writes nothing to either contact; the row
    is the never-suggest-again memory."""
    return ContactMerge.all_objects.create(
        user=user,
        primary=a,
        duplicate=b,
        evidence=evidence[:1000],
        status=ContactMerge.STATUS_REJECTED,
        resolved_at=timezone.now(),
    )


def undo(record: ContactMerge) -> bool:
    """Reverse exactly what the merge did, and only where the state still
    matches what the merge wrote — a value the user changed by hand since is
    never overwritten (the `capture.mailfacts.undo` contract). Idempotent:
    only `merged` rows undo. Returns whether anything was reversed."""
    if record.status != ContactMerge.STATUS_MERGED:
        return False
    user_id = record.user_id
    primary = record.primary
    duplicate = record.duplicate
    with transaction.atomic():
        moved = [int(pk) for pk in (record.moved_touch_ids or [])]
        if moved:
            # Only touches that still sit on the primary move back: one the
            # user deleted stays deleted, and none created since is touched.
            Touch.objects.for_user(user_id).filter(
                id__in=moved, contact=primary
            ).update(contact=duplicate)

        update_fields: list[str] = []
        for field, change in (record.field_changes or {}).items():
            if getattr(primary, field, None) == change.get("after"):
                setattr(primary, field, change.get("before"))
                update_fields.append("firm" if field == "firm_id" else field)
        if record.note_line and record.note_line in (primary.notes or ""):
            lines = [
                line for line in primary.notes.splitlines()
                if line != record.note_line
            ]
            primary.notes = "\n".join(lines).strip()
            update_fields.append("notes")
        if update_fields:
            primary.save(update_fields=update_fields)

        if duplicate.archived and not record.duplicate_was_archived:
            duplicate.archived = False
            duplicate.save(update_fields=["archived"])

        record.status = ContactMerge.STATUS_UNDONE
        record.resolved_at = timezone.now()
        record.save(update_fields=["status", "resolved_at"])
    return True
