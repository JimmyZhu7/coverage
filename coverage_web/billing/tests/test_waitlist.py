"""billing/views.py::waitlist_join — the Pro card's "Notify me when Pro
opens" control (templates/core/pricing.html). Pro is stamped "In the works"
with no checkout button; this is where that intent actually goes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from billing.models import ProWaitlist

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_cache():
    """The waitlist throttle lives in the cache — reset it between tests so
    one test's requests never bleed into the next's rate-limit count."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def student():
    return User.objects.create_user(email="waitlist-student@example.com", password="x")


class TestWaitlistJoinLoggedOut:
    def test_a_valid_email_joins_the_waitlist(self, client):
        with patch("billing.views.record_event") as mock_record:
            resp = client.post(reverse("billing:waitlist_join"), {"email": "future-user@example.com"})

        assert resp.status_code == 302
        entry = ProWaitlist.all_objects.get(email="future-user@example.com")
        assert entry.user is None
        assert entry.source == "pricing_page"
        mock_record.assert_called_once()
        assert mock_record.call_args.args[0] == "pro_waitlist_joined"

    def test_email_is_lowercased_before_storing(self, client):
        client.post(reverse("billing:waitlist_join"), {"email": "Mixed.Case@Example.COM"})
        assert ProWaitlist.all_objects.filter(email="mixed.case@example.com").exists()

    def test_an_invalid_email_is_rejected_without_creating_a_row(self, client):
        resp = client.post(reverse("billing:waitlist_join"), {"email": "not-an-email"})

        assert resp.status_code == 302
        assert ProWaitlist.all_objects.count() == 0

    def test_a_missing_email_is_rejected(self, client):
        resp = client.post(reverse("billing:waitlist_join"), {})

        assert resp.status_code == 302
        assert ProWaitlist.all_objects.count() == 0

    def test_a_second_join_with_the_same_email_is_deduped_not_duplicated(self, client):
        client.post(reverse("billing:waitlist_join"), {"email": "repeat@example.com"})
        with patch("billing.views.record_event") as mock_record:
            client.post(reverse("billing:waitlist_join"), {"email": "repeat@example.com"})

        assert ProWaitlist.all_objects.filter(email="repeat@example.com").count() == 1
        # The event only fires for the FIRST join — a repeat is a no-op read,
        # not a second funnel signal.
        mock_record.assert_not_called()

    def test_get_is_not_allowed(self, client):
        resp = client.get(reverse("billing:waitlist_join"))
        assert resp.status_code == 405


class TestWaitlistJoinLoggedIn:
    def test_a_logged_in_user_can_join_with_no_email_field_using_their_account_email(
        self, client, student
    ):
        client.force_login(student)
        client.post(reverse("billing:waitlist_join"), {})

        entry = ProWaitlist.all_objects.get(email=student.email)
        assert entry.user_id == student.id

    def test_a_logged_in_user_can_still_override_with_a_different_email(self, client, student):
        client.force_login(student)
        client.post(reverse("billing:waitlist_join"), {"email": "alt@example.com"})

        entry = ProWaitlist.all_objects.get(email="alt@example.com")
        assert entry.user_id == student.id


class TestWaitlistJoinThrottle:
    def test_repeated_requests_from_the_same_ip_are_eventually_throttled(self, client):
        for i in range(5):
            resp = client.post(reverse("billing:waitlist_join"), {"email": f"burst{i}@example.com"})
            assert resp.status_code == 302

        # The 6th request in the window is throttled — no row for it either.
        client.post(reverse("billing:waitlist_join"), {"email": "burst-blocked@example.com"})
        assert not ProWaitlist.all_objects.filter(email="burst-blocked@example.com").exists()
        # The first five went through.
        assert ProWaitlist.all_objects.count() == 5
