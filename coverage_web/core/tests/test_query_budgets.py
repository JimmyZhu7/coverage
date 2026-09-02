"""Query budgets on the pages that had none.

`audit-perf-tests.md §5` counted the guards this suite actually has and found
two, both on Today, both scaling contacts and target firms. Meanwhile the
Opportunities feed ran 25 to 35 queries with no ceiling at all, `firm_detail`
9, `contact_list` 18, `contact_detail` 9, Settings 28 — and the live
1,332-query N+1 the same audit found scaled with OPEN CAMPUS ROLES, which is
precisely the axis nothing was watching. It was found by a human reading a
query log at midnight. That is the job this file takes over.

TWO KINDS OF ASSERTION, and the difference is deliberate:

  * The COMPARATIVE tests are the real guard. Each renders a page against a
    board of 50 open roles and again against the same board holding 5, and
    asserts the count did not move. An N+1 cannot survive that whichever loop
    it hides in, and a new flat helper query does not trip it. Same shape as
    `directory/tests/test_role_people.py::
    test_one_query_covers_every_firm_asked_about`.
  * The CEILINGS are a coarse backstop, each measured on 2026-09-02 with the
    number written beside it (E3). They catch what a comparative test cannot:
    a page that is simply expensive at every size.

THE AXIS IS THE BOARD, NOT THE TENANT. The campus queryset behind both the
feed and Today's ribbon reads the SHARED zone — every open row at every firm,
not the signed-in student's own. So the two sizes here are two states of one
board measured by one user, rather than two users with differently sized
worlds. The first draft of this file made exactly that mistake and passed
with the N+1 reintroduced, because both users were looking at the same rows.

THE FIXTURE is module-scoped and built once outside the per-test transaction,
because `audit-perf-tests.md §5` item 4 asks for a budget fixture that does
not add to the suite's per-test setup cost. The test that shrinks the board
does it inside its own transaction, so the rollback puts it back. The
teardown is not optional: these rows are committed, and rows left behind
would be counted by every module that runs after this one.

THE TWO PROFILE FIELDS on the fixture user are load-bearing, not decoration.
`work_authorization` sends `directory.views._eligibility` down the visa
branch, which is what reads `directory.sponsorship.effective_sponsorship` ->
`opp.firm.sponsors` for every row whose own posting is silent; `class_year`
is what makes `crm.today._dashboard_context` run the eligibility count at
all. With neither, Today never reaches the code path that produced the 1,332
queries, and an earlier draft of this module sat green through a locally
reverted `select_related("firm")`. With both, that revert takes Today from 49
queries to 99 and this file goes red on three assertions.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

User = get_user_model()

# 5 firms, 10 open campus roles each, every posting SILENT on sponsorship and
# the campus fold on: the exact shape that produced the live N+1. The
# comparative test shrinks this to one role per firm and asserts nothing moved.
_FIRMS = 5
_ROLES_PER_FIRM = 10
_SMALL_ROLES_PER_FIRM = 1
_URL_PREFIX = "https://budget.test/"


@pytest.fixture(scope="module")
def budget_world(django_db_setup, django_db_blocker):
    from crm.models import Contact, Task, Touch, UserFirm
    from directory.models import Firm, Opportunity
    from django.utils import timezone

    with django_db_blocker.unblock():
        user = User.objects.create_user(
            email="budget-student@example.test", password="x",
            regions=["us"], tracks=["ib"],
            work_authorization={"us": "sponsorship"},
            class_year=2029,
        )
        firms, contact = [], None
        for f in range(_FIRMS):
            firm = Firm.objects.create(
                slug=f"budget-firm-{f}", name=f"Budget Firm {f}",
                regions=["us"], tracks=["ib"],
            )
            firms.append(firm)
            UserFirm.all_objects.create(
                user=user, firm=firm, tier=1, status="target")
            contact = Contact.all_objects.create(
                user=user, name=f"Budget Contact {f}", firm=firm,
                email=f"budget{f}@example.test",
            )
            Touch.all_objects.create(
                user=user, contact=contact, ts=timezone.now(),
                kind="outreach", channel="email",
            )
            Task.all_objects.create(user=user, title=f"Budget task {f}", firm=firm)
            for r in range(_ROLES_PER_FIRM):
                Opportunity.objects.create(
                    firm=firm, title=f"Summer Analyst {f}-{r}",
                    bucket="internship", status="open", region="us",
                    sponsorship="unknown",
                    url=f"{_URL_PREFIX}{f}/{r}",
                )

        yield {"user": user, "firms": firms, "contact": contact}

        Opportunity.objects.filter(url__startswith=_URL_PREFIX).delete()
        for model in (Touch, Task, UserFirm, Contact):
            model.all_objects.filter(user_id=user.pk).delete()
        Firm.objects.filter(slug__startswith="budget-firm-").delete()
        User.objects.filter(pk=user.pk).delete()


def _shrink_the_board(world):
    """Drop the board to one open role per firm, inside the caller's
    transaction so the rollback restores it."""
    from directory.models import Opportunity

    keep = []
    for firm in world["firms"]:
        keep += list(
            Opportunity.objects.filter(firm=firm, url__startswith=_URL_PREFIX)
            .order_by("pk")
            .values_list("pk", flat=True)[:_SMALL_ROLES_PER_FIRM]
        )
    Opportunity.objects.filter(url__startswith=_URL_PREFIX).exclude(
        pk__in=keep).delete()


def _count(user, url) -> int:
    """Steady-state cost of one GET.

    THE PAGE IS RENDERED TWICE and only the second is measured. Some pages do
    one-time lazy work on first sight of a user: Settings runs
    `billing.credits`' monthly grant, which is a SELECT, an INSERT and a
    savepoint pair on the first render of a billing period and nothing at all
    afterwards. Measured once, that made Settings look like it cost 8 queries
    more against a bigger board purely because the bigger board happened to be
    measured first — a comparative test that reports a false N+1 is worse than
    no comparative test, because the next reader spends an hour looking for a
    loop that is not there. This one is not measuring "first visit"; it is
    measuring what the page costs every time after.
    """
    client = Client()
    client.force_login(user)
    client.get(url)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, f"{url} returned {response.status_code}"
    return len(captured)


# Seven pages, seven ceilings: (name, measured 2026-09-02, ceiling). Every
# ceiling is its measurement plus roughly a quarter — enough room for a
# legitimate new helper query, not enough to hide a loop. Raising one needs
# the reason written here (E3).
_PAGES = [
    # The feed at campus scope: the star page, and the one whose row fetch ran
    # twice over 2,710 rows (audit-perf-tests.md §1 defect 2). The audit
    # measured 25 to 35 on live data against 23 here; the gap is the facets,
    # which read a directory this fixture keeps small.
    ("feed-campus", 23, 29),
    # ...and at `?role=all`, a different query PLAN rather than a bigger one:
    # the providers facet compiles to a DISTINCT over every open row. Its own
    # ceiling, so the two scopes cannot drift into one number.
    ("feed-all", 23, 29),
    # Today. Its two existing guards scale contacts and target firms; this is
    # the open-role axis that was missing when the page measured 1,397.
    ("today", 49, 61),
    # The Network list: audit measured 18 on live data (14 here, on five
    # contacts), no ceiling at all.
    ("network", 14, 18),
    # Settings: audit measured 28 against 27 here, no ceiling, and it runs
    # `merge.candidate_pairs` on every GET.
    ("settings", 27, 34),
    # One contact record: audit measured 9, matched here, no ceiling.
    ("contact", 9, 12),
    # A firm page: audit measured 9 against 8 here, no ceiling.
    ("firm", 8, 11),
]


def _url_for(name, world):
    return {
        "feed-campus": "/opportunities/",
        "feed-all": "/opportunities/?role=all",
        "today": reverse("crm:week"),
        "network": reverse("crm:contact_list"),
        "settings": reverse("accounts:settings"),
        "contact": reverse("crm:contact_detail", args=[world["contact"].pk]),
        "firm": reverse("directory:firm_detail", args=[world["firms"][0].slug]),
    }[name]


@pytest.mark.django_db
@pytest.mark.parametrize("name,measured,ceiling", _PAGES,
                         ids=[p[0] for p in _PAGES])
def test_the_page_stays_under_its_ceiling(budget_world, name, measured, ceiling):
    """The coarse half. A page expensive at EVERY size is invisible to the
    comparative test below, and this is what notices it."""
    count = _count(budget_world["user"], _url_for(name, budget_world))
    assert count <= ceiling, (
        f"{name} cost {count} queries against a ceiling of {ceiling} "
        f"(measured {measured} on 2026-09-02). Raising the ceiling is a "
        f"legitimate answer ONLY with the reason written beside the number "
        f"(E3); the first question is whether a loop got a query in it."
    )


@pytest.mark.django_db
@pytest.mark.parametrize("name", [p[0] for p in _PAGES])
def test_the_page_does_not_pay_per_open_role(budget_world, name):
    """THE budget that matters, and the one nothing in this suite had.

    Same firms, same contacts, same target-firm rows on both sides: 50 open
    campus roles against 5. If the count moves, the page is doing work per
    open role, and that is the defect that put Today at 1,397 queries on the
    founder's own data. Roles rather than firms is the point: Today's two
    existing guards already scale contacts and target firms, and nothing
    scaled the thing that grows every night the scraper runs.
    """
    url = _url_for(name, budget_world)
    large = _count(budget_world["user"], url)
    _shrink_the_board(budget_world)
    small = _count(budget_world["user"], url)
    assert large == small, (
        f"{name} cost {small} queries over {_SMALL_ROLES_PER_FIRM * _FIRMS} "
        f"open roles and {large} over {_ROLES_PER_FIRM * _FIRMS}, on the same "
        f"{_FIRMS} firms. Something in this page runs once per role. Hoist it; "
        f"do not budget for it."
    )
