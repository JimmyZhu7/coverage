"""Accessibility contracts that a page must not regress.

Measured, not assumed: an audit found the monogram tiles at 4.11:1 against a
4.5 requirement, no skip link on a page carrying ~3,000 focusable elements,
and a heading outline that jumped 1 to 3. Everything else it flagged turned
out to be the probe's fault — programmatic .focus() never triggers
:focus-visible, so "no focus ring" was a measurement artifact over a global
rule that covers every control.
"""

from __future__ import annotations

import re

import pytest

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _page(client, url="/opportunities/"):
    return client.get(url).content.decode()


def test_the_skip_link_is_the_first_thing_in_the_tab_order(client):
    body = _page(client)
    head = body[body.index("<body>"):body.index("<body>") + 600]
    assert 'class="skip-link"' in head
    assert head.index("skip-link") < (head.index("wordmark") if "wordmark" in head else 10**6)
    assert 'id="main" tabindex="-1"' in body, "the target must be able to take focus"


def test_the_heading_outline_never_skips_a_level(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(firm=firm, title="Summer Analyst", bucket="internship",
                               status="open", url="https://gs.com/sa")
    body = re.sub(r"<style.*?</style>", "", _page(client), flags=re.S)
    levels = [int(m) for m in re.findall(r"<h([1-4])\b", body)]
    assert levels, "the page has headings at all"
    jumps = [f"{a}->{b}" for a, b in zip(levels, levels[1:]) if b > a + 1]
    assert jumps == [], f"heading level skipped: {jumps}"


def test_every_firm_column_names_itself_as_a_section(client):
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(firm=firm, title="Analyst", bucket="internship",
                               status="open", url="https://ms.com/a")
    body = _page(client)
    assert '<h2 class="firmcol-h"' in body


def test_the_monogram_keeps_its_contrast_headroom(client):
    """The tile is hsl(hue 52% 90%) and the glyph hsl(hue 55% L). At L=31%
    the pair measured 4.11:1 on some hues, under the 4.5 that text needs;
    24% clears it across the wheel. A future tweak that raises L again should
    fail here rather than on a user's screen."""
    from pathlib import Path

    css = Path("coverage_web/templates/directory/_styles.html").read_text()
    m = re.search(r"\.firmcol-logo \{.*?color: hsl\(var\(--hue, 210\) 55% (\d+)%\)", css, re.S)
    assert m, "the monogram rule moved — re-measure before changing it"
    assert int(m.group(1)) <= 24, "lightness above 24% drops the glyph under 4.5:1"


# ---------------------------------------------------------------------------
# DARK MODE. One token block, not fifteen — and the pairings that flip.
# ---------------------------------------------------------------------------

def test_both_themes_are_defined_and_system_preference_leads():
    from pathlib import Path

    css = Path("coverage_web/static/css/coverage.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css, "the OS decides by default"
    assert ':root:not([data-theme="light"])' in css, "an explicit light choice wins"
    assert ':root[data-theme="dark"]' in css, "an explicit dark choice wins"


def test_text_on_an_accent_fill_flips_with_the_palette():
    """White is right over the light theme's deep navy and measures 2.53:1
    over dark mode's light blue. Every such pairing goes through
    --on-accent, so a future `color: #fff` on an accent fill is a
    regression this catches."""
    from pathlib import Path

    css = Path("coverage_web/static/css/coverage.css").read_text()
    for rule in (".btn-primary {", ".site-nav a.active {"):
        i = css.index(rule)
        block = css[i:css.index("}", i)]
        assert "var(--on-accent)" in block, f"{rule} must not hardcode its text colour"
    assert css.count("--on-accent:") >= 2, "the token is re-stated per theme"


def test_the_theme_is_applied_before_the_stylesheet_loads(client):
    """A deferred script runs after first paint — which is exactly the frame
    where the wrong palette would flash."""
    body = client.get("/opportunities/").content.decode()
    head = body[: body.index("</head>")]
    assert "coverage-theme" in head
    assert head.index("coverage-theme") < head.index("coverage.css")
