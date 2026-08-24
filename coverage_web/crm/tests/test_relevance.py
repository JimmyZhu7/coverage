"""The queue's relevance gate, its ask, its ranking and its cap.

Every case here corresponds to something measured on the founder's own
156-contact account on 2026-08-21/22, and the numbers in the docstrings are
those measurements rather than illustrations:

  - his 16-item queue held 14 people at firms he does not target, eight of them
    the same follow-up sentence, while every one of his tier-1 and tier-2
    chatted bankers was silent;
  - the top card read "Advocate. Last touch 34d ago.", which is a stopwatch;
  - and two campus-recruiting contacts were being asked to coffee, one of whom
    had already made her introduction and handed him on.

`transaction=True` for the same reason `test_today.py` uses it: some paths go
through `crm.services`, which opens its own connection.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm import relevance as rel
from crm.models import Contact, Touch, UserFirm
from crm.today import (
    OPENING_MIN_IDLE_DAYS,
    QUIET_UPKEEP_PLAN_MAX,
    TODAY_PLAN_MAX,
    _build_actions,
    _cockpit_context,
)
from directory.models import Firm, FirmDate, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="rel@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _touch(user, contact, kind, *, days_ago=0, channel="email"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel=channel,
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _target_firm(user, slug="nomura", name="Nomura", tier=1):
    firm = Firm.objects.create(slug=slug, name=name)
    UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    return firm


def _actions_by_name(user):
    actions, _ = _build_actions(user)
    return {a["contact"]["name"]: a for a in actions}


# ---------------------------------------------------------------------------
# 1. Who may generate a daily action at all.
# ---------------------------------------------------------------------------
def test_a_contact_at_a_firm_you_do_not_target_never_reaches_the_queue():
    """THE MEASURED BUG. Eight of sixteen cards read "no reply 13 business days
    after touch 1" about people at AccraCare, Paramount, Endpoint, WorkWhile
    and E*TRADE — none of them banks, none of them on his list."""
    user = _user()
    stranger = Contact.all_objects.create(
        user=user, name="Stranger At Nowhere", firm_text="AccraCare",
    )
    _touch(user, stranger, "outreach", days_ago=20)

    assert _actions_by_name(user) == {}


def test_the_same_contact_at_a_targeted_firm_does_reach_it():
    """The other side of the boundary — this must not become a gate that
    silences the whole queue."""
    user = _user()
    firm = _target_firm(user)
    c = Contact.all_objects.create(user=user, name="Real Banker", firm=firm)
    _touch(user, c, "outreach", days_ago=20)

    assert _actions_by_name(user)["Real Banker"]["action"] == "follow_up"


def test_a_school_tie_is_relevance_whatever_the_employer():
    """The founder's own call: a shared school is a real asset even at a firm
    he is not chasing, so an alum is never gated out on their employer."""
    user = _user()
    alum = Contact.all_objects.create(
        user=user, name="Trojan Somewhere", firm_text="WorkWhile",
        school_affiliation=True,
    )
    _touch(user, alum, "outreach", days_ago=20)

    a = _actions_by_name(user)["Trojan Somewhere"]
    assert a["relevance"] == rel.REL_SCHOOL


def test_an_untiered_firm_on_the_list_still_counts():
    """`UserFirm.tier` is nullable and `crm.views.set_firm_tier` writes NULL
    deliberately for the Unranked lane. Membership of the list is the claim,
    not the number, so the gate tests `in` and never truthiness."""
    user = _user()
    firm = _target_firm(user, slug="unranked", name="Unranked Bank", tier=None)
    c = Contact.all_objects.create(user=user, name="Unranked Person", firm=firm)
    _touch(user, c, "outreach", days_ago=20)

    a = _actions_by_name(user)["Unranked Person"]
    assert a["relevance"] == rel.REL_TIERED
    assert a["relevance_tier"] is None


# ---------------------------------------------------------------------------
# 2. The one override: somebody who wrote to you.
# ---------------------------------------------------------------------------
def test_an_unanswered_reply_survives_the_gate_whatever_the_firm():
    """Answering a person who wrote to you is basic courtesy and costs one
    reply. The gate must not swallow it."""
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Wrote To You", firm_text="Some Startup",
        warmth="replied", thread_state="replied",
    )
    _touch(user, c, "outreach", days_ago=10)
    _touch(user, c, "reply_received", days_ago=8)

    a = _actions_by_name(user)["Wrote To You"]
    assert a["relevance"] == rel.REL_INBOUND
    assert a["owed_reply"] is True


def test_the_override_ends_the_moment_you_answer():
    """It is an OWED reply, not a permanent pass. Once the student's own note
    lands after theirs, the contact goes back to being someone at a firm he
    does not target."""
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Already Answered", firm_text="Some Startup",
        warmth="replied", thread_state="replied",
    )
    _touch(user, c, "outreach", days_ago=12)
    _touch(user, c, "reply_received", days_ago=10)
    _touch(user, c, "outreach", days_ago=8)

    assert "Already Answered" not in _actions_by_name(user)


def test_the_override_ranks_below_every_relevant_person():
    """It buys visibility, not priority: one reply owed to a stranger must not
    outrank the people the student is actually chasing."""
    user = _user()
    firm = _target_firm(user)
    warm = Contact.all_objects.create(
        user=user, name="Tier One Reply", firm=firm,
        warmth="replied", thread_state="replied",
    )
    _touch(user, warm, "outreach", days_ago=10)
    _touch(user, warm, "reply_received", days_ago=8)

    stranger = Contact.all_objects.create(
        user=user, name="Stranger Reply", firm_text="Some Startup",
        warmth="replied", thread_state="replied",
    )
    _touch(user, stranger, "outreach", days_ago=10)
    _touch(user, stranger, "reply_received", days_ago=8)

    by_name = _actions_by_name(user)
    assert by_name["Tier One Reply"]["ev"] > by_name["Stranger Reply"]["ev"]


# ---------------------------------------------------------------------------
# 3. The ask: recruiters are never invited to coffee.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", [
    "Manager, Talent Acquisition",
    "Campus recruiting manager (Deloitte, national)",
    "Campus recruiter (PwC)",
    "Manager, Global Recruiting, Bain & Company",
    "University Recruiter",
    "Recruiting Coordinator",
    "Technical Sourcer",
])
def test_recruiting_titles_are_recognised(role):
    assert rel.is_recruiting_role(role)


@pytest.mark.parametrize("role", [
    "IB Analyst",
    "IB Associate TMT",
    "Vice President, Investment Banking",
    "USC alum, HR/people professional",   # works in HR; not HIS gatekeeper
    "Professor (USC Marshall, BUAD 306)",
    "Equities Sales",
    "",
])
def test_ordinary_titles_are_not(role):
    """CONSERVATIVE BY DESIGN. A false positive silences a real banker's coffee
    chat invisibly, which is far worse than one prompt the student ignores."""
    assert not rel.is_recruiting_role(role)


@pytest.mark.parametrize("role", [
    # How the founder's 2026-08-23 full-history refresh actually spelled it:
    # ten campus recruiters at Bain, BCG, PwC and KPMG arrived with this exact
    # role, read by `capture.discovery.split_display_name` off signatures like
    # "Keith Bevans, Recruiting". The function is the whole seat.
    "Recruiting",
    "recruiting",
    "Recruitment",
    "  Recruiting  ",   # split_display_name trims, but the rule must not rely on it
])
def test_a_role_that_is_nothing_but_recruiting_is_recognised(role):
    assert rel.is_recruiting_role(role)


@pytest.mark.parametrize("role", [
    # The false positive the bare-substring rejection exists to prevent, and
    # must KEEP preventing: a banker whose longer title merely contains the
    # word. Silencing this person's coffee chat would be invisible and worse
    # than any wrong prompt.
    "Analyst, recruiting",
    "IB Analyst - recruiting team liaison",
    "Recruiting the next generation of traders",  # leads with it, still not a seat
])
def test_recruiting_inside_a_longer_role_still_does_not_match_bare(role):
    """The whole-string carve-out must stay a whole-string carve-out. None of
    these are caught by the marker list either, so a regression here means the
    anchor leaked into a substring match."""
    assert not rel.is_recruiting_role(role)


def test_a_bare_recruiting_recruiter_who_wrote_gets_a_reply_not_a_chat_invitation():
    """The 2026-08-23 resurfacing of the founder's 2026-08-22 complaint: a
    Bain campus recruiter with role exactly "Recruiting", warmth `replied` off
    a real inbound note, was headed for "they replied, propose a 15-min chat"
    because the classifier only rejected the bare word as a substring and had
    no whole-string rule."""
    user = _user()
    firm = _target_firm(user, slug="bain", name="Bain & Company", tier=2)
    c = Contact.all_objects.create(
        user=user, name="Campus Recruiter Signed Bare", firm=firm,
        role="Recruiting", warmth="replied", thread_state="replied",
    )
    _touch(user, c, "reply_received", days_ago=8)

    a = _actions_by_name(user)["Campus Recruiter Signed Bare"]
    assert a["is_recruiting"] is True
    assert a["label"] == rel.RECRUITING_REPLY_LABEL
    assert "not a coffee chat" in a["reason"]
    assert "15-min chat" not in a["reason"]


def test_a_recruiter_who_wrote_to_you_gets_a_reply_not_a_chat_invitation():
    """The measured card: a "Manager, Talent Acquisition" whose mass programme
    invite had been logged as a reply, answered with "they replied, propose a
    15-min chat"."""
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Talent Person", firm_text="West Monroe",
        role="Manager, Talent Acquisition",
        warmth="replied", thread_state="replied",
    )
    _touch(user, c, "reply_received", days_ago=8)

    a = _actions_by_name(user)["Talent Person"]
    assert a["is_recruiting"] is True
    assert a["label"] == rel.RECRUITING_REPLY_LABEL
    assert a["label"] != "Propose a chat"
    assert "Answer the note" in a["reason"]
    # The card says out loud what NOT to do, because the thing it replaced did
    # exactly that thing.
    assert "not a coffee chat" in a["reason"]
    assert "15-min chat" not in a["reason"]


