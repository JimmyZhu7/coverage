"""Smoke coverage for the modules and commands nothing imported.

`audit-perf-tests.md §4` listed what no test touches at all: `analytics/views.py`
(234 lines, the founder dashboard, whose `instrument` URL is never requested),
`analytics/models.py` (107), `accounts/adapter.py` (34),
`core/context_processors.py` (30), `accounts/signals.py` (21), seven management
commands never named in a test, and ten route names never `reverse()`d.

WHAT A SMOKE TEST IS FOR HERE, and what it is not. None of these assertions
pins interesting behaviour, and they are not meant to: the class of bug they
catch is the one that only appears when the module is loaded at all. A staff
dashboard that raises on an empty database, a management command whose
`add_arguments` references a flag `handle` no longer reads, a context processor
that breaks on an anonymous request, a URL pattern whose view was renamed — all
of these are silent until somebody visits, and the audit's point is that on
these modules nobody ever did.

THE STAFF GATE on `/instrument/` is the exception and is asserted properly, for
both the anonymous and the signed-in-non-staff case: it reads every tenant's
contact and touch counts, so "does the gate hold" is a real question rather
than a smoke one.

`assistant/client.py` is on the audit's list and is NOT covered here: it is
owned by another workstream tonight and this file must not touch it.
"""

from __future__ import annotations

import contextlib
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse

User = get_user_model()

INSTRUMENT = "instrument"


# ---------------------------------------------------------------------------
# analytics: the founder dashboard
# ---------------------------------------------------------------------------

@pytest.fixture
def staff(db):
    return User.objects.create_user(
        email="staff@example.test", password="x", is_staff=True)


@pytest.fixture
def student(db):
    return User.objects.create_user(email="student@example.test", password="x")


def test_the_dashboard_renders_for_staff_on_an_empty_database(client, staff):
    """The empty case, because it is the one a dashboard fails on: every
    figure it prints is an aggregate, and an aggregate over nothing is where a
    `max()` with no default or a division by a zero busiest-day raises."""
    client.force_login(staff)
    resp = client.get(reverse(INSTRUMENT))
    assert resp.status_code == 200


def test_the_dashboard_renders_for_staff_with_data(client, staff, student):
    from analytics.models import ProductEvent

    ProductEvent.all_objects.create(user=student, event="signup")
    ProductEvent.all_objects.create(user=student, event="onboarding_step")
    ProductEvent.all_objects.create(user=None, event="landing")
    client.force_login(staff)
    assert client.get(reverse(INSTRUMENT)).status_code == 200


def test_the_dashboard_drilldown_renders(client, staff, student):
    """`?user=` is a second code path (`_pilot_drilldown`), and the only one
    that takes a value off the query string."""
    from analytics.models import ProductEvent

    ProductEvent.all_objects.create(user=student, event="signup")
    client.force_login(staff)
    assert client.get(
        f"{reverse(INSTRUMENT)}?user={student.pk}").status_code == 200


def test_a_garbage_user_parameter_does_not_raise(client, staff):
    client.force_login(staff)
    assert client.get(
        f"{reverse(INSTRUMENT)}?user=not-a-number").status_code == 200


def test_the_dashboard_refuses_an_anonymous_visitor(client):
    resp = client.get(reverse(INSTRUMENT))
    assert resp.status_code in (302, 403), resp.status_code


def test_the_dashboard_refuses_a_signed_in_student(client, student):
    """THE assertion in this file. `_pilot_rows` reads `ProductEvent`,
    `Contact` and `Touch` through `all_objects` — every tenant's — so this
    gate is the only thing between a signed-in student and a cross-account
    read."""
    client.force_login(student)
    assert student.is_staff is False
    resp = client.get(reverse(INSTRUMENT))
    assert resp.status_code in (302, 403), (
        "a non-staff student reached the founder dashboard, which reads every "
        "tenant's contacts and touches"
    )


def test_product_event_records_and_reads_back(db, student):
    """`analytics/models.py` had no test importing it at all."""
    from analytics.models import ProductEvent

    row = ProductEvent.all_objects.create(
        user=student, event="signup", props={"step": "profile"})
    assert str(row)
    assert ProductEvent.objects.for_user(student).count() == 1
    assert ProductEvent.objects.for_user(student).first().props["step"] == "profile"


# ---------------------------------------------------------------------------
# The small modules nothing imported
# ---------------------------------------------------------------------------

def test_the_social_provider_context_processor_answers_for_anonymous(settings):
    """`core/context_processors.py` runs on EVERY page render including the
    signed-out ones, and had no test."""
    from core.context_processors import social_providers

    settings.ENABLED_SOCIAL_PROVIDERS = ["google"]
    out = social_providers(RequestFactory().get("/"))
    by_id = {p["id"]: p for p in out["social_providers"]}
    assert by_id["google"]["configured"] is True
    assert by_id["apple"]["configured"] is False
    assert by_id["google"]["label"] == "Google"


def test_the_context_processor_survives_the_setting_being_absent(settings):
    del settings.ENABLED_SOCIAL_PROVIDERS
    from core.context_processors import social_providers

    out = social_providers(RequestFactory().get("/"))
    assert all(p["configured"] is False for p in out["social_providers"])


