"""send_deadline_push_alerts: who gets a Web Push alert, for which role, and
that a dead subscription (404/410) is deleted rather than retried forever.

`accounts.push.webpush` (the real `pywebpush.webpush`, imported into that
module's namespace) is mocked throughout — this suite never hits a real
push service, per the task brief. VAPID keys are set via `override_settings`
so `accounts.push.is_configured()` is True; a separate test confirms the
command no-ops cleanly with them unset (the default), matching every other
optional integration in this codebase (see directory/ai_extract.py).
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from pywebpush import WebPushException

from accounts.models import PushSubscription
from analytics.models import UserOpportunity
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
TODAY = timezone.localdate()

VAPID_SETTINGS = dict(
    VAPID_PUBLIC_KEY="test-public-key",
    VAPID_PRIVATE_KEY="test-private-key",
    VAPID_CLAIM_EMAIL="ops@example.com",
)


def _user(email, **kw):
    return User.objects.create_user(email=email, password="pw12345!", **kw)


def _sub(user, endpoint="https://push.example.com/1", **kw):
    return PushSubscription.all_objects.create(
        user=user, endpoint=endpoint, p256dh="p256dh-key", auth="auth-key", **kw
    )


def _tracked(user, *, n=1, days, applied_status="saved", status="open"):
    """`status` is the POSTING's (`Opportunity.status`, written by the nightly
    reverify pass); `applied_status` is the STUDENT's own funnel stage. Two
    unrelated facts that both spell "closed" — see directory/views.py's
    TRACK_CLOSED comment."""
    firm = Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    o = Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
        bucket="internship", status=status, deadline=TODAY + timedelta(days=days),
    )
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status=applied_status)
    return o


def _run(*args):
    out = io.StringIO()
    call_command("send_deadline_push_alerts", *args, stdout=out)
    return out.getvalue()


def test_unconfigured_vapid_keys_no_op_cleanly():
    """The default posture (blank keys, settings/base.py) — the command must
    not crash, and must not attempt any send."""
    user = _user("nokeys@example.com")
    _sub(user)
    _tracked(user, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run()

    mock_webpush.assert_not_called()
    assert "VAPID_PUBLIC_KEY" in out or "unset" in out.lower()


@override_settings(**VAPID_SETTINGS)
def test_a_role_exactly_seven_days_out_gets_a_push():
    user = _user("t7@example.com")
    _sub(user)
    _tracked(user, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_called_once()
    _, kwargs = mock_webpush.call_args
    assert kwargs["subscription_info"]["endpoint"] == "https://push.example.com/1"
    # Student-facing copy ("in 7 days"), not the command's own T-7/T-2 jargon.
    assert "Firm 1" in kwargs["data"]
    assert "in 7 days" in kwargs["data"]


@override_settings(**VAPID_SETTINGS)
def test_a_role_exactly_two_days_out_also_gets_a_push():
    user = _user("t2@example.com")
    _sub(user)
    _tracked(user, days=2)

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_called_once()


@override_settings(**VAPID_SETTINGS)
def test_a_role_outside_the_t7_t2_windows_gets_nothing():
    user = _user("quiet@example.com")
    _sub(user)
    _tracked(user, days=5)  # inside the app's wider "closing soon" window, but not T-7/T-2

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run()

    mock_webpush.assert_not_called()
    assert "quiet@example.com" in out
    assert "skipped" in out.lower()


@override_settings(**VAPID_SETTINGS)
def test_a_done_application_is_never_alerted():
    """A finished application has no deadline urgency left — same rule
    crm.digest's _closing_this_week enforces for the email digest."""
    user = _user("done@example.com")
    _sub(user)
    _tracked(user, days=7, applied_status="closed")

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_not_called()


