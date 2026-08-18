"""Tests for accounts/forms.py's vocabulary fixes: the region list sourced
from the same six-market vocabulary the Opportunities feed uses (B4), and
`ProfileForm.target_cycles`'s per-instance, never-silently-losing choices
(B1's form side + B3).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from accounts.forms import REGION_CHOICES, TRACK_CHOICES, ProfileForm
from directory.classify import (
    REGION_LABELS, TRACKED_REGIONS, TRACK_LABELS, TRACKED_TRACKS,
)
from directory.recommend import cycle_choices

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="forms@example.com", password="x")


# ---------------------------------------------------------------------------
# B4 — REGION_CHOICES must be the SAME six-market vocabulary the feed uses,
# not the narrower hk/us-only list `Firm.regions` happens to hold.
# ---------------------------------------------------------------------------

def test_region_choices_matches_the_feeds_six_market_vocabulary():
    """TRACKED_REGIONS, not REGION_ORDER. The facet's order gained "other"
    when stated-but-untracked locations got their own bucket, and "Other
    Markets" is a place a ROLE can be, never a place a student chooses to
    target — offering it here would ask someone to declare a preference for
    everywhere Coverage does not cover."""
    assert [code for code, _ in REGION_CHOICES] == list(TRACKED_REGIONS)
    assert len(REGION_CHOICES) == 6
    assert "other" not in {code for code, _ in REGION_CHOICES}
    for code, label in REGION_CHOICES:
        assert label == REGION_LABELS[code]


def test_a_student_can_now_state_a_preference_for_every_feed_market():
    """324 of 805 open campus roles sit in eu/sg/cn/jp — markets a student
    couldn't state a preference for while REGION_CHOICES was hk/us-only."""
    codes = {code for code, _ in REGION_CHOICES}
    assert {"eu", "sg", "cn", "jp"} <= codes


# ---------------------------------------------------------------------------
# TRACK_CHOICES must be the SAME vocabulary directory/views.py's Opportunities
# track filter uses (classify.TRACK_LABELS), not an independently hardcoded
# copy. Settings used to call the "pe" slug "Private Equity" while the filter
# called it "Private Equity / Credit" — same slug, two labels, on two pages a
# student reads back to back.
# ---------------------------------------------------------------------------

def test_track_choices_matches_the_filters_vocabulary():
    assert [code for code, _ in TRACK_CHOICES] == list(TRACKED_TRACKS)
    assert len(TRACK_CHOICES) == 6
    for code, label in TRACK_CHOICES:
        assert label == TRACK_LABELS[code]


def test_track_choices_calls_pe_private_equity_credit_not_just_private_equity():
    """The firms actually tagged "pe" (Apollo, Ares, Blue Owl, Golub Capital,
    HPS, Oaktree, Sixth Street among them) include credit shops, not just
    buyout funds — the fuller label is the accurate one, and it must be the
    SAME label the Opportunities filter shows for the identical slug."""
    labels = dict(TRACK_CHOICES)
    assert labels["pe"] == "Private Equity / Credit"


# ---------------------------------------------------------------------------
# B1 (form side) — the checkbox group's own vocabulary must be exactly what
# `parse_target_cycle` reads, because they're now the same source function.
# The leading ("", "Select a cycle") placeholder is dropped: it made sense as
# a <select>'s default option, not as a checkbox.
# ---------------------------------------------------------------------------

def test_profile_form_target_cycles_choices_match_cycle_choices(user):
    form = ProfileForm.from_user(user)
    assert list(form.fields["target_cycles"].choices) == [
        (v, label) for v, label in cycle_choices() if v
    ]


# ---------------------------------------------------------------------------
# B3 — stored values the current choices no longer list must not vanish
# silently: each must round-trip back into the rendered checkbox group,
# checked. Deliberately NOT disabled, unlike the old single-select's stale
# option: a disabled checkbox is dropped from the submitted form data
# entirely (unlike a <select>'s already-selected disabled option, which still
# posts), so disabling it here would recreate the exact silent-clear bug this
# machinery exists to prevent.
# ---------------------------------------------------------------------------

def test_a_stale_stored_cycle_value_appears_checked_not_dropped(user):
    user.target_cycles = ["sa2028_ib"]  # matches nothing this checkbox group has ever offered
    user.save(update_fields=["target_cycles"])

    form = ProfileForm.from_user(user)
    values = [v for v, _ in form.fields["target_cycles"].choices]
    assert "sa2028_ib" in values                  # visible, not silently dropped
    rendered = str(form["target_cycles"])
    assert 'value="sa2028_ib"' in rendered
    assert "checked" in rendered                  # pre-checked, so a no-op save keeps it
    assert "no longer offered" in rendered         # and visibly marked as such
    assert "disabled" not in rendered              # never disabled — see docstring above


def test_a_current_stored_cycle_value_is_not_marked_stale(user):
    current = cycle_choices()[1][0]  # any real, currently-offered choice
    user.target_cycles = [current]
    user.save(update_fields=["target_cycles"])

    form = ProfileForm.from_user(user)
    values = [v for v, _ in form.fields["target_cycles"].choices]
    assert values.count(current) == 1             # not duplicated as a "stale" entry
    rendered = str(form["target_cycles"])
    assert f'value="{current}"' in rendered
    assert "no longer offered" not in rendered


def test_a_blank_target_cycles_is_never_treated_as_stale(user):
    """The common case — nobody has picked a cycle yet — must not trigger
    the stale-value machinery."""
    form = ProfileForm.from_user(user)
    assert "no longer offered" not in str(form["target_cycles"])


def test_multiple_stored_cycles_all_round_trip_checked(user):
    """A student recruiting for two programmes at once must see BOTH boxes
    checked, not just the first."""
    a, b = cycle_choices()[1][0], cycle_choices()[2][0]
    user.target_cycles = [a, b]
    user.save(update_fields=["target_cycles"])

    form = ProfileForm.from_user(user)
    rendered = str(form["target_cycles"])
    assert rendered.count("checked") == 2
