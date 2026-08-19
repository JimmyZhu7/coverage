"""The onboarding wizard's live preview panel.

The panel's whole claim is that every number on it is real. So the tests
that matter most here are not "does it render" — they are "does the number
it renders equal the number the database gives when you ask by hand", and
"when the answer is nothing, does it say nothing rather than showing the
previous answer's rows".

Fixtures build a small, fully-known directory so every expected count in
this file is derivable by reading the fixture rather than by trusting the
code under test.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import Contact, UserFirm
from directory.models import Firm, Opportunity

User = get_user_model()

PREVIEW = "accounts:onboarding_preview"
WIZARD = "accounts:onboarding"


@pytest.fixture
def user(db):
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="x")


@pytest.fixture
def world(db):
    """A directory small enough to count by hand.

    us/ib   : 3 open campus roles at Goldman (1 says it sponsors, 1 says it
              does not, 1 is silent)
    hk/ib   : 1 open campus role at JPMorgan
    us/cons : 1 open campus role at Bain
    plus one CLOSED us/ib role and one EXPERIENCED us/ib role, both of which
    every count in this module must exclude.
    """
    gs = Firm.objects.create(slug="gs", name="Goldman Sachs",
                             regions=["us"], tracks=["ib"])
    jpm = Firm.objects.create(slug="jpm", name="JPMorgan",
                              regions=["us", "hk"], tracks=["ib"])
    bain = Firm.objects.create(slug="bain", name="Bain",
                               regions=["us"], tracks=["consulting"])

    def opp(firm, title, **kw):
        kw.setdefault("bucket", "internship")
        kw.setdefault("status", "open")
        kw.setdefault("region", "us")
        kw.setdefault("sponsorship", "unknown")
        return Opportunity.objects.create(
            firm=firm, title=title, url=f"https://x.test/{firm.slug}/{title}", **kw)

    opp(gs, "SA Sponsors", sponsorship="yes")
    opp(gs, "SA No Visa", sponsorship="no")
    opp(gs, "SA Silent")
    opp(jpm, "HK Summer", region="hk")
    opp(bain, "Consulting SA")
    # Must never be counted: closed, and out-of-scope bucket.
    opp(gs, "Closed One", status="closed")
    opp(gs, "Experienced One", bucket="other")
    return {"gs": gs, "jpm": jpm, "bain": bain}


def _get(client, step, **params):
    qs = "&".join(
        f"{k}={v}" for k, vals in params.items()
        for v in (vals if isinstance(vals, list) else [vals])
    )
    url = f"{reverse(PREVIEW)}?step={step}"
    return client.get(f"{url}&{qs}" if qs else url)


def _num(body: str) -> int:
    """The panel's one headline number."""
    m = re.search(r'ob-pv-num">(\d+)<', body)
    assert m, "panel rendered no headline number"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# The honesty contract
# ---------------------------------------------------------------------------
def test_profile_count_equals_a_hand_written_query(client, user, world):
    """THE test. The panel's count and a direct ORM count of the same
    question must be the same integer — not close, the same."""
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    client.force_login(user)

    hand = Opportunity.objects.filter(
        status="open", bucket="internship", region="us",
        firm__tracks__overlap=["ib"],
    ).count()
    assert hand == 3  # derivable from the fixture by reading it

    body = _get(client, "profile").content.decode()
    assert _num(body) == hand


def test_profile_count_tracks_the_live_form_not_the_saved_profile(client, user, world):
    """Ticking a chip changes the number before anything is saved."""
    client.force_login(user)
    assert user.regions in (None, [])

    all_open = _num(_get(client, "profile").content.decode())
    assert all_open == 5  # every open campus row in the fixture

    narrowed = _num(_get(client, "profile", live="1", regions="hk").content.decode())
    assert narrowed == 1  # only JPMorgan's HK role

    # And the saved profile is untouched — this endpoint writes nothing.
    user.refresh_from_db()
    assert user.regions in (None, [])


