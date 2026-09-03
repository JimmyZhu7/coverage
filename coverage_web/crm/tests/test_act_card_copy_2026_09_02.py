"""Today's queue card, 2026-09-02 — every card, what it may not say again.

THE COMPLAINT, in the founder's words, about a KEEP WARM card and explicitly
about all of them: "refine and concise info presented here, short, concise,
clean and informative. This is Today's page under Move it forward but do it
for all cards like Move it forward."

The card he was reading:

    KEEP WARM
    chatted
    Tier 1 target, and you have already had the conversation.
    A role there closes Sep 30.
    Last: Chat happened · 24 business days ago

Five surfaces, three of them saying `warmth == "chatted"`.

THE RULE, and it is `test_rail_copy_2026_09_02.py`'s rule one surface over:
KEEP EVERY FACT, CUT EVERY RESTATEMENT. A card sentence may not repeat what
the badge above it says, what the warmth chip beside it says, what the
deadline chip beside it says, what the buttons under it say, or what the
ledger row below it says. So the assertions here come in PAIRS: each one that
forbids a phrase is followed by one pinning the fact that phrase carried, in
whichever element now carries it — because the failure mode of a copy diet is
not a sentence surviving, it is a fact leaving with it.

ONE FACT MOVED OFF THE FACE, and only one: the warmth gloss ("you have
already had the conversation", "they would vouch for you"). It is the warmth
chip's `title` now (`crm.relevance.WARMTH_NOTE`), which is where this page has
put a fact-about-a-label since the rail pass earlier the same night.

WHAT THE CARDS BECAME, measured read-only on the founder's live account and on
demo, sentence and ledger row, before -> after:

    Follow up        No reply 7 business days after touch 1. Follow up.   (50)
                  -> No reply to your first note.                         (28)
                     Last: Reached out · 7 business days ago              (38)
                  -> Reached out · 7 business days                        (29)

    Keep warm        Tier 1 target, and you have already had the
                     conversation. A role there closes Sep 30.            (85)
                  -> Tier 1 target. A role there closes Sep 30.           (42)
                     Last: Chat happened · 24 business days ago           (41)
                  -> Chat happened · 24 business days                     (32)

    Reply            They wrote to you. Answer the note. Recruiting
                     contact, not a coffee chat.                          (74)
                  -> Recruiting contact, not a coffee chat.               (38)

    First outreach   Added but never contacted. Send the first note.      (47)
                  -> Tier 2 target.                                       (14)
                     No touches on record                                 (20)
                  -> No touches yet                                       (14)

    Propose a chat   They replied. Propose a 15-min chat.                 (36)
                  -> Ask for 15 minutes.                                  (19)

    Keep warm        Tier 2 target, and you have already had the
                     conversation.                                        (57)
                  -> Tier 2 target.                                       (14)

    Keep warm        Same school, and they would vouch for you.           (42)
                  -> Same school.                                         (12)

Headless Playwright at 1280x800 and 375x812, both colour schemes, against the
demo account. Act card heights, and how many card sentences wrapped past one
line, before -> after: see the commit message. A Django test client has no
layout engine, so those numbers are quoted rather than re-run; what is checked
here is the copy and the markup that produce them.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import relevance as rel
from crm.models import Contact, Touch, UserFirm
from crm.today import _build_actions
from directory.models import Firm, FirmDate, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------------------
# Fixtures. Deliberately small and one card each: every assertion below is
# about ONE card's sentence, and a fixture producing two of them lets an
# assertion pass on the wrong one.
# ---------------------------------------------------------------------------
def _user(email="card@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw)


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _target_firm(user, slug="nomura", name="Nomura", tier=1):
    firm = Firm.objects.create(slug=slug, name=name)
    UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    return firm


def _only(user) -> dict:
    """The account's one and only card."""
    actions, _ = _build_actions(user)
    assert len(actions) == 1, [(a["action"], a["contact"]["name"]) for a in actions]
    return actions[0]


def _footer(a: dict) -> str:
    """The ledger row as `_act_card.html` renders it, from the same keys.

    Kept here rather than read off the page so the sentence tests can run
    without a request; the markup itself is pinned in section 7 below.
    """
    if not a.get("last_kind"):
        return "No touches yet"
    n = a["last_business_days"]
    return f"{a['last_kind']} · {n} business day{'' if n == 1 else 's'}"


