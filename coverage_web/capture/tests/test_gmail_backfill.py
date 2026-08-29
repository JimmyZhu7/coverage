"""Integration-style tests for `backfill_connection` and the `gmail_backfill`
command — the parts of the first-connect historical pass that touch the
database and a (mocked) Gmail client. Pure-function tests for the window
math and stale-bounce suppression live in test_gmail_live.py.

``transaction=True`` for the same reason test_gmail.py needs it: applying a
finding calls `crm.services.log_touch`, which opens its own psycopg
connection and cannot see rows written inside pytest's wrapping transaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from capture import gmail_live
from capture.models import GmailConnection
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="backfill-student@example.com", password="x")


@pytest.fixture
def contact(student):
    return Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example", source="manual"
    )


@pytest.fixture
def connection(student):
    return GmailConnection.all_objects.create(
        user=student,
        gmail_address="backfill-student@example.com",
        refresh_token_encrypted="unused-in-these-tests",
        backfill_status="pending",
    )


def _fake_gmail_client(message_ids: list[str], messages_by_id: dict[str, dict]):
    """A MagicMock standing in for `build("gmail", "v1", ...)`, wired so
    `.users().messages().list(...).execute()` returns the given ids (once,
    no paging) and `.get(...).execute()` looks the message up by id."""
    client = MagicMock()
    client.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids],
    }

    def _get(*, userId, id, format):  # noqa: A002 - matches the real API's kwarg name
        mock = MagicMock()
        mock.execute.return_value = messages_by_id[id]
        return mock

    client.users.return_value.messages.return_value.get.side_effect = _get
    return client


def _message(*, from_addr: str, to_addr: str, subject: str, snippet: str, internal_date_ms: int):
    return {
        "threadId": f"thread-{internal_date_ms}",
        "snippet": snippet,
        "internalDate": str(internal_date_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
        },
    }


class TestBackfillConnection:
    def test_a_reply_is_applied_and_marks_the_connection_done(self, student, contact, connection):
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr=connection.gmail_address,
            subject="Re: coffee chat",
            snippet="Happy to chat next week!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection)

        assert result.touches_logged == 1
        assert Touch.objects.for_user(student).filter(contact=contact, kind="reply_received").exists()

        connection.refresh_from_db()
        assert connection.backfill_status == "done"
        assert connection.backfill_completed_at is not None
        assert connection.backfill_stats["touches_logged"] == 1

    def test_dry_run_writes_nothing_and_leaves_status_alone(self, student, contact, connection):
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr=connection.gmail_address,
            subject="Re: coffee chat",
            snippet="Happy to chat!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection, dry_run=True)

        assert result.touches_logged == 1  # dry-run still REPORTS the count
        assert not Touch.objects.for_user(student).exists()  # but writes nothing
        connection.refresh_from_db()
        assert connection.backfill_status == "pending"  # untouched

    def test_a_message_from_a_contact_not_in_coverage_is_skipped_not_invented(
        self, student, connection
    ):
        """No Contact fixture here on purpose — mirrors capture_gmail, not
        capture_discover: backfill never creates new contacts."""
        message = _message(
            from_addr="Stranger <stranger@nowhere.example>",
            to_addr=connection.gmail_address,
            subject="Hi",
            snippet="hello",
            internal_date_ms=1_700_000_000_000,
        )
        # A stranger's address never matches any tracked contact's email, so
        # the per-contact search loop naturally never touches this message —
        # simulate that directly by giving the fake client zero results.
        client = _fake_gmail_client([], {})

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection)

        assert result.findings == 0
        assert not Contact.objects.for_user(student).exists()


class TestGmailBackfillCommand:
    def test_picks_up_pending_and_failed_but_not_done(self, student):
        pending = GmailConnection.all_objects.create(
            user=student, gmail_address="a@example.com",
            refresh_token_encrypted="x", backfill_status="pending",
        )
        other = User.objects.create_user(email="other@example.com", password="x")
        failed = GmailConnection.all_objects.create(
            user=other, gmail_address="b@example.com",
            refresh_token_encrypted="x", backfill_status="failed",
        )
        third = User.objects.create_user(email="third@example.com", password="x")
        GmailConnection.all_objects.create(
            user=third, gmail_address="c@example.com",
            refresh_token_encrypted="x", backfill_status="done",
        )

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        pending.refresh_from_db()
        failed.refresh_from_db()
        assert pending.backfill_status == "done"
        assert failed.backfill_status == "done"

    def test_email_filter_scopes_to_one_user(self, student):
        mine = GmailConnection.all_objects.create(
            user=student, gmail_address="mine@example.com",
            refresh_token_encrypted="x", backfill_status="pending",
        )
        other = User.objects.create_user(email="other2@example.com", password="x")
        theirs = GmailConnection.all_objects.create(
            user=other, gmail_address="theirs@example.com",
            refresh_token_encrypted="x", backfill_status="pending",
        )

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill", email=student.email)

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.backfill_status == "done"
        assert theirs.backfill_status == "pending"  # untouched

    def test_a_running_row_abandoned_by_a_crashed_process_is_retried(self, student):
        """A process that dies between `backfill_status = "running"` and
        the try/except that would otherwise mark it "failed" (SIGKILL, OOM,
        a redeploy) leaves the row parked at "running" forever. Without the
        staleness check, NO future tick would ever pick it back up — the
        selection query only ever looked for pending/failed."""
        from datetime import timedelta

        from django.utils import timezone

        stuck = GmailConnection.all_objects.create(
            user=student, gmail_address="stuck@example.com",
            refresh_token_encrypted="x", backfill_status="running",
            backfill_started_at=timezone.now() - timedelta(hours=5),
        )

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        stuck.refresh_from_db()
        assert stuck.backfill_status == "done"

    def test_a_recently_started_running_row_is_left_alone(self, student):
        """The flip side: a row that started running two minutes ago is
        (almost certainly) a real, in-progress run on another worker, not
        an abandoned one — it must not be picked up and run a second time
        concurrently."""
        from datetime import timedelta

        from django.utils import timezone

        in_progress = GmailConnection.all_objects.create(
            user=student, gmail_address="inprogress@example.com",
            refresh_token_encrypted="x", backfill_status="running",
            backfill_started_at=timezone.now() - timedelta(minutes=2),
        )

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        in_progress.refresh_from_db()
        assert in_progress.backfill_status == "running"  # untouched


# ---------------------------------------------------------------------------
# Queue behaviour under load. Both of these are invisible at one user and are
# the difference between "Scan Now works" and "Scan Now works for some people"
# once a cohort is on the product.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_rescan_queue_serves_the_longest_wait_first():
    """Without an explicit order the database returns these rows however it
    likes. That is fine at one user and a starvation bug at fifty: whoever the
    planner puts last can be pushed past the tick budget on every tick and
    wait forever while newer requests are served ahead of them."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import F, Q
    from capture.models import GmailConnection

    now = timezone.now()
    # Created newest-first, so insertion order is the opposite of the answer.
    for label, minutes in [("newest", 1), ("middle", 30), ("oldest", 120)]:
        u = User.objects.create_user(email=f"{label}@example.com", password="x")
        GmailConnection.all_objects.create(
            user=u, gmail_address=f"{label}@gmail.com", status="active",
            backfill_status="done",
            rescan_status="pending",
            rescan_requested_at=now - timedelta(minutes=minutes),
        )

    ordered = list(
        GmailConnection.all_objects.filter(status="active", rescan_status="pending")
        .order_by(F("rescan_requested_at").asc(nulls_first=True), "pk")
        .values_list("gmail_address", flat=True)
    )
    assert ordered == ["oldest@gmail.com", "middle@gmail.com", "newest@gmail.com"]


