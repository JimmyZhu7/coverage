"""audit_fixtures — report rows in the shared dev database that look like
agent/test fixtures rather than real product data.

    python manage.py audit_fixtures

Read-only, always — this command only ever prints. Exits non-zero when it
finds anything, so a verification session can end on a check that either
comes back clean or says exactly what to look at, instead of "seemed fine."

WHY THIS EXISTS
---------------
This repo's dev Postgres database (`coverage`, not the pytest-only
`test_coverage_*` one — see settings/base.py's Database section) is shared
across every worktree on the box, and it is also where the founder's own
account lives. An agent "verifying its own work" against a running dev
server — rather than against pytest's throwaway database — writes directly
into that shared database. That has happened at least twice on record:

  - `Firm.slug` (directory/models.py) documents a blank-slug row that could
    only have come from "a `manage.py shell` insert that simply omitted the
    field" — nothing in this repo's own code paths passes no slug.
  - `crm/management/commands/purge_test_contacts.py` documents four rows
    ("ZZZ Smoke Test Contact", "ZZZ Idempotency Test", ...) written into the
    FOUNDER'S OWN production CRM by "automated smoke runs [...] pointed at
    the live database", and says outright: "Nothing in this repository
    creates these rows [...] Delete them today and the next such run writes
    them again."

A live instance found and deleted a third case by chance while investigating
this exact problem (Firm "Verify J.P. Morgan", slug `verify-jpm-play` — it
was rendering as a real card on the founder's Today page). This command is
the general form of "how was that one found" — noticing by chance does not
scale, so this makes the same shape of residue greppable on demand instead.

WHAT IT LOOKS FOR
------------------
1. Users signed up at an RFC 2606 reserved, non-deliverable email domain
   (example.com/.net/.org/.edu, anything ending .invalid or .test, bare
   `localhost`) — no real recruiting student's inbox lives there, whatever
   the display name says. `admin@coverage.local` and `demo@coverage.local`
   are the two legitimate system accounts and are always excluded by exact
   address, not by domain, so a third `...@coverage.local` account is
   deliberately NOT given a pass.
2. `directory.Firm` rows whose slug or name reads as synthetic (verify-,
   test, dummy, placeholder, sample, foo, acme, "busy co" — matched as whole
   words so real names like "Barclays" or "State Street" are never caught)
   AND are not present in the tracked seed file `directory/seeds/firms.yaml`
   (the canonical source of real shipped firms — see that file's header).
3. `directory.FirmDate` rows with no possible legitimate origin. Both
   writers that are allowed to create this table's rows —
   `seed_directory` (from the tracked timeline_*.yaml files) and
   `import_firm_dates` (the recruiting-radar agent's findings importer) —
   always do two things this command checks for: they cap `confidence` to
   {0.0, 0.3, 0.6, 1.0} (the only values either command's CONFIDENCE_BAND
   can produce), and they always set `found_on` together with a non-empty
   `history` entry recording where the date came from. A row with neither —
   or with a confidence outside that band — did not come from either path.
   The one legitimate exception is `scripts/demo_seed.py` / `seed_demo`'s
   single placeholder row, which is always tagged `source_url="seed:demo"`
   and is excluded by that marker (see `directory/tests/test_firm_timeline.py`,
   which asserts this exact row renders as labeled sample data).
4. Every private-zone row (see `coverage_web.tenancy.PrivateModel`) owned by
   a user flagged in (1) — walked generically over every model with a
   `user` foreign key, via `all_objects` so the tenant-scope guard does not
   get in the way of an admin-only audit like this one.
5. `crm.Contact` rows, for ANY owner, whose name matches the exact pattern
   `purge_test_contacts` was written to clean up (a "ZZZ" prefix, or
   "Smoke Test" / "Idempotency Test" in the name) — the generalized,
   standing version of that one-off command's hard-coded id list, so the
   next occurrence of the same smoke-test pattern shows up here even if it
   lands somewhere purge_test_contacts's DEFAULT_IDS does not look.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
Delete anything. Ever. A wrongly deleted real firm or a real user's contact
is worse than a leftover fixture — this command's only job is to make
residue visible to a human (or an agent that will then ask a human), the
same posture `purge_test_contacts` and `crm_health` already take.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from crm.models import Contact
from directory.models import Firm, FirmDate
from directory.seed_parsers import parse_firms_yaml

# RFC 2606 reserves example.com/.net/.org/.edu and the .invalid / .test TLDs
# for documentation and testing, permanently and by design — no mail server
# on the public internet will ever deliver to an address there. A handful of
# this repo's own tests and scripts additionally use the unroutable
# `.invalid` TLD for the same reason. Nothing that could plausibly be a real
# student's inbox matches any of these.
_RESERVED_EMAIL_DOMAIN = re.compile(
    r"(?:^|\.)example\.(?:com|net|org|edu)$|\.(?:invalid|test)$|^localhost$",
    re.IGNORECASE,
)

# The only two accounts this repo's own tooling creates outside of pytest —
# see scripts/demo_seed.py and the createsuperuser step in docs/deploy.md.
# Excluded by exact address on purpose: a THIRD @coverage.local account is
# not given a free pass just for sharing the domain.
_KNOWN_SYSTEM_ACCOUNTS = {"admin@coverage.local", "demo@coverage.local"}

# Matched as whole words against Firm.slug/name so "Barclays" or "State
# Street" (which merely contain "bar" as a substring) are never flagged.
_FIXTURE_FIRM_WORDS = re.compile(
    r"\b(verify|dummy|placeholder|sample|foo|acme|busy)\b|\btest\b|-play$",
    re.IGNORECASE,
)

# Both legitimate FirmDate writers (seed_directory._CONFIDENCE_BAND and
# import_firm_dates.CONFIDENCE_BAND) only ever produce one of these four
# floats. Anything else did not come from either command.
_VALID_CONFIDENCE = {0.0, 0.3, 0.6, 1.0}

# The generalized version of purge_test_contacts.FIXTURE_NAME_PREFIX's guard.
_FIXTURE_CONTACT_NAME = re.compile(r"^ZZZ\b|smoke test|idempotency test", re.IGNORECASE)


def _seeded_firm_slugs() -> set[str]:
    """Slugs the tracked seed file ships for real — never flag one of these
    regardless of what its name happens to contain. Uses the same
    hand-rolled flow-YAML parser `seed_directory` itself reads with (this
    repo carries no PyYAML dependency, see directory/seed_parsers.py)."""
    path = Path(settings.BASE_DIR) / "directory" / "seeds" / "firms.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {str(row.get("id", "")) for row in parse_firms_yaml(text)}


class Command(BaseCommand):
    help = "Report rows that look like agent/test fixtures leaked into the shared dev database."

    def handle(self, *args, **opts):
        findings = 0

        findings += self._audit_users()
        findings += self._audit_firms()
        findings += self._audit_firm_dates()
        findings += self._audit_contacts()

        self.stdout.write("")
        if findings:
            self.stdout.write(self.style.WARNING(
                f"{findings} fixture-shaped row(s) found. Nothing was deleted — "
                f"review each before removing it."))
            # CommandError (not a plain return) is what gives manage.py a
            # non-zero process exit code — the same mechanism crm_health
            # uses so a script or an agent's own end-of-session check can
            # branch on the status code instead of scraping stdout.
            raise CommandError(f"{findings} fixture-shaped row(s) found — see above.")
        self.stdout.write(self.style.SUCCESS("No fixture-shaped rows found."))

    # ------------------------------------------------------------- users

    def _flagged_users(self):
        return [
            u for u in User.objects.all()
            if u.email not in _KNOWN_SYSTEM_ACCOUNTS
            and _RESERVED_EMAIL_DOMAIN.search(u.email.split("@")[-1] or "")
        ]

    def _audit_users(self) -> int:
        flagged = self._flagged_users()
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"accounts.User — {len(flagged)} account(s) at a reserved test domain"))
        for u in flagged:
            self.stdout.write(f"  #{u.id} {u.email!r} — joined {u.date_joined:%Y-%m-%d}")
        self._audit_private_rows_for(flagged)
        return len(flagged)

    def _audit_private_rows_for(self, flagged_users) -> None:
        """Walk every private-zone model with a `user` FK and report rows
        owned by an already-flagged user — the cascade a User.delete() would
        take, shown up front rather than discovered mid-review."""
        uids = [u.id for u in flagged_users]
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if getattr(field, "related_model", None) is User and field.many_to_one:
                    manager = getattr(model, "all_objects", model.objects)
                    count = manager.filter(**{f"{field.name}__in": uids}).count()
                    if count:
                        self.stdout.write(
                            f"    cascade: {model._meta.app_label}.{model.__name__} "
                            f"({field.name}) — {count} row(s)")

    # ------------------------------------------------------------- firms

    def _audit_firms(self) -> int:
        real_slugs = _seeded_firm_slugs()
        flagged = [
            f for f in Firm.objects.all()
            if f.slug not in real_slugs
            and (_FIXTURE_FIRM_WORDS.search(f.slug) or _FIXTURE_FIRM_WORDS.search(f.name))
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"directory.Firm — {len(flagged)} synthetic-looking, unseeded firm(s)"))
        for f in flagged:
            self.stdout.write(
                f"  #{f.id} slug={f.slug!r} name={f.name!r} status={f.status!r} "
                f"domains={f.domains} opportunities={f.opportunities.count()}")
        return len(flagged)

    # -------------------------------------------------------- firm dates

    def _audit_firm_dates(self) -> int:
        flagged = [
            fd for fd in FirmDate.objects.select_related("firm").all()
            if fd.source_url != "seed:demo"
            and (fd.confidence not in _VALID_CONFIDENCE
                 or (fd.found_on is None and not fd.history))
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"directory.FirmDate — {len(flagged)} row(s) with no legitimate writer"))
        for fd in flagged:
            self.stdout.write(
                f"  #{fd.id} firm={fd.firm.name!r} cycle={fd.cycle!r} "
                f"event={fd.event_kind!r} confidence={fd.confidence} "
                f"source_url={fd.source_url!r} found_on={fd.found_on} "
                f"history_empty={not fd.history}")
        return len(flagged)

    # ---------------------------------------------------------- contacts

    def _audit_contacts(self) -> int:
        flagged = [c for c in Contact.all_objects.all() if _FIXTURE_CONTACT_NAME.search(c.name)]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"crm.Contact — {len(flagged)} smoke-test-shaped contact(s) "
            f"(purge_test_contacts pattern)"))
        for c in flagged:
            owner = User.objects.filter(pk=c.user_id).values_list("email", flat=True).first()
            self.stdout.write(f"  #{c.id} {c.name!r} owner={owner!r} archived={c.archived}")
        return len(flagged)
