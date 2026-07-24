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
