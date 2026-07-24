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

  uv run --package coverage-web python coverage_web/manage.py refresh
  echo "── $(date '+%Y-%m-%d %H:%M:%S') refresh finished ──"
} >>"$LOG" 2>&1
