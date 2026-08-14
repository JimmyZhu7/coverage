"""generate_vapid_keys — one-time, no-cost setup for Web Push deadline alerts.

    python manage.py generate_vapid_keys

Prints a fresh EC (P-256) keypair, base64url-encoded in the exact form
`settings.VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` want (see settings/base.py
and .env.example) — the same "raw point" format the wider Web Push ecosystem
(Node's `web-push` CLI, browsers' own `PushManager.subscribe()`) already
uses, so the public key can be handed to `applicationServerKey` unmodified.

Nothing here talks to a server or costs anything: VAPID (RFC 8292) is a
keypair this app generates and keeps, used to sign requests the browsers'
own push relays already trust by protocol, not a third-party push service
with an API key to buy. Run it once, paste the two printed lines into your
.env (or hosting provider's env vars), and deadline alerts activate — see
accounts.push.is_configured().

The private key is printed to stdout and never written to disk by this
command — treat it like any other secret (it can sign requests as this
server to every subscriber's browser) and don't commit it.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid
from py_vapid.utils import b64urlencode, num_to_bytes


class Command(BaseCommand):
    help = "Generate a VAPID (EC P-256) keypair for Web Push deadline alerts."

    def handle(self, *args, **opts):
        vapid = Vapid()
        vapid.generate_keys()

        # The private key as a raw 32-byte big-endian scalar, base64url —
        # NOT the PEM/DER py_vapid.save_key() would write to a file. This is
        # the compact form pywebpush.Vapid.from_string() recognizes by
        # length (32 raw bytes) and the form every other Web Push tool
        # (Node's web-push, browser devtools snippets) already emits, so a
        # key generated here or anywhere else is interchangeable.
        private_value = vapid.private_key.private_numbers().private_value
        private_key = b64urlencode(num_to_bytes(private_value, 32))

        # The public key as the uncompressed EC point (0x04 || X || Y, 65
        # bytes), base64url — exactly what the Push API's
        # `applicationServerKey` option expects client-side.
        public_bytes = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        public_key = b64urlencode(public_bytes)

        self.stdout.write("Generated a new VAPID keypair. Add these to your .env:\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public_key}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private_key}")
        self.stdout.write(
            "\nAlso set VAPID_CLAIM_EMAIL to a contact address (no \"mailto:\" "
            "prefix — that's added automatically). This keypair is yours to "
            "keep permanently; regenerating it invalidates every existing "
            "push subscription, so treat it like any other long-lived secret."
        )
