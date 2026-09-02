"""WS-OPP-17 — role-level pagination for the feed.

WHAT THE RE-MEASUREMENT FOUND, and why this file tests a different change
from the one the plan item's body describes. `audit-perf-tests.md §1` measured
`/opportunities/?role=all` at 7.5 MB and the item proposed paging the columns
"the way the campus scope already does". Measured 2026-09-02 before touching
anything: both scopes were ALREADY column-paged at `COLS_PAGE` = 12 and the
page was still 5.27 MB. The weight was the roles inside the columns, not the
column count — 4,077 top-level cards across those 12 at `role=all`, with 1,382
in the heaviest single column, and 1,114 across 12 at campus scope with 656 in
one. So the cap is on roles per column, at both scopes.

After: 0.68 MB at `role=all`, 0.67 MB at campus.
"""

from __future__ import annotations

import datetime as dt

import pytest

from directory.models import Firm, Opportunity
from directory.views import ROLES_PER_COLUMN, _cap_roles_per_column

pytestmark = pytest.mark.django_db


def _column(n_heads, *, variants_on_first=0, open_count=None):
    roles = [{"id": i, "title": f"Role {i}"} for i in range(n_heads)]
    if variants_on_first and roles:
        roles[0]["variants"] = [
            {"id": 1000 + j, "in_group": True} for j in range(variants_on_first)
        ]
        roles.extend(roles[0]["variants"])
    return {
        "roles": roles,
        "open_count": open_count if open_count is not None
        else n_heads + variants_on_first,
    }


def test_a_short_column_is_untouched():
    cl = _column(5)
    _cap_roles_per_column([cl])
    assert len(cl["roles"]) == 5
    assert cl["roles_hidden"] == 0


def test_a_long_column_is_capped_and_says_how_many_it_hid():
    cl = _column(100)
    _cap_roles_per_column([cl])
    assert len(cl["roles"]) == ROLES_PER_COLUMN
    assert cl["roles_hidden"] == 100 - ROLES_PER_COLUMN


def test_a_surviving_head_keeps_its_whole_city_family():
    """Siblings render inside their family head's disclosure, so counting
    them against the cap would make the column's length depend on how many
    cities a programme happens to run in."""
    cl = _column(50, variants_on_first=3)
    _cap_roles_per_column([cl])
    heads = [r for r in cl["roles"] if not r.get("in_group")]
    assert len(heads) == ROLES_PER_COLUMN
    assert len(heads[0]["variants"]) == 3
    # 24 heads + 3 siblings shown, out of 53 open.
    assert cl["roles_hidden"] == 53 - 27


def test_the_open_count_is_never_reduced():
    """P4: a student is shown fewer cards and told a smaller number never.
    The header's "N open", the stat strip and the facets all read the full
    count and are computed before this runs."""
    cl = _column(100)
    before = cl["open_count"]
    _cap_roles_per_column([cl])
    assert cl["open_count"] == before


def _board(n_roles, firm_slug="gs"):
    firm = Firm.objects.create(slug=firm_slug, name="Goldman Sachs")
    for i in range(n_roles):
        Opportunity.objects.create(
            firm=firm, title=f"2027 Summer Analyst {i}",
            url=f"https://example.test/{firm_slug}/{i}",
            status="open", bucket="internship",
            first_seen=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
    return firm


def test_the_feed_renders_the_cap_and_the_way_out(client):
    firm = _board(ROLES_PER_COLUMN + 9)
    body = client.get("/opportunities/").content.decode()
    assert f"9 more at {firm.name}" in body
    assert f"/firms/{firm.slug}/" in body


def test_a_board_under_the_cap_shows_no_overflow_line(client):
    _board(3)
    body = client.get("/opportunities/").content.decode()
    assert "more at Goldman Sachs" not in body


def test_the_show_more_columns_control_still_returns_the_next_slice(client):
    """The column sentinel is untouched by the role cap: it still walks the
    same `cols=` cursor and returns the remaining columns."""
    for i in range(14):
        _board(2, firm_slug=f"firm{i}")
    first = client.get("/opportunities/").content.decode()
    assert "cols=12" in first
    more = client.get("/opportunities/?cols=12", HTTP_HX_REQUEST="true")
    assert more.status_code == 200
    assert more.content.decode().count('<article class="firmcol') == 2


def test_the_feed_stays_within_its_query_budget(client, django_assert_max_num_queries):
    """The cap is a Python slice over rows already materialised, so it adds
    no query at either scope. 11 measured on live data 2026-09-02 for a
    signed-out visitor; the ceiling is that plus the two session/user lookups
    an authenticated request would add, and exists so a future edit cannot
    turn the overflow line into an N+1 over firms."""
    _board(ROLES_PER_COLUMN + 9)
    with django_assert_max_num_queries(13):
        client.get("/opportunities/")
    with django_assert_max_num_queries(13):
        client.get("/opportunities/?role=all")
