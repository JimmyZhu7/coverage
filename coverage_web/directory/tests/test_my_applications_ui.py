"""My Applications' funnel rail, urgency bands, and overlap affordances.

The page shows a role twice on purpose: the five funnel stages partition the
tracked rows, and the two deadline lenses are cross-sections of those same rows.
That is a deliberate design decision, and the thing it costs is a student who
miscounts their own pipeline. So the honesty affordances that pay for it are
behaviour, not decoration, and they are pinned here:

  * the "N of M live" fractions, so a lens never reads as extra roles;
  * a link from every lens row down to the ONE stage it is counted in, so the
    overlap can be proven rather than merely asserted in prose;
  * urgency carried by a word as well as a colour, so the "act now" rows are
    still act-now rows to a reader who cannot see the red.

The funnel rail is here for the same reason: a stage holding nothing used to
vanish, which made a five-stage pipeline look like whatever subset happened to
be occupied.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.models import UserOpportunity
from directory.models import Firm, Opportunity
from directory.views import _urgency_band

from .test_tracking import _user

TODAY = timezone.localdate()


# ---------------------------------------------------------------------------
# The urgency band — a pure function, so it is tested as one.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days, key, label", [
    (0, "now", "act now"),
    (1, "now", "act now"),
    (2, "now", "act now"),
    (3, "soon", "this week"),
    (7, "soon", "this week"),
    (8, "later", ""),
    (90, "later", ""),
])
def test_urgency_bands_split_where_the_styling_splits(days, key, label):
    assert _urgency_band(days) == {"key": key, "label": label}


def test_a_rolling_role_has_no_urgency_band_at_all():
    """No posted deadline means no countdown to be urgent about. It must not
    fall into the least-urgent band and pick up that band's styling."""
    assert _urgency_band(None)["key"] == "none"


def test_every_urgent_band_carries_a_word_not_just_a_colour():
    """The colour-only rule. Red and amber rows are distinguishable without
    colour because each states its own urgency in text."""
    assert _urgency_band(1)["label"] and _urgency_band(5)["label"]
    assert _urgency_band(1)["label"] != _urgency_band(5)["label"]


# ---------------------------------------------------------------------------
# The page.
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline(client, db):
    """A pipeline with a deliberate hole in it: Saved and Applied are occupied,
    Offer is empty. The empty stage is the interesting one."""
    firm = Firm.objects.create(name="Evercore", slug="evercore")

    def opp(n, days):
        return Opportunity.objects.create(
            firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
            bucket="internship", status="open",
            deadline=None if days is None else TODAY + timedelta(days=days),
        )

    user = _user()
    rows = {
        "urgent": (opp(1, 1), "saved"),
        "soon": (opp(2, 5), "saved"),
        "rolling": (opp(3, None), "submitted"),
        "done": (opp(4, 2), "closed"),
    }
    for o, status in rows.values():
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status=status)
    client.force_login(user)
    return rows


@pytest.mark.django_db
def test_every_stage_reaches_the_template_including_the_empty_ones(client, pipeline):
    """The funnel rail draws all five. A stage that disappears when empty is
    indistinguishable from a stage that does not exist."""
    stages = client.get(reverse("my_applications")).context["stages"]

    assert [s["key"] for s in stages] == [
        "saved", "submitted", "interview", "offer", "closed",
    ]
    by_key = {s["key"]: s for s in stages}
    assert by_key["offer"]["count"] == 0
    assert by_key["saved"]["count"] == 2


@pytest.mark.django_db
def test_stage_bars_are_sized_against_the_busiest_stage(client, pipeline):
    """Hierarchy by size, not by colour: a stage holding twice as much gets
    twice the bar. An empty stage gets none of it."""
    by_key = {s["key"]: s for s in client.get(reverse("my_applications")).context["stages"]}

    assert by_key["saved"]["pct"] == 100          # 2 roles, the busiest
    assert by_key["submitted"]["pct"] == 50       # 1 role
    assert by_key["offer"]["pct"] == 0


@pytest.mark.django_db
def test_an_empty_pipeline_does_not_divide_by_zero(client, db):
    """`pct` is a share of the busiest stage, and on a page with nothing
    tracked the busiest stage holds nothing."""
    client.force_login(_user())
    resp = client.get(reverse("my_applications"))

    assert resp.status_code == 200
    assert all(s["pct"] == 0 for s in resp.context["stages"])


@pytest.mark.django_db
def test_each_lens_row_links_to_the_stage_it_is_counted_in(client, pipeline):
    """The load-bearing overlap affordance. Prose can claim a row appears
    twice; a link that jumps to the other appearance settles it — so the
    anchor and its target must both exist."""
    body = client.get(reverse("my_applications")).content.decode()

    assert 'href="#stage-saved"' in body
    assert 'id="stage-saved"' in body


