"""Capture providers — the swappable component (docs/build-plan.md §5).

The **interface is the** :class:`InteractionEvent`, and the seam is exactly
where the legacy ``gmail_enrich.apply_enrichment`` drew it: mailbox reading
happens *somewhere else*; a typed finding arrives; a deterministic apply layer
(``coverage_domain.pipeline`` via ``crm.services.log_touch``) ratchets state.

A :class:`CaptureProvider` is an adapter for one source. It turns a raw payload
into a typed :class:`InteractionEvent`. Today's only provider is
``capture.gmail.GmailFindingsProvider`` — the BCC/forward inbound-email
provider that used to live here was retired 2026-08-19 once Gmail Live (real
Gmail API access) made it redundant; see capture/gmail_live.py.

Deliberately no DB or Django-ORM access in this module: providers parse; the
pipeline persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# The typed intermediate (§5's InteractionEvent)
# --------------------------------------------------------------------------- #

@dataclass
class InteractionEvent:
    """The one shape every provider emits and the pipeline consumes.

    ``signals`` is the typed, versioned payload — never raw email text flows
    past this into scoring.
    """

    user_id: int
    provider: str
    provider_ref: str  # stable dedup key: Message-ID / thread key / row hash
    direction: str  # "outbound" | "inbound"
    counterparty_email: str = ""
    counterparty_name: str = ""
    occurred_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    raw_ref: str = ""
    signals: dict = field(default_factory=dict)
    extraction_version: str = ""
    # Pipeline routing hints (not persisted verbatim; drive the apply step):
    touch_kind: Optional[str] = None
    needs_review: bool = False
    review_reason: str = ""


class CaptureProvider:
    """Adapter base. Subclasses build :class:`InteractionEvent`s from a source.

    The pipeline only depends on this shape, so adding a new source is a new
    subclass, not a pipeline change.
    """

    name: str = ""

    def build_event(self, *args: Any, **kwargs: Any) -> InteractionEvent:  # pragma: no cover
        raise NotImplementedError


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse an RFC-2822 date string into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_email(addr: str) -> str:
    return (addr or "").strip().lower()


def normalize_name(name: str) -> str:
    return " ".join((name or "").strip().split()).lower()


class AmbiguousContactError(Exception):
    """More than one of the user's contacts share the normalized name a
    finding named — there is no correct one to log against automatically.
    Shared between capture.gmail's contact resolver and capture_discover."""

    def __init__(self, name: str, count: int):
        self.name = name
        self.count = count
        super().__init__(f"{count} contacts match {name!r}")
