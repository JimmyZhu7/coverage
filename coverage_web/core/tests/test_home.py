"""
Exercises the base template + htmx static asset wiring, not just routing:
renders base.html -> core/home.html through the real template loader and
staticfiles finder. Since the v2 landing page, home reads two ledger counts
(live campus roles, firms tracked) from the shared zone, so the test needs a
database — healthz remains the deliberately DB-free check (see
test_healthz.py).
"""

import pytest

from directory.models import Firm, Opportunity


@pytest.mark.django_db
def test_home_page_renders_with_htmx_script_tag(client):
    response = client.get("/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Coverage" in content
    # Vendored static asset, not a CDN — see coverage_web/static/vendor/.
    assert "vendor/htmx.min.js" in content


@pytest.mark.django_db
def test_home_page_shows_live_ledger_counts(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(
        firm=firm, url="https://x/1", title="Summer Analyst", bucket="internship", status="open"
    )
    Opportunity.objects.create(
        firm=firm, url="https://x/2", title="Vice President", bucket="other", status="open"
    )

    content = client.get("/").content.decode()
    assert "Live Campus Roles" in content
    # Only the campus bucket counts toward the hero figure; "other" does not.
    assert '<span class="dash-num">1</span>' in content