def test_a_recruiter_you_already_answered_gets_no_card_at_all():
    """The second measured card: a national campus-recruiting manager who had
    ALREADY made the introduction and been written back to. Nothing is owed and
    no deadline is pending, so the honest card is no card — certainly not a
    coffee-chat proposal."""
    user = _user()
    c = Contact.all_objects.create(
        user=user, name="Handed You On", firm_text="USC",
        role="Campus recruiting manager (Deloitte, national)",
        school_affiliation=True, warmth="replied", thread_state="replied",
    )
    _touch(user, c, "reply_received", days_ago=30)
    _touch(user, c, "outreach", days_ago=14)

    assert "Handed You On" not in _actions_by_name(user)


def test_a_recruiter_never_gets_a_bare_keep_warm():
    """"Keeping a recruiter warm" with nothing in the process to talk about is
    the hollow prompt wearing a different name."""
    user = _user()
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Warm Recruiter", firm=firm,
        role="Campus Recruiter", warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=60)

    assert "Warm Recruiter" not in _actions_by_name(user)


def test_the_students_own_answer_beats_the_role_text_in_both_directions():
    """`recruiting_contact` is nullable on purpose: NULL means nobody has said,
    True/False is the student's word and always wins."""
    user = _user()
    firm = _target_firm(user)

    # A banker whose title happens to read like a recruiter's, corrected.
    banker = Contact.all_objects.create(
        user=user, name="Mislabelled Banker", firm=firm,
        role="Campus Recruiter", warmth="replied", thread_state="replied",
        recruiting_contact=False,
    )
    _touch(user, banker, "reply_received", days_ago=8)

    # A recruiter whose title says nothing, flagged by hand.
    quiet_recruiter = Contact.all_objects.create(
        user=user, name="Quiet Recruiter", firm=firm,
        role="Programme Lead", warmth="replied", thread_state="replied",
        recruiting_contact=True,
    )
    _touch(user, quiet_recruiter, "reply_received", days_ago=8)

    by_name = _actions_by_name(user)
    assert by_name["Mislabelled Banker"]["is_recruiting"] is False
    assert by_name["Mislabelled Banker"]["label"] == "Propose a chat"
    assert by_name["Quiet Recruiter"]["is_recruiting"] is True
    assert by_name["Quiet Recruiter"]["label"] == rel.RECRUITING_REPLY_LABEL


