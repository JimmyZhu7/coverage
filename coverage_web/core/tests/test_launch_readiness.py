"""The seams a stranger hits first: error pages, legal redirects, link
previews, throttles, and the unfinished-setup nudge."""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

pytestmark = pytest.mark.django_db


def test_a_wrong_url_gets_the_branded_404(client):
    with override_settings(DEBUG=False):
        resp = client.get("/definitely-not-a-page/")
    assert resp.status_code == 404
    assert b"That page doesn't exist." in resp.content
    assert b"/opportunities/" in resp.content, "a lost visitor gets a way back"


def test_the_500_page_renders_without_any_context():
    """Django renders 500.html with an EMPTY context after something has
    already broken, so the template must survive exactly that."""
    from django.template.loader import render_to_string

    html = render_to_string("500.html", {})
    assert "Something broke" in html
    assert "{% static" not in html, "must not depend on the static machinery"


def test_bare_legal_paths_redirect_to_the_real_pages(client):
    assert client.get("/privacy/").status_code == 301
    assert client.get("/privacy/", follow=True).status_code == 200
    assert client.get("/terms/", follow=True).status_code == 200


def test_every_page_ships_a_link_preview(client):
    body = client.get("/").content.decode()
    assert 'property="og:title"' in body
    assert 'name="description"' in body
    assert 'rel="icon"' in body


def test_the_count_up_animation_is_gone(client):
    """It froze mid-count on throttled tabs and presented 235 as the real
    value of 908 — twice, on the feed's headline stat. Numbers are now
    animated by container motion only, so every frame shows the true value."""
    body = client.get("/opportunities/").content.decode()
    assert "requestAnimationFrame(tick)" not in body


def test_search_throttles_a_hammering_client(client):
    cache.clear()
    try:
        statuses = [client.get("/search/?q=gs").status_code for _ in range(45)]
        assert 429 in statuses, "sustained hammering must hit the wall"
        assert statuses[0] == 200, "the first request is never throttled"
    finally:
        cache.clear()


def test_a_new_user_is_nudged_to_finish_setup(client, django_user_model):
    u = django_user_model.objects.create_user(email="fresh@x.com", password="x")
    client.force_login(u)
    body = client.get("/app/").content.decode()
    assert "Finish setting up" in body

    from django.utils import timezone

    u.onboarded_at = timezone.now()
    u.save(update_fields=["onboarded_at"])
    body = client.get("/app/").content.decode()
    assert "Finish setting up" not in body


def test_the_favicon_survives_the_pushstate_fallback(client):
    """This tab has gone blank twice on Opportunities, and the second time
    the test above was green while it happened — it asserted /favicon.ico
    redirected to the SVG, which was the exact thing that did not resolve.
    A test that pins the mechanism instead of the outcome cannot see the
    outcome break.

    So this pins the outcome: hx-push-url calls pushState, Chrome
    re-resolves the icon, and whatever it asks for has to come back as a
    real image. Every declared icon resolves, and the /favicon.ico fallback
    is a RASTER — an SVG served there is the same asset that already failed
    to resolve, not a fallback."""
    import re
    from django.conf import settings

    head = client.get("/").content.decode().split("</head>")[0]
    assert "data:image/svg" not in head, "a data URI is what blanked it the first time"

    hrefs = re.findall(r'<link[^>]*rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', head)
    assert len(hrefs) >= 2, f"an SVG alone is not a fallback chain: {hrefs}"

    # Every declared icon must exist on disk where staticfiles will serve it.
    for href in hrefs:
        rel = href.split("/static/", 1)[-1]
        assert (settings.BASE_DIR / "static" / rel).is_file(), f"missing icon file: {href}"
    assert any(h.endswith(".png") for h in hrefs), "no raster icon declared"

    # A raster must be declared BEFORE the SVG. Safari — the browser that
    # actually showed the blank tab — has the patchier SVG-favicon support of
    # the two, so whichever icon a browser reaches for first has to be one
    # that cannot fail to rasterise.
    first_icon = re.search(r'<link[^>]*rel="icon"[^>]*>', head).group(0)
    assert ".png" in first_icon or ".ico" in first_icon, \
        f"a raster must lead the icon list, got: {first_icon}"

    # The fallback path itself: served DIRECTLY, no redirect. Safari checks
    # /favicon.ico as a matter of course and is unreliable about following a
    # redirect for an icon; both earlier fixes left one here.
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200, f"must be served directly, got {resp.status_code}"
    assert "icon" in resp["Content-Type"], resp["Content-Type"]
    body = b"".join(resp.streaming_content) if resp.streaming else resp.content
    assert body[:4] == b"\x00\x00\x01\x00", "not a real .ico file"


def test_the_svg_icon_declares_its_own_size():
    """The third and actual cause of the blank tab on Opportunities: the SVG
    carried a viewBox but no width/height, so it had NO intrinsic size.
    Chrome fetched it 200, loaded it fine as an <img> (falling back to the
    150x150 default), and still refused to rasterize it into a tab icon —
    which is why the earlier fixes, both aimed at the /favicon.ico fallback,
    changed nothing: the fallback was never reached, because Chrome had
    already committed to an SVG it could not draw.

    Asserted on the ROOT element's attributes, not the file text: a naive
    /width=/ search matches `stroke-width` and passes on the broken file.
    That false positive is exactly how this was nearly missed."""
    import re
    from django.conf import settings

    svg = (settings.BASE_DIR / "static" / "img" / "favicon.svg").read_text()
    root = re.match(r"<svg\b([^>]*)>", svg.strip())
    assert root, "favicon.svg has no root <svg> element"
    attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', root.group(1)))
    for dim in ("width", "height"):
        assert dim in attrs, f"root <svg> is missing {dim}: no intrinsic size"
        assert attrs[dim].strip().rstrip("px").isdigit(), f"{dim}={attrs[dim]!r}"
    assert "viewBox" in attrs, "keep the viewBox so it still scales"
