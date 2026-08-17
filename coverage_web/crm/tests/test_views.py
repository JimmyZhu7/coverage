"""Route-level tests for the CRM UI (docs/build-plan.md §4 weekly list, §5
mailto-BCC compose + visible warmth movement, §6 fit score, §9 tenant
isolation).

Two DB modes are used deliberately:

- Plain `@pytest.mark.django_db` for read-only views (week list, detail,
  fit score, tenant 404): the view only reads through the Django ORM.
- `@pytest.mark.django_db(transaction=True)` for the log-a-touch POST: it
  goes through `crm.services.log_touch`, which opens its OWN psycopg
  connection (see services.py). That second connection can only see rows
  the test committed, which the transactional fixture guarantees — the same
  reasoning as test_services.py.
"""

from __future__ import annotations

import re

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from analytics.models import ProductEvent
from coverage_domain import cadence
from crm.models import Contact, Touch, UserFirm
from crm.views import TUNABLE_CADENCE_PARAMS, _cadence_params
from directory.models import Firm

User = get_user_model()


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="x")


# ---------------------------------------------------------------------------
# 1. Weekly priority list renders ranked cadence actions.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_week_list_renders_actions_ranked_by_priority(client):
    user = _user()
    now = timezone.now()

    # A chat happened 2 days ago with no thank-you yet -> branch 1, OVERDUE
    # (>24h) -> priority 0.
    thanks = Contact.all_objects.create(
        user=user, name="Priya Overdue", warmth="chatted", thread_state="chat_done"
    )
    Touch.all_objects.create(
        user=user, contact=thanks, ts=now - timedelta(days=2), kind="chat", channel="coffee_chat"
    )
    # A brand-new cold contact, never contacted -> branch 6 first_outreach ->
    # priority 1.
    Contact.all_objects.create(
        user=user, name="Sam Newcold", warmth="cold", thread_state="no_reply"
    )

    client.force_login(user)
    resp = client.get(reverse("crm:week"))
    assert resp.status_code == 200
    body = resp.content.decode()

    assert "Send thank-you" in body
    assert "First outreach" in body
    # Priority 0 (thank-you) must sort above priority 1 (first outreach).
    assert body.index("Priya Overdue") < body.index("Sam Newcold")


# ---------------------------------------------------------------------------
# 2. Logging a touch moves warmth and the htmx response reflects the movement.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_log_touch_moves_warmth_and_response_shows_movement(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Alex Cold")
    assert contact.warmth == "cold"

    client.force_login(user)
    resp = client.post(
        reverse("crm:log_touch", args=[contact.id]),
        {"kind": "reply_received", "channel": "email"},
    )
    assert resp.status_code == 200
    body = resp.content.decode()

    # The fragment shows the movement, not just the new state.
    assert "cold" in body and "replied" in body
    assert "→" in body  # from -> to arrow
    assert "Logged" in body

    contact.refresh_from_db()
    assert contact.warmth == "replied"
    assert contact.thread_state == "replied"

    # A real append-only touch landed, and the funnel event was recorded.
    assert Touch.all_objects.filter(user=user, contact=contact, kind="reply_received").exists()
    ev = ProductEvent.all_objects.filter(user=user, event="touch_logged").first()
    assert ev is not None
    assert ev.props.get("source") == "manual"


