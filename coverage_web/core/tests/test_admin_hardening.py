"""The staff door: locked, and not where everyone looks for it.

`/admin/login/` was the one password form in this app that nothing counted.
allauth's rate limits cover `/accounts/login/` and stop there; Django's admin
runs its own `AuthenticationForm` on its own view and never touches them. It
is also the one login whose session reads EVERY tenant, so an unthrottled
form there is worth more to an attacker than every other surface combined.

Two changes, tested here: django-axes locks a (username, IP) pair after five
failures for an hour, and the admin is mounted at `settings.ADMIN_URL_PREFIX`
rather than a hard-coded "admin/". The second is not a substitute for the
first. It is the cheap half: the scanners that walk /admin/ on every host on
the internet stop finding a form to POST to, so the lockout only has to deal
with someone who already knows where to look.
"""

from __future__ import annotations

import importlib
import subprocess

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import clear_url_caches, reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

GOOD = "a-real-staff-password-9271"
BAD = "not-the-password"


@pytest.fixture
def staff(db):
    return User.objects.create_superuser(email="staff@coverage.local", password=GOOD)


@pytest.fixture(autouse=True)
def _clean_axes_state():
    """Axes' cache handler keeps counters outside the database, so a rolled
    back test would otherwise leave them behind for the next one. Harmless
    under the database handler this suite runs with; kept so the tests still
    mean what they say if REDIS_URL is ever set in a test environment."""
    from axes.handlers.proxy import AxesProxyHandler

    AxesProxyHandler.reset_attempts()
    yield
    AxesProxyHandler.reset_attempts()


def _login(client, password, email="staff@coverage.local"):
    return client.post(
        reverse("admin:login"),
        {"username": email, "password": password, "next": reverse("admin:index")},
    )


# ---------------------------------------------------------------------------
# The lockout.
# ---------------------------------------------------------------------------
def test_the_admin_login_form_exists_and_takes_a_password(client, staff):
    """Baseline: the door opens for the right password, so a refusal below
    is the lockout and not a broken fixture."""
    resp = _login(client, GOOD)
    assert resp.status_code == 302
    assert resp["Location"] == reverse("admin:index")


def test_six_bad_admin_logins_lock_the_account_out(client, staff):
    """Five is the configured limit, and axes locks ON reaching it: the
    fifth failure already answers 429 instead of re-rendering the form, and
    the sixth never reaches the password check at all."""
    for attempt in range(settings.AXES_FAILURE_LIMIT - 1):
        resp = _login(client, BAD)
        assert resp.status_code == 200, f"attempt {attempt + 1} should re-render"

    assert _login(client, BAD).status_code == 429
    assert _login(client, BAD).status_code == 429


def test_the_lockout_refuses_the_CORRECT_password_too(client, staff):
    """The point of a lockout is that guessing right on attempt 400 is
    still no good. A limiter that only rejected wrong passwords would
    count attempts and prevent nothing."""
    for _ in range(settings.AXES_FAILURE_LIMIT):
        _login(client, BAD)

    resp = _login(client, GOOD)
    assert resp.status_code == 429


def test_a_success_clears_the_counter(client, staff):
    """AXES_RESET_ON_SUCCESS. Without it a staff member who fumbles four
    times today and twice next week is locked out by attempts a week
    apart."""
    for _ in range(settings.AXES_FAILURE_LIMIT - 1):
        _login(client, BAD)
    assert _login(client, GOOD).status_code == 302

    client.logout()
    for attempt in range(settings.AXES_FAILURE_LIMIT - 1):
        resp = _login(client, BAD)
        assert resp.status_code == 200, (
            f"attempt {attempt + 1} after a success should start from zero"
        )


def test_locking_one_username_does_not_lock_another_from_the_same_ip(client, staff):
    """AXES_LOCKOUT_PARAMETERS pins the COMBINATION, deliberately. Locking
    on username alone would let anyone who knows a staff address lock the
    founder out of his own admin from anywhere; locking on IP alone would
    let one guesser on a shared campus NAT lock out everybody behind it."""
    other = User.objects.create_superuser(email="second@coverage.local", password=GOOD)

    for _ in range(settings.AXES_FAILURE_LIMIT + 1):
        _login(client, BAD)
    assert _login(client, BAD).status_code == 429

    resp = _login(client, GOOD, email=other.email)
    assert resp.status_code == 302


