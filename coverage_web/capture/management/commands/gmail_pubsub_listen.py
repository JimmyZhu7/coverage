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

DATABASE CONNECTIONS ARE RECYCLED BY HAND HERE, per notification — see
`_process` below. Nothing else in this process does it, because this process
has no request cycle, and `settings/base.py`'s `CONN_MAX_AGE`/
`CONN_HEALTH_CHECKS` only take effect inside one.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

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
        # `is_push_configured()`, not the base `is_configured()`: this
        # command IS the push half of Gmail Live, so it holds itself to the
        # stricter gate (GMAIL_LIVE_PUBSUB_TOPIC) that `gmail_poll` and the
        # connect flow don't need.
        if not gmail_live.is_push_configured():
            self.stdout.write(
                "Gmail Live push is not configured (no GMAIL_LIVE_PUBSUB_TOPIC) "
                "— nothing to listen for."
            )
            return
        # A topic is enough to REGISTER a watch, but this command PULLS off a
        # subscription on that topic, which is a separate, hand-created
        # setting (docs/gmail-live-setup.md §5). Refuse clearly here rather
        # than let `SubscriberClient` fail obscurely on an empty path.
        if not settings.GMAIL_LIVE_PUBSUB_SUBSCRIPTION:
            raise CommandError(
                "GMAIL_LIVE_PUBSUB_SUBSCRIPTION is not set — a pull "
                "subscription on GMAIL_LIVE_PUBSUB_TOPIC is required to "
                "listen; see docs/gmail-live-setup.md §5."
            )

        # Imported here, not at module level: this command is the only
        # thing in the app that needs the Pub/Sub *subscriber* client (the
        # publish side is Google's own `gmail-api-push` service account,
        # granted access once by hand — see docs/gmail-live-setup.md), so
        # keeping the import local matches how cairosvg is deferred in
        # fetch_firm_logos for an equally optional runtime dependency.
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = settings.GMAIL_LIVE_PUBSUB_SUBSCRIPTION

        def _process(data: bytes) -> None:
            """Raises on failure — callers decide what that means for
            ack/nack, since the two Pub/Sub APIs below expose that
            differently (a synchronous `pull()` result has no .ack()/
            .nack() of its own; the streaming `subscribe()` callback's
            Message wrapper does).

            THE `close_old_connections()` PAIR IS LOAD-BEARING, and is what
            Django's own request cycle does for every view — it connects the
            same function to `request_started` and `request_finished`
            (django/db/__init__.py). This command is the one always-on
            process in the app, and it has no request cycle at all, so
            without these two calls nothing here ever recycles the database
            connection:

              - `CONN_MAX_AGE` (settings/base.py, 60s) is enforced only by
                `close_if_unusable_or_obsolete()`, which is reachable from
                those two signals and nowhere else. Un-called, `close_at`
                is set and never compared, so one connection lives for the
                whole life of the worker.
              - `CONN_HEALTH_CHECKS=True` is inert for the same reason.
                `connect()` sets `health_check_done = True` ("new
                connections are healthy") and only
                `close_if_unusable_or_obsolete()` ever sets it back to
                False, so the pre-query ping in `_cursor()` returns early
                forever. The health check that is supposed to catch a
                connection killed server-side never runs even once.

            The failure that combination produces is silent and permanent,
            which is why it is worth ten lines of comment: this worker can
            idle for hours between notifications, and a managed Postgres'
            idle reaper or a database restart drops the connection in that
            gap. The next notification's first query
            (`process_notification`'s `GmailConnection.all_objects.get`)
            then raises `InterfaceError`/`OperationalError` — which
            `_callback` below catches, nacks, and carries on from. Pub/Sub
            redelivers, the same dead connection raises again, and Gmail
            Live stops syncing for EVERY connected mailbox with nothing
            crashing to say so.

            Calling this at the start is what fixes that (an obsolete or
            dead connection is dropped and the ORM reconnects); calling it
            again at the end is what stops the worker from sitting on an
            idle connection between notifications, and matters a second
            time because the streaming `subscribe()` callback runs on the
            Pub/Sub client's own thread pool and Django connections are
            thread-local — one per callback thread, none of them attached
            to anything that would otherwise close them.
            """
            close_old_connections()
            try:
                payload = json.loads(data.decode("utf-8"))
                gmail_live.process_notification(
                    payload["emailAddress"], str(payload.get("historyId", ""))
                )
            finally:
                close_old_connections()

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
