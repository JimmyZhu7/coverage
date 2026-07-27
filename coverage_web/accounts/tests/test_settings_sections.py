"""Tests for the four independently-saving settings sections — Outreach
Assets, Work Authorization, Cadence, Weekly Pace — and the two onboarding
steps that reuse two of them.

The through-line of almost every case here is the same product rule: a blank
answer must leave the underlying value UNSET, not defaulted. `work_authorization`
feeds `scoring.needs_sponsorship`, which scores a missing region as neutral;
a guessed default would move every firm's structural score on a fact the user
never gave us. `cadence_params` and `weekly_touch_goal` fall back to the
documented defaults the same way. So "saving nothing" is a first-class
outcome, tested as deliberately as saving something.

The other recurring rule: `assets` and `cadence_params` are shared JSON
columns. Each form owns its own keys and must leave its siblings alone.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.forms import CadenceForm, WeeklyPaceForm
from crm.views import TUNABLE_CADENCE_PARAMS

User = get_user_model()

SETTINGS = "accounts:settings"
ONBOARDING = "accounts:onboarding"


@pytest.fixture
def user(db):
    return User.objects.create_user(email="sections@example.com", password="x")


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return user


def _post(client, **data):
    return client.post(reverse(SETTINGS), data)


# ---------------------------------------------------------------------------
# The page still renders every section
# ---------------------------------------------------------------------------
def test_settings_renders_the_new_sections(client, logged_in):
    resp = client.get(reverse(SETTINGS))
    assert resp.status_code == 200
    body = resp.content.decode()
    for anchor in ("work-auth", "assets", "cadence", "pace"):
        assert f'id="{anchor}"' in body
        assert f'href="#{anchor}"' in body  # rail entry


def test_cadence_section_shows_each_default_inline(client, logged_in):
    resp = client.get(reverse(SETTINGS))
    body = resp.content.decode()
    from coverage_domain.cadence import CADENCE_DEFAULTS

    for key in TUNABLE_CADENCE_PARAMS:
        assert f"Default: {CADENCE_DEFAULTS[key]}" in body


# ---------------------------------------------------------------------------
# Outreach Assets  (User.assets["angles"])
# ---------------------------------------------------------------------------
def test_assets_saves_one_angle_per_line_in_order(client, logged_in):
    resp = _post(
        client,
        section="assets",
        angles="London M&A boutique internship\nRowing at the same club\nMandarin",
    )
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == [
        "London M&A boutique internship",
        "Rowing at the same club",
        "Mandarin",
    ]


def test_assets_drops_blank_lines_and_surrounding_whitespace(client, logged_in):
    _post(client, section="assets", angles="  First angle  \n\n\n   \n Second angle\n")
    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == ["First angle", "Second angle"]


def test_assets_preserves_the_other_keys_in_the_dict(client, logged_in):
    logged_in.assets = {
        "languages": ["en", "zh"],
        "current_status": "Penultimate year",
        "advocate_target": 2,
    }
    logged_in.save(update_fields=["assets"])

    _post(client, section="assets", angles="Only angle")

    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == ["Only angle"]
    assert logged_in.assets["languages"] == ["en", "zh"]
    assert logged_in.assets["current_status"] == "Penultimate year"
    assert logged_in.assets["advocate_target"] == 2


def test_assets_blank_removes_the_key_rather_than_storing_an_empty_list(client, logged_in):
    logged_in.assets = {"angles": ["Old angle"], "advocate_target": 2}
    logged_in.save(update_fields=["assets"])

    _post(client, section="assets", angles="   \n\n")

    logged_in.refresh_from_db()
    # Absent, not [] — an empty list sitting in the column reads like an answer.
    assert "angles" not in logged_in.assets
    assert logged_in.assets["advocate_target"] == 2


def test_assets_rejects_an_absurd_number_of_angles(client, logged_in):
    too_many = "\n".join(f"angle {i}" for i in range(60))
    resp = _post(client, section="assets", angles=too_many)
    assert resp.status_code == 200  # re-rendered with errors, no redirect
    logged_in.refresh_from_db()
    assert logged_in.assets.get("angles") is None


def test_assets_round_trips_into_the_textarea(client, logged_in):
    _post(client, section="assets", angles="Alpha\nBeta")
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Alpha\nBeta" in body


# ---------------------------------------------------------------------------
# Work Authorization  (User.work_authorization)
# ---------------------------------------------------------------------------
def test_work_auth_saves_per_region(client, logged_in):
    resp = _post(client, section="work_auth", work_auth_us="citizen",
                 work_auth_hk="sponsorship")
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"us": "citizen", "hk": "sponsorship"}


def test_work_auth_blank_stores_nothing_for_that_region(client, logged_in):
    _post(client, section="work_auth", work_auth_us="citizen", work_auth_hk="")
    logged_in.refresh_from_db()
    # `hk` must be ABSENT, not "" and not a guessed value: scoring reads a
    # missing region as unknown and scores it neutral.
    assert logged_in.work_authorization == {"us": "citizen"}
    assert "hk" not in logged_in.work_authorization


def test_work_auth_blanking_an_answered_region_clears_it(client, logged_in):
    logged_in.work_authorization = {"us": "citizen", "hk": "citizen"}
    logged_in.save(update_fields=["work_authorization"])

    _post(client, section="work_auth", work_auth_us="citizen", work_auth_hk="")

    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"us": "citizen"}


def test_work_auth_rejects_a_value_outside_the_vocabulary(client, logged_in):
    resp = _post(client, section="work_auth", work_auth_us="green-card",
                 work_auth_hk="")
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {}


def test_work_auth_leaves_regions_the_form_does_not_render_alone(client, logged_in):
    # A region set elsewhere (admin, or one added to the directory later) is
    # not this form's to delete.
    logged_in.work_authorization = {"uk": "citizen"}
    logged_in.save(update_fields=["work_authorization"])

    _post(client, section="work_auth", work_auth_us="sponsorship", work_auth_hk="")

    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"uk": "citizen", "us": "sponsorship"}


# ---------------------------------------------------------------------------
# Cadence  (User.cadence_params)
# ---------------------------------------------------------------------------
def _cadence_post(**overrides) -> dict:
    """A full cadence POST: every whitelisted key, blank unless overridden."""
    data = {"section": "cadence"}
    data.update({key: "" for key in TUNABLE_CADENCE_PARAMS})
    data.update({k: str(v) for k, v in overrides.items()})
    return data


def test_cadence_saves_in_range_overrides(client, logged_in):
    resp = _post(client, **_cadence_post(followup_after_business_days=7,
                                         max_cold_touches=3))
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {
        "followup_after_business_days": 7,
        "max_cold_touches": 3,
    }


def test_cadence_clearing_an_input_removes_the_override(client, logged_in):
    logged_in.cadence_params = {"followup_after_business_days": 7,
                                "max_cold_touches": 3}
    logged_in.save(update_fields=["cadence_params"])

    _post(client, **_cadence_post(max_cold_touches=3))

    logged_in.refresh_from_db()
    # Removed, not zeroed — `crm.views._cadence_params` would drop a 0 as
    # out-of-range anyway, leaving the page showing a value nothing honors.
    assert logged_in.cadence_params == {"max_cold_touches": 3}


@pytest.mark.parametrize("key,bad", [
    ("followup_after_business_days", 0),
    ("followup_after_business_days", 31),
    ("park_after_business_days", 121),
    ("max_cold_touches", 11),
    ("advocate_touch_min_weeks", 53),
    ("pre_deadline_reping_days", 91),
])
def test_cadence_rejects_out_of_range(client, logged_in, key, bad):
    resp = _post(client, **_cadence_post(**{key: bad}))
    assert resp.status_code == 200  # re-rendered with errors
    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {}


def test_cadence_out_of_range_error_names_the_range(client, logged_in):
    resp = _post(client, **_cadence_post(followup_after_business_days=99))
    body = resp.content.decode()
    assert "must be between 1 and 30 business days" in body


def test_cadence_error_leaves_the_other_sections_intact(client, logged_in):
    """One bad Cadence entry must not blank out the sections above it."""
    logged_in.work_authorization = {"us": "citizen"}
    logged_in.save(update_fields=["work_authorization"])

    resp = _post(client, **_cadence_post(max_cold_touches=99))
    body = resp.content.decode()
    # The Work Authorization select still shows the saved value.
    assert 'value="citizen" selected' in body


def test_cadence_rejects_a_non_integer(client, logged_in):
    resp = _post(client, **_cadence_post(max_cold_touches="lots"))
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {}


def test_cadence_preserves_non_tunable_keys(client, logged_in):
    # A default pinned outside the whitelist (admin) isn't this form's to drop.
    logged_in.cadence_params = {"thank_you_within_hours": 12}
    logged_in.save(update_fields=["cadence_params"])

    _post(client, **_cadence_post(max_cold_touches=4))

    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {"thank_you_within_hours": 12,
                                        "max_cold_touches": 4}


def test_cadence_overrides_survive_the_engines_own_whitelist(logged_in):
    """What this form saves must be exactly what crm.views is willing to
    honor — otherwise the settings page saves values the engine ignores."""
    from crm.views import _cadence_params

    form = CadenceForm(_cadence_post(followup_after_business_days=9,
                                     advocate_touch_min_weeks=6))
    assert form.is_valid(), form.errors
    form.apply_to(logged_in)
    logged_in.refresh_from_db()
    assert _cadence_params(logged_in) == {
        "followup_after_business_days": 9,
        "advocate_touch_min_weeks": 6,
    }


def test_cadence_initial_ignores_junk_already_in_the_column(logged_in):
    logged_in.cadence_params = {"max_cold_touches": True, "advocate_touch_min_weeks": 5}
    form = CadenceForm.from_user(logged_in)
    # `True` is an int subclass but not a sane window length — same exclusion
    # crm.views._cadence_params makes.
    assert "max_cold_touches" not in form.initial
    assert form.initial["advocate_touch_min_weeks"] == 5


# ---------------------------------------------------------------------------
# Weekly Pace  (User.weekly_touch_goal)
# ---------------------------------------------------------------------------
def test_weekly_pace_saves_a_goal(client, logged_in):
    resp = _post(client, section="pace", weekly_touch_goal="15")
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.weekly_touch_goal == 15


def test_weekly_pace_blank_falls_back_to_null(client, logged_in):
    logged_in.weekly_touch_goal = 15
    logged_in.save(update_fields=["weekly_touch_goal"])

    _post(client, section="pace", weekly_touch_goal="")

    logged_in.refresh_from_db()
    # NULL, which crm.views reads as "use the product default" — not 0, which
    # would be a divide-by-zero pace ring.
    assert logged_in.weekly_touch_goal is None


@pytest.mark.parametrize("bad", ["0", "-3", str(WeeklyPaceForm.MAX_GOAL + 1), "many"])
def test_weekly_pace_rejects_nonsense(client, logged_in, bad):
    resp = _post(client, section="pace", weekly_touch_goal=bad)
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.weekly_touch_goal is None


def test_weekly_pace_feeds_the_today_pace_ring(client, logged_in):
    _post(client, section="pace", weekly_touch_goal="4")
    resp = client.get("/app/")
    assert resp.status_code == 200
    assert resp.context["pace"]["goal"] == 4


# ---------------------------------------------------------------------------
# Section isolation — each saves on its own, none clobbers another
# ---------------------------------------------------------------------------
def test_each_section_saves_without_touching_the_others(client, logged_in):
    _post(client, section="work_auth", work_auth_us="citizen", work_auth_hk="")
    _post(client, section="assets", angles="An angle")
    _post(client, **_cadence_post(max_cold_touches=5))
    _post(client, section="pace", weekly_touch_goal="12")

    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"us": "citizen"}
    assert logged_in.assets["angles"] == ["An angle"]
    assert logged_in.cadence_params == {"max_cold_touches": 5}
    assert logged_in.weekly_touch_goal == 12


def test_a_section_post_does_not_wipe_the_profile(client, logged_in):
    logged_in.school = "State U"
    logged_in.class_year = 2028
    logged_in.save(update_fields=["school", "class_year"])

    _post(client, section="pace", weekly_touch_goal="12")

    logged_in.refresh_from_db()
    assert logged_in.school == "State U"
    assert logged_in.class_year == 2028


def test_profile_save_still_works_alongside_the_new_sections(client, logged_in):
    """The profile form posts its own explicit `section="profile"` marker
    (see settings.html), so it must still hit the profile branch rather than
    being swallowed by the section dispatch.

    PINS A FIXED BUG: this test used to POST with no `section` at all and
    still expect a save — that was exactly the hole B2 closed. Every
    ProfileForm field is `required=False`, so a POST naming no section (or an
    unrecognised one) used to fall straight through to
    `ProfileForm(request.POST, ...)` and an empty/garbled POST validated as a
    legitimate blank save, silently erasing the profile. The marker is now
    required, like every other section."""
    resp = client.post(
        reverse(SETTINGS),
        {"section": "profile", "school": "Still Works U", "class_year": "2029",
         "target_cycle": "", "regions": ["us"], "tracks": ["ib"]},
    )
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.school == "Still Works U"


def test_unknown_section_value_is_a_noop_not_a_profile_fallthrough(client, logged_in):
    """PINS A FIXED BUG: a bogus `section` must not 500 — but it must ALSO no
    longer silently fall through to a profile save (the old behaviour this
    test used to pin under a different name). It is simply an unrecognised
    POST: a 200 re-render, and the profile untouched."""
    logged_in.school = "Original School"
    logged_in.save(update_fields=["school"])
    resp = client.post(
        reverse(SETTINGS),
        {"section": "not-a-section", "school": "Fallthrough U",
         "class_year": "", "target_cycle": "", "regions": [], "tracks": []},
    )
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.school == "Original School"


def test_each_section_flashes_a_success_message(client, logged_in):
    for data in (
        {"section": "assets", "angles": "An angle"},
        {"section": "work_auth", "work_auth_us": "citizen", "work_auth_hk": ""},
        _cadence_post(max_cold_touches=5),
        {"section": "pace", "weekly_touch_goal": "12"},
    ):
        resp = client.post(reverse(SETTINGS), data, follow=True)
        body = resp.content.decode()
        assert "msg success" in body, data["section"]


# ---------------------------------------------------------------------------
# Onboarding: the two new steps
# ---------------------------------------------------------------------------
def _step(step: str) -> str:
    return f"{reverse(ONBOARDING)}?step={step}"


@pytest.mark.parametrize("step,following", [
    ("work_auth", "firms"),
    ("assets", "import"),
])
def test_new_onboarding_steps_render_and_offer_a_skip(client, logged_in, step, following):
    resp = client.get(_step(step))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert f"?step={following}" in body  # the Skip link points at the next step


def test_onboarding_work_auth_step_saves_and_advances(client, logged_in):
    resp = client.post(_step("work_auth"), {
        "step": "work_auth", "work_auth_us": "citizen", "work_auth_hk": "sponsorship",
    })
    assert resp.status_code == 302
    assert "step=firms" in resp["Location"]
    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"us": "citizen", "hk": "sponsorship"}


def test_onboarding_work_auth_step_is_skippable_by_submitting_blank(client, logged_in):
    resp = client.post(_step("work_auth"), {
        "step": "work_auth", "work_auth_us": "", "work_auth_hk": "",
    })
    assert resp.status_code == 302
    assert "step=firms" in resp["Location"]
    logged_in.refresh_from_db()
    # Advanced without recording anything — unset, not defaulted.
    assert logged_in.work_authorization == {}


def test_onboarding_work_auth_step_is_skippable_by_link(client, logged_in):
    """The Skip link is a plain GET of the next step — nothing is written."""
    resp = client.get(_step("firms"))
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {}


def test_onboarding_assets_step_saves_and_advances(client, logged_in):
    resp = client.post(_step("assets"), {
        "step": "assets", "angles": "Rowing\nMandarin",
    })
    assert resp.status_code == 302
    assert "step=import" in resp["Location"]
    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == ["Rowing", "Mandarin"]


def test_onboarding_assets_step_is_skippable_by_submitting_blank(client, logged_in):
    resp = client.post(_step("assets"), {"step": "assets", "angles": ""})
    assert resp.status_code == 302
    assert "step=import" in resp["Location"]
    logged_in.refresh_from_db()
    assert logged_in.assets == {}


def test_onboarding_assets_step_is_skippable_by_link(client, logged_in):
    resp = client.get(_step("import"))
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.assets == {}


def test_onboarding_step_counter_covers_every_step(client, logged_in):
    from accounts.views import ONBOARDING_STEPS

    for i, step in enumerate(ONBOARDING_STEPS, start=1):
        resp = client.get(_step(step))
        assert resp.status_code == 200
        assert resp.context["step_number"] == i
        assert resp.context["step_total"] == len(ONBOARDING_STEPS) == 7


def test_onboarding_rail_labels_every_step_readably(client, logged_in):
    resp = client.get(_step("work_auth"))
    body = resp.content.decode()
    for label in ("Profile", "Work", "Firms", "Ranking", "Angles", "Import", "Capture"):
        assert f"<span>{label}</span>" in body


def test_onboarding_still_finishes_at_capture(client, logged_in):
    resp = client.post(_step("capture"), {"step": "capture"})
    assert resp.status_code == 302
    assert resp["Location"] == "/app/"
    logged_in.refresh_from_db()
    assert logged_in.onboarded_at is not None