@pytest.mark.django_db
def test_the_live_fraction_survives(client, pipeline):
    """"1 of 3 live", never a bare "1" — the fraction is what stops a lens
    reading as extra roles on top of the funnel. Done is excluded from live."""
    resp = client.get(reverse("my_applications"))

    assert resp.context["total"] == 4
    assert resp.context["live_total"] == 3
    assert "of 3 live" in resp.content.decode()


@pytest.mark.django_db
def test_the_lens_fractions_account_for_every_live_row(client, db):
    """Every fraction on the band is written over the same denominator, which
    invites a subtraction. The band ran Closing Soon + Rolling only, so a live
    role dated 94 days out — or one whose date has already gone by — appeared
    in neither, and the arithmetic came up short with nothing on the page to
    explain the difference."""
    firm = Firm.objects.create(name="SIG", slug="sig")

    def opp(n, days):
        return Opportunity.objects.create(
            firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
            bucket="internship", status="open",
            deadline=None if days is None else TODAY + timedelta(days=days),
        )

    user = _user()
    for o, status in [(opp(1, 3), "saved"), (opp(2, 94), "submitted"),
                      (opp(3, None), "saved"), (opp(4, -5), "saved"),
                      (opp(5, 2), "closed")]:
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status=status)
    client.force_login(user)

    resp = client.get(reverse("my_applications"))
    lenses = resp.context["lenses"]
    by_key = {lens["key"]: lens for lens in lenses}

    assert resp.context["live_total"] == 4
    assert sum(len(lens["items"]) for lens in lenses) == 4, "no live row falls through"
    assert [i["title"] for i in by_key["closing"]["items"]] == ["Summer Analyst 1"]
    assert [i["title"] for i in by_key["later"]["items"]] == ["Summer Analyst 2"]
    assert [i["title"] for i in by_key["rolling"]["items"]] == ["Summer Analyst 3"]
    assert [i["title"] for i in by_key["passed"]["items"]] == ["Summer Analyst 4"]

    body = resp.content.decode()
    assert "Further Out" in body, "the residue is named on the page, not just in context"
    assert "Deadline Passed" in body
    assert "These two lists" not in body, "the intro cannot promise a count of lists"


@pytest.mark.django_db
def test_the_residue_lenses_stay_off_the_page_when_they_hold_nothing(client, pipeline):
    """Closing Soon and Rolling earn their empty state because it coaches. A
    permanent "Deadline Passed — 0 of 3 live" band coaches nothing, so it only
    appears when it has rows to show."""
    body = client.get(reverse("my_applications")).content.decode()

    assert "Closing Soon" in body
    assert "Further Out" not in body
    assert "Deadline Passed" not in body


@pytest.mark.django_db
def test_an_empty_lens_offers_a_way_out(client, db):
    """Empty states get a message AND an action. A student tracking only
    rolling roles sees an empty Closing Soon, and it must not be a blank
    region."""
    firm = Firm.objects.create(name="Evercore", slug="evercore")
    o = Opportunity.objects.create(
        firm=firm, url="https://x/1", title="Summer Analyst",
        bucket="internship", status="open", deadline=None,
    )
    user = _user()
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")
    client.force_login(user)

    body = client.get(reverse("my_applications")).content.decode()
    # The page carries its own <style> block, which also names the class — so
    # anchor on the rendered element, not on the first textual match.
    empty = body[body.index('<p class="apps-lens-empty">'):][:600]

    assert "closes in the next" in empty
    assert reverse("opportunities") in empty


@pytest.mark.django_db
def test_urgency_reaches_the_row_that_renders_it(client, pipeline):
    """The band is computed in the view so the template stays a renderer; this
    is the wiring between the two."""
    resp = client.get(reverse("my_applications"))
    closing = next(l for l in resp.context["lenses"] if l["key"] == "closing")
    bands = {i["title"]: i["urgency"]["key"] for i in closing["items"]}

    assert bands[pipeline["urgent"][0].title] == "now"
    assert bands[pipeline["soon"][0].title] == "soon"
    # Done is out of the lenses entirely, urgent deadline or not.
    assert pipeline["done"][0].title not in bands


@pytest.mark.django_db
def test_a_done_row_states_that_it_is_done(client, pipeline):
    """Terminal cards recede visually, but "finished" must not rest on the
    background tint alone."""
    body = client.get(reverse("my_applications")).content.decode()
    assert "Done, no longer live" in body


