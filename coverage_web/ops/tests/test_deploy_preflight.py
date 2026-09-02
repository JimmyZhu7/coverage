"""`manage.py deploy_preflight` — the six things that broke a first deploy,
as checks.

The load-bearing test in this file is the last one: this command is meant to
be run in a deploy shell and pasted into a chat window, so it must never
print a value. Everything else here pins one check's verdict.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from ops.management.commands import deploy_preflight

pytestmark = pytest.mark.django_db


def _report(**kwargs) -> str:
    out = StringIO()
    call_command("deploy_preflight", "--warn-only", stdout=out, stderr=out, **kwargs)
    return out.getvalue()


def _line_for(report: str, key: str) -> str:
    """The one report line naming `key`."""
    matches = [ln for ln in report.splitlines() if key in ln]
    assert matches, f"no line mentions {key}:\n{report}"
    return matches[0]


# ---------------------------------------------------------------------------
# The rule that matters most
# ---------------------------------------------------------------------------
def test_no_check_ever_prints_a_value(settings, monkeypatch):
    """Names and verdicts only. A preflight that leaks the secret it was
    checking is worse than no preflight."""
    secrets = {
        "DJANGO_SECRET_KEY": "s3cret-key-value-nobody-should-see",
        "GOOGLE_OAUTH_CLIENT_ID": "1234-google-id.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-do-not-print-me",
        "GMAIL_LIVE_CLIENT_ID": "gmail-live-id-do-not-print",
        "GMAIL_LIVE_CLIENT_SECRET": "gmail-live-secret-do-not-print",
        "GMAIL_LIVE_TOKEN_KEY": "fernet-key-do-not-print",
        "STRIPE_SECRET_KEY": "rk_live_do_not_print",
        "STRIPE_WEBHOOK_SECRET": "whsec_do_not_print",
        "SENTRY_DSN": "https://sentry-do-not-print@o1.ingest.sentry.io/1",
        "ANTHROPIC_API_KEY": "sk-ant-do-not-print",
        "REDIS_URL": "redis://user:password-do-not-print@10.0.0.1:6379",
        "VAPID_PUBLIC_KEY": "vapid-public-do-not-print",
        "VAPID_PRIVATE_KEY": "vapid-private-do-not-print",
        "VAPID_CLAIM_EMAIL": "ops-do-not-print@example.org",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
        if hasattr(settings, key):
            setattr(settings, key, value)

    report = _report()

    for key, value in secrets.items():
        assert value not in report, f"{key}'s VALUE was printed"
        assert key in report, f"{key} was not checked at all"


def test_a_placeholder_is_reported_as_one_not_as_configured(settings, monkeypatch):
    """"Not set" and "set to the example value" are different states, and
    the command never quietly treats the second as the first — nor invents a
    plausible value for it."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "changeme")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "changeme")

    line = _line_for(_report(), "GOOGLE_OAUTH_CLIENT_ID")

    assert line.startswith("WARN")
    assert "placeholder" in line
    assert "changeme" not in line


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", False),
        ("sk-ant-api03-real-looking", False),
        ("changeme", True),
        ("CHANGEME-please", True),
        ("your-client-id-here", True),
        ("<paste it here>", True),
        ("insecure-dev-only-secret-key-do-not-deploy", True),
    ],
)
def test_placeholder_detection(value, expected):
    assert deploy_preflight._looks_like_placeholder(value) is expected


# ---------------------------------------------------------------------------
# The first-deploy breakers, in the audit's own order
# ---------------------------------------------------------------------------
def test_empty_allowed_hosts_is_a_FAIL(settings):
    settings.ALLOWED_HOSTS = []

    line = _line_for(_report(), "DJANGO_ALLOWED_HOSTS")

    assert line.startswith("FAIL")


def test_allowed_hosts_names_the_source_it_resolved_from(settings, monkeypatch):
    """The whole point of the platform fallback is that a blank env var is
    no longer a boot failure — so the report has to say which of the two
    answered."""
    monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "coverage-web.onrender.com")
    settings.ALLOWED_HOSTS = ["coverage-web.onrender.com"]

    line = _line_for(_report(), "DJANGO_ALLOWED_HOSTS")

    assert line.startswith("PASS")
    assert "RENDER_EXTERNAL_HOSTNAME" in line


