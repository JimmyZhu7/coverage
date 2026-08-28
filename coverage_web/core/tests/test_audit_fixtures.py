"""Tests for audit_fixtures — read-only, and it has to actually find the
shapes of residue this repo has already produced twice on record: a firm
written straight into the shared directory by a `manage.py shell` insert
(see Firm.slug's docstring), and a FirmDate carrying no possible provenance.
It also must never flag the two legitimate system accounts, real firms, or
the demo seed's own placeholder row."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from crm.models import Contact
from directory.models import Firm, FirmDate

User = get_user_model()


def _run(**kw):
    """Run audit_fixtures and return everything it printed. It raises
    CommandError on any finding (see the command's non-zero-exit note) —
    that's exercised separately below; here we only care what got printed
    before the raise, since Django writes to stdout first either way."""
    from io import StringIO

    out = StringIO()
    try:
        call_command("audit_fixtures", stdout=out, **kw)
    except CommandError:
        pass
    return out.getvalue()


@pytest.mark.django_db
def test_clean_database_reports_nothing():
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_system_accounts_are_never_flagged():
    User.objects.create_user(email="admin@coverage.local", password="x")
    User.objects.create_user(email="demo@coverage.local", password="x")
    out = _run()
    assert "admin@coverage.local" not in out
    assert "demo@coverage.local" not in out
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_reserved_domain_user_is_flagged_with_its_cascade():
    user = User.objects.create_user(email="ob-test@example.com", password="x")
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Contact.all_objects.create(user=user, firm=firm, name="Ada Lovelace", email="ada@gs.com")

    out = _run()

    assert "ob-test@example.com" in out
    assert "crm.Contact" in out and "1 row(s)" in out


@pytest.mark.django_db
def test_real_looking_email_domain_is_not_flagged():
    User.objects.create_user(email="real.student@gmail.com", password="x")
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_synthetic_firm_is_flagged_but_real_firms_are_not():
    Firm.objects.create(slug="verify-jpm-play", name="Verify J.P. Morgan")
    Firm.objects.create(slug="barclays", name="Barclays")
    Firm.objects.create(slug="statestreet", name="State Street")

    out = _run()

    assert "verify-jpm-play" in out
    assert "Barclays" not in out
    assert "State Street" not in out


@pytest.mark.django_db
def test_firm_date_with_out_of_band_confidence_is_flagged():
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    FirmDate.objects.create(
        firm=firm, cycle="2027", region="us", event_kind="app_close",
        confidence=95.0, source_url="", found_on=None, history=[],
    )
    out = _run()
    assert "J.P. Morgan" in out
    assert "confidence=95.0" in out


@pytest.mark.django_db
def test_firm_date_with_no_provenance_is_flagged_even_at_valid_confidence():
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(
        firm=firm, cycle="", region="us", event_kind="app_close",
        confidence=1.0, source_url="", found_on=None, history=[],
    )
    out = _run()
    assert "Goldman Sachs" in out


@pytest.mark.django_db
def test_legitimately_seeded_firm_date_is_not_flagged():
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    # A real import_firm_dates write: found_on + history always travel together.
    from django.utils import timezone

    FirmDate.objects.create(
        firm=firm, cycle="SA 2028", region="hk", event_kind="app_close",
        confidence=0.6, source_url="https://example.org/careers",
        found_on=timezone.now(), history=[{"date": "2026-09-30", "confidence": "reported"}],
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_demo_seed_marker_row_is_not_flagged():
    firm = Firm.objects.create(slug="apollo", name="Apollo")
    FirmDate.objects.create(
        firm=firm, cycle="sa2028_ib", region="us", event_kind="app_close",
        confidence=1.0, source_url="seed:demo", found_on=None, history=[],
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_smoke_test_contact_is_flagged_regardless_of_owner():
    user = User.objects.create_user(email="founder@realmail.com", password="x")
    Contact.all_objects.create(
        user=user, name="ZZZ Smoke Test Contact", archived=True,
        source="automated smoke test", warmth="advocate",
    )
    out = _run()
    assert "ZZZ Smoke Test Contact" in out
    assert "founder@realmail.com" in out


@pytest.mark.django_db
def test_raises_command_error_when_findings_exist_so_the_process_exit_code_is_nonzero():
    Firm.objects.create(slug="verify-test-play", name="Verify Test Play")
    with pytest.raises(CommandError, match="fixture-shaped row"):
        call_command("audit_fixtures")


@pytest.mark.django_db
def test_no_error_raised_when_nothing_found():
    call_command("audit_fixtures")  # must not raise