def _bd(days_ago: int) -> int:
    """Business days between a touch `days_ago` calendar days back and today.

    Computed, never spelled out: `business_days_since` counts Mon-Fri, so
    every literal in this file would be a different number depending on which
    weekday the suite ran. It is the ENGINE's own helper, which is also the
    point — the card's number and the cadence's number are one number, and a
    hardcoded expectation here would stop checking that.
    """
    from coverage_domain import cadence
    today = timezone.localdate()
    return cadence.business_days_since(today - timedelta(days=days_ago), today)


def _page(user) -> str:
    from django.test import Client
    client = Client()
    client.force_login(user)
    res = client.get(reverse("crm:week"))
    assert res.status_code == 200
    return res.content.decode()


def _reasons(html: str) -> list[str]:
    return [
        " ".join(m.split())
        for m in re.findall(r'<p class="act-reason">(.*?)</p>', html, re.S)
    ]


# ---------------------------------------------------------------------------
# 1. Follow up — the founder's own nineteen cards.
# ---------------------------------------------------------------------------
def _followup_user(email="fu@example.com"):
    user = _user(email=email)
    firm = _target_firm(user)
    c = Contact.all_objects.create(user=user, name="Jonathan Elsman", firm=firm)
    _touch(user, c, "outreach", days_ago=10)
    return user


def test_the_follow_up_card_stops_counting_the_days_its_own_row_counts():
    """BEFORE: "No reply 7 business days after touch 1. Follow up."

    Two of its three facts were printed twice on the same card. "7 business
    days" is the ledger row's number in the ledger row's unit, one line down.
    "Follow up." is the badge directly above, and the row's primary button
    logs exactly that.
    """
    a = _only(_followup_user())
    assert a["label"] == "Follow up"
    assert "business day" not in a["reason"], (
        "the sentence is counting the days the ledger row counts"
    )
    assert "Follow up." not in a["reason"], (
        "the sentence is restating the badge above it"
    )
    # AND THE FACTS BOTH PHRASES CARRIED, each pinned where it now lives.
    assert a["last_business_days"] == _bd(10)
    assert _footer(a) == f"Reached out · {_bd(10)} business days"


def test_the_follow_up_card_keeps_the_one_fact_only_it_held():
    """"touch 1" was the clause nothing else on the card said, and it is the
    clause that decides the student's next move: a first note unanswered is
    normal, a third is a verdict.

    It survives as "your first note" — "note" and not "touch" because the row
    beneath says "Reached out" and the button says Compose. "Touch" is the
    schema's word for the row, not a student's word for an email.
    """
    a = _only(_followup_user(email="fu2@example.com"))
    assert a["ctx"]["outbound"] == 1
    assert a["reason"] == "No reply to your first note."


