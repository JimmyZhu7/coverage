"""Tests for audit_fixtures — read-only, and it has to actually find the
shapes of residue this repo has already produced on record: a firm written
straight into the shared directory by a `manage.py shell` insert (see
Firm.slug's docstring), a FirmDate carrying no possible provenance, 60
`seed.local` Opportunity rows in the founder's live feed, and "Verify Cold
One"-shaped Contact rows on the demo account. It also must never flag the
two legitimate system accounts, real firms, real postings, or anything
`seed_demo` itself creates."""

from __future__ import annotations

from io import StringIO

import pytest
from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from analytics.models import UserOpportunity
from capture.models import ContactProposal
from crm.models import Campaign, CalendarEvent, Contact, Touch
from directory.models import Firm, FirmDate, Opportunity

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
def test_an_out_of_band_confidence_can_no_longer_be_written_at_all():
    """This used to assert the audit REPORTS a confidence of 95.0 — the real
    value that sat on a J.P. Morgan row after somebody typed 95 meaning 95%
    into a 0-to-1 column.

    That row is no longer reachable. `firm_dates_confidence_in_range` (see
    `directory.models.FirmDate`) now rejects it in Postgres, before the audit
    or any application code gets a look. So the honest assertion is that the
    write fails, not that we notice it afterwards: prevention outranks
    detection, and a test that still described detection would be describing
    a state the schema forbids.

    The audit's own out-of-band check stays where it is, deliberately. It
    costs nothing, and it is the thing that would still speak up if the
    constraint were ever dropped in a future migration."""
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FirmDate.objects.create(
                firm=firm, cycle="ft2027", region="us", event_kind="app_close",
                confidence=95.0, source_url="", found_on=None, history=[],
            )


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
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        confidence=0.6, source_url="https://example.org/careers",
        found_on=timezone.now(), history=[{"date": "2026-09-30", "confidence": "reported"}],
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_demo_seed_marker_row_is_not_flagged():
    firm = Firm.objects.create(slug="apollo", name="Apollo")
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", track="ib", region="us", event_kind="app_close",
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


# ------------------------------------------------------------ opportunities
# Reproduces leak #4: 60 rows like "Morgan Stanley / '2027 Summer Analyst,
# Seat 0'" .. "Seat 11", every one at a `seed.local` URL, sitting in the
# founder's live Opportunities feed until he found them by scrolling.
# Built here rather than in the shared database per the brief.

@pytest.mark.django_db
def test_seed_local_url_opportunity_is_flagged():
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst", bucket="internship",
        status="open", url="https://seed.local/ms/0",
    )
    out = _run()
    assert "directory.Opportunity" in out
    assert "seed.local" in out


@pytest.mark.django_db
def test_sequential_seat_titles_are_flagged_even_at_a_real_looking_url():
    """The two tells are independent — a future leak reusing only the
    naming pattern, at a URL that isn't seed.local, must still be caught."""
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst, Seat 0", bucket="internship",
        status="open", url="https://jpmorgan.com/careers/12345",
    )
    out = _run()
    assert "directory.Opportunity" in out
    assert "Seat 0" in out


