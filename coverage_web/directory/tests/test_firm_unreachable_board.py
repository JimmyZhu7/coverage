"""P9 on the one page where staying quiet would be a lie.

BlackRock runs a campus board. Coverage knows its address —
`BlackRock_Early_Careers_Program`, read straight off the tenant's own
`robots.txt` — and does not fetch it, because that same `robots.txt`
disallows it and D-20 settled that the product honours the ask. The firm
page therefore shows BlackRock's experienced requisitions and no internship,
which a student reads as "the programme is not running": a claim the product
has no evidence for and would be making by omission.

So the page says what it cannot see and hands over the link. Asserted against
rendered HTML, because the failure is a missing sentence, which no
helper-level test can miss more thoroughly than a human can.
"""

from __future__ import annotations

import pytest

from directory.models import Firm

pytestmark = pytest.mark.django_db


def _page(client, slug):
    res = client.get(f"/firms/{slug}/")
    assert res.status_code == 200
    return res.content.decode()


def test_the_firm_page_says_it_does_not_read_the_campus_board(client):
    Firm.objects.create(slug="blackrock", name="BlackRock")

    body = _page(client, "blackrock")

    assert "We do not scrape BlackRock's campus board" in body
    assert "robots.txt" in body


def test_it_hands_the_student_the_link(client):
    """The whole point of recording the board rather than forgetting it. A
    student who cannot get the rows from us can still go and apply."""
    Firm.objects.create(slug="blackrock", name="BlackRock")

    body = _page(client, "blackrock")

    assert 'href="https://careers.blackrock.com/early-careers"' in body
    assert "Open it yourself" in body


def test_an_ordinary_firm_says_nothing_of_the_kind(client):
    """The note is a fact about two firms, not a disclaimer on every page."""
    Firm.objects.create(slug="gs", name="Goldman Sachs")

    body = _page(client, "gs")

    assert "We do not scrape" not in body