@pytest.mark.django_db(transaction=True)
def test_log_touch_rejects_unknown_kind_without_writing(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Alex Cold")
    client.force_login(user)

    resp = client.post(
        reverse("crm:log_touch", args=[contact.id]),
        {"kind": "not_a_kind", "channel": "email"},
    )
    assert resp.status_code == 200
    assert "Pick an interaction type" in resp.content.decode()

    contact.refresh_from_db()
    assert contact.warmth == "cold"
    assert not Touch.all_objects.filter(user=user, contact=contact).exists()


# ---------------------------------------------------------------------------
# 2b. Today's "Park it" quick action is a state change, not a fake touch.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_today_park_sets_thread_state_and_writes_no_maintain_touch(client):
    """Regression: `today_act`'s 'sent' verb used to route "park" through
    `log_touch(kind='maintain')`. TOUCH_TRANSITIONS['maintain'] == (None,
    None), so that logged a permanent "Kept warm" touch and changed no
    state -- the contact reappeared in the queue with the same nag forever.
    Parking must be an audited thread_state change instead."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Old Lead", warmth="cold", thread_state="no_reply",
    )
    client.force_login(user)

    resp = client.post(reverse("crm:today_act", args=[contact.id, "park"]))
    assert resp.status_code == 200

    contact.refresh_from_db()
    assert contact.thread_state == "parked"
    assert not Touch.all_objects.filter(user=user, contact=contact, kind="maintain").exists()
    # set_contact_state's own audit touch still leaves a trail -- just not a
    # fabricated interaction.
    assert Touch.all_objects.filter(
        user=user, contact=contact, kind="manual_override"
    ).exists()


# ---------------------------------------------------------------------------
# 3. mailto compose carries the correct BCC (the user's capture address).
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_mailto_link_contains_capture_bcc(client):
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com"
    )
    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.id]))
    assert resp.status_code == 200
    body = resp.content.decode()

    # bcc=u-<slug>@in.coverage.app, URL-encoded (@ -> %40).
    expected = f"bcc=u-{user.capture_slug}%40in.coverage.app"
    assert expected in body


# ---------------------------------------------------------------------------
# 5. Fit-score display: band + axes + reasoning, and score_viewed recorded.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_contact_detail_shows_fit_score_axes_and_reasoning(client):
    user = _user()
    now = timezone.now()
    contact = Contact.all_objects.create(
        user=user, name="Dana MD", role="Managing Director", school_affiliation=True
    )
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=10), kind="outreach", channel="email")
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=9), kind="reply_received", channel="email")
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=3), kind="chat", channel="coffee_chat")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.id]))
    assert resp.status_code == 200
    body = resp.content.decode()

    assert "Fit Score" in body
    for axis in ("Depth", "Responsiveness", "Recency", "Leverage"):
        assert axis in body
    # The deterministic reasoning line rendered.
    assert 'class="reasoning"' in body

    assert ProductEvent.all_objects.filter(user=user, event="score_viewed").exists()


@pytest.mark.django_db
def test_responsiveness_meta_does_not_argue_with_itself(client):
    """A contact discovered by mailbox scan has their reply on record and no
    outbound of yours. The axis printed "1 reply to 0 notes" — a sentence
    contradicting itself, and unactionable: a student cannot tell what to do
    about a reply to nothing. It also called a coffee chat a "reply", so the
    SAME touch read "1 chat evidenced" on Depth and "1 reply" one line below."""
    user = _user()
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="Ellen Chung")
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=2),
                             kind="chat", channel="email", note="Discovered by mailbox scan")

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "1 from them, nothing logged from you" in body
    assert "0 note" not in body
    assert "1 reply to" not in body, "a chat is not a reply, and there was no note"


@pytest.mark.django_db
def test_responsiveness_meta_names_both_sides_when_both_exist(client):
    """The normal case still reads as a ratio, just without the "reply" noun
    that the counter cannot honestly promise."""
    user = _user()
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="Dana MD")
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=10),
                             kind="outreach", channel="email")
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=9),
                             kind="reply_received", channel="email")

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "1 from them, 1 from you" in body
    assert "nothing logged from you" not in body


# ---------------------------------------------------------------------------
# 5a. The Firm Fit rail's Structural axis printed the scoring engine's own
# method label at a student: "rules v1: region match, track no". Live on
# /app/contacts/484/ (Travis Chen, Amazon). Rendered, not unit-tested on the
# template string, because the defect is what reaches the page.
# ---------------------------------------------------------------------------
def _firm_fit_contact(user, *, firm_regions, firm_tracks):
    firm = Firm.objects.create(
        slug="amazon", name="Amazon", regions=firm_regions, tracks=firm_tracks
    )
    return Contact.all_objects.create(
        user=user, name="Travis Chen", role="Sales", firm=firm
    )


@pytest.mark.django_db
def test_structural_axis_speaks_english_not_scoring_engine(client):
    user = _user()
    user.regions = ["hk", "us"]
    user.tracks = ["ib", "st", "pe"]
    user.save(update_fields=["regions", "tracks"])
    contact = _firm_fit_contact(user, firm_regions=["us"], firm_tracks=["corp-strat"])

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "rules v1" not in body, "a scoring-engine version label is not copy"
    assert "track no" not in body, "the bare yesno token is not a sentence"
    assert "in your region, outside your track" in body


@pytest.mark.django_db
def test_structural_axis_never_prints_a_bare_question_mark(client):
    """`_overlap` returns None when EITHER side's list is empty, and half the
    firms in the directory carry `regions=[]`. The old yesno third branch
    rendered that as a literal "?" — a punctuation mark standing in for a
    sentence nobody wrote."""
    user = _user()
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save(update_fields=["regions", "tracks"])
    contact = _firm_fit_contact(user, firm_regions=[], firm_tracks=[])

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "region not listed, track not listed" in body
    assert "region ?" not in body and "track ?" not in body


# ---------------------------------------------------------------------------
# 5b. The Leverage axis said "role unknown" on a page whose own header
# printed the role. Live on /app/contacts/484/: eyebrow "Amazon · Sales",
# rail "Leverage 30.0 — role unknown", 37 contacts affected.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_leverage_does_not_call_a_shown_role_unknown(client):
    """"Sales" is a real role the header prints and the keyword table does
    not recognise. The rail may say it could not rank it; it may not say we
    do not know it."""
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Travis Chen", role="Sales")

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "· Sales" in body, "the header still prints the role"
    assert "role unknown" not in body
    assert "no seniority read from this role" in body


@pytest.mark.django_db
def test_leverage_says_no_role_on_file_when_there_is_no_role(client):
    """The other half of the split. A blank role is the ONE case the old
    string was true for, and it keeps a true string."""
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Nameless Role", role="")

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "no role on file" in body
    assert "no seniority read from this role" not in body


@pytest.mark.django_db
def test_a_ranked_role_still_reports_its_seniority(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Dana MD", role="Managing Director")

    client.force_login(user)
    raw = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    body = re.sub(r"\s+", " ", raw)

    assert "seniority 100.0/100 from role" in body
    assert "no seniority read from this role" not in body
    assert "no role on file" not in body


# ---------------------------------------------------------------------------
# 5b. Manual-override touch notes: the "manual override: <col>=<val>, ..."
# audit prefix is machine bookkeeping (services.set_contact_state's own
# comment), not something a user reading their own History should see —
# confirmed live on James Bai (contact id=312), whose top History entry
# rendered "thread_state=chat_done — Correction: ..." verbatim. Stripped at
# display, same posture as the existing `[gmail:...]`/`[capture:...]`
# marker-stripping right above `_display_note` in crm/views.py.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_manual_override_column_value_prefix_is_hidden_from_history(client):
    user = _user()
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="James Bai", role="IB Associate")
    Touch.all_objects.create(
        user=user, contact=contact, ts=now, kind="manual_override",
        note=("manual override: thread_state=chat_done — Correction: a "
              "duplicate chat_scheduled was logged in error. Restoring the "
              "correct chat_done state."),
    )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.id]))
    body = resp.content.decode()

    assert "thread_state=chat_done" not in body
    assert "Correction: a duplicate chat_scheduled was logged in error." in body


@pytest.mark.django_db
def test_manual_override_with_no_human_note_shows_no_column_dump(client):
    """A bare "manual override: warmth=hot" (no " — <note>" suffix) has no
    human-authored text to keep — the whole column=value dump is hidden
    rather than shown as if it meant something to the reader."""
    user = _user()
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="Dana Cole", role="Analyst")
    Touch.all_objects.create(
        user=user, contact=contact, ts=now, kind="manual_override",
        note="manual override: warmth=hot",
    )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.id]))
    body = resp.content.decode()

    assert "warmth=hot" not in body
    assert "Manual override" in body


# ---------------------------------------------------------------------------
# 9. Tenant isolation: user B cannot see user A's contact.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_user_b_cannot_view_user_a_contact(client):
    user_a = _user("a@example.com")
    user_b = _user("b@example.com")
    contact_a = Contact.all_objects.create(user=user_a, name="A Secret Contact")

    client.force_login(user_b)
    resp = client.get(reverse("crm:contact_detail", args=[contact_a.id]))
    assert resp.status_code == 404

    # And A's contact never appears in B's list.
    resp = client.get(reverse("crm:contact_list"))
    assert resp.status_code == 200
    assert "A Secret Contact" not in resp.content.decode()


@pytest.mark.django_db(transaction=True)
def test_user_b_cannot_log_touch_on_user_a_contact(client):
    user_a = _user("a@example.com")
    user_b = _user("b@example.com")
    contact_a = Contact.all_objects.create(user=user_a, name="A Secret Contact")

    client.force_login(user_b)
    resp = client.post(
        reverse("crm:log_touch", args=[contact_a.id]),
        {"kind": "reply_received", "channel": "email"},
    )
    assert resp.status_code == 404
    contact_a.refresh_from_db()
    assert contact_a.warmth == "cold"  # untouched


@pytest.mark.django_db
def test_contact_list_renders(client):
    user = _user()
    Contact.all_objects.create(user=user, name="Listed Person")
    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    assert resp.status_code == 200
    assert "Listed Person" in resp.content.decode()


@pytest.mark.django_db
def test_week_requires_login(client):
    resp = client.get(reverse("crm:week"))
    # login_required redirects unauthenticated users away.
    assert resp.status_code in (301, 302)


# ---------------------------------------------------------------------------
# Add / edit contact — the hand-add path (was: no way to create a contact).
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_contact_new_creates_scoped_to_user(client):
    user = _user()
    client.force_login(user)
    resp = client.post(
        reverse("crm:contact_new"),
        {"name": "Ada Lovelace", "firm_text": "A Boutique", "role": "Analyst",
         "email": "ada@example.com", "linkedin": "", "school": "", "angle": "", "notes": ""},
    )
    assert resp.status_code == 302
    c = Contact.all_objects.get(name="Ada Lovelace")
    assert c.user_id == user.id
    assert c.source == "manual"
    assert c.warmth == "cold"  # ratchet default, never set by the form
    assert ProductEvent.all_objects.filter(event="contact_added", user=user).exists()


@pytest.mark.django_db
def test_contact_new_requires_a_name(client):
    client.force_login(_user())
    resp = client.post(reverse("crm:contact_new"), {"name": "", "firm_text": "X"})
    assert resp.status_code == 200  # re-renders the form
    assert Contact.all_objects.filter(firm_text="X").count() == 0


@pytest.mark.django_db
def test_contact_edit_requires_login(client):
    user = _user()
    c = Contact.all_objects.create(user=user, name="Grace")
    resp = client.get(reverse("crm:contact_edit", args=[c.pk]))
    assert resp.status_code == 302 and "/accounts/login/" in resp["Location"]


@pytest.mark.django_db
def test_user_b_cannot_edit_user_a_contact(client):
    a = _user("a@example.com")
    b = _user("b@example.com")
    c = Contact.all_objects.create(user=a, name="Alan")
    client.force_login(b)
    resp = client.post(reverse("crm:contact_edit", args=[c.pk]), {"name": "Hacked"})
    assert resp.status_code == 404
    c.refresh_from_db()
    assert c.name == "Alan"


# ---------------------------------------------------------------------------
# Network page region scoping.
# ---------------------------------------------------------------------------
# The HK and US tabs used to filter on the FIRM's `regions` array. Most bulge
# brackets carry ['us', 'hk'], so one contact matched BOTH tabs and the two
# lists read as near-duplicates. A firm spans regions; a person does not.
def _names_in_scope(client, scope):
    """The contact names the Network page shows under one region tab."""
    resp = client.get(reverse("crm:contact_list"), {"scope": scope})
    assert resp.status_code == 200
    return {
        card["c"].name
        for section in resp.context["sections"]
        for card in section["cards"]
    }


@pytest.mark.django_db
def test_explicit_region_puts_a_contact_in_one_tab_only(client):
    """An explicitly-set region wins outright, even at a firm that recruits in
    both places — the contact appears in her own tab and nowhere else."""
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Hana HK", firm=dual, region="hk",
                              warmth="replied")
    Contact.all_objects.create(user=user, name="Uma US", firm=dual, region="us",
                              warmth="replied")

    client.force_login(user)
    assert _names_in_scope(client, "hk") == {"Hana HK"}
    assert _names_in_scope(client, "us") == {"Uma US"}


@pytest.mark.django_db
def test_multi_region_firm_no_longer_puts_one_contact_in_both_tabs(client):
    """The regression itself: one person at a dual-region firm must not be
    counted by both lists."""
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Solo Person", firm=dual, region="hk",
                              warmth="replied")

    client.force_login(user)
    hk, us = _names_in_scope(client, "hk"), _names_in_scope(client, "us")
    assert hk == {"Solo Person"}
    assert us == set()
    assert hk & us == set()


@pytest.mark.django_db
def test_region_scope_matches_the_cadence_engines_answer(client):
    """The page and the engine must never disagree about where someone works.

    The invariant is unchanged; the answer both sides now give is not. This
    test used to assert that a blank `region` with an HK-flavoured `source`
    resolved to Hong Kong in BOTH places, because `cadence.contact_region`
    fell back to `infer_region(source)`. That fallback is retired: a blank
    region is unknown, in the engine and on the page alike, so the contact
    takes the firm fallback and appears under both of the firm's regions
    rather than being asserted into Hong Kong by a provenance string.
    """
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    # Blank region survives save() here: the firm spans both regions, so
    # `default_region_from_firm` declines to guess.
    c = Contact.all_objects.create(
        user=user, name="Legacy Row", firm=dual, source="Apollo HK campaign",
        warmth="replied",
    )
    assert c.region == ""
    assert cadence.contact_region({"region": c.region, "source": c.source}) is None

    client.force_login(user)
    assert _names_in_scope(client, "hk") == {"Legacy Row"}
    assert _names_in_scope(client, "us") == {"Legacy Row"}
    # And the page says it's a guess rather than passing it off as confirmed —
    # which under the old inference it could not, because the contact looked
    # like a confident HK match.
    resp = client.get(reverse("crm:contact_list"), {"scope": "hk"})
    assert resp.context["unconfirmed_total"] == 1


@pytest.mark.django_db
def test_genuinely_unknown_region_shows_in_both_tabs_but_flagged(client):
    """No region set and no source to guess from is genuinely unknown. Hiding
    the contact would be worse than showing her, so she appears under every
    region her firm recruits in — but the page says so rather than passing the
    guess off as a confirmed match."""
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Unknown Person", firm=dual, source="",
                              warmth="replied")

    client.force_login(user)
    assert _names_in_scope(client, "hk") == {"Unknown Person"}
    assert _names_in_scope(client, "us") == {"Unknown Person"}

    resp = client.get(reverse("crm:contact_list"), {"scope": "hk"})
    assert resp.context["unconfirmed_total"] == 1
    body = resp.content.decode()
    assert "no region set" in body
    # ...and the card must not render a region pill asserting one.
    card = resp.context["sections"][0]["cards"][0]
    assert card["region"] == ""


@pytest.mark.django_db
def test_confirmed_region_contacts_are_not_flagged_as_guesses(client):
    """The caveat is only for guesses — a set region must not trip it."""
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Hana HK", firm=dual, region="hk",
                              warmth="replied")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "hk"})
    assert resp.context["unconfirmed_total"] == 0
    assert "no region set" not in resp.content.decode()


@pytest.mark.django_db
def test_firm_cards_still_span_regions(client):
    """Line-710's firm filter is deliberately NOT changed: a firm legitimately
    recruits in both places, so a dual-region firm belongs on both boards. Its
    contact counts follow the scoped contact list, though, so the HK board
    counts only its HK people."""
    user = _user()
    dual = Firm.objects.create(slug="dual-bank", name="Dual Bank", regions=["us", "hk"])
    UserFirm.all_objects.create(user=user, firm=dual, tier=1)
    Contact.all_objects.create(user=user, name="Hana HK", firm=dual, region="hk",
                              warmth="replied")
    Contact.all_objects.create(user=user, name="Uma US", firm=dual, region="us",
                              warmth="replied")

    client.force_login(user)
    for scope in ("hk", "us"):
        resp = client.get(reverse("crm:contact_list"), {"scope": scope})
        cards = [c for s in resp.context["tier_sections"] for c in s["cards"]]
        assert [c["firm"].name for c in cards] == ["Dual Bank"], scope
        # One firm card on both boards, but one contact each, not two.
        assert cards[0]["contact_count"] == 1, scope


# ---------------------------------------------------------------------------
# Cadence override whitelist.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_out_of_range_max_cold_touches_override_is_dropped():
    """`_cadence_params` is the read-side gate: an out-of-range value is
    DROPPED, never handed to the engine, so the queue falls back to the
    default rather than honoring a nonsense value."""
    user = _user()
    low, high = TUNABLE_CADENCE_PARAMS["max_cold_touches"]

    user.cadence_params = {"max_cold_touches": high + 1}
    assert "max_cold_touches" not in _cadence_params(user)

    user.cadence_params = {"max_cold_touches": low - 1}
    assert "max_cold_touches" not in _cadence_params(user)

    # Non-integers and bools are rejected too (bool is an int subclass).
    user.cadence_params = {"max_cold_touches": True}
    assert "max_cold_touches" not in _cadence_params(user)

    # An in-range value survives.
    user.cadence_params = {"max_cold_touches": 1}
    assert _cadence_params(user)["max_cold_touches"] == 1


@pytest.mark.django_db
def test_max_cold_touches_cannot_be_raised_past_two():
    """The range itself, not just the default, enforces "never a second
    follow-up" — this is what stops a stray override of 3+ from reopening
    the staged-follow-up behavior that was tried and reverted in cadence.py
    (see its DIVERGENCE note). Capped at the web layer because
    coverage_domain.cadence trusts whatever `params` it's handed by design;
    see cadence's own test that pins that boundary."""
    low, high = TUNABLE_CADENCE_PARAMS["max_cold_touches"]
    assert (low, high) == (1, 2)

    user = _user()
    user.cadence_params = {"max_cold_touches": 3}
    assert "max_cold_touches" not in _cadence_params(user), (
        "an out-of-range override must fall back to the default (2), "
        "never enable a second follow-up"
    )


@pytest.mark.django_db
def test_singapore_tab_still_falls_back_to_the_firm(client):
    """`Contact.region` is a us/hk vocabulary, so nobody can ever resolve to
    "sg". Asking the contact there would empty the tab, so Singapore and Europe
    keep filtering on the firm — the only evidence the model can express."""
    user = _user()
    sg = Firm.objects.create(slug="sg-bank", name="SG Bank", regions=["sg"])
    # An explicit HK region must not hide her from the Singapore tab: "hk" is
    # simply the nearest label the contact vocabulary offers.
    Contact.all_objects.create(user=user, name="Sinead SG", firm=sg, region="hk",
                               warmth="replied")

    client.force_login(user)
    assert _names_in_scope(client, "sg") == {"Sinead SG"}
    assert "sg" not in Contact.REGION_VALUES


# ---------------------------------------------------------------------------
# Archive / unarchive — the contact lifecycle's exit AND its way back.
#
# `Contact.archived` shipped in the first migration and every board query
# filters on it, but nothing could SET it from the UI and no page listed what
# it hid. It was a one-way trapdoor operated only by automated paths: 25 of
# the founder's 137 contacts sat archived and invisible, and because both
# capture resolvers filter `archived=False`, a later genuine reply from one of
# them forked a NEW contact rather than resurrecting the old one.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_archive_hides_a_contact_from_the_network_board(client):
    user = _user()
    keep = Contact.all_objects.create(user=user, name="Stays Visible", warmth="replied")
    gone = Contact.all_objects.create(user=user, name="Gets Archived", warmth="replied")
    client.force_login(user)

    resp = client.post(reverse("crm:contact_archive", args=[gone.pk]))
    assert resp.status_code == 302

    gone.refresh_from_db()
    assert gone.archived is True
    keep.refresh_from_db()
    assert keep.archived is False

    listed = client.get(reverse("crm:contact_list"))
    names = {
        card["c"].name
        for section in listed.context["sections"]
        for card in section["cards"]
    }
    assert names == {"Stays Visible"}


@pytest.mark.django_db
def test_archived_contacts_have_a_view_of_their_own(client):
    """The view that makes archiving reversible in practice rather than only
    in principle — before it, an archived row appeared on no page at all."""
    user = _user()
    Contact.all_objects.create(user=user, name="Visible One", warmth="replied")
    Contact.all_objects.create(user=user, name="Hidden One", warmth="replied",
                               archived=True)
    client.force_login(user)

    resp = client.get(reverse("crm:contact_archived"))
    assert resp.status_code == 200
    assert [c.name for c in resp.context["contacts"]] == ["Hidden One"]
    assert "Visible One" not in resp.content.decode()


@pytest.mark.django_db
def test_unarchive_round_trips_with_the_history_intact(client):
    """Archive then unarchive must return the SAME row — not a new one — with
    every touch still hanging off it. That identity is the whole point: a
    forked contact loses the history the CRM exists to hold."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Round Trip", warmth="replied")
    Touch.all_objects.create(
        user=user, contact=c, ts=timezone.now() - timedelta(days=3),
        kind="reply_received", channel="email",
    )
    client.force_login(user)

    client.post(reverse("crm:contact_archive", args=[c.pk]))
    c.refresh_from_db()
    assert c.archived is True

    client.post(reverse("crm:contact_unarchive", args=[c.pk]))
    c.refresh_from_db()
    assert c.archived is False

    assert Contact.objects.for_user(user).count() == 1, "no fork"
    assert Touch.objects.for_user(user).filter(contact=c).count() == 1
    assert c.warmth == "replied", "warmth untouched by the lifecycle flag"


@pytest.mark.django_db
def test_an_archived_contacts_page_is_still_reachable(client):
    """Otherwise the archived list would have nowhere to link and unarchiving
    would have no home."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Hidden One", archived=True)
    client.force_login(user)

    resp = client.get(reverse("crm:contact_detail", args=[c.pk]))
    assert resp.status_code == 200
    assert "Unarchive" in resp.content.decode()


