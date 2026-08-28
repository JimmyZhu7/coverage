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


def test_uses_real_seeded_firms_when_the_directory_is_populated():
    real = [Firm.objects.create(slug=f"firm-{i}", name=f"Firm {i}") for i in range(3)]

    call_command("seed_demo")

    demo = User.objects.get(email="demo@coverage.local")
    linked_firm_ids = set(UserFirm.objects.for_user(demo).values_list("firm_id", flat=True))
    assert linked_firm_ids == {f.id for f in real}
