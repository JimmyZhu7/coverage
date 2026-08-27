"""`gmail_poll` — the scheduler around `gmail_live.sync_connection`.

What is under test here is scheduling and error policy, NOT syncing:
`sync_connection` is patched in almost every test, because the thing this
command promises is "every active mailbox gets exactly one call, one
mailbox failing does not stop the rest, and a dry run writes nothing."
The sync itself already has its own tests (test_gmail_backfill.py,
test_gmail_live.py).

Two exceptions run the real `apply_findings` path, because the claim they
check ("overlapping runs do not double-log") is a claim about the ratchet
inside it and cannot be tested with the sync mocked out.

``transaction=True`` for the usual reason (see test_gmail_backfill.py):
`crm.services.log_touch` opens its own psycopg connection and cannot see
rows written inside pytest's wrapping transaction. It is also what lets the
`pg_try_advisory_lock` test hold a real second session's lock.
"""

from __future__ import annotations

import signal

import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connections
from google.auth.exceptions import RefreshError

from capture import gmail_live
from capture.management.commands import gmail_poll
from capture.models import GmailConnection
from crm.models import Contact, Touch
from ops.models import JobRun

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


def _user(email: str, *, plan: str = "pro"):
    return User.objects.create_user(email=email, password="x", plan=plan)


def _connection(user, *, status: str = "active", history_id: str = "1000"):
    return GmailConnection.all_objects.create(
        user=user,
        gmail_address=user.email,
        refresh_token_encrypted="unused-in-these-tests",
        history_id=history_id,
        status=status,
    )