@pytest.mark.django_db
def test_real_looking_opportunity_is_not_flagged():
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(
        firm=firm, title="Summer Analyst, Investment Banking", bucket="internship",
        status="open", url="https://higher.gs.com/roles/98765", source="workday",
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_seed_local_substring_in_an_unrelated_url_is_not_flagged():
    """`icontains` is used only as a coarse DB-side prefilter — the real
    check parses the URL and compares the actual host, so a legitimate URL
    that merely contains the substring "seed.local" (e.g. in a query
    string) is not a false positive."""
    firm = Firm.objects.create(slug="db", name="Deutsche Bank")
    Opportunity.objects.create(
        firm=firm, title="Analyst Programme", bucket="internship", status="open",
        url="https://careers.db.com/apply?ref=seed.local-campaign",
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_flagged_opportunity_shows_its_user_opportunity_cascade():
    """A fixture Opportunity has no free-text field on UserOpportunity to
    pattern-match — only the FK back to it — so the generic cascade is
    what has to surface this, not a bespoke UserOpportunity check."""
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst, Seat 0", bucket="internship",
        status="open", url="https://seed.local/ms/0",
    )
    user = User.objects.create_user(email="founder@realmail.com", password="x")
    UserOpportunity.all_objects.create(user=user, opportunity=opp)

    out = _run()

    assert "directory.Opportunity" in out
    assert "analytics.UserOpportunity" in out and "1 row(s)" in out


# ------------------------------------------------------------------ contacts
# Reproduces leak #5: six "Verify Cold One"-shaped Contact rows on the demo
# account, which the original ZZZ/smoke-test-only pattern did not catch.

@pytest.mark.django_db
def test_verify_prefixed_contact_is_flagged():
    demo = User.objects.create_user(email="demo@coverage.local", password="x")
    Contact.all_objects.create(user=demo, name="Verify Cold One", warmth="cold")
    out = _run()
    assert "Verify Cold One" in out


@pytest.mark.django_db
def test_name_merely_containing_verify_is_not_flagged():
    """Anchored + word-bounded on purpose: a real name that happens to
    start with letters spelling "verify" mid-word, or uses the word
    elsewhere in a sentence, must not trip this."""
    user = User.objects.create_user(email="real.student@gmail.com", password="x")
    Contact.all_objects.create(user=user, name="Verified Interview Prep Group", warmth="cold")
    out = _run()
    assert "No fixture-shaped rows found." in out


# ------------------------------------------------------------ other tables

@pytest.mark.django_db
def test_contact_proposal_with_verify_name_is_flagged():
    user = User.objects.create_user(email="founder@realmail.com", password="x")
    ContactProposal.all_objects.create(
        user=user, name="Verify Discovery Sender", email="verify@realbank.com",
    )
    out = _run()
    assert "capture.ContactProposal" in out
    assert "Verify Discovery Sender" in out


@pytest.mark.django_db
def test_real_contact_proposal_is_not_flagged():
    user = User.objects.create_user(email="founder@realmail.com", password="x")
    ContactProposal.all_objects.create(
        user=user, name="Jordan Reyes", email="jordan.reyes@somebank.com",
        role_hint="Campus Recruiting",
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_calendar_event_with_test_title_is_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    CalendarEvent.all_objects.create(user=user, title="Test Coffee Chat", starts_at=timezone.now())
    out = _run()
    assert "crm.CalendarEvent" in out
    assert "Test Coffee Chat" in out


@pytest.mark.django_db
def test_real_calendar_event_is_not_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    CalendarEvent.all_objects.create(user=user, title="Superday — Goldman Sachs", starts_at=timezone.now())
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_campaign_with_verify_label_is_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    now = timezone.now()
    Campaign.all_objects.create(
        user=user, signature="verify-sig", label="Verify Campaign Detection",
        first_sent=now, last_sent=now,
    )
    out = _run()
    assert "crm.Campaign" in out
    assert "Verify Campaign Detection" in out


@pytest.mark.django_db
def test_real_campaign_is_not_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    now = timezone.now()
    Campaign.all_objects.create(
        user=user, signature="fall-2026-icc-panel", label="Fall 2026 ICC Alumni Digital Panel Outreach",
        first_sent=now, last_sent=now,
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


@pytest.mark.django_db
def test_touch_with_verify_note_is_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    contact = Contact.all_objects.create(user=user, firm=firm, name="Ada Lovelace")
    Touch.all_objects.create(
        user=user, contact=contact, ts=timezone.now(), kind="outreach",
        note="Verify touch logging renders",
    )
    out = _run()
    assert "crm.Touch" in out
    assert "Verify touch logging renders" in out


@pytest.mark.django_db
def test_real_touch_is_not_flagged():
    from django.utils import timezone

    user = User.objects.create_user(email="founder@realmail.com", password="x")
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    contact = Contact.all_objects.create(user=user, firm=firm, name="Ada Lovelace")
    Touch.all_objects.create(
        user=user, contact=contact, ts=timezone.now(), kind="outreach",
        note="Met at the campus info session, exchanged cards",
    )
    out = _run()
    assert "No fixture-shaped rows found." in out


# --------------------------------------------------------- real seed data
# The one false-positive proof that matters most: running the actual safe
# path (`manage.py seed_demo`) must never trip any of the checks above,
# across every table it touches (User, Firm, FirmDate, Contact, Touch).

@pytest.mark.django_db(transaction=True)  # seed_demo's touch() calls
# crm.services.log_touch, which opens its own real psycopg connection (see
# crm/services.py's docstring and accounts/tests/test_seed_demo.py's own
# pytestmark) — a plain django_db test's uncommitted transaction is
# invisible to it.
def test_seed_demo_output_is_never_flagged():
    call_command("seed_demo", stdout=StringIO())
    out = _run()
    assert "No fixture-shaped rows found." in out
