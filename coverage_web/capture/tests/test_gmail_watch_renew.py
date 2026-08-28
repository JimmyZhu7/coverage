"""`gmail_watch_renew` — the command-level half of the push-configuration
narrowing in `capture/gmail_live.py`.

`is_configured()` no longer requires `GMAIL_LIVE_PUBSUB_TOPIC` (a deployment
can connect and sync mail with no Pub/Sub at all — see gmail_poll.py's
docstring), so this command cannot keep gating on it: a topicless-but-synced
deployment is a real, supported state, and "Gmail Live is not configured"
would be a false claim about it. It gates on `is_push_configured()` instead,
which is exactly the base config plus the topic — see gmail_live.py's
`TestIsConfigured`/`TestIsPushConfigured` for the split itself.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from capture import gmail_live
from ops.models import JobRun

pytestmark = pytest.mark.django_db


def _run(**opts):
    out = StringIO()
    call_command("gmail_watch_renew", stdout=out, **opts)
    return out.getvalue()


def test_nothing_to_renew_when_push_is_not_configured():
    """The exact scenario this narrowing exists for: a deployment with a
    working OAuth connect and `gmail_poll` running, but no Pub/Sub topic set
    up yet. `is_configured()` would now say True for that deployment — this
    command must still hold itself to the stricter `is_push_configured()`
    and refuse cleanly rather than call `renew_watches()` at all."""
    with patch.object(gmail_live, "is_push_configured", return_value=False), \
         patch.object(gmail_live, "renew_watches") as renew:
        output = _run()

    renew.assert_not_called()
    assert "GMAIL_LIVE_PUBSUB_TOPIC" in output
    assert "nothing to renew" in output


def test_renews_when_push_is_configured():
    with patch.object(gmail_live, "is_push_configured", return_value=True), \
         patch.object(gmail_live, "renew_watches", return_value=(2, 0)) as renew:
        output = _run()

    renew.assert_called_once()
    assert "renewed: 2, revoked: 0" in output


def test_a_revoked_connection_exits_non_zero():
    """`sys.exit(1)` on any revoked connection — a revoked watch is a
    user-visible "needs to reconnect" state worth surfacing to whatever's
    watching this cron the way a failing scrape is."""
    with patch.object(gmail_live, "is_push_configured", return_value=True), \
         patch.object(gmail_live, "renew_watches", return_value=(0, 1)):
        with pytest.raises(SystemExit) as exc_info:
            call_command("gmail_watch_renew")

    assert exc_info.value.code == 1


def test_the_unconfigured_no_op_still_records_a_successful_job_run():
    """"Nothing to renew" is a normal outcome of a supported deployment
    shape, not a failure — the cron heartbeat at /ops/health/cron/ must not
    flag it."""
    with patch.object(gmail_live, "is_push_configured", return_value=False):
        _run()

    run = JobRun.objects.filter(name="gmail-watch-renew").latest("started_at")
    assert run.status == JobRun.STATUS_SUCCESS