@pytest.mark.django_db
def test_archive_is_post_only_and_tenant_scoped(client):
    """A GET that hides a contact would fire on any crawl or link prefetch;
    and another tenant's id must 404 exactly like a missing one."""
    user = _user()
    other = _user("other@example.com")
    mine = Contact.all_objects.create(user=user, name="Mine")
    theirs = Contact.all_objects.create(user=other, name="Theirs")
    client.force_login(user)

    assert client.get(reverse("crm:contact_archive", args=[mine.pk])).status_code == 405
    mine.refresh_from_db()
    assert mine.archived is False

    assert client.post(reverse("crm:contact_archive", args=[theirs.pk])).status_code == 404
    theirs.refresh_from_db()
    assert theirs.archived is False


# ---------------------------------------------------------------------------
# Contact detail redesign (2026-08-05): the page speaks in words, not enums.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_contact_detail_never_shows_raw_state_enums(client):
    """The old page printed a chip reading `no_reply` and history rows like
    `reply_received · email`. The enums stay in the database now."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
        warmth="cold", thread_state="no_reply",
    )
    Touch.all_objects.create(user=user, contact=contact, kind="reply_received",
                             channel="email", ts=timezone.now())
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    # The log form's <option value="reply_received"> is POST vocabulary, not
    # display text — strip form controls before asserting what the page SAYS.
    display = re.sub(r"<select.*?</select>", "", body, flags=re.DOTALL)
    assert "no_reply" not in display
    assert "reply_received" not in display
    assert "No reply yet" in display, "the state, in words"
    assert "They replied" in display, "the history row, in words"


@pytest.mark.django_db
def test_the_fit_scores_timeline_axis_names_the_event_in_words(client):
    """The Timeline axis printed the raw firm_dates enum: "app_close in 77d,
    2 warm". The same card already said the same event in English two lines
    above, because the scorer's reasoning string runs it through a verb map —
    so one panel contradicted itself on a single screen.

    Goes through the real page, not `scoring.score_firm` directly: the enum
    reached the user across the view/template seam, which is where it had to
    be caught."""
    from directory.models import FirmDate

    user = _user()
    now = timezone.now()
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs", regions=["us"])
    contact = Contact.all_objects.create(
        user=user, name="Dana MD", firm=firm, role="Managing Director",
    )
    Touch.all_objects.create(user=user, contact=contact, ts=now - timedelta(days=3),
                             kind="chat", channel="coffee_chat")
    FirmDate.objects.create(
        firm=firm, cycle="sa2028_ib", region="us", event_kind="app_close",
        date=(now + timedelta(days=77)).date(), precision="day", confidence=1.0,
    )

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    assert "app_close" not in body
    assert "Applications close in 77d" in body


@pytest.mark.django_db
def test_the_state_line_does_not_say_one_thing_twice(client):
    """warmth=replied + thread=replied used to render "Replied · They
    replied" — the exact redundancy the sentence exists to remove."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
        warmth="replied", thread_state="replied",
    )
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    assert "They replied" in body
    assert "Replied · They replied" not in body


