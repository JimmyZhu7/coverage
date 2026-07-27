"""Capture providers — the swappable component (docs/build-plan.md §5).

The **interface is the** :class:`InteractionEvent`, and the seam is exactly
where the legacy ``gmail_enrich.apply_enrichment`` drew it: mailbox reading
happens *somewhere else*; a typed finding arrives; a deterministic apply layer
(``coverage_domain.pipeline`` via ``crm.services.log_touch``) ratchets state.

A :class:`CaptureProvider` is an adapter for one source. It turns a raw payload
into a typed :class:`InteractionEvent` that the pipeline
(``capture.services``) consumes **uniformly**, regardless of source. v1 ships
:class:`InboundEmailProvider`, one adapter that parses a Postmark-style
inbound-email JSON payload and tags the event ``bcc`` when the classifier read
the message as outbound and ``forward`` when it read it as inbound.

Read that second label as "inbound", NOT as "the student forwarded this":
``provider`` is set straight off ``direction`` and no header parsing stands
behind it. A message that really IS a forward is detected by
``extractors.detect_forward`` and routed to ``needs_review`` before any of
this matters, because a forward's envelope describes the student rather than
the correspondent (see that function). The name is kept because it is written
into stored rows; unpicking a forward into the exchange it carries is a
separate feature, not a relabelling.

``manual`` and ``import`` are documented seams (:class:`CaptureProvider`
subclasses) that the accounts/CRM quick-log and CSV importer plug into later —
the pipeline needs no change to accept them.

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
    # "bcc" (outbound) | "forward" (inbound) | "manual" | "import". See the
    # module docstring: "forward" is a direction label, not a claim that the
    # message was forwarded.
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
    """The v1 inbound-email provider. Parses a Postmark-style inbound JSON
    payload (``To``/``Cc``/``Bcc`` + their ``*Full`` arrays, ``From``,
    ``Subject``, ``Headers`` incl. Message-ID, ``TextBody``, ``Attachments``)
    and tags the event ``bcc`` or ``forward`` purely by direction — see the
    module docstring for what those two labels do and do not assert.
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

        if classification.signals.get("bounced"):
            # The counterparty for a bounce is the address that FAILED to
            # deliver, not mailer-daemon (the technical sender). `_counterparty`
            # would otherwise happily return mailer-daemon here, since it isn't
            # the user or the capture address — see extractors.bounced_recipient
            # for the recovery order (X-Failed-Recipients, else the DSN's
            # Final-Recipient, else the attached original's To:).
            counterparty_email = classification.signals.get("failed_recipient", "")
            counterparty_name = ""
        else:
            counterparty_email, counterparty_name = self._counterparty(parsed, user, direction)

        provider = "bcc" if direction == "outbound" else "forward"
        provider_ref = parsed.message_id or self._synthetic_ref(parsed, user, counterparty_email)

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
            # Fall through to the recipients rather than parsing a body for an
            # embedded sender. Genuine forwards (where the real correspondent
            # only exists inside the body) are caught upstream by
            # `extractors.detect_forward` and never reach the apply path.
        for name, addr in parsed.recipients:
            if addr and not is_self_or_capture(addr):
                return addr, name
        # Nothing usable — leave blank. `services.resolve_contact` REFUSES to
        # invent a contact from a blank pair (UnidentifiableContactError) and
        # the event lands in needs_review; this must never be read as licence
        # to create a placeholder.
        return "", ""

    @staticmethod
    def _synthetic_ref(parsed: ParsedInbound, user, counterparty_email: str) -> str:
        """Stable fallback dedup key when an email has no Message-ID: a hash of
        (user, sender, counterparty, subject, date). Deterministic, so a
        redelivery of the same header-less message still de-dupes.

        `counterparty_email` is load-bearing: without it, a batch of
        templated cold emails sent to different people in the same second
        (same sender, same subject, same date, no Message-ID) all hashed to
        the SAME key — the first one landed, and the rest silently returned
        "duplicate" with no contact resolved and no touch logged. Keying on
        the resolved counterparty (the one address each of those emails
        actually differs on) is enough to tell them apart; the caller may
        pass "" when no counterparty could be resolved at all, in which case
        this degrades to exactly the old behavior for that one message."""
        basis = "|".join([
            str(user.pk),
            extractors.normalize_email(parsed.from_email),
            extractors.normalize_email(counterparty_email),
            (parsed.subject or "").strip(),
            parsed.occurred_at.isoformat() if parsed.occurred_at else "",
        ])
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
        return f"synth-{digest}"
