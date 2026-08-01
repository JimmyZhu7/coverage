"""Guard: every app page wears the same header, and only marketing wears a hero.

The app had grown four header patterns across five nav pages — the tall
`.kin-hero` marquee on Network/Settings/Capture, a compact variant on
Opportunities, a bespoke strip on Today, and a centred `.page-head`
elsewhere. Same product, four different first impressions, and three
different y positions for the first line of content.

The rule this pins is the one every serious app makes: MARKETING pages get
the decorated hero, TOOL pages get one compact `.pagehead` and then get out
of the way. A gradient card on a page whose job is a 900-row list is
decoration charged to the content's budget.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

# Every signed-in page a student actually navigates to.
APP_PAGES = [
    "/app/",
    "/opportunities/",
    "/opportunities/mine/",
    "/app/contacts/",
    "/app/contacts/new/",
    "/app/contacts/archived/",
    "/capture/health/",
    # Settings, not "/welcome/": that path is the ONBOARDING wizard for a user
    # who hasn't finished it, and only redirects here once `onboarded_at` is
    # set. The nav's "Settings" link points at /welcome/ precisely so a
    # half-onboarded user is sent back to finish.
    "/welcome/settings/",
]

# Deliberately NOT on the shared header: the onboarding wizard and the auth
# screens are full-page focused flows with no nav, no sidebar and one job.
# Giving them a page header would frame them as somewhere you navigated to
# rather than something you are in the middle of.
FLOW_PAGES = ["/welcome/", "/accounts/login/", "/accounts/signup/"]

# The two pages that sell the product rather than being it.
MARKETING_PAGES = ["/", "/pricing/"]


def _client():
    user = get_user_model().objects.create_user(
        email="header@example.com", password="x" * 14
    )
    c = Client()
    c.force_login(user)
    return c


def _body(html: str) -> str:
    """Markup only — the stylesheet names every class it styles."""
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


@pytest.mark.parametrize("path", APP_PAGES)
def test_every_app_page_uses_the_shared_header(path):
    body = _body(_client().get(path).content.decode())
    assert 'class="pagehead"' in body, f"{path} is not on the shared header"
    assert "pagehead-title" in body, f"{path} has no title in its header"


@pytest.mark.parametrize("path", APP_PAGES)
def test_no_app_page_wears_the_marketing_hero(path):
    """The marquee is for `/` and `/pricing/`. Anywhere else it is 230px of
    gradient above content the user came to read."""
    body = _body(_client().get(path).content.decode())
    assert "kin-hero" not in body, f"{path} still renders the marketing hero"
    assert 'class="page-head"' not in body, f"{path} still uses the legacy centred head"


@pytest.mark.parametrize("path", MARKETING_PAGES)
def test_marketing_pages_keep_their_hero(path):
    """The other half of the split, pinned so a future sweep doesn't
    "standardise" the landing page into a dashboard header."""
    body = _body(Client().get(path).content.decode())
    assert "kin-hero" in body, f"{path} lost the marketing hero"
    assert 'class="pagehead"' not in body


def test_exactly_one_header_per_app_page():
    """Two headers on a page means a conversion left the old one behind —
    which is exactly what happened twice while doing this, and rendered as an
    empty shell rather than anything obviously wrong."""
    c = _client()
    for path in APP_PAGES:
        body = _body(c.get(path).content.decode())
        assert body.count('class="pagehead"') == 1, f"{path} has more than one header"


@pytest.mark.parametrize("path", FLOW_PAGES)
def test_focused_flows_are_left_alone(path):
    """Documents the exemption rather than leaving it to be rediscovered as a
    gap. These render, they just don't wear the app chrome."""
    c = Client() if path.startswith("/accounts/") else _client()
    assert c.get(path).status_code == 200