def test_the_allauth_adapter_imports_and_answers(db, settings):
    """`accounts/adapter.py` is instantiated by allauth on every auth request
    and nothing imported it."""
    import importlib

    adapter_module = importlib.import_module("accounts.adapter")
    classes = [obj for name, obj in vars(adapter_module).items()
               if isinstance(obj, type) and obj.__module__ == "accounts.adapter"]
    assert classes, "accounts/adapter.py defines no adapter class"
    for cls in classes:
        assert cls()  # constructing it is the smoke test: it has no state


def test_the_accounts_signals_module_is_connected(db):
    """`accounts/signals.py` is 21 lines of receivers. Importing it is what
    registers them, so an import error here is a receiver that silently never
    fires."""
    import importlib

    module = importlib.import_module("accounts.signals")
    receivers = [name for name in vars(module) if not name.startswith("_")]
    assert receivers


# ---------------------------------------------------------------------------
# The seven commands never named in a test
# ---------------------------------------------------------------------------

_COMMANDS = [
    "generate_vapid_keys",
    "backup_db",
    "audit_close_trust",
    "audit_firm_logos",
    "backfill_sponsorship",
    "dedupe_opportunities",
    "seed_logo_domains",
]


@pytest.mark.parametrize("name", _COMMANDS)
def test_the_command_has_a_working_argument_parser(name):
    """`--help` builds the parser and imports the module. That is enough to
    catch the two failures these commands actually have: an import that broke
    when something was renamed, and an `add_arguments` that no longer agrees
    with `handle`."""
    # `--help` is argparse's, so it prints to the real sys.stdout and exits;
    # the `stdout=` kwarg Django threads through to `self.stdout` never sees
    # it. Redirecting is the only way to read it.
    out = StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_code:
        call_command(name, "--help")
    assert exit_code.value.code == 0
    assert out.getvalue().strip()


# Commands that read and report but never write, plus the flag that keeps them
# that way. `dedupe_opportunities` takes `--apply` and refuses it by design
# ("merging rows is a decision, not a chore"), which is worth pinning too.
_REPORT_ONLY = [
    ("audit_close_trust", []),
    ("audit_firm_logos", []),
    ("backfill_sponsorship", []),
    ("dedupe_opportunities", ["--dry-run"]),
    ("seed_logo_domains", ["--dry-run"]),
]


@pytest.mark.parametrize("name,args", _REPORT_ONLY,
                         ids=[n for n, _ in _REPORT_ONLY])
def test_the_report_only_command_writes_nothing_on_an_empty_database(db, name, args):
    """Against an empty database, which is the case none of these were ever
    run under: a report that assumes at least one row is a report that raises
    on a fresh deploy."""
    from directory.models import Firm, Opportunity

    before = (Firm.objects.count(), Opportunity.objects.count())
    call_command(name, *args, stdout=StringIO(), stderr=StringIO())
    assert (Firm.objects.count(), Opportunity.objects.count()) == before


def test_dedupe_refuses_to_apply(db):
    """`--apply` is declared and deliberately not implemented. A flag that
    parses and then silently does nothing is worse than no flag; this pins
    that it says so."""
    from django.core.management.base import CommandError

    out, err = StringIO(), StringIO()
    said = ""
    try:
        call_command("dedupe_opportunities", "--apply", stdout=out, stderr=err)
    except (CommandError, SystemExit) as exc:
        said = str(exc)
    said = (said + out.getvalue() + err.getvalue()).lower()
    assert "not implemented" in said or "decision" in said, said


def test_generate_vapid_keys_prints_a_pair_and_writes_nothing(db, settings, tmp_path):
    """It prints a private key to stdout and touches no file. Both halves
    matter: the pair has to be usable, and a command that quietly wrote a
    secret to disk would be a surprise nobody had asked for."""
    before = set(tmp_path.iterdir())
    out = StringIO()
    call_command("generate_vapid_keys", stdout=out)
    printed = out.getvalue()
    assert "VAPID_PUBLIC_KEY" in printed
    assert "VAPID_PRIVATE_KEY" in printed
    assert set(tmp_path.iterdir()) == before


def test_backup_db_help_names_its_destination_and_retention(db):
    """`backup_db` is the one command here that would write outside the
    database, so it is not run — only its parser is, and only to confirm the
    two flags a cron would pass still exist."""
    out = StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        call_command("backup_db", "--help")
    text = out.getvalue()
    assert "--dest" in text
    assert "--keep" in text


# ---------------------------------------------------------------------------
# The routes nothing reversed
# ---------------------------------------------------------------------------

# (name, args). Measured 2026-09-02: nine of this project's 106 named routes
# were never named in any test, so a rename would have been caught by nothing.
# `reverse()` is the whole assertion — it fails when the pattern is gone, the
# view is renamed, or the argument count changes.
_UNREVERSED = [
    ("crm:contact_opener", [1]),
    ("crm:contact_unrelated_keep", [1]),
    ("favicon", []),
    ("core:home", []),
    ("instrument", []),
    ("crm:mail_fact_act", [1, 1]),
    ("crm:play_dismiss", []),
    ("crm:remove_target_firm", []),
    ("accounts:university_search", []),
]


@pytest.mark.parametrize("name,args", _UNREVERSED, ids=[n for n, _ in _UNREVERSED])
def test_the_route_still_reverses(name, args):
    url = reverse(name, args=args)
    assert url.startswith("/")
