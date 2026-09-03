"""Tests for `manage.py reclassify` — the backfill pass that re-derives
bucket/cohort/class_year/region for rows already in the shared table.

Runs entirely against pytest-django's isolated test database (a fresh,
throwaway database per test run, distinct from any developer's live
worktree data) — never the real corpus. See the module's own docstring for
why: it writes to shared, unowned rows, so a correctness bug here has no
per-user blast radius to contain it.

`BOARDS` is monkeypatched to a small synthetic catalog for the collision
tests, so they pin the GENERAL rule (`campus_hint_pairs` requires unanimous
agreement) rather than depending on today's live catalog shape — see
`test_stress_classify.py::test_live_catalog_collisions_stay_unhinted` for the
complementary test that pins the two real firms currently affected.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from directory.management.commands import reclassify as reclassify_mod
from directory.models import Firm, Opportunity


pytestmark = pytest.mark.django_db


class _FakeBoard:
    def __init__(self, provider, **kw):
        self.provider = provider
        for k, v in kw.items():
            setattr(self, k, v)


def _firm(slug, name=None):
    return Firm.objects.create(slug=slug, name=name or slug, status="active")


def _opp(firm, title, *, url=None, source="", bucket="", **extra):
    return Opportunity.objects.create(
        firm=firm, title=title, url=url or f"https://x/{firm.slug}/{title}",
        source=source, status="open", bucket=bucket, **extra,
    )


def test_reclassify_is_idempotent(monkeypatch):
    """A second run over rows the first run already fixed reports zero
    further changes — the command's own docstring's central promise."""
    monkeypatch.setattr(reclassify_mod, "BOARDS", [])
    firm = _firm("acme")
    _opp(firm, "2027 Summer Analyst Program", bucket="")
    _opp(firm, "Senior Vice President, Coverage", bucket="")

    call_command("reclassify")
    first_pass_buckets = list(Opportunity.objects.order_by("id").values_list("bucket", flat=True))
    assert first_pass_buckets == ["internship", "other"]

    call_command("reclassify")
    second_pass_buckets = list(Opportunity.objects.order_by("id").values_list("bucket", flat=True))
    assert second_pass_buckets == first_pass_buckets


def test_reclassify_dry_run_persists_nothing(monkeypatch):
    monkeypatch.setattr(reclassify_mod, "BOARDS", [])
    firm = _firm("acme")
    _opp(firm, "2027 Summer Analyst Program", bucket="")

    call_command("reclassify", "--dry-run")

    o = Opportunity.objects.get()
    assert o.bucket == "", "a dry run must not write, even inside its own rolled-back transaction"


def test_reclassify_promotes_neutral_titles_only_on_an_unambiguous_campus_board(monkeypatch):
    """The regression this audit fixed: a firm running one campus board and
    one non-campus board on the SAME provider must not have its non-campus
    board's neutral titles promoted to entry_level, because `reclassify`
    only knows the stored row's provider, not which specific board it came
    from."""
    monkeypatch.setattr(reclassify_mod, "BOARDS", [
        ("acme", _FakeBoard("greenhouse", token="acme-students-graduates")),
        ("acme", _FakeBoard("greenhouse", token="acme-professionals")),
    ])
    firm = _firm("acme")
    neutral = _opp(firm, "Investment Banking Associate - Technology", source="greenhouse")

    call_command("reclassify")

    neutral.refresh_from_db()
    assert neutral.bucket == "other", (
        "an ambiguous (firm, provider) pair must never promote a neutral "
        "title — see classify.campus_hint_pairs"
    )


def test_reclassify_still_promotes_neutral_titles_on_an_unambiguous_campus_board(monkeypatch):
    """The positive case, so the fix above is proven to be about ambiguity
    specifically and not a blanket regression that stops promotion
    altogether."""
    monkeypatch.setattr(reclassify_mod, "BOARDS", [
        ("beta", _FakeBoard("workday", site="Beta_Campus_Careers")),
    ])
    firm = _firm("beta")
    neutral = _opp(firm, "Investment Banking Associate - Technology", source="workday")

    call_command("reclassify")

    neutral.refresh_from_db()
    assert neutral.bucket == "entry_level"


def test_reclassify_never_downgrades_a_senior_title_regardless_of_hint(monkeypatch):
    monkeypatch.setattr(reclassify_mod, "BOARDS", [
        ("beta", _FakeBoard("workday", site="Beta_Campus_Careers")),
    ])
    firm = _firm("beta")
    senior = _opp(firm, "Vice President, Fund Finance", source="workday")

    call_command("reclassify")

    senior.refresh_from_db()
    assert senior.bucket == "other"


# ---------------------------------------------------------------------------
# The catch-up half of the Workday `bulletFields` read. Ingest fills the
# location on the way in, but 429 open rows are already stored blank beside a
# payload that names their office, and they should not have to wait for their
# board's next scrape to answer a Region filter.
# ---------------------------------------------------------------------------

def test_reclassify_fills_a_blank_location_from_the_stored_payload():
    firm = _firm("raymondjames", "Raymond James")
    row = _opp(firm, "2027 Equity Research Associate", source="workday",
               location="", region="",
               raw={"bulletFields": ["Saint Petersburg, Florida - "
                                     "United States", "R-0012398"]})

    call_command("reclassify")

    row.refresh_from_db()
    assert row.location == "Saint Petersburg, Florida - United States"
    assert row.region == "us"


def test_reclassify_never_overwrites_a_location_the_connector_reported():
    """Fill-only, the same contract ingest keeps. A tenant that states its
    place and also lists a different one in its display bullets keeps the
    stated one."""
    firm = _firm("raymondjames", "Raymond James")
    row = _opp(firm, "2027 Equity Research Associate", source="workday",
               location="Chicago, Illinois", region="",
               raw={"bulletFields": ["London - United Kingdom"]})

    call_command("reclassify")

    row.refresh_from_db()
    assert row.location == "Chicago, Illinois"
    assert row.region == "us"


def test_reclassify_leaves_a_bullet_list_of_codes_alone():
    """The negative: nothing in these bullets names a place, so the row stays
    honestly blank rather than showing a requisition number as an office."""
    firm = _firm("pwc", "PwC")
    row = _opp(firm, "Assurance Associate", source="workday",
               location="", region="", raw={"bulletFields": ["566817WD"]})

    call_command("reclassify")

    row.refresh_from_db()
    assert row.location == ""
    assert row.region == ""