# ---------------------------------------------------------------------------
# 4. Keep-warm: rare, and pointed at a reason.
# ---------------------------------------------------------------------------
def test_a_keep_warm_reason_never_states_a_day_count():
    """THE COMPLAINT. "Advocate. Last touch 34d ago." is a stopwatch, not a
    reason to spend social capital on a human being."""
    user = _user()
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Old Advocate", firm=firm,
        warmth="advocate", thread_state="advocate",
    )
    _touch(user, c, "maintain", days_ago=34)

    reason = _actions_by_name(user)["Old Advocate"]["reason"]
    assert "34d" not in reason and "days ago" not in reason
    assert reason == "Tier 1 target, and they would vouch for you."


def test_a_live_deadline_at_their_firm_becomes_the_reason():
    user = _user()
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    c = Contact.all_objects.create(
        user=user, name="Warm Banker", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=60)

    a = _actions_by_name(user)["Warm Banker"]
    assert a["opening"]["kind"] == rel.OPENING_FIRM_DATE
    assert "Applications close" in a["reason"]
    assert "you have already had the conversation" in a["reason"]


def test_a_rumoured_date_is_not_a_reason():
    """Same confirmed-only bar the cadence engine's re-ping branch holds. A
    countdown built on a rumour is worse than no countdown."""
    user = _user()
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=0.6,          # "reported", not confirmed
    )
    c = Contact.all_objects.create(
        user=user, name="Warm Banker", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=60)

    assert _actions_by_name(user)["Warm Banker"]["opening"] is None


