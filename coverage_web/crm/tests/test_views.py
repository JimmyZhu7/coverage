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

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from analytics.models import ProductEvent
from crm.models import Contact, Touch

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
