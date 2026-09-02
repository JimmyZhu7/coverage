"""The recruitment-relevance rule: who belongs in Coverage at all.

Every named case below is one of the founder's own live rows, measured
2026-08-25 on his 131 active contacts (see `crm/recruitment.py` for the whole
doctrine and the reversal of the school-tie exemption it carries):

  - the nine CLSA / CMB International bankers his tier list would have hidden;
  - the two Amazon rows his tier list would have kept;
  - the two professors and the campus advising office his school tie kept;
  - the campus recruiters and finance-club peers that same tie was right about.

The MUST-keep / MUST-hide split asserted here is the acceptance test the rule
shipped against — if a change here starts special-casing a name, the rule is
wrong, not the name.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from capture import discovery
from capture.models import ContactProposal
from crm import recruitment
from crm import relevance as rel
from crm.models import Contact, UserFirm
from directory.models import Firm

pytestmark = pytest.mark.django_db


def _user(email="rec@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


# ---------------------------------------------------------------------------
# The pure ladder.
# ---------------------------------------------------------------------------

def classify(role="", **kw):
    return recruitment.classify_person(role=role, **kw)


def test_recruiting_function_keeps_whoever_employs_them():
    # Deloitte/PwC/Bain/KPMG campus recruiters (filed under firm "usc") and
    # the West Monroe talent-acquisition manager: recruiting IS the job.
    for role in (
        "Campus recruiter (PwC) — self-described 'the campus recruiter'",
        "Manager, Global Recruiting, Bain & Company",
        "Bain campus recruiting lead for USC",
        "Manager, Talent Acquisition",
    ):
        assert classify(role).verdict == recruitment.KEEP, role


def test_track_role_keeps_off_tier_bankers():
    # The trap the tier test fails: CLSA and CMB International are not on the
    # founder's tier list, and their people pass on the role alone.
    for role in ("IB Analyst", "IB Associate", "IB VP",
                 "Markets/banking professional", "Global Markets Analyst",
                 "PE Associate", "Equities Sales", "FX Trader"):
        v = classify(role)
        assert v.verdict == recruitment.KEEP, role
        assert v.code in ("track_role",), role


def test_track_vocabulary_beats_off_track_words():
    # Ordering is the safety mechanism: "Equities Sales" is a markets seat,
    # "Fintech IB Associate" is a banker — the off-track words inside those
    # roles must never fire because the keep vocabulary is consulted first.
    assert classify("Equities Sales").verdict == recruitment.KEEP
    assert classify("Fintech IB Associate").verdict == recruitment.KEEP
    assert classify("USC alum, investment banking/fintech professional").verdict \
        == recruitment.KEEP


def test_finance_club_peers_keep():
    for role in (
        "USC junior/senior peer (Trojan Investing Society Capstone lead)",
        "USC junior/senior peer (finance/restructuring interest club contact)",
        "USC alum, finance professional",
        "USC junior/senior peer (Director of External Relations, "
        "USC International Consulting Club)",
    ):
        assert classify(role).verdict == recruitment.KEEP, role


def test_campus_roles_hide():
    # The reversal of the school exemption: a professor shares the school and
    # is still not part of recruiting. The founder's three verified rows.
    for role in (
        "Professor (USC Dornsife, WRIT 150)",
        "Professor (USC Marshall, BUAD 306)",
        "USC on-campus staff — Assistant Director, Dornsife First-Year "
        "Advising (academic advising, not career services)",
    ):
        v = classify(role)
        assert v.verdict == recruitment.HIDE, role
        assert v.code == "campus"
        # The ledger's sentence quotes the row, never infers.
        assert "Professor" in v.reason or "staff" in v.reason


def test_off_track_role_hides_even_at_a_tiered_firm():
    # The other half of the trap: Amazon is on the founder's tier list for
    # corp-strat, and the person's own seat still decides. Judged on the
    # PERSON, tiered=True must not rescue an AWS account manager.
    for role in ("Account Manager, AWS", "Sales",
                 "USC alum, technology background", "USC alum in fintech",
                 "USC alum, audit/accounting professional"):
        v = classify(role, tiered=True, firm_tracks=("corp-strat",))
        assert v.verdict == recruitment.HIDE, role
        assert v.code == "off_track"


def test_silent_role_falls_back_to_firm_evidence():
    # A blank-role contact at Barclays (tiered) or CLSA (directory tracks) is
    # somebody the user met through recruiting; the firm speaks when the role
    # is silent — but only then.
    assert classify("", tiered=True).code == "tiered_firm"
    assert classify("Intern", firm_tracks=("st",)).code == "track_firm"


def test_no_signal_keeps():
    # "USC junior/senior peer (coffee-chat contact)": no vocabulary either
    # way, no firm — kept. A wrongly hidden person costs a relationship; a
    # wrongly kept one costs a line on a board. The tie goes to keeping.
    v = classify("USC junior/senior peer (coffee-chat contact)")
    assert v.verdict == recruitment.KEEP
    assert v.code == "no_signal"


def test_notes_rescue_but_never_hide():
    # The user's own notes are scanned for KEEP vocabulary ("Beta Sigma
    # Investment Group" rescued a blank-role Jefferies reply on live data) —
    # but free prose never hides anybody: "referencing her AWS background"
    # in a note is a mention, not an occupation.
    kept = classify("", notes="USC | Beta Sigma Investment Group | coffee chat")
    assert kept.verdict == recruitment.KEEP
    assert kept.code == "track_notes"
    not_hidden = classify("", notes="referencing her AWS background and sales org")
    assert not_hidden.verdict == recruitment.KEEP


def test_override_wins_both_ways_over_everything():
    assert classify("Professor (WRIT 150)", override=True).verdict == recruitment.KEEP
    assert classify("IB Analyst", override=False).verdict == recruitment.HIDE


def test_contact_verdict_honours_recruiting_contact_yes():
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Recruiter By Word", role="", recruiting_contact=True
    )
    v = recruitment.contact_verdict(c, tiers={}, firm_tracks={})
    assert v.verdict == recruitment.KEEP
    assert v.code == "recruiter"


# ---------------------------------------------------------------------------
# The queue gate (crm.relevance).
# ---------------------------------------------------------------------------

def test_recruitment_hidden_drops_daily_action_with_inbound_override():
    # Same shape as the campaign gate, judged right after it: hidden people
    # generate no daily action, EXCEPT the one reply they are owed when they
    # actually wrote — answering a person who wrote to you is basic courtesy,
    # whoever they are.
    hidden = {"recruitment_hidden": True, "firm_id": 1, "school_affiliation": True}
    assert rel.contact_relevance(hidden, {1: 1}, owed_reply=False) is rel.REL_NONE
    assert rel.contact_relevance(hidden, {1: 1}, owed_reply=True) == rel.REL_INBOUND
    # And the reversal on record: a school tie alone still grants REL_SCHOOL,
    # but only for people the recruitment rule kept.
    kept = {"recruitment_hidden": False, "school_affiliation": True}
    assert rel.contact_relevance(kept, {}, owed_reply=False) == rel.REL_SCHOOL


# ---------------------------------------------------------------------------
# The board (crm.views.contact_list) and the ledger.
# ---------------------------------------------------------------------------

def _board_fixture(user):
    firm = Firm.objects.create(slug="barclays", name="Barclays", tracks=["ib"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    banker = Contact.all_objects.create(
        user=user, name="Kept Banker", firm=firm, role="IB Analyst"
    )
    prof = Contact.all_objects.create(
        user=user, name="Hidden Professor", firm_text="usc",
        role="Professor (USC Dornsife, WRIT 150)", school_affiliation=True,
    )
    return banker, prof


def test_board_hides_unrelated_and_the_product_says_so(client):
    """The board hides the professor; SETTINGS is where it says so.

    The board used to carry that sentence itself, in a meta strip above the
    first contact card. The strip was removed on 2026-08-28 and the count and
    route landed in Settings > Your Data — the guarantee was never the
    strip's to keep, only to state."""
    from django.urls import reverse

    user = _user("board@example.com")
    banker, prof = _board_fixture(user)
    client.force_login(user)
    resp = client.get("/app/contacts/")
    assert resp.status_code == 200
    # Every count derives from the one filtered list: the professor is out of
    # the board's total, and the product still says where he went.
    assert resp.context["contact_total"] == 1
    assert "hidden from this board" not in resp.content.decode()
    settings_body = client.get(reverse("accounts:settings")).content.decode()
    assert "1 not recruitment" in settings_body
    assert reverse("crm:contact_unrelated") in settings_body
    # The card sections never contain the professor.
    section_names = [
        card["c"].name
        for section in resp.context["sections"]
        for card in section["cards"]
    ]
    assert "Kept Banker" in section_names
    assert "Hidden Professor" not in section_names


