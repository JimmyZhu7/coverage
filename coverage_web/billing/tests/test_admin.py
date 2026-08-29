"""billing/admin.py — the append-only guarantee `CreditLedger`'s own
docstring makes ("An append-only audit trail for every credit grant, spend,
and admin adjustment... no mutable balance column, because a running total
is a second copy of the truth that can silently drift from its own
history... the ledger itself IS the audit trail") has to be enforced
somewhere, and the admin is the one place a human can touch a row at all.

`CreditLedgerAdmin.has_change_permission` already returns False — see its
own docstring: "Existing rows are read-only, never editable... A mistaken
grant is corrected with a second, opposite-sign row... not by rewriting
history." Deleting a row is the same kind of history-rewrite (worse: it also
silently changes `_raw_balance`'s `Sum("delta")`, so a deleted spend row
would hand a student back credits they already used), and the admin left it
open — `ModelAdmin`'s own default `has_delete_permission` is True. The
sibling `ProcessedStripeEventAdmin`, three lines down in the same file,
blocks change/delete/add all three for exactly this reason ("Deleting a row
here would just make a legitimate redelivery grant credits a second time,
so no delete permission either") — `CreditLedgerAdmin` blocked two of the
three and missed the one that matters most for an append-only ledger.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from billing.admin import CreditLedgerAdmin
from billing.models import CreditLedger

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def ledger_admin():
    from django.contrib import admin

    return CreditLedgerAdmin(CreditLedger, admin.site)


@pytest.fixture
def staff_request():
    """A request from a real superuser — `ModelAdmin`'s default
    `has_*_permission` implementations (the ones `CreditLedgerAdmin` does
    NOT override, like `has_add_permission`) read `request.user.has_perm`,
    so `None` won't do."""
    staff = User.objects.create_superuser(email="founder@coverage.local", password="x")
    request = RequestFactory().get("/admin/billing/creditledger/")
    request.user = staff
    return request


def test_a_ledger_row_cannot_be_deleted_through_admin(ledger_admin, staff_request):
    """The bug with teeth: without this, a founder clearing the admin's
    "delete selected" checkbox on a CreditLedger row silently corrupts every
    balance/usage figure that sums `delta` over the ledger — no error, no
    trace, just a number that stops reconciling with its own history."""
    assert ledger_admin.has_delete_permission(staff_request) is False


def test_a_ledger_row_still_cannot_be_edited(ledger_admin, staff_request):
    """Unchanged behaviour — pinned alongside the delete guard above so the
    two guarantees this docstring makes are asserted together."""
    assert ledger_admin.has_change_permission(staff_request) is False


def test_a_new_ledger_row_can_still_be_added(ledger_admin, staff_request):
    """The one write the docstring explicitly wants left open — "admin IS
    the billing system for now": a founder grants/adjusts credits by adding
    a new row, never by touching an old one."""
    assert ledger_admin.has_add_permission(staff_request) is True
