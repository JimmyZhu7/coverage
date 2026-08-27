"""`gmail_live.sync_connection` against the history stream Gmail actually
sends — draft-autosave churn and already-deleted message ids.

Measured on the founder's live mailbox (2026-08-27, read-only): 14 of 20
sampled `messagesAdded` records were DRAFT autosaves, and 4 of 40 sampled
ids returned 404 on fetch (each autosave permanently deletes its
predecessor). Two defects followed, both regression-pinned here:

1. THE WEDGE. The per-message `messages.get` loop had no 404 handling, so
   the first deleted id raised out of `sync_connection` BEFORE the cursor
   was saved. Every later pass re-listed the same window and died on the
   same id — total silent sync loss until Gmail's ~7-day retention expired
   the cursor and the re-anchor skipped the whole gap.
2. THE PHANTOM OUTREACH. Nothing filtered `labelIds`, so a draft fetched
   inside the poll window (From: the user, To: a banker) classified as an
   outbound finding — an `outreach` touch for an email never sent, and the
   outbound discovery arm minting a "You wrote to them" proposal off a
   composition the user may yet abandon. SPAM and TRASH classified the
   same way, which let a spoofed firm-domain sender reach the proposal
   ladder.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from googleapiclient.errors import HttpError

from capture import gmail_live
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db

User = get_user_model()

OWN = "sync-student@example.com"


def _connection(history_id: str = "1000") -> GmailConnection:
    user = User.objects.create_user(email=OWN, password="x", plan="pro")
    return GmailConnection.all_objects.create(
        user=user,
        gmail_address=OWN,
        refresh_token_encrypted="unused",
        history_id=history_id,
        status="active",
    )


def _history_response(records, latest="2000"):
    return {"history": records, "historyId": latest}


def _added(message_id, label_ids=None):
    message = {"id": message_id}
    if label_ids is not None:
        message["labelIds"] = label_ids
    return {"messagesAdded": [{"message": message}]}


def _full_message(message_id, *, from_addr, to_addr, label_ids):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "snippet": "hello",
        "internalDate": "1756200000000",
        "labelIds": label_ids,
        "payload": {"headers": [
            {"name": "From", "value": from_addr},
            {"name": "To", "value": to_addr},
            {"name": "Subject", "value": "USC | Coffee chat"},
        ]},
    }


def _client(history_records, messages_by_id):
    """A fake Gmail client: one history page, then `messages.get` served
    from `messages_by_id` (an HttpError value is raised instead)."""
    client = MagicMock()
    client.users.return_value.history.return_value.list.return_value.execute.return_value = (
        _history_response(history_records)
    )

    def _get(userId, id, format):  # noqa: A002 - Gmail's own parameter name
        request = MagicMock()
        value = messages_by_id[id]
        if isinstance(value, Exception):
            request.execute.side_effect = value
        else:
            request.execute.return_value = value
        return request

    client.users.return_value.messages.return_value.get.side_effect = _get
    return client


def _404() -> HttpError:
    response = MagicMock()
    response.status = 404
    return HttpError(response, b"not found")


def test_a_deleted_message_skips_instead_of_wedging_the_cursor():
    connection = _connection()
    real = _full_message(
        "real", from_addr="Alice <alice@firm.example>", to_addr=OWN,
        label_ids=["INBOX"],
    )
    client = _client(
        [_added("gone"), _added("real")],
        {"gone": _404(), "real": real},
    )
    with patch.object(gmail_live, "_gmail_client", return_value=client), \
            patch.object(gmail_live, "apply_findings") as applied:
        gmail_live.sync_connection(connection)

    # The good message still landed, and the cursor advanced past the gap —
    # the next pass starts AFTER the deleted id instead of dying on it again.
    (_, findings), _ = applied.call_args
    assert [f["email"] for f in findings] == ["alice@firm.example"]
    connection.refresh_from_db()
    assert connection.history_id == "2000"
    assert connection.last_notification_at is not None


def test_draft_history_records_are_never_fetched_or_classified():
    connection = _connection()
    real = _full_message(
        "real", from_addr="Alice <alice@firm.example>", to_addr=OWN,
        label_ids=["INBOX"],
    )
    client = _client(
        [_added("draft-autosave", ["DRAFT"]), _added("real", ["INBOX"])],
        {"real": real},  # fetching the draft id would KeyError — it must not be fetched
    )
    with patch.object(gmail_live, "_gmail_client", return_value=client), \
            patch.object(gmail_live, "apply_findings") as applied:
        gmail_live.sync_connection(connection)

    (_, findings), _ = applied.call_args
    assert [f["email"] for f in findings] == ["alice@firm.example"]


@pytest.mark.parametrize("label", ["DRAFT", "SPAM", "TRASH"])
def test_excluded_labels_on_the_fetched_message_are_skipped(label):
    """The history record may carry no labels at all — the fetched message's
    own labels are the truth, and a draft/spam/trash message classifies as
    nothing rather than as outreach or a reply."""
    connection = _connection()
    excluded = _full_message(
        "x", from_addr=f"Jimmy <{OWN}>", to_addr="banker@firm.example",
        label_ids=[label],
    )
    client = _client([_added("x")], {"x": excluded})
    with patch.object(gmail_live, "_gmail_client", return_value=client), \
            patch.object(gmail_live, "apply_findings") as applied:
        gmail_live.sync_connection(connection)

    assert applied.call_count == 0
    connection.refresh_from_db()
    assert connection.history_id == "2000"


def test_sync_connection_returns_the_report_it_used_to_discard():
    """The SyncResult is the only carrier of the pipeline's honesty valves
    (unresolved application mail, ambiguous names, surfaced mail facts) —
    `sync_connection` used to call `apply_findings` and throw it away, so
    on the every-two-minutes path those lines went nowhere."""
    connection = _connection()
    real = _full_message(
        "real", from_addr="Alice <alice@firm.example>", to_addr=OWN,
        label_ids=["INBOX"],
    )
    client = _client([_added("real", ["INBOX"])], {"real": real})
    sentinel = MagicMock()
    with patch.object(gmail_live, "_gmail_client", return_value=client), \
            patch.object(gmail_live, "apply_findings", return_value=sentinel):
        assert gmail_live.sync_connection(connection) is sentinel

    # And a pass with nothing new returns None rather than a fake report.
    empty_client = _client([], {})
    with patch.object(gmail_live, "_gmail_client", return_value=empty_client):
        assert gmail_live.sync_connection(connection) is None
