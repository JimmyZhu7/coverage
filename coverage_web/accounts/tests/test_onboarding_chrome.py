"""The wizard's own chrome: where you are, what it asks, and how you leave.

Four defects from the 2026-09-01 UI audit, plus the first-visit audit's two,
all on the four screens between signing up and using the product.

D1. WRONG "YOU ARE HERE". /welcome/ is the Settings prefix, so `base.html`'s
    nav lit the SETTINGS pill on every step: a student on step 1 of 4 was
    told they were in Settings, and offered five ways out of a wizard that
    gates the whole product. The nav is hidden while `onboarded_at is None`.
    The account block stays, because signing out is not the same as
    wandering into a product that has not been set up.

D2. STEP 1 WAS 2080px TALL. Twelve controls before the student has seen
    Coverage do anything. accounts/views.py's own ONBOARDING_STEPS comment
    states the rule ("asking early is how a wizard gets abandoned"), and it
    applies within a step as much as across them. Languages, affiliations
    and timezone move to Settings, where all three already are.

D3. NO SKIP, NOTHING SAID OPTIONAL. Steps 2 and 3 both carry "Skip for now";
    step 1, the longest, did not, and every field on ProfileForm is
    `required=False` with the page saying so nowhere.

D4. NO "ADD ONE PERSON" DOOR on step 4. Gmail needs a sent-mail history and
    a CSV needs a spreadsheet; a student with one name in their head had no
    route from the contacts step to a queue card at all.

THE TEST THAT MATTERS MOST IN THIS FILE is the last one. `ProfileForm.
apply_to` writes languages, affiliations and timezone on every save,
unconditionally, so simply not rendering them would blank a returning
student's answers — and worse, post an empty timezone, which `apply_to`
reads as an explicit choice and uses to turn `timezone_auto` OFF. Trimming
the step is only safe because the three values ride through as hidden
inputs. That is the regression this file exists to catch.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

User = get_user_model()
WIZARD = "accounts:onboarding"


@pytest.fixture
def newcomer(db):
    """Signed up, onboarding not finished."""
    user = User.objects.create_user(email="newcomer@example.com", password="x")
    assert user.onboarded_at is None
    return user


def _nav(body: str) -> str | None:
    m = re.search(r'<nav class="site-nav".*?</nav>', body, re.S)
    return m.group(0) if m else None


@pytest.mark.django_db
def test_the_site_nav_is_hidden_for_the_whole_wizard(client, newcomer):
    client.force_login(newcomer)

    for step in ("profile", "work_auth", "firms", "import"):
        body = client.get(f"{reverse(WIZARD)}?step={step}").content.decode()
        assert _nav(body) is None, (
            f"step {step} still renders the site nav, which lights SETTINGS "
            "and offers five exits from a wizard that gates the product"
        )
        # The wordmark and the way out of the account both stay.
        assert 'class="wordmark"' in body
        assert "/accounts/logout/" in body


@pytest.mark.django_db
def test_the_site_nav_comes_back_the_moment_onboarding_is_finished(client, newcomer):
    newcomer.onboarded_at = timezone.now()
    newcomer.save(update_fields=["onboarded_at"])
    client.force_login(newcomer)

    nav = _nav(client.get("/app/").content.decode())
    assert nav is not None, "an onboarded user gets the product's navigation back"
    assert "Opportunities" in nav


@pytest.mark.django_db
def test_a_signed_out_visitor_still_gets_the_nav(client):
    """The hide is keyed on an unfinished wizard, not on being logged in.
    A stranger on the landing page must still be able to reach the feed."""
    assert _nav(client.get("/").content.decode()) is not None


@pytest.mark.django_db
def test_step_one_no_longer_asks_for_what_the_feed_does_not_read(client, newcomer):
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()

    # Gone as controls.
    assert 'name="languages"' not in re.sub(r'<input type="hidden"[^>]*>', "", body)
    assert 'name="affiliations"' not in re.sub(r'<input type="hidden"[^>]*>', "", body)
    assert 'id="id_timezone"' not in body
    assert "<label>Languages</label>" not in body
    assert "Affiliations</label>" not in body

    # Still asked: everything the Opportunities feed and the eligibility
    # checks actually read.
    for kept in ('name="school"', 'name="class_year"', 'name="study_level"',
                 'name="target_cycles"', 'name="regions"', 'name="tracks"'):
        assert kept in body, f"{kept} feeds the preview panel and must stay on step 1"

    assert "Languages, affiliations and timezone: set later in Settings." in body


@pytest.mark.django_db
def test_settings_still_asks_for_all_three(client, newcomer):
    """The fields moved, they were not deleted. Same partial, no `compact`."""
    newcomer.onboarded_at = timezone.now()
    newcomer.save(update_fields=["onboarded_at"])
    client.force_login(newcomer)

    body = client.get(reverse("accounts:settings")).content.decode()
    assert "<label>Languages</label>" in body
    assert 'name="affiliations"' in body
    assert 'name="timezone"' in body


@pytest.mark.django_db
def test_step_one_can_be_skipped_and_says_it_is_optional(client, newcomer):
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()

    assert "All optional." in body
    skip = re.search(r'<a class="ob-skip" href="([^"]+)"', body)
    assert skip, "step 1 should offer the same Skip the later steps do"
    href = skip.group(1)
    # The funnel instrumentation, not just a link: without these a declined
    # step and an answered one land as the same event.
    assert "from=skip" in href and "skipped=profile" in href


@pytest.mark.django_db
def test_step_four_offers_a_third_door_for_a_student_with_no_history(client, newcomer):
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=import").content.decode()

    assert reverse("crm:contact_new") in body, (
        "Gmail needs a sent-mail history and a CSV needs a spreadsheet; the "
        "step about contacts has to offer adding one by hand"
    )
    assert "Add One by Hand" in body


@pytest.mark.django_db
def test_saving_step_one_does_not_blank_the_fields_it_stopped_asking(client, newcomer):
    """The regression the hidden carry-through exists to prevent.

    `ProfileForm.apply_to` writes languages, affiliations, timezone and
    timezone_auto on every save. A step that renders none of them would post
    none of them, and the form would read that as "the student cleared all
    three" — including an empty timezone, which `apply_to` treats as a
    deliberate pick and uses to switch auto-detection off.
    """
    newcomer.languages = ["mandarin", "french"]
    newcomer.affiliations = ["Consulting club, e-board"]
    newcomer.timezone_auto = True
    newcomer.timezone = "Asia/Hong_Kong"
    newcomer.save()

    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()

    # The values are in the form, as hidden inputs, ready to post back.
    assert '<input type="hidden" name="languages" value="mandarin">' in body
    assert '<input type="hidden" name="languages" value="french">' in body
    assert 'name="affiliations" value="Consulting club, e-board"' in body
    assert 'name="timezone" value="__auto__"' in body

    posted = {
        "step": "profile",
        "name": "Newcomer",
        "school": "USC",
        "class_year": "",
        "study_level": "",
        "school_emails": "",
        "languages": ["mandarin", "french"],
        "affiliations": "Consulting club, e-board",
        "timezone": "__auto__",
    }
    response = client.post(f"{reverse(WIZARD)}?step=profile", posted)
    assert response.status_code == 302

    newcomer.refresh_from_db()
    assert newcomer.languages == ["mandarin", "french"]
    assert newcomer.affiliations == ["Consulting club, e-board"]
    assert newcomer.timezone_auto is True, (
        "an empty timezone would have turned auto-detection off; the hidden "
        "input carries the AUTO sentinel through instead"
    )
    assert newcomer.timezone == "Asia/Hong_Kong"


@pytest.mark.django_db
def test_the_rail_labels_are_not_the_smallest_type_on_the_page(client, newcomer):
    """`--fs-nano` (10px) is reserved for uppercase badge labels. The rail is
    the student's only map of how much of the wizard is left."""
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()
    css = " ".join(re.findall(r"<style>(.*?)</style>", body, re.S))

    rule = re.search(r"\.ob-rail-lab\s*\{(.*?)\}", css, re.S)
    assert rule and "font-size: var(--fs-micro)" in rule.group(1)


