"""WS-OPP-11 — the weight calibration command.

Sixteen weights, none of them backed by a measured outcome
(`audit-personalization-opportunities.md §Q8`). This command is the "what
would change it" half of P6 made runnable: it scores every role the student
saved, applied to or dismissed, prints each axis's contribution and the rank
the role would have held on the whole board, and ends with a per-axis verdict
that says "no measured justification" wherever the sample cannot speak.

Run against the founder read-only on 2026-09-02: n=18 (4 applied, 1 saved, 13
dismissed) over 2,737 open campus roles. `track_fit` was the only axis that
separated the two labels (+16.4 acted-on vs +4.0 dismissed); `class_fit` was
constant at +30 across all 18 rows and is therefore unmeasurable from this
sample; `tier_fit`, `region_fit` and `network_fit` did not separate, two of
them pointing the wrong way. The findings live in `recommend.py`'s docstring.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from analytics.models import UserOpportunity
from crm.models import UserFirm
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _world():
    user = get_user_model().objects.create_user(
        email="calib@example.com", password="pw12345!",
        tracks=["ib"], regions=["us"], class_year=2029,
    )
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib"], regions=["us"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    rows = []
    for i in range(3):
        rows.append(Opportunity.objects.create(
            firm=firm, title=f"2027 Summer Analyst, Investment Banking {i}",
            url=f"https://example.test/gs/{i}", status="open",
            bucket="internship", region="us",
            first_seen=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        ))
    UserOpportunity.all_objects.create(user=user, opportunity=rows[0],
                                       applied_status="submitted")
    UserOpportunity.all_objects.create(user=user, opportunity=rows[1])
    UserOpportunity.all_objects.create(user=user, opportunity=rows[2],
                                       dismissed=True)
    return user


def _run(email="calib@example.com"):
    out = StringIO()
    call_command("calibrate_recommender", email=email, stdout=out, stderr=out)
    return out.getvalue()


def test_it_prints_the_sample_size_the_next_reader_needs():
    _world()
    body = _run()
    assert "Labelled examples (UserOpportunity rows): 3" in body
    assert "'applied': 1" in body
    assert "'saved': 1" in body
    assert "'dismissed': 1" in body


def test_it_prints_a_per_axis_contribution_for_every_row():
    _world()
    body = _run()
    for axis in ("tier_fit", "track_fit", "region_fit", "class_fit",
                 "network_fit"):
        assert axis in body


def test_it_prints_the_rank_each_row_would_have_had():
    _world()
    body = _run()
    assert "#1 of 3" in body


def test_a_constant_axis_is_reported_as_unjustified():
    """The finding on a sample this size, stated plainly. An axis that never
    varies cannot be calibrated by the sample, and a number that says it is
    unjustified is worth more than one that quietly looks justified."""
    _world()
    body = _run()
    assert "no measured justification" in body


def test_it_writes_nothing():
    """`--dry-run` is the only mode. There is no `--apply` to forget."""
    _world()
    with CaptureQueriesContext(connection) as ctx:
        _run()
    writes = [
        q for q in ctx.captured_queries
        if q["sql"].strip().split(" ", 1)[0].upper()
        in ("INSERT", "UPDATE", "DELETE")
    ]
    assert writes == []


def test_an_unknown_account_says_so_rather_than_raising():
    out = _run("nobody@example.com")
    assert "No account for" in out


def test_an_account_with_nothing_to_calibrate_against_says_so():
    get_user_model().objects.create_user(
        email="empty@example.com", password="pw12345!")
    assert "Nothing to calibrate against." in _run("empty@example.com")