def test_healthz_not_exempt_under_the_ssl_redirect_is_a_FAIL(settings):
    """Render marks the service live on a 200 from /healthz. Behind
    SECURE_SSL_REDIRECT, a probe with no X-Forwarded-Proto header gets a 301
    and the deploy never goes green."""
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_REDIRECT_EXEMPT = []

    line = _line_for(_report(), "SECURE_REDIRECT_EXEMPT")

    assert line.startswith("FAIL")


def test_healthz_exempt_under_the_ssl_redirect_passes(settings):
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

    assert _line_for(_report(), "SECURE_REDIRECT_EXEMPT").startswith("PASS")


def test_the_ssl_redirect_being_off_also_passes(settings):
    """`DJANGO_SECURE_SSL_REDIRECT=False` is the documented escape hatch and
    is safe behind Render's TLS termination — the check is about /healthz
    answering 200, not about the redirect existing."""
    settings.SECURE_SSL_REDIRECT = False

    assert _line_for(_report(), "SECURE_SSL_REDIRECT").startswith("PASS")


def test_applied_migrations_pass_on_the_test_database():
    """The test database is migrated by construction, so this is the PASS
    side of the check; the FAIL side is what a first Blueprint apply hits
    when a `*/5` cron starts before the web service's preDeploy migrate."""
    assert _line_for(_report(), "migrations").startswith("PASS")


def test_a_missing_site_url_warns_about_the_localhost_links(settings):
    settings.SITE_URL = "http://localhost:8000"

    line = _line_for(_report(), "SITE_URL")

    assert line.startswith("WARN")
    assert "local default" in line


def test_a_real_site_url_passes(settings):
    settings.SITE_URL = "https://coverage-web.onrender.com"

    assert _line_for(_report(), "SITE_URL").startswith("PASS")


def test_a_blank_redis_url_warns_that_rate_limits_are_per_worker(settings, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    line = _line_for(_report(), "REDIS_URL")

    assert line.startswith("WARN")
    assert "per-process" in line or "PER gunicorn worker" in line


def test_a_set_redis_url_passes(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://red-abc:6379")

    assert _line_for(_report(), "REDIS_URL").startswith("PASS")


def test_console_email_warns_that_nothing_is_sent(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

    line = _line_for(_report(), "EMAIL_URL")

    assert line.startswith("WARN")


def test_blank_stripe_warns_that_there_is_no_way_to_pay(settings):
    settings.STRIPE_SECRET_KEY = ""
    settings.STRIPE_WEBHOOK_SECRET = ""

    line = _line_for(_report(), "STRIPE_SECRET_KEY")

    assert line.startswith("WARN")
    assert "no way to pay" in line


def test_an_unrestricted_stripe_key_warns(settings):
    """`sk_` works and is a much bigger blast radius than this app needs."""
    settings.STRIPE_SECRET_KEY = "sk_live_x"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_x"

    report = _report()

    assert any(
        ln.startswith("WARN") and "restricted key" in ln
        for ln in report.splitlines()
    ), report


def test_a_restricted_stripe_key_does_not_warn(settings):
    settings.STRIPE_SECRET_KEY = "rk_live_x"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_x"

    report = _report()

    assert "restricted key" not in report


# ---------------------------------------------------------------------------
# Exit behaviour
# ---------------------------------------------------------------------------
def test_a_failing_check_exits_non_zero_by_default(settings):
    """So it can gate a deploy, not just inform one. `CommandError` is
    Django's non-zero exit."""
    settings.ALLOWED_HOSTS = []

    with pytest.raises(CommandError):
        call_command("deploy_preflight", stdout=StringIO(), stderr=StringIO())


def test_warn_only_never_exits_non_zero(settings):
    settings.ALLOWED_HOSTS = []

    call_command("deploy_preflight", "--warn-only", stdout=StringIO(), stderr=StringIO())


def test_warnings_alone_never_fail_the_run(settings):
    """A deploy full of WARNs is a valid deploy — every optional integration
    in this codebase no-ops rather than crashing."""
    settings.STRIPE_SECRET_KEY = ""
    settings.SENTRY_DSN = ""
    settings.SITE_URL = "http://localhost:8000"

    call_command("deploy_preflight", stdout=StringIO(), stderr=StringIO())


def test_the_report_ends_with_a_count(settings):
    report = _report()

    assert "pass ·" in report and "warn ·" in report and "fail" in report
