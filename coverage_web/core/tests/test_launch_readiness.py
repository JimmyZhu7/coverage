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
