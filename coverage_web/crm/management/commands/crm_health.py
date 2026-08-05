"""crm_health — does the contact board agree with itself?

    python manage.py crm_health --email you@example.com
    python manage.py crm_health                      # every account

Prints `crm.health`'s findings. Exits non-zero when a real contradiction is
present (not for the review list), so the daily sync — or anything else that
wants to fail loudly — can branch on the status code rather than parse text.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from crm import health


class Command(BaseCommand):
    help = "Report contacts whose record contradicts what Today says about them."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="One account. Omit to check all.")

    def handle(self, *args, **opts):
        User = get_user_model()
        if opts["email"]:
            try:
                users = [User.objects.get(email__iexact=opts["email"])]
            except User.DoesNotExist as exc:
                raise CommandError(f"no user with email {opts['email']}") from exc
        else:
            users = list(User.objects.filter(deleted_at__isnull=True).order_by("email"))

        contradictions = 0
        for user in users:
            lines = health.health_report(user)
            if not lines:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(user.email))
            for line in lines:
                self.stdout.write(f"  {line}")
                contradictions += line.startswith("⚠")

        if contradictions:
            # Loud on purpose: this is the failure mode where the product
            # tells the user something untrue about their own outreach.
            raise CommandError(
                f"{contradictions} contradiction(s) — a contact found in the "
                "mailbox carries no touch, so Today will call them "
                "never-contacted. Fix the writer that created them."
            )
        self.stdout.write(self.style.SUCCESS("The board agrees with itself."))
