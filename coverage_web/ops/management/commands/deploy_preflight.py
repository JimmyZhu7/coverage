"""deploy_preflight — "would this deploy actually work", answered against
the environment it is run in.

    python manage.py deploy_preflight
    python manage.py deploy_preflight --warn-only   # never exit non-zero

`manage.py check --deploy` already tells you whether Django's own security
settings are on. It cannot tell you the six things that actually broke a
first Blueprint apply of THIS app, because every one of them is about
whether a value exists rather than what it is set to: an unset
DJANGO_ALLOWED_HOSTS (the service never boots), a /healthz that 301s under
the SSL redirect (the health check never goes green), a Gmail Live worker
with no credentials (Render restarts it in a loop), crons that start before
the first migrate (tracebacks on missing tables), and SITE_URL/STRIPE_*
absent from render.yaml entirely (digest links pointing at localhost, no way
to pay). This command is that list, as checks.

IT NEVER PRINTS A VALUE. Every line names a KEY and a verdict — set, blank,
placeholder — and nothing else. This is meant to be run in a deploy shell
and pasted into a chat window, and a preflight that leaks the secret it was
checking is worse than no preflight. That rule is tested
(ops/tests/test_deploy_preflight.py).

IT NEVER INVENTS ONE EITHER. A key holding `changeme` is reported as a
placeholder, not quietly treated as configured and not silently replaced
with something plausible. "Not set" and "set to the example value" are
different states and this command says which.

VERDICTS
  PASS  nothing to do.
  WARN  a feature is dark, and that is a choice with a consequence named on
        the line. A deploy full of WARNs is a valid deploy: this project's
        whole posture is that every optional integration no-ops rather than
        crashing (see settings/base.py). Never exits non-zero.
  FAIL  this deploy will not work. Exits non-zero unless --warn-only.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

#: Substrings that mark a value as an untouched example rather than a real
#: credential. Matched case-insensitively against the value, which is read
#: but never printed. Deliberately conservative — a false "placeholder" on a
#: real key is a confusing line, and a missed one is caught by the feature
#: being dark anyway.
_PLACEHOLDER_MARKERS = (
    "changeme", "change-me", "change_me", "placeholder", "your-", "your_",
    "yourdomain", "example.com", "replace-me", "replaceme", "insecure-dev-only",
    "xxxxx", "todo",
)


def _looks_like_placeholder(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class Check:
    """One line of output: a verdict, the key(s) it is about, and what the
    verdict means for the deploy. `keys` is a tuple of NAMES; there is
    nowhere in this class to put a value, on purpose."""

    __slots__ = ("level", "keys", "message")

    def __init__(self, level: str, keys, message: str):
        self.level = level
        self.keys = (keys,) if isinstance(keys, str) else tuple(keys)
        self.message = message

    def render(self) -> str:
        return f"{self.level}  {', '.join(self.keys):<42} {self.message}"


class Command(BaseCommand):
    help = "Check this environment for the things that break a first deploy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--warn-only", action="store_true",
            help="Print the report and exit 0 even when a check FAILs.",
        )

    def handle(self, *args, **opts):
        checks: list[Check] = []
        checks.append(self._settings_module())
        checks.append(self._secret_key())
        checks.append(self._allowed_hosts())
        checks.append(self._csrf_origins())
        checks.append(self._healthz_exempt())
        checks.extend(self._database())
        checks.append(self._redis())
        checks.append(self._site_url())
        checks.append(self._email())
        checks.append(self._google_login())
        checks.append(self._gmail_live())
        checks.extend(self._stripe())
        checks.append(self._vapid())
        checks.append(self._sentry())
        checks.append(self._anthropic())

        for check in checks:
            style = {
                PASS: self.style.SUCCESS,
                WARN: self.style.WARNING,
                FAIL: self.style.ERROR,
            }[check.level]
            self.stdout.write(style(check.render()))

        failed = [c for c in checks if c.level == FAIL]
        warned = [c for c in checks if c.level == WARN]
        self.stdout.write(
            f"\n{len(checks) - len(failed) - len(warned)} pass · "
            f"{len(warned)} warn · {len(failed)} fail"
        )
        if failed and not opts["warn_only"]:
            raise CommandError(
                f"{len(failed)} check(s) would break this deploy: "
                + ", ".join(k for c in failed for k in c.keys)
            )

    # -- individual checks -------------------------------------------------

    def _settings_module(self) -> Check:
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "") or settings.SETTINGS_MODULE
        if module.endswith(".production"):
            return Check(PASS, "DJANGO_SETTINGS_MODULE", "production settings.")
        return Check(
            WARN, "DJANGO_SETTINGS_MODULE",
            f"not production ({module}) — this report describes THAT "
            "environment, not the deployed one.",
        )

    def _secret_key(self) -> Check:
        key = getattr(settings, "SECRET_KEY", "") or ""
        if not key:
            return Check(FAIL, "DJANGO_SECRET_KEY", "blank — the app will not boot.")
        if _looks_like_placeholder(key):
            return Check(
                FAIL, "DJANGO_SECRET_KEY",
                "still the insecure dev key from settings/base.py. Render "
                "generates a real one (generateValue: true in render.yaml).",
            )
        return Check(PASS, "DJANGO_SECRET_KEY", "set.")

    def _allowed_hosts(self) -> Check:
        hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        if not hosts:
            return Check(
                FAIL, "DJANGO_ALLOWED_HOSTS",
                "empty — every request 400s and production settings refuse to "
                "import. Set it, or run on a host that provides "
                "RENDER_EXTERNAL_HOSTNAME.",
            )
        if os.environ.get("DJANGO_ALLOWED_HOSTS", "").strip():
            source = "from DJANGO_ALLOWED_HOSTS"
        elif os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip():
            source = "from the platform's RENDER_EXTERNAL_HOSTNAME"
        else:
            source = "from this settings module"
        return Check(PASS, "DJANGO_ALLOWED_HOSTS", f"{len(hosts)} host(s), {source}.")

    def _csrf_origins(self) -> Check:
        origins = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or [])
        if origins:
            return Check(PASS, "DJANGO_CSRF_TRUSTED_ORIGINS", f"{len(origins)} origin(s).")
        return Check(
            WARN, "DJANGO_CSRF_TRUSTED_ORIGINS",
            "empty — same-origin POSTs still pass behind the proxy header, "
            "but a custom domain will need this before its forms work.",
        )

    def _healthz_exempt(self) -> Check:
        """Render marks the service live on a 200 from /healthz. Behind
        SECURE_SSL_REDIRECT, a probe that does not send
        X-Forwarded-Proto: https gets a 301 instead and the deploy never
        goes green — the second thing on the audit's first-deploy list."""
        keys = ("SECURE_SSL_REDIRECT", "SECURE_REDIRECT_EXEMPT")
        if not getattr(settings, "SECURE_SSL_REDIRECT", False):
            return Check(PASS, keys, "SSL redirect off; /healthz cannot 301.")
        exempt = [str(p) for p in getattr(settings, "SECURE_REDIRECT_EXEMPT", []) or []]
        if any("healthz" in pattern for pattern in exempt):
            return Check(PASS, keys, "/healthz is exempt from the SSL redirect.")
        return Check(
            FAIL, keys,
            "/healthz is NOT exempt from the SSL redirect — the health check "
            "may 301 and the service may never go green.",
        )

    def _database(self) -> list[Check]:
        """Reachability first, then "has migrate run" — the two crons and
        the worker share this image with no pre-deploy step of their own, so
        a first apply can start them before the web service's
        preDeployCommand lands (docs/deploy.md §1)."""
        keys = ("DATABASE_URL",)
        connection = connections[DEFAULT_DB_ALIAS]
        try:
            connection.ensure_connection()
        except Exception as exc:  # noqa: BLE001 — the message is the check.
            return [Check(FAIL, keys, f"unreachable: {type(exc).__name__}.")]

        reachable = Check(PASS, keys, "reachable.")
        try:
            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            plan = executor.migration_plan(targets)
        except Exception as exc:  # noqa: BLE001
            return [reachable, Check(FAIL, "migrations", f"could not be read: {exc}.")]

        if plan:
            return [reachable, Check(
                FAIL, "migrations",
                f"{len(plan)} unapplied — run `manage.py migrate` BEFORE any "
                "cron or worker starts, or they traceback on missing tables.",
            )]
        return [reachable, Check(PASS, "migrations", "all applied.")]

    def _redis(self) -> Check:
        if (os.environ.get("REDIS_URL", "") or "").strip():
            return Check(PASS, "REDIS_URL", "set; rate-limit counters are shared.")
        return Check(
            WARN, "REDIS_URL",
            "blank — the cache falls back to per-process memory, so allauth's "
            "login limits count once PER gunicorn worker and reset on every "
            "deploy. render.yaml wires this from the coverage-kv service.",
        )

    def _site_url(self) -> Check:
        url = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
        if not url:
            return Check(WARN, "SITE_URL", "blank — links built outside a request have no host.")
        if "localhost" in url or "127.0.0.1" in url:
            return Check(
                WARN, "SITE_URL",
                "still the local default — every digest and trial-ended email "
                "link would point at the reader's own machine.",
            )
        return Check(PASS, "SITE_URL", "set to a real origin.")

    def _email(self) -> Check:
        from accounts import trials as pro_trials

        if pro_trials.email_is_configured():
            return Check(PASS, ("EMAIL_URL", "DEFAULT_FROM_EMAIL"), "mail is sent.")
        return Check(
            WARN, ("EMAIL_URL", "DEFAULT_FROM_EMAIL"),
            "blank — password resets and the weekly digest print to the "
            "service logs, nobody can self-serve a reset, and the trial-ended "
            "notice is the Settings banner alone.",
        )

    def _google_login(self) -> Check:
        keys = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")
        values = [os.environ.get(k, "") for k in keys]
        if any(_looks_like_placeholder(v) for v in values):
            return Check(
                WARN, keys,
                "placeholder value(s) — treated as unset, so no Google button "
                "renders. Email/password sign-in still works.",
            )
        if all(v.strip() for v in values):
            return Check(PASS, keys, "set; Google sign-in renders.")
        return Check(
            WARN, keys,
            "blank — the Google sign-in button is not rendered at all; "
            "email/password only.",
        )

    def _gmail_live(self) -> Check:
        from capture import gmail_live

        keys = ("GMAIL_LIVE_CLIENT_ID", "GMAIL_LIVE_CLIENT_SECRET", "GMAIL_LIVE_TOKEN_KEY")
        if any(_looks_like_placeholder(os.environ.get(k, "")) for k in keys):
            return Check(WARN, keys, "placeholder value(s) — treated as unset.")
        if gmail_live.is_configured():
            return Check(
                PASS, keys,
                "set. Paste the same values onto coverage-gmail-live, "
                "coverage-gmail-watch-renew and coverage-gmail-backfill too.",
            )
        return Check(
            WARN, keys,
            "blank — no Connect button, and the gmail-live worker idles "
            "instead of polling (it will not crash-loop).",
        )

    def _stripe(self) -> list[Check]:
        from billing import stripe_gateway

        keys = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")
        if not stripe_gateway.is_configured():
            return [Check(
                WARN, keys,
                "blank — credit top-ups are off and the webhook 400s cleanly. "
                "There is no way to pay for anything on this deploy.",
            )]
        out = [Check(PASS, keys, "set; credit top-ups are live.")]
        secret = (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
        if not secret.startswith("rk_"):
            out.append(Check(
                WARN, "STRIPE_SECRET_KEY",
                "not a restricted key (rk_). Use one scoped to Checkout "
                "Sessions write and nothing else.",
            ))
        return out

    def _vapid(self) -> Check:
        from accounts import push

        keys = ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_CLAIM_EMAIL")
        if push.is_configured():
            return Check(PASS, keys, "set; deadline push alerts can send.")
        return Check(
            WARN, keys,
            "blank — the Settings push toggle is unavailable and the "
            "push-alerts cron no-ops. `manage.py generate_vapid_keys` prints "
            "a pair; no third-party service and no cost.",
        )

    def _sentry(self) -> Check:
        if (os.environ.get("SENTRY_DSN", "") or "").strip():
            return Check(PASS, "SENTRY_DSN", "set; unhandled exceptions are reported.")
        return Check(
            WARN, "SENTRY_DSN",
            "blank — no error monitoring. Tracebacks still reach stderr "
            "(settings/base.py's LOGGING block).",
        )

    def _anthropic(self) -> Check:
        if (getattr(settings, "ANTHROPIC_API_KEY", "") or "").strip():
            return Check(PASS, "ANTHROPIC_API_KEY", "set; AI surfaces are live and billed.")
        return Check(
            WARN, "ANTHROPIC_API_KEY",
            "blank — the advisor, autopilot and AI extraction all no-op.",
        )
