"""send_weekly_digest: the management command wrapping crm.digest — who it
mails, who it skips, and that --dry-run truly sends nothing.

Uses pytest-django's `mailoutbox` fixture, which forces the locmem email
backend for the test regardless of what EMAIL_URL resolves to in whatever
settings module the suite runs under — the standard Django-testing pattern
the task brief calls for, independent of the console-vs-SMTP backend choice
made at deploy time.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from analytics.models import UserOpportunity
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
TODAY = timezone.localdate()


def _user(email, *, onboarded=True, deleted=False, **kw):
    user = User.objects.create_user(email=email, password="pw12345!", **kw)
    if onboarded:
        user.onboarded_at = timezone.now()
    if deleted:
        user.deleted_at = timezone.now()
    user.save()
    return user


def _closing_opp(user, n=1, *, days=2):
    firm = Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    o = Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
        bucket="internship", status="open", deadline=TODAY + timedelta(days=days),
    )
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")
    return o


def _run(*args):
    out = io.StringIO()
    call_command("send_weekly_digest", *args, stdout=out)
    return out.getvalue()


def test_dry_run_sends_nothing(mailoutbox):
    user = _user("dry@example.com")
    _closing_opp(user)

    out = _run("--dry-run")

    assert "dry@example.com" in out
    assert mailoutbox == []


def test_an_onboarded_user_with_something_due_gets_mailed(mailoutbox):
    user = _user("real@example.com")
    _closing_opp(user)

    _run()

    assert len(mailoutbox) == 1
    msg = mailoutbox[0]
    assert msg.to == ["real@example.com"]
    assert "closing" in msg.subject.lower()
    # Both parts of the pair the task brief asks for: plain-text body plus an
    # HTML alternative.
    assert msg.body.strip()
    alt_types = [mimetype for _, mimetype in msg.alternatives]
    assert "text/html" in alt_types


def test_the_plain_text_body_never_html_escapes_an_ampersand(mailoutbox):
    """Regression: Django's template engine autoescapes by default
    REGARDLESS of file extension, so a firm/role name or a mailto: URL's
    '&'-joined query params rendered as literal '&amp;' in the plain-text
    part the first time this was wired up (caught by hand, rendering a real
    dev-DB digest, then pinned here). weekly_digest.txt wraps its body in
    {% autoescape off %} specifically to keep this from regressing."""
    user = _user("amp@example.com")
    firm = Firm.objects.create(name="Smith & Co", slug="smith-and-co")
    o = Opportunity.objects.create(
        firm=firm, url="https://x/amp?a=1&b=2", title="Analyst & Associate",
        bucket="internship", status="open", deadline=TODAY + timedelta(days=2),
    )
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    _run()

    assert "&amp;" not in mailoutbox[0].body
    assert "Smith & Co" in mailoutbox[0].body
    assert "https://x/amp?a=1&b=2" in mailoutbox[0].body


def test_a_user_with_nothing_due_is_skipped_not_mailed(mailoutbox):
    _user("quiet@example.com")  # onboarded, nothing tracked, nothing due

    out = _run()

    assert mailoutbox == []
    assert "quiet@example.com" in out
    assert "skipped" in out.lower()


def test_a_not_yet_onboarded_user_is_never_mailed_by_default(mailoutbox):
    user = _user("new@example.com", onboarded=False)
    _closing_opp(user)

    _run()

    assert mailoutbox == []


def test_a_deleted_user_is_never_mailed(mailoutbox):
    user = _user("gone@example.com", deleted=True)
    _closing_opp(user)

    _run()

    assert mailoutbox == []


def test_the_user_flag_targets_one_account_bypassing_onboarding(mailoutbox):
    """--user is explicitly for testing a specific account; it must reach
    someone who hasn't finished onboarding yet."""
    user = _user("preview@example.com", onboarded=False)
    _closing_opp(user)

    _run("--user", "preview@example.com")

    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["preview@example.com"]


def test_an_unknown_user_flag_raises_rather_than_silently_sending_nothing():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        _run("--user", "nobody@example.com")


def test_the_user_flag_matches_email_case_insensitively(mailoutbox):
    user = _user("Mixed.Case@example.com", onboarded=False)
    _closing_opp(user)

    _run("--user", "mixed.case@example.com")

    assert len(mailoutbox) == 1


def test_two_users_one_quiet_one_due_only_the_due_one_is_mailed(mailoutbox):
    loud = _user("loud@example.com")
    _closing_opp(loud, n=1)
    _user("quiet2@example.com")

    _run()

    assert [m.to[0] for m in mailoutbox] == ["loud@example.com"]