def test_the_ordinary_sign_in_page_is_left_to_allauth(client, staff):
    """allauth already counts its own form, and axes reads the identifier
    from a field allauth does not post ("username"; allauth's is "login"),
    so counting there would record `username=None` and silently degrade the
    lockout to IP-only — five bad sign-ins from one shared campus NAT
    taking out everyone behind it.

    Asserted on the ROWS axes writes rather than on the response status: the
    status is allauth's own limiter's business, and a test that read it
    would pass on a build where axes had quietly taken the form over.
    `AXES_ONLY_ADMIN_SITE` alone does not achieve this — it is checked in
    `is_allowed` and not in `user_login_failed` — which is why
    core/axes_scope.py exists.
    """
    from axes.models import AccessAttempt

    for _ in range(settings.AXES_FAILURE_LIMIT + 3):
        client.post("/accounts/login/", {"login": staff.email, "password": BAD})

    assert not AccessAttempt.objects.exists(), (
        "axes recorded an attempt for a form allauth already limits"
    )


# ---------------------------------------------------------------------------
# Where the door is.
# ---------------------------------------------------------------------------
def test_the_default_prefix_is_unchanged(client):
    """Local development, every bookmark, and every existing test that
    reverses an admin URL must behave exactly as before."""
    assert settings.ADMIN_URL_PREFIX == "admin/"
    assert reverse("admin:index") == "/admin/"


@pytest.mark.parametrize("raw, expected", [
    ("admin/", "admin/"),
    ("admin", "admin/"),
    ("/admin/", "admin/"),
    ("  back-office-4f2a  ", "back-office-4f2a/"),
    ("/deep/path", "deep/path/"),
])
def test_the_prefix_is_normalised_rather_than_trusted(monkeypatch, raw, expected):
    """A leading slash makes Django's own `path()` raise; a missing trailing
    slash silently mounts the whole admin one segment shallower than
    intended. Neither is a mistake worth making at 2am on a deploy."""
    monkeypatch.setenv("DJANGO_ADMIN_URL_PREFIX", raw)
    module = importlib.reload(importlib.import_module("coverage_web.settings.base"))
    assert module.ADMIN_URL_PREFIX == expected


@pytest.fixture
def moved_admin():
    """Mount the admin at an unguessable prefix for one test.

    `override_settings` alone is not enough: coverage_web/urls.py reads
    ADMIN_URL_PREFIX once, at import, so the module has to be re-executed
    and the resolver cache dropped on the way in AND on the way out. The
    restore runs after the override is lifted, or the reload would rebuild
    the moved urlconf and leak it into every later test.
    """
    prefix = "back-office-4f2a/"
    urls = importlib.import_module("coverage_web.urls")
    with override_settings(ADMIN_URL_PREFIX=prefix):
        importlib.reload(urls)
        clear_url_caches()
        try:
            yield f"/{prefix}"
        finally:
            pass
    importlib.reload(urls)
    clear_url_caches()


def test_the_admin_moves_when_the_prefix_does(client, staff, moved_admin):
    """The whole point: production sets an unguessable prefix and /admin/
    stops answering."""
    assert reverse("admin:index") == moved_admin
    assert client.get("/admin/login/").status_code == 404
    assert client.get(f"{moved_admin}login/").status_code == 200


def test_the_moved_prefix_is_restored_for_everyone_else(client):
    """The fixture above rewrites a module-level urlconf, which is exactly
    the kind of test-only surgery that leaks. Pinned so a later failure
    reads as "the fixture leaked" rather than as a mystery."""
    assert reverse("admin:index") == "/admin/"


def test_the_lockout_follows_the_admin_to_its_new_prefix(client, staff, moved_admin):
    """django-axes finds the admin by reversing `admin:index`, not by
    matching the literal string "/admin/", so moving the door does not
    move the lock off it. Worth pinning: the two settings are independent
    and it would be easy to ship the move and lose the protection."""
    login_url = f"{moved_admin}login/"
    body = {"username": staff.email, "password": BAD, "next": moved_admin}
    for _ in range(settings.AXES_FAILURE_LIMIT - 1):
        assert client.post(login_url, body).status_code == 200
    assert client.post(login_url, body).status_code == 429


# ---------------------------------------------------------------------------
# The operational file that should never have been in a public repo.
# ---------------------------------------------------------------------------
def test_no_backfill_undo_file_is_tracked():
    """`coverage_web/region_backfill_undo_20260826T062308.json` was tracked
    in a PUBLIC repo until 2026-09-01. Those files are what a backfill
    command writes so it can be reversed: a row-by-row map of "this id held
    this value before", which for the region backfill meant an account
    email and a few hundred private-zone contact ids. They are operational
    state, not source. Keep writing them; keep them local.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--", "coverage_web/*_undo_*.json"],
        cwd=settings.REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    assert listed.stdout.strip() == "", (
        "an undo file is tracked again; it is ignored by .gitignore but a "
        "`git add -f` or a rule change can put one back"
    )
