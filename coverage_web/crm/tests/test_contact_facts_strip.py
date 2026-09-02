"""What the contact page already knew and would not say (WS-CRM-04).

`audit-crm-lifecycle.md` D8, measured on the founder's 265 live contacts:

  * 171 carry a region the page never showed — it lived only on the edit
    form, behind a click, on another URL;
  * 219 of 265 sit at a tiered firm and the tier was nowhere on the page;
  * 34 sit at a firm with a confirmed FUTURE close (Goldman 2026-09-22, HSBC
    2026-10-30) whose only trace was "app close in 34d" inside the Firm Fit
    meta, with no date and no market;
  * 12 debriefs existed and the page had no list and no link, and 3 of them
    answered "would advocate: yes" with the promotion never offered again.

Every value was already inside `_contact_live_context`'s reach. The two new
queries are the tier row and the debrief list, and the budget below is what
holds that to two.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import ChatDebrief, Contact, Touch, UserFirm
from directory.models import Firm, FirmDate

User = get_user_model()


def _user(email="facts@example.com", **kw):
    return User.objects.create_user(email=email, password="pw12345!", **kw)


def _goldman():
    return Firm.objects.create(slug="goldman-sachs", name="Goldman Sachs",
                               regions=["us", "hk"])


def _close(firm, *, days, region="us", confidence=1.0, precision="day"):
    return FirmDate.objects.create(
        firm=firm, event_kind="app_close", region=region, cycle="sa2028",
        date=timezone.localdate() + timedelta(days=days),
        confidence=confidence, precision=precision,
    )


@pytest.mark.django_db
def test_the_strip_states_region_tier_and_the_next_confirmed_date(client):
    user = _user()
    firm = _goldman()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1, status="target")
    contact = Contact.all_objects.create(
        user=user, name="Pat", firm=firm, region="us",
        region_source=Contact.REGION_SOURCE_USER,
    )
    _close(firm, days=21, region="us")

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()

    assert "United States" in body
    assert "Set by you" in body          # the region_source tooltip
    assert "Tier 1" in body
    assert "Applications close" in body
    # The market travels with the date. An unmarked firm date reads as
    # global, which is exactly how the founder's September calendar told him
    # about a Hong Kong deadline.
    assert "· US" in body


@pytest.mark.django_db
def test_a_hong_kong_date_says_hong_kong(client):
    """The defect this criterion exists for. A date from the other market on
    a page about one person is not a small omission."""
    user = _user()
    firm = _goldman()
    contact = Contact.all_objects.create(user=user, name="Pat", firm=firm)
    _close(firm, days=30, region="hk")

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "· HK" in body


@pytest.mark.django_db
def test_an_estimated_date_is_not_drawn_as_a_confirmed_one(client):
    """P1 and P5. `firm_date_confidence` is the one bar, the same one the
    deadlines rail and the calendar's confirmed layer read: a month-level
    estimate is a real fact about the firm and not one to put a date on."""
    user = _user()
    firm = _goldman()
    contact = Contact.all_objects.create(user=user, name="Pat", firm=firm)
    _close(firm, days=40, precision="estimated", confidence=0.6)

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "Applications close" not in body


@pytest.mark.django_db
def test_a_past_date_is_not_the_next_one(client):
    user = _user()
    firm = _goldman()
    contact = Contact.all_objects.create(user=user, name="Pat", firm=firm)
    _close(firm, days=-10)

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "Applications close" not in body


@pytest.mark.django_db
def test_a_blank_region_asks_rather_than_guesses(client):
    """P1. 94 of 265 rows are blank and 90 of them sit at a firm recruiting
    in both markets — the one place the deterministic rule correctly refuses
    to answer. The strip says so and links the tab that asks."""
    user = _user()
    firm = _goldman()
    contact = Contact.all_objects.create(user=user, name="Pat", firm=firm)
    contact.refresh_from_db()
    assert contact.region == ""

    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "Region unknown" in body
    assert "?scope=unplaced" in body
    assert "United States" not in body.split('class="cd-facts"')[1].split("</p>")[0]


@pytest.mark.django_db
def test_an_assessment_firm_says_so_and_a_campus_firm_says_nothing(client):
    user = _user()
    quant = Firm.objects.create(slug="jane-street", name="Jane Street",
                                recruiting_style=Firm.RECRUITING_STYLE_ASSESSMENT)
    contact = Contact.all_objects.create(user=user, name="Pat", firm=quant)
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "Assessment: test-gated, a chat does not" in body

    plain = Contact.all_objects.create(user=user, name="Sam", firm=_goldman())
    body = client.get(reverse("crm:contact_detail", args=[plain.pk])).content.decode()
    assert "Assessment: test-gated" not in body


@pytest.mark.django_db(transaction=True)
def test_a_would_advocate_debrief_offers_the_promotion(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Pat", warmth="chatted")
    touch = Touch.all_objects.create(user=user, contact=contact, kind="chat",
                                     channel="video", ts=timezone.now())
    ChatDebrief.all_objects.create(user=user, contact=contact, touch=touch,
                                   learned="They run the SA programme.",
                                   advocate_answer="yes")
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert "Chats" in body
    assert reverse("crm:debrief_promote", args=[touch.pk]) in body
    assert "They run the SA programme." in body


@pytest.mark.django_db
def test_an_already_promoted_debrief_does_not_offer_again(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Pat", warmth="advocate")
    touch = Touch.all_objects.create(user=user, contact=contact, kind="chat",
                                     channel="video", ts=timezone.now())
    ChatDebrief.all_objects.create(user=user, contact=contact, touch=touch,
                                   advocate_answer="yes", promoted=True)
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert reverse("crm:debrief_promote", args=[touch.pk]) not in body


@pytest.mark.django_db
def test_a_contact_with_no_debriefs_renders_no_chats_section(client):
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Pat")
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()
    assert 'class="cd-chats"' not in body


@pytest.mark.django_db
def test_a_bare_contact_renders_as_before(client):
    """P3. No firm, no region, no debriefs: the page is what it was, plus one
    honest "Region unknown" link where a guess would have gone."""
    user = _user()
    contact = Contact.all_objects.create(user=user, name="Pat")
    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.pk]))
    assert resp.status_code == 200
    body = resp.content.decode()
    strip = body.split('class="cd-facts"')[1].split("</p>")[0]
    assert "Tier " not in strip
    assert "Applications close" not in strip
    assert "Region unknown" in strip
    assert 'class="cd-chats"' not in body


@pytest.mark.django_db
def test_the_page_stays_cheap(client, django_assert_max_num_queries):
    """THE BUDGET, AND THE JUSTIFICATION BESIDE THE NUMBER.

    `audit-perf-tests.md` §1 measured this page at 9 queries. The strip adds
    exactly two and no more: one `UserFirm` row for the tier, one ordered
    read of this contact's debriefs. The next confirmed date rides on the
    `FirmDate` rows the fit score already fetches, and the region and its
    source are columns on the contact itself.

    RAISED 11 -> 12 ON 2026-09-02, and the twelfth query is named here so it
    stays the only one. `_dead_address_fact` reads this contact's
    `MailFact` rows to say WHY an address is missing, because "No email on
    file. Add one." was reading as the student's omission on people whose
    address a bounce or a departure auto-reply had cleared. It is gated on
    the blank column: a contact who has an address does not pay it, and this
    fixture's Pat has none, so the ceiling here is the worst case rather
    than the ordinary one.

    12 is the ceiling, not the measurement — a max, so a future saving does
    not fail the test, and a thirteenth query does.
    """
    user = _user()
    firm = _goldman()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1, status="target")
    contact = Contact.all_objects.create(user=user, name="Pat", firm=firm,
                                         region="us")
    _close(firm, days=21)
    for i in range(3):
        t = Touch.all_objects.create(user=user, contact=contact, kind="chat",
                                     channel="video", ts=timezone.now())
        ChatDebrief.all_objects.create(user=user, contact=contact, touch=t,
                                       advocate_answer="yes")
    client.force_login(user)
    with django_assert_max_num_queries(12):
        client.get(reverse("crm:contact_detail", args=[contact.pk]))
