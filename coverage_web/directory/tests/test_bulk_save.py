"""The bulk-save banner ("N open roles name your class year... Save them
all") and the three guardrails a customer-perspective walk found missing:
one click used to dump 200+ roles into My Applications with no confirm, no
undo, and no way to remove them except one Remove click per row.

Pinned here:

  * `track_eligible` refuses to write without `confirmed=1` — the template's
    `<details>` confirm is a UI affordance, not the actual gate.
  * `track_eligible_undo` reverses exactly the ids the triggering bulk save
    created, never a role the student saved by hand before or since, and
    never a role that has since moved past Saved.
  * `clear_saved` is tenant-scoped (`.for_user`) and Saved-only — Applied/
    Interviewing/Offer/Done rows survive it no matter what.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from analytics.models import UserOpportunity
from directory.models import Firm, Opportunity

User = get_user_model()


def _eligible_opp(n, class_year="2027", firm=None):
    firm = firm or Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=f"Summer Analyst {n}",
        bucket="internship", status="open", class_year=class_year,
    )


def _student(class_year=2027, email="student@example.com"):
    u = User.objects.create_user(email=email, password="x")
    u.class_year = class_year
    u.save(update_fields=["class_year"])
    return u


def _confirm_bulk_save(client, *, follow=False):
    """Load the feed, then confirm — the sequence a real click makes.

    The GET is not ceremony. `track_eligible` writes the exact ids the banner
    OFFERED (stashed under `BULK_SAVE_OFFER_SESSION_KEY` when the feed
    rendered) rather than re-deriving a set of its own, because those two
    used to be different questions with different answers: the confirm said
    206, the write made 209. No render, no offer, nothing to honour."""
    client.get(reverse("opportunities"))
    return client.post(reverse("track_eligible"), {"confirmed": "1"}, follow=follow)


# ---------------------------------------------------------------------------
# Confirm gate
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_plain_post_without_confirmation_does_not_write(client):
    """The historical bug, reproduced and pinned: a POST that never carries
    `confirmed=1` — exactly what a bare click on a button with no confirm
    step used to send — must not create a single row."""
    user = _student()
    _eligible_opp(1)
    _eligible_opp(2)
    client.force_login(user)

    resp = client.post(reverse("track_eligible"), {})

    assert resp.status_code == 400
    assert UserOpportunity.objects.for_user(user).count() == 0


@pytest.mark.django_db
def test_confirmed_post_saves_every_eligible_role(client):
    user = _student()
    _eligible_opp(1)
    _eligible_opp(2)
    client.force_login(user)

    resp = _confirm_bulk_save(client, follow=True)

    assert resp.status_code == 200
    assert UserOpportunity.objects.for_user(user).count() == 2


@pytest.mark.django_db
def test_confirmed_save_shows_an_undo_banner_naming_the_count(client):
    user = _student()
    _eligible_opp(1)
    _eligible_opp(2)
    _eligible_opp(3)
    client.force_login(user)

    resp = _confirm_bulk_save(client, follow=True)

    body = resp.content.decode()
    assert "Saved 3 roles that name your year." in body
    assert "Undo" in body


# ---------------------------------------------------------------------------
# The number in the confirm is the number that happens
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_confirm_saves_exactly_what_the_banner_counted(client):
    """The drift, reproduced: one page load, three numbers.

    The banner counted the FEED's materialised rows — folded for duplicates
    by `directory.dupes.fold_duplicates` — while `track_eligible` re-derived
    its own set from the whole open table, unfolded. On the live board that
    was 206 confirmed and 209 written; My Applications then folded them again
    and its tile read 208.

    Here: one requisition listed twice by the same firm, same title, same
    location, different URLs — the shape a board scraped twice in one week
    produces. The feed shows one row. The confirm must write one row."""
    user = _student()
    firm = Firm.objects.create(name="Repeat Bank", slug="repeat-bank")
    for n in (1, 2):
        Opportunity.objects.create(
            firm=firm, url=f"https://repeat/{n}", title="Summer Analyst",
            bucket="internship", status="open", class_year="2027",
            location="New York")
    client.force_login(user)

    body = client.get(reverse("opportunities")).content.decode()
    assert "1 open role names your class year" in body

    resp = _confirm_bulk_save(client, follow=True)
    assert "Saved 1 role that names your year." in resp.content.decode()
    assert UserOpportunity.objects.for_user(user).count() == 1


@pytest.mark.django_db
def test_a_confirm_with_no_offer_behind_it_writes_nothing(client):
    """A POST that never rendered the banner has no number to honour. Same
    posture as the `confirmed=1` gate: refuse, rather than fall back to a set
    the student was never shown — which is precisely how the write came to be
    three rows wider than the confirm."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    resp = client.post(reverse("track_eligible"), {"confirmed": "1"})

    assert resp.status_code == 400
    assert UserOpportunity.objects.for_user(user).count() == 0


