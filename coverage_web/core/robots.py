"""robots.txt, honoured — one cached parser per host.

WHY THIS EXISTS. Two management commands go out to firms' own websites:
`enrich_postings` (fetches the detail page behind a posting) and
`fetch_firm_logos` (fetches a homepage and its declared icons). Both used to
send a Chrome user-agent string and neither ever asked for robots.txt — a
`grep robotparser` over the whole repo returned nothing on 2026-09-01. The
package that does the bulk fetching, `coverage_connectors/http.py`, has
always identified itself honestly:

    coverage-connectors/0.1 (+https://coverage.app; deterministic ATS/board fetcher)

so the spoofing was two commands out of step with the convention, not a
policy. Both now follow it, and both ask here first.

WHAT "HONOURING" MEANS HERE. This is a politeness check, not a security
boundary: it decides whether Coverage sends a request at all. A disallowed
URL is SKIPPED AND LOGGED, never silently dropped — a firm whose robots.txt
walls its careers pages is a standing fact the operator should be able to
read off a run's output, exactly like the bot-challenge detection in
`coverage_connectors/http.py`.

FAILURE MEANS ALLOW. A host with no robots.txt (404), or one that times out,
is not saying no — the standard is explicit that absence permits, and
treating a flaky fetch as a prohibition would silently empty a run. Only an
actual `Disallow` match, or a robots.txt served as 401/403 (the host
refusing to tell us the rules at all), blocks a fetch.

ONE PARSER PER HOST, CACHED FOR THE PROCESS. Both commands walk many URLs
per host in one run; re-fetching robots.txt per URL would cost more requests
than the work itself. The cache lives for the life of the process, which is
one management-command run — long enough to matter, short enough that a
changed robots.txt is picked up on the next cron tick.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

# Short: robots.txt is a precondition for work, not the work. A host slow
# enough to blow this is a host whose pages we are about to give up on
# anyway, and the failure mode here is "allow", so a timeout costs a few
# seconds and nothing else.
TIMEOUT_SECONDS = 5

# host key -> parser, or None when that host's rules could not be read and
# everything is therefore allowed. `None` is cached as deliberately as a
# parser is: one failed robots.txt must not mean one failed fetch per URL.
_CACHE: dict[str, RobotFileParser | None] = {}


def reset_cache() -> None:
    """Drop every cached parser. For tests, and for a long-lived process
    that wants to re-read the rules."""
    _CACHE.clear()


def _robots_url(url: str) -> tuple[str, str] | None:
    """(cache key, robots.txt URL) for `url`, or None if it has no host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    key = f"{parts.scheme}://{parts.netloc}"
    return key, urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def _fetch_parser(robots_url: str, user_agent: str) -> RobotFileParser | None:
    """Read and parse one host's robots.txt. None when it could not be read.

    Fetched by hand rather than through `RobotFileParser.read()` for two
    reasons: `read()` takes no timeout (an unresponsive host would hang a
    whole run), and it sends Python's default user-agent rather than ours,
    which would make the one request Coverage sends about identity the one
    request that lies about it.
    """
    parser = RobotFileParser()
    parser.set_url(robots_url)
    req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # The host is refusing to tell us its rules. RFC 9309 reads that
            # as "everything is disallowed", and so do we.
            parser.disallow_all = True
            return parser
        return None
    except Exception as exc:  # noqa: BLE001 — network, DNS, timeout, bad TLS
        logger.info("robots.txt unreadable at %s (%s) — allowing", robots_url, exc)
        return None
    parser.parse(raw.decode("utf-8", errors="replace").splitlines())
    return parser


def is_allowed(url: str, user_agent: str) -> bool:
    """True when `user_agent` may fetch `url` under that host's robots.txt.

    `user_agent` is the full header string the caller will send; the parser
    matches robots.txt `User-agent:` lines against its leading token, which
    is why every UA in this project starts with a bare product name.
    """
    target = _robots_url(url)
    if target is None:
        return True
    key, robots_url = target
    if key not in _CACHE:
        _CACHE[key] = _fetch_parser(robots_url, user_agent)
    parser = _CACHE[key]
    if parser is None:
        return True
    return parser.can_fetch(user_agent, url)
