"""Capture providers — the swappable component (docs/build-plan.md §5).

The **interface is the** :class:`InteractionEvent`, and the seam is exactly
where the legacy ``gmail_enrich.apply_enrichment`` drew it: mailbox reading
happens *somewhere else*; a typed finding arrives; a deterministic apply layer
(``coverage_domain.pipeline`` via ``crm.services.log_touch``) ratchets state.

A :class:`CaptureProvider` is an adapter for one source. It turns a raw payload
into a typed :class:`InteractionEvent` that the pipeline
(``capture.services``) consumes **uniformly**, regardless of source. v1 ships
the ``bcc`` / ``forward`` provider (:class:`InboundEmailProvider`, one adapter
that parses a Postmark-style inbound-email JSON payload and tags the event
``bcc`` for outbound BCCs, ``forward`` for forwarded replies). ``manual`` and
``import`` are documented seams (:class:`CaptureProvider` subclasses) that the
accounts/CRM quick-log and CSV importer plug into later — the pipeline needs no
change to accept them.

Deliberately no DB or Django-ORM access in this module beyond reading
``User.email``/``capture_slug`` off a passed-in user: providers parse; the
pipeline persists. That keeps each provider unit-testable against a fixture
payload with no database.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Optional

from capture import extractors


# --------------------------------------------------------------------------- #
# The typed intermediate (§5's InteractionEvent)
# --------------------------------------------------------------------------- #

@dataclass
class InteractionEvent:
    """The one shape every provider emits and the pipeline consumes.

    Mirrors ``capture_events`` (§2) plus the classifier's verdict. ``signals``
    is the typed, versioned payload — never raw email text flows past this into
    scoring.
    """

    user_id: int
    provider: str  # "bcc" | "forward" | "manual" | "import"
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


# --------------------------------------------------------------------------- #
# Normalised inbound email (what InboundEmailProvider parses a payload into)
# --------------------------------------------------------------------------- #

@dataclass
class Attachment:
    name: str = ""
    content_type: str = ""
    content_b64: str = ""

    def decoded_text(self) -> str:
        if not self.content_b64:
            return ""
        try:
            return base64.b64decode(self.content_b64).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""


@dataclass
class ParsedInbound:
    """A provider-agnostic view of one inbound email, parsed from the raw
    webhook payload. Extractors operate on this, never on the raw dict."""

    from_email: str = ""
    from_name: str = ""
    recipients: list[tuple[str, str]] = field(default_factory=list)  # (name, email)
    subject: str = ""
    message_id: str = ""
    text_body: str = ""
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased name -> value
    attachments: list[Attachment] = field(default_factory=list)
    occurred_at: Optional[datetime] = None
    postmark_message_id: str = ""

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


# --------------------------------------------------------------------------- #
# Provider base + inbound-email adapter
# --------------------------------------------------------------------------- #

class CaptureProvider:
    """Adapter base. Subclasses build :class:`InteractionEvent`s from a source.

    The pipeline only depends on this shape, so adding ``manual``/``import``/a
    future ``gmail_api`` adapter is a new subclass, not a pipeline change.
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


