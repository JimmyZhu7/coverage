""""Not for me" as a decision the whole product honours, and can undo.

`UserOpportunity.dismissed` and the feed card's "Not for me" button already
existed. What did not: an undo at the moment of the click, and agreement
between the number on screen and the number the click writes once a dismissal
has moved it.

THE PEEK PANEL THESE TESTS WERE WRITTEN AGAINST IS GONE (2026-09-02). The
bulk-save banner it belonged to was folded into the Picked for you column —
the founder's call, "merge the two into the pick for you widget and take away
the blue banner on the top" — so the list a bulk save reads from is now the
column itself, every row of which already carries the card's own "Not for me".
The invariants below did not move with it; only the surface they are asked of
did, and each rewritten test says so.

Pinned here:

  * THE COUNT INVARIANT. Dismissing a role moves three things at once: the
    column's "Save all" count, the confirm sentence's count, and the id set
    stashed in the session for `track_eligible` to write. All three move
    together or the 206/209/208 bug is back from the other end — see
    `directory.views.track_eligible`.
  * REVERSIBILITY. The dismiss affordance hands back an undo in the same
    response, and undo puts the role back into every surface it left.
  * TENANCY. One student's "not for me" is invisible to another.
  * APPLIED OUTRANKS DISMISSED. A role the student turns out to have applied
    to (detected by the mail parser, or set by hand) comes back — the
    dismissal must not fight a fact.
  * The Today surfaces that were still arguing with a dismissal: the
    now-retired `_new_at_your_firms` (see the "Today" section below for
    where that invariant lives now) and the ribbon's "at your firms" count.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from analytics.models import UserOpportunity
from crm.models import UserFirm
from directory.models import Firm, Opportunity
from directory.views import PICK_SAVE_OFFER_SESSION_KEY

User = get_user_model()

HX = {"HTTP_HX_REQUEST": "true"}


def _student(class_year=2027, email="hider@example.com"):
    u = User.objects.create_user(email=email, password="x")
    u.class_year = class_year
    u.save(update_fields=["class_year"])
    return u


def _eligible_opp(n, *, class_year="2027", firm=None, title=None):
    """An open campus role whose own text names class year 2027.

    A stated class match is `W_CLASS_STATED` (30) against a `MIN_SCORE` of 25,
    so one of these is a PICK for a 2027 student on nothing but that — which
    is what puts it in the column "Save all" writes."""
    firm = firm or Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=title or f"Summer Analyst {n}",
        bucket="internship", status="open", class_year=class_year,
    )


def _offer(client):
    """Render the feed and return (body, ids stashed for the write).

    The pair is the whole point: these are the two halves that used to
    disagree, so every assertion below reads them together."""
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    return body, client.session.get(PICK_SAVE_OFFER_SESSION_KEY) or []


def _picked_column(body):
    """Just the Picked for you column, so a row's absence can be asserted
    without the same role's card in its own firm column answering for it."""
    start = body.index('id="cov-pickcol"')
    return body[start:body.index("</article>", start)]


def _dismiss(client, opp, *, origin, extra=None):
    return client.post(
        reverse("track_opportunity", args=[opp.id]),
        {"status": "dismiss", "from": origin, **(extra or {})}, **HX,
    )


def _undismiss(client, opp, *, origin):
    return client.post(
        reverse("track_opportunity", args=[opp.id]),
        {"status": "undismiss", "from": origin}, **HX,
    )


# ---------------------------------------------------------------------------
# The Picked column: saying "not for me" about a role the save would write
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_every_row_the_save_would_write_carries_a_not_for_me(client):
    """The founder's report, as a test, and it survives the surface change.

    It was written against the peek panel, which named four roles and gave the
    student one verb for all four: take them, or take none. The peek is gone
    and the column is the list now, so the guarantee is asked of the column,
    where it holds for free: every row in it is a standard role card carrying
    the card's own veto."""
    user = _student()
    for n in range(1, 4):
        _eligible_opp(n)
    client.force_login(user)

    body, offered = _offer(client)
    column = _picked_column(body)

    assert len(offered) == 3
    assert column.count('"status": "dismiss", "from": "card"') == 3


