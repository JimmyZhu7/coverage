"""seed_demo — the promoted management-command version of what used to be
scripts/demo_seed.py, run via `manage.py shell < scripts/demo_seed.py`. The
whole point of promoting it is to make the demo account the path of least
resistance for manual verification, so these tests check both that it
actually builds a populated account and that it is safe to run twice."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from crm.models import Contact, Touch, UserFirm
from directory.models import Firm

User = get_user_model()


pytestmark = pytest.mark.django_db(transaction=True)  # seed_demo calls
# crm.services.log_touch, which opens its own real psycopg connection
# (see crm/services.py's docstring) — a plain django_db test's uncommitted
# transaction is invisible to it, so this needs the commit-then-truncate
# variant, matching crm/tests/test_services.py's own convention.


def test_creates_a_populated_demo_account():
    call_command("seed_demo")

    demo = User.objects.get(email="demo@coverage.local")
    assert demo.check_password("demo1234")
    assert Contact.objects.for_user(demo).count() == 6
    assert Touch.objects.for_user(demo).count() > 0
    assert UserFirm.objects.for_user(demo).count() == 3


def test_running_it_twice_is_a_noop_the_second_time(capsys):
    call_command("seed_demo")
    demo = User.objects.get(email="demo@coverage.local")
    contacts_after_first_run = Contact.objects.for_user(demo).count()

    call_command("seed_demo")
    out = capsys.readouterr().out

    assert "already set up" in out
    assert Contact.objects.for_user(demo).count() == contacts_after_first_run
    assert User.objects.filter(email="demo@coverage.local").count() == 1


def test_falls_back_to_creating_its_own_firms_when_directory_is_empty():
    assert Firm.objects.count() == 0

    call_command("seed_demo")

    assert Firm.objects.filter(slug__startswith="demo-firm-").count() == 3


def test_the_demo_cycle_is_one_the_recommender_can_actually_parse():
    """The measured defect (WS-AI-13). This command wrote the literal
    `"sa2028_ib"`, which `directory.recommend.parse_target_cycle` has never
    recognised — the vocabulary is `cycle_choices()`' own
    `"2028 Summer Internship"`. An unparsed cycle costs the student nothing
    by design (that function returns None rather than guessing), so the
    15-point cycle bonus and the level gate were simply OFF on the account
    every demo the founder gives runs on, silently. Measured 2026-09-01: 0
    of the demo's picks carried a `W_CYCLE` reason.

    Asserted through `parse_target_cycle` rather than against a string, so
    this fails the day the vocabulary moves and not a release later.
    """
    from directory.recommend import cycle_choices, parse_target_cycle

    call_command("seed_demo")
    demo = User.objects.get(email="demo@coverage.local")

    assert demo.target_cycles
    assert parse_target_cycle(demo.target_cycles[0]) is not None
    assert demo.target_cycles[0] in {value for value, _ in cycle_choices()}


def test_the_demo_class_year_follows_from_its_cycle():
    """A summer analyst intake IS the penultimate summer: a student
    graduating in 2028 does the 2027 internship. The class year and the
    seeded firm date are both derived from the cycle, so the account cannot
    drift into recruiting for one intake while graduating in a year that
    does not follow from it."""
    from directory.models import FirmDate

    call_command("seed_demo")
    demo = User.objects.get(email="demo@coverage.local")
    year = int(demo.target_cycles[0].split()[0])

    assert demo.class_year == year + 1
    assert FirmDate.objects.filter(source_url="seed:demo",
                                   cycle=f"sa{year}").exists()


def test_the_demo_targets_the_intake_that_is_actually_in_market():
    """Next year's summer, not the furthest the dropdown offers. Measured on
    the live board 2026-09-02: 1,103 open campus rows carry a 2027 cohort
    against 4 carrying 2028. A demo pointed two summers out has a cycle bonus
    that fires on nothing, which is the same silent zero the old unparseable
    literal produced by a better-looking route."""
    from datetime import date

    from accounts.management.commands.seed_demo import demo_cycle

    assert demo_cycle(date(2026, 9, 2)) == "2027 Summer Internship"
    assert demo_cycle(date(2030, 1, 15)) == "2031 Summer Internship"


def test_the_demo_work_authorization_answers_the_market_it_targets():
    """The live demo row carried `{"hk": "citizen"}` against
    `regions=["us"]` — an answer about a market this student does not
    recruit in, which leaves `directory.views._eligibility` with nothing to
    say about the only market they do."""
    call_command("seed_demo")
    demo = User.objects.get(email="demo@coverage.local")

    assert set(demo.work_authorization) <= set(demo.regions)
    assert demo.work_authorization


def test_uses_real_seeded_firms_when_the_directory_is_populated():
    real = [Firm.objects.create(slug=f"firm-{i}", name=f"Firm {i}") for i in range(3)]

    call_command("seed_demo")

    demo = User.objects.get(email="demo@coverage.local")
    linked_firm_ids = set(UserFirm.objects.for_user(demo).values_list("firm_id", flat=True))
    assert linked_firm_ids == {f.id for f in real}
