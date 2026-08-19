"""billing/views.py::waitlist_join — the Pro card's "Notify me when Pro
opens" control AND the Team card's "Run a club? Notify me"
(templates/core/pricing.html, both rendering core/_waitlist_form.html). Both
tiers are stamped "In the works" with no checkout button; this is where those
intents actually go, kept apart by `source`.
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


class TestWaitlistIntents:
    """Team shipped with `<a href="#notify" aria-disabled="true">` pointing at
    an anchor that exists nowhere on the page, and this view hard-coded
    `source="pricing_page"`. Both halves had to move: a shared control, and a
    key that can hold two intents from one person."""

    def test_the_team_card_records_its_own_intent(self, client):
        with patch("billing.views.record_event") as mock_record:
            client.post(reverse("billing:waitlist_join"),
                        {"email": "officer@example.com", "source": "pricing_page_team"})

        entry = ProWaitlist.all_objects.get(email="officer@example.com")
        assert entry.source == "pricing_page_team"
        assert mock_record.call_args.kwargs["source"] == "pricing_page_team"

    def test_one_person_can_want_pro_and_run_a_club(self, client):
        """The reason the unique key is (email, source). Keyed on email alone
        the second ask is a silent `get_or_create` no-op and the Team list
        never learns this person exists."""
        client.post(reverse("billing:waitlist_join"),
                    {"email": "both@example.com", "source": "pricing_page"})
        client.post(reverse("billing:waitlist_join"),
                    {"email": "both@example.com", "source": "pricing_page_team"})

        assert set(ProWaitlist.all_objects.filter(email="both@example.com")
                   .values_list("source", flat=True)) == {"pricing_page", "pricing_page_team"}

    def test_a_repeat_of_the_SAME_intent_is_still_deduped(self, client):
        for _ in range(2):
            client.post(reverse("billing:waitlist_join"),
                        {"email": "twice@example.com", "source": "pricing_page_team"})
        assert ProWaitlist.all_objects.filter(email="twice@example.com").count() == 1

    def test_an_unknown_source_falls_back_to_pro_rather_than_being_stored(self, client):
        """`source` is what the list is segmented on, so it is an allowlist,
        not a pass-through — otherwise the first script to post arbitrary
        strings makes the field mean nothing. The visitor still keeps their
        place: a tampered hidden input is not their fault."""
        client.post(reverse("billing:waitlist_join"),
                    {"email": "tampered@example.com", "source": "'; DROP TABLE --"})

        entry = ProWaitlist.all_objects.get(email="tampered@example.com")
        assert entry.source == "pricing_page"


class TestPricingPageControls:
    def test_both_cards_post_to_the_waitlist_and_neither_is_a_dead_anchor(self, client):
        body = client.get(reverse("core:pricing")).content.decode()
        assert 'value="pricing_page"' in body
        assert 'value="pricing_page_team"' in body
        assert "Run a club? Notify me" in body
        # The anchor the Team CTA used to point at never existed on this page.
        assert 'href="#notify"' not in body


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