# ---------------------------------------------------------------------------
# Identity-duplicate folding — my_applications() must fold the same way
# Browse Openings does, without the trap that made a naive wiring collapse a
# real 13-row pipeline down to 1 (see directory/dupes.py's fold_duplicates
# and the round-8 dedup finding).
# ---------------------------------------------------------------------------

@pytest.fixture
def duplicate_pipeline(db):
    """Two Opportunity rows that are the same tal.net requisition filed under
    two candidate-pool addresses (identical firm + title + location), plus a
    THIRD, genuinely different tracked role at the same firm. The third role
    is the trap: a naive fold that keys on UserOpportunity (which has no
    firm/title/location of its own) collapses all three into one instead of
    folding only the true pair.
    """
    firm = Firm.objects.create(name="Bank of America", slug="bofa")
    dup_a = Opportunity.objects.create(
        firm=firm, url="https://bankcampuscareers.tal.net/pl/1/opp/14594",
        title="Campus Insight Forum", location="New York, NY",
        bucket="event", status="open", deadline=None,
    )
    dup_b = Opportunity.objects.create(
        firm=firm, url="https://bankcampuscareers.tal.net/pl/2/opp/14594",
        title="Campus Insight Forum", location="New York, NY",
        bucket="event", status="open", deadline=None,
    )
    other = Opportunity.objects.create(
        firm=firm, url="https://bankcampuscareers.tal.net/pl/1/opp/9999",
        title="Summer Analyst, Global Markets", location="New York, NY",
        bucket="internship", status="open", deadline=None,
    )
    user = _user()
    return user, dup_a, dup_b, other


@pytest.mark.django_db
def test_a_tracked_identity_duplicate_folds_to_one_row(client, duplicate_pipeline):
    """The bug: tracking both addresses of the same posting showed it twice
    and inflated the funnel total. Folding must remove exactly the one
    duplicate, leaving the unrelated tracked role untouched."""
    user, dup_a, dup_b, other = duplicate_pipeline
    for o in (dup_a, dup_b, other):
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")
    client.force_login(user)

    resp = client.get(reverse("my_applications"))

    assert resp.context["total"] == 2
    titles = [i["title"] for s in resp.context["stages"] for i in s["items"]]
    assert titles.count("Campus Insight Forum") == 1
    assert titles.count("Summer Analyst, Global Markets") == 1


@pytest.mark.django_db
def test_folding_does_not_touch_a_real_13_row_pipeline(client, duplicate_pipeline):
    """The regression this fix exists to prevent: passing UserOpportunity
    rows (or the calendar's plain dicts) straight into fold_duplicates keys
    every row to the same (None, '') bucket and discards all but one
    tracked role. Ten genuinely distinct tracked roles, plus the one
    duplicate pair, must survive as eleven — not collapse to one."""
    user, dup_a, dup_b, other = duplicate_pipeline
    firm = dup_a.firm
    UserOpportunity.all_objects.create(user=user, opportunity=dup_a, applied_status="saved")
    UserOpportunity.all_objects.create(user=user, opportunity=dup_b, applied_status="saved")
    UserOpportunity.all_objects.create(user=user, opportunity=other, applied_status="saved")
    for n in range(9):
        o = Opportunity.objects.create(
            firm=firm, url=f"https://bankcampuscareers.tal.net/pl/1/opp/{5000 + n}",
            title=f"Distinct Role {n}", location="New York, NY",
            bucket="internship", status="open", deadline=None,
        )
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")
    client.force_login(user)

    resp = client.get(reverse("my_applications"))

    assert resp.context["total"] == 11


@pytest.mark.django_db
def test_progressed_stage_wins_the_fold_over_the_untouched_copy(client, duplicate_pipeline):
    """If a student applied on one duplicate address and only saved the
    other, the applied copy is the one worth keeping — not whichever
    fold_duplicates' own (deadline/location/first_seen/id) tie-break would
    have picked."""
    user, dup_a, dup_b, other = duplicate_pipeline
    UserOpportunity.all_objects.create(user=user, opportunity=dup_a, applied_status="saved")
    UserOpportunity.all_objects.create(user=user, opportunity=dup_b, applied_status="submitted")
    UserOpportunity.all_objects.create(user=user, opportunity=other, applied_status="saved")
    client.force_login(user)

    by_key = {s["key"]: s for s in client.get(reverse("my_applications")).context["stages"]}

    assert by_key["submitted"]["count"] == 1
    assert by_key["submitted"]["items"][0]["title"] == "Campus Insight Forum"
    assert by_key["saved"]["count"] == 1
    assert by_key["saved"]["items"][0]["title"] == "Summer Analyst, Global Markets"
