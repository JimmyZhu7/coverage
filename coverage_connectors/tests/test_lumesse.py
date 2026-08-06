"""Lumesse FO-REST connector — auth headers, deadline timezone, pagination.
No network; every payload here mirrors the live BOCI board's real shape."""
from __future__ import annotations

from coverage_connectors import lumesse
from coverage_connectors.models import LumesseBoard

BOARD = LumesseBoard(firm="BOCI", host="au01-foc.lumessetalentlink.com",
                     tech_id="Q7WFK026203F3VBQBLOV7F624")


def _job(jid, title, *, end_ms=1806533999000, tz="Europe/London"):
    return {
        "customFields": [
            {"title": "The Role", "content": "<p>Support the desk.</p>"},
            {"title": "Skills", "content": "<p>Cantonese a plus.</p>"},
        ],
        "jobFields": {
            "id": jid, "jobNumber": f"BOCI{jid}", "jobTitle": title,
            "SLOVLIST3": "Operations",
            "DPOSTINGEND": end_ms, "POSTINGTIMEZONE": tz,
            "applicationUrl": f"https://au01-apply.lumessetalentlink.com/apply-app/pages/application-form?jobId=X-{jid}&langCode=en_GB",
        },
    }


def _payload(jobs, total=None):
    return {"globals": {"jobsCount": total if total is not None else len(jobs)},
            "jobs": jobs}


def test_guest_auth_is_two_literal_headers():
    """The widget sends `username`/`password` HEADERS; HTTP Basic built from
    the same strings gets a 403. This is the fact that kept the board on the
    deferred list for two weeks, so it is pinned."""
    h = lumesse._guest_headers(BOARD)
    assert h["username"] == "Q7WFK026203F3VBQBLOV7F624:guest:FO"
    assert h["password"] == "guest"
    assert "Authorization" not in h


def test_parses_jobs_with_deadline_and_description(monkeypatch):
    monkeypatch.setattr(lumesse, "fetch_json",
                        lambda url, **kw: _payload([_job(1, "Graduate Analyst")]))
    r = lumesse.fetch(BOARD)
    assert r.ok is True and len(r.opportunities) == 1
    o = r.opportunities[0]
    assert o.title == "Graduate Analyst"
    assert o.url.startswith("https://au01-apply.lumessetalentlink.com/")
    assert o.raw["detail_text"].startswith("The Role: Support the desk.")
    assert o.raw["detail_source"] == "payload"


def test_deadline_converts_in_the_boards_own_timezone():
    """1806533999000 is 23:59:59 on 31 March 2027 in Europe/London — and
    already 1 April in UTC+8. Converting in UTC would report every closing
    date a day late for exactly the market this board serves."""
    fields = {"DPOSTINGEND": 1806533999000, "POSTINGTIMEZONE": "Europe/London"}
    assert lumesse._deadline(fields) == "2027-03-31"
    fields_hk = {"DPOSTINGEND": 1806533999000, "POSTINGTIMEZONE": "Asia/Hong_Kong"}
    assert lumesse._deadline(fields_hk) == "2027-04-01"


def test_no_posting_end_means_no_deadline():
    assert lumesse._deadline({}) is None
    assert lumesse._deadline({"DPOSTINGEND": 0}) is None


def test_pages_until_jobs_count(monkeypatch):
    pages = {0: _payload([_job(i, f"Role {i}") for i in range(50)], total=60),
             50: _payload([_job(50 + i, f"Role {50+i}") for i in range(10)], total=60)}
    def fake(url, **kw):
        import re
        first = int(re.search(r"firstResult=(\d+)", url).group(1))
        return pages[first]
    monkeypatch.setattr(lumesse, "fetch_json", fake)
    r = lumesse.fetch(BOARD)
    assert r.ok is True
    assert len(r.opportunities) == 60
    assert r.raw_count == 60


def test_fetch_failure_is_board_level(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("timeout")
    monkeypatch.setattr(lumesse, "fetch_json", boom)
    r = lumesse.fetch(BOARD)
    assert r.ok is False and "timeout" in (r.error or "")
