"""Avature RSS connector — parsing + pagination-stop, no network."""
from __future__ import annotations

from coverage_connectors import avature
from coverage_connectors.models import AvatureBoard

BOARD = AvatureBoard(firm="Bain", feed_url="https://careers.bain.com/jobs/SearchJobs/feed/")

_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title><![CDATA[Associate - Financial Services]]></title>
<link>https://careers.bain.com/jobs/FolderDetail/Associate/107310</link></item>
<item><title>Project Leader ENR</title>
<link>https://careers.bain.com/jobs/FolderDetail/Project-Leader/90399</link></item>
</channel></rss>"""


def test_parses_items_with_real_urls(monkeypatch):
    monkeypatch.setattr(avature, "fetch_text", lambda url, **kw: _FEED)
    r = avature.fetch(BOARD)
    assert r.ok is True
    assert len(r.opportunities) == 2
    assert r.opportunities[0].title == "Associate - Financial Services"
    assert r.opportunities[0].url.startswith("https://careers.bain.com/jobs/")
    assert r.opportunities[0].location == ""  # feed carries none; never guessed


def test_repeated_page_stops_pagination(monkeypatch):
    # Avature ignores folderOffset, so every page repeats — dedup must halt it.
    monkeypatch.setattr(avature, "fetch_text", lambda url, **kw: _FEED)
    r = avature.fetch(BOARD)
    assert len(r.opportunities) == 2  # not an infinite duplicate pile


def test_fetch_failure_is_board_level(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("timeout")
    monkeypatch.setattr(avature, "fetch_text", boom)
    r = avature.fetch(BOARD)
    assert r.ok is False and "timeout" in (r.error or "")


def test_classify_url_still_matches_real_avature_urls():
    assert avature.classify_url("https://careers.bain.com/jobs/FolderDetail/x/1") is not None


def test_classify_url_does_not_steal_icims_urls():
    """CONFIRMED DEFECT: a bare '/jobs/' substring test matched every icims
    posting URL too (icims job URLs are
    'https://{tenant}.icims.com/jobs/<id>/<slug>/job'). Because avature is
    dispatched before icims in CONNECTORS, this made avature.verify()
    always answer first with a false 'needs-verification' and icims's own
    real liveness check (404/410 on closed) never ran."""
    icims_url = "https://careers-sig.icims.com/jobs/11191/discovery-program/job"
    assert avature.classify_url(icims_url) is None


def test_verify_defers_to_icims_through_the_public_dispatcher(monkeypatch):
    """End-to-end: coverage_connectors.verify() must resolve an icims URL
    to the icims provider, not avature's blanket 'needs-verification'. No
    live network: icims's own verify() does a real page fetch once it's
    reached, so that one call is stubbed."""
    from coverage_connectors import icims as icims_mod
    from coverage_connectors import verify as public_verify
    monkeypatch.setattr(icims_mod, "fetch_text", lambda url, **kw: "<title>Role</title>")
    icims_url = "https://careers-sig.icims.com/jobs/11191/discovery-program/job"
    v = public_verify(icims_url)
    assert v.provider == "icims"