@pytest.mark.django_db
def test_the_firm_tile_indicator_is_a_checkbox_not_a_radio(client, newcomer):
    """A hollow circle on a control that expects a dozen answers tells a
    student, before they read a word, to pick one. The geometry now matches
    the shared square checkbox in coverage.css."""
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=firms").content.decode()
    css = " ".join(re.findall(r"<style>(.*?)</style>", body, re.S))

    rule = re.search(r"\.ob-firm::after\s*\{(.*?)\}", css, re.S)
    assert rule, "the firm tile's indicator should be styled"
    assert "border-radius: 4px" in rule.group(1)
    assert "border-radius: 50%" not in rule.group(1), "50% is the radio shape"
    # Same 16px box, same 1.5px hairline, same 11px tick as every other
    # checkbox in the product.
    assert "width: 16px" in rule.group(1)
    assert "1.5px solid var(--line-strong)" in rule.group(1)
    assert "background-size: 11px 11px" in rule.group(1)


@pytest.mark.django_db
def test_the_preview_count_tween_is_guarded_against_the_count_up_failure(client, newcomer):
    """Coverage deleted its count-up animations because a throttled tab froze
    one mid-count and presented a wrong number as real. This tween is the
    opposite case (a change the reader caused, between two server-sent
    values) and it carries the guards that removal earned."""
    client.force_login(newcomer)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()
    scripts = " ".join(re.findall(r"<script[^>]*>(.*?)</script>", body, re.S))

    assert "ob-preview" in scripts and "ob-pv-num" in scripts
    assert "prefers-reduced-motion" in scripts
    assert "document.hidden" in scripts
    assert "setTimeout(land" in scripts, (
        "the backstop that writes the server's exact number whatever happens "
        "to the frame loop is the whole reason this is allowed to exist"
    )
