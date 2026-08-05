"""CRM self-contradiction checks: when the board disagrees with itself.

`directory/health.py` does this for scraping — the failures that happen in
silence. This is the same idea for the contact board, and it exists because a
contradiction shipped: contacts created BY the mailbox scan sat at zero
touches, so the cadence engine's never-contacted branch fired and Today said
"Added but never contacted. Send the first note." about people whose own notes
read "Follow-up outreach sent … no reply yet" (observed 2026-08-05, three
contacts, reported by the owner).

The root cause is fixed (`capture_discover` now records `outreach_sent`), but
a fix for one cause is not a guarantee about the class. Anything that creates
contacts — a CSV import, a hand-add, a future connector, a skill file edited
by someone who never reads this module — can reintroduce the same shape. So
the INVARIANT is asserted here rather than trusted to each writer.

WHAT IS AND IS NOT A CONTRADICTION

A hand-added contact with no touches is perfectly honest: you added someone
you have not written to yet, and "send the first note" is exactly right. That
is the common case and it must never be flagged.

The contradiction is narrower: a contact the MAILBOX SCAN created with no
touches. You cannot discover a stranger — the scan only creates people it
found in your mail, which means an interaction exists by construction. Zero
touches there is not a fact about the relationship; it is evidence that
something was dropped between finding it and recording it.
"""

from __future__ import annotations

from django.db.models import Count

from .models import Contact

# Provenance stamped by the discovery command (its --source default). A
# contact carrying it was created because the scan FOUND them.
CAPTURE_SOURCE = "capture"


def discovered_but_untouched(user) -> list[Contact]:
    """Contacts the mailbox scan created that carry no interaction at all.

    By construction this list should be empty: discovery only creates people
    it found in the user's mail. A non-empty result means a finding's evidence
    reached `Contact.notes` but not the touch ledger — the exact shape of the
    dropped-`outreach_sent` bug.
    """
    return list(
        Contact.objects.for_user(user)
        .filter(archived=False, source=CAPTURE_SOURCE)
        .annotate(n_touches=Count("touches"))
        .filter(n_touches=0)
        .order_by("name")
    )


def untouched_with_an_address(user) -> list[Contact]:
    """Contacts with an email address and no touches — a REVIEW list, not an
    error.

    Deliberately separate from the check above, and deliberately not phrased
    as a fault: every one of these is legitimately "I added them, I have not
    written yet". The reason to surface them at all is that they are exactly
    the population whose history the sync cannot see with a two-day window,
    so they are where a missed thread would hide. `capture_worklist` gives
    each of them a 365-day first scan for the same reason.
    """
    return list(
        Contact.objects.for_user(user)
        .filter(archived=False)
        .exclude(email="")
        .annotate(n_touches=Count("touches"))
        .filter(n_touches=0)
        .order_by("name")
    )


def health_report(user) -> list[str]:
    """Human-readable lines; empty when the board agrees with itself."""
    lines: list[str] = []

    broken = discovered_but_untouched(user)
    if broken:
        names = ", ".join(c.name for c in broken[:8])
        more = f" (+{len(broken) - 8} more)" if len(broken) > 8 else ""
        lines.append(
            "⚠ found in your mail but carrying NO touch, so Today will call "
            "them never-contacted — evidence was dropped between the scan and "
            f"the ledger: {names}{more}"
        )

    review = untouched_with_an_address(user)
    if review:
        names = ", ".join(c.name for c in review[:8])
        more = f" (+{len(review) - 8} more)" if len(review) > 8 else ""
        lines.append(
            "· have an address but no touches yet. Honest if you truly have "
            "not written; the next sync gives each a full-history first scan "
            f"in case the thread predates them: {names}{more}"
        )
    return lines
