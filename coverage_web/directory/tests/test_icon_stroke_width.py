"""The custom-select caret must stay on the app-wide 1.5 stroke-width
convention.

Round 11 converged every small inline SVG in the product onto
stroke-width="1.5" (icons one stroke-width, stroked in currentColor -
see STATE.md's Design benchmark standards). The `.csel-caret` markup that
`enhance()` in base.html injects for every progressively-enhanced <select>
(and its no-JS fallback in opportunities.html) was missed by that sweep
because it's built via JS string concatenation rather than living as a
static inline <svg> a grep-based pass would catch. It shipped at 1.8,
visibly thicker than every other 14x14 icon on the same page. This test
pins both the JS-injected caret and the static fallback to 1.5 so a
future edit can't reintroduce the outlier.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def test_the_select_caret_ships_at_the_app_wide_stroke_width(client):
    firm = Firm.objects.create(slug="evercore", name="Evercore")
    Opportunity.objects.create(
        firm=firm, url="https://evercore.example/1", title="Summer Analyst",
        bucket="internship", status="open", region="us",
    )

    response = client.get(reverse("opportunities"))
    html = response.content.decode()

    # The static no-JS-fallback caret(s) actually rendered on the page.
    fallback_carets = re.findall(
        r'<svg class="csel-caret"[^>]*>\s*<path[^>]*stroke-width="([\d.]+)"',
        html,
    )
    assert fallback_carets, "expected at least one static .csel-caret in the rendered page"
    assert set(fallback_carets) == {"1.5"}, (
        f"static .csel-caret fallback caret(s) drifted off the 1.5 icon "
        f"convention: {fallback_carets}"
    )

    # The JS that progressively-enhances every <select> in the app (shipped
    # inline via base.html on every page, opportunities.html included)
    # must inject the same stroke-width, not the old 1.8 outlier.
    js_carets = re.findall(
        r'csel-caret.*?stroke-width=\\?"([\d.]+)\\?"',
        html,
        flags=re.DOTALL,
    )
    assert js_carets, "expected the enhance() caret markup to be present in the inline script"
    assert set(js_carets) == {"1.5"}, (
        f"JS-injected .csel-caret markup drifted off the 1.5 icon "
        f"convention: {js_carets}"
    )