@pytest.mark.django_db
def test_dismissing_a_pick_moves_all_three_numbers_together(client):
    """THE COUNT INVARIANT. What the column's button says, what the confirm
    dialog says, and what the confirm would actually write are one fact. A
    dismissal that decremented the visible number and left the session stash
    holding the id would be the 206/209/208 bug wearing new clothes.

    Asked of the column rather than the banner (2026-09-02): the banner is
    gone, and the count it stated now lives in `.firmcol-head` as the label of
    the one button there."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 5)]
    client.force_login(user)

    body, offered = _offer(client)
    assert "Save all 4" in body
    assert "Save 4 picked roles to My Applications?" in body
    assert sorted(offered) == sorted(o.id for o in opps)

    resp = _dismiss(client, opps[0], origin="card")
    after = resp.content.decode()

    assert resp.status_code == 200
    # The button, the confirm sentence, and the stash, in one breath.
    assert "Save all 3" in after
    assert "Save 3 picked roles to My Applications?" in after
    assert sorted(client.session[PICK_SAVE_OFFER_SESSION_KEY]) == sorted(
        o.id for o in opps[1:]
    )
    # And the row itself is gone from the column that named it.
    assert f"/opportunities/{opps[0].id}/track/" not in _picked_column(after)
    assert f"/opportunities/{opps[1].id}/track/" in _picked_column(after)


@pytest.mark.django_db
def test_the_confirm_then_writes_exactly_what_the_column_last_said(client):
    """End to end: dismiss one, then take the offer. The rows created must
    equal the number the screen was showing at the moment of the click, not
    the number it showed before the dismissal."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 5)]
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opps[0], origin="card").content.decode()
    assert "Save 3 picked roles to My Applications?" in after

    client.post(reverse("track_eligible"), {"confirmed": "1"})

    saved = UserOpportunity.objects.for_user(user).filter(dismissed=False)
    assert saved.count() == 3
    assert opps[0].id not in set(saved.values_list("opportunity_id", flat=True))


@pytest.mark.django_db
def test_undo_puts_the_role_back_in_the_offer(client):
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 3)]
    client.force_login(user)

    _offer(client)
    _dismiss(client, opps[0], origin="card")
    assert "Save 1" in _offer(client)[0]

    after = _undismiss(client, opps[0], origin="card").content.decode()

    assert "Save all 2" in after
    assert sorted(client.session[PICK_SAVE_OFFER_SESSION_KEY]) == sorted(
        o.id for o in opps
    )
    # Undo is a deletion, not a second flag: nothing is left behind claiming
    # the student ever had an opinion about this role.
    assert not UserOpportunity.objects.for_user(user).filter(
        opportunity=opps[0]
    ).exists()


# ---------------------------------------------------------------------------
# The role card: the same verb, a different disruption budget
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_card_dismissal_swaps_the_card_for_an_undo_in_place(client):
    """The card used to swap to an empty body: the row vanished and there
    was nothing on screen saying what had happened. "Not for me" sits one
    pixel from Save."""
    user = _student()
    opp = _eligible_opp(1, title="Markets Summer Analyst")
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opp, origin="card").content.decode()

    assert "rolerow-dismissed" in after
    assert "Markets Summer Analyst" in after
    assert '"status": "undismiss", "from": "card"' in after


