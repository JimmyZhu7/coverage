"""How the calendar renders a row mirrored from a connected Google Calendar.

One rule, tested from both sides: Coverage holds a VIEW-ONLY grant, so the
page must not offer to change what it cannot change. A Remove button on a
mirrored row would delete the local copy, the next `gcal_sync` would read
the event straight back out of Google, and the student would learn that
this page's controls do not stick — the same ghost loop
`_retire_cancelled_chat` refused outright deletion to avoid.

The template hiding the button and the view refusing the delete are two
halves of the same guarantee, not a belt and a second opinion: the template
is what a student sees, and the view is what actually holds when a stale
form is replayed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import CalendarEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="grid@example.com", password="x")


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return user


def _event(user, **kwargs):
    defaults = {
        "user": user,
        "title": "Superday",
        # Mid-month so the month grid definitely contains it whatever today
        # is, and near enough to now that the default view lands on it.
        "starts_at": timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0),
        "source": CalendarEvent.SOURCE_GCAL,
        "external_id": "g1",
    }
    defaults.update(kwargs)
    return CalendarEvent.all_objects.create(**defaults)


class TestTheGridTellsTheTruthAboutAMirroredRow:
    def test_a_mirrored_row_offers_no_remove_button(self, client, logged_in):
        _event(logged_in)

        html = client.get(reverse("crm:calendar")).content.decode()

        assert "Superday" in html
        assert reverse("crm:calendar_delete", args=[1]) not in html

    def test_it_says_where_to_change_it_instead(self, client, logged_in):
        _event(logged_in)

        html = client.get(reverse("crm:calendar")).content.decode()

        assert "From your Google Calendar" in html

    def test_a_typed_event_still_has_its_remove_button(self, client, logged_in):
        typed = _event(
            logged_in, source=CalendarEvent.SOURCE_MANUAL, external_id="",
            title="Flight to HK",
        )

        html = client.get(reverse("crm:calendar")).content.decode()

        assert reverse("crm:calendar_delete", args=[typed.id]) in html


class TestTheViewHoldsTheSameLine:
    def test_a_replayed_delete_cannot_remove_a_mirrored_row(self, client, logged_in):
        """Unreachable from the UI, so anyone hitting it is replaying a stale
        form. Silent rather than an error: the redirect shows the row still
        present, which is the truth."""
        event = _event(logged_in)

        response = client.post(reverse("crm:calendar_delete", args=[event.id]))

        assert response.status_code == 302
        assert CalendarEvent.all_objects.filter(pk=event.pk).exists()

    def test_a_typed_event_is_still_deletable(self, client, logged_in):
        event = _event(logged_in, source=CalendarEvent.SOURCE_MANUAL, external_id="")

        client.post(reverse("crm:calendar_delete", args=[event.id]))

        assert not CalendarEvent.all_objects.filter(pk=event.pk).exists()

    def test_a_captured_chat_is_still_deletable(self, client, logged_in):
        """The mailbox path's rows are the user's to remove: unlike a Google
        event, there is no upstream copy for a sync to restore."""
        event = _event(
            logged_in, source=CalendarEvent.SOURCE_CAPTURE, external_id="",
            thread_id="thread-1", kind=CalendarEvent.KIND_CHAT,
        )

        client.post(reverse("crm:calendar_delete", args=[event.id]))

        assert not CalendarEvent.all_objects.filter(pk=event.pk).exists()


class TestAnAdoptedRowCountsAsMirroredToo:
    """One chat that arrived BOTH as an invite in the mailbox and on the
    student's Google Calendar is a single row: `capture.gcal_live` joins them
    by iCalUID. That row keeps `source="capture"` — the mail pipeline goes on
    maintaining it — but it is still backed by a live Google event, so
    Remove is still a button that could not stick. The rule is `external_id`,
    not `source`."""

    def _adopted(self, user):
        return _event(
            user,
            title="Chat with Jane Banker",
            source=CalendarEvent.SOURCE_CAPTURE,
            kind=CalendarEvent.KIND_CHAT,
            thread_id="thread-1",
            ics_uid="uid-abc@google.com",
            external_id="g1",
        )

    def test_it_offers_no_remove_button_either(self, client, logged_in):
        event = self._adopted(logged_in)

        html = client.get(reverse("crm:calendar")).content.decode()

        assert "Chat with Jane Banker" in html
        assert reverse("crm:calendar_delete", args=[event.id]) not in html

    def test_and_the_view_refuses_a_replayed_delete(self, client, logged_in):
        event = self._adopted(logged_in)

        client.post(reverse("crm:calendar_delete", args=[event.id]))

        assert CalendarEvent.all_objects.filter(pk=event.pk).exists()
