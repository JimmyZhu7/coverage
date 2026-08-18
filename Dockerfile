# Portable image for Coverage's web service — works on Render (Docker env),
# Fly.io, or any container host. Uses uv for fast, locked installs.
FROM python:3.13-slim

# uv from the official distroless image (pinned minor).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DJANGO_SETTINGS_MODULE=coverage_web.settings.production

WORKDIR /app

# 1) Dependency layer — copy only what uv needs to resolve, so app-code edits
#    don't bust the cached install.
COPY pyproject.toml uv.lock ./
COPY coverage_web/pyproject.toml coverage_web/
COPY coverage_domain/pyproject.toml coverage_domain/
COPY coverage_connectors/pyproject.toml coverage_connectors/
COPY coverage_domain/ coverage_domain/
COPY coverage_connectors/ coverage_connectors/
RUN uv sync --frozen --no-dev --package coverage-web

# 1b) Browser tier: the Beisen connector (CICC) drives headless Chromium via
#     Playwright during scrapes/refreshes. Install the browser + its system
#     libs so the scrape cron can run it. (~300MB; the web service itself
#     never launches a browser, but one image serves both roles on Render.)
RUN uv run --package coverage-web playwright install --with-deps chromium

# 2) App code.
COPY . .

# 3) Collect static into STATIC_ROOT for WhiteNoise. Dummy values for every
#    setting production.py requires with no fallback (SECRET_KEY,
#    ALLOWED_HOSTS, and CAPTURE_INBOUND_SECRET — the webhook secret was
#    deliberately given no default there; see that module's comment) let the
#    management command run at build time without real secrets. None of
#    these three are used for anything at build time; ALLOWED_HOSTS is
#    unused by collectstatic and CAPTURE_INBOUND_SECRET is only read at
#    request time by the inbound-email view. Nothing here reaches a request
#    path.
RUN DJANGO_SECRET_KEY=build-only DJANGO_ALLOWED_HOSTS=localhost \
    CAPTURE_INBOUND_SECRET=build-only \
    uv run --package coverage-web python coverage_web/manage.py collectstatic --noinput

EXPOSE 8000

# $PORT is provided by the host (Render/Fly). Migrations run as a separate
# release step (see render.yaml / the deploy checklist), NOT here, so a
# rollback never half-applies a migration.
CMD ["sh", "-c", "uv run --package coverage-web gunicorn coverage_web.wsgi:application --chdir coverage_web --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout 60 --access-logfile - --error-logfile -"]
