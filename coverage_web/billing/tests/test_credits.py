"""billing.credits — the ledger's own behaviour, independent of either
metered surface that spends from it (assistant/agent.py and
capture/gmail_live.py get their own end-to-end tests). Everything here is
driven straight through the `billing.credits` API, matching the plan doc's
(`docs/credit-system-plan.md`) own description of that API.
"""

from __future__ import annotations

import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TransactionTestCase, override_settings

from billing import credits
from billing.models import CreditLedger

User = get_user_model()

pytestmark = pytest.mark.django_db

_PLANS = {
    "free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    "pro": {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
}


@pytest.fixture
def student():
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def pro_student():
    return User.objects.create_user(email="pro-student@example.com", password="x", plan=User.PLAN_PRO)


# ---------------------------------------------------------------------------
# Lazy monthly grant
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS=_PLANS)
def test_the_first_balance_check_of_the_month_writes_the_grant(student):
    assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_GRANT).count() == 0

    balance = credits.balance(student)

    assert balance == 60
    assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_GRANT).count() == 1


@override_settings(CREDIT_PLANS=_PLANS)
def test_a_second_balance_check_the_same_month_does_not_grant_again(student):
    credits.balance(student)
    credits.balance(student)
    credits.balance(student)

    assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_GRANT).count() == 1


@override_settings(CREDIT_PLANS=_PLANS)
def test_ensure_monthly_grant_is_a_no_op_call_after_the_first(student):
    credits.ensure_monthly_grant(student)
    credits.ensure_monthly_grant(student)

    assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_GRANT).count() == 1


class ConcurrentGrantTest(TransactionTestCase):
    """A real transactional test (not the default `django_db` fixture,
    which wraps each test in a rolled-back transaction a second thread
    can't see): two threads racing `ensure_monthly_grant` for the SAME
    user must still land exactly one grant row, per docs/credit-system-
    plan.md §4's "the unique constraint makes a concurrent double-write a
    harmless IntegrityError to swallow"."""

    @override_settings(CREDIT_PLANS=_PLANS)
    def test_concurrent_grant_checks_never_double_grant(self):
        student = User.objects.create_user(email="racer@example.com", password="x")
        errors = []

        def _go():
            try:
                credits.ensure_monthly_grant(student)
            except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=_go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_GRANT).count() == 1
        assert credits.balance(student) == 60


# ---------------------------------------------------------------------------
# Balance arithmetic
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS=_PLANS)
def test_balance_is_the_sum_of_every_ledger_row(student):
    credits.ensure_monthly_grant(student)  # +60
    credits.spend(student, 5, CreditLedger.KIND_SPEND_CHAT)
    credits.spend(student, 3, CreditLedger.KIND_SPEND_RESCAN, threads=25)
    CreditLedger.all_objects.create(user=student, delta=10, kind=CreditLedger.KIND_ADJUST)

    assert credits.balance(student) == 60 - 5 - 3 + 10


@override_settings(CREDIT_PLANS=_PLANS)
def test_spend_writes_one_negative_row_with_the_given_kind_and_props(student):
    credits.spend(student, 3, CreditLedger.KIND_SPEND_RESCAN, threads=37)

    row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_SPEND_RESCAN)
    assert row.delta == -3
    assert row.props == {"threads": 37}


def test_spend_of_zero_or_negative_writes_nothing(student):
    credits.spend(student, 0, CreditLedger.KIND_SPEND_CHAT)
    credits.spend(student, -5, CreditLedger.KIND_SPEND_CHAT)

    assert CreditLedger.objects.for_user(student).count() == 0


# ---------------------------------------------------------------------------
# can_spend / the daily burst guard
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 2}})
def test_the_burst_guard_blocks_once_todays_spend_reaches_the_limit(student):
    assert credits.can_spend(student, 1)
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)
    assert credits.can_spend(student, 1)
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)

    # 2 spent today, burst is 2 — blocked even though the monthly balance
    # (58 left of 60) has plenty of room.
    assert not credits.can_spend(student, 1)
    assert credits.balance(student) == 58


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_zero_balance_blocks_regardless_of_the_burst_guard(student):
    credits.spend(student, 60, CreditLedger.KIND_SPEND_CHAT)  # exhaust the whole monthly grant

    assert credits.balance(student) == 0
    assert not credits.can_spend(student, 1)


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_an_admin_adjustment_never_counts_against_the_burst_guard(student):
    """A large negative admin correction (undoing a mistaken grant, say)
    must not look like "58 credits of chat spend today" to the burst guard
    — only actual spend() rows (spend_chat/spend_rescan) count."""
    CreditLedger.all_objects.create(user=student, delta=-58, kind=CreditLedger.KIND_ADJUST)

    assert credits.daily_spent(student) == 0
    assert credits.can_spend(student, 1)