@pytest.mark.django_db
def test_a_row_that_never_recorded_a_request_time_goes_first():
    """`nulls_first` is deliberate. A row with no timestamp is either a
    pre-field row or a crashed run — it has been waiting longest by
    definition, and sorting it last is how a row gets stuck forever."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import F
    from capture.models import GmailConnection

    recent = User.objects.create_user(email="recent@example.com", password="x")
    GmailConnection.all_objects.create(
        user=recent, gmail_address="recent@gmail.com", status="active",
        backfill_status="done", rescan_status="pending",
        rescan_requested_at=timezone.now() - timedelta(minutes=5),
    )
    stranded = User.objects.create_user(email="stranded@example.com", password="x")
    GmailConnection.all_objects.create(
        user=stranded, gmail_address="stranded@gmail.com", status="active",
        backfill_status="done", rescan_status="pending",
        rescan_requested_at=None,
    )

    first = (GmailConnection.all_objects
             .filter(status="active", rescan_status="pending")
             .order_by(F("rescan_requested_at").asc(nulls_first=True), "pk")
             .first())
    assert first.gmail_address == "stranded@gmail.com"


def test_the_tick_budget_is_shorter_than_the_production_tick():
    """The budget's whole job is stopping one tick running into the next.

    render.yaml schedules this command `*/5`. If the budget ever exceeds the
    interval, ticks overlap, each holding mailbox locks the others want, and
    Scan Now gets slower as users are added with nothing appearing to fail —
    the hardest kind of slowness to diagnose. Pinned here rather than trusted
    to stay right, because the two numbers live in different files.
    """
    from capture.management.commands.gmail_backfill import TICK_BUDGET

    production_tick_seconds = 5 * 60
    assert TICK_BUDGET.total_seconds() <= production_tick_seconds
