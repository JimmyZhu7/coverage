"""assistant.brief: the once-a-day advisor paragraph on the Today page.

`get_or_build` is deliberately never-raising and cache-first — these tests
pin both halves: the cache means at most one model call per student per
calendar day, and the try/except means a bad response, a network error, or
a dark API key degrades to "no card today" rather than a broken Today page.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from assistant import brief
from assistant.models import DailyBrief

User = get_user_model()

pytestmark = pytest.mark.django_db


def _user(email="brief@example.com"):
    return User.objects.create_user(email=email, password="pw12345!")


def _action(name="Ada Lovelace", firm="Goldman Sachs", label="Follow up",
            reason="idle 12 days", closes_on=None):
    return {
        "contact": {"name": name, "firm_text": firm},
        "label": label,
        "reason": reason,
        "closes_on": closes_on,
    }


class FakeMessages:
    def __init__(self, response_or_exception):
        self.response_or_exception = response_or_exception
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response_or_exception, Exception):
            raise self.response_or_exception
        return self.response_or_exception


class FakeClient:
    def __init__(self, response_or_exception):
        self.messages = FakeMessages(response_or_exception)


def _response(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def test_a_cached_brief_is_returned_without_calling_the_model():
    user = _user()
    DailyBrief(user=user, date=timezone.localdate(), text="Chen's idle 12 days.").save()
    client = FakeClient(_response("should never be used"))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Chen's idle 12 days."
    assert client.messages.requests == []


def test_an_empty_queue_returns_none_and_spends_no_call():
    user = _user()
    client = FakeClient(_response("unused"))

    text = brief.get_or_build(user, [], client=client)

    assert text is None
    assert client.messages.requests == []
    assert not DailyBrief.objects.for_user(user).exists()


def test_a_successful_generation_is_cached_for_the_day():
    user = _user()
    client = FakeClient(_response("Ada's Goldman deadline is Friday."))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Ada's Goldman deadline is Friday."
    row = DailyBrief.objects.for_user(user).get()
    assert row.date == timezone.localdate()
    assert row.text == "Ada's Goldman deadline is Friday."


def test_the_model_call_always_uses_the_cheap_tier():
    """Every plan gets the same brief model — this is bookkeeping copy, not
    the judgement call a student is on a paid plan for (see brief.py's own
    module docstring)."""
    user = _user()
    client = FakeClient(_response("Ada's deadline is Friday."))

    brief.get_or_build(user, [_action()], client=client)

    assert client.messages.requests[0]["model"] == brief.BRIEF_MODEL


def test_a_model_error_returns_none_and_writes_no_row():
    user = _user()
    client = FakeClient(RuntimeError("network blip"))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text is None
    assert not DailyBrief.objects.for_user(user).exists()


def test_an_empty_response_returns_none_and_writes_no_row():
    user = _user()
    client = FakeClient(_response(""))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text is None
    assert not DailyBrief.objects.for_user(user).exists()


def test_a_long_response_is_capped():
    user = _user()
    client = FakeClient(_response("x" * (brief.MAX_BRIEF_CHARS + 50)))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text is not None
    assert len(text) == brief.MAX_BRIEF_CHARS


def test_yesterdays_brief_does_not_leak_into_today():
    user = _user()
    DailyBrief(
        user=user,
        date=timezone.localdate() - timedelta(days=1),
        text="Stale from yesterday.",
    ).save()
    client = FakeClient(_response("Fresh brief for today."))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Fresh brief for today."
    assert DailyBrief.objects.for_user(user).count() == 2


def test_with_no_client_and_the_api_dark_it_returns_none(monkeypatch):
    """Same dark-by-default posture as every other optional integration.
    Force is_configured() False explicitly rather than trusting a blank
    ANTHROPIC_API_KEY in the environment — a machine with a real key set
    must not let this test silently spend a real call."""
    monkeypatch.setattr(brief, "is_configured", lambda: False)
    user = _user()

    text = brief.get_or_build(user, [_action()])

    assert text is None
    assert not DailyBrief.objects.for_user(user).exists()


def test_a_second_call_the_same_day_does_not_call_the_model_again():
    user = _user()
    client = FakeClient(_response("Ada's Goldman deadline is Friday."))

    first = brief.get_or_build(user, [_action()], client=client)
    second = brief.get_or_build(user, [_action()], client=client)

    assert first == second
    assert len(client.messages.requests) == 1