def test_ledger_lists_reason_and_bring_back_restores(client):
    user = _user("ledger@example.com")
    banker, prof = _board_fixture(user)
    client.force_login(user)
    resp = client.get("/app/contacts/unrelated/")
    assert resp.status_code == 200
    assert [c.name for c in resp.context["contacts"]] == ["Hidden Professor"]
    # The cited reason quotes the row's own text.
    assert "Professor" in resp.context["contacts"][0].hide_reason
    # One click back: the override wins over the rule permanently.
    resp = client.post(f"/app/contacts/{prof.id}/recruitment-keep/")
    assert resp.status_code == 302
    prof.refresh_from_db()
    assert prof.recruitment_related is True
    resp = client.get("/app/contacts/")
    assert resp.context["contact_total"] == 2
    resp = client.get("/app/contacts/unrelated/")
    assert resp.context["contacts"] == []


# ---------------------------------------------------------------------------
# Capture agrees (capture.discovery).
# ---------------------------------------------------------------------------

def _threaded_finding(email, name):
    return {
        "email": email,
        "name": name,
        "replied": True,
        "threaded_reply": True,
        "subject": "Re: Coffee chat request",
    }


def test_discovery_refuses_disqualified_role_hint_and_keeps_banker():
    # The same rule at the door: a sender whose own signature names an
    # off-track seat never becomes a proposal, while the banker-shaped hint
    # (and the hint that says nothing) still does. Same function as the
    # board's gate, so the two cannot disagree.
    user = _user("cap@example.com")
    refused = discovery.consider_finding(
        user, _threaded_finding("bob@acme-widgets.com", "Bob Smith, Account Manager"),
    )
    assert refused is None
    assert not ContactProposal.all_objects.filter(user=user).exists()
    proposed = discovery.consider_finding(
        user, _threaded_finding("jane@acme-widgets.com", "Jane Doe, IB Analyst"),
    )
    assert proposed == discovery.PROPOSED
    silent = discovery.consider_finding(
        user, _threaded_finding("kim@acme-widgets.com", "Kim Lee"),
    )
    assert silent == discovery.PROPOSED


