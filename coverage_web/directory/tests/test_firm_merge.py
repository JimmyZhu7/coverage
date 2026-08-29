"""directory.firm_merge — merging Firm rows that share the same name.

Reproduces the live TD Securities shape directly: two Firm rows named
identically (one seeded/scraped, one minted by a test fixture run against
the dev DB — see ingest.py's `_FirmResolver` docstring), with the same
Workday requisition URL posted open under both, plus per-user tracking rows
on each side that must combine rather than collide or silently vanish.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.models import UserFirm
from directory.firm_merge import find_duplicate_firm_groups, merge_firms
from directory.models import EmailPatternStats, Firm, FirmDate, Opportunity

User = get_user_model()
NOW = timezone.now()


def _user(email):
    return User.objects.create_user(email=email, password="x")


@pytest.mark.django_db
def test_find_duplicate_firm_groups_matches_the_live_td_shape():
    older = Firm.objects.create(slug="td", name="TD Securities")
    newer = Firm.objects.create(slug="td-closed", name="TD Securities")
    Firm.objects.create(slug="citi", name="Citi")  # not a collision — must not show up

    groups = find_duplicate_firm_groups()

    assert len(groups) == 1
    assert [f.id for f in groups[0]] == [older.id, newer.id]  # canonical (lowest id) first


@pytest.mark.django_db
def test_find_duplicate_firm_groups_is_case_insensitive():
    a = Firm.objects.create(slug="td", name="TD Securities")
    b = Firm.objects.create(slug="td2", name="td securities")
    groups = find_duplicate_firm_groups()
    assert len(groups) == 1
    assert {f.id for f in groups[0]} == {a.id, b.id}


@pytest.mark.django_db
def test_merge_reparents_a_non_overlapping_opportunity():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    o = Opportunity.objects.create(
        firm=duplicate, url="https://td.wd3.myworkdayjobs.com/job/only-on-dup",
        title="Banking Associate", bucket="internship", status="open",
    )

    stats = merge_firms(canonical, duplicate)

    o.refresh_from_db()
    assert o.firm_id == canonical.id
    assert stats["opportunities_moved"] == 1
    assert stats["opportunities_merged"] == 0
    assert not Firm.objects.filter(id=duplicate.id).exists()


@pytest.mark.django_db
def test_merge_folds_a_url_collision_into_one_row_open_wins():
    """The exact live shape: the SAME url posted open under both firms —
    the merge must end with one row, not two, and it must stay open."""
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    url = "https://td.wd3.myworkdayjobs.com/job/rive-sud"

    keep = Opportunity.objects.create(
        firm=canonical, url=url, title="Rive Sud Event", bucket="insight",
        status="closed", location="", deadline=None,
    )
    Opportunity.objects.filter(pk=keep.pk).update(
        first_seen=NOW - timedelta(days=10), last_verified=NOW - timedelta(days=5))

    lose = Opportunity.objects.create(
        firm=duplicate, url=url, title="Rive Sud Event", bucket="insight",
        status="open", location="Montreal", deadline=None,
    )
    Opportunity.objects.filter(pk=lose.pk).update(
        first_seen=NOW - timedelta(days=3), last_verified=NOW - timedelta(days=1))

    stats = merge_firms(canonical, duplicate)

    assert stats["opportunities_merged"] == 1
    assert Opportunity.objects.filter(url=url).count() == 1
    survivor = Opportunity.objects.get(url=url)
    assert survivor.firm_id == canonical.id
    assert survivor.status == "open"          # open beats closed
    assert survivor.location == "Montreal"     # a stated location beats a blank one
    assert survivor.first_seen == NOW - timedelta(days=10)  # earliest first_seen kept
    assert survivor.last_verified == NOW - timedelta(days=1)  # most recent verification kept


@pytest.mark.django_db
def test_merge_combines_user_opportunity_tracking_instead_of_dropping_one_side():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    url = "https://td.wd3.myworkdayjobs.com/job/rive-sud"
    keep_opp = Opportunity.objects.create(
        firm=canonical, url=url, title="Rive Sud Event", bucket="insight", status="open")
    lose_opp = Opportunity.objects.create(
        firm=duplicate, url=url, title="Rive Sud Event", bucket="insight", status="open")

    student = _user("student@example.com")
    # The student tracked the DUPLICATE copy and marked it "interview" —
    # this must survive the merge, not silently vanish.
    UserOpportunity.all_objects.create(
        user=student, opportunity=lose_opp, applied_status="interview",
        interview_dates=["2026-09-01"],
    )
    # ...and separately saved the CANONICAL copy at a lower stage — the
    # merge must keep the furthest-along status, not overwrite it backwards.
    UserOpportunity.all_objects.create(
        user=student, opportunity=keep_opp, applied_status="saved",
    )

    merge_firms(canonical, duplicate)

    rows = UserOpportunity.all_objects.filter(user=student)
    assert rows.count() == 1  # combined, not duplicated
    row = rows.get()
    assert row.opportunity_id == Opportunity.objects.get(url=url).id
    assert row.applied_status == "interview"       # furthest-along wins
    assert row.interview_dates == ["2026-09-01"]    # not lost


@pytest.mark.django_db
def test_merge_combines_user_firm_network_rows_without_integrity_error():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    student = _user("student@example.com")
    UserFirm.all_objects.create(user=student, firm=canonical, tier=1)
    UserFirm.all_objects.create(user=student, firm=duplicate, status="tracked")

    merge_firms(canonical, duplicate)  # must not raise UniqueConstraint(user, firm)

    rows = UserFirm.all_objects.filter(user=student, firm=canonical)
    assert rows.count() == 1
    assert rows.get().tier == 1
    assert rows.get().status == "tracked"


@pytest.mark.django_db
def test_merge_combines_firm_dates_without_integrity_error():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    FirmDate.objects.create(
        firm=canonical, cycle="ft2027", event_kind="applications_open", confidence=0.4)
    FirmDate.objects.create(
        firm=duplicate, cycle="ft2027", event_kind="applications_open", confidence=0.9)

    merge_firms(canonical, duplicate)

    rows = FirmDate.objects.filter(firm=canonical, cycle="ft2027", event_kind="applications_open")
    assert rows.count() == 1
    assert rows.get().confidence == 0.9  # the more confident reading survives


@pytest.mark.django_db
def test_merge_combines_email_pattern_stats():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    duplicate = Firm.objects.create(slug="td-closed", name="TD Securities")
    duplicate_id = duplicate.id
    EmailPatternStats.objects.create(firm=canonical, delivered=10, bounced=1)
    EmailPatternStats.objects.create(firm=duplicate, delivered=5, bounced=2)

    merge_firms(canonical, duplicate)

    stats = EmailPatternStats.objects.get(firm=canonical)
    assert stats.delivered == 15
    assert stats.bounced == 3
    assert not EmailPatternStats.objects.filter(firm_id=duplicate_id).exists()


@pytest.mark.django_db
def test_command_dry_run_writes_nothing():
    Firm.objects.create(slug="td", name="TD Securities")
    Firm.objects.create(slug="td-closed", name="TD Securities")

    call_command("merge_duplicate_firms")

    assert Firm.objects.count() == 2  # untouched


@pytest.mark.django_db
def test_command_apply_actually_merges():
    canonical = Firm.objects.create(slug="td", name="TD Securities")
    Firm.objects.create(slug="td-closed", name="TD Securities")

    call_command("merge_duplicate_firms", apply=True)

    assert Firm.objects.count() == 1
    assert Firm.objects.get().id == canonical.id
