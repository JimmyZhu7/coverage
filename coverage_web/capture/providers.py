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


# --------------------------------------------------------------------------- #
# What a chat claim has to bring with it
# --------------------------------------------------------------------------- #

# `chat_status` is the only field on a finding that can move a contact onto
# the chat rungs of the ladder, and those rungs are the expensive ones:
# `chat_scheduled` parks a contact in a state branch 2 of the cadence engine
# `continue`s on, and `chat` sets warmth `chatted`, which
# `capture_worklist.RECHECK_WARMTH` drops from every later re-check. A wrong
# value there is not corrected by the next run; it is corrected by a human or
# never.
#
# Nothing in this repo produces the value. `capture.gmail_live` emits
# `"scheduled" if ics_dt else "none"` at both of its exits and never emits
# `"completed"` at all, so every chat claim that is not backed by a parsed
# .ics DTSTART reaches Coverage from a classifier running OUTSIDE this repo,
# over prose. Two live failures came in exactly that way:
#
#   * Ellen Chung, 2026-08-12. `"completed"` off "Thanks for the email,
#     filled out the form!" — no call, no meeting. She was ratcheted to
#     warmth `chatted` / thread_state `chat_done` and nagged for a debrief of
#     a conversation that never happened. Corrected by hand on 08-15;
#     no automated run could have found it, because `chatted` had already
#     dropped her off the worklist.
#   * Youqi Chen, 2026-08-31. `"scheduled"` off "coffee in HK, offered
#     same-day meetup" — an offer nobody had accepted became
#     `thread_state="chat_scheduled"`, with zero calendar events behind it.
#
# THE BAR IS A STATED TIME, and it is the bar the live path already meets. An
# agreement produces a time; a language judgment about enthusiasm does not.
# This function is the one place that test lives, so the three paths that
# consume `chat_status` (capture.gmail's ladder, capture.discovery's proposal
# evidence, and the capture_discover command) cannot drift apart again — the
# asymmetry between them is what let Youqi Chen through after the same shape
# had already been fixed one rung up.
CHAT_CLAIMS = ("scheduled", "completed")


def chat_status_of(finding: dict) -> str:
    """The finding's `chat_status`, normalized. "none" when absent."""
    return str(finding.get("chat_status", "none") or "none").strip().lower()


def chat_time_stated(finding: dict) -> bool:
    """Does this finding name a time for the chat it claims?

    Deliberately a presence test on `chat_scheduled_at`, not a parse: the
    field is produced by callers this module does not control, and the
    consumers that need a real datetime (`capture.gmail._upsert_scheduled_chat`)
    already parse it themselves and refuse what will not parse. Asking only
    "did anyone state a time" keeps this test the same cheap, deterministic
    question at all three call sites.
    """
    return bool(str(finding.get("chat_scheduled_at") or "").strip())


def corroborated_chat_status(finding: dict) -> str:
    """`chat_status`, with an uncorroborated chat claim reduced to "none".

    Callers then fall back to whatever the message itself proves — a reply is
    still a reply, a send is still a send. Under-reporting is the recoverable
    direction: warmth `replied` stays in `capture_worklist.RECHECK_WARMTH`, so
    the next run that sees real evidence can still climb. Over-reporting is
    not.
    """
    status = chat_status_of(finding)
    if status in CHAT_CLAIMS and not chat_time_stated(finding):
        return "none"
    return status
