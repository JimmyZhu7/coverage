"""Affinity: what a shared school is worth in `crm.relevance.expected_value`
(section 4b, 2026-09-01).

THE DEFECT, measured on the founder's account: `school_affiliation` was an
admission flag and nothing more. At a tiered firm it changed a card's ev by
0.00 (8.64 vs 8.64 for an alumnus and a stranger at the same tier-1 bank);
at a non-tiered firm `_SCHOOL_WEIGHT` (0.9) sat below the weight of a
stranger at an unranked firm (1.2).

THE EVIDENCE the multipliers carry: on a counted log of 93 cold emails,
alumni replied at 43% against 34% for strangers — a ~1.3x lift, not the
4-6x folklore — and a high-school-directory approach replied at 85%+
against ~25% for a bare college tie, so a NAMED tie earns 1.6.

The last section goes through the real queue to prove the class ladder is
untouched: a cold alumnus with the best tie on the board still sits below a
stranger who actually wrote back.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm import relevance as rel
from crm.models import Contact, Touch, UserFirm
from crm.today import _build_actions, _cockpit_context
from directory.models import Firm


def _action(**kw):
    contact = {"warmth": kw.pop("warmth", "cold")}
    for key in ("school_affiliation", "school", "angle", "notes", "role"):
        if key in kw:
            contact[key] = kw.pop(key)
    base = {
        "contact": contact,
        "action": kw.pop("action", "follow_up"),
        "priority": kw.pop("priority", 1),
        "relevance": kw.pop("relevance", rel.REL_TIERED),
        "relevance_tier": kw.pop("tier", 1),
    }
    base.update(kw)
    return base


class FakeUser:
    def __init__(self, affiliations=None):
        if affiliations is not None:
            self.affiliations = affiliations


# ---------------------------------------------------------------------------
# 1. The multiplier table.
# ---------------------------------------------------------------------------
def test_a_stranger_is_one_point_zero():
    assert rel.affinity({"warmth": "cold"}) == 1.0
    assert rel.affinity({}) == 1.0


def test_the_bare_flag_is_the_measured_lift():
    assert rel.affinity({"school_affiliation": True}) == 1.3
    assert rel._AFFINITY_SCHOOL == 1.3, "43% vs 34% on 93 emails is 1.3x, not folklore's 4-6x"


def test_a_named_tie_beats_the_flag():
    c = {"school_affiliation": True, "notes": "Met at the Trojan Investing Society mixer."}
    assert rel.specific_tie(c) == "Society"
    assert rel.affinity(c) == 1.6


def test_a_named_tie_counts_without_the_flag_too():
    """A prior employer or a hometown is a tie whatever the school."""
    c = {"school_affiliation": False, "angle": "former colleague from the Deloitte summer"}
    assert rel.affinity(c) == 1.6


@pytest.mark.parametrize("text", [
    "same programme at Marshall",
    "my cohort",
    "grew up in Shenzhen too, hometown connection",
    "went to the same high school",
    "we interned together at HSBC",
    "prior employer overlap",
    "classmate in BUAD 306",
    "referred by Amy Zhou",
    "intro from Patina",
    "mutual friend with James",
    "finance club treasurer",
])
def test_tie_markers_are_recognised(text):
    assert rel.affinity({"notes": text}) == 1.6


@pytest.mark.parametrize("text", [
    "Summer Analyst Programme 2027",   # a job, not a bond
    "IB analyst, TMT",
    "USC",                             # the flag's job, not a specific tie
    "finance professional",
    "MBA, Wharton",
    "Recruiting",
    "",
])
def test_topics_and_credentials_are_not_ties(text):
    assert rel.affinity({"notes": text}) == 1.0
    assert rel.affinity({"notes": text, "school_affiliation": True}) == 1.3


def test_role_is_never_scanned():
    """"Analyst Programme" in a title is a job; the markers read `school`,
    `angle` and `notes` only."""
    assert rel.affinity({"role": "Analyst, Investment Banking Society liaison"}) == 1.0


def test_the_contacts_school_field_is_scanned():
    assert rel.affinity({"school": "Marshall, same cohort"}) == 1.6


# ---------------------------------------------------------------------------
# 2. The student's own affiliations (a User column another change is adding).
# ---------------------------------------------------------------------------
def test_a_user_affiliation_named_in_the_contacts_text_is_a_specific_tie():
    user = FakeUser(["Trojan Investing Society", "Shenzhen"])
    c = {"school_affiliation": True, "notes": "shenzhen native, analyst at Citi"}
    assert rel.specific_tie(c, user) == "Shenzhen"
    assert rel.affinity(c, user) == 1.6


def test_a_user_without_the_column_yet_degrades_to_the_flag():
    c = {"school_affiliation": True, "notes": "nothing specific here"}
    assert rel.affinity(c, FakeUser()) == 1.3
    assert rel.affinity(c, None) == 1.3
    assert rel.affinity(c, object()) == 1.3


def test_short_affiliations_cannot_match_inside_other_words():
    user = FakeUser(["PE", "", None])
    assert rel.affinity({"notes": "open to a chat"}, user) == 1.0


def test_affiliations_that_are_not_strings_are_data_not_a_crash():
    user = FakeUser([42, {"x": 1}, "Marshall"])
    assert rel.affinity({"notes": "Marshall grad"}, user) == 1.6


# ---------------------------------------------------------------------------
# 3. Inside expected_value.
# ---------------------------------------------------------------------------
def test_an_alumnus_now_outscores_the_same_stranger_by_the_measured_lift():
    """The measured pair: tier-1, replied, `advance` — 8.64 vs 8.64 before."""
    stranger = _action(warmth="replied", action="advance", tier=1)
    alumnus = _action(warmth="replied", action="advance", tier=1, school_affiliation=True)
    assert rel.expected_value(stranger) == 8.64
    assert rel.expected_value(alumnus) == round(8.64 * 1.3, 4)


def test_a_specific_tie_scores_one_point_six():
    stranger = _action(warmth="cold", tier=2)
    tied = _action(warmth="cold", tier=2, school_affiliation=True,
                   notes="same club, worked together on the case comp")
    assert rel.expected_value(tied) == round(rel.expected_value(stranger) * 1.6, 4)


def test_the_user_is_optional_and_only_feeds_affiliations():
    a = _action(warmth="cold", tier=2, notes="grew up in Hangzhou")
    assert rel.expected_value(a) == rel.expected_value(a, None)
    assert rel.expected_value(a, FakeUser(["Hangzhou"])) == \
        round(rel.expected_value(_action(warmth="cold", tier=2)) * 1.6, 4)


def test_a_school_tie_at_a_non_tiered_firm_no_longer_scores_below_a_stranger_at_an_unranked_one():
    """0.9 × 0.8 = 0.72 used to sit below 1.2 × 0.8 = 0.96. With the lift the
    alumnus reads 0.936 — still below, by design: the admission weight is
    untouched, the tier list is the student's own ranking, and the research
    lift is 1.3x, not enough to overturn it. Pinned so the number is a
    decision and not a drift."""
    alumnus = _action(warmth="cold", relevance=rel.REL_SCHOOL, tier=None, school_affiliation=True)
    unranked = _action(warmth="cold", relevance=rel.REL_TIERED, tier=None)
    assert rel.relevance_weight(rel.REL_SCHOOL, None) == 0.9
    assert rel.expected_value(alumnus) == round(0.9 * 0.8 * 1.3 * 1.0, 4)
    assert rel.expected_value(alumnus) < rel.expected_value(unranked)


def test_the_admission_ladder_is_untouched():
    """REL_SCHOOL still admits, still weighs 0.9, and the flag still changes
    nothing about WHICH reason admits a contact at a tiered firm."""
    assert rel.contact_relevance({"firm_id": 1, "school_affiliation": True}, {1: 1},
                                 owed_reply=False) == rel.REL_TIERED
    assert rel.contact_relevance({"firm_id": 9, "school_affiliation": True}, {1: 1},
                                 owed_reply=False) == rel.REL_SCHOOL
    assert rel._SCHOOL_WEIGHT == 0.9


# ---------------------------------------------------------------------------
# 4. The class ladder wins over any lift.
# ---------------------------------------------------------------------------
pytestmark_db = pytest.mark.django_db(transaction=True)


def _user(email="aff@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


@pytestmark_db
def test_a_cold_alumnus_never_outranks_a_stranger_who_wrote_back():
    """THE FENCE. A cold alumnus at a tier-1 firm outscores, on `ev` alone,
    a stranger at no target firm who wrote and is still waiting on an answer
    (3.0 × 0.8 × 1.3 × 1.0 = 3.12 against the inbound override's 0.5 × 1.6
    × 3.0 = 2.4), and the plan must still put the stranger first, because
    `_today_class` ranks engagement before any score. The affinity is a
    multiplier on strength, never a rung on the ladder."""
    user = _user()
    bank = Firm.objects.create(slug="aff-bank", name="Aff Bank")
    UserFirm.all_objects.create(user=user, firm=bank, tier=1)
    alum = Contact.all_objects.create(
        user=user, name="Cold Alum", firm=bank, school_affiliation=True,
    )
    _touch(user, alum, "outreach", days_ago=12)

    stranger = Contact.all_objects.create(
        user=user, name="Wrote Back", warmth="replied", thread_state="replied",
    )
    _touch(user, stranger, "reply_received", days_ago=12)

    actions, _ = _build_actions(user)
    by_name = {a["contact"]["name"]: a for a in actions}
    assert by_name["Cold Alum"]["action"] == "follow_up"
    assert by_name["Wrote Back"]["action"] == "advance"
    assert by_name["Wrote Back"]["relevance"] == rel.REL_INBOUND
    # The lift is live on the real queue: 1.3 rides on the alumnus's card.
    assert by_name["Cold Alum"]["ev"] == round(3.0 * 0.8 * 1.3 * 1.0, 4)
    assert by_name["Cold Alum"]["ev"] > by_name["Wrote Back"]["ev"], (
        "precondition: the alumnus must OUTSCORE the stranger, else this "
        "test proves nothing about the fence"
    )

    ctx = _cockpit_context(user)
    names = [a["contact"]["name"] for lane in ctx["lanes"] for a in lane["items"]]
    assert names[0] == "Wrote Back", (
        f"a higher-scoring cold alumnus must still sit below someone who "
        f"engaged, got {names}"
    )


@pytestmark_db
def test_the_lift_orders_two_cold_cards_within_their_own_class():
    """Where the ladder says nothing — two cold follow-ups at the same tier —
    the alumnus goes first. That is the lift doing its one job."""
    user = _user("aff2@example.com")
    bank = Firm.objects.create(slug="aff-bank-2", name="Aff Bank")
    UserFirm.all_objects.create(user=user, firm=bank, tier=1)
    alum = Contact.all_objects.create(
        user=user, name="Alum First", firm=bank, school_affiliation=True,
    )
    stranger = Contact.all_objects.create(user=user, name="Stranger Second", firm=bank)
    _touch(user, alum, "outreach", days_ago=12)
    _touch(user, stranger, "outreach", days_ago=12)

    ctx = _cockpit_context(user)
    names = [a["contact"]["name"] for lane in ctx["lanes"] for a in lane["items"]]
    assert names.index("Alum First") < names.index("Stranger Second")
