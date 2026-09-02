"""Which rows a 200-row reverify run actually reaches.

The cutoff decides which rows are ELIGIBLE; the order decides which of them a
capped run gets to, and the eligible pool is 14,227 rows deep against a budget
of 200. Ordering by `deadline_checked_at` alone spends that budget in arrival
order, and arrival order is overwhelmingly experienced-hire inventory: 13,306
of 16,029 open rows are `other`, against 2,723 campus. Measured on the live
board 2026-09-01, the first run under the old order reached 23 campus rows out
of 200 (12%), and 1 dated row out of 200.

The budget is unchanged — same limit, same cutoff, same request volume. Only
the order moved.
"""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _firm():
    return Firm.objects.create(slug="citi", name="Citi")


def _row(firm, n, *, bucket, deadline=None, checked_days_ago=None,
         verified_days_ago=None):
    now = timezone.now()
    return Opportunity.objects.create(
        firm=firm, url=f"https://citi.wd5/{bucket}/{n}", title=f"Role {n}",
        bucket=bucket, status="open", source="workday", deadline=deadline,
        deadline_checked_at=(now - timedelta(days=checked_days_ago)
                             if checked_days_ago is not None else None),
        last_verified=(now - timedelta(days=verified_days_ago)
                       if verified_days_ago is not None else None),
    )


def _reached(limit, verifier):
    """Run the command with `verify` stubbed and return the ids it checked,
    in the order it checked them."""
    out = StringIO()
    call_command("reverify", limit=limit, dry_run=True, workers=1, stdout=out)
    return verifier["seen"]


@pytest.fixture
def stub_verify(monkeypatch):
    from directory.management.commands import reverify as cmd

    seen: list[str] = []

    def fake(url):
        seen.append(url)
        return type("R", (), {"result": "needs-verification", "provider": "workday",
                              "deadline_dates": [], "url": url})()

    monkeypatch.setattr(cmd, "verify", fake)
    return {"seen": seen}


def test_campus_rows_come_before_the_experienced_hire_inventory(stub_verify):
    """THE STARVATION. 12 `other` rows queued ahead of 3 campus rows by
    arrival order; a 3-row budget used to reach none of the campus ones."""
    firm = _firm()
    for i in range(12):
        _row(firm, i, bucket="other", checked_days_ago=None)
    for i in range(3):
        _row(firm, 100 + i, bucket="internship", checked_days_ago=None)

    reached = _reached(3, stub_verify)
    assert len(reached) == 3
    assert all("/internship/" in url for url in reached)


def test_every_campus_bucket_counts_not_just_internships(stub_verify):
    """`insight` and `entry_level` are campus too — `classify.TARGET_BUCKETS`
    is the one definition, and this reads it rather than restating it."""
    firm = _firm()
    for i in range(5):
        _row(firm, i, bucket="other")
    _row(firm, 100, bucket="insight")
    _row(firm, 101, bucket="entry_level")
    _row(firm, 102, bucket="internship")

    reached = _reached(3, stub_verify)
    assert not any("/other/" in url for url in reached)


def test_within_campus_the_nearest_deadline_goes_first(stub_verify):
    """A posting closing on Friday is the one a student is about to act on."""
    firm = _firm()
    today = date.today()
    _row(firm, 1, bucket="internship", deadline=today + timedelta(days=90))
    _row(firm, 2, bucket="internship", deadline=today + timedelta(days=2))
    _row(firm, 3, bucket="internship", deadline=today + timedelta(days=30))

    reached = _reached(3, stub_verify)
    assert [u.rsplit("/", 1)[1] for u in reached] == ["2", "3", "1"]


def test_a_dated_row_outranks_an_undated_one(stub_verify):
    firm = _firm()
    _row(firm, 1, bucket="internship", deadline=None)
    _row(firm, 2, bucket="internship", deadline=date.today() + timedelta(days=200))

    reached = _reached(2, stub_verify)
    assert reached[0].endswith("/2")


def test_last_verified_age_breaks_the_remaining_ties(stub_verify):
    """The old order's intent, kept as the tie-break it should always have
    been: among rows the first two keys cannot separate, the least recently
    confirmed goes first, and a row never confirmed at all goes before those."""
    firm = _firm()
    _row(firm, 1, bucket="internship", verified_days_ago=1)
    _row(firm, 2, bucket="internship", verified_days_ago=30)
    _row(firm, 3, bucket="internship", verified_days_ago=None)

    reached = _reached(3, stub_verify)
    assert [u.rsplit("/", 1)[1] for u in reached] == ["3", "2", "1"]


def test_the_budget_and_the_staleness_cutoff_are_unchanged(stub_verify):
    """The reorder must not become a volume increase: a run still checks at
    most `--limit` rows, and still only rows past the cutoff."""
    firm = _firm()
    for i in range(10):
        _row(firm, i, bucket="internship", checked_days_ago=None)
    # Checked yesterday: inside the 7-day cutoff, so not a candidate at all,
    # campus bucket or not.
    for i in range(5):
        _row(firm, 200 + i, bucket="internship", checked_days_ago=1)

    reached = _reached(4, stub_verify)
    assert len(reached) == 4
    assert not any(url.rsplit("/", 1)[1].startswith("20") for url in reached)


def test_other_rows_still_get_the_budget_the_campus_set_does_not_use(stub_verify):
    """Nothing is starved by this that was not already being starved. Once
    the campus candidates are exhausted the run flows straight into the
    rest — the queue is reordered, not partitioned."""
    firm = _firm()
    _row(firm, 1, bucket="internship")
    for i in range(5):
        _row(firm, 100 + i, bucket="other")

    reached = _reached(4, stub_verify)
    assert len(reached) == 4
    assert sum(1 for u in reached if "/other/" in u) == 3
