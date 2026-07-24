"""founder_cutover — migrate the founder's real campaign.db into Coverage
(build-plan §7, M2's gate: "migrate the real campaign.db through the import
path and stop using the CLI").

    python manage.py founder_cutover --email you@example.com
    python manage.py founder_cutover --email you@example.com --db /path/campaign.db
    python manage.py founder_cutover --email ... --password '...'   # else generated

What it does, in order — and why each step goes the long way round:

1. **Contacts go through the real import path** (`accounts.services
   .import_contacts`), not a bulk insert. M2 exists to prove that path on
   real data; a side door would prove nothing. The campaign firm ids
   ("gs", "ms") are translated to directory firm NAMES first so the
   importer's name-matcher links contacts to shared Firm rows.
2. **Enrichment** fills what the CSV path doesn't carry (linkedin, source,
   school flag, archived, original created timestamp) straight onto the
   created rows — display fields only, no state.
3. **Touches replay through the domain state machine**
   (`coverage_domain.pipeline.apply_touch`, chronological, historical
   `now=`), so warmth/thread_state are RE-DERIVED by the same ratchet that
   will govern them from now on — not copied as dead numbers.
4. **Reconciliation**: where the replayed state differs from campaign.db's
   stored state (e.g. state was set manually in the CLI), `set_state`
   applies the stored value with an audit note. The machine's ledger stays
   honest about which values were derived and which were declared.
5. **UserFirm rows** are created for every firm the founder has contacts
   at, with tiers read from the founder's own firms.yaml (tier is personal
   targeting, which is why the shared seed drops it).

Idempotent: contacts dedup inside the import path; the touch replay is
skipped entirely if imported touches already exist for this user.
"""

from __future__ import annotations

import csv
import io
import secrets
import sqlite3
from datetime import datetime, timezone as py_tz
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.services import import_contacts
from coverage_domain import pipeline
from directory.models import Firm
from directory.seed_parsers import parse_firms_yaml

from crm.models import Contact, Touch, UserFirm
from crm.services import _pipeline_connection

_DEFAULT_DB = Path(
    "/Users/zhujimmy/Claude/Projects/Recruitment Opportunities/campaign/campaign.db"
)
_DEFAULT_FIRMS_YAML = _DEFAULT_DB.parent / "firms.yaml"