def test_an_opening_raises_a_keep_warm_the_bare_clock_has_not_reached():
    """The founder had turned his keep-warm dial out to six weeks to stop the
    hollow prompts, which also silenced ten chatted contacts at tier-1 and
    tier-2 banks. The dial still owns "how long is too long to go quiet"; it
    cannot answer "is there something to say today"."""
    user = _user()
    user.cadence_params = {"chatted_touch_min_weeks": 6}
    user.save(update_fields=["cadence_params"])
    firm = _target_firm(user)
    c = Contact.all_objects.create(
        user=user, name="Not Due Yet", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=21)          # inside the 6-week dial

    # Precondition: the engine itself says nothing about them today.
    assert "Not Due Yet" not in _actions_by_name(user)

    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="insight_deadline",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    a = _actions_by_name(user)["Not Due Yet"]
    assert a["action"] == "keep_warm"
    assert a["from_opening"] is True
    assert "Insight deadline" in a["reason"]


def test_an_opening_does_not_reopen_a_parked_contact():
    """Park is a deliberate exit. A deadline at the firm is not consent to
    undo it."""
    user = _user()
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    c = Contact.all_objects.create(
        user=user, name="Parked Warm", firm=firm,
        warmth="chatted", thread_state="parked",
    )
    _touch(user, c, "chat", days_ago=60)

    assert "Parked Warm" not in _actions_by_name(user)


def test_an_opening_does_not_nudge_somebody_you_just_wrote_to():
    user = _user()
    user.cadence_params = {"chatted_touch_min_weeks": 6}
    user.save(update_fields=["cadence_params"])
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    c = Contact.all_objects.create(
        user=user, name="Just Written To", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)
    _touch(user, c, "maintain", days_ago=OPENING_MIN_IDLE_DAYS - 1)

    assert "Just Written To" not in _actions_by_name(user)


def test_a_new_role_on_the_board_is_a_reason_too():
    """The board half of `firm_openings`, which is the half the cadence engine
    structurally cannot see: it is never handed an `Opportunity` row."""
    user = _user()
    firm = _target_firm(user)
    Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst", bucket="internship",
        status="open", location="Hong Kong",
        url="https://example.com/nomura/new",
    )
    c = Contact.all_objects.create(
        user=user, name="Warm Banker", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=60)

    a = _actions_by_name(user)["Warm Banker"]
    assert a["opening"]["kind"] == rel.OPENING_NEW_ROLE
    assert "opened there this week" in a["reason"]


