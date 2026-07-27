"""The capture pipeline (docs/build-plan.md §5) — deterministic, idempotent,
identical for every provider.

    idempotent insert  ->  resolver  ->  deterministic extractors  ->  apply

This module owns steps that touch the database and tenancy; the *parsing* and
*classification* live in ``capture.providers`` / ``capture.extractors`` so they
stay DB-free and unit-testable. Nothing here calls an LLM: ambiguous events are
parked as ``status='needs_review'`` (a one-click human queue), never guessed.

Warmth is only ever advanced through the ported, reviewed ratchet
(``crm.services.log_touch`` -> ``coverage_domain.pipeline.apply_touch``); this
module never writes ``contacts.warmth`` / ``thread_state`` itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db.models import Count, Max
from django.utils import timezone

from analytics.events import record_event
from capture import extractors
from capture.providers import InboundEmailProvider, InteractionEvent, ParsedInbound
from crm import services as crm_services
from crm.models import CaptureEvent, Contact, Touch

User = get_user_model()

# The inbound capture domain. A module constant rather than a setting because
# settings/ is out of this app's ownership (see task boundaries); the founder
# points Postmark's inbound stream at this domain and sets DNS/MX for it — see
# report. Routing itself is domain-tolerant: we key on the ``u-<slug>`` local
# part, so a different real domain still routes as long as the local part
# survives.
CAPTURE_DOMAIN = "in.coverage.app"

# Auto-created, not-yet-confirmed contacts (the resolver's "unknown -> pending
# contact for one-click confirm", §5). A greppable marker so the confirm UI /
# founder tooling can surface them; confirming flips it to plain "capture".
PENDING_SOURCE = "capture_pending"
CONFIRMED_SOURCE = "capture"

_SLUG_LOCALPART_RE = re.compile(r"^u-(?P<slug>[A-Za-z0-9_-]+)$")


@dataclass
class IngestResult:
    """What the webhook view turns into an HTTP response."""

    status: str  # applied | needs_review | duplicate | ignored
    http_status: int = 200
    detail: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.detail is None:
            self.detail = {}


# --------------------------------------------------------------------------- #
# Routing: inbound address -> user
# --------------------------------------------------------------------------- #

def _slugs_in_recipients(recipients: list[tuple[str, str]]) -> list[str]:
    """Extract every ``u-<slug>`` capture slug present among recipient
    addresses. Case is preserved: ``capture_slug`` is a mixed-case base64url
    token, so we must not lower-case the local part before matching."""
    slugs: list[str] = []
    for _name, addr in recipients:
        if "@" not in (addr or ""):
            continue
        localpart = addr.split("@", 1)[0].strip()
        match = _SLUG_LOCALPART_RE.match(localpart)
        if match:
            slugs.append(match.group("slug"))
    return slugs


def resolve_user(parsed: ParsedInbound):
    """Route a parsed inbound email to its owning user via the capture address.
    Returns the ``User`` or ``None`` when no recipient carries a known slug."""
    for slug in _slugs_in_recipients(parsed.recipients):
        try:
            return User.objects.get(capture_slug=slug)
        except User.DoesNotExist:
            continue
    return None


# --------------------------------------------------------------------------- #
# Resolver: counterparty -> contact (email, else name, else create pending)
# --------------------------------------------------------------------------- #

class AmbiguousContactError(Exception):
    """More than one of the user's contacts share this normalized name (see
    resolve_contact). Silently picking the first one found (previous
    behavior — whichever row the queryset happened to yield first) would
    land the touch on the wrong person as often as the right one, e.g. two
    "Michael Chen"s at different firms. The caller must route this to
    needs_review instead of guessing; a single unambiguous name match is
    unaffected and keeps resolving exactly as before."""

    def __init__(self, name: str, count: int):
        super().__init__(f"{count} contacts match name {name!r} — ambiguous")
        self.name = name
        self.count = count


class UnidentifiableContactError(Exception):
    """The event names nobody: neither a counterparty email nor a name.

    THE GUARD THAT STOPS JUNK CONTACTS. A contact row with no name and no
    address is not a lead the user can act on — it is a row that says "an
    email happened". The auto-create below used to reach its `or "Unknown
    contact"` fallback and make one anyway, complete with an `outreach`
    touch, so N unparseable messages produced N identical placeholder people
    cluttering the Network board with a warmth ratchet each.

    Every path that can produce a nameless, address-less counterparty is
    ambiguous by definition (a forward whose envelope describes the student,
    a message with no readable From and no usable recipient), so the honest
    destination is the review queue — where the user says who it was — not a
    fabricated row asserting that somebody exists.
    """

    def __init__(self) -> None:
        super().__init__("no counterparty name or email — refusing to create a contact")


def resolve_contact(user, email: str, name: str) -> tuple[Contact, bool]:
    """Match the counterparty to one of the user's contacts by email, else by
    normalized name; unknown -> auto-create a ``pending`` contact for one-click
    confirm. Returns (contact, created).

    Raises:
        AmbiguousContactError: more than one contact shares the normalized
            name and there's no email to disambiguate with.
        UnidentifiableContactError: neither a name nor an email was supplied,
            so there is nobody to match and nobody to create.
    """
    email = (email or "").strip()
    name = (name or "").strip()

    # Checked before any query: with both blank there is nothing to match on
    # either, so every branch below would fall through to the create anyway.
    if not email and not name:
        raise UnidentifiableContactError()

    scoped = Contact.objects.for_user(user).filter(archived=False)

    if email:
        match = scoped.filter(email__iexact=email).first()
        if match:
            return match, False

    if name:
        norm = extractors.normalize_name(name)
        # Small per-user contact counts make an in-Python normalized compare
        # cheap and robust to spacing/case differences a DB iexact would miss.
        matches = [
            contact for contact in scoped
            if contact.name and extractors.normalize_name(contact.name) == norm
        ]
        if len(matches) > 1:
            raise AmbiguousContactError(name, len(matches))
        if matches:
            return matches[0], False

    # Unknown -> create a pending contact (all_objects + explicit user, per the
    # tenancy contract: all_objects for creation, .for_user for scoped reads).
    # `name or email` cannot be empty here — the guard at the top of this
    # function already refused that case.
    contact = Contact.all_objects.create(
        user=user,
        name=name or email,
        email=email,
        source=PENDING_SOURCE,
    )
    return contact, True


# --------------------------------------------------------------------------- #
# Apply: signals -> touch kind -> ported ratchet
# --------------------------------------------------------------------------- #

def _clamped_occurred_at(occurred_at):
    """`event.occurred_at` is parsed from the email's Date header — a student
    forwarding/syncing last week's reply must still ratchet with THAT touch
    timestamped when it happened, not when it was ingested, or the cadence's
    business-day math, the fit score's recency/latency axes, and the
    thank-you window all read the wrong age for it. But a forged or
    misparsed Date header must never produce a touch in the future (which
    would sort ahead of "now" everywhere that reads Touch.ts), so this
    clamps to the wall clock at call time rather than trusting the header
    past "now". Returns None unchanged — `apply_touch` already defaults a
    None `now` to the current time itself."""
    if occurred_at is None:
        return None
    now = timezone.now()
    return occurred_at if occurred_at <= now else now


def _apply_touch_for_event(user, capture_event: CaptureEvent, event: InteractionEvent) -> dict:
    """Resolve the contact and advance the warmth ratchet for a classified,
    actionable event. Sets ``status='applied'`` and records instrumentation.
    Returns the dict of changed contact columns (warmth/thread_state).

    Raises:
        AmbiguousContactError: propagated from resolve_contact — the caller
            (ingest_inbound_email / confirm_event) is responsible for
            routing this to needs_review instead of guessing which contact.
        UnidentifiableContactError: likewise propagated — the event names
            nobody, so there is no contact to ratchet.
    """
    contact, _created = resolve_contact(
        user, event.counterparty_email, event.counterparty_name
    )

    # "[gmail:<thread_id>]" becomes "[capture:<event_id>]" (§5). Note carries no
    # email body — privacy (§10: no email bodies in logs/notes).
    note = f"[capture:{capture_event.id}] {event.direction} via {event.provider}"

    updates = crm_services.log_touch(
        user.id, contact.id, event.touch_kind, "email", note=note,
        now=_clamped_occurred_at(event.occurred_at),
        source="capture",
    )

    # Touch.capture_event is a declared FK that nothing ever populated — see
    # pipeline.TouchResult's docstring for why `apply_touch` can hand back
    # the inserted row's id without changing its return contract for every
    # other caller. Scoped through .for_user even though we just created
    # this exact row moments ago, matching the tenancy convention that a
    # write always goes through an explicit user scope, not an unscoped pk.
    if updates.touch_id is not None:
        Touch.objects.for_user(user).filter(pk=updates.touch_id).update(
            capture_event=capture_event
        )

    capture_event.status = "applied"
    capture_event.save(update_fields=["status"])

    record_event(
        "touch_logged",
        user=user,
        source="capture",
        kind=event.touch_kind,
        contact_id=contact.id,
        warmth_changed=bool(updates),
    )
    return updates


# --------------------------------------------------------------------------- #
# The pipeline entry point
# --------------------------------------------------------------------------- #

def ingest_inbound_email(payload: dict) -> IngestResult:
    """Run one inbound-email payload through the full pipeline. Pure of HTTP —
    the view handles auth/serialization; this returns an :class:`IngestResult`.
    """
    provider = InboundEmailProvider()
    parsed = provider.parse(payload)

    # --- route to user -------------------------------------------------- #
    user = resolve_user(parsed)
    if user is None:
        # Unknown address: counted, never crashes, never inserts. 2xx so the
        # sender (Postmark) doesn't retry a permanently-unroutable message.
        record_event(
            "capture_email_received",
            user=None,
            routed=False,
            reason="unknown_capture_address",
        )
        return IngestResult(
            status="ignored",
            http_status=202,
            detail={"reason": "unknown_capture_address"},
        )

    # --- build the typed event (classify) ------------------------------- #
    event = provider.build_event(parsed, user)

    initial_status = "needs_review" if event.needs_review else "pending"
    received_at = timezone.now()

    # --- idempotent insert (the load-bearing dedup) --------------------- #
    # UNIQUE(user, provider, provider_ref=Message-ID) makes at-least-once
    # webhook delivery a no-op: a redelivered payload finds the existing row
    # and we short-circuit before resolving a contact or logging a touch, so a
    # duplicate POST yields exactly one CaptureEvent and one Touch.
    capture_event, created = CaptureEvent.all_objects.get_or_create(
        user=user,
        provider=event.provider,
        provider_ref=event.provider_ref,
        defaults={
            "direction": event.direction,
            "counterparty_email": event.counterparty_email,
            "counterparty_name": event.counterparty_name,
            "occurred_at": event.occurred_at,
            "received_at": received_at,
            "raw_ref": event.raw_ref,
            "signals": event.signals,
            "extraction_version": event.extraction_version,
            "status": initial_status,
        },
    )

    if not created:
        return IngestResult(
            status="duplicate",
            http_status=200,
            detail={"event_id": capture_event.id, "existing_status": capture_event.status},
        )

    # A distinct email was captured (received counter, §8 / health strip).
    record_event(
        "capture_email_received",
        user=user,
        routed=True,
        provider=event.provider,
        direction=event.direction,
    )

    # --- apply --------------------------------------------------------- #
    if event.needs_review:
        # Deliberately NOT sent to an LLM: parked for one-click human review.
        return IngestResult(
            status="needs_review",
            http_status=200,
            detail={"event_id": capture_event.id, "reason": event.review_reason},
        )

    if event.touch_kind is None:
        # Cleanly classified but no warmth touch (e.g. a bounce). Fully
        # processed — mark applied so it doesn't linger as pending.
        capture_event.status = "applied"
        capture_event.save(update_fields=["status"])
        return IngestResult(
            status="applied",
            http_status=200,
            detail={
                "event_id": capture_event.id,
                "touch": None,
                "bounced": bool(event.signals.get("bounced")),
            },
        )

    try:
        updates = _apply_touch_for_event(user, capture_event, event)
    except AmbiguousContactError:
        # Two+ contacts share this normalized name and there's no email to
        # tell them apart — logging the touch on whichever one the query
        # happened to return first would be a guess dressed up as certainty.
        # Park it for a human instead of silently picking one.
        capture_event.status = "needs_review"
        capture_event.save(update_fields=["status"])
        return IngestResult(
            status="needs_review",
            http_status=200,
            detail={"event_id": capture_event.id, "reason": "ambiguous_contact_name"},
        )
    except UnidentifiableContactError:
        # The classifier was confident enough to pick a touch kind, but the
        # message names nobody to hang it on. Same destination as the
        # ambiguous case and for the same reason: the user knows who this
        # was, and one click is cheaper than a placeholder contact that
        # nobody can ever reconcile.
        capture_event.status = "needs_review"
        capture_event.save(update_fields=["status"])
        return IngestResult(
            status="needs_review",
            http_status=200,
            detail={"event_id": capture_event.id, "reason": "unidentifiable_contact"},
        )
    return IngestResult(
        status="applied",
        http_status=200,
        detail={
            "event_id": capture_event.id,
            "touch_kind": event.touch_kind,
            "warmth_changed": updates,
        },
    )


# --------------------------------------------------------------------------- #
# needs_review confirmation (the one-click queue)
# --------------------------------------------------------------------------- #

def confirm_event(user, event_id: int, touch_kind: str) -> dict:
    """Confirm a ``needs_review`` event: resolve its counterparty to a contact
    and log the chosen touch kind through the ratchet, then mark it applied.
    Scoped to ``user`` — a foreign event id is indistinguishable from a
    missing one (raises ``CaptureEvent.DoesNotExist``)."""
    if touch_kind not in {
        extractors.KIND_OUTREACH,
        extractors.KIND_REPLY,
        extractors.KIND_CHAT_SCHEDULED,
        extractors.KIND_CHAT,
    }:
        raise ValueError(f"unsupported touch_kind {touch_kind!r}")

    capture_event = CaptureEvent.objects.for_user(user).get(
        id=event_id, status="needs_review"
    )
    event = InteractionEvent(
        user_id=user.pk,
        provider=capture_event.provider,
        provider_ref=capture_event.provider_ref,
        direction=capture_event.direction or "inbound",
        counterparty_email=capture_event.counterparty_email,
        counterparty_name=capture_event.counterparty_name,
        # The event this confirmation applies to already recorded when the
        # underlying email happened — carry it through so a confirmed
        # needs_review item ratchets at that time too, not at confirm-click
        # time (same reasoning as the auto-applied path; see
        # _clamped_occurred_at).
        occurred_at=capture_event.occurred_at,
        touch_kind=touch_kind,
    )
    return _apply_touch_for_event(user, capture_event, event)


def ignore_event(user, event_id: int) -> None:
    """Dismiss a ``needs_review`` event without logging a touch."""
    capture_event = CaptureEvent.objects.for_user(user).get(
        id=event_id, status="needs_review"
    )
    capture_event.status = "ignored"
    capture_event.save(update_fields=["status"])


# --------------------------------------------------------------------------- #
# Health ("is my capture working?" strip, §7 M3 DoD / risk 3)
# --------------------------------------------------------------------------- #

def capture_address(user) -> str:
    return f"u-{user.capture_slug}@{CAPTURE_DOMAIN}"


def capture_health(user) -> dict:
    """Summary for the capture-health view: the address, last-received time,
    and counts by status."""
    events = CaptureEvent.objects.for_user(user)
    agg = events.aggregate(last_received=Max("received_at"), total=Count("id"))
    by_status = {
        row["status"]: row["n"]
        for row in events.values("status").annotate(n=Count("id"))
    }
    return {
        "address": capture_address(user),
        "last_received": agg["last_received"],
        "total": agg["total"] or 0,
        "counts": {
            "applied": by_status.get("applied", 0),
            "needs_review": by_status.get("needs_review", 0),
            "pending": by_status.get("pending", 0),
            "ignored": by_status.get("ignored", 0),
        },
    }


def needs_review_events(user):
    """The needs_review queue, newest first."""
    return CaptureEvent.objects.for_user(user).filter(status="needs_review")