def _run(*args, **kwargs) -> str:
    """Run the command with `is_configured()` forced on, returning stdout."""
    out, err = StringIO(), StringIO()
    with patch.object(gmail_live, "is_configured", return_value=True):
        call_command("gmail_poll", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


# ---------------------------------------------------------------------------
# A single pass syncs each active connection exactly once
# ---------------------------------------------------------------------------

class TestSinglePass:
    def test_every_active_connection_is_synced_once(self):
        first = _connection(_user("poll-a@example.com"))
        second = _connection(_user("poll-b@example.com"))

        with patch.object(gmail_live, "sync_connection") as sync:
            output = _run("--spacing", "0")

        synced = [call.args[0].pk for call in sync.call_args_list]
        assert sorted(synced) == sorted([first.pk, second.pk])
        assert len(synced) == 2, "each mailbox must be synced exactly once per pass"
        assert "2 synced, 0 skipped, 0 failed" in output

    def test_the_command_exits_rather_than_looping_without_an_interval(self):
        _connection(_user("poll-once@example.com"))
        with patch.object(gmail_live, "sync_connection"):
            # No timeout machinery needed: if this ever started looping, the
            # test would hang, which is the failure signal.
            assert "1 synced" in _run("--spacing", "0")

    def test_email_narrows_the_pass_to_one_mailbox(self):
        wanted = _connection(_user("poll-wanted@example.com"))
        _connection(_user("poll-other@example.com"))

        with patch.object(gmail_live, "sync_connection") as sync:
            _run("--email", "poll-wanted@example.com", "--spacing", "0")

        assert [call.args[0].pk for call in sync.call_args_list] == [wanted.pk]

    def test_a_single_pass_records_a_job_run(self):
        _connection(_user("poll-jobrun@example.com"))
        with patch.object(gmail_live, "sync_connection"):
            _run("--spacing", "0")
        run = JobRun.objects.filter(name="gmail-poll").latest("started_at")
        assert run.status == JobRun.STATUS_SUCCESS

    def test_an_unconfigured_deploy_polls_nothing(self):
        _connection(_user("poll-unconfigured@example.com"))
        out = StringIO()
        with patch.object(gmail_live, "is_configured", return_value=False), \
                patch.object(gmail_live, "sync_connection") as sync:
            call_command("gmail_poll", stdout=out)
        sync.assert_not_called()
        assert "not configured" in out.getvalue()


# ---------------------------------------------------------------------------
# Selection: revoked and non-Pro rows are skipped, never fatal
# ---------------------------------------------------------------------------

class TestSelection:
    def test_a_revoked_connection_is_skipped_not_fatal(self):
        """Matches `renew_watches` / `gmail_backfill`: both filter
        `status="active"`, so a revoked row is simply never picked up."""
        _connection(_user("poll-revoked@example.com"), status="revoked")
        healthy = _connection(_user("poll-healthy@example.com"))

        with patch.object(gmail_live, "sync_connection") as sync:
            output = _run("--spacing", "0")

        assert [call.args[0].pk for call in sync.call_args_list] == [healthy.pk]
        assert "1 synced, 0 skipped, 0 failed" in output

    def test_a_pass_of_only_revoked_connections_is_a_clean_no_op(self):
        _connection(_user("poll-only-revoked@example.com"), status="revoked")
        with patch.object(gmail_live, "sync_connection") as sync:
            output = _run()
        sync.assert_not_called()
        assert "No connected mailboxes" in output

    def test_a_free_plan_connection_is_not_polled(self):
        """Real-time sync is the Pro line — `renew_watches` and
        `process_notification` both gate on it, and a poller that did not
        would hand it back through a different door."""
        _connection(_user("poll-free@example.com", plan="free"))
        with patch.object(gmail_live, "sync_connection") as sync:
            _run()
        sync.assert_not_called()


# ---------------------------------------------------------------------------
# One mailbox failing must not abort the rest
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_one_mailbox_raising_does_not_stop_the_others(self):
        first = _connection(_user("poll-boom@example.com"))
        second = _connection(_user("poll-after-boom@example.com"))
        third = _connection(_user("poll-after-boom-2@example.com"))

        def _sync(conn):
            if conn.pk == first.pk:
                raise RuntimeError("Gmail said no")

        with patch.object(gmail_live, "sync_connection", side_effect=_sync) as sync:
            output = _run("--spacing", "0")

        assert sorted(c.args[0].pk for c in sync.call_args_list) == sorted(
            [first.pk, second.pk, third.pk]
        )
        assert "2 synced, 0 skipped, 1 failed" in output
        assert "Gmail said no" in output

    def test_a_revoked_grant_marks_the_row_and_keeps_going(self):
        dead = _connection(_user("poll-invalid-grant@example.com"))
        alive = _connection(_user("poll-still-good@example.com"))

        def _sync(conn):
            if conn.pk == dead.pk:
                raise RefreshError(
                    "invalid_grant: Token has been expired or revoked.",
                    {"error": "invalid_grant"},
                )

        with patch.object(gmail_live, "sync_connection", side_effect=_sync):
            output = _run("--spacing", "0")

        dead.refresh_from_db()
        alive.refresh_from_db()
        assert dead.status == "revoked"
        assert alive.status == "active", "one dead grant must not touch anyone else"
        assert "1 synced, 1 skipped, 0 failed" in output

    def test_a_transient_token_error_does_not_mark_the_row_revoked(self):
        """google-auth raises the same exception type for a token-endpoint
        5xx. Marking a working connection `revoked` over that would put a
        "needs reconnect" banner in front of a user with nothing to fix."""
        connection = _connection(_user("poll-transient@example.com"))

        with patch.object(
            gmail_live, "sync_connection",
            side_effect=RefreshError("500 Internal Server Error"),
        ):
            output = _run()

        connection.refresh_from_db()
        assert connection.status == "active"
        assert "0 synced, 0 skipped, 1 failed" in output


# ---------------------------------------------------------------------------
# --dry-run writes nothing
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_never_calls_sync_and_writes_nothing(self):
        connection = _connection(_user("poll-dry@example.com"), history_id="500")
        before = (
            connection.history_id,
            connection.last_notification_at,
            connection.status,
        )

        preview = {
            "reanchor": False,
            "message_ids": ["m1", "m2", "m3"],
            "latest_history_id": "777",
        }
        with patch.object(gmail_live, "sync_connection") as sync, \
                patch.object(gmail_live, "preview_sync", return_value=preview):
            output = _run("--dry-run")

        sync.assert_not_called()
        connection.refresh_from_db()
        assert (
            connection.history_id,
            connection.last_notification_at,
            connection.status,
        ) == before
        assert not JobRun.objects.filter(name="gmail-poll").exists(), (
            "a dry run must not record a successful run of work it did not do"
        )
        assert "3 new message(s)" in output
        assert "would advance to 777" in output

    def test_dry_run_reports_an_expired_history_window_rather_than_re_anchoring(self):
        connection = _connection(_user("poll-dry-404@example.com"), history_id="9")
        preview = {"reanchor": True, "message_ids": [], "latest_history_id": None}

        with patch.object(gmail_live, "preview_sync", return_value=preview):
            output = _run("--dry-run")

        connection.refresh_from_db()
        assert connection.history_id == "9", "re-anchoring is a write"
        assert "re-anchor" in output

    def test_dry_run_does_not_mark_a_dead_grant_revoked(self):
        connection = _connection(_user("poll-dry-revoked@example.com"))
        with patch.object(
            gmail_live, "preview_sync",
            side_effect=RefreshError("invalid_grant: Token has been revoked."),
        ):
            output = _run("--dry-run")

        connection.refresh_from_db()
        assert connection.status == "active"
        assert "needs the user to reconnect" in output


# ---------------------------------------------------------------------------
# preview_sync itself — the read-only half of a sync
# ---------------------------------------------------------------------------

class TestPreviewSync:
    def test_it_lists_pending_messages_without_writing(self):
        connection = _connection(_user("preview@example.com"), history_id="100")
        client = MagicMock()
        client.users.return_value.history.return_value.list.return_value.execute.return_value = {
            "history": [{"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}],
            "historyId": "140",
        }
        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.preview_sync(connection)

        assert result == {
            "reanchor": False,
            "message_ids": ["a", "b"],
            "latest_history_id": "140",
        }
        connection.refresh_from_db()
        assert connection.history_id == "100"
        assert connection.last_notification_at is None
        client.users.return_value.messages.assert_not_called()

    def test_a_404_reports_a_re_anchor_instead_of_performing_one(self):
        from googleapiclient.errors import HttpError

        connection = _connection(_user("preview-404@example.com"), history_id="1")
        client = MagicMock()
        response = MagicMock()
        response.status = 404
        client.users.return_value.history.return_value.list.return_value.execute.side_effect = (
            HttpError(response, b"gone")
        )
        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.preview_sync(connection)

        assert result["reanchor"] is True
        connection.refresh_from_db()
        assert connection.history_id == "1"


# ---------------------------------------------------------------------------
# The loop: honours --interval, exits cleanly on SIGINT
# ---------------------------------------------------------------------------

class TestLoop:
    def test_the_loop_runs_repeatedly_and_stops_on_sigint(self):
        _connection(_user("poll-loop@example.com"))
        passes: list[float] = []

        def _sync(_conn):
            passes.append(time.monotonic())
            if len(passes) == 3:
                # Stand-in for the operator's Ctrl-C, delivered to this
                # process so the REAL handler the command installed is what
                # stops the loop.
                signal.raise_signal(signal.SIGINT)

        started = time.monotonic()
        with patch.object(gmail_live, "sync_connection", side_effect=_sync):
            output = _run("--interval", "0.05")
        elapsed = time.monotonic() - started

        assert len(passes) == 3, "the loop must keep running until signalled"
        assert output.endswith("stopped.\n"), "SIGINT must exit cleanly, not traceback"
        # Three passes at 0.05s apart cannot finish in less than two gaps.
        assert elapsed >= 0.09, f"the loop ignored --interval (finished in {elapsed:.3f}s)"

    def test_the_interval_is_honoured_between_passes(self):
        _connection(_user("poll-interval@example.com"))
        stamps: list[float] = []

        def _sync(_conn):
            stamps.append(time.monotonic())
            if len(stamps) == 2:
                signal.raise_signal(signal.SIGINT)

        with patch.object(gmail_live, "sync_connection", side_effect=_sync):
            _run("--interval", "0.2")

        assert stamps[1] - stamps[0] >= 0.18

    def test_sigterm_stops_the_loop_too(self):
        """A deployed worker is stopped with SIGTERM on every redeploy; the
        default action for it is immediate death, mid-sync."""
        _connection(_user("poll-sigterm@example.com"))
        calls: list[int] = []

        def _sync(_conn):
            calls.append(1)
            signal.raise_signal(signal.SIGTERM)

        with patch.object(gmail_live, "sync_connection", side_effect=_sync):
            output = _run("--interval", "0.05")

        assert len(calls) == 1
        assert "stopped." in output

    def test_the_previous_signal_handlers_are_restored(self):
        _connection(_user("poll-restore@example.com"))
        before = signal.getsignal(signal.SIGINT)

        def _sync(_conn):
            signal.raise_signal(signal.SIGINT)

        with patch.object(gmail_live, "sync_connection", side_effect=_sync):
            _run("--interval", "0.05")

        assert signal.getsignal(signal.SIGINT) is before

    def test_a_pass_level_failure_does_not_kill_the_loop(self):
        """Per-mailbox failures never reach the loop; a broken SELECT does.
        A poller that died on one database blip would stop syncing for
        everyone with nothing crashing loudly enough to notice."""
        _connection(_user("poll-pass-fail@example.com"))
        attempts: list[int] = []

        original = gmail_poll.Command._select

        def _select(self, email):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("database blinked")
            signal.raise_signal(signal.SIGINT)
            return original(self, email)

        with patch.object(gmail_poll.Command, "_select", _select), \
                patch.object(gmail_live, "sync_connection"):
            output = _run("--interval", "0.05")

        assert len(attempts) == 2, "the loop must survive a failed pass"
        assert "database blinked" in output
        assert "stopped." in output

    def test_a_non_positive_interval_is_rejected(self):
        with pytest.raises(CommandError):
            _run("--interval", "0")


# ---------------------------------------------------------------------------
# Overlap: two runs must not double-log
# ---------------------------------------------------------------------------

class TestOverlap:
    """The claim under test is the one in the command's docstring, rule 3 —
    including the half of it that is NOT true, so nobody has to re-derive it
    from scratch: `history_id` alone guards only the sequential case."""

    def _wire_gmail(self, message):
        client = MagicMock()
        client.users.return_value.history.return_value.list.return_value.execute.return_value = {
            "history": [{"messagesAdded": [{"message": {"id": "msg-1"}}]}],
            "historyId": "2000",
        }
        get = MagicMock()
        get.execute.return_value = message
        client.users.return_value.messages.return_value.get.return_value = get
        return client

    def _reply(self, own_email: str) -> dict:
        return {
            "threadId": "thread-overlap",
            "snippet": "Happy to chat next week!",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Jane Banker <jane@bank.example>"},
                    {"name": "To", "value": own_email},
                    {"name": "Subject", "value": "Re: coffee chat"},
                ],
            },
        }

    def test_a_second_sequential_run_finds_nothing_because_the_cursor_moved(self):
        """The half of the claim that IS true. This fake honours
        `startHistoryId` the way Gmail does, so the second run is asked for
        changes since 2000 and is handed an empty page — it never even
        fetches a message, let alone applies one."""
        user = _user("poll-overlap-seq@example.com")
        Contact.all_objects.create(
            user=user, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        connection = _connection(user, history_id="1000")

        client = MagicMock()

        def _list(*, userId, startHistoryId, historyTypes, pageToken):  # noqa: A002,N803
            page = MagicMock()
            if str(startHistoryId) == "1000":
                page.execute.return_value = {
                    "history": [{"messagesAdded": [{"message": {"id": "msg-1"}}]}],
                    "historyId": "2000",
                }
            else:
                page.execute.return_value = {"history": [], "historyId": startHistoryId}
            return page

        client.users.return_value.history.return_value.list.side_effect = _list
        get = MagicMock()
        get.execute.return_value = self._reply(user.email)
        client.users.return_value.messages.return_value.get.return_value = get

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            _run("--spacing", "0")
            after_first = Touch.objects.for_user(user).count()
            assert after_first, "the first run must actually log something"
            connection.refresh_from_db()
            assert connection.history_id == "2000", "the cursor must advance"

            fetches_after_first = client.users.return_value.messages.return_value.get.call_count
            _run("--spacing", "0")

        assert Touch.objects.for_user(user).count() == after_first
        assert (
            client.users.return_value.messages.return_value.get.call_count
            == fetches_after_first
        ), "the advanced cursor should have spared the second run any work at all"

    def test_two_runs_over_the_same_messages_do_not_double_log(self):
        """The real guard, isolated: replay the identical history page with
        the cursor artificially rewound, so the second run genuinely
        re-classifies the same message. `apply_findings`' thread ratchet is
        what refuses the second write, not `history_id`."""
        user = _user("poll-overlap-replay@example.com")
        Contact.all_objects.create(
            user=user, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        connection = _connection(user, history_id="1000")
        client = self._wire_gmail(self._reply(user.email))

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            _run("--spacing", "0")
            after_first = Touch.objects.for_user(user).count()
            assert after_first, "the first run must actually log something"

            GmailConnection.all_objects.filter(pk=connection.pk).update(history_id="1000")
            _run("--spacing", "0")

        assert Touch.objects.for_user(user).count() == after_first

    def test_a_mailbox_already_locked_by_another_run_is_skipped(self):
        """The narrow race the ratchet cannot close: both runs reading the
        thread's stage before either writes. `pg_try_advisory_lock` makes
        the second run step over the mailbox instead of racing it."""
        connection = _connection(_user("poll-locked@example.com"))

        # A genuinely separate Postgres session standing in for the other
        # running copy of the command.
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [gmail_poll.ADVISORY_LOCK_NAMESPACE, connection.pk],
                )
                assert cursor.fetchone()[0] is True

            with patch.object(gmail_live, "sync_connection") as sync:
                output = _run()

            sync.assert_not_called()
            assert "already being synced by another run" in output
            assert "0 synced, 1 skipped, 0 failed" in output
        finally:
            other.close()

    def test_the_lock_is_released_after_a_pass(self):
        connection = _connection(_user("poll-unlock@example.com"))
        with patch.object(gmail_live, "sync_connection"):
            _run()

        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [gmail_poll.ADVISORY_LOCK_NAMESPACE, connection.pk],
                )
                assert cursor.fetchone()[0] is True, (
                    "the pass held onto the mailbox's lock after finishing"
                )
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [gmail_poll.ADVISORY_LOCK_NAMESPACE, connection.pk],
                )
        finally:
            other.close()

    def test_the_lock_is_released_even_when_the_sync_raises(self):
        connection = _connection(_user("poll-unlock-boom@example.com"))
        with patch.object(
            gmail_live, "sync_connection", side_effect=RuntimeError("boom")
        ):
            _run()

        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [gmail_poll.ADVISORY_LOCK_NAMESPACE, connection.pk],
                )
                assert cursor.fetchone()[0] is True
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [gmail_poll.ADVISORY_LOCK_NAMESPACE, connection.pk],
                )
        finally:
            other.close()


