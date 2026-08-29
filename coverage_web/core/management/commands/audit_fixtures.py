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

A FOURTH case is why this command grew past Firm/FirmDate/User/Contact: 60
rows in `directory.Opportunity` — "Morgan Stanley / '2027 Summer Analyst,
Seat 0' .. 'Seat 11'" and 48 more at J.P. Morgan, every one carrying a
`url=https://seed.local/...` address — sat in the founder's live Opportunities
feed until he found them by scrolling. Nothing above was looking at
`Opportunity` at all. A FIFTH, six `crm.Contact` rows named "Verify Cold One"
etc. on the demo account, was the same shape as (5) below but did not match
its pattern (no "ZZZ"/"smoke test" in the name) — the fix there was widening
the pattern, not adding a table.

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
4. `directory.Opportunity` rows that either carry a `seed.local` URL (no
   connector this repo ships ever fetches from that host — see
   `directory/ingest.py`'s `Opportunity.objects.create`, whose `url` always
   comes straight off a real provider's own API response) or a title with a
   sequential "Seat N" suffix (real postings are never numbered that way;
   it is what a script generating N fake rows for one firm produces when it
   loops). Either tell alone is enough — the two happened to travel
   together in the one leak found so far, but a future leak reusing only
   one of them should still be caught. There is no tracked seed file for
   this table to cross-check against, unlike Firm: `Opportunity` rows are
   never legitimately created by anything except `ingest.py`'s real scrape,
   so there is no "real fixture" carve-out to build here at all.
5. Every private-zone row (see `coverage_web.tenancy.PrivateModel`) owned by
   a user flagged in (1), OR pointing at an `Opportunity` flagged in (4), OR
   pointing at a `Firm` flagged in (2) — walked generically over every model
   with the relevant foreign key, via `all_objects` so the tenant-scope
   guard does not get in the way of an admin-only audit like this one. This
   is what surfaces a fixture Opportunity's `analytics.UserOpportunity` /
   `directory.OpportunityChange` rows (and a fixture Firm's `FirmDate` /
   `ContactProposal` / `UserFirm` rows) without a bespoke check for each —
   they have no free-text field of their own to pattern-match, only a
   foreign key to something already flagged, so cascading is the right
   generalization rather than one more hand-written check per table.
6. Free-text name/title fields across every private-zone table an agent
   plausibly writes to while eyeballing whether a feature renders —
   `crm.Contact.name`, `capture.ContactProposal.name`,
   `crm.CalendarEvent.title`, `crm.Campaign.label`/`signature`, and
   `crm.Touch.note`/`subject` — against one shared pattern: a "ZZZ" prefix,
   "Verify "/"Test " at the start of the string, or "smoke test"/
   "idempotency test" anywhere. Checked for ANY owner, not just already-
   flagged users, because (per the fifth leak above) these rows tend to
   land on real accounts — the founder's own, or the demo account — not on
   a throwaway `@example.com` signup.

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
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from accounts.models import User
from capture.models import ContactProposal
from crm.models import Campaign, CalendarEvent, Contact, Touch
from directory.models import Firm, FirmDate, Opportunity
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

# No connector this repo ships ever points at this host (see
# directory/ingest.py: `opp.url` is always the provider's own address,
# fetched live). A bare `icontains` on the DB side would also match it
# inside an unrelated query string; this checks the actual parsed host so
# that near-miss can never fire.
def _has_seed_local_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "seed.local" or host.endswith(".seed.local")


# Real postings are never titled this way — it's the shape a script
# generating N fake rows for one firm in a loop produces ("Seat 0" ..
# "Seat 11"), not anything a recruiting site would post.
_SEQUENTIAL_SEAT_TITLE = re.compile(r"\bseat\s*\d+\b", re.IGNORECASE)

# The generalized version of purge_test_contacts.FIXTURE_NAME_PREFIX's guard,
# widened past Contact after a "Verify Cold One"-shaped leak on the demo
# account didn't match the original ZZZ/smoke-test-only pattern. Shared by
# every free-text name/title field this command checks (see WHAT IT LOOKS
# FOR (6)) rather than one regex per table, since the shape an agent's
# "let me just verify this renders" leaves behind is the same everywhere it
# lands: a "ZZZ" prefix, or "Verify"/"Test" leading the string (anchored and
# word-bounded, so "Verify J.P. Morgan" and "Test User" match but "Testino
# Capital" or "Verifying" do not), or the two exact smoke-test phrases
# purge_test_contacts already knew about.
_FIXTURE_NAME_PATTERN = re.compile(
    r"^(?:ZZZ|Verify|Test)\b|smoke test|idempotency test", re.IGNORECASE
)


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
        findings += self._audit_opportunities()
        findings += self._audit_contacts()
        findings += self._audit_contact_proposals()
        findings += self._audit_calendar_events()
        findings += self._audit_campaigns()
        findings += self._audit_touches()

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
        self._cascade_rows_for(User, [u.id for u in flagged])
        return len(flagged)

    def _cascade_rows_for(self, related_model, ids) -> None:
        """Walk every model with a foreign key to `related_model` and report
        rows pointing at one of `ids` — the cascade a delete would take,
        shown up front rather than discovered mid-review. Originally written
        for flagged Users only; generalized so a fixture Firm's FirmDate /
        UserFirm / ContactProposal rows and a fixture Opportunity's
        UserOpportunity / OpportunityChange rows get the same treatment
        without a bespoke check per table — those rows carry no free-text
        field of their own to pattern-match, only a foreign key to something
        already flagged, so "who points at it" is the only check that makes
        sense for them. `all_objects` (falling back to `objects` for
        shared-zone models that don't define it) so the tenant-scope guard
        never gets in the way of an admin-only audit like this one."""
        if not ids:
            return
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if getattr(field, "related_model", None) is related_model and field.many_to_one:
                    manager = getattr(model, "all_objects", model.objects)
                    count = manager.filter(**{f"{field.name}__in": ids}).count()
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
        self._cascade_rows_for(Firm, [f.id for f in flagged])
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

    # ------------------------------------------------------- opportunities

    def _audit_opportunities(self) -> int:
        # Coarse DB-side prefilter before the precise Python check. This
        # table runs ~20k+ rows on the live dev set (see Opportunity.Meta's
        # index comment) and each carries a `raw` JSONField of the
        # provider's full posting payload — looping every row through
        # Python just to regex-check its title/url would mean pulling that
        # whole payload for every legitimate posting on every audit run.
        # `icontains`/`iregex` are deliberately broader than the real check
        # (no word-boundary, no exact-host match): a prefilter that is too
        # loose only costs a wasted row fetch below, one that is too tight
        # would silently hide a real finding, so this errs wide on purpose.
        candidates = (
            Opportunity.objects
            .filter(Q(url__icontains="seed.local")
                    | Q(title__iregex=r"seat\s*[0-9]+"))
            .select_related("firm")
        )
        flagged = [
            o for o in candidates
            if _has_seed_local_host(o.url) or _SEQUENTIAL_SEAT_TITLE.search(o.title)
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"directory.Opportunity — {len(flagged)} fixture-shaped posting(s)"))
        for o in flagged:
            self.stdout.write(
                f"  #{o.id} firm={o.firm.name!r} title={o.title!r} url={o.url!r} "
                f"source={o.source!r} status={o.status!r}")
        self._cascade_rows_for(Opportunity, [o.id for o in flagged])
        return len(flagged)

    # ---------------------------------------------------------- contacts

    def _audit_contacts(self) -> int:
        flagged = [c for c in Contact.all_objects.all() if _FIXTURE_NAME_PATTERN.search(c.name)]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"crm.Contact — {len(flagged)} fixture-shaped contact(s) "
            f"(purge_test_contacts pattern)"))
        for c in flagged:
            owner = User.objects.filter(pk=c.user_id).values_list("email", flat=True).first()
            self.stdout.write(f"  #{c.id} {c.name!r} owner={owner!r} archived={c.archived}")
        return len(flagged)

    # ------------------------------------------------------ contact proposals

    def _audit_contact_proposals(self) -> int:
        # Gmail Live's "propose, never auto-create" door (capture/models.py's
        # ContactProposal docstring) is exactly the kind of surface an agent
        # checking "does discovery render" would poke at directly, and a
        # proposal's only free-text field is the sender's display name.
        flagged = [
            p for p in ContactProposal.all_objects.all()
            if _FIXTURE_NAME_PATTERN.search(p.name)
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"capture.ContactProposal — {len(flagged)} fixture-shaped proposal(s)"))
        for p in flagged:
            owner = User.objects.filter(pk=p.user_id).values_list("email", flat=True).first()
            self.stdout.write(
                f"  #{p.id} {p.name!r} <{p.email}> owner={owner!r} status={p.status!r}")
        return len(flagged)

    # -------------------------------------------------------- calendar events

    def _audit_calendar_events(self) -> int:
        flagged = [
            e for e in CalendarEvent.all_objects.all()
            if _FIXTURE_NAME_PATTERN.search(e.title)
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"crm.CalendarEvent — {len(flagged)} fixture-shaped event(s)"))
        for e in flagged:
            owner = User.objects.filter(pk=e.user_id).values_list("email", flat=True).first()
            self.stdout.write(
                f"  #{e.id} {e.title!r} owner={owner!r} starts_at={e.starts_at} "
                f"source={e.source!r}")
        return len(flagged)

    # -------------------------------------------------------------- campaigns

    def _audit_campaigns(self) -> int:
        flagged = [
            c for c in Campaign.all_objects.all()
            if _FIXTURE_NAME_PATTERN.search(c.label) or _FIXTURE_NAME_PATTERN.search(c.signature)
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"crm.Campaign — {len(flagged)} fixture-shaped campaign(s)"))
        for c in flagged:
            owner = User.objects.filter(pk=c.user_id).values_list("email", flat=True).first()
            self.stdout.write(
                f"  #{c.id} label={c.label!r} signature={c.signature!r} "
                f"owner={owner!r} kind={c.kind!r}")
        return len(flagged)

    # ----------------------------------------------------------------touches

    def _audit_touches(self) -> int:
        # Touch has no "name" field — its two free-text fields are `note`
        # (a hand-typed line) and `subject` (a captured email Subject
        # header, see the model's docstring). Either is where a
        # verification touch's fixture-shaped text would land.
        flagged = [
            t for t in Touch.all_objects.all()
            if _FIXTURE_NAME_PATTERN.search(t.note or "")
            or _FIXTURE_NAME_PATTERN.search(t.subject or "")
        ]
        if not flagged:
            return 0
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"crm.Touch — {len(flagged)} fixture-shaped touch(es)"))
        for t in flagged:
            owner = User.objects.filter(pk=t.user_id).values_list("email", flat=True).first()
            self.stdout.write(
                f"  #{t.id} kind={t.kind!r} owner={owner!r} note={t.note!r} "
                f"subject={t.subject!r}")
        return len(flagged)