def test_a_selection_that_matches_nothing_says_so(client, user, world):
    """No stale rows, no invented rows, no last-good-answer. The fixture has
    no Japanese roles at all, so this must be the empty state."""
    client.force_login(user)
    body = _get(client, "profile", live="1", regions="jp").content.decode()
    assert _num(body) == 0
    assert "No live roles match yet" in body
    # None of the firms that WOULD have matched a wider selection leak in.
    assert "Goldman" not in body
    assert "JPMorgan" not in body


def test_the_footer_names_only_the_filters_that_actually_ran(client, user, world):
    """A true count under a false caption is still a lie. The footer is built
    from the applied filters, so it can never name one that did not run."""
    client.force_login(user)

    none_yet = _get(client, "profile", live="1").content.decode()
    assert "Every live campus role" in none_yet
    assert "Narrowed from" not in none_yet

    region_only = _get(client, "profile", live="1", regions="us").content.decode()
    assert "Narrowed from 5 live roles by region." in region_only

    both = _get(client, "profile", live="1", regions="us",
                tracks="ib").content.decode()
    assert "Narrowed from 5 live roles by region and track." in both

    all_three = _get(client, "profile", live="1", regions="us", tracks="ib",
                     class_year="2028").content.decode()
    assert "by region, track and class year." in all_three


def test_sample_rows_are_real_postings(client, user, world):
    client.force_login(user)
    body = _get(client, "profile", live="1", regions="hk").content.decode()
    # The one HK row in the fixture, by name, from the database.
    assert "JPMorgan" in body
    assert "HK Summer" in body


def test_closed_and_non_campus_rows_are_never_counted(client, user, world):
    """The panel promises the Opportunities feed's population. A closed
    posting and an experienced-hire posting are in neither."""
    client.force_login(user)
    assert _num(_get(client, "profile", live="1").content.decode()) == 5
    body = _get(client, "profile", live="1").content.decode()
    assert "Closed One" not in body
    assert "Experienced One" not in body


# ---------------------------------------------------------------------------
# Step 2 — sponsorship
# ---------------------------------------------------------------------------
def test_work_step_counts_the_sponsorship_answers_honestly(client, user, world):
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    client.force_login(user)

    body = _get(client, "work_auth", live="1", work_auth_us="sponsorship").content.decode()
    # 3 us/ib roles: one yes, one no, one silent.
    assert _num(body) == 2  # "2 of 3 answer the visa question"
    assert "of 3 answer the visa question" in body
    nums = [int(n) for n in re.findall(r'ob-pv-bar-n">(\d+)<', body)]
    assert nums == [1, 1, 1]  # yes / no / silent
    # The one blocking verdict: a us role saying "no" while the student says
    # they need sponsorship in the us.
    assert "<strong>1</strong> of these are ruled out" in body


def test_work_step_reads_the_saved_profile_not_an_empty_one(client, user, world):
    """REGRESSION. `live=1` used to be read as a global flag, so on this step
    — whose form carries no region or track inputs — the profile filters came
    back empty and the panel counted the whole directory while its footer
    said "the roles matching your profile". Correct arithmetic, false
    sentence. `live` is per-step now."""
    user.regions = ["hk"]
    user.save()
    client.force_login(user)

    body = _get(client, "work_auth", live="1",
                work_auth_hk="sponsorship").content.decode()
    # Only the single HK role, not all five.
    assert "of 1 answer the visa question" in body


def test_work_step_reports_nothing_blocked_rather_than_a_zero(client, user, world):
    """"0 ruled out" reads as a measurement; the sentence is the honest form
    and it is only drawn when the student has actually claimed a region."""
    user.regions = ["hk"]
    user.save()
    client.force_login(user)
    body = _get(client, "work_auth", live="1",
                work_auth_hk="sponsorship").content.decode()
    assert "Nothing is ruled out on visa." in body

    # Nothing claimed at all: no verdict line either way.
    silent = _get(client, "work_auth", live="1").content.decode()
    assert "ruled out" not in silent


# ---------------------------------------------------------------------------
# Step 3 — firms
# ---------------------------------------------------------------------------
def test_firms_step_counts_live_roles_per_picked_firm(client, user, world):
    client.force_login(user)
    body = _get(client, "firms", live="1",
                firms=[str(world["gs"].id), str(world["bain"].id)]).content.decode()
    assert "Goldman Sachs" in body and "Bain" in body
    # gs has 3 open campus roles, bain 1 — highest first.
    assert [int(n) for n in re.findall(r'ob-pv-count[^"]*">(\d+)<', body)] == [3, 1]
    assert _num(body) == 4


