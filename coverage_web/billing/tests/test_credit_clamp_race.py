"""The clamp race between the two pre-clamped spend surfaces.

THE BUG. `affordable_residue_threads` and `affordable_autopilot_rows` are
pure reads with no lock, and render.yaml fired `coverage-gmail-backfill`
and `coverage-autopilot` on the same `*/5` minute. A student with a pending
rescan, a queued autopilot run and 5 credits had both jobs clamp their work
against the same 5, do it, and then both debit 5 — ending at -5, past the
"overdraw by at most one action's cost" edge `can_spend` documents and past
the daily burst guard that exists to bound exactly this.

THE FIX has two halves and this file tests the load-bearing one:
`billing.credits._spend_clamped` re-runs the clamp inside the user's row
lock, so the second debit to arrive charges only what the ledger can still
pay for. The cron offset in render.yaml (`2-59/5` against `*/5`) is belt and
braces on top, and is not a substitute — two ticks can still overlap when
one runs long.

`TransactionTestCase`, not the default `django_db` fixture, for the reason
`billing/tests/test_credits.py::ConcurrentGrantTest` gives: the fixture
wraps each test in a rolled-back transaction a second thread cannot see, so
a race test written against it races nothing.
"""

from __future__ import annotations

import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings

from billing import credits
from billing.models import CreditLedger

User = get_user_model()

_PLANS = {
    "free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    "pro": {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
}

# 10 threads per credit, so "50 threads' worth of residue" is 5 credits.
_PER_CREDIT = 10


pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason=(
        "The concurrent path needs SELECT ... FOR UPDATE and two real "
        "connections; SQLite has neither. This project is Postgres-only "
        "(settings/base.py's Database section says why), so this skip should "
        "never fire here — it exists so the reason is stated rather than the "
        "test failing mysteriously if it ever does."
    ),
)


def _burn_to(student, target: int) -> None:
    """Leave exactly `target` credits on the ledger, without going through
    the spend path under test."""
    credits.balance(student)  # writes this month's grant
    excess = credits.balance(student) - target
    if excess:
        CreditLedger.all_objects.create(
            user=student, delta=-excess, kind=CreditLedger.KIND_ADJUST,
            props={"why": "test setup"},
        )