class InboundEmailProvider(CaptureProvider):
    """The v1 ``bcc``/``forward`` provider. Parses a Postmark-style inbound
    JSON payload (``To``/``Cc``/``Bcc`` + their ``*Full`` arrays, ``From``,
    ``Subject``, ``Headers`` incl. Message-ID, ``TextBody``, ``Attachments``).
    """

    name = "inbound_email"

    # ---- parse: raw payload -> ParsedInbound (pure, no DB) ---------------- #

    def parse(self, payload: dict) -> ParsedInbound:
        headers = {
            str(h.get("Name", "")).lower(): str(h.get("Value", ""))
            for h in payload.get("Headers", []) or []
            if isinstance(h, dict)
        }

        from_email, from_name = self._first_addr(
            payload.get("FromFull"), payload.get("From")
        )

        recipients: list[tuple[str, str]] = []
        for full_key, raw_key in (("ToFull", "To"), ("CcFull", "Cc"), ("BccFull", "Bcc")):
            recipients.extend(self._all_addrs(payload.get(full_key), payload.get(raw_key)))
        # Postmark also reports the address the message was delivered to.
        orig = payload.get("OriginalRecipient")
        if orig:
            for name, addr in getaddresses([str(orig)]):
                if addr:
                    recipients.append((name, addr))

        attachments = [
            Attachment(
                name=str(a.get("Name", "")),
                content_type=str(a.get("ContentType", "")),
                content_b64=str(a.get("Content", "")),
            )
            for a in payload.get("Attachments", []) or []
            if isinstance(a, dict)
        ]

        message_id = (
            headers.get("message-id", "")
            or str(payload.get("MessageID", ""))
        ).strip()

        occurred_at = _as_datetime(payload.get("Date") or headers.get("date"))

        return ParsedInbound(
            from_email=from_email,
            from_name=from_name,
            recipients=recipients,
            subject=str(payload.get("Subject", "")),
            message_id=message_id,
            text_body=str(payload.get("TextBody", "") or ""),
            content_type=headers.get("content-type", ""),
            headers=headers,
            attachments=attachments,
            occurred_at=occurred_at,
            postmark_message_id=str(payload.get("MessageID", "")),
        )

    # ---- build_event: ParsedInbound + user -> InteractionEvent ----------- #

    def build_event(self, parsed: ParsedInbound, user) -> InteractionEvent:
        """Run the deterministic classifier and assemble the typed event.

        ``user`` is the already-resolved recipient (routing happened upstream in
        the pipeline). We need ``user.email`` here to decide direction and to
        pick the counterparty (the party that isn't the user).
        """
        classification = extractors.classify(parsed, user.email)
        direction = classification.direction

        counterparty_email, counterparty_name = self._counterparty(parsed, user, direction)

        provider = "bcc" if direction == "outbound" else "forward"
        provider_ref = parsed.message_id or self._synthetic_ref(parsed, user)

        return InteractionEvent(
            user_id=user.pk,
            provider=provider,
            provider_ref=provider_ref,
            direction=direction,
            counterparty_email=counterparty_email,
            counterparty_name=counterparty_name,
            occurred_at=parsed.occurred_at,
            raw_ref=parsed.postmark_message_id,
            signals=classification.signals,
            extraction_version=classification.signals.get(
                "extraction_version", extractors.EXTRACTION_VERSION
            ),
            touch_kind=classification.touch_kind,
            needs_review=classification.needs_review,
            review_reason=classification.review_reason,
        )

    # ---- helpers --------------------------------------------------------- #

    @staticmethod
    def _first_addr(full: Any, raw: Any) -> tuple[str, str]:
        """Return (email, name) for the first address in a Postmark ``*Full``
        array, falling back to parsing the raw header string."""
        if isinstance(full, list) and full:
            first = full[0] or {}
            return str(first.get("Email", "")).strip(), str(first.get("Name", "")).strip()
        if isinstance(full, dict):  # some payloads give FromFull as an object
            return str(full.get("Email", "")).strip(), str(full.get("Name", "")).strip()
        if raw:
            parsed = getaddresses([str(raw)])
            if parsed:
                name, addr = parsed[0]
                return addr.strip(), name.strip()
        return "", ""

    @staticmethod
    def _all_addrs(full: Any, raw: Any) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if isinstance(full, list):
            for item in full:
                if isinstance(item, dict) and item.get("Email"):
                    out.append((str(item.get("Name", "")).strip(), str(item["Email"]).strip()))
        if not out and raw:
            for name, addr in getaddresses([str(raw)]):
                if addr:
                    out.append((name.strip(), addr.strip()))
        return out

    @staticmethod
    def _counterparty(parsed: ParsedInbound, user, direction: str) -> tuple[str, str]:
        """The party that isn't the user and isn't the capture address.

        Outbound: the primary recipient. Inbound: the sender.
        """
        user_email = extractors.normalize_email(user.email)
        slug = (getattr(user, "capture_slug", "") or "").lower()

        def is_self_or_capture(addr: str) -> bool:
            a = extractors.normalize_email(addr)
            if a == user_email:
                return True
            return bool(slug) and f"u-{slug}@" in a

        if direction == "inbound":
            if parsed.from_email and not is_self_or_capture(parsed.from_email):
                return parsed.from_email, parsed.from_name
            # Forwarded mail can put the original sender in the body; v1 falls
            # back to the first non-self recipient rather than parsing bodies.
        for name, addr in parsed.recipients:
            if addr and not is_self_or_capture(addr):
                return addr, name
        # Nothing usable — leave blank; the resolver/needs_review path copes.
        return "", ""

    @staticmethod
    def _synthetic_ref(parsed: ParsedInbound, user) -> str:
        """Stable fallback dedup key when an email has no Message-ID: a hash of
        (user, sender, subject, date). Deterministic, so a redelivery of the
        same header-less message still de-dupes."""
        basis = "|".join([
            str(user.pk),
            extractors.normalize_email(parsed.from_email),
            (parsed.subject or "").strip(),
            parsed.occurred_at.isoformat() if parsed.occurred_at else "",
        ])
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
        return f"synth-{digest}"