@pytest.mark.django_db
def test_a_role_that_appeared_after_the_banner_rendered_is_not_swept_in(client):
    """The other half of "exactly what was shown": saving MORE than the
    confirm named is the product doing something the student never agreed
    to. A role that lands between the render and the click waits for the
    next banner."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    client.get(reverse("opportunities"))
    latecomer = _eligible_opp(2)
    client.post(reverse("track_eligible"), {"confirmed": "1"})

    saved = set(UserOpportunity.objects.for_user(user)
                .values_list("opportunity_id", flat=True))
    assert latecomer.id not in saved
    assert len(saved) == 1


@pytest.mark.django_db
def test_a_double_submit_does_not_re_run_the_batch(client):
    """The offer is consumed on use, so a back-then-resubmit (or a
    double-click) finds nothing left to honour rather than re-running against
    a batch the student has already acted on."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    _confirm_bulk_save(client)
    resp = client.post(reverse("track_eligible"), {"confirmed": "1"})

    assert resp.status_code == 400
    assert UserOpportunity.objects.for_user(user).count() == 1


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_undo_removes_only_the_batchs_rows_and_leaves_hand_saved_ones(client):
    user = _student()
    hand_saved = _eligible_opp(1)  # not eligible-flow, just an ordinary save
    bulk_a = _eligible_opp(2)
    bulk_b = _eligible_opp(3)
    client.force_login(user)

    # A curated, hand-saved role from before the bulk action.
    client.post(reverse("track_opportunity", args=[hand_saved.id]), {"status": "saved"})

    _confirm_bulk_save(client)
    assert UserOpportunity.objects.for_user(user).count() == 3

    resp = client.post(reverse("track_eligible_undo"), {}, follow=True)

    assert resp.status_code == 200
    remaining = UserOpportunity.objects.for_user(user)
    assert remaining.count() == 1
    assert remaining.first().opportunity_id == hand_saved.id
    assert "Removed 2 roles" in resp.content.decode()


@pytest.mark.django_db
def test_undo_never_removes_a_row_the_student_has_since_advanced(client):
    """A bulk-saved role the student already marked Applied is real progress.
    Undo must not eat it just because it was in the batch."""
    user = _student()
    o1 = _eligible_opp(1)
    o2 = _eligible_opp(2)
    client.force_login(user)

    _confirm_bulk_save(client)
    # The student acts fast on one of the two before hitting Undo.
    client.post(reverse("track_opportunity", args=[o1.id]), {"status": "submitted"})

    resp = client.post(reverse("track_eligible_undo"), {}, follow=True)

    assert resp.status_code == 200
    remaining = UserOpportunity.objects.for_user(user)
    assert remaining.count() == 1
    kept = remaining.first()
    assert kept.opportunity_id == o1.id
    assert kept.applied_status == "submitted"


@pytest.mark.django_db
def test_undo_with_nothing_to_undo_is_a_bad_request(client):
    user = _student()
    client.force_login(user)
    resp = client.post(reverse("track_eligible_undo"), {})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_a_second_bulk_save_only_offers_undo_for_the_newest_batch(client):
    user = _student()
    o1 = _eligible_opp(1)
    client.force_login(user)
    _confirm_bulk_save(client)
    # The first batch's undo banner is consumed by visiting My Applications.
    client.get(reverse("my_applications"))

    o2 = _eligible_opp(2)
    _confirm_bulk_save(client)
    resp = client.post(reverse("track_eligible_undo"), {}, follow=True)

    remaining = UserOpportunity.objects.for_user(user)
    assert remaining.count() == 1
    assert remaining.first().opportunity_id == o1.id


# ---------------------------------------------------------------------------
# Clear saved
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_clear_saved_requires_confirmation(client):
    user = _student()
    o = _eligible_opp(1)
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[o.id]), {"status": "saved"})

    resp = client.post(reverse("clear_saved"), {})

    assert resp.status_code == 400
    assert UserOpportunity.objects.for_user(user).count() == 1


@pytest.mark.django_db
def test_clear_saved_empties_only_the_saved_stage(client):
    user = _student()
    saved1, saved2 = _eligible_opp(1), _eligible_opp(2)
    applied = _eligible_opp(3)
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[saved1.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[saved2.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[applied.id]), {"status": "submitted"})

    resp = client.post(reverse("clear_saved"), {"confirmed": "1"}, follow=True)

    assert resp.status_code == 200
    remaining = UserOpportunity.objects.for_user(user)
    assert remaining.count() == 1
    assert remaining.first().opportunity_id == applied.id
    assert remaining.first().applied_status == "submitted"
    assert "Cleared 2 saved roles." in resp.content.decode()


@pytest.mark.django_db
def test_clear_saved_only_touches_the_signed_in_users_rows(client):
    """Tenant scope: another user's Saved rows must survive someone else's
    Clear saved click. `.for_user` is what enforces this — asserted here
    end to end rather than trusting the call site."""
    a, b = _student(email="a@example.com"), _student(email="b@example.com")
    oa = _eligible_opp(1)
    ob = _eligible_opp(2)

    client.force_login(a)
    client.post(reverse("track_opportunity", args=[oa.id]), {"status": "saved"})
    client.force_login(b)
    client.post(reverse("track_opportunity", args=[ob.id]), {"status": "saved"})

    client.force_login(a)
    client.post(reverse("clear_saved"), {"confirmed": "1"})

    assert UserOpportunity.objects.for_user(a).count() == 0
    assert UserOpportunity.objects.for_user(b).count() == 1
    assert UserOpportunity.objects.for_user(b).first().opportunity_id == ob.id


@pytest.mark.django_db
def test_clear_saved_leaves_dismissed_rows_alone(client):
    """Dismissed ("not for me") rows are a different judgement entirely and
    live in their own section — Clear saved must not touch them."""
    user = _student()
    dismissed = _eligible_opp(1)
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[dismissed.id]), {"status": "dismiss"})

    client.post(reverse("clear_saved"), {"confirmed": "1"})

    row = UserOpportunity.all_objects.get(user=user, opportunity=dismissed)
    assert row.dismissed is True
