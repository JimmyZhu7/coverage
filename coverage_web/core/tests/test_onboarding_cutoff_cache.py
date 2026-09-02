"""`onboarding_cutoffs` is cached per scrape run.

`audit-perf-tests.md §1` defect 10: a sequential scan of 24,078 rows and 4,180
buffers, 5 to 21 ms, on EVERY feed render and EVERY Today render — for a value
that changes only when a scrape brings in new postings. Three live call sites
in `directory/views.py` plus one in `open_runs` itself paid it on every page.

The cache key carries the latest `ScrapeRun` id, which is what makes this safe
to leave on: a new scrape changes the key and every entry is invalid at once,
with nothing having to remember to clear anything. The two properties worth
pinning are exactly the two ways a cache like this goes wrong — it serves a
stale answer after the data moved, or it serves a DIFFERENT answer from the
uncached call.

This module lives in `core/tests/` rather than `directory/tests/` because it is
a guard on a shared seam, alongside `test_query_budgets.py` which measures what
the seam costs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from directory.models import Firm, Opportunity, ScrapeRun
from directory.open_runs import onboarding_cutoffs

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def firm(db):
    return Firm.objects.create(slug="cutoff-co", name="Cutoff Co",
                               regions=["us"], tracks=["ib"])


def _scrape():
    """`started` is not null and has no default: a ScrapeRun is a record of a
    run that happened, so it cannot exist without a start."""
    return ScrapeRun.objects.create(
        connector="all", status="ok", started=timezone.now())


def _posting(firm, days_ago, suffix=""):
    opp = Opportunity.objects.create(
        firm=firm, title=f"Summer Analyst {days_ago}{suffix}",
        bucket="internship", status="open", region="us",
        url=f"https://cutoff.test/{days_ago}{suffix}",
    )
    # `first_seen` is auto-set on insert, so the past has to be written after.
    Opportunity.objects.filter(pk=opp.pk).update(
        first_seen=timezone.now() - timedelta(days=days_ago))
    return opp


def test_a_cold_cache_returns_the_same_dict_as_an_uncached_call(firm):
    """The first property. A cache that answers differently from the function
    it is caching is not a cache, it is a second implementation."""
    _posting(firm, 30)
    _posting(firm, 10)
    _scrape()

    fresh = onboarding_cutoffs([firm.pk], fresh=True)
    cold = onboarding_cutoffs([firm.pk])
    warm = onboarding_cutoffs([firm.pk])

    assert cold == fresh
    assert warm == fresh
    assert fresh[firm.pk] == (timezone.now() - timedelta(days=30)).date()


def test_a_new_scrape_run_invalidates_the_cached_answer(firm):
    """The second property, and the whole reason the run id is in the key.

    Without it, a scrape that brought in an earlier posting would leave every
    surface reading a cutoff from before the new data, and `open_run_days`
    would print a duration for a row that is actually onboarding-batch: the
    one thing this module's docstring says it must never do.
    """
    _posting(firm, 30)
    _scrape()
    before = onboarding_cutoffs([firm.pk])
    assert before[firm.pk] == (timezone.now() - timedelta(days=30)).date()

    # A new posting, first seen earlier than anything held.
    _posting(firm, 60, suffix="b")
    _scrape()
    after = onboarding_cutoffs([firm.pk])
    assert after[firm.pk] == (timezone.now() - timedelta(days=60)).date()
    assert after != before


def test_a_different_firm_set_gets_a_different_entry(firm):
    """The bug a scrape-run-only key would have: two callers asking about
    different firms sharing one answer. The feed names thirty firms and the
    Today rail names four."""
    other = Firm.objects.create(slug="cutoff-two", name="Cutoff Two",
                                regions=["us"], tracks=["ib"])
    _posting(firm, 30)
    _posting(other, 5)
    _scrape()

    one = onboarding_cutoffs([firm.pk])
    both = onboarding_cutoffs([firm.pk, other.pk])
    assert set(one) == {firm.pk}
    assert set(both) == {firm.pk, other.pk}
    # Order must not matter: two callers naming the same firms differently
    # should share one entry rather than compute it twice.
    assert onboarding_cutoffs([other.pk, firm.pk]) == both


def test_the_unscoped_call_is_cached_too(firm):
    _posting(firm, 30)
    _scrape()
    assert onboarding_cutoffs() == onboarding_cutoffs(fresh=True)


def test_an_empty_firm_list_still_short_circuits(firm):
    """The pre-existing contract: asking about no firms is answered without
    touching the database at all, cache or no cache."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        assert onboarding_cutoffs([]) == {}
    assert len(captured) == 0


def test_a_new_posting_alone_invalidates_it_too(firm):
    """No scrape, just an insert. The process-local cache outlives a
    rolled-back transaction, so a key that watched only the ScrapeRun let two
    tests on a database with no scrape share one entry — which is how this
    was found (`directory/tests/test_open_runs.py::
    test_scoping_the_cutoff_to_some_firms_matches_the_unscoped_answer` went
    red in a full-directory run and green alone). It is also the real case: a
    fixture or a management command can insert a posting without a scrape."""
    _posting(firm, 30)
    before = onboarding_cutoffs([firm.pk])
    _posting(firm, 90, suffix="c")
    after = onboarding_cutoffs([firm.pk])
    assert after[firm.pk] == (timezone.now() - timedelta(days=90)).date()
    assert after != before


def test_the_warm_path_costs_two_index_lookups_instead_of_the_aggregate(firm):
    """What this bought. The warm read is two max-id lookups; the 24,078-row
    `TruncDate`/`Min` group-by is gone. Measured read-only on the founder's
    board (26,921 rows, 2026-09-02): 15.47 ms to 0.26 ms for his 54 target
    firms, 11.67 ms to 0.30 ms unscoped."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _posting(firm, 30)
    _scrape()
    onboarding_cutoffs([firm.pk])  # warm it

    with CaptureQueriesContext(connection) as captured:
        onboarding_cutoffs([firm.pk])
    assert len(captured) == 2, [q["sql"][:80] for q in captured]