# ---------------------------------------------------------------------------
# Spacing: no thundering herd
# ---------------------------------------------------------------------------

class TestSpacing:
    def test_mailboxes_are_spaced_out_within_a_pass(self):
        for i in range(3):
            _connection(_user(f"poll-spaced-{i}@example.com"))
        stamps: list[float] = []

        with patch.object(
            gmail_live, "sync_connection",
            side_effect=lambda _c: stamps.append(time.monotonic()),
        ):
            _run("--spacing", "0.05")

        assert len(stamps) == 3
        assert stamps[1] - stamps[0] >= 0.04
        assert stamps[2] - stamps[1] >= 0.04

    def test_a_single_mailbox_is_never_delayed(self):
        """Spacing is a gap BETWEEN mailboxes — the common deployment has
        one, and must not sit through a sleep for it."""
        _connection(_user("poll-alone@example.com"))
        started = time.monotonic()
        with patch.object(gmail_live, "sync_connection"):
            _run("--spacing", "5")
        assert time.monotonic() - started < 1.0

    @pytest.mark.parametrize(
        ("interval", "mailboxes", "expected"),
        [
            # One mailbox never waits — the common deployment.
            (120, 1, 0.0),
            (120, 0, 0.0),
            # Spread across the interval, until the cap takes over.
            (120, 60, 2.0),
            (120, 4, gmail_poll.MAX_SPACING),
            (600, 2, gmail_poll.MAX_SPACING),
            # A pass with more mailboxes than seconds still trickles.
            (10, 100, 0.1),
        ],
    )
    def test_auto_spacing_scales_with_the_deployment(self, interval, mailboxes, expected):
        assert gmail_poll.auto_spacing(interval, mailboxes) == pytest.approx(expected)

    def test_negative_spacing_is_rejected(self):
        with pytest.raises(CommandError):
            _run("--spacing", "-1")