@override_settings(CREDIT_PLANS={"pro": {"monthly_grant": 3, "message_cost": 3, "daily_burst": 45}})
def test_a_pro_user_on_their_last_credits_is_still_let_through_the_overdraw_edge(pro_student):
    """docs/credit-system-plan.md §6's "overdraw edge": a positive balance
    is enough to start a turn, even when it's smaller than the cost about
    to be charged."""
    credits.spend(pro_student, 2, CreditLedger.KIND_SPEND_CHAT)  # 1 credit left, cost is 3

    assert credits.can_spend(pro_student, 3)

    credits.spend(pro_student, 3, CreditLedger.KIND_SPEND_CHAT)
    assert credits.balance(pro_student) == 0  # floored, even though the raw sum is negative
    assert credits._raw_balance(pro_student) == -2


# ---------------------------------------------------------------------------
# refund() — a compensating credit that nets out of usage, not just balance
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_refund_writes_a_positive_kind_refund_row(student):
    credits.refund(student, 3, reason="turn_failed_after_charge", model="claude-haiku-4-5")

    row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_REFUND)
    assert row.delta == 3
    assert row.props == {"reason": "turn_failed_after_charge", "model": "claude-haiku-4-5"}


def test_refund_of_zero_or_negative_writes_nothing(student):
    credits.refund(student, 0)
    credits.refund(student, -5)

    assert CreditLedger.objects.for_user(student).count() == 0


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 2}})
def test_a_refund_frees_the_burst_guard_it_reverses(student):
    """The scenario docs/credit-system-plan.md's refund section walks
    through: round 0 of a turn charges, a later round fails, the turn is
    refunded. That refunded turn must not count as one of today's 2
    burst-guard slots — a `spend()` immediately followed by its matching
    `refund()` must land the student back where they started, not one
    slot closer to being locked out for the day."""
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT, model="claude-haiku-4-5")
    credits.refund(student, 1, reason="turn_failed_after_charge", model="claude-haiku-4-5")

    assert credits.daily_spent(student) == 0
    assert credits.balance(student) == 60

    # Both burst-guard slots are still available — a refunded charge left
    # no trace on today's spend count.
    assert credits.can_spend(student, 1)
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)
    assert credits.can_spend(student, 1)
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)
    assert not credits.can_spend(student, 1)


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_a_refund_frees_the_monthly_usage_line(student):
    """Settings' "used N so far this month" line must reconcile with the
    balance shown next to it — a refunded turn changes neither."""
    credits.spend(student, 5, CreditLedger.KIND_SPEND_CHAT)
    credits.spend(student, 3, CreditLedger.KIND_SPEND_RESCAN, threads=25)
    credits.refund(student, 5, reason="turn_failed_after_charge")

    assert credits.month_usage(student) == 3  # only the un-refunded rescan spend
    assert credits.balance(student) == 60 - 3


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_a_repeated_refunded_turn_never_exhausts_the_burst_guard(student):
    """The consequence docs/credit-system-plan.md now calls out by name: a
    run of turns that each charge and then get refunded (an outage, a
    flaky patch of tool-call failures) must not burn through daily_burst
    at all — every one of them nets to zero."""
    for _ in range(20):  # far more than daily_burst=15 would allow if unrefunded
        credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)
        credits.refund(student, 1, reason="turn_failed_after_charge")

    assert credits.daily_spent(student) == 0
    assert credits.can_spend(student, 1)
    assert credits.balance(student) == 60


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_an_unrefunded_spend_still_counts_normally_alongside_refunded_ones(student):
    """Netting must not over-correct: a refund only cancels the spend it
    actually reverses, not unrelated spend in the same window."""
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)
    credits.refund(student, 1, reason="turn_failed_after_charge")
    credits.spend(student, 1, CreditLedger.KIND_SPEND_CHAT)  # a normal, un-refunded charge

    assert credits.daily_spent(student) == 1
    assert credits.balance(student) == 59


