#!/bin/bash
# Daily freshness pass for local Coverage — run by launchd
# (~/Library/LaunchAgents/com.coverage.refresh.plist) and safe to run by hand.
# Wakes Postgres.app if needed, then: scrape + reclassify + reverify.
# Logs to ~/Library/Logs/coverage-refresh.log.

set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

LOG="$HOME/Library/Logs/coverage-refresh.log"
{
  echo "── $(date '+%Y-%m-%d %H:%M:%S') refresh starting ──"

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found on PATH; aborting."; exit 1
  fi

  # Wake the database if it isn't already up (no-op when running).
  if ! pg_isready -h localhost -p 5432 -q 2>/dev/null; then
    open -a Postgres 2>/dev/null || true
    for _ in $(seq 1 30); do
      pg_isready -h localhost -p 5432 -q 2>/dev/null && break
      sleep 1
    done
  fi
  if ! pg_isready -h localhost -p 5432 -q 2>/dev/null; then
    echo "Postgres never came up; skipping this run."; exit 1
  fi

  # ---- Nightly backup, BEFORE the refresh touches anything ----------------
  # The database is the only copy of the user's entire outreach history
  # (contacts, touches, debriefs) — git holds code, never data. Dumps go to
  # iCloud Drive so a dead laptop doesn't take the backups with it, with a
  # local fallback when iCloud isn't set up. 30 dumps ≈ a month of history;
  # compressed custom format restores with pg_restore.
  ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
  if [ -d "$ICLOUD" ]; then BACKUPS="$ICLOUD/Coverage Backups"; else BACKUPS="$HOME/Backups/coverage"; fi
  mkdir -p "$BACKUPS"
  STAMP="$(date '+%Y-%m-%d')"
  if pg_dump --format=custom --file="$BACKUPS/coverage-$STAMP.pgdump" \
       "postgres://coverage:coverage@localhost:5432/coverage" 2>&1; then
    echo "backup written: $BACKUPS/coverage-$STAMP.pgdump ($(du -h "$BACKUPS/coverage-$STAMP.pgdump" | cut -f1))"
    # Rotate: keep the newest 30 dumps.
    ls -t "$BACKUPS"/coverage-*.pgdump 2>/dev/null | tail -n +31 | while read -r old; do
      rm -f "$old" && echo "rotated out: $(basename "$old")"
    done
  else
    # A failed backup must be loud in the log but must not block the refresh:
    # stale listings are recoverable, a skipped scrape is just a day's lag.
    echo "BACKUP FAILED — the refresh continues, but fix this before trusting the data to one disk."
  fi

  uv run --package coverage-web python coverage_web/manage.py refresh
  echo "── $(date '+%Y-%m-%d %H:%M:%S') refresh finished ──"
} >>"$LOG" 2>&1
