"""The live-roles marker states a number; it does not pulse at you.

`.live-dot` used to fire an expanding ring out of an `::after`, forever. On
the landing page that put "READING 2478 LIVE ROLES RIGHT NOW" in the same
family as the "12 people are viewing this now" badge every SaaS marketing
site ships, and it was the least "annual report" element on the page.

The count is real, sourced, and worth stating — it is only the animation that
was asking to be believed rather than read. So the number stays on both
surfaces that carry it (the landing hero and the Opportunities stat strip)
and the ring is gone.

Gone, not merely paused: held still at its first keyframe the ring is a 14px
circle drawn around an 8px dot, which reads as a target rather than a marker.

The `kin-pulse-ring` keyframes stay defined — the Today board's `.lane-dot`
is a separate component that still uses them, and deleting the keyframes
would silently break it.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def _coverage_css(settings) -> str:
    return (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()


def test_the_live_dot_does_not_pulse(settings):
    """An infinite expanding ring is a growth-hacking tell, not a status."""
    css = _coverage_css(settings)

    block = re.search(r"^\.live-dot \{(.*?)\}", css, re.S | re.M)
    assert block, ".live-dot is gone from coverage.css; re-check this guard"
    assert "animation" not in block.group(1), (
        "the live dot animates again. The roles count is real and can simply "
        "be stated; a pulsing dot next to it reads as a landing-page growth "
        'trope ("12 people viewing this now"), which is the one thing this '
        "page's paper-and-serif treatment is trying not to be."
    )

    assert not re.search(r"^\.live-dot::after \{", css, re.M), (
        "the live dot's ring is back. Even unanimated it is a 14px circle "
        "around an 8px dot — a target, not a marker."
    )


def test_the_today_boards_lane_dot_keeps_its_keyframes(settings):
    """`.lane-dot` is a different component on a different surface and still
    references `kin-pulse-ring`; removing the keyframes would break it."""
    assert "@keyframes kin-pulse-ring" in _coverage_css(settings), (
        "kin-pulse-ring was deleted along with the live dot's ring, but "
        "crm/_styles.html's .lane-dot still animates with it — that lane "
        "marker is now static by accident rather than by decision."
    )


@pytest.mark.parametrize(
    "path, marker",
    [("/", "live roles right now"), ("/opportunities/", "Open Role")],
)
def test_the_live_count_itself_is_still_reported(path, marker):
    """Removing the animation must not remove the number with it."""
    html = Client().get(path).content.decode()

    assert 'class="live-dot"' in html, (
        f"{path} lost its live marker entirely; only the pulse was meant to go"
    )
    assert marker in html, f"{path} no longer reports the live roles count"
