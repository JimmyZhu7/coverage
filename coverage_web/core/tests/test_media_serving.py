"""Guard: `/media/` (avatars) must be routable in production, not DEBUG only.

Measured live on Render: `coverage_web/urls.py` used to append the media
`static()` pattern only `if settings.DEBUG`, so production had NO route for
`/media/` at all — every avatar 404'd permanently, and the nav showed a
broken-image icon instead of a photo. That is a routing gap, not the
ephemeral-storage risk `settings/base.py`'s MEDIA_ROOT comment warns about
(a real S3-backed store still needs to land before an avatar survives a
redeploy) — this pins the narrower, already-fixable half: the URL exists.

Also pins the belt-and-braces fix alongside it: even with the route wired
up, a DB row can still name a file that isn't actually there (ephemeral
storage wiped it, upload never finished) — the nav and Settings avatars
must degrade to the initials fallback, not a broken `<img>`.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import Resolver404, resolve

User = get_user_model()


def test_the_media_url_resolves_to_a_view():
    """The bug in one assertion. `urlpatterns` is built once, at import
    time, from whatever `settings.DEBUG` was THEN — flipping the setting
    inside a test would prove nothing about a module-level `if` that no
    longer exists. What actually matters, and is what changed: the old code
    had `if settings.DEBUG: urlpatterns += static(...)`, so a process that
    ever imports this module with DEBUG off (production, always) built an
    `urlpatterns` list with no media route in it, permanently, for the life
    of that process. This just proves the route exists in THIS process,
    which is the same process any request — DEBUG on or off — resolves
    against."""
    try:
        resolve("/media/avatars/whatever.jpg")
    except Resolver404:
        pytest.fail("/media/ has no route — the live bug this test pins")


@pytest.mark.django_db
def test_the_nav_avatar_falls_back_to_initials_if_the_file_is_missing(client, settings, tmp_path):
    """`{% if user.avatar %}` only proves the DB row NAMES a file — not that
    the file is actually servable. The `<img>` needs its own `onerror`
    escape hatch to the sibling initials span for the case the row is
    truthy but the file 404s.

    `MEDIA_ROOT` is redirected to a throwaway `tmp_path` for this test: the
    default storage backend really does write bytes to disk on `.save()`,
    and without this override an earlier version of this test left stray
    files behind in the real project's `media/avatars/`."""
    settings.MEDIA_ROOT = tmp_path
    user = User.objects.create_user(email="avatar@example.com", password="x", name="Ava Tar")
    user.avatar.save("photo.jpg", ContentFile(b"not a real image, just needs a name"), save=True)
    client.force_login(user)

    body = client.get("/app/").content.decode()
    assert 'onerror="this.hidden=true; this.nextElementSibling.hidden=false;"' in body
    assert 'class="site-user-avatar site-user-avatar-fallback" hidden' in body


@pytest.mark.django_db
def test_a_user_with_no_avatar_still_gets_the_plain_fallback(client):
    """The untouched case: no avatar at all must not suddenly need the
    onerror machinery — it never renders an <img> to begin with."""
    user = User.objects.create_user(email="noavatar@example.com", password="x", name="No Photo")
    client.force_login(user)

    body = client.get("/app/").content.decode()
    assert 'class="site-user-avatar site-user-avatar-fallback">N</span>' in body
    assert "onerror=" not in body
