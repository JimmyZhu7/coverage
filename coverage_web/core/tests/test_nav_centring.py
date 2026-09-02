"""The phone nav must centre the pill you are ON, against the FINAL layout.

On a 375px phone the primary nav is one horizontally scrolled row, and an
inline script scrolls the active pill into view so the page you are on is not
parked off-screen. The script measured too early, twice over:

1. It sat immediately after `</nav>`, so `.site-auth` — the avatar and Sign
   Out, which share the same flex row — was not in the DOM yet and the nav
   measured ~230px instead of its final 174px. `scrollIntoView` cannot
   overshoot; it clamps to the wider box's smaller maximum. When `.site-auth`
   then laid out, the nav shrank, the real maximum grew, and the browser kept
   the now-56px-short scrollLeft.

2. The pills are webfont-set, so the row's scrollWidth grows after first
   paint. Even below `.site-auth`, a one-shot call clamps to the pre-font
   maximum and lands short a second time.

Live on /welcome/settings/, where Settings is the LAST pill, that left the
accent-filled you-are-here marker reading "SET" with its tail cut off: 41.9px
of a 92.8px pill inside the box, 55% clipped, with Today and Opportunities
entirely off-screen.

These tests pin the two structural facts a browser cannot get right without:
the script runs after the row is complete, and it re-measures when the row
changes size instead of guessing which reflow is the last one.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

pytestmark = pytest.mark.django_db

# Every signed-in page wearing the shared header. The bug was worst on
# Settings (last pill, so the clamp lands hardest) but the script is shared.
NAV_PAGES = ["/app/", "/opportunities/", "/app/contacts/", "/app/calendar/",
             "/welcome/settings/"]


def _client():
    """An ONBOARDED user, which every page in NAV_PAGES describes anyway.

    `onboarded_at` was left None here until 2026-09-01, when the header
    started hiding `.site-nav` for the length of the wizard: /welcome/ is the
    Settings prefix, so every step of onboarding lit the SETTINGS pill and
    handed a student on step 1 of 4 five ways out of the thing gating the
    product. There is no nav to centre while that is true, so a fixture with
    no `onboarded_at` was asserting the centring script against a header that
    correctly has no pills in it.

    Stamping the field does not soften anything these tests check. Today,
    Opportunities, Network, Calendar and Settings are all pages you reach
    after the wizard, and the bug being pinned (a pill measured before
    `.site-auth` lays out) is a property of that finished header.
    """
    user = get_user_model().objects.create_user(
        email="nav@example.com", password="x" * 14, onboarded_at=timezone.now(),
    )
    c = Client()
    c.force_login(user)
    return c


def _nav_script(html: str) -> str:
    """The centring script, by the call only it makes.

    `[^>]*` on the open tag, not a bare `<script>`, because base.html's
    inline scripts render through django-csp's `{% script %}` tag (see
    "Wire django-csp + django-permissions-policy" in git log), which stamps
    a `nonce="..."` attribute onto the tag -- this test cares which script
    ran, not what attributes the CSP layer put on it.
    """
    for block in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
        if 'inline: "center"' in block and "site-nav" in block:
            return block
    raise AssertionError("the nav-centring script is gone from the header")


@pytest.mark.parametrize("path", NAV_PAGES)
def test_the_centring_script_runs_after_the_whole_header_row(path):
    """`.site-auth` shares the nav's flex row. Measured before it exists, the
    nav is ~56px too wide and the clamp lands the active pill half outside."""
    html = _client().get(path).content.decode()

    auth = html.index('class="site-auth"')
    nav_close = html.index("</nav>")
    script = html.index(_nav_script(html))

    assert nav_close < auth, "sanity: .site-auth should follow the nav"
    assert script > auth, (
        f"{path}: the nav-centring script runs before .site-auth lays out, so "
        "it measures a nav ~56px wider than its final width and clamps the "
        "active pill half outside the scroll box"
    )


def test_the_script_re_measures_instead_of_trusting_one_reading():
    """The webfont widens the pills after first paint, so a single call — at
    any placement — clamps against a stale maximum."""
    script = _nav_script(_client().get("/welcome/settings/").content.decode())

    assert "ResizeObserver" in script, (
        "the script takes a single measurement; the row's scrollWidth grows "
        "when the webfont lands and the pill ends up short again"
    )
    # A container-only observer never fires here: the nav's own box stays
    # 174px while its CONTENT is what widens. The pills must be observed too.
    assert re.search(r"\.children|querySelectorAll", script), (
        "only the nav container is observed; its border box does not change "
        "when the webfont widens the pills, so the callback never fires"
    )
    assert "pointerdown" in script, (
        "no interaction latch: a late reflow will yank the row back after a "
        "reader has scrolled it themselves"
    )


def test_the_last_pill_can_clear_the_right_edge_fade(settings):
    """Settings is the last pill, so centring pins the row flush to its end —
    and flush against the end, the pill's tail sits under the 28px mask fade
    and finishes at ~18% opacity. Trailing padding gives it somewhere to go."""
    css = (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()

    phone = re.search(r"@media \(max-width: 640px\) \{(.*)", css, re.S).group(1)
    nav = re.search(r"\.site-nav \{(.*?)\}", phone, re.S).group(1)

    fade = re.search(r"mask-image: linear-gradient\(90deg, #000 calc\(100% - (\d+)px\)", nav)
    assert fade, "the right-edge overflow fade is gone; re-check this guard"
    pad = re.search(r"padding-right:\s*(\d+)px", nav)
    assert pad, (
        "no trailing padding on the scrolled nav: scrolled to the end, the "
        "fade covers the last pill rather than empty space"
    )
    assert int(pad.group(1)) >= int(fade.group(1)), (
        f"{pad.group(1)}px of trailing padding does not clear the "
        f"{fade.group(1)}px fade; the last pill still ends faded"
    )