# ---------------------------------------------------------------------------
# 5. Rank and cap.
# ---------------------------------------------------------------------------
def test_the_plan_is_capped_and_the_remainder_is_reachable():
    """Sixteen was too many at 156 contacts and would be forty at 500. The
    remainder is held, never dropped."""
    user = _user()
    firm = _target_firm(user)
    for i in range(12):
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}", firm=firm)
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert len(planned) <= TODAY_PLAN_MAX
    assert len(planned) + ctx["held_total"] == 12


def test_a_warm_tier_one_with_a_reason_outranks_a_pile_of_cold_alumni():
    """The founder's queue, in miniature: eight cold follow-ups at firms he
    does not target sat above every banker he had actually met."""
    user = _user()
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    warm = Contact.all_objects.create(
        user=user, name="Met You", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, warm, "chat", days_ago=60)
    for i in range(8):
        c = Contact.all_objects.create(
            user=user, name=f"Cold Alum {i:02d}", firm_text="AccraCare",
            school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    planned = [a["contact"]["name"] for lane in ctx["lanes"] for a in lane["items"]]
    assert planned[0] == "Met You"
    by_name = _actions_by_name(user)
    assert by_name["Met You"]["ev"] > by_name["Cold Alum 00"]["ev"]


def test_only_one_reasonless_keep_warm_reaches_the_plan():
    """The "rare" half of the keep-warm decision. Three advocates with nothing
    live at their firms is three cards the product cannot justify; one is a
    nudge, three is the hollow queue again."""
    user = _user()
    firm = _target_firm(user)
    for i in range(3):
        c = Contact.all_objects.create(
            user=user, name=f"Quiet Advocate {i}", firm=firm,
            warmth="advocate", thread_state="advocate",
        )
        _touch(user, c, "maintain", days_ago=60)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    quiet = [a for a in planned if a["action"] in ("keep_warm", "maintain")]
    assert len(quiet) == QUIET_UPKEEP_PLAN_MAX == 1
    assert ctx["held_total"] == 2


def test_a_confirmed_deadline_is_never_capped_away():
    """The pre-existing invariant, re-checked against the new cap: class 0 is
    shown in full however small the plan is."""
    user = _user()
    firm = _target_firm(user)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=5),
        precision="day", confidence=1.0,
    )
    for i in range(8):
        c = Contact.all_objects.create(
            user=user, name=f"Warm {i:02d}", firm=firm, region="hk",
            warmth="chatted", thread_state="chat_done",
        )
        _touch(user, c, "chat", days_ago=60)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert len([a for a in planned if a["action"] == "reping"]) == 8
    assert len(planned) > TODAY_PLAN_MAX, "critical work is never rationed"


# ---------------------------------------------------------------------------
# 6. The scorer, as a pure function.
# ---------------------------------------------------------------------------
def _action(**kw):
    base = {
        "contact": {"warmth": kw.pop("warmth", "cold")},
        "action": kw.pop("action", "follow_up"),
        "priority": kw.pop("priority", 1),
        "relevance": kw.pop("relevance", rel.REL_TIERED),
        "relevance_tier": kw.pop("tier", 1),
    }
    base.update(kw)
    return base


def test_expected_value_multiplies_rather_than_adds():
    """Added, a pile of small facts about a stranger outweighs one big fact
    about the person who would vouch for you. Multiplied, it cannot."""
    advocate = _action(warmth="advocate", tier=1, action="maintain",
                       opening={"kind": rel.OPENING_NEW_ROLE})
    stranger = _action(warmth="cold", relevance=rel.REL_INBOUND, tier=None,
                       owed_reply=True)
    assert rel.expected_value(advocate) > rel.expected_value(stranger)


def test_nothing_happening_scores_below_everything_happening():
    quiet = _action(warmth="advocate", tier=1, action="maintain")
    busy = _action(warmth="advocate", tier=1, action="maintain",
                   opening={"kind": rel.OPENING_FIRM_DATE})
    assert rel.expected_value(busy) > rel.expected_value(quiet)


