"""Phase 2: the user-triggered "Scan Now" rescan — `capture.views.gmail_rescan`
(queues it), `gmail_live.run_rescan` (the deterministic pass + Phase 3 residue
stage composed together), and the `gmail_backfill` command's rescan
selection (separate from its original backfill selection).

``transaction=True`` for the same reason `test_gmail_backfill.py` needs it:
applying a finding calls `crm.services.log_touch`, which opens its own
psycopg connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from billing import credits as billing_credits
from billing.models import CreditLedger
from capture import gmail_live, gmail_residue
from capture.models import GmailConnection
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="rescan-student@example.com", password="x")


@pytest.fixture
def connection(student):
    return GmailConnection.all_objects.create(
        user=student,
        gmail_address="rescan-student@example.com",
        refresh_token_encrypted="unused-in-these-tests",
        status="active",
        backfill_status="done",
    )


def _fake_gmail_client(message_ids: list[str], messages_by_id: dict[str, dict]):
    client = MagicMock()
    client.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids],
    }

    def _get(*, userId, id, format):  # noqa: A002
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


# ---------------------------------------------------------------------------
# The "Scan Now" POST endpoint
# ---------------------------------------------------------------------------
class TestGmailRescanView:
    def test_queues_a_rescan_for_a_connected_user(self, client, student, connection):
        client.force_login(student)
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code == 302
        connection.refresh_from_db()
        assert connection.rescan_status == "pending"
        assert connection.rescan_requested_at is not None

    def test_refuses_a_second_rescan_while_one_is_in_flight(self, client, student, connection):
        connection.rescan_status = "running"
        connection.save(update_fields=["rescan_status"])
        client.force_login(student)
        client.post(reverse("capture:gmail_rescan"))
        connection.refresh_from_db()
        assert connection.rescan_status == "running"  # unchanged, not re-queued

    def test_requires_a_connection_to_exist(self, client, student):
        client.force_login(student)
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code == 302
        assert not GmailConnection.all_objects.filter(user=student).exists()

    def test_requires_login(self, client):
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code in (302, 401, 403)

    def test_get_is_not_allowed(self, client, student, connection):
        client.force_login(student)
        resp = client.get(reverse("capture:gmail_rescan"))
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# gmail_live.run_rescan — the deterministic + residue composition
# ---------------------------------------------------------------------------
class TestRunRescan:
    def test_run_rescan_scans_all_contacts_and_never_touches_backfill_status(self, student, connection):
        Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr=connection.gmail_address,
            subject="Re: chat",
            snippet="Happy to chat!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            stats = gmail_live.run_rescan(connection)

        assert stats["touches_logged"] == 1
        assert "residue" in stats
        connection.refresh_from_db()
        assert connection.backfill_status == "done"  # untouched by run_rescan itself

    def _residue_messages(self, connection, count: int, *, base_ms: int = 1_700_000_000_000):
        """`count` bounce-shaped messages, each `_classify_message` returns
        None for (unresolvable sender) — the deterministic pass's own
        residue, one per distinct thread."""
        message_ids = []
        messages_by_id = {}
        for i in range(count):
            ms = base_ms + i
            mid = f"residue-m{i}"
            message_ids.append(mid)
            messages_by_id[mid] = {
                "threadId": f"thread-{ms}",
                "snippet": "delivery failed, no address found in this text",
                "internalDate": str(ms),
                "payload": {
                    "headers": [
                        {"name": "From", "value": "mailer-daemon@mail.example"},
                        {"name": "To", "value": connection.gmail_address},
                        {"name": "Subject", "value": "Delivery Status Notification"},
                    ],
                },
            }
        return message_ids, messages_by_id

    @override_settings(ANTHROPIC_API_KEY="sk-test-key", CREDIT_RESCAN_THREADS_PER_CREDIT=1)
    def test_the_residue_stage_is_truncated_to_the_affordable_thread_budget(self, student, connection):
        """docs/credit-system-plan.md's enforcement point 2: the residue
        stage never sends more threads to Haiku than the ledger can pay
        for, even though 5 candidates are on offer and the hard
        MAX_RESIDUE_THREADS cap would allow all of them."""
        Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        # Free's default grant is 60; spend it down to exactly 2, and with
        # 1 thread/credit (this test's override) that's 2 affordable out of
        # 5 candidates.
        CreditLedger.all_objects.create(user=student, delta=-58, kind=CreditLedger.KIND_ADJUST)
        assert billing_credits.balance(student) == 2

        message_ids, messages_by_id = self._residue_messages(connection, 5)
        client = _fake_gmail_client(message_ids, messages_by_id)

        calls = []

        def _fake_post_json(payload, *, timeout, retries):
            calls.append(payload)
            return {"content": [{"type": "text", "text": '{"outcome": "ambiguous", "quote": null}'}]}

        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch.object(gmail_residue, "_post_json", side_effect=_fake_post_json):
            stats = gmail_live.run_rescan(connection)

        residue = stats["residue"]
        assert residue["residue_threads_seen"] == 5
        assert residue["residue_threads_processed"] == 2
        assert len(calls) == 2
        # Honest partial labeling (docs/credit-system-plan.md §6): fewer
        # threads processed than seen must say so, not report a clean
        # number that hides the truncation.
        assert residue["credit_limited"] is True

        # Debited for exactly what ran, not what was offered.
        assert billing_credits.balance(student) == 0
        row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_SPEND_RESCAN)
        assert row.delta == -2
        assert row.props == {"threads": 2}

    @override_settings(ANTHROPIC_API_KEY="sk-test-key")
    def test_zero_affordable_credits_still_runs_the_free_deterministic_pass(self, student, connection):
        """A student who has burned every credit still gets the free
        deterministic pass in full — only the metered residue stage is
        skipped, and it says so honestly rather than silently under-
        reporting."""
        contact = Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        CreditLedger.all_objects.create(user=student, delta=-60, kind=CreditLedger.KIND_ADJUST)
        assert billing_credits.balance(student) == 0

        genuine_reply = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr=connection.gmail_address,
            subject="Re: chat",
            snippet="Happy to chat!",
            internal_date_ms=1_700_000_000_000,
        )
        residue_ids, residue_by_id = self._residue_messages(connection, 3, base_ms=1_800_000_000_000)
        all_ids = ["m-genuine"] + residue_ids
        all_by_id = {"m-genuine": genuine_reply, **residue_by_id}
        client = _fake_gmail_client(all_ids, all_by_id)

        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch.object(gmail_residue, "_post_json") as mocked_post:
            stats = gmail_live.run_rescan(connection)

        # The free deterministic pass ran in full and logged the genuine reply.
        assert stats["touches_logged"] == 1
        assert Touch.objects.for_user(student).filter(contact=contact).exists()

        residue = stats["residue"]
        assert residue["residue_threads_seen"] == 3
        assert residue["residue_threads_processed"] == 0
        assert residue["credit_limited"] is True
        mocked_post.assert_not_called()  # not one metered API call was made

        # No debit for a stage that never ran.
        assert billing_credits.balance(student) == 0
        assert not CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_SPEND_RESCAN).exists()

    def test_dry_run_makes_no_ai_call_even_if_configured(self, student, connection, settings):
        settings.ANTHROPIC_API_KEY = "sk-test-key"
        Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        # A message from an unresolvable stranger becomes residue (no from_addr
        # match issue here — use a bounce-shaped message with no findable
        # recipient, which `_classify_message` returns None for).
        message = {
            "threadId": "thread-residue-1",
            "snippet": "delivery failed, no address found in this text",
            "internalDate": "1700000000000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "mailer-daemon@mail.example"},
                    {"name": "To", "value": connection.gmail_address},
                    {"name": "Subject", "value": "Delivery Status Notification"},
                ],
            },
        }
        client = _fake_gmail_client(["m1"], {"m1": message})

        called = []
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch.object(gmail_residue, "_post_json", side_effect=lambda *a, **kw: called.append(1)):
            stats = gmail_live.run_rescan(connection, dry_run=True)

        assert called == []  # no AI call made under dry-run
        assert stats["residue"]["residue_threads_processed"] == 0


# ---------------------------------------------------------------------------
# The gmail_backfill command's SEPARATE rescan selection
# ---------------------------------------------------------------------------
class TestGmailBackfillCommandRescanSelection:
    def test_picks_up_pending_rescans_independently_of_backfill_status(self, student, connection):
        connection.backfill_status = "done"  # already done — must stay done
        connection.rescan_status = "pending"
        connection.save(update_fields=["backfill_status", "rescan_status"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.rescan_status == "done"
        assert connection.rescan_completed_at is not None
        assert connection.backfill_status == "done"  # untouched, still sticky

    def test_a_connection_only_needing_backfill_does_not_get_a_rescan_status_change(self, student, connection):
        connection.backfill_status = "pending"
        connection.rescan_status = "none"
        connection.save(update_fields=["backfill_status", "rescan_status"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.backfill_status == "done"
        assert connection.rescan_status == "none"  # never touched — wasn't queued

    def test_a_running_rescan_abandoned_by_a_crashed_process_is_retried(self, student, connection):
        """Same failure mode as the backfill side (see
        test_gmail_backfill.py): a process killed mid-rescan leaves
        `rescan_status="running"` with nothing left to finish it, and
        without a staleness check that row is invisible to every future
        tick — permanently disabling the user's own "Scan Now" button,
        since the view refuses to queue a second rescan while one reads
        pending/running."""
        from datetime import timedelta

        from django.utils import timezone

        connection.rescan_status = "running"
        connection.rescan_started_at = timezone.now() - timedelta(hours=5)
        connection.save(update_fields=["rescan_status", "rescan_started_at"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.rescan_status == "done"

    def test_a_recently_started_running_rescan_is_left_alone(self, student, connection):
        from datetime import timedelta

        from django.utils import timezone

        connection.rescan_status = "running"
        connection.rescan_started_at = timezone.now() - timedelta(minutes=2)
        connection.save(update_fields=["rescan_status", "rescan_started_at"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.rescan_status == "running"  # untouched

    def test_a_slow_to_start_but_still_running_rescan_is_not_double_picked_up(
        self, student, connection
    ):
        """Regression test: staleness used to be measured from
        `rescan_requested_at` (when the user clicked Scan Now) instead of
        `rescan_started_at` (when a tick actually began running it). A
        rescan can sit `pending` for a while before a tick claims it, so a
        request old enough to cross STALE_RUNNING_AFTER but a run that only
        just started must NOT be reclaimed — reclaiming it would fire a
        second `run_rescan` on the same mailbox while the first is still
        genuinely in flight, double-charging `spend_rescan` and double-
        logging touches."""
        from datetime import timedelta

        from django.utils import timezone

        connection.rescan_status = "running"
        connection.rescan_requested_at = timezone.now() - timedelta(hours=5)
        connection.rescan_started_at = timezone.now() - timedelta(minutes=2)
        connection.save(
            update_fields=["rescan_status", "rescan_requested_at", "rescan_started_at"]
        )

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.rescan_status == "running"  # untouched, not re-run
