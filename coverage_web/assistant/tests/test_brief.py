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
            reason="idle 12 days", closes_on=None, contact_id=None):
    contact = {"name": name, "firm_text": firm}
    if contact_id is not None:
        contact["id"] = contact_id
    return {
        "contact": contact,
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


def test_a_linked_contact_is_described_by_its_real_firm_not_no_firm_on_file():
    """Regression for the brief asserting the INVERSE of a student's own
    data, two review rounds in a row. A contact the CSV import matched to a
    directory firm has `firm_text` cleared (the linked firm is the source of
    truth) and the resolved name travels on the action as `firm_name`.
    Reading `firm_text` here made every correctly-linked contact read as
    "no firm on file" and the one UNMATCHED contact the only one carrying a
    name — so the model, reading wrong data correctly, told the student the
    broken contact was "the only one connected to a target firm".
    """
    linked = {
        "contact": {"name": "Maya Chen", "firm_text": ""},  # matched → cleared
        "firm_name": "Goldman Sachs",                      # resolved by cadence
        "label": "Follow up", "reason": "idle 12 days", "closes_on": None,
    }
    unmatched = {
        "contact": {"name": "Sam Okafor", "firm_text": "Bain and Company"},
        "firm_name": "Bain and Company",
        "label": "Follow up", "reason": "idle 9 days", "closes_on": None,
    }
    text = brief._summarize_actions([linked, unmatched])
    assert "Maya Chen (Goldman Sachs)" in text
    assert "no firm on file" not in text
    assert "Sam Okafor (Bain and Company)" in text


def test_a_contact_with_no_firm_anywhere_still_says_so_honestly():
    """The fallback chain still bottoms out honestly when there genuinely
    is no firm: no linked firm, no free text, no resolved name."""
    bare = {
        "contact": {"name": "Pat Doe", "firm_text": ""},
        "label": "Follow up", "reason": "", "closes_on": None,
    }
    assert "Pat Doe (no firm on file)" in brief._summarize_actions([bare])


def test_a_cached_brief_is_returned_without_calling_the_model():
    user = _user()
    DailyBrief(user=user, date=timezone.localdate(), text="Chen's idle 12 days.").save()
    client = FakeClient(_response("should never be used"))

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Chen's idle 12 days."
    assert client.messages.requests == []


def test_an_empty_queue_still_returns_a_cached_quiet_day_line():
    """A quiet day is still a day Today gets opened — see
    assistant.brief._QUIET_DAY_MESSAGES. No model call, but the card must
    not just disappear."""
    user = _user()
    client = FakeClient(_response("unused"))

    text = brief.get_or_build(user, [], client=client)

    assert text in brief._QUIET_DAY_MESSAGES
    assert client.messages.requests == []
    row = DailyBrief.objects.for_user(user).get()
    assert row.text == text
    assert row.contact_ids == []


def test_the_quiet_day_line_is_the_same_for_two_requests_the_same_day():
    """Deterministic per (user, date), not random — two tabs open on a
    quiet Today must not each render a different sentence before the
    cached row settles."""
    user = _user()

    first = brief.get_or_build(user, [], client=FakeClient(_response("unused")))
    DailyBrief.objects.for_user(user).delete()
    second = brief.get_or_build(user, [], client=FakeClient(_response("unused")))

    assert first == second


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


def test_a_concurrent_winner_is_returned_instead_of_500ing():
    """`DailyBrief` carries a real UniqueConstraint on (user, date) — exactly
    the guard a check-then-create sequence needs when two requests for the
    same student's brief overlap (the new async crm.views.daily_brief
    endpoint made this realistic: two tabs open on Today at once, or a
    retried POST, can both find `existing is None` and both reach the
    model). Without handling the constraint's own IntegrityError, the
    request that loses the race 500s on its own `.save()` instead of
    simply returning the winner's already-cached text — the exact
    "even a replayed POST costs nothing after the first" guarantee
    crm.views.daily_brief's docstring claims but this path never
    actually honoured.

    A `FakeClient` that writes the CONCURRENT winner's row from inside its
    own `.create()` call stands in for the wall-clock gap between our
    `existing is None` check and our own save — the same gap two real
    overlapping requests would race across.
    """
    user = _user()

    class RacyMessages:
        def create(self, **kwargs):
            DailyBrief(user=user, date=timezone.localdate(), text="Winner's brief.").save()
            return _response("Our own brief, arriving second.")

    client = SimpleNamespace(messages=RacyMessages())

    text = brief.get_or_build(user, [_action()], client=client)

    assert text == "Winner's brief."
    assert DailyBrief.objects.for_user(user).count() == 1


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


def test_prompt_states_todays_date_so_relative_timing_is_not_a_guess():
    """Regression: the prompt handed the model a `closes_on` date (e.g.
    `2026-08-30`) but never said what today is, so the model had nothing to
    diff it against but its own training cutoff — observed live as a Today's
    Move card claiming a role "closes in under two years" when the queue's
    own deadline chip, computed from the real date, said 3 days. Anchoring
    today's date in the prompt is what makes "closes in 3 days" the only
    number the model can produce.
    """
    user = _user()
    client = FakeClient(_response("Reach out to Ada before Friday."))

    brief.get_or_build(user, [_action(closes_on="2026-08-30")], client=client)

    prompt = client.messages.requests[0]["messages"][0]["content"]
    assert timezone.localdate().isoformat() in prompt


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

    assert text in brief._QUIET_DAY_MESSAGES
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
        DailyBrief.objects.for_user(self._user).create(
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


# ---------------------------------------------------------------------------
# Staleness: a cached brief can name someone the queue has since disowned.
#
# THE LIVE CASE (2026-08-27, the founder's own account). 18:14 UTC: the brief
# generates while Anant Taparia (Citi) and Xinyi Xu (Morgan Stanley) are both
# `thread_state=replied` — cadence branch 7 is correct to say "they replied,
# propose a 15-min chat", and the brief repeats it. 23:04 UTC: he parks both
# by hand from the Today queue — a deliberate exit, `crm.today`'s cadence
# branch 4 and `_gate_and_rank` now agree neither produces any action at all.
# Nothing told the cached `DailyBrief` row that the sentence it wrote five
# hours earlier no longer described the queue. The Today page kept telling
# him to "respond to Anant Taparia... and Xinyi Xu... immediately" against
# two people he had just told the product, twice, to leave alone.
# ---------------------------------------------------------------------------

ANANT_ID, XINYI_ID = 741, 740


def _founders_queue_before_parking():
    """The two real actions cadence branch 7 produced pre-park — `advance`,
    "they replied — propose a 15-min chat" — shaped exactly like
    `crm.today._build_actions` hands them to `_summarize_actions`."""
    return [
        _action("Anant Taparia", "Citi", "Propose a chat",
                "they replied — propose a 15-min chat", contact_id=ANANT_ID),
        _action("Xinyi Xu", "Morgan Stanley", "Propose a chat",
                "they replied — propose a 15-min chat", contact_id=XINYI_ID),
    ]


def test_fails_before_the_fix_a_cached_brief_still_chases_a_contact_just_parked():
    """Pin the exact regression. Before `_is_stale` existed, `get_or_build`
    returned `existing.text` unconditionally whenever a row for today was
    found — this test's FIRST assertion is what failed against that code
    (confirmed by stashing the fix and re-running: the cached text won
    unconditionally, `client.messages.requests` stayed empty, and the fresh
    "no one to chase" queue never got a say)."""
    user = _user()
    client = FakeClient(_response(
        "Respond to **Anant Taparia** at Citi and Xinyi Xu at Morgan Stanley "
        "immediately by proposing those 15-minute chats before they lose "
        "interest."
    ))
    # 18:14 generation, thread_state=replied for both.
    first = brief.get_or_build(user, _founders_queue_before_parking(), client=client)
    assert "Anant Taparia" in first
    row = DailyBrief.objects.for_user(user).get()
    assert sorted(row.contact_ids) == sorted([ANANT_ID, XINYI_ID])

    # 23:04: both parked. crm.today's own engine/gate now produce nothing for
    # either of them; the rest of the queue is untouched (one unrelated cold
    # contact still due a follow-up) — the same shape `crm.views.daily_brief`
    # would compute and pass in for real.
    client.messages.requests.clear()
    client.messages.response_or_exception = _response(
        "Follow up with Priya Nair at Bain — no reply after 9 business days."
    )
    fresh_queue = [_action("Priya Nair", "Bain", "Follow up",
                            "no reply after 9 business days", contact_id=999)]
    second = brief.get_or_build(user, fresh_queue, client=client)

    # The fix: the stale sentence about two parked contacts must not survive,
    # and regenerating it is what a correct fix has to spend the second call
    # on. Both assertions fail on the pre-fix code — see the docstring above.
    assert client.messages.requests, "a stale row naming a parked contact must trigger a refresh"
    assert "Anant Taparia" not in second
    assert "Xinyi Xu" not in second


def test_a_cached_brief_survives_when_every_named_contact_is_still_in_the_queue():
    """The non-regression half: an ordinary day where nothing named in the
    brief has moved must still cost exactly one call, same as before this
    fix — staleness is opt-in to a REAL disappearance, not a standing tax on
    every cache hit."""
    user = _user()
    client = FakeClient(_response("Respond to **Anant Taparia** at Citi."))
    brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    client.messages.requests.clear()
    second = brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    assert second == "Respond to **Anant Taparia** at Citi."
    assert client.messages.requests == []


def test_get_cached_hides_a_stale_brief_without_spending_a_call():
    """`get_cached` never calls the model (see its own docstring) — it can
    only say "not usable", not fix it. `crm.today.week` is the caller that
    matters here: it renders whatever `get_cached` hands back with no
    generation of its own, so this is the function standing between the
    founder's screen and the stale sentence on an ordinary page load, not
    just on the next `get_or_build` call."""
    user = _user()
    client = FakeClient(_response("Respond to **Anant Taparia** at Citi."))
    brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    # Anant Taparia parked; Xinyi Xu untouched — one of the two named
    # contacts leaving the queue is enough to distrust the whole sentence.
    still_live = [_action("Xinyi Xu", "Morgan Stanley", contact_id=XINYI_ID)]
    assert brief.get_cached(user, still_live) is None
    # Without a fresh queue to check against, the old contract holds — a
    # caller that hasn't been updated to pass `actions` still gets the row
    # exactly as written, never a surprise None.
    assert brief.get_cached(user) == "Respond to **Anant Taparia** at Citi."


def test_is_pending_reopens_once_a_named_contact_is_parked(monkeypatch):
    """`crm.today.week` draws the htmx placeholder off `is_pending` — this is
    what actually gets the founder's Today page to ask `crm.views.daily_brief`
    for a fresh sentence instead of silently keeping the stale one on screen
    for the rest of the day.

    `is_configured` forced True (same reason as
    `test_with_no_client_and_the_api_dark_it_returns_none`): `is_pending`
    gates on it directly and a real key on the test machine must not be what
    makes this pass or fail.

    The "parked" queue here is a REAL one that simply no longer contains the
    named contact, not `[]`. It used to be `[]`, which passed for the wrong
    reason: an empty list is not "Anant was parked", it is "there is no
    queue today", and `_is_stale` deliberately stopped treating those as the
    same thing (see `test_an_empty_queue_does_not_invalidate_a_still_true
    _brief`). Expressed the way the sibling test at
    `test_get_cached_hides_a_stale_brief_without_spending_a_call` already
    does it, this pins the behaviour the name actually claims."""
    monkeypatch.setattr(brief, "is_configured", lambda: True)
    user = _user()
    client = FakeClient(_response("Respond to **Anant Taparia** at Citi."))
    brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    # Anant parked; the rest of the day's queue is untouched.
    after_parking = [_action("Priya Nair", "Bain", "Follow up",
                             "no reply after 9 business days", contact_id=999)]
    assert brief.is_pending(user, after_parking) is True
    assert brief.is_pending(user, _founders_queue_before_parking()) is False


def test_reply_after_park_is_never_treated_as_stale():
    """The ratchet un-parks on `reply_received` because an inbound reply is a
    real event (`coverage_domain.pipeline.TOUCH_TRANSITIONS`; only `advocate`
    is terminal) — a contact who writes back after being parked is back in
    the fresh `actions` list by the time anyone asks, so `_is_stale` never
    has a reason to fire for them. This pins that the fix does not
    accidentally suppress that signal."""
    user = _user()
    client = FakeClient(_response("Respond to **Anant Taparia** at Citi."))
    brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    client.messages.requests.clear()
    # Anant Taparia parked, then replied again — thread_state ratchets to
    # `replied` and cadence branch 7 puts him straight back in the queue.
    same_queue = _founders_queue_before_parking()
    second = brief.get_or_build(user, same_queue, client=client)

    assert second == "Respond to **Anant Taparia** at Citi."
    assert client.messages.requests == []


def test_an_empty_queue_does_not_invalidate_a_still_true_brief():
    """FAILS BEFORE THE FIX. `_live_contact_ids([])` is the empty set, so
    every brief naming anyone failed the subset test the instant the queue
    emptied — and an empty queue is not evidence that the person named was
    overruled, it is the absence of anything to check against.

    Measured on the founder's own account, 2026-08-29: queue at zero, that
    morning's brief named Katy Chen (`chat_done`, i.e. the chat HAPPENED)
    about a Nomura deadline that had not moved, and Today threw it away.
    Worse than a wrong sentence, it produced NO sentence: staleness only
    pays for itself when a better brief can replace the discarded one, and
    with no actions `get_or_build` has nothing to write from, so the slot
    renders empty on exactly the day the brief is the only thing on the
    page.

    Distinct from `..._survives_when_every_named_contact_is_still_in_the
    _queue` above, which pins a NON-empty queue that still contains
    everyone. This pins the zero case, which took the opposite branch."""
    user = _user()
    client = FakeClient(_response("Respond to **Anant Taparia** at Citi."))
    brief.get_or_build(user, _founders_queue_before_parking(), client=client)

    # The student cleared their queue. Nothing was parked or excluded;
    # there is simply no work due today.
    assert brief.get_cached(user, []) == "Respond to **Anant Taparia** at Citi."
    # And the Today page must not fall back to the lazy-load placeholder,
    # which would POST, generate nothing from an empty queue, and leave a
    # blank where the brief was.
    assert brief.is_pending(user, []) is False