@pytest.mark.django_db
def test_a_card_dismissal_also_corrects_the_column_out_of_band(client):
    """The card may be 600 rows below the Picked column, and that column is
    what says how many roles one click is about to save. Patching only the
    card left it promising a number the confirm could no longer honour.

    The out-of-band target moved from the blue banner (`#cov-scope`) to the
    column itself (`#cov-pickcol`) when the two were merged, 2026-09-02. The
    swap now also drops the dismissed role out of a column headed "Picked for
    you", which the banner swap never could."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 4)]
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opps[0], origin="card").content.decode()

    assert 'id="cov-pickcol"' in after
    assert 'hx-swap-oob="true"' in after
    assert "Save all 2" in after
    assert "Save 2 picked roles to My Applications?" in after
    assert sorted(client.session[PICK_SAVE_OFFER_SESSION_KEY]) == sorted(
        o.id for o in opps[1:]
    )


@pytest.mark.django_db
def test_undo_on_the_card_brings_the_real_card_back_untracked(client):
    user = _student()
    opp = _eligible_opp(1, title="Sales And Trading Analyst")
    client.force_login(user)

    _offer(client)
    _dismiss(client, opp, origin="card")
    after = _undismiss(client, opp, origin="card").content.decode()

    assert 'class="rolerow' in after
    assert "rolerow-dismissed" not in after
    # `smart_title`, which is how the CARD has always spelled a title. This
    # line used to read "Sales And Trading Analyst" and passed on the raw
    # spelling only because the response also carried the bulk-save peek,
    # which printed titles verbatim. The peek is gone (2026-09-02), so the
    # assertion now reads the card it was always about.
    assert "Sales and Trading Analyst" in after
    # Untracked, which is what it was before the click — the Save star is
    # back, not a "Saved" chip.
    assert '"status": "saved"' in after


@pytest.mark.django_db
def test_the_undo_stub_remembers_which_copy_of_the_card_it_replaced(client):
    """A role pinned in "Picked for you" prints its firm name; the same role
    in that firm's own column does not. The server cannot re-derive which
    copy was clicked, so the card tells it."""
    user = _student()
    opp = _eligible_opp(1)
    client.force_login(user)
    _offer(client)

    picked = _dismiss(client, opp, origin="card",
                      extra={"show_firm": "1"}).content.decode()
    _undismiss(client, opp, origin="card")
    in_column = _dismiss(client, opp, origin="card",
                         extra={"show_firm": "0"}).content.decode()

    assert "Firm 1" in picked
    assert "Firm 1" not in in_column


# ---------------------------------------------------------------------------
# Everywhere else it has to be quiet
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_dismissed_role_leaves_the_feed_and_is_counted_out_loud(client):
    user = _student()
    opp = _eligible_opp(1, title="Wealth Management Intern")
    client.force_login(user)

    _offer(client)
    _dismiss(client, opp, origin="card")
    body = client.get(reverse("opportunities")).content.decode()

    assert "Wealth Management Intern" not in body
    # Never silently dropped — but the sentence that used to say so above the
    # board is gone (2026-08-27, "take this thing away"). The count and the
    # way back live on My Applications' own durable "Not for me" section now,
    # which is where this guarantee is asked.
    assert 'you marked "not for me"' not in body

    applications_body = client.get(reverse("my_applications")).content.decode()
    assert "Wealth Management Intern" in applications_body
    assert "Not for me" in applications_body
    assert 'scrub-count">1<' in applications_body


@pytest.mark.django_db
def test_a_dismissed_role_is_listed_and_restorable_on_my_applications(client):
    """The durable half. An immediate undo covers the mis-click; this covers
    the change of mind three weeks later."""
    user = _student()
    opp = _eligible_opp(1, title="Research Summer Analyst")
    client.force_login(user)
    _offer(client)
    _dismiss(client, opp, origin="card")

    body = client.get(reverse("my_applications")).content.decode()
    assert 'id="hidden"' in body
    assert "Research Summer Analyst" in body

    client.post(reverse("track_opportunity", args=[opp.id]),
                {"status": "undismiss", "next": reverse("my_applications")})

    assert "Research Summer Analyst" in (
        client.get(reverse("opportunities")).content.decode()
    )


@pytest.mark.django_db
def test_one_students_dismissal_is_invisible_to_another(client):
    """Tenancy, on the one model that now carries a preference rather than a
    fact."""
    alice = _student(email="alice@example.com")
    bob = _student(email="bob@example.com")
    opp = _eligible_opp(1, title="Private Equity Analyst")

    client.force_login(alice)
    _offer(client)
    _dismiss(client, opp, origin="card")
    assert "Private Equity Analyst" not in (
        client.get(reverse("opportunities")).content.decode()
    )

    client.force_login(bob)
    body, offered = _offer(client)
    assert "Private Equity Analyst" in body
    assert offered == [opp.id]
    assert UserOpportunity.objects.for_user(bob).count() == 0


@pytest.mark.django_db
def test_a_dismissal_cannot_be_undone_by_a_different_student(client):
    alice = _student(email="alice2@example.com")
    mallory = _student(email="mallory@example.com")
    opp = _eligible_opp(1)

    client.force_login(alice)
    _offer(client)
    _dismiss(client, opp, origin="card")

    client.force_login(mallory)
    _undismiss(client, opp, origin="card")

    assert UserOpportunity.all_objects.get(
        user=alice, opportunity=opp
    ).dismissed is True


@pytest.mark.django_db
def test_the_refresh_respects_the_filters_that_were_on_screen(client):
    """The dismiss controls send the live filter bar with the POST. Without
    it the board that comes back would be the unfiltered default, and its
    counts would describe roles the student's own filters had excluded."""
    user = _student()
    keep = _eligible_opp(1, firm=Firm.objects.create(name="Alpha", slug="alpha"))
    other = _eligible_opp(2, firm=Firm.objects.create(name="Beta", slug="beta"))
    client.force_login(user)

    client.get(reverse("opportunities"), {"firm": "alpha"})
    resp = client.post(
        reverse("track_opportunity", args=[keep.id]),
        {"status": "dismiss", "from": "card", "firm": "alpha"}, **HX,
    )

    # `other` belongs to a firm the filter excluded, so the offer the refresh
    # stashes is empty — not "everything except the one just dismissed".
    assert client.session[PICK_SAVE_OFFER_SESSION_KEY] == []
    assert other.title not in resp.content.decode()


