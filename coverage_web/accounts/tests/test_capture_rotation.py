"""Tests for regenerating the capture address (docs/specs/settings-page.md
audit #4, B2).

The hole: `u-<slug>@in.coverage.app` is a bearer secret. `capture.services.
resolve_user` routes inbound mail purely by the slug in the recipient list, so
anyone who learns the address — a forwarded thread, a screenshot, a recipient
hitting reply-all on a BCC'd chain — could post mail that landed as capture
events, pending contacts, and (via the deterministic extractors) touches in a
student's private CRM. The only remedy was deleting the account.

The two assertions that matter are that the OLD slug stops resolving in the
real inbound path, and that nothing already captured is disturbed. Everything
else here is the confirm-page contract.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from accounts import services
from analytics.models import ProductEvent
from capture.services import ParsedInbound, resolve_user
from crm.models import CaptureEvent, Contact

User = get_user_model()

pytestmark = pytest.mark.django_db

SETTINGS = "accounts:settings"
REGENERATE = "accounts:capture_regenerate"


@pytest.fixture
def student():
    return User.objects.create_user(
        email="rotate@example.com", password="x", capture_slug="oldslug1234"
    )


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return student


def _inbound_to(address: str) -> ParsedInbound:
    """A parsed inbound whose only recipient is `address` — the shape
    `resolve_user` actually consumes."""
    parsed = ParsedInbound.__new__(ParsedInbound)
    parsed.recipients = [("", address)]
    return parsed


# ---------------------------------------------------------------------------
# The security property
# ---------------------------------------------------------------------------
def test_the_old_address_stops_resolving_in_the_inbound_path(student):
    """The whole point. Mail to the leaked address must not reach this user's
    CRM — and must not reach anyone else's either."""
    old_address = services.capture_address(student)
    assert resolve_user(_inbound_to(old_address)) == student

    new_address = services.regenerate_capture_address(student)

    assert new_address != old_address
    assert resolve_user(_inbound_to(old_address)) is None
    assert resolve_user(_inbound_to(new_address)) == student


def test_the_new_slug_is_not_the_old_one_and_is_unguessable_length(student):
    old = student.capture_slug
    services.regenerate_capture_address(student)
    student.refresh_from_db()
    assert student.capture_slug != old
    # token_urlsafe(9) — 12 base64url characters, ~72 bits.
    assert len(student.capture_slug) >= 12


def test_regenerating_twice_in_a_row_is_safe_under_the_unique_constraint(student):
    """Idempotency-adjacent: the unique constraint on `capture_slug` must not
    be a way to 500 a security control."""
    seen = {student.capture_slug}
    for _ in range(5):
        services.regenerate_capture_address(student)
        student.refresh_from_db()
        assert student.capture_slug not in seen
        seen.add(student.capture_slug)


def test_it_never_collides_with_another_users_slug(student):
    other = User.objects.create_user(email="other@example.com", password="x")
    for _ in range(3):
        services.regenerate_capture_address(student)
        student.refresh_from_db()
        other.refresh_from_db()
        assert student.capture_slug != other.capture_slug


# ---------------------------------------------------------------------------
# What must survive
# ---------------------------------------------------------------------------
def test_past_captured_activity_is_untouched(student):
    """`capture_events.provider_ref` is the sender's Message-ID, not the slug,
    so rotation has nothing durable to invalidate. Asserted rather than
    assumed — this is the claim the confirm page makes to the user."""
    contact = Contact.all_objects.create(user=student, name="Kept Person")
    event = CaptureEvent.all_objects.create(
        user=student, provider="postmark", provider_ref="<kept@example>",
        received_at=timezone.now(), status="applied",
    )

    services.regenerate_capture_address(student)

    assert Contact.objects.for_user(student).filter(pk=contact.pk).exists()
    kept = CaptureEvent.objects.for_user(student).get(pk=event.pk)
    assert kept.provider_ref == "<kept@example>"
    assert kept.status == "applied"


def test_it_records_a_product_event(student):
    services.regenerate_capture_address(student)
    assert ProductEvent.all_objects.filter(
        user=student, event="capture_address_regenerated"
    ).exists()


# ---------------------------------------------------------------------------
# The confirm-page contract
# ---------------------------------------------------------------------------
def test_settings_links_to_the_confirm_page_and_never_rotates_on_a_click(
    client, logged_in
):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse(REGENERATE) in body
    logged_in.refresh_from_db()
    assert logged_in.capture_slug == "oldslug1234"


def test_the_confirm_page_states_the_consequences_and_changes_nothing(
    client, logged_in
):
    resp = client.get(reverse(REGENERATE))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "stops working immediately" in body
    assert "untouched" in body
    assert "no undo" in body.lower()
    # A GET is not the action.
    logged_in.refresh_from_db()
    assert logged_in.capture_slug == "oldslug1234"


def test_posting_rotates_and_flashes_the_new_address(client, logged_in):
    resp = client.post(reverse(REGENERATE), follow=True)
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.capture_slug != "oldslug1234"
    # The flash carries the NEW address — a rotation the user can't read back
    # is a rotation they can't act on.
    assert services.capture_address(logged_in) in resp.content.decode()


def test_the_settings_page_shows_the_new_address_afterwards(client, logged_in):
    client.post(reverse(REGENERATE), follow=True)
    logged_in.refresh_from_db()
    body = client.get(reverse(SETTINGS)).content.decode()
    assert services.capture_address(logged_in) in body
    assert "u-oldslug1234@" not in body


def test_regeneration_requires_a_login(client):
    assert client.get(reverse(REGENERATE)).status_code == 302
    assert client.post(reverse(REGENERATE)).status_code == 302
