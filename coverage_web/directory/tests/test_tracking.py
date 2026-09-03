"""Opportunity tracking — the writable UserOpportunity path behind the feed's
Save star and the My Applications funnel. Every write must be tenant-scoped."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.urls import reverse

from analytics.models import UserOpportunity
from directory.models import Firm, Opportunity

User = get_user_model()


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="x")


def _opp():
    firm = Firm.objects.create(name="Evercore", slug="evercore")
    return Opportunity.objects.create(
        firm=firm, url="https://x/1", title="Summer Analyst",
        bucket="internship", status="open",
    )


@pytest.mark.django_db
def test_track_requires_login(client):
    o = _opp()
    resp = client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})
    assert resp.status_code == 302 and "/accounts/login/" in resp["Location"]


@pytest.mark.django_db
def test_save_then_apply_stamps_the_funnel(client):
    user = _user()
    o = _opp()
    client.force_login(user)

    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})
    uo = UserOpportunity.objects.for_user(user).get(opportunity=o)
    assert uo.applied_status == ""       # saved == tracked, not yet in funnel
    assert uo.applied_at is None

    client.post(reverse("track_opportunity", args=[o.id]), {"status": "submitted"})
    uo.refresh_from_db()
    assert uo.applied_status == "submitted"
    assert uo.applied_at is not None     # funnel entry stamps applied_at


@pytest.mark.django_db
def test_clear_removes_the_row(client):
    user = _user()
    o = _opp()
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "clear"})
    assert UserOpportunity.objects.for_user(user).filter(opportunity=o).count() == 0


@pytest.mark.django_db
def test_track_is_per_user(client):
    a, b = _user("a@example.com"), _user("b@example.com")
    o = _opp()
    client.force_login(a)
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})
    # b's My Applications never shows a's tracked role.
    client.force_login(b)
    assert UserOpportunity.objects.for_user(b).count() == 0
    resp = client.get(reverse("my_applications"))
    assert resp.status_code == 200
    assert b"No roles saved yet" in resp.content


@pytest.mark.django_db
def test_my_applications_status_change_swaps_in_place_instead_of_redirecting(client):
    """Regression: My Applications' stage dropdown and Remove button used to
    be a plain `<form method="post">` with no `hx-*` attributes, so every
    status change was a full page reload — reported live as "why does the
    page refresh when I change status". `hx-post` on the form plus
    `HX-Request` on the request must now return the re-rendered
    `_apps_body.html` partial (200, same content type as the full page's
    body), not a redirect."""
    user = _user()
    o = _opp()
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})

    resp = client.post(
        reverse("track_opportunity", args=[o.id]),
        {"status": "submitted", "next": reverse("my_applications")},
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200, "must swap in place, not redirect"
    body = resp.content.decode()
    assert '<div id="apps-body">' in body
    assert "<html" not in body.lower(), "a partial, not the full page"
    # The row actually moved stage — the reason a single-row swap can't work
    # here, and the reason this test posts a real status change rather than
    # asserting on markup alone. It used to look for the stage SECTION the row
    # had moved into; those went on 2026-09-03, so the proof is now the row's
    # own control coming back preselected to the new stage.
    assert '<option value="submitted" selected>Applied</option>' in body
    uo = UserOpportunity.objects.for_user(user).get(opportunity=o)
    assert uo.applied_status == "submitted"


@pytest.mark.django_db
def test_the_feed_still_gets_its_own_single_control_swap(client):
    """The `next` field is what tells `track_opportunity` which of the two
    htmx callers is asking — the feed's card control never sends one, and
    must keep getting `_track_control.html`, not the My Applications
    partial, after this change."""
    user = _user()
    o = _opp()
    client.force_login(user)

    resp = client.post(
        reverse("track_opportunity", args=[o.id]),
        {"status": "saved"},
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="apps-body"' not in body
    assert f'id="track-{o.id}"' in body


@pytest.mark.django_db
def test_track_ignores_external_next_redirect(client):
    """A non-HX POST with an attacker-controlled `next` must not open-redirect
    off-site; it falls back to My Applications."""
    user = _user()
    o = _opp()
    client.force_login(user)
    resp = client.post(
        reverse("track_opportunity", args=[o.id]),
        {"status": "saved", "next": "https://evil.example/phish"},
    )
    assert resp.status_code == 302
    assert "evil.example" not in resp["Location"]
    assert resp["Location"].endswith(reverse("my_applications"))


def test_save_button_targets_closest_track_not_the_shared_id():
    """Every "Picked for you" role is also rendered a second time under its
    own firm's column (see _results.html), so `id="track-{{ r.id }}"` is NOT
    unique in the DOM on that page — both copies render it. If the Save
    button's hx-target were the bare id selector `#track-{{ r.id }}`, htmx's
    querySelector-style lookup would always resolve to the FIRST matching
    node in document order (the Picked-for-you copy, since that column
    renders first) — so clicking the button on the second, firm-column copy
    would swap the untouched first copy instead, leaving the actually-clicked
    button visually unchanged while a different card silently flips to
    "Saved". The fix is a self-scoped target, `closest .track`, mirroring the
    "Not for me" button's own `closest .rolecard` — verified here directly on
    the rendered partial, independent of the duplicate id it protects
    against."""
    html = render_to_string(
        "directory/_track_control.html", {"r": {"id": 999, "track_status": None}}
    )
    assert 'hx-target="closest .track"' in html
    assert 'hx-target="#track-999"' not in html

    html_saved = render_to_string(
        "directory/_track_control.html", {"r": {"id": 999, "track_status": "saved"}}
    )
    assert 'hx-target="closest .track"' in html_saved
    assert 'hx-target="#track-999"' not in html_saved


def test_duplicate_track_ids_no_longer_matter_for_targeting():
    """Directly reproduces the collision: render the SAME opportunity twice
    (as _results.html does for a picked role) and confirm both copies still
    share an id — that duplication is inherent to the deliberate
    dual-listing and is not being removed — but neither copy's hx-target
    depends on that id being unique."""
    ctx = {"r": {"id": 4242, "track_status": None}}
    first = render_to_string("directory/_track_control.html", ctx)
    second = render_to_string("directory/_track_control.html", ctx)

    assert 'id="track-4242"' in first and 'id="track-4242"' in second
    for rendered in (first, second):
        assert "hx-target=\"closest .track\"" in rendered
        assert "#track-4242" not in rendered
