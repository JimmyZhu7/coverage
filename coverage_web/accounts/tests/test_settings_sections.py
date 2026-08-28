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

import re

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
    for anchor in ("work-auth", "cadence", "pace"):
        assert f'id="{anchor}"' in body
        assert f'href="#{anchor}"' in body  # rail entry


def test_cadence_section_shows_each_default_inline(client, logged_in):
    resp = client.get(reverse(SETTINGS))
    body = resp.content.decode()
    from coverage_domain.cadence import CADENCE_DEFAULTS

    for key in TUNABLE_CADENCE_PARAMS:
        assert f"Default: {CADENCE_DEFAULTS[key]}" in body


def test_cadence_diagram_draws_the_same_defaults_the_hints_promise(client, logged_in):
    """The three rails read each knob's placeholder when nothing is typed, and
    those placeholders are CADENCE_DEFAULTS — so an untouched page draws the
    numbers it prints.

    The diagram used to carry its own hardcoded `data-defaults` JSON of
    example values, which drifted from the engine: it painted a 8-week
    chatted clock, a 6-week advocate clock and a 10-day re-ping while the
    hints two lines below said 3 weeks, 4 weeks and 14 days. A first-time
    user saw a picture contradicting both the copy and the behaviour. There
    is now one copy of each number in the page, emitted by the form.
    """
    from coverage_domain.cadence import CADENCE_DEFAULTS

    body = client.get(reverse(SETTINGS)).content.decode()
    assert "data-defaults" not in body  # no second source of truth
    for key in TUNABLE_CADENCE_PARAMS:
        tag = re.search(rf'<input[^>]*name="{key}"[^>]*>', body)
        assert tag, f"no input rendered for {key}"
        assert f'placeholder="{CADENCE_DEFAULTS[key]}"' in tag.group(0)


