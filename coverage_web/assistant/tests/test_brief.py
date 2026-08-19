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


# ---------------------------------------------------------------------------
# The situation snapshot extends the SAME prompt — no second model call.
# ---------------------------------------------------------------------------
def _deadline_event(title="Summer Analyst", firm="Goldman Sachs", old="2026-08-01", new="2026-08-15"):
    return {
        "kind": "deadline_moved", "title": title, "firm": firm,
        "old_value": old, "new_value": new,
    }


def test_omitting_situation_entirely_keeps_the_old_queue_only_behaviour():
    """Callers written before build_situation existed (and every test above
    this one in the file) still work unmodified — `situation` defaults to
    None rather than being required."""
    user = _user()
    client = FakeClient(_response("Ada's Goldman deadline is Friday."))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Ada's Goldman deadline is Friday."


def test_a_situation_event_is_folded_into_the_same_prompt_not_a_second_call():
    user = _user()
    client = FakeClient(_response("Watch that deadline."))

    brief.get_or_build(user, [_action()], [_deadline_event()], client=client)

    assert len(client.messages.requests) == 1
    prompt = client.messages.requests[0]["messages"][0]["content"]
    assert "Summer Analyst" in prompt
    assert "Goldman Sachs" in prompt
    assert "2026-08-01" in prompt and "2026-08-15" in prompt


def test_situation_events_alone_with_an_empty_queue_still_spends_a_call():
    """An empty cadence queue used to mean "nothing worth saying" — but a
    deadline moving on a tracked role is worth saying even when the queue
    itself is empty."""
    user = _user()
    client = FakeClient(_response("Your Goldman deadline moved."))

    text = brief.get_or_build(user, [], [_deadline_event()], client=client)

    assert text == "Your Goldman deadline moved."
    assert len(client.messages.requests) == 1


def test_an_empty_queue_and_no_situation_events_still_spends_no_call():
    user = _user()
    client = FakeClient(_response("unused"))

    text = brief.get_or_build(user, [], [], client=client)

    assert text is None
    assert client.messages.requests == []


# ---------------------------------------------------------------------------
# Concurrency: crm.views.daily_brief is an htmx POST endpoint a student can
# fire twice (a double-load, two tabs on Today at once, a client-side retry
# after a slow response). Both requests can pass the "nothing cached yet"
# check before either has written its row — DailyBrief.date has a real
# UniqueConstraint(user, date) precisely to prevent two rows for one day, so
# the loser of the race must not crash.
# ---------------------------------------------------------------------------

class _RaceClient:
    """Like FakeClient, but the concurrent writer's row lands DURING this
    call's `messages.create` — simulating a second in-flight request that
    finishes first."""

    def __init__(self, response, user, date, winner_text="Other request won."):
        self.messages = self
        self.requests = []
        self._response = response
        self._user = user
        self._date = date
        self._winner_text = winner_text

    def create(self, **kwargs):
        self.requests.append(kwargs)
        DailyBrief.all_objects.create(
            user=self._user, date=self._date, text=self._winner_text,
        )
        return self._response


def test_a_concurrent_generation_does_not_crash_on_the_unique_constraint():
    """Two requests both see no cached row, both call the model; the second
    one to try to save must not blow up with an IntegrityError from
    `uniq_daily_brief_user_date` — it should quietly defer to whichever
    request actually won the race, the same way `debrief.dismiss`'s
    `get_or_create` already does for `ChatDebrief`."""
    user = _user()
    today = timezone.localdate()
    client = _RaceClient(_response("This request's own answer."), user, today)

    text = brief.get_or_build(user, [_action()], client=client)

    # Exactly one row for the day either way — the constraint's whole job —
    # and the caller gets text back rather than a 500.
    assert DailyBrief.objects.for_user(user).filter(date=today).count() == 1
    assert text is not None
