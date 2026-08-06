"""One consistent snapshot of the database, kept on a small ring.

    python manage.py backup_db                # ~/Backups/coverage/
    python manage.py backup_db --dest /path --keep 14

pg_dump in custom format (-Fc): compressed, and restorable table-by-table with
pg_restore rather than all-or-nothing psql. The ring keeps the newest N and
deletes older ones so an unattended schedule cannot quietly fill a disk.

This command is the free half of the backup story; wiring it to a schedule is
a deploy-time decision (cron, launchd, or the host's own snapshots). It exists
now because the first real user's data deserves a restore path from day one —
"the database is local" is an availability statement, not a durability one.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "pg_dump the app database to a timestamped file, keeping the newest N."

    def add_arguments(self, parser):
        parser.add_argument("--dest", default=str(Path.home() / "Backups" / "coverage"))
        parser.add_argument("--keep", type=int, default=14,
                            help="How many snapshots to retain (default 14).")

    def handle(self, *args, **opts):
        db = settings.DATABASES["default"]
        if "postgresql" not in db["ENGINE"]:
            raise CommandError(f"backup_db only knows PostgreSQL, not {db['ENGINE']}")

        dest = Path(opts["dest"]).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = dest / f"coverage_{stamp}.dump"

        cmd = ["pg_dump", "-Fc", "-f", str(out), "-d", db["NAME"]]
        for flag, key in (("-h", "HOST"), ("-p", "PORT"), ("-U", "USER")):
            if db.get(key):
                cmd += [flag, str(db[key])]
        # Layered over the inherited environment, never replacing it: a bare
        # {"PGPASSWORD": ...} dict wipes PATH and turns "wrong password" into
        # the far more misleading "pg_dump not found".
        env = ({**os.environ, "PGPASSWORD": db["PASSWORD"]}
               if db.get("PASSWORD") else None)

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           env=env, timeout=600)
        except FileNotFoundError:
            raise CommandError("pg_dump not on PATH — install the Postgres client tools")
        except subprocess.CalledProcessError as exc:
            # A failed dump must not leave a half-written file that a restore
            # later mistakes for a snapshot.
            out.unlink(missing_ok=True)
            raise CommandError(f"pg_dump failed: {exc.stderr.strip()[:400]}")

        size_mb = out.stat().st_size / 1_048_576
        # Prune beyond the ring, oldest first, and never the file just written.
        snapshots = sorted(dest.glob("coverage_*.dump"))
        for old in snapshots[:-opts["keep"]]:
            old.unlink()

        self.stdout.write(self.style.SUCCESS(
            f"{out.name} written ({size_mb:.1f} MB) — "
            f"{min(len(snapshots), opts['keep'])} snapshot(s) on the ring"))