def test_firms_step_empty_until_something_is_picked(client, user, world):
    client.force_login(user)
    body = _get(client, "firms", live="1").content.decode()
    assert "No firms picked yet" in body
    assert "Goldman" not in body


def test_a_firm_with_no_live_roles_still_shows_its_real_zero(client, user, world):
    quiet = Firm.objects.create(slug="quiet", name="Quiet Capital", regions=["us"])
    client.force_login(user)
    body = _get(client, "firms", live="1", firms=str(quiet.id)).content.decode()
    assert "Quiet Capital" in body
    assert re.search(r'ob-pv-count is-zero">0<', body)


# ---------------------------------------------------------------------------
# Step 4 — the board
# ---------------------------------------------------------------------------
def test_import_step_shows_the_real_board_state(client, user, world):
    client.force_login(user)
    body = _get(client, "import").content.decode()
    assert _num(body) == 0
    assert "Nothing on it yet" in body

    UserFirm.all_objects.create(user=user, firm=world["gs"], tier=2, status="target")
    Contact.all_objects.create(user=user, name="Jane Banker")
    body = _get(client, "import").content.decode()
    assert _num(body) == 1  # one contact
    assert "1 target firm" in body


# ---------------------------------------------------------------------------
# Tenancy, auth, and the read-only contract
# ---------------------------------------------------------------------------
def test_preview_requires_login(client):
    resp = client.get(f"{reverse(PREVIEW)}?step=profile")
    assert resp.status_code == 302


def test_one_students_board_never_appears_on_anothers(client, user, other_user, world):
    UserFirm.all_objects.create(user=other_user, firm=world["gs"], tier=1,
                                status="target")
    Contact.all_objects.create(user=other_user, name="Someone Else")
    client.force_login(user)
    body = _get(client, "import").content.decode()
    assert _num(body) == 0
    assert "Nothing on it yet" in body

    firms_body = _get(client, "firms").content.decode()
    assert "Goldman" not in firms_body


def test_the_preview_writes_nothing(client, user, world):
    """It is a GET and it must stay one. A student clicking through the
    wizard and abandoning it must leave the same rows behind as one who
    never opened it."""
    client.force_login(user)
    before = (user.regions, user.tracks, user.class_year, user.work_authorization)
    for step in ("profile", "work_auth", "firms", "import"):
        _get(client, step, live="1", regions="us", tracks="ib", class_year="2029")
    assert UserFirm.objects.for_user(user).count() == 0
    user.refresh_from_db()
    assert (user.regions, user.tracks, user.class_year, user.work_authorization) == before

    # And POST is not an entrance.
    assert client.post(reverse(PREVIEW), {"step": "profile"}).status_code == 405


def test_an_unknown_step_falls_back_instead_of_erroring(client, user, world):
    """Same posture as the step machine's own `if step not in
    ONBOARDING_STEPS` — a stale bookmark gets the first panel, not a 500."""
    client.force_login(user)
    resp = _get(client, "assets", live="1")
    assert resp.status_code == 200
    assert "match" in resp.content.decode()


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("step,budget", [
    # Two of every budget below are the session + user lookups every
    # authenticated request pays; the rest is this panel's own work. The
    # ceiling exists so a future edit cannot quietly reintroduce the feed
    # pipeline (or an N+1 over the picked firms) on a control that re-runs
    # every time a chip is toggled.
    ("profile", 5),
    ("work_auth", 3),
    ("firms", 3),
    ("import", 4),
])
def test_the_panel_stays_cheap(client, user, world, django_assert_max_num_queries,
                               step, budget):
    UserFirm.all_objects.create(user=user, firm=world["gs"], tier=2, status="target")
    UserFirm.all_objects.create(user=user, firm=world["jpm"], tier=2, status="target")
    UserFirm.all_objects.create(user=user, firm=world["bain"], tier=2, status="target")
    client.force_login(user)
    with django_assert_max_num_queries(budget):
        _get(client, step, live="1", regions="us", tracks="ib")