def test_a_second_unanswered_note_says_two_and_not_one():
    """The count is read off `ctx["outbound"]`, never off the engine's prose,
    so the sentence and the number that produced it cannot disagree. Two
    outbound touches past the park window is the `park` branch, which shares
    this sentence because it is the same fact — only the badge differs, and
    the badge is where the difference belongs."""
    user = _user(email="fu3@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(user=user, name="Twice Written", firm=firm)
    _touch(user, c, "outreach", days_ago=40)
    _touch(user, c, "follow_up", days_ago=30)

    a = _only(user)
    assert a["action"] == "park"
    assert a["ctx"]["outbound"] == 2
    assert a["reason"] == "No reply after 2 notes."
    assert a["label"] == "Park it", "the ask is the badge's job, not the sentence's"


def test_an_expired_follow_up_says_why_it_is_a_park_and_nothing_else():
    """BEFORE: "First note went unanswered 5 weeks ago. Park it, or re-open
    with a new reason."

    "5 weeks ago" was the ledger row's silence in a SECOND unit — the exact
    two-registers-for-one-fact defect the rail pass ended the same night — and
    "Park it" is the badge and the primary button. What is left is the only
    thing this card can say that no other element does: why the same silence
    that earned a follow-up last week earns a park now.
    """
    user = _user(email="fu4@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(user=user, name="Long Gone", firm=firm)
    _touch(user, c, "outreach", days_ago=40)

    a = _only(user)
    assert a["action"] == "park" and a["ctx"]["expired"] is True
    assert a["reason"] == "Too late to follow up. Re-open only with a new reason."
    assert "weeks" not in a["reason"], "the silence is back in a second unit"
    # The silence itself, in the card's one unit, on the row that owns it.
    assert _footer(a) == f"Reached out · {_bd(40)} business days"


# ---------------------------------------------------------------------------
# 2. Keep warm — the card the founder was actually looking at.
# ---------------------------------------------------------------------------
def _keep_warm_user(email="kw@example.com", *, deadline_days=28):
    user = _user(email=email)
    firm = _target_firm(user)
    if deadline_days is not None:
        FirmDate.objects.create(
            firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
            date=timezone.localdate() + timedelta(days=deadline_days),
            precision="day", confidence=1.0,
        )
    c = Contact.all_objects.create(
        user=user, name="Katy Chen", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)
    return user


def test_the_keep_warm_card_stops_glossing_the_chip_beside_it():
    """"and you have already had the conversation" is nine words for
    `warmth == "chatted"`, printed as a chip two lines above the sentence and
    restated as "Chat happened" in the row one line below it.

    THE GLOSS IS NOT DELETED. A bare stored value IS vocabulary rather than a
    reason — that was the 2026-08-31 finding and it still holds — so the gloss
    moved to the chip's own `title`, which is where this page has put a
    fact-about-a-label since the rail pass hours earlier.
    """
    a = _only(_keep_warm_user())
    assert "you have already had the conversation" not in a["reason"]
    assert a["contact"]["warmth"] == "chatted"
    assert a["warmth_note"] == "You have already had the conversation."
    assert rel.WARMTH_NOTE["advocate"] == "They would vouch for you.", (
        "the advocate gloss has to survive the same move"
    )


def test_the_keep_warm_card_keeps_the_two_facts_the_chip_cannot_state():
    """The tier is a fact about the student's own list and the deadline is a
    fact about the firm's calendar. Neither is a temperature, so neither is
    ever going to be on a warmth chip, and both stay on the face."""
    a = _only(_keep_warm_user(email="kw2@example.com"))
    on = timezone.localdate() + timedelta(days=28)
    assert a["reason"] == (
        f"Tier 1 target. Applications close {on.strftime('%b')} {on.day}."
    )


def test_a_keep_warm_with_nothing_live_is_the_lead_alone():
    """Section 5's oldest rule, unchanged by the diet: where there is no
    answer to "what makes today the day", the card says less rather than
    filling the gap with a clock."""
    a = _only(_keep_warm_user(email="kw3@example.com", deadline_days=None))
    assert a["reason"] == "Tier 1 target."


def test_the_recruiting_keep_warm_drops_a_subject_and_a_second_there():
    """BEFORE: "{opening} They are your recruiting contact there." The clause
    in front of it already named where, and "They are" is three words for a
    noun phrase the rest of this module states without one."""
    user = _user(email="kw4@example.com")
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=28),
        precision="day", confidence=1.0,
    )
    c = Contact.all_objects.create(
        user=user, name="Campus Person", firm=firm,
        role="Campus Recruiting Manager",
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)

    a = _only(user)
    assert a["is_recruiting"] is True
    on = timezone.localdate() + timedelta(days=28)
    assert a["reason"] == (
        f"Applications close {on.strftime('%b')} {on.day}. Your recruiting contact."
    )
    assert "They are your recruiting contact there" not in a["reason"]


# ---------------------------------------------------------------------------
# 3. First outreach — a sentence that was the badge and the row read back.
# ---------------------------------------------------------------------------
def _first_outreach_user(email="fo@example.com", tier=2):
    user = _user(email=email)
    firm = _target_firm(user, tier=tier)
    Contact.all_objects.create(user=user, name="Sephi Konstantoudakis", firm=firm)
    return user


def test_the_first_outreach_card_says_something_about_the_person():
    """BEFORE: "Added but never contacted. Send the first note." The first
    half is the ledger row ("No touches yet"), the second is the badge (FIRST
    OUTREACH), and between them the card said nothing at all about the person
    it was asking the student to spend a morning on.

    So the sentence is the lead `keep_warm_reason` has always used, which is
    the one thing the card knows about a cold stranger and had never printed.
    """
    a = _only(_first_outreach_user())
    assert a["label"] == "First outreach"
    assert a["reason"] == "Tier 2 target."
    assert "never contacted" not in a["reason"]
    assert "Send the first note" not in a["reason"]
    # Both halves of the old sentence, each where it now lives.
    assert _footer(a) == "No touches yet"


