"""Tests for accounts/forms.py's vocabulary fixes: the region list sourced
from the same six-market vocabulary the Opportunities feed uses (B4), and
`ProfileForm.target_cycle`'s per-instance, never-silently-losing choices
(B1's form side + B3).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from accounts.forms import REGION_CHOICES, ProfileForm
from directory.classify import REGION_LABELS, REGION_ORDER
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
    assert [code for code, _ in REGION_CHOICES] == list(REGION_ORDER)
    assert len(REGION_CHOICES) == 6
    for code, label in REGION_CHOICES:
        assert label == REGION_LABELS[code]


def test_a_student_can_now_state_a_preference_for_every_feed_market():
    """324 of 805 open campus roles sit in eu/sg/cn/jp — markets a student
    couldn't state a preference for while REGION_CHOICES was hk/us-only."""
    codes = {code for code, _ in REGION_CHOICES}
    assert {"eu", "sg", "cn", "jp"} <= codes


# ---------------------------------------------------------------------------
# B1 (form side) — the dropdown's own vocabulary must be exactly what
# `parse_target_cycle` reads, because they're now the same source function.
# ---------------------------------------------------------------------------

def test_profile_form_target_cycle_choices_match_cycle_choices(user):
    form = ProfileForm.from_user(user)
    assert list(form.fields["target_cycle"].choices) == cycle_choices()


# ---------------------------------------------------------------------------
# B3 — a stored value the current choices no longer list must not vanish
# silently: it must round-trip back into the rendered <select>, disabled,
# rather than being dropped with no trace.
# ---------------------------------------------------------------------------

def test_a_stale_stored_cycle_value_appears_disabled_not_dropped(user):
    user.target_cycle = "sa2028_ib"  # matches nothing this dropdown has ever offered
    user.save(update_fields=["target_cycle"])

    form = ProfileForm.from_user(user)
    values = [v for v, _ in form.fields["target_cycle"].choices]
    assert "sa2028_ib" in values                 # visible, not silently dropped
    rendered = str(form["target_cycle"])
    assert 'value="sa2028_ib"' in rendered
    assert "disabled" in rendered                # and marked as no longer offered


def test_a_current_stored_cycle_value_is_not_marked_stale(user):
    current = cycle_choices()[1][0]  # any real, currently-offered choice
    user.target_cycle = current
    user.save(update_fields=["target_cycle"])

    form = ProfileForm.from_user(user)
    values = [v for v, _ in form.fields["target_cycle"].choices]
    assert values.count(current) == 1            # not duplicated as a "stale" entry
    rendered = str(form["target_cycle"])
    assert f'value="{current}"' in rendered
    # No disabled option at all when nothing is stale.
    assert "disabled" not in rendered


def test_a_blank_target_cycle_is_never_treated_as_stale(user):
    """The common case — nobody has picked a cycle yet — must not trigger
    the disabled-stale-option machinery."""
    form = ProfileForm.from_user(user)
    assert "disabled" not in str(form["target_cycle"])
