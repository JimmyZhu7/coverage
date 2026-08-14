"""The Cycle Dates timeline on a firm page: what it may claim, and how.

`FirmDate.source_url` is a URLField that nothing validates, so 26 of the 39
live rows hold a provenance token ("seed:historical-pattern", "seed:demo")
rather than a citation. The template's only gate was truthiness, so every
token became an `<a>` styled exactly like a working link — on /firms/gs/,
four identical SOURCE pills of which two went nowhere.

These assert against RENDERED HTML, because the bug lives in the seam between
the view dict and the template, which a helper-only test walks straight past.
"""

from __future__ import annotations

import datetime as dt

import pytest

from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db


def _firm(slug="gs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _date(firm, **kw):
    kw.setdefault("cycle", "sa2028_ib")
    kw.setdefault("region", "us")
    kw.setdefault("event_kind", "app_open")
    kw.setdefault("date", dt.date(2027, 3, 1))
    kw.setdefault("precision", "estimated")
    kw.setdefault("confidence", 0.6)
    return FirmDate.objects.create(firm=firm, **kw)


def _page(client, firm):
    res = client.get(f"/firms/{firm.slug}/")
    assert res.status_code == 200
    return res.content.decode()


# ---------------------------------------------------------------------------
# 1. A pill that looks like a citation has to be one
# ---------------------------------------------------------------------------
def test_a_real_url_still_renders_as_a_link(client):
    firm = _firm()
    _date(firm, source_url="https://www.goldmansachs.com/careers/students/")
    body = _page(client, firm)
    assert 'href="https://www.goldmansachs.com/careers/students/"' in body
    assert ">source</a>" in body


def test_a_seed_token_never_becomes_an_anchor(client):
    """The live defect: "seed:historical-pattern" is truthy, so it was
    emitted as href="seed:historical-pattern" — an unknown URI scheme the
    browser parses as absolute, so target=_blank opened nothing."""
    firm = _firm()
    _date(firm, source_url="seed:historical-pattern")
    body = _page(client, firm)
    assert "seed:historical-pattern" not in body
    assert 'href="seed' not in body
    # ...and the provenance is still SAID, in words, as plain text.
    assert "from past cycles" in body


def test_the_demo_seed_row_says_it_is_a_placeholder(client):
    """Apollo's single row wore CONFIRMED on a hard past date and offered
    "seed:demo" as its proof. Suppressing the pill would leave it looking
    like ordinary confirmed data; naming it is the honest fix."""
    firm = _firm(slug="apollo", name="Apollo")
    _date(firm, source_url="seed:demo", precision="day", confidence=1.0,
          date=dt.date(2026, 8, 2), event_kind="app_close")
    body = _page(client, firm)
    assert "seed:demo" not in body
    assert "sample data" in body


def test_an_unrecognised_non_url_is_marked_unverified(client):
    firm = _firm()
    _date(firm, source_url="internal:analyst-note")
    body = _page(client, firm)
    assert "internal:analyst-note" not in body
    assert "unverified" in body


def test_an_empty_source_shows_no_pill_at_all(client):
    firm = _firm()
    _date(firm, source_url="")
    body = _page(client, firm)
    for token in ("from past cycles", "sample data", "unverified", ">source<"):
        assert token not in body
