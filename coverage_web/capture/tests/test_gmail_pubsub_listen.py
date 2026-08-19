"""The always-on Gmail Live listener's database-connection hygiene.

`gmail_pubsub_listen` is the one process in this app that runs for days at a
time with no request cycle. `settings/base.py` turns on persistent
connections (CONN_MAX_AGE=60, CONN_HEALTH_CHECKS=True), and BOTH of those are
enforced only from Django's `request_started`/`request_finished` signals — so
in this worker neither one does anything, and a connection dropped
server-side during an idle gap between notifications stays broken forever
(`_callback` catches the InterfaceError, nacks, and every redelivery fails
the same way, silently, for every connected mailbox).

The fix is the `close_old_connections()` pair around each notification, which
is exactly what Django's request cycle does. These tests pin it: they assert
the calls happen, and that they bracket the work rather than merely
existing somewhere in the module.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.core.management import call_command

COMMAND = "capture.management.commands.gmail_pubsub_listen"
SUBSCRIPTION = "projects/p/subscriptions/s"
PAYLOAD = json.dumps({"emailAddress": "her@example.com", "historyId": "9910"}).encode()


class _FakeFuture:
    """`subscribe()`'s return value. `result()` returns immediately so
    `handle()` finishes and hands the captured callback back to the test —
    the real one blocks until the process is killed."""

    def __init__(self):
        self.cancelled = False

    def result(self):
        return None

    def cancel(self):
        self.cancelled = True


class _FakeMessage:
    def __init__(self, data: bytes = PAYLOAD):
        self.data = data
        self.acked = False
        self.nacked = False

    def ack(self):
        self.acked = True

    def nack(self):
        self.nacked = True


class _FakeReceived:
    def __init__(self, data: bytes = PAYLOAD, ack_id: str = "ack-1"):
        self.message = _FakeMessage(data)
        self.ack_id = ack_id


class _FakePullResponse:
    def __init__(self, received):
        self.received_messages = received


class _FakeSubscriber:
    def __init__(self, received=None):
        self.callback = None
        self.acknowledged = []
        self._received = received or []

    def subscribe(self, subscription, callback=None):
        self.callback = callback
        return _FakeFuture()

    def pull(self, subscription=None, max_messages=None):
        return _FakePullResponse(self._received)

    def acknowledge(self, subscription=None, ack_ids=None):
        self.acknowledged.extend(ack_ids or [])


@pytest.fixture
def order():
    """Shared call-order log. `close` entries come from the patched
    `close_old_connections`, `process` from the patched sync."""
    return []


def _patches(order, *, process_side_effect=None):
    def _process(*_args, **_kwargs):
        order.append("process")
        if process_side_effect is not None:
            raise process_side_effect

    return (
        mock.patch(f"{COMMAND}.close_old_connections", side_effect=lambda: order.append("close")),
        mock.patch("capture.gmail_live.is_configured", return_value=True),
        mock.patch("capture.gmail_live.process_notification", side_effect=_process),
    )


def _run(order, subscriber, *, process_side_effect=None, once=False):
    close_p, configured_p, process_p = _patches(order, process_side_effect=process_side_effect)
    with close_p, configured_p, process_p, mock.patch(
        "google.cloud.pubsub_v1.SubscriberClient", return_value=subscriber
    ):
        if once:
            call_command("gmail_pubsub_listen", "--once")
            return None
        call_command("gmail_pubsub_listen")
        # `handle()` only REGISTERS the callback; the real Pub/Sub client is
        # what would invoke it. Drive it by hand, still inside the patches.
        message = _FakeMessage()
        subscriber.callback(message)
        return message


def test_streaming_callback_recycles_the_connection_around_each_notification(order):
    """The regression this file exists for. Without the
    `close_old_connections()` pair in `_process`, `order` is just
    `["process"]` — the worker syncs on whatever connection it opened at
    boot, with CONN_MAX_AGE and CONN_HEALTH_CHECKS both silently inert."""
    message = _run(order, _FakeSubscriber())

    assert order == ["close", "process", "close"]
    assert message.acked is True
    assert message.nacked is False


def test_a_failing_notification_still_releases_the_connection(order):
    """The `finally` half. A notification that raises is exactly the case
    where a broken connection is most likely to be the cause, so the worker
    must not hold onto it on the way out — otherwise the very first drop
    poisons every redelivery that follows."""
    message = _run(order, _FakeSubscriber(), process_side_effect=RuntimeError("boom"))

    assert order == ["close", "process", "close"]
    # Unchanged behaviour: one bad notification is nacked, not fatal.
    assert message.nacked is True
    assert message.acked is False


def test_once_mode_recycles_per_message_too(order):
    """`--once` shares `_process` with the streaming branch, so it gets the
    same bracketing — pinned so a future refactor that splits the two paths
    can't quietly drop it from one of them."""
    subscriber = _FakeSubscriber(received=[_FakeReceived(), _FakeReceived(ack_id="ack-2")])

    _run(order, subscriber, once=True)

    assert order == ["close", "process", "close", "close", "process", "close"]
    assert subscriber.acknowledged == ["ack-1", "ack-2"]


def test_nothing_runs_at_all_when_gmail_live_is_dark(order):
    """The unconfigured guard comes first — no subscriber client, and so no
    connection churn either."""
    with mock.patch(f"{COMMAND}.close_old_connections", side_effect=lambda: order.append("close")), \
            mock.patch("capture.gmail_live.is_configured", return_value=False):
        call_command("gmail_pubsub_listen")

    assert order == []