@override_settings(**VAPID_SETTINGS)
def test_a_user_with_no_subscription_is_never_queried():
    user = _user("nosub@example.com")
    _tracked(user, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run()

    mock_webpush.assert_not_called()
    assert "nosub@example.com" not in out


@override_settings(**VAPID_SETTINGS)
def test_multiple_subscriptions_each_get_their_own_push():
    user = _user("twodevices@example.com")
    _sub(user, endpoint="https://push.example.com/laptop")
    _sub(user, endpoint="https://push.example.com/phone")
    _tracked(user, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    assert mock_webpush.call_count == 2
    endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in mock_webpush.call_args_list}
    assert endpoints == {"https://push.example.com/laptop", "https://push.example.com/phone"}


@override_settings(**VAPID_SETTINGS)
def test_dry_run_reports_but_sends_nothing():
    user = _user("dry@example.com")
    _sub(user)
    _tracked(user, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run("--dry-run")

    mock_webpush.assert_not_called()
    assert "dry@example.com" in out


@override_settings(**VAPID_SETTINGS)
def test_a_404_response_deletes_the_stale_subscription():
    user = _user("gone@example.com")
    sub = _sub(user)
    _tracked(user, days=7)

    response = MagicMock(status_code=404)
    with patch("accounts.push.webpush", side_effect=WebPushException("gone", response=response)):
        out = _run()

    assert not PushSubscription.all_objects.filter(pk=sub.pk).exists()
    assert "expired" in out.lower()


@override_settings(**VAPID_SETTINGS)
def test_a_410_response_also_deletes_the_stale_subscription():
    user = _user("gone410@example.com")
    sub = _sub(user)
    _tracked(user, days=2)

    response = MagicMock(status_code=410)
    with patch("accounts.push.webpush", side_effect=WebPushException("gone", response=response)):
        _run()

    assert not PushSubscription.all_objects.filter(pk=sub.pk).exists()


@override_settings(**VAPID_SETTINGS)
def test_a_transient_failure_does_not_delete_the_subscription():
    user = _user("transient@example.com")
    sub = _sub(user)
    _tracked(user, days=7)

    response = MagicMock(status_code=500)
    with patch("accounts.push.webpush", side_effect=WebPushException("server error", response=response)):
        _run()

    assert PushSubscription.all_objects.filter(pk=sub.pk).exists()


@override_settings(**VAPID_SETTINGS)
def test_the_user_flag_targets_one_account():
    loud = _user("loud@example.com")
    _sub(loud, endpoint="https://push.example.com/loud")
    _tracked(loud, n=1, days=7)
    quiet = _user("quiet2@example.com")
    _sub(quiet, endpoint="https://push.example.com/quiet")
    _tracked(quiet, n=2, days=7)

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run("--user", "loud@example.com")

    mock_webpush.assert_called_once()
    assert "quiet2@example.com" not in out


@override_settings(**VAPID_SETTINGS)
def test_an_unknown_user_flag_raises():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        _run("--user", "nobody@example.com")


@override_settings(**VAPID_SETTINGS)
def test_explicit_days_argument_overrides_the_default_window():
    user = _user("custom@example.com")
    _sub(user)
    _tracked(user, days=3)

    with patch("accounts.push.webpush") as mock_webpush:
        _run("--days", "3")

    mock_webpush.assert_called_once()


# ---------------------------------------------------------------------------
# The posting the firm already took down.
#
# The incident: `_due_rows` filtered on the student's own funnel stage
# (TRACK_CLOSED, "Done") and never on `Opportunity.status`, which the nightly
# reverify pass writes when a firm pulls a posting. A student's phone raised
# "Goldman Sachs Summer Analyst closes in 2 days" for a role that had died the
# night before, naming a deadline that no longer belonged to anything.
# ---------------------------------------------------------------------------

@override_settings(**VAPID_SETTINGS)
def test_a_posting_the_firm_closed_is_never_pushed():
    """The bug itself: a scraper-closed posting sitting exactly on T-7."""
    user = _user("dead@example.com")
    _sub(user)
    _tracked(user, days=7, status="closed")

    with patch("accounts.push.webpush") as mock_webpush:
        out = _run()

    mock_webpush.assert_not_called()
    assert "nothing due" in out


@override_settings(**VAPID_SETTINGS)
def test_a_closed_posting_is_silent_at_every_funnel_stage():
    """Including for a student who already submitted. They still care about
    the role, and it stays on their list saying so, but a push is an
    instruction to act before a door shuts and that door is shut."""
    user = _user("applied@example.com")
    _sub(user)
    _tracked(user, n=1, days=7, applied_status="submitted", status="closed")
    _tracked(user, n=2, days=2, applied_status="interview", status="closed")

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_not_called()


@override_settings(**VAPID_SETTINGS)
def test_a_posting_the_scraper_never_rechecked_still_pushes():
    """The over-filtering guard, and the reason the rule is `== "closed"`
    rather than `!= "open"`. `Opportunity.status` is a bare CharField
    defaulting to "", so a row no reverify pass has reached carries neither
    value — reading blank as "not open" would silence the alerts for every
    one of them, which is a bigger outage than the bug being fixed."""
    user = _user("blank@example.com")
    _sub(user)
    _tracked(user, days=7, status="")

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_called_once()


@override_settings(**VAPID_SETTINGS)
def test_an_open_posting_beside_a_closed_one_still_gets_its_push():
    """Guards the filter against over-reach in the other direction: one dead
    row in a student's list must not suppress the live rows next to it."""
    user = _user("mixed@example.com")
    _sub(user)
    _tracked(user, n=1, days=7, status="closed")
    live = _tracked(user, n=2, days=7, status="open")

    with patch("accounts.push.webpush") as mock_webpush:
        _run()

    mock_webpush.assert_called_once()
    _, kwargs = mock_webpush.call_args
    assert live.title in kwargs["data"]
