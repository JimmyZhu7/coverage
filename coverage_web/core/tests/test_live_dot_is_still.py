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

`kin-pulse-ring` itself is gone as of 2026-09-02. It survived the live dot
only because Today's `.lane-dot` still ran it, and that dot has since been
made static on the same argument: "critical" is the state the lane is IN,
and motion rule M1 reserves an infinite animation for a state that is
actually live. With no caller left, a kept-warm keyframe is dead CSS.
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


def test_no_rule_animates_with_a_keyframe_that_is_gone(settings):
    """Rewritten 2026-09-02. This test used to assert the OPPOSITE: that
    `kin-pulse-ring` must stay defined, because Today's `.lane-dot` was still
    running it. That premise expired when the lane dot was made static for
    the same reason the live dot was, so the keyframe lost its last caller
    and was deleted.

    The invariant worth guarding was never "keep this one keyframe". It is
    that no rule animates with a name nothing defines: a dangling reference
    is silent, the element simply sits at its computed style, and the next
    reader cannot tell a deliberate stillness from a broken one. So the test
    now checks the general property across both stylesheets rather than the
    one instance that happened to prompt it."""
    def _without_prose(text: str) -> str:
        """Comments discuss animation in English. `/* the animation was the
        part asking to be believed */` is not a declaration, and scanning it
        as one turns every sentence into a fake keyframe name."""
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
                      " ", text, flags=re.S)
        return text

    css = _without_prose(_coverage_css(settings))
    styles = _without_prose(
        (settings.BASE_DIR / "templates" / "crm" / "_styles.html").read_text()
    )

    defined = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)", css + styles))
    used = set()
    for sheet in (css, styles):
        for decl in re.findall(r"animation(?:-name)?\s*:([^;}]+)", sheet):
            for token in re.split(r"[,\s]+", decl.strip()):
                if re.fullmatch(r"[A-Za-z_-][A-Za-z0-9_-]*", token or ""):
                    used.add(token)

    # Words that legally appear in an `animation` shorthand and are not names.
    keywords = {
        "none", "infinite", "alternate", "alternate-reverse", "reverse",
        "normal", "forwards", "backwards", "both", "running", "paused",
        "linear", "ease", "ease-in", "ease-out", "ease-in-out", "step-start",
        "step-end", "steps", "cubic-bezier", "var", "inherit", "initial",
        "unset", "revert",
    }
    dangling = {n for n in used - defined - keywords if not n.startswith("--")}
    assert not dangling, (
        f"animation name(s) {sorted(dangling)} are referenced but never "
        "defined as @keyframes. The element will sit still, silently, and "
        "read as a deliberate decision it never was."
    )

    assert "@keyframes kin-pulse-ring" not in css, (
        "kin-pulse-ring is back. Its last caller, Today's .lane-dot, was made "
        "static on purpose; a keyframe kept warm for a caller that no longer "
        "exists is dead CSS."
    )


@pytest.mark.parametrize(
    "path, marker",
    # `/opportunities/` was the second case, matched on "Open Role" and the
    # `.live-dot` beside it. Both lived in the stat strip, which the founder
    # removed on 2026-09-03 ("just take this thing away"). This test guards
    # one thing — that killing the PULSE did not take the number with it — and
    # a surface removed outright on its own merits is not that. The board's
    # total is still stated, by the segment pill, and
    # `test_the_segment_pill_is_the_only_surface_stating_the_board_total`
    # is what holds it there now.
    [("/", "live roles right now")],
)
def test_the_live_count_itself_is_still_reported(path, marker):
    """Removing the animation must not remove the number with it."""
    html = Client().get(path).content.decode()

    assert 'class="live-dot"' in html, (
        f"{path} lost its live marker entirely; only the pulse was meant to go"
    )
    assert marker in html, f"{path} no longer reports the live roles count"