def _parse_ts(raw: str):
    """campaign.db timestamps are ISO-ish text; anything unparseable maps to
    now (better a fresh-looking touch than a dropped one)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip()[: len(fmt) + 2], fmt).replace(
                tzinfo=py_tz.utc
            )
        except (ValueError, AttributeError):
            continue
    return timezone.now()


class Command(BaseCommand):
    help = "Migrate the founder's campaign.db (contacts, touches, state) into Coverage."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", default=None,
                            help="Password for the (new) account; generated if omitted.")
        parser.add_argument("--db", default=str(_DEFAULT_DB))
        parser.add_argument("--firms-yaml", default=str(_DEFAULT_FIRMS_YAML))

    def handle(self, *args, **opts):
        db_path = Path(opts["db"])
        if not db_path.exists():
            raise CommandError(f"campaign.db not found at {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # ---- 0. The founder's account (local password login) --------------
        User = get_user_model()
        user = User.objects.filter(email__iexact=opts["email"]).first()
        password = opts["password"]
        if user is None:
            password = password or f"coverage-{secrets.token_urlsafe(8)}"
            user = User.objects.create_user(email=opts["email"], password=password)
            self.stdout.write(self.style.SUCCESS(
                f"Created account {opts['email']} — password: {password}"
            ))
        elif password:
            user.set_password(password)
            user.save(update_fields=["password"])

        # ---- 1. Contacts through the real import path ---------------------
        firm_name_by_slug = dict(Firm.objects.values_list("slug", "name"))
        rows = conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", "email", "firm", "role", "notes", "angle"])
        for r in rows:
            writer.writerow([
                r["name"],
                r["email"] or "",
                firm_name_by_slug.get(r["firm"], r["firm"]),
                r["role"] or "",
                r["notes"] or "",
                r["angle"] or "",
            ])
        result = import_contacts(
            user, file_bytes=buf.getvalue().encode(), filename="campaign-cutover.csv"
        )
        self.stdout.write(
            f"import path: {result.created} created, "
            f"{result.skipped_duplicate} duplicate, {result.firm_matched} firm-matched"
        )

        # Map campaign contact id -> Coverage Contact (by name+email).
        mine = {(c.name.strip().lower(), (c.email or "").strip().lower()): c
                for c in Contact.objects.for_user(user)}
        id_map: dict[int, Contact] = {}
        for r in rows:
            key = ((r["name"] or "").strip().lower(), (r["email"] or "").strip().lower())
            if key in mine:
                id_map[r["id"]] = mine[key]

        # ---- 2. Enrich display fields the CSV path doesn't carry ----------
        enriched = 0
        for r in rows:
            c = id_map.get(r["id"])
            if c is None:
                continue
            linkedin = (r["linkedin"] or "").strip()
            if linkedin and not linkedin.startswith("http"):
                linkedin = "https://" + linkedin
            updates = {
                "linkedin": linkedin[:512],
                "source": (r["source"] or "")[:64],
                "school_affiliation": bool((r["school_affiliation"] or "").strip()),
                "archived": bool(r["archived"]),
                "email_pattern_recorded": bool(r["email_pattern_recorded"]),
                "created": _parse_ts(r["created"]),
            }
            Contact.all_objects.filter(pk=c.pk).update(**updates)
            enriched += 1
        self.stdout.write(f"enriched: {enriched} contacts")

        # ---- 3. Replay touches through the state machine ------------------
        if Touch.all_objects.filter(user=user, source="import").exists():
            self.stdout.write("touch replay: already done for this user — skipped")
        else:
            touches = conn.execute(
                "SELECT * FROM touches ORDER BY ts, id"
            ).fetchall()
            replayed = skipped = 0
            with _pipeline_connection() as pconn:
                for t in touches:
                    c = id_map.get(t["contact_id"])
                    if c is None or not t["kind"]:
                        skipped += 1
                        continue
                    pipeline.apply_touch(
                        pconn, user.id, c.id, t["kind"], t["channel"] or "email",
                        t["note"], now=_parse_ts(t["ts"]),
                    )
                    replayed += 1
            # Tag the replayed rows so a re-run can detect the cutover.
            Touch.all_objects.filter(user=user).update(source="import")
            self.stdout.write(f"touch replay: {replayed} applied, {skipped} skipped")

            # ---- 4. Reconcile machine state vs campaign's stored state ----
            overridden = 0
            with _pipeline_connection() as pconn:
                for r in rows:
                    c = id_map.get(r["id"])
                    if c is None:
                        continue
                    c.refresh_from_db()
                    want_w, want_t = r["warmth"], r["thread_state"]
                    if c.warmth != want_w or c.thread_state != want_t:
                        pipeline.set_state(
                            pconn, user.id, c.id,
                            warmth=want_w, thread_state=want_t,
                            note="cutover: aligned to campaign.db stored state",
                        )
                        overridden += 1
            self.stdout.write(f"state reconciliation: {overridden} overrides")

        # ---- 5. UserFirm targeting with the founder's own tiers -----------
        tiers = {}
        yaml_path = Path(opts["firms_yaml"])
        if yaml_path.exists():
            for f in parse_firms_yaml(yaml_path.read_text()):
                if f.get("tier") is not None:
                    tiers[f["id"]] = f["tier"]
        firm_ids = {c.firm_id for c in Contact.objects.for_user(user) if c.firm_id}
        created_uf = 0
        for firm in Firm.objects.filter(id__in=firm_ids):
            _, was_created = UserFirm.all_objects.get_or_create(
                user=user, firm=firm, defaults={"tier": tiers.get(firm.slug)}
            )
            created_uf += int(was_created)
        self.stdout.write(f"user_firms: {created_uf} created")

        conn.close()
        self.stdout.write(self.style.SUCCESS("Cutover complete."))
