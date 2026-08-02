#!/bin/bash
# Double-click this file in Finder to start Coverage and open it in your browser.
# Keep the window that appears OPEN while you use Coverage; close it to stop.

cd "$(dirname "$0")" || exit 1

# Find the tools Coverage needs, wherever they were installed.
export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "──────────────────────────────────────────────"
echo "  Starting Coverage…"
echo "──────────────────────────────────────────────"

if ! command -v uv >/dev/null 2>&1; then
  echo "✗ Can't find 'uv' (the tool that runs Coverage)."
  echo "  Ask Claude to help — this window can stay open."
  read -r -p "Press Return to close." _; exit 1
fi

# 1) Make sure the database (Postgres.app) is running.
if [ ! -d "/Applications/Postgres.app" ]; then
  echo "✗ Postgres.app isn't installed. Download it from https://postgresapp.com,"
  echo "  drag it to Applications, open it once and click Initialize, then try again."
  read -r -p "Press Return to close." _; exit 1
fi
echo "• Waking the database…"
open -a Postgres
for _ in $(seq 1 30); do pg_isready -h localhost -p 5432 -q 2>/dev/null && break; sleep 1; done

# 2) Make sure the tables exist (safe to repeat).
#
# The demo-student seed used to run here on every launch. It was right when
# nobody had signed up yet and an empty app looked broken; it is just noise
# now that the real account has 139 contacts of its own. The demo user still
# exists and is tenant-isolated — run `scripts/demo_seed.py` by hand if you
# ever need a populated account to show someone.
echo "• Preparing your data…"
uv run --package coverage-web python coverage_web/manage.py migrate --noinput >/dev/null 2>&1

# 2b) If the listings haven't been refreshed in 12+ hours, refresh them in the
#     background (scrape + classify + re-verify) so the feed is never stale
#     just because the daily job didn't get a chance to run.
FRESHNESS=$(uv run --package coverage-web python coverage_web/manage.py shell -c "
from django.utils import timezone
from directory.models import ScrapeRun
r = ScrapeRun.objects.filter(status__in=['ok','partial']).order_by('-started').first()
print('stale' if r is None or (timezone.now() - r.started).total_seconds() > 43200 else 'fresh')
" 2>/dev/null | tail -1)
if [ "$FRESHNESS" = "stale" ]; then
  echo "• Listings look stale — refreshing in the background…"
  (bash scripts/refresh.sh >/dev/null 2>&1 &)
fi

# 3) Open the browser once the site is ready (unless told not to, for testing).
if [ "$COVERAGE_NO_OPEN" != "1" ]; then
  ( for _ in $(seq 1 40); do
      curl -s http://127.0.0.1:8000/healthz >/dev/null 2>&1 && { open "http://127.0.0.1:8000/opportunities/"; break; }
      sleep 0.5
    done ) &
fi

echo ""
echo "  ✓ Coverage is starting. A browser tab will open in a moment."
echo ""
echo "    Opportunities feed (no login): http://127.0.0.1:8000/opportunities/"
echo "    Log in to see the CRM:         http://127.0.0.1:8000/accounts/login/"
echo "        email:     you@example.com"
echo "        password:  (the one you set)"
echo ""
echo "    Demo student (sample data):    demo@coverage.local / demo1234"
echo ""
echo "  KEEP THIS WINDOW OPEN while you use Coverage. Close it to stop."
echo "──────────────────────────────────────────────"

# 4) Run the site (this holds the window open).
exec uv run --package coverage-web python coverage_web/manage.py runserver 127.0.0.1:8000