def test_tier_one_beats_tier_three_all_else_equal():
    assert rel.expected_value(_action(tier=1)) > rel.expected_value(_action(tier=3))


# ---------------------------------------------------------------------------
# 7. The override is reachable, not just storable.
# ---------------------------------------------------------------------------
def test_the_contact_form_offers_the_recruiting_override(client):
    """A rule the student cannot correct is a rule they have to work around.
    The control renders on the full add/edit form, and blank is one of its
    three choices."""
    from django.urls import reverse

    user = _user()
    client.force_login(user)
    body = client.get(reverse("crm:contact_new")).content.decode()
    assert 'name="recruiting_contact"' in body
    assert "Work it out from their role" in body
    assert "Yes, recruiting contact" in body


def test_a_blank_answer_stays_null_rather_than_becoming_false():
    """"" is not "no". Storing False would be the student asserting something
    they never said, and it would freeze the role-text fallback shut."""
    from crm.forms import ContactForm

    form = ContactForm(data={"name": "Nobody Asked", "recruiting_contact": ""})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["recruiting_contact"] is None

    yes = ContactForm(data={"name": "A Recruiter", "recruiting_contact": "yes"})
    assert yes.is_valid(), yes.errors
    assert yes.cleaned_data["recruiting_contact"] is True

    no = ContactForm(data={"name": "A Banker", "recruiting_contact": "no"})
    assert no.is_valid(), no.errors
    assert no.cleaned_data["recruiting_contact"] is False


# ---------------------------------------------------------------------------
# 6. Naming the role a deadline belongs to.
# ---------------------------------------------------------------------------
# `firm_openings` has always fetched `Opportunity.title` and the card threw it
# away, so a surface that HAD looked at the board read like one that had run a
# query: "A role there closes Sep 30." The title is already in hand and every
# other clause in this module is read off a row, so this one is too.
def _role_opening(title, days=30):
    return {
        "kind": rel.OPENING_ROLE_DEADLINE,
        "date": timezone.localdate() + timedelta(days=days),
        "days": days,
        "label": "Applications close",
        "title": title,
    }


def test_a_titled_role_deadline_names_the_role():
    reason = rel.keep_warm_reason({
        "contact": {"warmth": "chatted"},
        "relevance": rel.REL_TIERED,
        "relevance_tier": 1,
        "opening": _role_opening("IB Summer Analyst"),
    })
    assert "The IB Summer Analyst role closes" in reason
    assert "A role there closes" not in reason


def test_an_untitled_role_deadline_keeps_the_old_wording():
    """A board row with no title is common enough that this is the fallback,
    not an error path."""
    for title in (None, "", "   "):
        reason = rel.keep_warm_reason({
            "contact": {"warmth": "chatted"},
            "relevance": rel.REL_TIERED,
            "relevance_tier": 1,
            "opening": _role_opening(title),
        })
        assert "A role there closes" in reason


def test_an_absurd_scraped_title_falls_back_rather_than_wrapping_the_card():
    """Bank career sites publish requisition strings, not titles. Naming one
    buries the date the sentence exists to deliver."""
    monster = ("2027 Global Markets Summer Analyst Program - Hong Kong - "
               "Sales and Trading - Requisition 24081")
    assert len(monster) > rel._MAX_ROLE_TITLE_CHARS
    reason = rel.keep_warm_reason({
        "contact": {"warmth": "chatted"},
        "relevance": rel.REL_TIERED,
        "relevance_tier": 1,
        "opening": _role_opening(monster),
    })
    assert "A role there closes" in reason
    assert "Requisition" not in reason


def test_a_role_title_is_never_case_folded():
    """Case-folding would be the one thing here that alters a fact rather than
    reporting it. A role title is a name."""
    reason = rel.keep_warm_reason({
        "contact": {"warmth": "chatted"},
        "relevance": rel.REL_TIERED,
        "relevance_tier": 1,
        "opening": _role_opening("M&A Analyst, TMT"),
    })
    assert "The M&A Analyst, TMT role closes" in reason
