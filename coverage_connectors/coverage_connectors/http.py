"""Shared HTTP layer: timeouts, retries, user-agent, error handling.

Ported from the original `sources.py`'s `_fetch()` / `_fetch_workday_page()`
(15s `urllib.request` calls with a fixed `User-Agent`) and `verify_rows.py`'s
`_fetch_text()` / `_fetch_json()`. Two differences from the original,
both deliberate:

1. **Retries.** The original had none — a single failed `urlopen()` call
   propagated straight to the caller as a board-level error. This package
   adds a small bounded retry (2 attempts, short backoff) for transient
   failures, since a shared multi-tenant fetcher hitting the same handful
   of boards on a cron schedule should not report a board "down" over one
   dropped connection. A `404` is never retried — it is retried-uselessly
   by definition (Greenhouse/Workday both use it as a definitive "this
   specific thing is gone" signal, and the verify layer depends on seeing
   it promptly).
2. **User-Agent.** Renamed from "recruiting-radar/1.0 (personal recruiting
   tracker)" to identify this package instead, since it may run against the
   same boards under different operational ownership.

No board-count or per-board learnings/instrumentation lives here — that
tracking (`learnings.record_fetch`) was a single-user file-backed side-store
and is exactly the kind of state this extraction is told to drop. Callers
who want fetch telemetry should wrap `fetch_json`/`post_json` themselves;
this module raises cleanly and lets the caller decide what to log where.

3. **Trust store.** Every request verifies against `certifi`'s CA bundle
   rather than whatever the interpreter's OpenSSL happens to point at.
   Measured 2026-09-01 on the scraper host: `ssl.get_default_verify_paths()`
   resolves to macOS's `/etc/ssl/cert.pem`, which does not carry Sectigo's
   "Public Server Authentication Root R46". EY (`careers.ey.com`) and HSBC
   (`apply.careers.hsbc.com`) both chain to it, so both boards had returned
   `CERTIFICATE_VERIFY_FAILED … unable to get local issuer certificate` in
   every run for two weeks — 175 open rows frozen behind a stale bundle on
   OUR machine, not a problem with either site. A direct handshake against
   both hosts confirmed the split: system bundle fails, certifi succeeds
   (issuers "Sectigo Public Server Authentication CA OV R40" and "… R36").
   `successfactors.py`'s docstring had already diagnosed this and prescribed
   `SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())')` — an
   env var nobody exports before a cron run. Reading certifi here makes the
   fix the default instead of a thing to remember. Verification is never
   disabled and never relaxed; the only change is WHICH roots are trusted,
   and certifi is the strictly larger, actively-maintained set.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "coverage-connectors/0.1 (+https://coverage.app; deterministic ATS/board fetcher)"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5


def _build_ssl_context() -> ssl.SSLContext:
    """A verifying context backed by certifi's roots (see module docstring).

    Falls back to the interpreter's own default context if certifi is
    somehow absent — that is the behaviour this package had before, so a
    stripped install degrades to the old bug rather than to no TLS at all.
    Hostname checking and certificate verification stay on in both branches;
    there is no code path here that turns either off.
    """
    try:
        import certifi
    except ImportError:  # pragma: no cover — certifi is a declared dependency
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


#: Built once at import: loading a ~300KB PEM bundle per request would cost
#: more than the fetch it protects, and the roots do not change mid-process.
SSL_CONTEXT = _build_ssl_context()


class FetchError(Exception):
    """Raised after exhausting retries on a transient failure. Wraps the
    last underlying exception so callers can still inspect `cause` (e.g. an
    `HTTPError` with a `.code`) without catching a bare `Exception`."""

    def __init__(self, url: str, method: str, cause: BaseException):
        self.url = url
        self.method = method
        self.cause = cause
        super().__init__(f"{method} {url}: {cause}")


def _do_request(
    url: str,
    *,
    data: bytes | None,
    headers: dict[str, str] | None,
    timeout: float,
) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    with urllib.request.urlopen(  # noqa: S310 — deliberate plain HTTP client
        req, timeout=timeout, context=SSL_CONTEXT
    ) as resp:
        return resp.read()


def fetch_bytes(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    method_label: str = "GET",
) -> bytes:
    """GET (or POST, if `data` is given) `url`, retrying transient failures
    up to `retries` times. A `404` is raised immediately, unretried — see
    module docstring."""
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return _do_request(url, data=data, headers=headers, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 410 joins 404 as retried-uselessly-by-definition: iCIMS answers
            # Gone for filled/expired reqs, and the verify layer reads both as
            # a definitive "this posting no longer exists".
            if e.code in (404, 410):
                raise
            last_exc = e
        except Exception as e:  # noqa: BLE001 — network/timeout/etc, retry-eligible
            last_exc = e
        if attempt < retries:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last_exc is not None
    raise FetchError(url, method_label, last_exc) from last_exc


def fetch_text(url: str, **kwargs: Any) -> str:
    return fetch_bytes(url, **kwargs).decode("utf-8", errors="replace")


# An ATS tenant can switch on an interactive bot check (Oleeo Protect/Altcha,
# Cloudflare) at any time. Those serve HTTP 200 with a challenge page, so a
# connector that only trusts the status code parses zero rows and reports
# success — which downstream reads as "the board's markup changed" and sends
# someone to fix a parser that is fine. Detection only: nothing in this
# package attempts to solve a challenge.
#
# Connectors that hit a challenge build their error message on this prefix,
# and health reporting matches on it to tell "the operator walled the board"
# (a standing condition, nothing to fix) apart from "the fetch is failing"
# (a config or network bug). Keep the two in lockstep via the constant.
BOT_BLOCK_PREFIX = "blocked by bot protection"
_CHALLENGE_MARKERS = (
    ("oleeoprotect", "Oleeo Protect"),
    ("altcha-widget", "Altcha widget"),
    ("cf-browser-verification", "Cloudflare browser check"),
    ("cf_chl_opt", "Cloudflare challenge"),
    ("checking your browser before accessing", "browser check interstitial"),
    ("enable javascript and cookies to continue", "JS/cookie interstitial"),
)
_CHALLENGE_TITLES = {
    "quick check needed": "Oleeo Protect",
    "just a moment...": "Cloudflare browser check",
    "attention required!": "Cloudflare block page",
}
_TITLE_TAG_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)


# ---------------------------------------------------------------- zero rows
#
# "Unreadable, not empty" is the second half of the same contract
# `bot_challenge_reason` states above, and it is the one that had only one
# implementation. Fifteen of eighteen connectors returned `ok=True` with zero
# opportunities whenever a 200 came back and their parser found nothing —
# which downstream means "this firm has no openings", the single most
# expensive lie this package can tell: `ingest` reads a clean zero as
# permission to close the firm's entire open set, and `health` reads it as a
# market fact worth a quiet line rather than a bug.
#
# Every connector now answers one question before returning a clean zero:
# does this response carry POSITIVE evidence that the board is empty, or
# merely an absence of rows we know how to read? The three outcomes:
#
#   ok=False + UNREADABLE_SUFFIX   the response is the wrong shape (a key the
#                                  platform always sends is missing, a stated
#                                  total disagrees with the rows, a login stub
#                                  came back instead of a feed). Nothing may
#                                  be concluded from it.
#   ok=True, empty_state=True      the board said, in its own words, that it
#                                  has nothing: `{"jobs": []}` under a
#                                  `total: 0`, an "no jobs found" panel, a
#                                  sitemap with no matching paths. A real
#                                  market fact; auto-close may proceed.
#   ok=True, empty_state=False     parsed cleanly, no rows, and the platform
#                                  offered no empty-state signal either way.
#                                  Greenhouse's vacated token lives here and
#                                  is indistinguishable on the wire from a
#                                  quiet board — so the CALLER, which is the
#                                  only party that knows how many rows this
#                                  board has banked before, decides (see
#                                  `zero_rows_guard`).
UNREADABLE_SUFFIX = "board unreadable, not empty"


def unreadable(reason: str) -> str:
    """The standard board-level error text for a response that cannot be
    read. One phrasing across every connector, so health reporting and a
    human reading a log both learn it once."""
    return f"{reason} — {UNREADABLE_SUFFIX}"


def bot_challenge_reason(html: str) -> str | None:
    """Name the vendor when `html` is a bot-check interstitial rather than
    real content, else None. Callers should turn a hit into an explicit
    board-level failure — a challenge page is unreadable, not empty."""
    if not html:
        return None
    lowered = html.lower()
    title_m = _TITLE_TAG_RE.search(lowered)
    if title_m:
        vendor = _CHALLENGE_TITLES.get(title_m.group(1).strip())
        if vendor:
            return vendor
    for marker, vendor in _CHALLENGE_MARKERS:
        if marker in lowered:
            return vendor
    return None


def fetch_json(url: str, **kwargs: Any) -> Any:
    """Parse a JSON response, requiring a dict or list at the top level.

    WAFs and error pages sometimes return valid-but-useless JSON scalars
    (e.g. a bare "Access Denied" string). Every connector immediately calls
    `.get(...)`/iterates on the result, so a scalar here previously escaped
    the connector's own try/except as an AttributeError and aborted the whole
    multi-board run. Raising ValueError instead keeps the failure inside the
    per-board `ok=False` path.
    """
    data = json.loads(fetch_text(url, **kwargs))
    if not isinstance(data, (dict, list)):
        raise ValueError(
            f"expected JSON object/array, got {type(data).__name__}: {str(data)[:80]!r}"
        )
    return data


def post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
    body = json.dumps(payload).encode()
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    return fetch_json(url, data=body, headers=req_headers, method_label="POST", **kwargs)
