#!/usr/bin/env bash
# Create the local Postgres role + databases Coverage expects.
#
# Run this ONCE, after Postgres.app is installed and initialized (green
# "running" state). It is idempotent — safe to re-run. It creates:
#   - role     "coverage" (password "coverage", can create DBs)
#   - database "coverage"        (dev)
#   - database "coverage_test"   (used by pytest / the domain concurrency test)
#
# These match the DATABASE_URL default in .env.example AND the Postgres
# service in .github/workflows/ci.yml, so local and CI stay identical.

set -euo pipefail

# Postgres.app ships psql under /Applications; fall back to PATH.
PSQL=""
for candidate in \
  /Applications/Postgres.app/Contents/Versions/latest/bin/psql \
  "$(command -v psql 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then PSQL="$candidate"; break; fi
done

if [ -z "$PSQL" ]; then
  echo "ERROR: psql not found. Is Postgres.app installed and initialized?" >&2
  echo "       Looked in /Applications/Postgres.app and on PATH." >&2
  exit 1
fi

echo "Using: $PSQL"

# Connect to the default per-user database Postgres.app creates (named after
# \$USER). Everything below is guarded so re-runs don't error.
"$PSQL" -d "$USER" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coverage') THEN
    CREATE ROLE coverage LOGIN PASSWORD 'coverage' CREATEDB;
  END IF;
END
$$;
SELECT 'CREATE DATABASE coverage OWNER coverage'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'coverage')\gexec
SELECT 'CREATE DATABASE coverage_test OWNER coverage'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'coverage_test')\gexec
SQL

echo "OK — roles/databases ready:"
"$PSQL" -d "$USER" -c "\du coverage"
"$PSQL" -d "$USER" -c "\l coverage*"