def test_a_stranger_who_shares_your_school_is_told_apart_from_a_target():
    """The lead is read off `contact_relevance`, so the two reasons a cold
    contact may be in the queue at all produce two different sentences. Only
    those two are reachable here: REL_INBOUND needs an owed reply, and a
    contact with no touches on record is owed nothing."""
    user = _user(email="fo2@example.com")
    Contact.all_objects.create(
        user=user, name="Marcus Webb", firm_text="Somewhere Else",
        school_affiliation=True,
    )
    assert _only(user)["reason"] == "Same school."


def test_an_untiered_target_still_names_the_list_it_is_on():
    """An "Unranked" drag leaves `UserFirm.tier` empty. The firm is still on
    the student's list, which is still the reason the card exists."""
    user = _user(email="fo3@example.com")
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    UserFirm.all_objects.create(user=user, firm=firm, tier=None)
    Contact.all_objects.create(user=user, name="Unranked Person", firm=firm)
    assert _only(user)["reason"] == "On your target list."


# ---------------------------------------------------------------------------
# 4. The two inbound cards.
# ---------------------------------------------------------------------------
def test_the_reply_card_is_the_qualifier_and_nothing_else():
    """BEFORE: "They wrote to you. Answer the note. Recruiting contact, not a
    coffee chat."

    "They wrote to you" is the ledger row ("They replied · N business days").
    "Answer the note" is the badge (REPLY) and the row's own Compose button.
    The qualifier is the only clause that changes what the student writes, and
    it is the clause the card exists for: the prompt it replaced proposed a
    coffee chat to a talent-acquisition manager.
    """
    user = _user(email="rep@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Talent Person", firm=firm,
        role="Manager, Talent Acquisition",
        warmth="replied", thread_state="replied",
    )
    _touch(user, c, "reply_received", days_ago=12)

    a = _only(user)
    assert a["label"] == "Reply"
    assert a["reason"] == "Recruiting contact, not a coffee chat."
    assert "They wrote to you" not in a["reason"]
    assert _footer(a) == f"They replied · {_bd(12)} business days"


def test_the_two_other_reply_sentences_lost_the_same_two_clauses():
    """The campaign and the recruitment-hidden overrides are the same card
    with a different qualifier, and they had the same two restatements in
    front of it. Their qualifiers are DIFFERENT claims ("you said this send
    was not your recruiting" vs "this person is not part of your recruiting")
    and both survive word for word."""
    assert rel.CAMPAIGN_REPLY_REASON == "From a send that was not your recruiting."
    assert rel.UNRELATED_REPLY_REASON == "Not part of your recruiting."
    for reason in (rel.CAMPAIGN_REPLY_REASON, rel.UNRELATED_REPLY_REASON,
                   rel.RECRUITING_REPLY_REASON):
        assert "They wrote to you" not in reason
        assert "Answer the note" not in reason
        assert reason.count(".") == 1, f"one clause, one full stop: {reason!r}"


def test_the_chat_proposal_keeps_the_size_of_the_ask():
    """BEFORE: "They replied. Propose a 15-min chat." Sentence one is the
    ledger row, sentence two is the badge — except for "15-min", which is the
    whole of what the badge leaves out and the half that gets a chat agreed.
    """
    user = _user(email="adv@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Ben Ortiz", firm=firm,
        warmth="replied", thread_state="replied",
    )
    _touch(user, c, "reply_received", days_ago=12)

    a = _only(user)
    assert a["label"] == "Propose a chat"
    assert a["reason"] == "Ask for 15 minutes."
    assert "They replied" not in a["reason"]
    assert "15" in a["reason"], "the size of the ask is the fact this card holds"


