"""Opportunity tracking — the writable UserOpportunity path behind the feed's
Save star and the My Applications funnel. Every write must be tenant-scoped."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
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