# ---------------------------------------------------------------------------
# HIDE-WORD COLLISIONS WITH REAL FINANCE TITLES. `residential` entered the
# campus vocabulary to catch USC's housing-office titles and was never checked
# against a finance title. "Analyst, Residential Mortgage-Backed Securities"
# is a real S&T seat and the bare word hid it as campus staff — the same
# ordering discipline already locked in for "Equities Sales", applied to the
# word that had not been tested.


@pytest.mark.parametrize("role", [
    "Analyst, Residential Mortgage-Backed Securities",
    "Associate, Residential Real Estate",
    "VP, Residential Credit",
    "Residential Mortgage Trading Analyst",
])
def test_a_residential_finance_seat_is_not_campus_staff(role):
    verdict = recruitment.classify_person(role=role, notes="")
    assert verdict.code != "campus", (
        f"{role!r} is a track seat; hiding it as campus staff loses a banker"
    )


@pytest.mark.parametrize("role", [
    "Residential Advisor",
    "Resident Assistant",
    "Residential Life Coordinator",
    "Residential College Advisor",
])
def test_campus_housing_staff_are_still_campus(role):
    """The guard: narrowing `residential` must not reopen the hole it closed.
    These are the USC housing titles the word was added for."""
    verdict = recruitment.classify_person(role=role, notes="")
    assert verdict.code == "campus", f"{role!r} is campus housing staff"


# ---------------------------------------------------------------------------
# The classifier is memoised on the role string (2026-09-01)
#
# `_track_signal` asks `directory.recommend.role_function` — a regex sweep —
# and it runs once per contact per render. Measured on the founder's live
# account, 15 ms of every Today and every Network render went to re-answering
# the same question about the same 265 role strings, and `crm.views.
# contact_list` pays it twice on one request (`_build_actions` again).
#
# The cache is only sound because the classifier is PURE on its argument, and
# only bounded because a role is a `CharField(max_length=255)`. Both of those
# are load-bearing and both are pinned below.
# ---------------------------------------------------------------------------

class TestTrackSignalCache:
    def _calls(self, monkeypatch):
        """Every string that reaches `role_function` this test."""
        from directory import recommend

        seen = []
        real = recommend.role_function

        def counting(text):
            seen.append(text)
            return real(text)

        monkeypatch.setattr(recommend, "role_function", counting)
        recruitment._cached_role_function.cache_clear()
        return seen

    def test_one_classification_per_distinct_role(self, monkeypatch):
        seen = self._calls(monkeypatch)
        for _ in range(50):
            recruitment._track_signal("Investment Banking Analyst")
            recruitment._track_signal("Equity Research Associate")
        assert seen == ["Investment Banking Analyst",
                        "Equity Research Associate"]

    def test_the_answer_is_the_same_answer(self, monkeypatch):
        """A cache that changed a verdict would hide a banker or surface a
        professor — the two errors this whole module exists to trade off. Warm
        and cold must agree on every title the rule distinguishes."""
        from directory.recommend import role_function

        titles = [
            "Investment Banking Summer Analyst",
            "Equity Research Associate",
            "Credit Sales",
            "Prime Brokerage Sales",
            "Internal Audit Analyst",
            "Residential Advisor",
            "Trojan Investing Society",
            "",
        ]
        recruitment._cached_role_function.cache_clear()
        cold = [recruitment._track_signal(t) for t in titles]
        warm = [recruitment._track_signal(t) for t in titles]
        assert cold == warm
        for title, answer in zip(titles, cold):
            named = role_function(title) if title else ""
            if named and named != "none":
                assert answer == named, title

    def test_free_prose_is_never_keyed(self, monkeypatch):
        """`contact_verdict` hands this function `notes` and `angle` too, and
        those are TextFields — unbounded in length and never repeated, so
        caching them buys nothing and would park a student's writing in a
        process-local dict. Over the cap the classifier is called straight
        through, which is what it did before the cache existed."""
        seen = self._calls(monkeypatch)
        prose = "met at the info session, " * 40
        assert len(prose) > recruitment._TRACK_SIGNAL_CACHE_MAX_CHARS
        recruitment._track_signal(prose)
        recruitment._track_signal(prose)
        assert seen == [prose, prose]
        assert recruitment._cached_role_function.cache_info().currsize == 0

    def test_the_cache_is_bounded(self):
        """A per-process cache with no ceiling is a leak with a nice name."""
        assert recruitment._cached_role_function.cache_info().maxsize == 4096