# ---------------------------------------------------------------------------
# 5. The cards that name a date, and the chip that names it too.
# ---------------------------------------------------------------------------
def test_the_reping_card_does_not_reprint_the_chip_beside_it():
    """BEFORE: "Barclays app closes Aug 30. Re-ping before you submit." The
    firm is the card's own identity line, the date is the `Closes Aug 30` chip
    beside the badge, and "re-ping" is the badge. This test pins the chip and
    the sentence together, because the restatement only existed because they
    share a card."""
    user = _user(email="rp@example.com")
    firm = _target_firm(user, slug="barclays", name="Barclays")
    close = timezone.localdate() + timedelta(days=7)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=close, precision="day", confidence=1.0,
    )
    c = Contact.all_objects.create(
        user=user, name="Warm Banker", firm=firm, region="hk",
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)

    a = _only(user)
    assert a["action"] == "reping"
    assert a["reason"] == "Before you submit the application."
    assert "Barclays" not in a["reason"], "the identity line already names the firm"
    assert a["closes_on"] == close, (
        "the date left the sentence, so the chip has to be holding it"
    )
    assert f'Closes {close.strftime("%b")} {close.day}' in _page(user)


def test_the_thank_you_card_finally_states_the_window_it_is_about():
    """BEFORE: "Chat done 2d ago. Send thank-you." — the ledger row's own fact
    in calendar days beside its business days, then the badge.

    And the 24-hour window that is the entire reason this card is urgent never
    reached the screen at all: the engine writes it as "(within 24h)" and
    `crm.today._sentenceize` strips every parenthetical. So this branch cuts
    two restatements and promotes the fact they were hiding.
    """
    user = _user(email="ty@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Fresh Chat", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    Touch.all_objects.create(
        user=user, contact=c, kind="chat", channel="email",
        ts=timezone.now() - timedelta(hours=5),
    )
    a = _only(user)
    assert a["action"] == "thank_you"
    assert a["reason"] == "Within 24 hours of the chat."
    assert a["ctx"]["overdue"] is False


def test_an_overdue_thank_you_says_overdue_where_a_bracket_used_to_hide_it():
    """"(OVERDUE)" was stripped by `_sentenceize` exactly like "(within 24h)",
    so the one card on this page with a genuinely urgent state had no way to
    say so. Read off `ctx["overdue"]`, never off the prose."""
    user = _user(email="ty2@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Late Chat", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    Touch.all_objects.create(
        user=user, contact=c, kind="chat", channel="email",
        ts=timezone.now() - timedelta(hours=57),
    )
    a = _only(user)
    assert a["ctx"]["overdue"] is True
    assert a["reason"] == "Overdue past the 24 hour window."


def test_the_confirm_chat_card_does_not_read_out_its_own_two_buttons():
    """BEFORE: "Chat was scheduled for Aug 24. Did it happen? Log the chat or
    reschedule." The question and both options are the card's own two
    controls, rendered an inch below the sentence — as literal as a
    restatement gets. The booked day is the fact, and it stays.
    """
    user = _user(email="cc@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Booked Person", firm=firm,
        warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=12)

    a = _only(user)
    assert a["action"] == "confirm_chat"
    assert a["reason"] == "A chat was being arranged."
    assert "Log the chat" not in a["reason"]
    # ...and the buttons that clause was reading out are still both there.
    page = _page(user)
    assert ">Log the chat</a>" in page
    assert ">Reschedule</summary>" in page


def test_the_assessment_card_stops_at_what_the_firm_process_is():
    """BEFORE it closed "...so put the time there instead of into a first
    note", which is fourteen words for the badge above it (Apply).

    The clause this copy may NEVER contain is unchanged and unchallenged: no
    source shows networking is counterproductive at these firms, so the
    sentence states the process and stops.
    """
    assert rel.APPLY_ONLY_REASON == (
        "This firm hires by assessment. The application and the test are the process."
    )
    assert "first note" not in rel.APPLY_ONLY_REASON
    assert "assessment" in rel.APPLY_ONLY_REASON
    for banned in ("networking", "instead", "waste"):
        assert banned not in rel.APPLY_ONLY_REASON.lower()


# ---------------------------------------------------------------------------
# 6. An engine branch this module does not know renders as it always did.
# ---------------------------------------------------------------------------
def test_an_unrecognised_action_keeps_the_engines_own_sentence():
    """`card_reason` is a rewrite, not a filter. A cadence branch added
    tomorrow renders exactly the way it did before anybody edited this file,
    which is the property that makes it safe to own every card's copy in one
    function."""
    action = {"action": "some_future_branch", "reason": "Whatever the engine said.",
              "ctx": {}}
    assert rel.card_reason(action) == "Whatever the engine said."


def test_a_follow_up_with_no_count_on_the_dict_invents_none():
    """The engine always sends `outbound`, and if it ever stops, the card says
    the part it still knows rather than guessing a number. Same discipline as
    everything else in this module: nothing rounds a maybe into a statement.
    """
    assert rel.card_reason({"action": "follow_up", "reason": "x", "ctx": {}}) == (
        "No reply yet."
    )


# ---------------------------------------------------------------------------
# 7. The ledger row, and the page it renders on.
# ---------------------------------------------------------------------------
def test_the_ledger_row_drops_its_label_and_keeps_the_claim():
    """"Last:" is not one of the row's facts, it is a claim ABOUT them — that
    this is the most recent touch and not merely a touch. That is a `title` on
    this page, and the title picks up the clock as well, which nothing on the
    card had ever said out loud."""
    page = _page(_followup_user(email="row@example.com"))
    row = re.search(r'<p class="act-last".*?</p>', page, re.S).group(0)
    assert _face(row) == f"Reached out · {_bd(10)} business days"
    assert "Last: Reached out" not in page
    assert "The most recent touch on record" in row
    assert "counted in business days" in row


def test_the_ledger_row_drops_the_suffix_the_rail_dropped_the_same_night():
    """The page's one reading rule, settled hours earlier on the rail: a bare
    count is time already spent, and a future distance says "in" (see
    `test_rail_copy_2026_09_02.py`, both directions). Recent Activity dropped
    the identical word for the identical reason, and a queue whose two halves
    disagree about it is worse than one that never applied the rule."""
    page = _page(_followup_user(email="row2@example.com"))
    row = re.search(r'<p class="act-last".*?</p>', page, re.S).group(0)
    assert "ago" not in _face(row), "the suffix is back on the ledger row"
    assert "business day" in row, (
        "the unit is not the suffix; the cadence counts business days and "
        "shortening it to a bare Nd would change a number, not a word"
    )


def _face(markup: str) -> str:
    """The plain text of a slice of markup, tags stripped — so an assertion
    about what a student READS cannot be satisfied by a `title` attribute.
    Same trap and same guard as `test_rail_copy_2026_09_02._face`."""
    return " ".join(re.sub(r"<[^>]+>", " ", markup).split())


def test_the_warmth_chip_explains_itself_on_the_page():
    """The gloss has to be REACHABLE, not merely stored: the assertion above
    reads a dict, this one reads the rendered chip."""
    page = _page(_keep_warm_user(email="chip@example.com"))
    chip = re.search(r'<span class="chip warmth-chatted"[^>]*>', page).group(0)
    assert 'title="You have already had the conversation."' in chip
    assert "chatted" in _face(
        re.search(r'<span class="chip warmth-chatted".*?</span>', page, re.S).group(0)
    ), "the chip still prints the value it is a title for"


def test_a_warmth_with_no_gloss_draws_no_empty_title_at_all():
    """`Contact.warmth` is a free `CharField` with a default, not a choices
    field, so a value outside the four this product uses is storable. A blank
    `warmth_note` must then draw no attribute — an empty `title=""` is a
    tooltip that opens onto nothing, and screen readers announce it."""
    user = _user(email="blank@example.com")
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Odd Warmth", firm=firm, thread_state="chat_done")
    Touch.all_objects.create(
        user=user, contact=c, kind="chat", channel="email",
        ts=timezone.now() - timedelta(hours=5),
    )
    Contact.all_objects.filter(pk=c.pk).update(warmth="not_a_warmth")
    page = _page(user)
    chip = re.search(r'<span class="chip warmth-not_a_warmth"[^>]*>', page).group(0)
    assert "title" not in chip


# ---------------------------------------------------------------------------
# 8. The rule, checked across the whole queue at once.
# ---------------------------------------------------------------------------
def _many_card_user(email="all@example.com"):
    """One account, five different card shapes, so the cross-card rules below
    are checked on a page and not on a series of one-card fixtures."""
    user = _user(email=email)
    firm = _target_firm(user)
    other = _target_firm(user, slug="hsbc", name="HSBC", tier=2)

    Contact.all_objects.create(user=user, name="Cold Stranger", firm=firm)
    fu = Contact.all_objects.create(user=user, name="No Reply Yet", firm=firm)
    _touch(user, fu, "outreach", days_ago=10)
    adv = Contact.all_objects.create(
        user=user, name="Wrote Back", firm=other,
        warmth="replied", thread_state="replied")
    _touch(user, adv, "reply_received", days_ago=12)
    kw = Contact.all_objects.create(
        user=user, name="Met Once", firm=other,
        warmth="chatted", thread_state="chat_done")
    _touch(user, kw, "chat", days_ago=60)
    return user


def test_no_card_sentence_repeats_the_badge_directly_above_it():
    """The rule, mechanically: a sentence may not contain its own label. This
    is the assertion the whole pass was working to, and it is the one that
    will catch the next sentence that drifts back into the badge's job."""
    user = _many_card_user()
    actions, _ = _build_actions(user)
    assert len(actions) >= 4, "the fixture stopped producing several shapes"
    for a in actions:
        assert a["label"].lower() not in a["reason"].lower(), (
            f"the {a['label']!r} card's sentence says its own badge: "
            f"{a['reason']!r}"
        )


def test_no_card_sentence_counts_the_days_its_own_ledger_row_counts():
    """The other half of the same rule. Two facts, two registers, one card was
    the founder's complaint on the rail and it was true here too: the sentence
    said "7 business days" while the row under it said "7 business days ago".
    """
    for a, _ in [(a, None) for a in _build_actions(_many_card_user("all2@example.com"))[0]]:
        assert "business day" not in a["reason"], (
            f"{a['label']!r}: {a['reason']!r} counts what the ledger row counts"
        )


def test_no_card_sentence_uses_an_em_dash():
    """The founder's standing rule for Coverage copy. Checked on the SENTENCE
    rather than on the page, because `crm.today._pace_by_firm` joins its own
    disclosure clause on with one and that join is another workstream's."""
    for a in _build_actions(_many_card_user("all3@example.com"))[0]:
        assert "—" not in rel.card_reason(a), a["reason"]
        assert "—" not in rel.keep_warm_reason(a) if a["action"] in (
            "keep_warm", "maintain") else True


def test_every_card_still_carries_a_sentence_at_all():
    """A diet that empties a card is not a diet. The digest's HTML row prints
    the reason and NOT the label (`templates/crm/emails/weekly_digest.html`),
    so a blank sentence there is a row with no ask on it — which is why the
    first-outreach card was given the lead instead of being emptied."""
    for a in _build_actions(_many_card_user("all4@example.com"))[0]:
        assert a["reason"].strip(), f"{a['label']} renders no sentence"
        assert a["reason"].endswith("."), a["reason"]


# ---------------------------------------------------------------------------
# 9. The one bug this pass had to close on its way through.
# ---------------------------------------------------------------------------
def test_a_dead_address_survives_the_card_sentence_being_rewritten():
    """REGRESSION, found writing this pass. `_mark_undeliverable` rewrites a
    card whose only address has bounced into "find a new address", and it runs
    BEFORE `_gate_and_rank` — which then wrote `a["reason"]` again on every
    branch it took. A `keep_warm` at a bounced address lost the bounce
    sentence and went back to asking for an email to an address the mail
    system has already rejected, which is the entire defect
    `_mark_undeliverable` shipped to end.

    Held and restored, and only while the card still WEARS the undeliverable
    label: a branch that legitimately relabels the card is asking for
    something else, and its own sentence is the right one for that ask.
    """
    from capture.models import MailFact

    user = _keep_warm_user(email="dead@example.com")
    c = Contact.all_objects.get(user=user, name="Katy Chen")
    Contact.all_objects.filter(pk=c.pk).update(email="katy@nomura.test")
    MailFact.all_objects.create(
        user=user, contact=c, kind=MailFact.KIND_BOUNCED,
        about_email="katy@nomura.test",
    )
    a = _only(user)
    assert a["undeliverable"] is True
    assert a["label"] == "Find an address"
    assert "bounced" in a["reason"], (
        f"the bounce sentence was overwritten by a card rewrite: {a['reason']!r}"
    )
    assert "Tier 1 target" not in a["reason"]
