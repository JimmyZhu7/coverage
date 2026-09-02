"""Tests for accounts/forms.py's vocabulary fixes: the region list sourced
from the same six-market vocabulary the Opportunities feed uses (B4), and
`ProfileForm.target_cycles`'s per-instance, never-silently-losing choices
(B1's form side + B3).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.forms import REGION_CHOICES, TRACK_CHOICES, ProfileForm
from directory.classify import (
    REGION_LABELS, RETIRED_TRACKS, SELECTABLE_TRACKS, TRACKED_REGIONS,
    TRACK_LABELS, TRACKED_TRACKS,
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
    """REWRITTEN for D-3 (2026-09-02). This used to read TRACKED_TRACKS and
    require six choices; the picker is now `SELECTABLE_TRACKS`, which is the
    storage vocabulary minus the retired `corp-strat`. The rule it pins is
    unchanged — the checkboxes and the Opportunities filter read one
    vocabulary and one set of labels — against the vocabulary that now says
    what a student may choose."""
    assert [code for code, _ in TRACK_CHOICES] == list(SELECTABLE_TRACKS)
    assert len(TRACK_CHOICES) == 5
    for code, label in TRACK_CHOICES:
        assert label == TRACK_LABELS[code]


def test_a_retired_track_is_not_offered_but_keeps_its_label():
    """D-3. `corp-strat` returned zero open rows in any bucket while nine
    firms carried it, so it stops being somewhere a student can say they
    want to work. It is not deleted: `FirmDate`'s check constraint is built
    from TRACKED_TRACKS, the nine firms keep their tag, and their cards
    still need a human name for it."""
    codes = {code for code, _ in TRACK_CHOICES}
    assert "corp-strat" in TRACKED_TRACKS
    assert "corp-strat" in TRACK_LABELS
    assert not codes & set(RETIRED_TRACKS)


def test_a_profile_holding_a_retired_track_degrades_to_the_rest(user):
    """The whole migration-free half of D-3: a stored `['ib', 'corp-strat']`
    reads as `['ib']`, an already-supported one-track state, and the row is
    not rewritten to make it so. The next Settings save drops the slug on
    its own, because the box that would have kept it is gone."""
    user.tracks = ["ib", "corp-strat"]
    user.save(update_fields=["tracks"])

    form = ProfileForm.from_user(user)
    assert form.initial["tracks"] == ["ib"]

    user.refresh_from_db()
    assert user.tracks == ["ib", "corp-strat"], "no row rewritten behind them"


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
    """REWRITTEN 2026-09-02. This used to assert that `sa2028_ib` is labelled
    "no longer offered", which conflated two states the student has to be
    able to tell apart. A cycle that has merely rolled off the dropdown still
    WORKS — `parse_target_cycle` reads the year out of the words, so the
    picks and the level gate score against it exactly as before. A cycle that
    does not parse does nothing at all, silently, and the demo account sat in
    that state for weeks under this very value. The label now says which,
    and this pins the unparseable half. See `accounts.forms._stale_cycle_
    label`."""
    user.target_cycles = ["sa2028_ib"]  # matches nothing this checkbox group has ever offered
    user.save(update_fields=["target_cycles"])

    form = ProfileForm.from_user(user)
    values = [v for v, _ in form.fields["target_cycles"].choices]
    assert "sa2028_ib" in values                  # visible, not silently dropped
    rendered = str(form["target_cycles"])
    assert 'value="sa2028_ib"' in rendered
    assert "checked" in rendered                  # pre-checked, so a no-op save keeps it
    # Marked as such, AND with the engine effect stated: this value scores
    # nothing, which "no longer offered" does not say.
    assert "not recognised, so it does not affect your matches" in rendered
    assert "disabled" not in rendered              # never disabled — see docstring above


def test_a_cycle_that_rolled_off_the_window_is_marked_offered_not_broken(user):
    """The other half of the same split. "2020 Summer Internship" is not in
    this year's dropdown and parses perfectly, so it still scores — saying it
    is "not recognised" would be a lie in the student's favour's opposite
    direction, telling them a working setting is dead."""
    user.target_cycles = ["2020 Summer Internship"]
    user.save(update_fields=["target_cycles"])

    rendered = str(ProfileForm.from_user(user)["target_cycles"])

    assert "no longer offered" in rendered
    assert "not recognised" not in rendered


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


def test_a_stale_cycle_can_still_be_saved_through_the_settings_page(client, user):
    """PINS A FIXED BUG. The stale checkbox is rendered ticked and ENABLED so
    the student can keep or drop it — but a BOUND ProfileForm has no
    `initial`, so the stale value was missing from `choices` at validation
    time and the whole profile save died on "Select a valid choice". Nothing
    on the page said which control was at fault; the profile was simply
    unsaveable. Caught on the demo account (`sa2028_ib`), 2026-08-25."""
    user.target_cycles = ["sa2028_ib"]
    user.school = "Before U"
    user.save(update_fields=["target_cycles", "school"])
    client.force_login(user)

    resp = client.post(
        reverse("accounts:settings"),
        {"section": "profile", "school": "After U", "school_emails": "",
         "class_year": "", "target_cycles": ["sa2028_ib"], "regions": [],
         "tracks": [], "timezone": ""},
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    user.refresh_from_db()
    assert user.school == "After U"
    assert user.target_cycles == ["sa2028_ib"]


def test_a_post_cannot_invent_a_cycle_for_itself(client, user):
    """The fix reads the STORED row, not the POST. An unlisted value the user
    does not already hold must still be refused."""
    client.force_login(user)
    resp = client.post(
        reverse("accounts:settings"),
        {"section": "profile", "school": "", "school_emails": "",
         "class_year": "", "target_cycles": ["made_up_cycle"], "regions": [],
         "tracks": [], "timezone": ""},
    )
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.target_cycles == []


# ---------------------------------------------------------------------------
# `school_emails` — the student's own institutional address(es).
#
# Read by exactly one thing (capture.discovery._own_institution_domains) and
# only to EXCLUDE. What matters here is that it round-trips honestly and
# refuses the two answers that would silently do nothing.
# ---------------------------------------------------------------------------

def _profile_post(**over):
    data = {"name": "", "school": "", "school_emails": "", "class_year": "",
            "target_cycles": [], "regions": [], "tracks": [], "timezone": ""}
    data.update(over)
    return data


def test_school_emails_round_trip_through_the_form(user):
    form = ProfileForm(_profile_post(school_emails="Jimmy@USC.edu"))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    # Lower-cased on the way in: the gate compares domains, and a stored
    # "USC.edu" would match nothing.
    assert user.school_emails == ["jimmy@usc.edu"]
    assert ProfileForm.from_user(user)["school_emails"].value() == "jimmy@usc.edu"


def test_school_emails_accepts_more_than_one(user):
    """A student can carry an undergrad address and a graduate one; the
    field must not assume exactly one."""
    form = ProfileForm(_profile_post(
        school_emails="a@usc.edu, b@marshall.usc.edu  c@lse.ac.uk"
    ))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["school_emails"] == [
        "a@usc.edu", "b@marshall.usc.edu", "c@lse.ac.uk",
    ]


def test_school_emails_deduplicates(user):
    form = ProfileForm(_profile_post(school_emails="a@usc.edu, A@usc.edu"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["school_emails"] == ["a@usc.edu"]


def test_school_emails_refuses_freemail_out_loud(user):
    """Storing gmail.com here would be a setting the engine ignores — the
    freemail guard in `_own_institution_domains` drops it. Say why instead
    of saving a no-op."""
    form = ProfileForm(_profile_post(school_emails="jimmy@gmail.com"))
    assert not form.is_valid()
    assert "personal email provider" in str(form.errors["school_emails"])


def test_school_emails_refuses_a_bare_domain(user):
    """The stored value is an ADDRESS. A bare domain is a different answer to
    a different question, and taking it silently would leave the student
    unsure which one the box wanted."""
    form = ProfileForm(_profile_post(school_emails="usc.edu"))
    assert not form.is_valid()
    assert "email address" in str(form.errors["school_emails"])


def test_school_emails_blank_stays_blank(user):
    user.school_emails = ["old@usc.edu"]
    user.save(update_fields=["school_emails"])
    form = ProfileForm(_profile_post(school_emails=""))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert user.school_emails == []
