"""The Firms step (onboarding step 3) and Settings -> Target Firms both used
to render every firm in the directory alphabetically, with no regard for the
regions and tracks the student had already declared one step earlier — a
US-only IB student saw Hong Kong quant funds and consulting shops with equal
prominence.

The fix is a REGROUP, not a filter: `accounts.views._split_by_declared_profile`
splits the same full list into "matches your profile" and "everything else",
never dropping a firm from either half. These tests assert on the split
function directly (fast, precise) and on the rendered onboarding/settings
pages (the actual seam a student sees).
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.views import _split_by_declared_profile
from crm.models import UserFirm
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def us_ib_student():
    return User.objects.create_user(
        email="us-ib@example.com", password="x", regions=["us"], tracks=["ib"],
    )


@pytest.fixture
def blank_student():
    return User.objects.create_user(email="blank@example.com", password="x")


@pytest.fixture
def firm_board():
    return {
        "jpm": Firm.objects.create(slug="jpmorgan", name="JPMorgan",
                                    regions=["us"], tracks=["ib"]),
        "sig": Firm.objects.create(slug="sig", name="SIG",
                                    regions=["hk"], tracks=["st"]),
        "bain": Firm.objects.create(slug="bain", name="Bain & Company",
                                     regions=["us"], tracks=["consulting"]),
        # No coverage info on file at all — must land in "everything else",
        # not be dropped for having nothing to match on.
        "unknown": Firm.objects.create(slug="mystery-co", name="Mystery Co"),
    }


# ---------------------------------------------------------------------------
# The split function itself
# ---------------------------------------------------------------------------
def test_a_declared_region_or_track_surfaces_the_overlapping_firm_first(us_ib_student, firm_board):
    """JPMorgan matches on both axes (us/ib) and Bain matches on region alone
    (us/consulting) — both surface. SIG (hk/st) and the firm with no coverage
    info on file match on neither and fall to "everything else"."""
    firms = list(Firm.objects.all().order_by("name"))
    matches, rest = _split_by_declared_profile(firms, us_ib_student)
    assert {f.slug for f in matches} == {"jpmorgan", "bain"}
    assert {f.slug for f in rest} == {"sig", "mystery-co"}


def test_a_region_only_match_and_a_track_only_match_both_count(us_ib_student, firm_board):
    """OR, not AND: SIG matches on neither axis and correctly sits out, but a
    firm that overlaps on just one of the two stated axes still belongs in
    the match group."""
    region_only = Firm.objects.create(slug="region-only", name="Region Only",
                                       regions=["us"], tracks=["pe"])
    track_only = Firm.objects.create(slug="track-only", name="Track Only",
                                      regions=["eu"], tracks=["ib"])
    firms = list(Firm.objects.filter(slug__in=["region-only", "track-only", "sig"]))
    matches, rest = _split_by_declared_profile(firms, us_ib_student)
    assert {f.slug for f in matches} == {"region-only", "track-only"}
    assert {f.slug for f in rest} == {"sig"}


def test_nothing_declared_yet_leaves_the_list_untouched(blank_student, firm_board):
    """A student who skipped (or hasn't reached) the profile questions gets
    exactly today's behaviour — one flat, alphabetical list — rather than an
    empty "matches" group sitting above it for no reason."""
    firms = list(Firm.objects.all().order_by("name"))
    matches, rest = _split_by_declared_profile(firms, blank_student)
    assert matches == []
    assert rest == firms


def test_the_split_never_drops_a_firm(us_ib_student, firm_board):
    """Whatever comes in must all come back out, across the two groups."""
    firms = list(Firm.objects.all().order_by("name"))
    matches, rest = _split_by_declared_profile(firms, us_ib_student)
    assert set(matches) | set(rest) == set(firms)
    assert len(matches) + len(rest) == len(firms)


# ---------------------------------------------------------------------------
# The onboarding Firms step, rendered
# ---------------------------------------------------------------------------
def test_onboarding_firms_step_groups_matches_before_the_rest(client, us_ib_student, firm_board):
    client.force_login(us_ib_student)
    body = client.get(reverse("accounts:onboarding") + "?step=firms").content.decode()
    assert "Matches your profile" in body
    assert "All firms" in body
    # Every firm is still reachable in the same response — nothing filtered.
    for name in ("JPMorgan", "SIG", "Bain", "Mystery Co"):
        assert name in body
    # And the match prints ahead of the non-matches in document order.
    assert body.index("JPMorgan") < body.index("SIG")
    assert body.index("JPMorgan") < body.index("Mystery Co")


def test_onboarding_firms_step_with_no_declared_profile_has_no_headings(client, blank_student, firm_board):
    client.force_login(blank_student)
    body = client.get(reverse("accounts:onboarding") + "?step=firms").content.decode()
    assert "Matches your profile" not in body
    for name in ("JPMorgan", "SIG", "Bain", "Mystery Co"):
        assert name in body


def test_a_previously_picked_firm_outside_the_profile_never_disappears(client, us_ib_student, firm_board):
    """SIG (HK/S&T) doesn't match this US/IB student's declared profile at
    all, but they targeted it already — re-rendering the step must still
    show it, still checked."""
    UserFirm(user=us_ib_student, firm=firm_board["sig"], tier=2).save()
    client.force_login(us_ib_student)
    body = client.get(reverse("accounts:onboarding") + "?step=firms").content.decode()
    assert "SIG" in body
    assert re.search(rf'value="{firm_board["sig"].id}"\s+checked', body)


# ---------------------------------------------------------------------------
# Settings -> Target Firms, rendered
# ---------------------------------------------------------------------------
def test_settings_target_firms_search_uses_the_same_grouping(client, us_ib_student, firm_board):
    client.force_login(us_ib_student)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert "Matches your profile" in body
    for name in ("JPMorgan", "SIG", "Bain", "Mystery Co"):
        assert name in body
    assert body.index("JPMorgan") < body.index("SIG")
