"""gmail_watch_renew — keep every Gmail Live `users.watch()` registration
alive.

Google expires a watch after 7 days regardless of activity — this is
Google's limit, not a Coverage setting, so there is no way to register one
that just lasts. Run this daily (cron/launchd); it only touches connections
whose watch expires within 24h, so a normal run does nothing for most rows.

    python manage.py gmail_watch_renew

Exits non-zero if a connection is discovered `revoked` this run — that is a
user-visible "needs to reconnect" state (also shown on the settings page),
worth surfacing to whatever's watching this job the way a failing board
scrape is.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from capture import gmail_live
from ops.tracking import track_job_run


class Command(BaseCommand):
    help = "Renew Gmail Live watch registrations expiring within 24h."

    def handle(self, *args, **opts):
        # "gmail-watch-renew" matches render.yaml's coverage-gmail-watch-renew
        # cron — see ops/tracking.py. `track_job_run` treats the `sys.exit(1)`
        # below the same as any other exception (SystemExit is a
        # BaseException): a run that discovers a revoked connection records
        # as `failed`, which is exactly the "worth surfacing" posture this
        # command's own docstring already wanted.
        with track_job_run("gmail-watch-renew"):
            # `is_push_configured()`, not the base `is_configured()`: a
            # deployment can be fully connected and syncing (gmail_poll) with
            # no Pub/Sub topic at all — there is simply no push to renew in
            # that state, which is distinct from "Gmail Live is unconfigured".
            if not gmail_live.is_push_configured():
                self.stdout.write(
                    "Gmail Live push is not configured (no "
                    "GMAIL_LIVE_PUBSUB_TOPIC) — nothing to renew."
                )
                return

            renewed, revoked = gmail_live.renew_watches()
            self.stdout.write(f"renewed: {renewed}, revoked: {revoked}")
            if revoked:
                sys.exit(1)
