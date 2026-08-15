"""What a firm page counts as "Open Roles", and whether it says so.

THE DEFECT. `firm_detail()` was the only user-facing queryset in the app that
filtered `status="open"` and stopped there. Every other surface — the feed,
the contacts board, Today, My Applications — scopes to `TARGET_BUCKETS`. On
live data that made /firms/barclays/ head its section "Open Roles 925" while
/app/contacts/ headed the same firm's tile "Barclays 13 Open", and 912 of the
925 were experienced requisitions grouped under a heading that read "Other"
and explained nothing.

These tests assert against RENDERED HTML rather than the view dict, because
the defect a reader hit was a heading and a missing sentence, not a count in
a context variable. A test that read `resp.context["total"]` would have gone
green on the old code the moment someone scoped the count without scoping the
rows, or vice versa.
"""

from __future__ import annotations

import pytest

from directory.classify import BUCKET_LABELS, OTHER
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _firm(slug="barclays", name="Barclays"):
    return Firm.objects.create(slug=slug, name=name)


def _opp(firm, n, *, bucket, title=None):
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", bucket=bucket, status="open",
        title=title or f"Role {n}",
    )


def _page(client, firm, qs=""):
    res = client.get(f"/firms/{firm.slug}/{qs}")
    assert res.status_code == 200
    return res.content.decode()


@pytest.fixture
def barclays(db):
    """13 campus rows and 912-in-miniature: the live shape, scaled down."""
    firm = _firm()
    for i in range(13):
        _opp(firm, i, bucket="internship", title=f"Summer Analyst {i}")
    for i in range(20):
        _opp(firm, 100 + i, bucket=OTHER,
             title=f"Active Directory Engineer {i}")
    return firm


# ---------------------------------------------------------------------------
# 1. The count is the campus count — the same number every other surface shows
# ---------------------------------------------------------------------------
def test_open_roles_counts_campus_only(client, barclays):
    body = _page(client, barclays)
    assert 'Open Roles <span class="scrub-count">13</span>' in body


def test_experienced_rows_are_not_rendered_by_default(client, barclays):
    body = _page(client, barclays)
    assert "Summer Analyst 0" in body
    assert "Active Directory Engineer 0" not in body


# ---------------------------------------------------------------------------
# 2. The page says what it is not showing, and links out of it
# ---------------------------------------------------------------------------
def test_default_scope_states_itself_and_offers_the_optin(client, barclays):
    body = _page(client, barclays)
    assert "Showing campus roles only" in body
    assert "20 experienced roles hidden" in body
    assert 'href="/firms/barclays/?role=all"' in body


def test_scope_line_is_silent_when_there_is_nothing_to_disclose(client):
    """A firm with no experienced rows has no scope gap, so it gets no
    sentence. The disclosure exists to explain a difference; printed
    unconditionally it would be noise on the majority of firms."""
    firm = _firm(slug="gs", name="Goldman Sachs")
    _opp(firm, 1, bucket="internship")
    body = _page(client, firm)
    assert "Showing campus roles only" not in body
    assert "experienced role" not in body


# ---------------------------------------------------------------------------
# 3. The opt-in is real: ?role=all shows everything and says so
# ---------------------------------------------------------------------------
def test_role_all_shows_everything_and_names_both_halves(client, barclays):
    body = _page(client, barclays, "?role=all")
    assert 'Open Roles <span class="scrub-count">33</span>' in body
    assert "Active Directory Engineer 0" in body
    assert "Showing everything we scraped" in body
    assert "13 campus, 20 experienced" in body


def test_role_other_shows_only_the_experienced_half(client, barclays):
    body = _page(client, barclays, "?role=other")
    assert 'Open Roles <span class="scrub-count">20</span>' in body
    assert "Summer Analyst 0" not in body
    assert "13 campus roles hidden" in body


def test_unrecognised_role_falls_back_to_campus(client, barclays):
    """`?role=banana` — and `?role=internship`, which firm detail does not
    offer — must land on the scope the page's own sentence describes, or the
    line and the rows disagree."""
    for value in ("banana", "internship"):
        body = _page(client, barclays, f"?role={value}")
        assert 'Open Roles <span class="scrub-count">13</span>' in body
        assert "Showing campus roles only" in body


# ---------------------------------------------------------------------------
# 4. The group heading names what it groups
# ---------------------------------------------------------------------------
def test_the_experienced_group_is_called_experienced(client, barclays):
    body = _page(client, barclays, "?role=all")
    assert "Experienced" in body
    assert BUCKET_LABELS[OTHER] == "Experienced"


# ---------------------------------------------------------------------------
# 5. An empty campus scope is not an empty firm
# ---------------------------------------------------------------------------
def test_no_campus_roles_says_so_without_claiming_the_firm_is_quiet(client):
    firm = _firm(slug="db", name="Deutsche Bank")
    for i in range(4):
        _opp(firm, i, bucket=OTHER)
    body = _page(client, firm)
    assert "No campus roles open right now." in body
    assert "Nothing open right now." not in body
    assert "4 experienced roles open here" in body
    assert 'href="/firms/db/?role=all"' in body


def test_a_genuinely_empty_firm_still_reads_as_empty(client):
    firm = _firm(slug="ms", name="Morgan Stanley")
    body = _page(client, firm)
    assert "Nothing open right now." in body


# ---------------------------------------------------------------------------
# 6. A row whose last check couldn't reconfirm it live is marked here too —
# firm_detail's own row is the second surface (besides the feed card) a
# student can leave Coverage from without opening the drawer.
# ---------------------------------------------------------------------------
def test_a_row_our_last_check_could_not_reconfirm_is_marked(client):
    import re
    from django.utils import timezone
    from datetime import timedelta

    firm = _firm(slug="hsbc", name="HSBC")
    o = _opp(firm, 1, bucket="internship", title="Unconfirmed Analyst")
    now = timezone.now()
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=now - timedelta(days=1), last_checked=now)

    body = _page(client, firm)
    assert "is-unconfirmed" in body
    assert "(Not recently confirmed live)" in body


def test_a_freshly_confirmed_row_wears_no_caution(client):
    import re
    from django.utils import timezone

    firm = _firm(slug="hsbc2", name="HSBC 2")
    o = _opp(firm, 1, bucket="internship", title="Confirmed Analyst")
    now = timezone.now()
    Opportunity.objects.filter(pk=o.pk).update(last_verified=now, last_checked=now)

    body = re.sub(r"<style.*?</style>", "", _page(client, firm), flags=re.S)
    assert "Confirmed Analyst" in body
    assert "is-unconfirmed" not in body
