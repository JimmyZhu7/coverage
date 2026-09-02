"""The throttles must not be steerable by the thing they are throttling.

`audit-security.md` finding 10: both burst guards keyed on the FIRST hop of
`X-Forwarded-For`. That hop is client-supplied text. Nothing in front of
Django rewrites it — a proxy appends, it does not replace, and locally there
is no proxy at all — so a caller could send a different value per request and
collect a fresh window every time. The guard bounded honest traffic and let
the dishonest kind through, which is the exact inversion of what it is for.

The fix is `core.clientip.client_ip`: read only the hops our own proxies
appended, and when the deployment has not declared any proxies, read none of
the header at all.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from billing.views import _waitlist_throttled
from core.clientip import UNKNOWN, client_ip
from core.views import _search_throttled

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    """Both throttles are cache-keyed, and a key surviving into the next test
    would make these pass or fail on collection order."""
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def test_a_forged_forwarded_for_is_ignored_with_no_declared_proxies(settings):
    """The default posture, and the one that covers the founder's laptop and
    every test in this suite: TRUSTED_PROXY_HOPS is 0, so the header is not
    evidence of anything and REMOTE_ADDR is the answer."""
    settings.TRUSTED_PROXY_HOPS = 0
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="9.9.9.9", REMOTE_ADDR="10.0.0.1")
    assert client_ip(request) == "10.0.0.1"


def test_one_declared_proxy_reads_the_hop_that_proxy_appended(settings):
    """Behind a single edge the real client is the RIGHTMOST entry, because
    the edge appended the peer it actually saw after whatever the client
    wrote. Anything to its left is the client's own fiction."""
    settings.TRUSTED_PROXY_HOPS = 1
    request = RequestFactory().get(
        "/",
        HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.7",
        REMOTE_ADDR="10.0.0.1",
    )
    assert client_ip(request) == "203.0.113.7"


def test_two_declared_proxies_skip_past_both(settings):
    settings.TRUSTED_PROXY_HOPS = 2
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.7, 198.51.100.4")
    assert client_ip(request) == "203.0.113.7"


def test_a_short_header_falls_back_rather_than_reading_the_client(settings):
    """Fewer entries than declared proxies means the header was stripped or
    the count is wrong. Reading the leftmost entry then is reading exactly the
    attacker-supplied value the whole change exists to stop, so it falls
    through to REMOTE_ADDR instead."""
    settings.TRUSTED_PROXY_HOPS = 2
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="9.9.9.9", REMOTE_ADDR="10.0.0.1")
    assert client_ip(request) == "10.0.0.1"


def test_no_address_at_all_shares_one_bucket(settings):
    settings.TRUSTED_PROXY_HOPS = 0
    request = RequestFactory().get("/")
    request.META.pop("REMOTE_ADDR", None)
    assert client_ip(request) == UNKNOWN


# ---------------------------------------------------------------------------
# The two throttles that read it
# ---------------------------------------------------------------------------

def _burst(throttle, count, **meta):
    """Fire `count` requests through `throttle` and return whether the last
    one was refused."""
    factory = RequestFactory()
    refused = False
    for _ in range(count):
        refused = throttle(factory.get("/", **meta))
    return refused


@pytest.mark.parametrize(
    "throttle,limit",
    [(_search_throttled, 40), (_waitlist_throttled, 5)],
    ids=["search", "waitlist"],
)
def test_a_rotating_forwarded_for_no_longer_buys_a_fresh_window(
    throttle, limit, settings
):
    """THE REGRESSION. Before the fix, a new `X-Forwarded-For` per request was
    a new cache key per request, so this loop never tripped either guard no
    matter how far past the limit it ran."""
    settings.TRUSTED_PROXY_HOPS = 0
    factory = RequestFactory()
    refused = False
    for n in range(limit + 5):
        refused = throttle(
            factory.get("/", HTTP_X_FORWARDED_FOR=f"9.9.9.{n}",
                        REMOTE_ADDR="10.0.0.1")
        )
    assert refused, (
        "a caller varying X-Forwarded-For was never throttled: the key is "
        "still being taken from a header the caller writes"
    )


@pytest.mark.parametrize(
    "throttle", [_search_throttled, _waitlist_throttled],
    ids=["search", "waitlist"],
)
def test_the_key_is_the_same_with_and_without_a_forged_header(throttle, settings):
    """The acceptance criterion as written: one connection, one bucket. The
    forged request lands in the same window the unforged one opened, so the
    two together trip a guard that either alone would not."""
    settings.TRUSTED_PROXY_HOPS = 0
    limit = 5 if throttle is _waitlist_throttled else 40
    half = limit // 2 + 1

    _burst(throttle, half, REMOTE_ADDR="10.0.0.1")
    refused = _burst(
        throttle, half, HTTP_X_FORWARDED_FOR="9.9.9.9", REMOTE_ADDR="10.0.0.1")
    assert refused


@pytest.mark.parametrize(
    "throttle", [_search_throttled, _waitlist_throttled],
    ids=["search", "waitlist"],
)
def test_two_real_addresses_still_get_their_own_windows(throttle, settings):
    """The other half, and the reason this is not just "throttle everyone
    together": a shared campus NAT is one address, but two students on two
    networks must not spend each other's budget."""
    settings.TRUSTED_PROXY_HOPS = 0
    limit = 5 if throttle is _waitlist_throttled else 40

    assert _burst(throttle, limit + 1, REMOTE_ADDR="10.0.0.1")
    assert not _burst(throttle, 1, REMOTE_ADDR="10.0.0.2")
