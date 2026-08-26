"""seed_mail_domains — put the firms' MAIL domains in the directory, so the
capture pipeline can recognise a real banker's email address.

    python manage.py seed_mail_domains --dry-run
    python manage.py seed_mail_domains

WHY THIS COMMAND EXISTS RATHER THAN A ROW IN `firms.yaml`
---------------------------------------------------------
`capture.discovery.FirmDomains` decides whether an unknown sender belongs to
a firm the student tracks by matching the address against `Firm.domains`.
That column had been filled almost entirely by board connectors, which only
ever knew a CAREER-SITE host (`careers.bcg.com`, `jobs.rbc.com`,
`blackstone.wd1.myworkdayjobs.com`). Nobody sends mail from those. So
`gs.com` did not resolve to Goldman, `bofa.com` did not resolve to Bank of
America, and nineteen of forty-eight real bankers in the founder's own
mailbox were refused for that reason alone.

The fix was applied by hand to the founder's database and to
`data/seeds/firms.yaml` — and `data/` is gitignored (the whole directory is,
under the "this repo is PUBLIC" block: it holds real people's names, private
assessments of them, and the founder's own hand-tiered target list). So none
of it was under version control and a fresh deploy would have regressed to
the broken state. `firms.yaml` was NOT un-ignored to fix that: its firm rows
carried the founder's personal `tier: 1/2/3` curation and several paragraphs
reasoning about which employers are prestigious, which is exactly the
"founder's own target list" the ignore rule names. The mail domains
themselves are public facts about firms, so they moved to a tracked module
instead — `directory/_mail_domains.py`, mirroring `_logo_domains.py` and its
`seed_logo_domains` command, which solved the same shape of problem (a
hand-curated firm→domain map that must survive a fresh clone) the same way.

Later the same day the seed corpus turned out to have the identical problem
whole rather than one column of it: `seed_directory` read `data/seeds/` too,
so a fresh deploy got no firms at all. It was fixed the same way and for the
same reason — a SCRUBBED copy at `directory/seeds/*.yaml`, with `tier` and the
prestige prose left behind in the private archive. That does not fold this
command into that one. `seed_directory` knows only the firms in the seed
corpus, replaces their `domains` outright, and runs BEFORE `scrape`; the firms
this command creates and the connector firms it appends to are not in that
corpus at all. Of the eleven slugs here that do have a row in it, seven now
carry their mail domain there already and are skipped as present; the other
four (bcg, blackrock, rbc, socgen) still get theirs from here. The overlap is
left rather than tidied away, so this file stays the single place to look up
what a human at a given firm sends mail from.

`seed_directory` is deliberately NOT the mechanism. It calls
`update_or_create(slug=..., defaults={"domains": ...})`, i.e. it REPLACES the
domain list from the YAML — which would drop every domain a connector or
`seed_logo_domains` had appended (`macquarie.com.au`, `htsc.com.cn`,
`guggenheiminvestments.com`, ...). This command only ever appends.

WHAT IT GUARANTEES
------------------
- **Idempotent.** A domain already on the firm is skipped, so running twice
  changes nothing. Safe against a database that already has the fix.
- **Additive.** Domains are appended to the existing list. Nothing is
  replaced, and a domain someone added by hand is never dropped.
- **Create-or-update by slug.** The four firms in `CREATABLE_FIRMS` are
  created when missing and adopted when present — never duplicated. A firm
  already present under the same NAME is adopted too, whatever its slug:
  the founder's database carries a "Citadel Securities" row with an EMPTY
  slug (a defect predating this work), and keying on slug alone would have
  minted a second Citadel row beside it. `ingest._FirmResolver` resolves by
  exact name before creating for the same reason.
- **Never renames, never re-slugs, never overwrites.** `tracks`/`regions`
  are filled in only when the firm has none; an existing value stands.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory._mail_domains import CREATABLE_FIRMS, MAIL_DOMAINS
from directory.models import Firm


class Command(BaseCommand):
    help = "Add each firm's real email (not career-site) domains to the directory."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""

        resolved: dict[str, Firm | None] = {}
        firms_created = 0

        # ------------------------------------------------------------ firms
        for slug, spec in sorted(CREATABLE_FIRMS.items()):
            firm = self._resolve(slug, spec["name"])
            if firm is None:
                firms_created += 1
                self.stdout.write(f"{tag}+    firm {spec['name']} ({slug})")
                if not dry:
                    firm = Firm.objects.create(
                        slug=slug,
                        name=spec["name"],
                        tracks=list(spec.get("tracks") or []),
                        regions=list(spec.get("regions") or []),
                        status=spec.get("status", "active"),
                    )
            else:
                # Present already: fill in only what is genuinely blank. Never
                # overwrite a curated value, never touch the name or the slug.
                fields = []
                if not firm.tracks and spec.get("tracks"):
                    firm.tracks = list(spec["tracks"])
                    fields.append("tracks")
                if not firm.regions and spec.get("regions"):
                    firm.regions = list(spec["regions"])
                    fields.append("regions")
                if fields:
                    self.stdout.write(
                        f"{tag}~    firm {firm.name}: filled {', '.join(fields)}"
                    )
                    if not dry:
                        firm.save(update_fields=fields)
            resolved[slug] = firm

        # ---------------------------------------------------------- domains
        added = skipped = unknown = 0
        for slug, domains in sorted(MAIL_DOMAINS.items()):
            firm = resolved.get(slug)
            if firm is None:
                firm = self._resolve(slug, None)
            if firm is None:
                # A directory this command may not invent a row for — a
                # connector firm absent from this environment, most likely.
                # Reported, never created: see the module docstring.
                unknown += 1
                self.stdout.write(f"{tag}?    no firm with slug {slug!r}")
                continue
            existing = list(firm.domains or [])
            have = {(d or "").strip().lower() for d in existing}
            new = [d for d in domains if d.strip().lower() not in have]
            if not new:
                skipped += len(domains)
                continue
            added += len(new)
            where = "first domain" if not existing else "appended"
            self.stdout.write(
                f"{tag}+    {firm.name}: {', '.join(new)} ({where})"
            )
            if not dry:
                # APPEND, never replace. The same list carries the career-site
                # hosts the board connectors rely on; swapping it out to fix
                # email matching would quietly break the boards.
                firm.domains = [*existing, *new]
                firm.save(update_fields=["domains"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{firms_created} firm(s) created, {added} mail domain(s) added, "
            f"{skipped} already present, {unknown} unknown slug(s)"
        ))

    # ------------------------------------------------------------------ util

    @staticmethod
    def _resolve(slug: str, name: str | None) -> Firm | None:
        """The firm this entry means: by slug, else by exact name (oldest row
        wins, matching `ingest._FirmResolver`). The name fallback is what
        stops a second "Citadel Securities" being minted beside the
        blank-slug row the founder's database already carries."""
        firm = Firm.objects.filter(slug=slug).first()
        if firm is None and name:
            firm = Firm.objects.filter(name__iexact=name).order_by("id").first()
        return firm
