"""The Anthropic SDK client for the advisor page, and its dark-by-default gate.

WHY THE SDK HERE AND NOT `directory.ai_extract`
------------------------------------------------
`ai_extract` is a hand-rolled urllib client, and for what it does — one
single-shot completion, one narrow question, no conversation — that is the
right amount of machinery. This feature is a MULTI-TURN TOOL LOOP: assistant
turns carrying `tool_use` blocks, user turns carrying `tool_result` blocks
paired by id, a system prompt with a `cache_control` breakpoint, strict tool
schemas. Re-implementing that request/response shape by hand is exactly the
kind of thing a maintained SDK exists to get right, and getting the pairing
subtly wrong is a 400 the student sees. So this module uses `anthropic`, and
`ai_extract` is left completely alone.

CONFIGURATION — same posture as every other optional integration in this app
(EMAIL_URL, the OAuth providers, VAPID): `ANTHROPIC_API_KEY` is blank by
default, `is_configured()` is then False, the whole feature goes dark, the
app boots, and every test passes with nothing set. Callers must never treat
"not configured" as an error.

TIMEOUT is set on the client, not left to the SDK default (10 minutes). Every
call here happens inside a synchronous htmx POST behind gunicorn's 60s worker
timeout, so a hung request has to fail well inside that or the student gets a
dead page instead of a message.
"""

from __future__ import annotations

from django.conf import settings

# Deliberately under gunicorn's 60s worker timeout, with room left for the
# tool round-trips and the template render on either side of the call.
REQUEST_TIMEOUT_SECONDS = 45.0


def is_configured() -> bool:
    return bool(getattr(settings, "ANTHROPIC_API_KEY", "") or "")


def get_client():
    """A configured `anthropic.Anthropic`. Imported lazily so the package is
    only touched when a real call is about to be made — the app must boot,
    and the whole test suite must run, with the key unset.

    `trust_env=False` plus an explicit `proxy=`, rather than letting httpx
    scan the environment itself: httpx's own env-var resolution picks
    `ALL_PROXY` for an `https://` request unless `HTTPS_PROXY` is set with
    exactly matching case, and on this dev machine that variable holds a
    `socks5://` URL — turning every call into an `ImportError` (httpx wants
    the `socks` extra for that) before a single byte left the machine. The
    plain `http://` proxy in `HTTP_PROXY`/`HTTPS_PROXY` is what actually
    reaches Anthropic (confirmed with curl: direct connection to
    api.anthropic.com is network-blocked from here — a 403 with no
    Anthropic-shaped body — and only works when routed through that proxy).
    So: read the http(s) proxy explicitly, ignore `ALL_PROXY`/SOCKS
    entirely, and disable httpx's own scan so it can't silently fall back to
    the thing that crashes. Where no proxy env var is set (production),
    `proxy` is `None` and this is a plain direct client, identical to
    passing nothing."""
    import os

    import anthropic
    import httpx

    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )

    return anthropic.Anthropic(
        api_key=getattr(settings, "ANTHROPIC_API_KEY", ""),
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=1,
        http_client=httpx.Client(
            proxy=proxy, trust_env=False, timeout=REQUEST_TIMEOUT_SECONDS
        ),
    )
