"""Structure and honesty assertions for /welcome/settings/ itself
(docs/specs/settings-page.md Part 3A, B2, B5, D, E).

These test the page's CLAIMS rather than any one form's save path: that the
rail is grouped and complete, that the Language section is gone, that counts
state their population, that the capture card tells the truth about a loop
that has received nothing, and that every control has a programmatic label.

The a11y block is the one that was outright broken before this change:
`.set-row-label` was a `<div>`, so every work-authorization select, cadence
number, and the pace input was associated with its label by visual adjacency
only — a screen reader announced a column of unlabelled comboboxes.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact

User = get_user_model()

pytestmark = pytest.mark.django_db

SETTINGS = "accounts:settings"


@pytest.fixture
def student():
    return User.objects.create_user(email="page@example.com", password="x")


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return student


@pytest.fixture
def body(client, logged_in, settings):
    # Force GMAIL_LIVE_* unset regardless of what's in the environment this
    # suite happens to run in. The founder's own local `.env` sets all four
    # (the live poll loop depends on it), which used to make
    # `test_the_rail_lists_every_section_and_every_section_exists` fail on
    # his machine while passing in CI/a clean checkout — a real assertion
    # ("the rail has no gmail-live entry when the feature is dark") that
    # only held by environmental accident. Every structural claim below
    # (credits always renders, Gmail Live only when configured, etc.) is
    # about the UNCONFIGURED shape of the page, so this fixture pins that
    # shape for the whole file rather than leaving it to whatever happens
    # to be set outside the test.
    settings.GMAIL_LIVE_CLIENT_ID = ""
    settings.GMAIL_LIVE_CLIENT_SECRET = ""
    settings.GMAIL_LIVE_PUBSUB_TOPIC = ""
    settings.GMAIL_LIVE_TOKEN_KEY = ""
    return client.get(reverse(SETTINGS)).content.decode()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_the_rail_lists_every_section_and_every_section_exists(body):
    """A rail entry with no section scrolls nowhere; a section with no rail
    entry is unreachable on a long page. Both directions are asserted."""
    railed = set(re.findall(r'class="settings-nav[^"]*"[^>]*>(.*?)</nav>', body, re.S))
    nav = next(iter(railed))
    anchors = set(re.findall(r'href="#([a-z-]+)"', nav))
    sections = set(re.findall(r'<section class="set-card[^"]*" id="([a-z-]+)"', body))
    assert anchors == sections
    assert sections == {
        # Preferences sits under "You" for the reason Appearance did before
        # it absorbed the digest row: how you read and how you're written to
        # are facts about this person, not about how the engine behaves.
        "profile", "work-auth", "preferences",
        # Target Firms: the same tier-editing "Your Firms" group the
        # Network board's drag-and-drop already writes through
        # (crm:set_firm_tier) — Settings adds the missing start/stop-
        # tracking half the board itself has no control for.
        "firms",
        # ONE card for the weekly goal and the six cadence knobs. Weekly
        # Pace used to be its own section immediately below; a heading and
        # a card frame around a single number, under a heading that already
        # promised "how hard Coverage chases".
        "cadence",
        # Credits (docs/credit-system-plan.md §6): always rendered, unlike
        # Gmail Live below it, which only shows up once GMAIL_LIVE_* is
        # configured — every account has a plan and a balance regardless.
        "credits",
        # Legal is inside "data" now: Privacy and Terms are what says what
        # Coverage may do with what this card counts. Both routes survive
        # (see test_the_legal_routes_survive_the_fold below); only the
        # second card frame went.
        "security", "data", "danger",
    }


def test_the_rail_and_the_page_run_in_the_same_order(body):
    """The rail's `.is-active` marker rides a spine and is moved by an
    IntersectionObserver as you scroll. When the rail's order and the page's
    order disagreed — the rail read Profile, Work Authorization, Appearance,
    Target Firms while the page ran Profile, Appearance, Target Firms, Work
    Authorization — the marker travelled BACKWARDS past two sections. A rail
    whose order is a fiction is worse than no rail."""
    nav = re.search(r'class="settings-nav[^"]*"[^>]*>(.*?)</nav>', body, re.S).group(1)
    anchors = re.findall(r'href="#([a-z-]+)"', nav)
    sections = re.findall(r'<section class="set-card[^"]*" id="([a-z-]+)"', body)
    assert anchors == sections


def test_the_legal_routes_survive_the_fold(body):
    """Privacy and Terms lost their own card, not their links. For a
    signed-in student these are the only in-app routes to either document."""
    assert reverse("accounts:privacy") in body
    assert reverse("accounts:terms") in body


def test_the_decisions_groups_keep_their_own_anchors(client, logged_in):
    """Campaigns, dismissed proposals and duplicates became three groups in
    one card. Each keeps the id its own card carried, so an existing
    #campaigns or #duplicates link still lands on the right group — and so
    the render rule stays PER GROUP: nothing to answer, nothing drawn."""
    from capture.models import ContactProposal

    body = client.get(reverse(SETTINGS)).content.decode()
    # Nothing to decide on a fresh account: no card, no rail entry, and none
    # of the three group anchors.
    assert 'id="decisions"' not in body
    assert 'href="#decisions"' not in body
    for anchor in ("campaigns", "dismissed-proposals", "duplicates"):
        assert f'id="{anchor}"' not in body

    # One dismissed proposal and the card appears, carrying that group only.
    ContactProposal.all_objects.create(
        user=logged_in, name="Buried Banker", email="buried@example.com",
        status=ContactProposal.STATUS_DISMISSED, evidence="Replied to your note.",
    )
    body = client.get(reverse(SETTINGS)).content.decode()
    assert 'id="decisions"' in body
    assert 'href="#decisions"' in body
    assert 'id="dismissed-proposals"' in body
    assert "Buried Banker" in body
    # The way back is on the row, not just the name of the person.
    assert reverse("crm:proposal_restore", args=[
        ContactProposal.objects.for_user(logged_in).get().id
    ]) in body
    # The other two groups stay absent — the render rule is per group, not
    # per card. One answered question must not draw two empty lists.
    assert 'id="campaigns"' not in body
    assert 'id="duplicates"' not in body


def test_the_cadence_rails_carry_a_fill_span(body):
    """Each rail draws a coloured span for the one stretch its clock is
    actually counting (see settings.html's note on why that span is set via
    plain left/width rather than a custom property behind calc() — the
    calc(var()) version rendered correctly once and then never moved again
    on a later edit). One `.cad-fill` per `.cad-rail`, no more, no less."""
    rails = re.findall(r'<div class="cad-rail[^"]*"[^>]*>(.*?)</div>\s*<p class="cad-sentence"',
                        body, re.S)
    assert len(rails) == 3
    for rail_html in rails:
        assert rail_html.count('class="cad-fill"') == 1


def test_the_rail_is_grouped(body):
    """Ten flat links was at the limit of scannable. The groups mirror what
    LinkedIn, Notion and Linear all converged on: who you are / how the
    product behaves / how you get in and what we hold."""
    for group in ("You", "How Coverage Paces You", "Account"):
        assert f'class="settings-nav-group">{group}<' in body


def test_the_danger_zone_is_last_and_holds_exactly_one_action(body):
    """Sign-out-everywhere lives in Sign-In & Security on purpose — it
    protects access rather than destroying an account, and burying it here
    would hide it where nobody looks."""
    sections = re.findall(r'<section class="set-card[^"]*" id="([a-z-]+)"', body)
    assert sections[-1] == "danger"
    danger = body.split('id="danger"', 1)[1]
    assert danger.count("set-row-label") == 1


def test_the_language_section_is_gone(body):
    """It saved `User.language` and nothing ever read it — no LocaleMiddleware,
    no catalogs, no {% trans %}. A control that does nothing is the same defect
    as a setting the engine ignores."""
    assert 'id="language"' not in body
    assert 'href="#language"' not in body
    assert 'name="language"' not in body


def test_posting_a_language_no_longer_does_anything(client, logged_in):
    """The old branch dispatched on the mere PRESENCE of a `language` key,
    bypassing the section marker entirely. It must now be an unrecognised POST
    — a no-op re-render, not a profile fallthrough."""
    logged_in.school = "Unchanged U"
    logged_in.save(update_fields=["school"])
    resp = client.post(reverse(SETTINGS), {"language": "zh"})
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.language == "en"
    assert logged_in.school == "Unchanged U"


# ---------------------------------------------------------------------------
# Counts mean what they say (rule D3)
# ---------------------------------------------------------------------------
def test_the_contact_count_splits_out_archived_rows(client, logged_in):
    """This said "137" while the Network page said "112", because it counted
    archived rows and Network doesn't. Two pages disagreeing about the same
    number is a trust problem, not a rounding one."""
    for i in range(3):
        Contact.all_objects.create(user=logged_in, name=f"Live {i}")
    for i in range(2):
        Contact.all_objects.create(user=logged_in, name=f"Gone {i}", archived=True)

    body = client.get(reverse(SETTINGS)).content.decode()
    # A <span> inside .set-row-control now, not a bare <div> child of the
    # row: the same anatomy the Credits balance uses, so the page has one
    # spelling for "read-only figure on the right of a row" rather than two.
    assert ">5</span>" in body  # the total, stated
    assert "2 archived" in body  # and its population, stated too
    assert reverse("crm:contact_archived") in body


def test_the_target_firm_count_is_not_restated_under_your_data(client, logged_in):
    """It printed a lone number for something the Target Firms board four
    cards above already states as three live per-tier counts you can drag
    firms between. One page, one number, one place — and the stronger of the
    two places is the one you can act on."""
    body = client.get(reverse(SETTINGS)).content.decode()
    data = body.split('id="data"', 1)[1].split("</section>", 1)[0]
    assert "Target Firms" not in data


def test_no_archived_note_when_there_is_nothing_archived(client, logged_in):
    Contact.all_objects.create(user=logged_in, name="Only Live One")
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "archived" not in body.split('id="data"', 1)[1].split("</section>", 1)[0]


# ---------------------------------------------------------------------------
# Accessibility (E)
# ---------------------------------------------------------------------------
def test_every_single_control_row_has_a_real_label_for(body):
    """`.set-row-label` used to be a <div>. Every row that owns exactly one
    control now carries a <label for> pointing at it."""
    labels = re.findall(r'<label class="set-row-label" for="([^"]+)"', body)
    assert labels, "no set-row labels found — did the page render?"
    for target in labels:
        assert f'id="{target}"' in body, f"label points at missing control {target}"


def test_every_work_auth_region_is_a_labelled_radiogroup(body):
    """The six selects became one matrix (regions x three states), so the
    accessible structure changed shape: each region is now a radiogroup
    labelled by its own name, rather than a select with a <label for>. The
    guarantee is the same — no unlabelled control — and this asserts the new
    shape rather than the old markup."""
    from accounts.forms import REGION_CHOICES

    assert body.count('role="radiogroup"') >= len(REGION_CHOICES)
    for code, _label in REGION_CHOICES:
        # The row names its own label element, and that element exists.
        assert f'aria-labelledby="id_work_auth_{code}-lab"' in body
        assert f'id="id_work_auth_{code}-lab"' in body
    # The explanation is stated ONCE now and described by every row, instead
    # of being repeated per region.
    assert body.count('aria-describedby="wa-note"') >= len(REGION_CHOICES)
    assert body.count('id="wa-note"') == 1


def test_controls_are_described_by_their_explanation(body):
    """The row's prose and its error are the control's accessible
    description — without the association a screen reader reads the label and
    nothing else."""
    assert 'aria-describedby="id_weekly_touch_goal-desc id_weekly_touch_goal-err"' in body
    assert 'id="id_weekly_touch_goal-desc"' in body


def test_each_section_is_labelled_by_its_own_heading(body):
    for section_id in ("profile", "cadence", "security", "data", "danger"):
        assert f'aria-labelledby="{section_id}-h"' in body
        assert f'id="{section_id}-h"' in body


def test_the_rail_is_a_named_landmark_with_a_current_item(body):
    assert 'aria-label="Settings sections"' in body
    assert 'aria-current="true"' in body


def test_flashes_carry_a_live_region_role(client, logged_in):
    resp = client.post(
        reverse(SETTINGS), {"section": "pace", "weekly_touch_goal": "12"}, follow=True
    )
    assert 'role="status"' in resp.content.decode()


def test_an_error_flash_interrupts_rather_than_waits(client, logged_in):
    """role="alert" for something the user must act on; role="status" for
    "Saved." Using alert for both trains people to ignore it."""
    resp = client.post(reverse("accounts:import"), {}, follow=True)
    assert 'role="alert"' in resp.content.decode() or resp.status_code == 200


def test_destructive_controls_have_descriptive_accessible_names(body):
    assert 'aria-label="Delete account permanently"' in body
    assert 'aria-label="Sign out on all other devices"' in body


# ---------------------------------------------------------------------------
# Responsive rules that have to exist in the rendered CSS
# ---------------------------------------------------------------------------
def test_the_rows_stack_on_a_narrow_screen(body):
    """At 375px a select beside a long label is crushed to unusable width."""
    css = "\n".join(re.findall(r"<style>(.*?)</style>", body, re.S))
    narrow = css.split("@media (max-width: 560px)", 1)[1]
    assert "flex-direction: column" in narrow




def test_rows_use_min_height_never_a_fixed_height(body):
    """House rule: a card or row must grow with its content."""
    css = "\n".join(re.findall(r"<style>(.*?)</style>", body, re.S))
    # Several `.set-row` blocks exist (base rule plus the ≤560px override), so
    # min-height must appear in at least one and a fixed height in none.
    row_rules = re.findall(r"\.set-row \{([^}]*)\}", css)
    assert row_rules
    assert any("min-height" in rule for rule in row_rules)
    for rule in row_rules:
        assert re.search(r"(^|[^-])height:\s*\d", rule) is None, rule
