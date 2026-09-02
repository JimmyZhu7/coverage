"""Branch 5a: the post-chat promised-action follow-up (WS-CRM-07, C6).

The one cadence split the sources support is on RELATIONSHIP STATE and on
nothing else (`research-networking-norms.md §8d`): cold with no reply about
two weeks, post-chat WITH A PROMISED ACTION about one week, post-chat with
nothing promised six weeks or more and event-triggered. §8a and §8f of the
same file looked for a track or seniority conditioning and found none, so
there is deliberately no second axis in here to test.

The pinned properties:
  - the branch fires ONLY when a promise is on the contact dict;
  - a chat with nothing promised still falls to branch 5b at whatever
    `chatted_touch_min_weeks` says, and the founder's 6 is unmoved;
  - a promise younger than the window produces no card at all, not a
    keep-warm card standing in for one;
  - an undateable promise still surfaces, without inventing a number.
"""

from datetime import date, datetime, timezone

from coverage_domain import cadence

UTC = timezone.utc
TODAY = date(2026, 7, 22)
AS_OF = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
FIRMS = {"usfirm": {"name": "US Firm", "tier": 1}}

# The founder's own dial (`coverage-keepwarm-6-weeks-deliberate`), used as the
# params in every test below so that "the promise branch fires and keep-warm
# does not" is asserted against the value he actually runs.
FOUNDER = {"chatted_touch_min_weeks": 6}


def _chatted(cid, **kw):
    base = dict(
        id=cid, firm_id="usfirm", warmth="chatted", thread_state="chat_done",
        source=None,
    )
    base.update(kw)
    return base


def _touch(cid, kind, day):
    return dict(
        contact_id=cid, kind=kind,
        ts=datetime.combine(day, datetime.min.time()).replace(tzinfo=UTC),
        channel="email", note=None,
    )


def _run(contacts, touches, params=FOUNDER):
    return cadence.due_actions(
        contacts, touches, [], as_of=AS_OF, firms=FIRMS, params=params,
    )


def test_a_promise_older_than_the_window_asks_the_student_to_chase_it():
    """Eight days after the chat, past the 7-day default."""
    chat_day = date(2026, 7, 14)
    acts = _run(
        [_chatted(1, promised_action="an intro to Dana Reed",
                  promised_action_at=chat_day)],
        [_touch(1, "chat", chat_day), _touch(1, "thank_you", date(2026, 7, 15))],
    )
    assert [a["action"] for a in acts] == ["promised_followup"]
    a = acts[0]
    assert "an intro to Dana Reed" in a["reason"]
    assert "8d ago" in a["reason"]
    assert a["priority"] == 1
    assert a["ctx"]["days_since"] == 8
    assert a["ctx"]["target_days"] == 7


def test_a_promise_inside_the_window_produces_no_card_at_all():
    """Not a keep-warm card standing in for the one that is not due yet.

    Branch 5a `continue`s exactly as 5b does. A keep-warm card here would be
    the wrong ask about the same person on the same day: "send an update" is
    not "chase the intro they offered you four days ago".
    """
    chat_day = date(2026, 7, 18)
    acts = _run(
        [_chatted(1, promised_action="an intro to Dana Reed",
                  promised_action_at=chat_day)],
        [_touch(1, "chat", chat_day), _touch(1, "thank_you", chat_day)],
    )
    assert acts == []


def test_a_chat_with_nothing_promised_still_falls_to_keep_warm_at_six_weeks():
    """The founder's `chatted_touch_min_weeks=6` is untouched by this branch.

    43 days since the last touch, so the 6-week keep-warm is due and the
    promise branch is dark because there is no promise. This is the exact
    pairing the plan asks for: the new interval must not have shortened the
    old one, and the old one must still be reachable.
    """
    acts = _run(
        [_chatted(1)],
        [_touch(1, "chat", date(2026, 6, 9))],
    )
    assert [a["action"] for a in acts] == ["keep_warm"]
    assert "target every 6 weeks" in acts[0]["reason"]


def test_a_chat_with_nothing_promised_is_silent_before_six_weeks():
    """29 days: past the 3-week default, short of the founder's 6."""
    acts = _run([_chatted(1)], [_touch(1, "chat", date(2026, 6, 23))])
    assert acts == []


def test_the_default_keep_warm_window_is_still_three_weeks():
    """P3/E3: the branch must not have moved the shipped default either."""
    assert cadence.CADENCE_DEFAULTS["chatted_touch_min_weeks"] == 3
    assert cadence.CADENCE_DEFAULTS["promised_followup_after_days"] == 7
    acts = _run([_chatted(1)], [_touch(1, "chat", date(2026, 6, 23))], params={})
    assert [a["action"] for a in acts] == ["keep_warm"]


def test_a_blank_promise_string_is_no_promise():
    """The caller passes "" for every contact with nothing open, so an empty
    string must be indistinguishable from an absent key."""
    acts = _run(
        [_chatted(1, promised_action="", promised_action_at=None)],
        [_touch(1, "chat", date(2026, 6, 9))],
    )
    assert [a["action"] for a in acts] == ["keep_warm"]


def test_an_undateable_promise_surfaces_without_inventing_a_number():
    """Same posture as every other clock in this module: say what is known."""
    acts = _run(
        [_chatted(1, promised_action="an intro to Dana Reed")],
        [_touch(1, "chat", date(2026, 7, 14))],
    )
    assert [a["action"] for a in acts] == ["promised_followup"]
    assert "no date on record" in acts[0]["reason"]
    assert acts[0]["ctx"]["days_since"] is None


def test_the_branch_is_scoped_to_chatted_contacts():
    """A cold contact carrying a promise (which the caller never produces)
    still runs the cold tree, so a stray key can never reroute branch 6."""
    acts = _run(
        [dict(id=1, firm_id="usfirm", warmth="cold", thread_state="no_reply",
              promised_action="an intro to Dana Reed",
              promised_action_at=date(2026, 6, 1))],
        [_touch(1, "outreach", date(2026, 6, 1))],
    )
    assert [a["action"] for a in acts] == ["park"]


def test_a_replied_thread_still_belongs_to_branch_seven():
    """Branch 7 owns thread_state 'replied' and says something strictly
    better; 5a must not have taken it."""
    acts = _run(
        [_chatted(1, thread_state="replied",
                  promised_action="an intro to Dana Reed",
                  promised_action_at=date(2026, 7, 1))],
        [_touch(1, "reply_received", date(2026, 7, 1))],
    )
    assert [a["action"] for a in acts] == ["advance"]


def test_an_advocate_with_a_promise_still_gets_the_advocate_branch():
    """Branch 5 returns before 5a, unchanged."""
    acts = _run(
        [_chatted(1, warmth="advocate",
                  promised_action="an intro to Dana Reed",
                  promised_action_at=date(2026, 6, 1))],
        [_touch(1, "chat", date(2026, 6, 1))],
    )
    assert [a["action"] for a in acts] == ["maintain"]


def test_the_window_is_a_parameter_not_a_literal():
    chat_day = date(2026, 7, 19)
    contacts = [_chatted(1, promised_action="an intro to Dana Reed",
                         promised_action_at=chat_day)]
    touches = [_touch(1, "chat", chat_day), _touch(1, "thank_you", chat_day)]
    assert _run(contacts, touches) == []
    tuned = _run(contacts, touches, params={**FOUNDER,
                                            "promised_followup_after_days": 3})
    assert [a["action"] for a in tuned] == ["promised_followup"]
