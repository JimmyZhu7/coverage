"""Tests for `crm.sourcing` — the "Who to find" suggestions on a Coverage
Gap card — and for the render + event endpoint that carry them.

`crm.sourcing` is pure (no DB, no clock, no network), so most of this file
is plain unit tests over tiny stand-in objects. That is deliberate: the
claims the panel makes to a student ("these are your tracks' seats", "this
link searches THIS firm") should be assertable without a database row.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import sourcing
from crm.models import UserFirm
from directory.classify import SELECTABLE_TRACKS, TRACK_LABELS
from directory.models import Firm, FirmDate

User = get_user_model()

STYLES = (
    Path(__file__).resolve().parents[2] / "templates" / "crm" / "_styles.html"
)


class FakeUser:
    """Everything `suggestions_for` reads off a user, and nothing else."""

    def __init__(self, tracks=None, school=""):
        self.tracks = tracks
        self.school = school


def _keywords(url: str) -> str:
    """The decoded `keywords` value out of a LinkedIn search URL."""
    return parse_qs(urlparse(url).query)["keywords"][0]


# ---------------------------------------------------------------------------
# 1. Every track has a real table, and the user's tracks pick it.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("track", sorted(SELECTABLE_TRACKS))
def test_every_app_track_has_archetypes(track):
    """The app's track vocabulary and this module's table must not drift:
    a student whose track has no entry would silently fall back to the
    generic trio and never know their track was unsupported.

    SELECTABLE_TRACKS, not TRACK_LABELS, since D-3 retired `corp-strat`
    from the picker: a label still exists for the slug because nine firms
    still carry it, and a table would be a set of seats nobody can ask
    for. The rule this pins is unchanged — every track a student can
    CHOOSE has real archetypes — it now reads the vocabulary that says
    what a student can choose."""
    assert track in sourcing.TRACK_ARCHETYPES
    table = sourcing.TRACK_ARCHETYPES[track]
    assert 2 <= len(table) <= 3
    for label, why, terms in table:
        assert label and why and terms
        # House voice: no em dashes anywhere in user-facing copy.
        assert "—" not in label and "—" not in why


@pytest.mark.parametrize("track", sorted(SELECTABLE_TRACKS))
def test_a_single_track_student_gets_that_tracks_seats(track):
    rows = sourcing.suggestions_for({"name": "Goldman Sachs"}, FakeUser([track]))
    assert len(rows) == 3
    expected = [a[0] for a in sourcing.TRACK_ARCHETYPES[track][:3]]
    assert [r["label"] for r in rows] == expected
    for row in rows:
        assert _keywords(row["linkedin_url"]).startswith('"Goldman Sachs" ')


def test_two_tracks_alternate_instead_of_stacking():
    """A dual-track student should see one seat per track, not three of
    the first track and nothing about the second."""
    rows = sourcing.suggestions_for({"name": "Citi"}, FakeUser(["ib", "st"]))
    labels = [r["label"] for r in rows]
    assert labels[0] == sourcing.TRACK_ARCHETYPES["ib"][0][0]
    assert labels[1] == sourcing.TRACK_ARCHETYPES["st"][0][0]
    assert labels[2] == sourcing.TRACK_ARCHETYPES["ib"][1][0]


def test_unknown_and_duplicate_tracks_are_ignored_not_fatal():
    """`User.tracks` is a free-form ArrayField. Junk in it is data."""
    rows = sourcing.suggestions_for(
        {"name": "Citi"}, FakeUser(["ib", "ib", "quant-research", ""])
    )
    assert [r["label"] for r in rows] == [
        a[0] for a in sourcing.TRACK_ARCHETYPES["ib"]
    ]


# ---------------------------------------------------------------------------
# 2. The no-track fallback.
# ---------------------------------------------------------------------------
def test_no_tracks_falls_back_to_the_generic_trio():
    for user in (FakeUser(None), FakeUser([]), FakeUser(["nonsense"])):
        rows = sourcing.suggestions_for({"name": "Barclays"}, user)
        assert [r["label"] for r in rows] == [
            a[0] for a in sourcing.GENERIC_ARCHETYPES
        ]


def test_a_user_object_without_the_fields_at_all_still_answers():
    """Nothing here may assume the caller passed a full User."""
    rows = sourcing.suggestions_for({"name": "Barclays"}, object())
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 3. The school row.
# ---------------------------------------------------------------------------
def test_school_claims_the_last_slot_rather_than_adding_a_fourth():
    with_school = sourcing.suggestions_for(
        {"name": "Lazard"}, FakeUser(["ib"], school="USC")
    )
    without = sourcing.suggestions_for({"name": "Lazard"}, FakeUser(["ib"]))
    assert len(with_school) == len(without) == 3
    assert with_school[-1]["key"] == "alumni"
    assert with_school[-1]["label"] == "Someone from USC"
    # The two archetype rows above it are the track's first two, unchanged.
    assert [r["label"] for r in with_school[:2]] == [r["label"] for r in without[:2]]
    assert _keywords(with_school[-1]["linkedin_url"]) == '"Lazard" "USC"'


def test_a_blank_school_adds_no_alumni_row():
    for school in ("", "   ", None):
        rows = sourcing.suggestions_for(
            {"name": "Lazard"}, FakeUser(["ib"], school=school)
        )
        assert [r["key"] for r in rows] == ["ib-0", "ib-1", "ib-2"]


def test_the_generic_trio_also_gives_up_a_slot_to_the_school():
    rows = sourcing.suggestions_for(
        {"name": "Lazard"}, FakeUser(None, school="HKU")
    )
    assert len(rows) == 3
    assert rows[-1]["label"] == "Someone from HKU"
    assert [r["label"] for r in rows[:2]] == [
        a[0] for a in sourcing.GENERIC_ARCHETYPES[:2]
    ]


# ---------------------------------------------------------------------------
# 4. The URL. This is the part a student actually clicks.
# ---------------------------------------------------------------------------
def test_the_firm_name_is_phrase_quoted():
    """Unquoted, "Bank of America" matches every person in America."""
    rows = sourcing.suggestions_for({"name": "Bank of America"}, FakeUser(["ib"]))
    assert _keywords(rows[0]["linkedin_url"]).startswith('"Bank of America" ')


@pytest.mark.parametrize(
    "name",
    ["Rothschild & Co", "S&P Global", "J.P. Morgan", "Moelis & Company",
     "Houlihan Lokey", "Crédit Agricole", "BofA/Merrill"],
)
def test_odd_firm_names_survive_the_round_trip(name):
    """An unencoded `&` would truncate the query string at the ampersand
    and quietly search for the first word alone."""
    rows = sourcing.suggestions_for({"name": name}, FakeUser(["ib"], school="USC"))
    for row in rows:
        url = row["linkedin_url"]
        assert url.startswith(sourcing.LINKEDIN_PEOPLE_SEARCH + "?keywords=")
        assert "&" not in url.split("?", 1)[1].replace("%26", "")
        assert _keywords(url).startswith(f'"{name}"')


def test_a_nameless_firm_gets_no_suggestions():
    """A keyword search with no firm in it is not a suggestion about this
    firm, so the panel simply does not render."""
    assert sourcing.suggestions_for({"name": ""}, FakeUser(["ib"])) == []
    assert sourcing.suggestions_for({"name": None}, FakeUser(["ib"])) == []
    assert sourcing.suggestions_for(object(), FakeUser(["ib"])) == []


def test_it_reads_a_model_style_object_too():
    class FakeFirm:
        name = "Evercore"

    rows = sourcing.suggestions_for(FakeFirm(), FakeUser(["pe"]))
    assert _keywords(rows[0]["linkedin_url"]).startswith('"Evercore" ')


def test_limit_is_honoured_and_a_zero_limit_renders_nothing():
    assert len(sourcing.suggestions_for({"name": "UBS"}, FakeUser(["ib"]), limit=2)) == 2
    assert sourcing.suggestions_for({"name": "UBS"}, FakeUser(["ib"]), limit=0) == []


def test_keys_are_unique_so_the_event_log_can_tell_rows_apart():
    rows = sourcing.suggestions_for(
        {"name": "UBS"}, FakeUser(["ib", "st"], school="USC")
    )
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == len(keys)


def test_a_key_names_the_same_seat_whatever_order_the_tracks_are_in():
    """The key is what the funnel groups on, so "st-0" has to mean the
    markets flow-desk analyst whether markets was the student's first
    track or their second."""
    first = sourcing.suggestions_for({"name": "UBS"}, FakeUser(["st", "ib"]))
    second = sourcing.suggestions_for({"name": "UBS"}, FakeUser(["ib", "st"]))
    by_key = {r["key"]: r["label"] for r in first}
    for row in second:
        assert by_key.get(row["key"], row["label"]) == row["label"]
    assert by_key["st-0"] == sourcing.TRACK_ARCHETYPES["st"][0][0]


# ---------------------------------------------------------------------------
# 5. The render: the panel on the Coverage Gaps card.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_gap_card_carries_its_who_to_find_panel(client):
    user = User.objects.create_user(email="src@example.com", password="x")
    user.tracks = ["ib"]
    user.school = "USC"
    user.save(update_fields=["tracks", "school"])
    firm = Firm.objects.create(slug="rothschild", name="Rothschild & Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    # Anchored on the rendered element, not the bare words: `crm/_styles.html`
    # is inlined into this same document and its CSS comment names the panel
    # too, so a plain `"Who to find" in body` passes on the stylesheet alone.
    assert '<summary class="src-toggle">Who to find</summary>' in body
    assert sourcing.DISCLOSURE in body
    assert sourcing.TRACK_ARCHETYPES["ib"][0][0] in body
    assert "Someone from USC" in body
    # Outbound, new tab, and no window.opener handed to LinkedIn.
    assert 'rel="noopener noreferrer"' in body
    assert 'target="_blank"' in body
    # The firm name is encoded in the href, ampersand and all.
    assert "Rothschild%20%26%20Co" in body or "Rothschild+%26+Co" in body


@pytest.mark.django_db
def test_an_ordinary_firms_panel_states_its_disclosure_once(client):
    """The panel had two note slots and they resolved to the same sentence.
    `sourcing_note` is the page-level `DISCLOSURE`, `g.sourcing_note` is
    `panel_note(firm)`, and `panel_note` returns `DISCLOSURE` for every firm
    that is not assessment-gated — so an opened panel printed "Suggestions,
    not a list of people. Each one opens a LinkedIn search." twice, one line
    under the other, on five of the six rows the demo board renders.

    Rendered once now. Nothing is lost: the paragraph above the rows still
    carries it, and the per-card slot still prints when it has something the
    paragraph does not, which is what the next test pins.
    """
    user = User.objects.create_user(email="src-once@example.com", password="x")
    user.tracks = ["ib"]
    user.save(update_fields=["tracks"])
    firm = Firm.objects.create(slug="lazard", name="Lazard")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    # The class attribute now also carries `panel panel--raised` (2026-09-02,
    # D-13's panel primitive), so an exact-string match on `class="src-panel"`
    # alone silently matched nothing. Anchored on the class NAME instead of
    # the full attribute.
    panels = re.findall(r'<div class="src-panel[^"]*">(.*?)</div>', body, re.S)
    assert panels, "the firm rendered no sourcing panel"
    assert panels[0].count(sourcing.DISCLOSURE) == 1, (
        "the disclosure is printed twice in one panel again — the per-card "
        "note slot is repeating the page-level one."
    )


@pytest.mark.django_db
def test_an_assessment_firms_panel_still_says_the_thing_only_it_says(client):
    """The per-card slot exists for exactly one case: a firm whose own FAQ
    declines the coffee chat gets a note saying so and pointing at Apply.
    That note is not the disclosure, so it still prints, and the disclosure
    prints above it. Two lines, two facts.
    """
    user = User.objects.create_user(email="src-assess@example.com", password="x")
    user.tracks = ["st"]
    user.save(update_fields=["tracks"])
    firm = Firm.objects.create(
        slug="jane-street", name="Jane Street", recruiting_style="assessment",
        tracks=["st"],
    )
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    # `track_fit` zeroes an assessment firm's gap points, so the only way one
    # reaches this strip at all is on the additive deadline bonus — see the
    # `exposure <= 0` branch in `coverage.rank_gaps`. A confirmed, day-precise
    # close is what puts this row on the board to be asserted against.
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=10),
        confidence=1.0, precision="day",
    )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    panels = re.findall(r'<div class="src-panel[^"]*">(.*?)</div>', body, re.S)
    assert panels, "the firm rendered no sourcing panel"
    assert sourcing.ASSESSMENT_NOTE in panels[0], (
        "the assessment note went with the duplicate; a test-gated firm now "
        "prompts for a chat its own FAQ declines."
    )
    assert panels[0].count(sourcing.DISCLOSURE) == 1


@pytest.mark.django_db
def test_a_covered_firm_has_no_card_and_so_no_panel(client):
    """The panel lives on gap cards only. A firm that is done is not a
    place the product should be sending anyone looking for names."""
    user = User.objects.create_user(email="cov@example.com", password="x")
    firm = Firm.objects.create(slug="done-co", name="Done Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    from crm.models import Contact

    for i in range(2):
        Contact.all_objects.create(
            user=user, name=f"A{i}", firm=firm, warmth="advocate"
        )
    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    # Same anchoring reason as the test above: the inlined stylesheet names
    # the panel in a comment whether or not any card renders one.
    assert '<summary class="src-toggle">' not in body
    assert sourcing.DISCLOSURE not in body


def test_the_panel_drop_is_a_real_animation_and_reduced_motion_turns_it_off():
    """`--t-med` is a TRANSITION token: it expands to a duration AND a
    timing function. Written as `animation: src-drop var(--t-med) ease
    both` that is two timing functions in one shorthand, so the browser
    throws the whole declaration away and the panel appears with no drop
    at all. Measured live at 1440x950: `animationName` computed to "none"
    with `prefers-reduced-motion: no-preference`, which is the value the
    reduced-motion guard below is supposed to be the only way to get.

    An invalid shorthand is silent in every unit test that reads the
    rendered page, so the check has to be on the declaration itself.
    """
    css = " ".join(STYLES.read_text().split())
    match = re.search(r"\.src-panel \{(.*?)\}", css, re.S)
    assert match, ".src-panel is gone from crm/_styles.html"
    shorthand = re.search(r"animation:\s*src-drop([^;]*);", match.group(1))
    assert shorthand, "the panel lost its drop-in animation"
    value = shorthand.group(1)
    assert "var(--t-" not in value, (
        "--t-* are transition tokens (duration + easing together); one in "
        "an animation shorthand alongside a second easing makes the whole "
        "declaration invalid and the animation silently never runs"
    )
    assert re.search(r"\d+ms", value), "the drop needs a literal duration"
    # And the only way to get no animation stays the user's own setting.
    guard = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{([^}]*\.src-panel[^}]*\})",
        css,
    )
    assert guard and "animation: none" in guard.group(1), (
        "the panel's drop must be off under prefers-reduced-motion: reduce"
    )


# ---------------------------------------------------------------------------
# 6. The event endpoint. Fire-and-forget, tenant-scoped.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_opening_the_panel_records_an_event(client):
    from analytics.models import ProductEvent

    user = User.objects.create_user(email="ev@example.com", password="x")
    firm = Firm.objects.create(slug="ev-co", name="Ev Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    client.force_login(user)

    resp = client.post(
        reverse("crm:sourcing_event"), {"firm": "ev-co", "kind": "panel"}
    )
    assert resp.status_code == 200
    event = ProductEvent.all_objects.get(event="sourcing_panel_opened")
    assert event.user_id == user.id
    assert event.props["firm"] == "ev-co"


@pytest.mark.django_db
def test_clicking_a_search_records_which_archetype(client):
    from analytics.models import ProductEvent

    user = User.objects.create_user(email="ev2@example.com", password="x")
    firm = Firm.objects.create(slug="ev2-co", name="Ev2 Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    client.force_login(user)

    client.post(
        reverse("crm:sourcing_event"),
        {"firm": "ev2-co", "kind": "search", "archetype": "ib-0"},
    )
    event = ProductEvent.all_objects.get(event="sourcing_search_opened")
    assert event.props["archetype"] == "ib-0"


@pytest.mark.django_db
def test_another_students_firm_is_not_loggable(client):
    """The slug comes off a POST body, so it gets the same `.for_user`
    treatment every other id in this app gets."""
    from analytics.models import ProductEvent

    mine = User.objects.create_user(email="mine@example.com", password="x")
    theirs = User.objects.create_user(email="theirs@example.com", password="x")
    firm = Firm.objects.create(slug="not-mine", name="Not Mine")
    UserFirm.all_objects.create(user=theirs, firm=firm, tier=1)

    client.force_login(mine)
    resp = client.post(
        reverse("crm:sourcing_event"), {"firm": "not-mine", "kind": "panel"}
    )
    assert resp.status_code == 404
    assert not ProductEvent.all_objects.filter(event="sourcing_panel_opened").exists()


@pytest.mark.django_db
def test_an_unknown_kind_is_rejected(client):
    user = User.objects.create_user(email="bad@example.com", password="x")
    firm = Firm.objects.create(slug="bad-co", name="Bad Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    client.force_login(user)
    resp = client.post(
        reverse("crm:sourcing_event"), {"firm": "bad-co", "kind": "wat"}
    )
    assert resp.status_code == 400
