"""Shared instrumentation helper — the single write path for `product_events`.

Every Wave 2 app records funnel events through `record_event` so the 40%
activity health check (build-plan.md §8) is a query, not a guess. Keep event
names stable and snake_case; the canonical set the health check reads:

    signup, onboarded, import_completed, capture_email_received,
    touch_logged, score_viewed, digest_sent, digest_opened

`import_completed` fires only when a CSV import actually created at least one
contact — an unreadable file, an all-duplicate file, or an all-empty file
records the sibling `import_failed` instead (accounts/services.py), so this
funnel number never counts "the button was clicked" as "the import worked".

`props` is free-form JSON (e.g. {"source": "capture"} on touch_logged so the
email-vs-manual capture mix is measurable). Never put raw email bodies or
secrets in props.
"""
from __future__ import annotations

from typing import Any

from analytics.models import ProductEvent


def record_event(event: str, *, user=None, **props: Any) -> ProductEvent:
    """Append one immutable product event. `user` may be None for
    pre-auth/aggregate events. Uses the unscoped manager so this works
    regardless of tenant context (it is the writer, not a tenant reader)."""
    manager = getattr(ProductEvent, "all_objects", ProductEvent.objects)
    return manager.create(
        user=user,
        event=event,
        props=props or {},
    )