# ---------------------------------------------------------------------------
# Applying to it anyway
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_applying_to_a_dismissed_role_un_hides_it(client):
    """THE CHOSEN INTERACTION. "Not for me" is a preference; an application
    is a fact, and a fact outranks a preference. So the mail parser's accept
    path clears the flag rather than refusing the move or writing a hidden
    Applied row nobody can see. The alternative — an application sitting
    invisibly in a pipeline the student cannot open — is strictly worse than
    a preference being overridden by the student's own later action.
    """
    from capture.appmail import accept
    from capture.models import ApplicationEvent

    user = _student()
    opp = _eligible_opp(1, title="Global Markets Intern")
    client.force_login(user)
    _offer(client)
    _dismiss(client, opp, origin="card")

    event = ApplicationEvent.all_objects.create(
        user=user, opportunity=opp, firm=opp.firm,
        target_status="submitted", status=ApplicationEvent.STATUS_PENDING,
    )
    accept(event)

    row = UserOpportunity.objects.for_user(user).get(opportunity=opp)
    assert row.dismissed is False
    assert row.applied_status == "submitted"
    # And it is back where a tracked role belongs, not in the hidden list.
    body = client.get(reverse("my_applications")).content.decode()
    assert "Global Markets Intern" in body


@pytest.mark.django_db
def test_saving_a_dismissed_role_by_hand_also_clears_the_flag(client):
    """Same rule through the other door: the student who finds it again on
    the firm's own page and stars it has changed their mind, out loud."""
    user = _student()
    opp = _eligible_opp(1)
    client.force_login(user)
    _offer(client)
    _dismiss(client, opp, origin="card")

    client.post(reverse("track_opportunity", args=[opp.id]), {"status": "saved"})

    assert UserOpportunity.objects.for_user(user).get(
        opportunity=opp
    ).dismissed is False


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------
# `crm.today._new_at_your_firms` — "New at your firms" on the Today cockpit
# — was retired whole 2026-08-31: it duplicated the situation strip
# (`assistant.situation.build_situation`) without that strip's track/
# region/level/eligibility filtering, and measured on the founder's real
# account the two surfaced the identical firms. Its "don't call a dismissed
# role news" test lived here rather than in assistant/tests/test_situation.py
# because this file owns the not-interested feature end to end; that
# invariant has a live successor there —
# assistant/tests/test_situation.py::test_a_dismissed_role_is_not_reported_as_new
# — which pins the identical behaviour for the surface that replaced it, so
# nothing about "a dismissed role must not come back as news" went untested.


@pytest.mark.django_db
def test_todays_at_your_firms_count_drops_when_a_role_is_dismissed(client):
    """A personal number that links to a board which hides those rows. It
    promised more than the page it sends you to could show."""
    from crm.today import _dashboard_context

    user = _student()
    firm = Firm.objects.create(name="South Bank", slug="south-bank")
    a = _eligible_opp(11, firm=firm)
    _eligible_opp(12, firm=firm)
    UserFirm.all_objects.create(user=user, firm=firm)

    assert _dashboard_context(user)["dash"]["at_your_firms"] == 2

    UserOpportunity.all_objects.create(user=user, opportunity=a, dismissed=True)

    assert _dashboard_context(user)["dash"]["at_your_firms"] == 1