@pytest.mark.django_db(transaction=True)
def test_the_swap_fragment_is_the_whole_grid(client):
    # transaction=True: log_touch goes through crm.services, which opens its
    # OWN psycopg connection and cannot see rows inside the test transaction.
    """log_touch returns #contact-live for an outerHTML swap. The redesign
    made that element the two-column grid, so the swap must carry BOTH
    columns — a fragment missing the rail would blank the scores on the
    first logged touch."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
    )
    client.force_login(user)
    resp = client.post(
        reverse("crm:log_touch", args=[contact.id]),
        {"kind": "outreach", "channel": "email", "note": ""},
    )
    body = resp.content.decode()
    assert 'id="contact-live"' in body and "cd-grid" in body
    assert "Fit Score" in body, "the rail rides in the swap"
    assert "Compose (BCC Capture)" in body, "so does the reach card"
    assert "Logged" in body, "and the movement flag"


@pytest.mark.django_db
def test_sync_bookkeeping_markers_never_reach_the_page(client):
    """`[gmail:<thread>]` is the sync's idempotency marker and
    `[capture:<id>]` the BCC pipeline's provenance pointer — load-bearing in
    the database, noise on the page ("the user doesn't learn anything").
    Stripped at display only: the stored note keeps its marker."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now(),
        note="[gmail:19f893368bdf46ac] outreach sent 2026-07-24, no reply yet",
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="reply_received", channel="email",
        ts=timezone.now(),
        note="[capture:42] inbound via gmail",
    )
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    assert "[gmail:" not in body and "[capture:" not in body
    assert "outreach sent 2026-07-24, no reply yet" in body, "the human half survives"
    # And the database still carries the marker — display-only stripping.
    stored = Touch.all_objects.filter(contact=contact, kind="outreach").get()
    assert stored.note.startswith("[gmail:")