# ---------------------------------------------------------------------------
# Rollover, capped at 2x monthly grant
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_an_untouched_balance_rolls_over_in_full(student):
    CreditLedger.all_objects.create(
        user=student, delta=60, kind=CreditLedger.KIND_GRANT, period="2026-07",
    )
    # Simulate "it's a new month": the July grant exists, balance is 60,
    # nothing has been spent. ensure_monthly_grant for THIS month should
    # add the full 60 again (120 total, under the 120 cap exactly).
    delta = min(60, 2 * 60 - credits._raw_balance(student))
    assert delta == 60


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_rollover_is_clamped_so_balance_never_exceeds_2x_the_monthly_grant(student):
    # A dormant account: last month's grant, nothing ever spent.
    CreditLedger.all_objects.create(
        user=student, delta=60, kind=CreditLedger.KIND_GRANT, period="2020-01",
    )
    assert credits._raw_balance(student) == 60

    # This month's lazy grant should add only 60 more (120 total = the 2x
    # cap), not a second full 60 stacked with no ceiling.
    period = credits._period_for(student)
    plan = credits.plan_config(student)
    current = credits._raw_balance(student)
    expected_delta = max(0, min(plan["monthly_grant"], 2 * plan["monthly_grant"] - current))
    assert expected_delta == 60

    CreditLedger.all_objects.create(user=student, delta=expected_delta, kind=CreditLedger.KIND_GRANT, period=period)
    assert credits._raw_balance(student) == 120  # exactly 2x, never more


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}})
def test_rollover_clamps_to_zero_never_goes_negative(student):
    # A balance already sitting above the 2x cap somehow (e.g. an admin
    # adjustment) must not make the grant negative.
    CreditLedger.all_objects.create(user=student, delta=500, kind=CreditLedger.KIND_ADJUST)
    plan = credits.plan_config(student)
    current = credits._raw_balance(student)
    delta = max(0, min(plan["monthly_grant"], 2 * plan["monthly_grant"] - current))
    assert delta == 0


# ---------------------------------------------------------------------------
# Rescan residue helpers
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}},
                    CREDIT_RESCAN_THREADS_PER_CREDIT=10)
def test_affordable_residue_threads_is_clamped_by_balance(student):
    # 60 credits * 10 threads/credit = 600 affordable, but only 37 are on offer.
    assert credits.affordable_residue_threads(student, 37) == 37


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 2, "message_cost": 1, "daily_burst": 15}},
                    CREDIT_RESCAN_THREADS_PER_CREDIT=10)
def test_affordable_residue_threads_is_clamped_by_a_thin_balance(student):
    # 2 credits * 10 threads/credit = 20 affordable, of 100 on offer.
    assert credits.affordable_residue_threads(student, 100) == 20


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 0, "message_cost": 1, "daily_burst": 15}},
                    CREDIT_RESCAN_THREADS_PER_CREDIT=10)
def test_affordable_residue_threads_is_zero_at_zero_balance(student):
    assert credits.affordable_residue_threads(student, 100) == 0


@override_settings(CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}},
                    CREDIT_RESCAN_THREADS_PER_CREDIT=10)
def test_spend_rescan_rounds_up_to_the_next_whole_credit(student):
    credits.spend_rescan(student, 25)  # 25 threads / 10 = 2.5 -> 3 credits

    row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_SPEND_RESCAN)
    assert row.delta == -3
    assert row.props == {"threads": 25}


def test_spend_rescan_of_zero_threads_writes_nothing(student):
    credits.spend_rescan(student, 0)

    assert CreditLedger.objects.for_user(student).count() == 0


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------
@override_settings(CREDIT_PLANS=_PLANS)
def test_one_students_ledger_never_reaches_another(student, pro_student):
    credits.ensure_monthly_grant(student)
    credits.spend(student, 5, CreditLedger.KIND_SPEND_CHAT)

    assert credits.balance(pro_student) == 180  # only the Pro grant, untouched by student's spend
    assert CreditLedger.objects.for_user(pro_student).count() == 1  # just its own grant row


def test_credit_ledger_objects_refuses_an_unscoped_query():
    from coverage_web.tenancy import TenantScopeError

    with pytest.raises(TenantScopeError):
        list(CreditLedger.objects.all())


# ---------------------------------------------------------------------------
# Admin grant/adjustment
# ---------------------------------------------------------------------------
def test_an_admin_adjustment_row_is_reflected_in_balance(student):
    """The founder's own hand-grant path (billing/admin.py): a plain
    CreditLedger row with kind="adjust", written the same way the Django
    admin's Add form would write one."""
    before = credits.balance(student)

    CreditLedger.all_objects.create(
        user=student, delta=50, kind=CreditLedger.KIND_ADJUST, props={"reason": "beta tester top-up"},
    )

    assert credits.balance(student) == before + 50


def test_the_unique_grant_constraint_only_applies_to_grant_rows(student):
    """Two spend rows (or any non-grant kind) in the same "period" slot must
    not collide — the UniqueConstraint is scoped to kind="grant" only."""
    CreditLedger.all_objects.create(user=student, delta=-1, kind=CreditLedger.KIND_SPEND_CHAT, period="2026-08")
    CreditLedger.all_objects.create(user=student, delta=-1, kind=CreditLedger.KIND_SPEND_CHAT, period="2026-08")

    assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_SPEND_CHAT).count() == 2


def test_a_second_grant_row_for_the_same_user_and_period_is_rejected(student):
    CreditLedger.all_objects.create(user=student, delta=60, kind=CreditLedger.KIND_GRANT, period="2026-08")

    with pytest.raises(IntegrityError):
        CreditLedger.all_objects.create(user=student, delta=60, kind=CreditLedger.KIND_GRANT, period="2026-08")
