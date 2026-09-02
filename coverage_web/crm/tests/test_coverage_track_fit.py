"""Track fit, recruiting style and the advocate aggregate in the Coverage
Gaps formula (crm/coverage.py, terms 4 and 5, 2026-09-01).

THE DEFECT, measured on the founder's account that day: 18 of his 25
zero-contact tiered firms were OFF his tracks (7 PE, 3 AM, 2 consulting, 6
corp-strat tech), and eleven of them — Apollo, Ares, Bain & Co, Bain
Capital, BlackRock, Blue Owl, Carlyle, Fidelity International, KKR,
McKinsey, Oaktree, PIMCO — outranked HSBC, a tier-1 bank on his track with
8 contacts and a confirmed close on 2026-10-30. `rank_gaps` never read
`firm.tracks` or `user.tracks`, and it ranked Jane Street "No contacts ·
exp 12" at a firm whose own FAQ says a coffee chat does nothing.

`crm.coverage` is pure, so most of this is plain unit tests over dicts with
an explicit `today`; the last section goes through the Network page to
prove the view hands the new facts over.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import coverage, sourcing
from crm.models import Contact, UserFirm
from directory.models import Firm, FirmDate

User = get_user_model()

TODAY = date(2026, 9, 1)


def _firm(name, tier, warmths, *, firm_tracks=None, user_tracks=None,
          style=None, app_close=None, firm_id=None):
    row = {
        "firm_id": firm_id if firm_id is not None else name,
        "name": name,
        "tier": tier,
        "warmths": warmths,
        "app_close": app_close,
    }
    if firm_tracks is not None:
        row["firm_tracks"] = firm_tracks
    if user_tracks is not None:
        row["user_tracks"] = user_tracks
    if style is not None:
        row["recruiting_style"] = style
    return row


def _rank(rows, **kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("limit", 50)
    return coverage.rank_gaps(rows, **kw)


# ---------------------------------------------------------------------------
# 1. track_fit itself.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("firm_tracks, user_tracks, fit", [
    (["ib"], ["ib"], 1.0),
    (["ib", "st"], ["st"], 1.0),
    (["st", "am"], ["ib", "st"], 1.0),        # the founder: two tracks, one shared
    (["pe"], ["ib", "st"], 0.5),
    (["corp-strat"], ["ib"], 0.5),
    ([], ["ib"], 1.0),                        # firm unclassified: not "off track"
    (["pe"], [], 1.0),                        # student skipped onboarding
    ([], [], 1.0),
    (None, None, 1.0),
    (["", "pe"], ["ib"], 0.5),                # blanks are not a track
])
def test_track_fit_table(firm_tracks, user_tracks, fit):
    assert coverage.track_fit(firm_tracks, user_tracks) == fit


# ---------------------------------------------------------------------------
# 2. What the multiplier does to the ranking.
# ---------------------------------------------------------------------------
def test_the_founders_measured_inversion_is_fixed():
    """HSBC (tier 1, on track, two chatted contacts, close 59 days out) vs
    Apollo (tier 2, PE, nobody). Before: Apollo 8 > HSBC 7. After: HSBC 7 >
    Apollo 4."""
    user_tracks = ["ib", "st"]
    hsbc = _firm("HSBC", 1, ["cold"] * 6 + ["chatted"] * 2, firm_tracks=["ib"],
                 user_tracks=user_tracks, app_close=TODAY + timedelta(days=59))
    apollo = _firm("Apollo", 2, [], firm_tracks=["pe"], user_tracks=user_tracks)

    without_tracks = _rank([
        dict(hsbc, firm_tracks=[], user_tracks=[]),
        dict(apollo, firm_tracks=[], user_tracks=[]),
    ])
    assert [g["name"] for g in without_tracks] == ["Apollo", "HSBC"]
    assert [g["exposure"] for g in without_tracks] == [8, 7]

    ranked = _rank([hsbc, apollo])
    assert [g["name"] for g in ranked] == ["HSBC", "Apollo"]
    assert [g["exposure"] for g in ranked] == [7, 4]


def test_an_off_track_firm_says_why_it_sank():
    g = _rank([_firm("KKR", 2, [], firm_tracks=["pe"], user_tracks=["ib"])])[0]
    assert g["state"] == coverage.OFF_TRACK
    assert g["label"] == "Not on your tracks"
    assert g["off_track"] is True
    assert g["track_fit"] == 0.5
    # The rung it would otherwise show rides along, so nothing is lost.
    assert g["ladder_state"] == coverage.NO_CONTACTS
    assert g["ladder_label"] == "No contacts"
    assert g["ladder_points"] == 4
    assert g["gap_points"] == 2
    assert g["exposure"] == 4


def test_an_on_track_firm_is_untouched():
    g = _rank([_firm("HSBC", 2, [], firm_tracks=["ib"], user_tracks=["ib"])])[0]
    assert g["state"] == coverage.NO_CONTACTS
    assert g["label"] == "No contacts"
    assert g["off_track"] is False
    assert g["track_fit"] == 1.0
    assert g["gap_points"] == 4 and g["exposure"] == 8


def test_half_lands_where_a_student_would_put_it():
    """The ordering claim in the formula comment: a tier-2 off-track firm
    with nobody sits below a tier-1 on-track firm with no advocate and above
    a tier-3 on-track firm one advocate short."""
    ranked = _rank([
        _firm("T2 off-track, nobody", 2, [], firm_tracks=["pe"], user_tracks=["ib"]),
        _firm("T1 on-track, no advocate", 1, ["replied"], firm_tracks=["ib"], user_tracks=["ib"]),
        _firm("T3 on-track, one short", 3, ["advocate"], firm_tracks=["ib"], user_tracks=["ib"]),
    ])
    assert [g["name"] for g in ranked] == [
        "T1 on-track, no advocate", "T2 off-track, nobody", "T3 on-track, one short",
    ]
    assert [g["exposure"] for g in ranked] == [6, 4, 1]


def test_the_cards_arithmetic_stays_true_after_the_multiplier():
    """`tier_weight × gap_points + deadline_bonus == exposure` on every card,
    because `gap_points` is the FITTED number — the hover math the template
    already prints does not need to know about tracks."""
    rows = [
        _firm("A", 1, [], firm_tracks=["pe"], user_tracks=["ib"],
              app_close=TODAY + timedelta(days=10)),
        _firm("B", 3, ["cold"], firm_tracks=["am"], user_tracks=["ib"]),
        _firm("C", 2, ["replied"], firm_tracks=["ib"], user_tracks=["ib"]),
        _firm("D", 1, ["advocate"], firm_tracks=["consulting"], user_tracks=["ib"]),
    ]
    for g in _rank(rows):
        assert g["tier_weight"] * g["gap_points"] + g["deadline_bonus"] == g["exposure"]
    by_name = {g["name"]: g for g in _rank(rows)}
    assert by_name["B"]["gap_points"] == 1.5 and by_name["B"]["exposure"] == 1.5
    assert by_name["D"]["gap_points"] == 0.5 and by_name["D"]["exposure"] == 1.5
    assert by_name["A"]["exposure"] == 9   # 3 × 2 + 3, printed as an int


def test_a_multi_track_student_is_never_penalised_for_the_other_track():
    """The founder runs ib and st. A pure S&T shop is fully on track for him
    even though it has no IB seat."""
    g = _rank([_firm("Optiver", 1, [], firm_tracks=["st"], user_tracks=["ib", "st"])])[0]
    assert g["track_fit"] == 1.0 and g["state"] == coverage.NO_CONTACTS


def test_no_tracks_on_either_side_is_todays_ranking_exactly():
    """The degrade rule, asserted as identity: rows with no track keys at
    all, rows with empty lists, and rows with only one side known all rank
    identically and carry the pre-2026-09-01 exposures."""
    base = [
        _firm("T1 nobody", 1, [], app_close=TODAY + timedelta(days=20)),
        _firm("T1 cold", 1, ["cold", "cold"]),
        _firm("T2 nobody", 2, []),
        _firm("T3 no advocate", 3, ["chatted"]),
        _firm("T2 short", 2, ["advocate"]),
    ]
    plain = _rank(base)
    empties = _rank([dict(r, firm_tracks=[], user_tracks=[]) for r in base])
    user_only = _rank([dict(r, user_tracks=["ib", "st"]) for r in base])
    firm_only = _rank([dict(r, firm_tracks=["pe"]) for r in base])
    for variant in (empties, user_only, firm_only):
        assert [(g["name"], g["exposure"], g["state"]) for g in variant] == \
               [(g["name"], g["exposure"], g["state"]) for g in plain]
    assert [g["exposure"] for g in plain] == [14, 9, 8, 2, 2]
    assert all(g["track_fit"] == 1.0 and not g["off_track"] for g in plain)
    assert all(g["verb"] == coverage.VERB_ADD and g["verb_reason"] == "" for g in plain)


# ---------------------------------------------------------------------------
# 3. Assessment firms.
# ---------------------------------------------------------------------------
def test_an_assessment_firm_with_no_confirmed_close_is_not_a_gap_at_all():
    """Jane Street, tier 1, nobody: the old strip read "No contacts · exp
    12". There is no networking gap to close at a firm that hires off a
    test, and no confirmed close to apply for, so the card is not drawn."""
    ranked = _rank([
        _firm("Jane Street", 1, [], firm_tracks=["st"], user_tracks=["st"],
              style=coverage.ASSESSMENT),
        _firm("Goldman Sachs", 1, [], firm_tracks=["st"], user_tracks=["st"]),
    ])
    assert [g["name"] for g in ranked] == ["Goldman Sachs"]


def test_an_assessment_firm_with_a_close_is_on_the_strip_for_the_deadline_alone():
    g = _rank([_firm("Jane Street", 1, [], firm_tracks=["st"], user_tracks=["st"],
                     style=coverage.ASSESSMENT,
                     app_close=TODAY + timedelta(days=10))])[0]
    assert g["gap_points"] == 0
    assert g["deadline_bonus"] == 3
    assert g["exposure"] == 3
    assert g["verb"] == coverage.VERB_APPLY == "Apply"
    assert g["verb_reason"] == "They hire off their test, not off a chat."
    assert g["recruiting_style"] == coverage.ASSESSMENT
    # The ladder rung is still reported honestly; only the points are zeroed.
    assert g["ladder_state"] == coverage.NO_CONTACTS and g["label"] == "No contacts"


def test_the_same_firm_tagged_campus_keeps_todays_card():
    g = _rank([_firm("Jane Street", 1, [], firm_tracks=["st"], user_tracks=["st"],
                     style=coverage.CAMPUS, app_close=TODAY + timedelta(days=10))])[0]
    assert g["exposure"] == 15 and g["verb"] == "Add" and g["verb_reason"] == ""


def test_an_assessment_firm_ranks_below_every_networking_gap_with_the_same_deadline():
    ranked = _rank([
        _firm("Jane Street", 1, [], style=coverage.ASSESSMENT,
              app_close=TODAY + timedelta(days=10)),
        _firm("Tier 3, one short", 3, ["advocate"], app_close=TODAY + timedelta(days=10)),
    ])
    assert [g["name"] for g in ranked] == ["Tier 3, one short", "Jane Street"]
    assert [g["exposure"] for g in ranked] == [4, 3]


def test_a_covered_firm_is_still_never_drawn():
    """The `state == COVERED` skip replaced `if not points`, and must still
    hold even when a covered firm has a deadline that would otherwise add
    a bonus."""
    assert _rank([_firm("Done", 1, ["advocate", "advocate"],
                        app_close=TODAY + timedelta(days=3))]) == []


# ---------------------------------------------------------------------------
# 4. The advocate aggregate.
# ---------------------------------------------------------------------------
def test_advocate_summary_counts_across_tiered_firms_only():
    """The line spells the two yardsticks out rather than hyphenating them.

    It read "aim for 2-20", which is one range: somewhere between two and
    twenty advocates, total. `ADVOCATE_RANGE` is not a range — `low` is the
    per-firm target and `high` is the total across every target firm — so
    the hyphen was stating a number the research never claimed. Both figures
    still come from the constant; these assertions pin the wording, not the
    arithmetic, which the `low`/`high` assertion below covers separately.
    """
    rows = [
        _firm("A", 1, ["advocate", "advocate", "cold"]),
        _firm("B", 2, ["advocate", "chatted"]),
        _firm("C", 3, []),
        _firm("Unranked", None, ["advocate"] * 5),
    ]
    s = coverage.advocate_summary(rows, target=2)
    assert s["advocates"] == 3
    assert s["firms"] == 3
    assert s["covered"] == 1
    assert s["target"] == 2
    assert (s["low"], s["high"]) == coverage.ADVOCATE_RANGE == (2, 20)
    assert s["line"] == "Advocates: 3 across 3 target firms · aim for 2 per firm, 20 in all"


def test_advocate_summary_on_the_founders_board_reads_zero_honestly():
    """54 tiered firms, every one at zero, two advocates at a free-text firm
    that is not a UserFirm row: the line says 0, not 2."""
    rows = [_firm(f"Firm {i:02d}", 1 + i % 3, ["cold"] * (i % 4)) for i in range(54)]
    s = coverage.advocate_summary(rows, target=2)
    assert s["line"] == "Advocates: 0 across 54 target firms · aim for 2 per firm, 20 in all"


def test_advocate_summary_singular_and_empty():
    assert coverage.advocate_summary([_firm("A", 1, [])], target=2)["line"] == \
        "Advocates: 0 across 1 target firm · aim for 2 per firm, 20 in all"
    s = coverage.advocate_summary([], target=2)
    assert s["line"] == "Advocates: 0 across 0 target firms · aim for 2 per firm, 20 in all"
    assert s["advocates"] == s["firms"] == s["covered"] == 0


# ---------------------------------------------------------------------------
# 5. The view hands the facts over.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_network_page_ranks_with_tracks_and_style_and_exposes_the_aggregate(client):
    user = User.objects.create_user(
        email="fit@example.com", password="pw12345!", tracks=["ib"],
    )
    hsbc = Firm.objects.create(slug="hsbc-fit", name="HSBC", tracks=["ib"])
    kkr = Firm.objects.create(slug="kkr-fit", name="KKR", tracks=["pe"])
    jane = Firm.objects.create(
        slug="janestreet-fit", name="Jane Street", tracks=["st"],
        recruiting_style=Firm.RECRUITING_STYLE_ASSESSMENT,
    )
    for firm in (hsbc, kkr, jane):
        UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Contact.all_objects.create(user=user, name="Cold Banker", firm=hsbc)
    FirmDate.objects.create(
        firm=jane, cycle="sa2028", region="us", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=10),
        precision="day", confidence=1.0,
    )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    assert resp.status_code == 200
    gaps = {g["name"]: g for g in resp.context["gaps"]}

    # HSBC (all cold, on track: 3 × 3 = 9) over KKR (nobody, off track:
    # 3 × 4 × 0.5 = 6) over Jane Street (deadline alone: 3).
    assert [g["name"] for g in resp.context["gaps"]] == ["HSBC", "KKR", "Jane Street"]
    assert gaps["KKR"]["state"] == coverage.OFF_TRACK
    assert gaps["KKR"]["label"] == "Not on your tracks"
    assert gaps["Jane Street"]["verb"] == "Apply"
    assert gaps["Jane Street"]["verb_reason"] == "They hire off their test, not off a chat."
    assert gaps["Jane Street"]["apply_url"] == reverse(
        "directory:firm_detail", args=["janestreet-fit"])
    assert gaps["Jane Street"]["sourcing_note"] == sourcing.ASSESSMENT_NOTE
    assert gaps["HSBC"]["apply_url"] == "" and gaps["HSBC"]["verb"] == "Add"
    assert gaps["HSBC"]["sourcing_note"] == sourcing.DISCLOSURE

    summary = resp.context["advocate_summary"]
    assert summary["line"] == "Advocates: 0 across 3 target firms · aim for 2 per firm, 20 in all"
    assert summary["firms"] == 3 and summary["advocates"] == 0


@pytest.mark.django_db
def test_the_advocate_line_is_off_the_face_and_on_the_headings_title(client):
    """The founder quoted this caption back word for word and asked for it
    gone from the page. It was a rendered `<p class="strip-note">` under the
    "Coverage Gaps" heading.

    Cut the explanation, keep the fact: `advocate_summary` still computes it,
    the view still puts it in the context (asserted directly above), and it
    is in the heading's own `title=` — the same convention the rest of this
    strip already runs on, where every number taken off a face is in the
    `title` of the thing it describes.

    Asserted on the STRIPPED text rather than on the raw HTML, because the
    hover version of a sentence and the printed version of it are the same
    characters and only one of them is the thing that was rejected.
    """
    user = User.objects.create_user(
        email="advline@example.com", password="pw12345!", tracks=["ib"]
    )
    firm = Firm.objects.create(slug="hsbc-adv", name="HSBC", tracks=["ib"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    line = "Advocates: 0 across 1 target firm · aim for 2 per firm, 20 in all"

    heading = re.search(
        r'<h2 class="strip-title strip-title-lg" title="([^"]*)">Coverage Gaps</h2>',
        body,
    )
    assert heading, "the Coverage Gaps heading is gone or lost its tooltip"
    assert line in heading.group(1), "the advocate count is not reachable at all"

    assert 'class="strip-note"' not in body, "the caption element is back"
    text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    assert line not in text, (
        "the advocate line is printed on the page again; it may only be a "
        "hover on the heading"
    )


@pytest.mark.django_db
def test_network_page_without_a_close_draws_no_card_for_an_assessment_firm(client):
    user = User.objects.create_user(email="fit2@example.com", password="pw12345!", tracks=["st"])
    jane = Firm.objects.create(
        slug="janestreet-fit2", name="Jane Street", tracks=["st"],
        recruiting_style="assessment",
    )
    gs = Firm.objects.create(slug="gs-fit2", name="Goldman Sachs", tracks=["st"])
    UserFirm.all_objects.create(user=user, firm=jane, tier=1)
    UserFirm.all_objects.create(user=user, firm=gs, tier=1)

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    assert [g["name"] for g in resp.context["gaps"]] == ["Goldman Sachs"]


# ---------------------------------------------------------------------------
# D-3: a retired track is not a track the strip ranks by.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_gap_strip_reads_a_retired_track_as_no_longer_stated(client):
    """The 28% of the founder's zero-contact gap work that `corp-strat`
    manufactured, in miniature. A student holding `['ib', 'corp-strat']`
    used to make Google an ON-track tier-1 firm with nobody at it, worth a
    full 12 and the top of the strip. The slug is retired, so the strip
    ranks Google as the off-track firm it is (12 × 0.5 = 6) and the bank on
    the track the student can actually still choose comes first.

    Read-time, and nothing is rewritten: `User.tracks` still holds both."""
    user = User.objects.create_user(
        email="retired@example.com", password="pw12345!",
        tracks=["ib", "corp-strat"],
    )
    google = Firm.objects.create(slug="google-fit", name="Google", tracks=["corp-strat"])
    hsbc = Firm.objects.create(slug="hsbc-fit3", name="HSBC", tracks=["ib"])
    UserFirm.all_objects.create(user=user, firm=google, tier=1)
    UserFirm.all_objects.create(user=user, firm=hsbc, tier=1)
    Contact.all_objects.create(user=user, name="Cold One", firm=hsbc, warmth="cold")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    gaps = {g["name"]: g for g in resp.context["gaps"]}

    assert [g["name"] for g in resp.context["gaps"]] == ["HSBC", "Google"]
    assert gaps["Google"]["state"] == coverage.OFF_TRACK
    assert gaps["Google"]["track_fit"] == coverage.OFF_TRACK_FIT

    user.refresh_from_db()
    assert user.tracks == ["ib", "corp-strat"]