# ---------------------------------------------------------------------------
# Outreach Assets — DELETED (owner's call, 2026-08-05). The section was
# write-only: the form saved User.assets["angles"] and nothing in the product
# ever read it back. These pin the removal.
# ---------------------------------------------------------------------------
def test_the_assets_section_is_gone_from_settings(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Outreach Assets" not in body
    assert "Your Angles" not in body


def test_a_stale_assets_post_is_a_no_op_not_a_crash(client, logged_in):
    """A tab left open from before the removal can still POST section=assets.
    It takes the unrecognised-section path: a no-op re-render, nothing
    written — the same guard that stops an empty POST wiping the profile."""
    resp = _post(client, section="assets", angles="Left over")
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert "angles" not in (logged_in.assets or {})


def test_stored_angles_survive_the_feature_removal(client, logged_in):
    """Deleting the UI must not delete the data: the key stays in `assets`
    (and stays in the export) until the owner clears it deliberately."""
    logged_in.assets = {"angles": ["Old angle"], "advocate_target": 2}
    logged_in.save(update_fields=["assets"])

    _post(client, section="work_auth", work_auth_us="citizen")

    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == ["Old angle"]


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
    # max_cold_touches's only two legal values are 1 and 2 (the default) —
    # capped there so a second follow-up can never be configured back on; see
    # crm.views.TUNABLE_CADENCE_PARAMS. 1 is the differs-from-default value
    # this test needs to prove a save round-trips.
    resp = _post(client, **_cadence_post(followup_after_business_days=7,
                                         max_cold_touches=1))
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {
        "followup_after_business_days": 7,
        "max_cold_touches": 1,
    }


def test_cadence_clearing_an_input_removes_the_override(client, logged_in):
    logged_in.cadence_params = {"followup_after_business_days": 7,
                                "max_cold_touches": 1}
    logged_in.save(update_fields=["cadence_params"])

    _post(client, **_cadence_post(max_cold_touches=1))

    logged_in.refresh_from_db()
    # Removed, not zeroed — `crm.views._cadence_params` would drop a 0 as
    # out-of-range anyway, leaving the page showing a value nothing honors.
    assert logged_in.cadence_params == {"max_cold_touches": 1}


@pytest.mark.parametrize("key,bad", [
    ("followup_after_business_days", 0),
    ("followup_after_business_days", 31),
    ("park_after_business_days", 121),
    # 3, not some larger number: the range is capped at 2 specifically so a
    # second follow-up can never be configured back on (see cadence.py's
    # DIVERGENCE note on the staged-window behaviour tried and reverted
    # 2026-07-28) — 3 is the first value that would reopen it, so it's the
    # boundary case worth pinning, not an arbitrary large one.
    ("max_cold_touches", 3),
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
    # Work Authorization still shows the saved value. It is a radio matrix
    # now rather than six selects, so the saved state reads as a checked
    # radio — the behaviour under test (an error in ONE section must not
    # blank another) is unchanged. Matched on the ONE input that must carry
    # it: `"checked" in body` would pass on any checked radio anywhere.
    import re

    citizen = re.search(
        r'<input[^>]*name="work_auth_us"[^>]*value="citizen"[^>]*>', body)
    assert citizen and "checked" in citizen.group(0), (
        "the saved US answer must survive an error in another section")


def test_cadence_rejects_a_non_integer(client, logged_in):
    resp = _post(client, **_cadence_post(max_cold_touches="lots"))
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {}


def test_cadence_preserves_non_tunable_keys(client, logged_in):
    # A default pinned outside the whitelist (admin) isn't this form's to drop.
    logged_in.cadence_params = {"thank_you_within_hours": 12}
    logged_in.save(update_fields=["cadence_params"])

    _post(client, **_cadence_post(max_cold_touches=1))

    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {"thank_you_within_hours": 12,
                                        "max_cold_touches": 1}


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


def test_cadence_initial_ignores_a_stale_out_of_range_value(logged_in):
    """A value that was legal before max_cold_touches's range tightened to
    (1, 2) — e.g. the 3 the staged-follow-up feature allowed before it was
    reverted — must not render into a field whose own widget caps at 2. The
    engine (crm.views._cadence_params) already ignores it; the form showing
    it anyway would be a number the input itself rejects on the same page."""
    logged_in.cadence_params = {"max_cold_touches": 3, "advocate_touch_min_weeks": 5}
    form = CadenceForm.from_user(logged_in)
    assert "max_cold_touches" not in form.initial
    assert form.initial["advocate_touch_min_weeks"] == 5


def test_saving_cadence_clears_a_stale_out_of_range_value_from_the_column(client, logged_in):
    """Since the field renders blank (previous test) and `apply_to` treats an
    unposted field as "clear the override", saving the Cadence section at
    all — even leaving max_cold_touches untouched — scrubs the stale 3 out
    of the stored column, not just out of what's displayed."""
    logged_in.cadence_params = {"max_cold_touches": 3}
    logged_in.save(update_fields=["cadence_params"])

    _post(client, **_cadence_post(advocate_touch_min_weeks=6))

    logged_in.refresh_from_db()
    assert logged_in.cadence_params == {"advocate_touch_min_weeks": 6}


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
# Notifications — the weekly digest opt-out. NotificationsForm's field is
# named and valued the OPPOSITE of the column it writes (see the form's own
# docstring): these tests exercise both sides of that translation, not just
# the column.
# ---------------------------------------------------------------------------
def test_notifications_defaults_to_enabled_for_a_fresh_account(logged_in):
    assert logged_in.weekly_digest_opt_out is False


def test_unchecking_the_digest_box_opts_out(client, logged_in):
    # An unchecked BooleanField posts no key at all — this IS "unchecked",
    # not a missing-parameter oversight in the test.
    resp = _post(client, section="notifications")
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.weekly_digest_opt_out is True


def test_checking_the_digest_box_opts_back_in(client, logged_in):
    logged_in.weekly_digest_opt_out = True
    logged_in.save(update_fields=["weekly_digest_opt_out"])

    _post(client, section="notifications", weekly_digest_enabled="on")

    logged_in.refresh_from_db()
    assert logged_in.weekly_digest_opt_out is False


def test_notifications_section_renders_checked_by_default(client, logged_in):
    resp = client.get(reverse(SETTINGS))
    body = resp.content.decode()
    snippet = body[body.index('id="id_weekly_digest_enabled"'):]
    assert "checked" in snippet[:200]


def test_notifications_section_renders_unchecked_once_opted_out(client, logged_in):
    logged_in.weekly_digest_opt_out = True
    logged_in.save(update_fields=["weekly_digest_opt_out"])

    resp = client.get(reverse(SETTINGS))
    body = resp.content.decode()
    snippet = body[body.index('id="id_weekly_digest_enabled"'):]
    assert "checked" not in snippet[:200]


def test_send_weekly_digest_excludes_an_opted_out_user(db):
    from io import StringIO

    from django.core.management import call_command

    User.objects.create_user(
        email="digest-out@example.com", password="x",
        onboarded_at="2026-08-01T00:00:00Z", weekly_digest_opt_out=True,
    )
    out = StringIO()
    call_command("send_weekly_digest", "--dry-run", stdout=out)
    assert "digest-out@example.com" not in out.getvalue()


# ---------------------------------------------------------------------------
# Advocate Target — a live engine parameter that had no control at all
# ---------------------------------------------------------------------------
# `crm.coverage.advocate_target()` has always read `assets["advocate_target"]`,
# and it drives the gap ladder on Network plus the network axis of the firm fit
# score. No UI could set it: the founder's row carried the key from the cutover
# import and nothing could change it. It saves on the Cadence section's POST,
# beside the other engine knobs, but it lives in `assets` rather than
# `cadence_params` because `assets` is the column the reader already reads.
def test_advocate_target_saves_into_assets(client, logged_in):
    resp = _post(client, **_cadence_post(), advocate_target="4")
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.assets["advocate_target"] == 4


def test_advocate_target_is_honoured_by_the_engine_that_reads_it(client, logged_in):
    """The rule this page exists to keep: what Settings saves is exactly what
    the engine honours, with no translation layer in between."""
    from crm.coverage import advocate_target

    _post(client, **_cadence_post(), advocate_target="4")
    logged_in.refresh_from_db()
    assert advocate_target(logged_in) == 4


def test_advocate_target_blank_removes_the_key_and_restores_the_default(
    client, logged_in
):
    from crm.coverage import DEFAULT_ADVOCATE_TARGET, advocate_target

    logged_in.assets = {"advocate_target": 4}
    logged_in.save(update_fields=["assets"])

    _post(client, **_cadence_post(), advocate_target="")

    logged_in.refresh_from_db()
    # Absent, not stored-as-the-default: "the user didn't answer" and "the
    # user chose 2" must stay distinguishable in the column.
    assert "advocate_target" not in logged_in.assets
    assert advocate_target(logged_in) == DEFAULT_ADVOCATE_TARGET


@pytest.mark.parametrize("bad", ["0", "6", "-1", "many"])
def test_advocate_target_rejects_values_the_engine_would_ignore(client, logged_in, bad):
    """The read side falls back on anything below 1, and above 5 the gap
    ladder's top rung is unreachable for any real student. Either way the
    number would be saved and then ignored, which is the defect the whole
    section-form contract exists to prevent."""
    resp = _post(client, **_cadence_post(), advocate_target=bad)
    assert resp.status_code == 200  # re-rendered with errors
    logged_in.refresh_from_db()
    assert "advocate_target" not in (logged_in.assets or {})


def test_advocate_target_owns_only_its_own_key_in_assets(client, logged_in):
    logged_in.assets = {"angles": ["Kept angle"], "current_status": "student"}
    logged_in.save(update_fields=["assets"])

    _post(client, **_cadence_post(), advocate_target="3")

    logged_in.refresh_from_db()
    assert logged_in.assets["angles"] == ["Kept angle"]
    assert logged_in.assets["current_status"] == "student"
    assert logged_in.assets["advocate_target"] == 3


def test_advocate_target_initial_drops_an_out_of_range_stored_value(logged_in):
    """Same rule as the cadence knobs: a value the engine would ignore is not
    rendered as if it were honoured."""
    logged_in.assets = {"advocate_target": 99}
    form = CadenceForm.from_user(logged_in)
    assert "advocate_target" not in form.initial


def test_the_cadence_card_renders_the_advocate_target_row(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Advocate Target" in body
    assert 'name="advocate_target"' in body


# ---------------------------------------------------------------------------
# Section isolation — each saves on its own, none clobbers another
# ---------------------------------------------------------------------------
def test_each_section_saves_without_touching_the_others(client, logged_in):
    _post(client, section="work_auth", work_auth_us="citizen", work_auth_hk="")
    _post(client, **_cadence_post(max_cold_touches=1))
    _post(client, section="pace", weekly_touch_goal="12")

    logged_in.refresh_from_db()
    assert logged_in.work_authorization == {"us": "citizen"}
    assert logged_in.cadence_params == {"max_cold_touches": 1}
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
         "target_cycles": [], "regions": ["us"], "tracks": ["ib"]},
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
         "class_year": "", "target_cycles": [], "regions": [], "tracks": []},
    )
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.school == "Original School"


def test_each_section_flashes_a_success_message(client, logged_in):
    for data in (
        {"section": "work_auth", "work_auth_us": "citizen", "work_auth_hk": ""},
        _cadence_post(max_cold_touches=1),
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


def test_the_onboarding_wizard_no_longer_offers_an_assets_step(client, logged_in):
    """The step key survives in old bookmarks. An unknown step must fall back
    to the wizard's first step rather than 404 or render a ghost form."""
    resp = client.get(_step("assets"))
    assert resp.status_code in (200, 302)
    body = (resp.content.decode() if resp.status_code == 200
            else client.get(resp["Location"]).content.decode())
    assert "Your Angles" not in body


class TestTheLastStepLeadsWithGmail:
    """The wizard's last step used to have one door (upload a CSV) and told
    students to walk through it BEFORE connecting Gmail, because the
    historical pass searched per-contact and so found nobody new. The sent
    sweep (capture/gmail_live.py) changed the fact under that argument, so
    Gmail leads — but only where the deploy can honour it.
    """

    def _cfg(self, monkeypatch, value):
        from accounts import views
        monkeypatch.setattr(views.gmail_live, "is_configured", lambda: value)

    def test_connect_gmail_is_the_primary_action(self, client, logged_in, monkeypatch):
        self._cfg(monkeypatch, True)
        body = client.get(_step("import")).content.decode()
        assert reverse("capture:gmail_connect") in body
        assert "Import a CSV" in body  # the second door, still there

    def test_an_unconfigured_deploy_renders_no_dead_button(
        self, client, logged_in, monkeypatch
    ):
        """`capture:gmail_connect` raises Http404 without credentials."""
        self._cfg(monkeypatch, False)
        body = client.get(_step("import")).content.decode()
        assert reverse("capture:gmail_connect") not in body
        assert "Import a CSV" in body

    def test_the_step_order_did_not_change(self, client, logged_in):
        from accounts.views import ONBOARDING_STEPS

        assert ONBOARDING_STEPS == ["profile", "work_auth", "firms", "import"]


def test_onboarding_step_counter_covers_every_step(client, logged_in):
    from accounts.views import ONBOARDING_STEPS

    for i, step in enumerate(ONBOARDING_STEPS, start=1):
        resp = client.get(_step(step))
        assert resp.status_code == 200
        assert resp.context["step_number"] == i
        assert resp.context["step_total"] == len(ONBOARDING_STEPS) == 4


def test_onboarding_rail_labels_every_step_readably(client, logged_in):
    """Every step is named in words on the rail, not just numbered.

    The label markup moved from a bare `<span>` to `.ob-rail-lab` when the
    rail became a filled track; the thing worth protecting was never the tag
    shape, it is that a student can read where they are.
    """
    resp = client.get(_step("work_auth"))
    body = resp.content.decode()
    # "Contacts", not "Import": the last step now leads with connecting
    # Gmail (which reads the student's own sent mail once and offers the
    # people they already wrote to) and keeps the CSV as the second door.
    # The step KEY is still `import`; only what a student reads changed.
    for label in ("Profile", "Work", "Firms", "Contacts"):
        assert f'<span class="ob-rail-lab">{label}' in body


def test_onboarding_rail_marks_current_and_completed_without_relying_on_the_dot(
    client, logged_in
):
    """The dot is aria-hidden, so the rail's state has to survive without it:
    the current step carries aria-current, and finished steps say "completed"
    in words rather than leaving a bare glyph to be announced."""
    body = client.get(_step("firms")).content.decode()
    assert body.count('aria-current="step"') == 1
    # profile and work_auth are behind us on step 3; firms and import are not.
    assert body.count('<span class="ob-sr"> completed</span>') == 2


def test_onboarding_still_finishes_at_import(client, logged_in):
    resp = client.post(_step("import"), {"step": "import"})
    assert resp.status_code == 302
    assert resp["Location"] == "/app/"
    logged_in.refresh_from_db()
    assert logged_in.onboarded_at is not None


# ---------------------------------------------------------------------------
# School Email — the one setting `capture.discovery` reads to know which
# domain is the student's OWN institution. Invisible state would be the wrong
# answer here, so this pins that it is a real control on a real page.
# ---------------------------------------------------------------------------

def test_school_email_is_a_labelled_control_on_the_settings_page(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert 'for="id_school_emails"' in body
    assert 'name="school_emails"' in body


def test_school_email_round_trips_through_the_settings_page(client, logged_in):
    resp = _post(
        client, section="profile", school="USC", school_emails="jimmyz@usc.edu",
        class_year="", target_cycles=[], regions=[], tracks=[], timezone="",
    )
    assert resp.status_code == 302
    logged_in.refresh_from_db()
    assert logged_in.school_emails == ["jimmyz@usc.edu"]

    # And it comes back in the box, so the student can see and change it.
    body = client.get(reverse(SETTINGS)).content.decode()
    assert 'value="jimmyz@usc.edu"' in body


def test_a_rejected_school_email_leaves_the_stored_value_alone(client, logged_in):
    """Freemail is refused by `ProfileForm.clean_school_emails`. The whole
    profile save fails with it, which must mean UNCHANGED, not blanked."""
    logged_in.school_emails = ["jimmyz@usc.edu"]
    logged_in.school = "USC"
    logged_in.save(update_fields=["school_emails", "school"])

    resp = _post(
        client, section="profile", school="Elsewhere U",
        school_emails="jimmy@gmail.com", class_year="",
        target_cycles=[], regions=[], tracks=[], timezone="",
    )
    assert resp.status_code == 200
    assert "personal email provider" in resp.content.decode()
    logged_in.refresh_from_db()
    assert logged_in.school_emails == ["jimmyz@usc.edu"]
    assert logged_in.school == "USC"


def test_an_invalid_htmx_profile_save_returns_the_partial_not_the_page(
    client, logged_in
):
    """PINS A FIXED BUG. The valid htmx save returns `_profile_form.html`;
    the invalid one used to fall through to the full settings page, which
    htmx then swapped into `#profile-fields` — a whole second page nested
    inside the form the person was trying to fix."""
    resp = client.post(
        reverse(SETTINGS),
        {"section": "profile", "school": "", "school_emails": "x@gmail.com",
         "class_year": "", "target_cycles": [], "regions": [], "tracks": [],
         "timezone": ""},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "personal email provider" in body
    assert 'id="profile-fields"' not in body
    assert "<nav" not in body
