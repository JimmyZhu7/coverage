"""SocGen Quantum connector — token flow, language dedupe, location codes.
No network; payloads mirror the live API's real shape."""
from __future__ import annotations

from coverage_connectors import socgen
from coverage_connectors.models import SocGenBoard

BOARD = SocGenBoard(firm="Société Générale")


def _doc(jid, title, *, loc="GBR_A01, GBR", ct="INTERNSHIP"):
    return {
        "title": title, "sourcestr4": jid, "sourcestr7": loc, "sourcestr8": ct,
        "sourcestr10": "Corporate & Investment banking", "sourcestr15": "SG CIB",
        "url1": f"https://careers.societegenerale.com/en/job-offers/{jid}-en",
        "url3": f"https://socgen.taleo.net/careersection/sgcareers/jobapply.ftl?job={jid}",
        "sourcevarchar2": "<p>Prepare client presentations.</p>",
        "sourcedatetime2": "2026-10-01 14:00:00",
    }


def _payload(docs, total=None):
    return {"TotalCount": total if total is not None else len(docs),
            "Result": {"Docs": docs}}


def test_the_query_keeps_one_language_per_posting():
    """Every offer indexes twice (-en and -fr); without the language filter
    each job would ingest as two rows."""
    q = socgen._query(0)
    names = {f["name"]: f["value"] for f in q["query"]["advanced"]}
    assert names["documentlanguages"] == "en"
    assert names["sourcestr6"] == "job"


def test_location_codes_decode_to_countries_and_text_passes_through():
    assert socgen._location("GBR_A01, GBR") == "United Kingdom"
    assert socgen._location("Frankfurt am Main, Germany") == "Frankfurt am Main, Germany"
    # An unrecognised code is passed through, never guessed at.
    assert socgen._location("XQZ_A01, XQZ") == "XQZ_A01, XQZ"


def test_parses_docs_with_description_and_no_claimed_deadline(monkeypatch):
    monkeypatch.setattr(socgen, "_token", lambda: "tok")
    monkeypatch.setattr(socgen, "post_json",
                        lambda url, payload, **kw: _payload([_doc("25000HQJ", "Intern M&A")]))
    r = socgen.fetch(BOARD)
    assert r.ok and len(r.opportunities) == 1
    o = r.opportunities[0]
    assert o.title == "Intern M&A"
    assert o.location == "United Kingdom"
    assert o.deadline is None, "sourcedatetime2's meaning is unproven; evidence only"
    assert o.raw["detail_text"].startswith("Prepare client")
    assert o.raw["apply_url"].startswith("https://socgen.taleo.net/")


def test_pages_until_total_count(monkeypatch):
    monkeypatch.setattr(socgen, "_token", lambda: "tok")
    pages = {0: _payload([_doc(f"A{i}", f"Role {i}") for i in range(100)], total=130),
             100: _payload([_doc(f"B{i}", f"Role {100+i}") for i in range(30)], total=130)}
    def fake(url, payload, **kw):
        return pages[payload["query"]["skipFrom"]]
    monkeypatch.setattr(socgen, "post_json", fake)
    r = socgen.fetch(BOARD)
    assert r.ok and len(r.opportunities) == 130 and r.raw_count == 130


def test_a_missing_csrf_or_token_fails_the_board(monkeypatch):
    monkeypatch.setattr(socgen, "fetch_text", lambda url, **kw: "<html>no settings</html>")
    r = socgen.fetch(BOARD)
    assert r.ok is False and "csrfToken" in (r.error or "")