@override_settings(
    CREDIT_PLANS=_PLANS,
    CREDIT_RESCAN_THREADS_PER_CREDIT=_PER_CREDIT,
    CREDIT_AUTOPILOT_ROWS_PER_CREDIT=_PER_CREDIT,
)
class ClampRaceTest(TransactionTestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            email="clamp-racer@example.com", password="x"
        )

    def _run(self, *calls):
        """Fire each callable on its own thread, all at once, and return any
        exceptions they raised."""
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(calls))

        def _go(call):
            try:
                barrier.wait(timeout=10)
                call()
            except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=_go, args=(c,)) for c in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return errors

    def test_a_rescan_and_an_autopilot_pass_cannot_both_spend_the_same_credits(self):
        """The exact shape from the audit: 5 credits, two jobs that each
        clamped to 5 credits' worth of work on the same tick. The ledger must
        not end below zero."""
        _burn_to(self.student, 5)
        assert credits.balance(self.student) == 5

        errors = self._run(
            lambda: credits.spend_rescan(self.student, 5 * _PER_CREDIT),
            lambda: credits.spend_autopilot(self.student, 5 * _PER_CREDIT),
        )

        assert errors == []
        assert credits.balance(self.student) == 0
        # `balance()` floors at zero for display, so assert on the raw sum
        # too — that is where the -5 used to show up.
        raw = sum(
            row.delta for row in CreditLedger.objects.for_user(self.student)
        )
        assert raw == 0

    def test_the_loser_of_the_race_is_written_off_and_says_so(self):
        """The second job did work it was told it could afford. Charging it
        anyway would push the ledger negative; charging it nothing silently
        would hide a real cost. So: charge what is left, and record what was
        asked for."""
        _burn_to(self.student, 5)

        self._run(
            lambda: credits.spend_rescan(self.student, 5 * _PER_CREDIT),
            lambda: credits.spend_autopilot(self.student, 5 * _PER_CREDIT),
        )

        rows = list(
            CreditLedger.objects.for_user(self.student).filter(
                kind__in=(
                    CreditLedger.KIND_SPEND_RESCAN,
                    CreditLedger.KIND_SPEND_AUTOPILOT,
                )
            )
        )
        # Exactly one debit landed: the winner took all five credits, and the
        # loser's clamp came out at zero, which writes no row at all.
        assert len(rows) == 1
        assert rows[0].delta == -5
        assert "requested_credits" not in rows[0].props

    def test_a_partial_loser_charges_what_is_left_and_records_the_rest(self):
        """The in-between case: 8 credits, two jobs each clamped to 5. The
        winner takes 5, the loser can only pay 3, and its row says it wanted
        5 so the write-off is a query rather than a hole."""
        _burn_to(self.student, 8)

        self._run(
            lambda: credits.spend_rescan(self.student, 5 * _PER_CREDIT),
            lambda: credits.spend_autopilot(self.student, 5 * _PER_CREDIT),
        )

        rows = sorted(
            CreditLedger.objects.for_user(self.student).filter(
                kind__in=(
                    CreditLedger.KIND_SPEND_RESCAN,
                    CreditLedger.KIND_SPEND_AUTOPILOT,
                )
            ),
            key=lambda r: r.delta,
        )
        assert [r.delta for r in rows] == [-5, -3]
        assert credits.balance(self.student) == 0
        clamped = rows[1]
        assert clamped.props["requested_credits"] == 5

    def test_eight_concurrent_rescans_still_cannot_go_negative(self):
        """Not a shape the crons produce, but the property the fix claims:
        no number of concurrent pre-clamped debits drives the balance below
        zero."""
        _burn_to(self.student, 5)

        errors = self._run(*[
            (lambda: credits.spend_rescan(self.student, 2 * _PER_CREDIT))
            for _ in range(8)
        ])

        assert errors == []
        raw = sum(row.delta for row in CreditLedger.objects.for_user(self.student))
        assert raw == 0

    def test_the_daily_burst_guard_is_the_other_ceiling(self):
        """The clamp is `min(balance, what is left of today's burst)` — a
        student with plenty of credits still cannot burn past the burst
        guard by racing two jobs. Free's burst here is 15."""
        credits.balance(self.student)  # 60 credits, burst 15

        self._run(
            lambda: credits.spend_rescan(self.student, 12 * _PER_CREDIT),
            lambda: credits.spend_autopilot(self.student, 12 * _PER_CREDIT),
        )

        assert credits.daily_spent(self.student) == 15


@pytest.mark.django_db
@override_settings(
    CREDIT_PLANS=_PLANS,
    CREDIT_RESCAN_THREADS_PER_CREDIT=_PER_CREDIT,
)
def test_the_chat_overdraw_edge_is_untouched():
    """`spend()` is NOT clamped, on purpose. A Pro student on their last
    credit is documented to be let through and to overdraw by the full
    message cost (`can_spend`'s "overdraw edge") — the fix above must not
    quietly undo that by clamping every debit in the module."""
    student = User.objects.create_user(
        email="overdrawer@example.com", password="x", plan=User.PLAN_PRO
    )
    credits.balance(student)
    CreditLedger.all_objects.create(
        user=student, delta=-(180 - 1), kind=CreditLedger.KIND_ADJUST, props={},
    )
    assert credits.can_spend(student, 3) is True

    credits.spend(student, 3, CreditLedger.KIND_SPEND_CHAT, model="sonnet")

    raw = sum(row.delta for row in CreditLedger.objects.for_user(student))
    assert raw == -2  # overdrawn by two, exactly as documented
