"""WS-OPP-12 — the onboarding preview counts rows the feed blocks.

`_matching` applies the decidable half of the feed's eligibility rule (the
title-stated class year) and stops there, because the rest is a JSON walk per
row. So the panel's count was honest about what it filtered and silent about
what happens next, and what happens next is large: measured on the founder's
live profile 2026-09-02, 123 of the 308 rows the panel counted (40%) carry a
BLOCKING `_eligibility` verdict on the feed he is about to land on. The audit
measured the same shape at 141 of 357.

Second defect, same item: eleven preview rows showed firm-track pills that did
not include the row-level track they had been counted under, because the count
went through `_row_tracks` and the pills read `firm.tracks` (P5).
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.onboarding_preview import BLOCKED_SCAN_LIMIT, profile_preview
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _user(**kw):
    kw.setdefault("tracks", ["ib"])
    kw.setdefault("regions", ["us"])
    return get_user_model().objects.create_user(
        email="ob@example.com", password="pw12345!", **kw)


def _answers(user, **kw):
    base = {
        "live": False,
        "regions": list(user.regions or []),
        "tracks": list(user.tracks or []),
        "class_year": user.class_year,
        "work_auth": dict(user.work_authorization or {}),
        "firm_ids": [],
    }
    base.update(kw)
    return base


def _role(firm, title, **kw):
    kw.setdefault("status", "open")
    kw.setdefault("bucket", "internship")
    kw.setdefault("region", "us")
    kw.setdefault("first_seen", dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc))
    return Opportunity.objects.create(
        firm=firm, title=title,
        url=f"https://example.test/{firm.slug}/{title}".replace(" ", "-"),
        **kw,
    )


def test_the_footer_names_the_blocked_count_for_a_sponsorship_needing_profile():
    user = _user(work_authorization={"us": "sponsorship"})
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib"], regions=["us"])
    _role(firm, "2027 Summer Analyst Investment Banking A")
    _role(firm, "2027 Summer Analyst Investment Banking B", sponsorship="no")
    out = profile_preview(user, _answers(user))
    assert out["count"] == 2
    assert out["blocked"] == 1


def test_a_profile_with_nothing_to_block_renders_no_number():
    """P3: a student who has stated no class year and no work authorization
    has nothing to block, and the footer does not render."""
    user = _user()
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib"], regions=["us"])
    _role(firm, "2027 Summer Analyst Investment Banking A", sponsorship="no")
    out = profile_preview(user, _answers(user))
    assert out["count"] == 1
    assert not out["blocked"]


def test_no_matches_means_no_number():
    user = _user(work_authorization={"us": "sponsorship"})
    out = profile_preview(user, _answers(user))
    assert out["count"] == 0
    assert out["blocked"] is None


def test_a_set_past_the_scan_limit_is_not_estimated():
    """P1: a proportion measured on the first 400 of a 2,700-row set ordered
    by deadline is a fact about early deadlines, not about the set. Past the
    cap the count is not shown at all rather than scaled up."""
    assert BLOCKED_SCAN_LIMIT == 400
    user = _user(work_authorization={"us": "sponsorship"})
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib"], regions=["us"])
    Opportunity.objects.bulk_create([
        Opportunity(
            firm=firm, title=f"2027 Summer Analyst Investment Banking {i}",
            url=f"https://example.test/gs/{i}", status="open",
            bucket="internship", region="us", sponsorship="no",
            first_seen=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        for i in range(BLOCKED_SCAN_LIMIT + 1)
    ])
    out = profile_preview(user, _answers(user))
    assert out["count"] == BLOCKED_SCAN_LIMIT + 1
    assert out["blocked"] is None


def test_the_pills_name_the_rows_own_track_not_the_firms():
    """The count goes through `_row_tracks`; so does the pill now. Before
    this, a row counted under `am` printed its employer's `ib` pills."""
    user = _user(tracks=["am"])
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib", "am"], regions=["us"])
    _role(firm, "2027 Asset Management Summer Analyst")
    out = profile_preview(user, _answers(user))
    assert out["count"] == 1
    pills = out["rows"][0]["tracks"]
    assert "Asset Management" in pills
    assert "Investment Banking" not in pills


def test_the_wizard_renders_the_line(client):
    user = _user(work_authorization={"us": "sponsorship"})
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs",
                               tracks=["ib"], regions=["us"])
    _role(firm, "2027 Summer Analyst Investment Banking A")
    _role(firm, "2027 Summer Analyst Investment Banking B", sponsorship="no")
    client.force_login(user)
    body = client.get(
        reverse("accounts:onboarding_preview") + "?step=profile"
    ).content.decode()
    assert "1 of which your Settings rule out." in body
