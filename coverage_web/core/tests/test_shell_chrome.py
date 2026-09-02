"""The shell: browser chrome, landmarks, one toast style, and a 500 page that
does not assume daylight.

Four small things the 2026-09-01 audit found in `base.html` and `500.html`,
which between them frame every other page in the product:

  * `theme-color` was `#1d3a5f`, the navy from before the v4 palette, and it
    did not follow the theme. It is the one piece of UI the site paints
    OUTSIDE its own document, so it was also the only place the old accent
    survived where a user could see it.
  * two `<nav>` landmarks had no accessible name, so a screen reader listing
    regions got "navigation" twice with nothing to choose between.
  * the htmx failure toast built itself from inline styles: 14px (off the
    type scale entirely), an 8px radius where the app's control radius is
    10px, and hardcoded dark fallbacks that could not follow the palette. A
    second toast style beside `.msg`, which every flash message already uses.
  * `500.html` was light-only, on the old ink and the old navy. It cannot
    depend on coverage.css (that stylesheet may be exactly what is down), so
    it inlines the palette; what it had inlined was three redesigns stale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.template.loader import render_to_string

BASE = Path(__file__).resolve().parents[2] / "templates" / "base.html"
ERROR_500 = Path(__file__).resolve().parents[2] / "templates" / "500.html"


@pytest.mark.django_db
def test_theme_color_is_the_v4_navy_and_follows_the_scheme(client):
    body = client.get("/").content.decode()
    tags = re.findall(r'<meta name="theme-color"[^>]*>', body)

    assert len(tags) == 2, f"one fallback plus one dark override, found {tags}"
    assert 'content="#1f4e79"' in tags[0] and "media" not in tags[0], (
        "the unqualified tag comes first, as the fallback for clients that "
        "do not understand `media` here"
    )
    assert 'media="(prefers-color-scheme: dark)"' in tags[1]
    assert "#1d3a5f" not in body, "the pre-v4 navy is gone from the shell"


@pytest.mark.django_db
def test_every_nav_landmark_has_a_name(client):
    body = client.get("/").content.decode()
    navs = re.findall(r"<nav\b[^>]*>", body)

    assert navs, "the shell should render navigation landmarks"
    for nav in navs:
        assert "aria-label=" in nav, f"unnamed navigation landmark: {nav}"
    labels = re.findall(r'<nav[^>]*aria-label="([^"]+)"', body)
    assert len(labels) == len(set(labels)), (
        f"two landmarks with the same name are as useless as two with none: {labels}"
    )
    assert "Account" in labels, "the profile/sign-out block is an account nav"
    assert "Footer" in labels


def test_the_htmx_toast_uses_the_shared_message_style():
    source = BASE.read_text()

    assert 'el.className = "msg";' in source, (
        "one toast style: the failure toast wears the same class as every "
        "flash message the app renders"
    )
    assert "border-radius:8px" not in source, "8px is a third radius in a two-radius system"
    assert "font-size:14px" not in source, "14px is not on the type scale"


@pytest.mark.django_db
def test_the_custom_select_button_is_named_and_its_options_are_options(client):
    """axe: "button, Any Year, collapsed", with an empty listbox.

    The native <select> keeps the `<label>`; the styled button that replaces
    it on screen had no name of its own, so four filters on the busiest page
    in the product announced only their current value. The options had no
    `role=option` either, which makes the `aria-selected` on them meaningless
    and leaves the listbox announcing no children.

    Asserted on the shipped script rather than on a live DOM, because this
    behaviour is JS and the suite has no browser. Verified by hand against
    the running server on 2026-09-01: the four filters compute to "Programme
    Year Any Year", "Region Any Region", "Track Any Track" and "Sponsorship
    Any Sponsorship", and the option rows read `option`/`aria-selected`.
    """
    body = client.get("/").content.decode()
    scripts = " ".join(re.findall(r"<script[^>]*>(.*?)</script>", body, re.S))

    assert 'li.setAttribute("role", "option")' in scripts
    assert 'li.setAttribute("aria-selected"' in scripts
    assert 'btn.setAttribute("aria-labelledby", capSpan.id + " " + val.id)' in scripts
    # Half the selects sit INSIDE their label, whose text is the caption plus
    # every option; naming the button after that element would be worse than
    # leaving it unnamed. The caption is read out of a clone with the control
    # removed instead.
    assert "clone.querySelector(\"select\")" in scripts


def test_the_500_page_carries_both_palettes_and_no_stale_ink():
    """Asserted on the RENDERED page, not the template source.

    Django renders 500.html with an EMPTY context, which is the whole reason
    this page is written the way it is, so rendering it with none is also the
    honest way to test it. It matters here for a second reason too: the
    template's own `{% comment %}` block names the stale hexes it replaced,
    and a source-level assertion would trip over the explanation of the fix.
    """
    page = render_to_string("500.html")

    # Still self-contained: an error page that needs a stylesheet is an error
    # page that fails when the stylesheet is what broke.
    assert "coverage.css" not in page
    assert "<link" not in page

    assert "@media (prefers-color-scheme: dark)" in page
    for stale in ("#171717", "#1d3a5f", "#555"):
        assert stale not in page, f"{stale} is pre-v4 ink/navy"
    for light in ("#f2f4ee", "#191b16", "#1f4e79"):
        assert light in page, f"{light} is a current light token"
    for dark in ("#141712", "#eaece5", "#7aa7d4"):
        assert dark in page, f"{dark} is a current dark token"
