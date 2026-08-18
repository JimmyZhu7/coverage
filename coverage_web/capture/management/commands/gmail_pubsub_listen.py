"""gmail_pubsub_listen — the real-time half of Gmail Live.

Pulls Gmail change notifications off the Pub/Sub PULL subscription
(GMAIL_LIVE_PUBSUB_SUBSCRIPTION) and syncs the named mailbox on each one.
Pull, not push: see gmail_live.py's module docstring for why — a push
subscription needs a public HTTPS endpoint, and this command deliberately
doesn't require Coverage to be deployed anywhere to work.

Long-running by design — this IS the "real-time" part, so it has to stay
attached to receive notifications as they happen. Run it under whatever
keeps a process alive on the host (launchd/systemd/tmux); it is not a cron
job like every other capture command in this app.

    python manage.py gmail_pubsub_listen            # runs until killed
    python manage.py gmail_pubsub_listen --once      # pulls once, for testing

Each Pub/Sub message's data is Gmail's own notification payload:
`{"emailAddress": "...", "historyId": "..."}`, base64-encoded per the Pub/Sub
wire format (the client library decodes this automatically).
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from capture import gmail_live


class Command(BaseCommand):
    help = "Listen for Gmail Live change notifications and sync on each one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="Pull whatever is queued right now, sync, and exit "
                 "(for testing the pipeline without a running process).",
        )

    def handle(self, *args, **opts):
        if not gmail_live.is_configured():
            self.stdout.write("Gmail Live is not configured — nothing to listen for.")
            return

        # Imported here, not at module level: this command is the only
        # thing in the app that needs the Pub/Sub *subscriber* client (the
        # publish side is Google's own `gmail-api-push` service account,
        # granted access once by hand — see docs/gmail-live-setup.md), so
        # keeping the import local matches how cairosvg is deferred in
        # fetch_firm_logos for an equally optional runtime dependency.
        from google.cloud import pubsub_v1
        from django.conf import settings as django_settings

        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = django_settings.GMAIL_LIVE_PUBSUB_SUBSCRIPTION

        def _process(data: bytes) -> None:
            """Raises on failure — callers decide what that means for
            ack/nack, since the two Pub/Sub APIs below expose that
            differently (a synchronous `pull()` result has no .ack()/
            .nack() of its own; the streaming `subscribe()` callback's
            Message wrapper does)."""
            payload = json.loads(data.decode("utf-8"))
            gmail_live.process_notification(
                payload["emailAddress"], str(payload.get("historyId", ""))
            )

        if opts["once"]:
            response = subscriber.pull(
                subscription=subscription_path, max_messages=50
            )
            processed = 0
            for received in response.received_messages:
                try:
                    _process(received.message.data)
                except Exception as exc:  # noqa: BLE001
                    # Leave it unacked — Pub/Sub redelivers after the
                    # subscription's ack deadline, same "don't let one bad
                    # notification eat the message" behaviour as the
                    # streaming branch's nack() below.
                    self.stderr.write(f"notification failed, leaving unacked: {exc}")
                    continue
                subscriber.acknowledge(
                    subscription=subscription_path,
                    ack_ids=[received.ack_id],
                )
                processed += 1
            self.stdout.write(
                f"processed {processed}/{len(response.received_messages)} notification(s)"
            )
            return

        def _callback(message) -> None:
            try:
                _process(message.data)
            except Exception as exc:  # noqa: BLE001
                # One bad notification must not take the listener down —
                # nack it (Pub/Sub redelivers) and keep going. A crash here
                # means every OTHER connected mailbox stops getting synced
                # until something notices the process died.
                self.stderr.write(f"notification failed, nacking: {exc}")
                message.nack()
                return
            message.ack()

        future = subscriber.subscribe(subscription_path, callback=_callback)
        self.stdout.write(f"listening on {subscription_path} ... (Ctrl-C to stop)")
        try:
            future.result()
        except KeyboardInterrupt:
            future.cancel()
            future.result()
