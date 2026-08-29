""""Not for me" as a decision the whole product honours, and can undo.

`UserOpportunity.dismissed` and the feed card's "Not for me" button already
existed. What did not: a way to say it from inside the bulk-save PEEK — the
one panel that names the roles a single click is about to save — an undo at
the moment of the click, and agreement between the number on screen and the
number the click writes once a dismissal has moved it.

Pinned here:

  * THE COUNT INVARIANT. Dismissing a role moves four things at once: the
    banner's count, the confirm sentence's count, the peek list, and the id
    set stashed in the session for `track_eligible` to write. All four move
    together or the 206/209/208 bug is back from the other end — see
    `directory.views._eligible_unsaved_ids`.
  * REVERSIBILITY. Both dismiss affordances hand back an undo in the same
    response, and undo puts the role back into every surface it left.
  * TENANCY. One student's "not for me" is invisible to another.
  * APPLIED OUTRANKS DISMISSED. A role the student turns out to have applied
    to (detected by the mail parser, or set by hand) comes back — the
    dismissal must not fight a fact.
  * The Today surfaces that were still arguing with a dismissal:
    `_new_at_your_firms` and the ribbon's "at your firms" count.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from analytics.models import UserOpportunity
from crm.models import UserFirm
from directory.models import Firm, Opportunity
from directory.views import BULK_SAVE_OFFER_SESSION_KEY

User = get_user_model()

HX = {"HTTP_HX_REQUEST": "true"}


def _student(class_year=2027, email="hider@example.com"):
    u = User.objects.create_user(email=email, password="x")
    u.class_year = class_year
    u.save(update_fields=["class_year"])
    return u


def _eligible_opp(n, *, class_year="2027", firm=None, title=None):
    """An open campus role whose own text names class year 2027 — i.e. one
    the bulk-save banner will offer to a 2027 student."""
    firm = firm or Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=title or f"Summer Analyst {n}",
        bucket="internship", status="open", class_year=class_year,
    )


def _offer(client):
    """Render the feed and return (count on screen, ids stashed for the write).

    The pair is the whole point: these are the two halves that used to
    disagree, so every assertion below reads them together."""
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    return body, client.session.get(BULK_SAVE_OFFER_SESSION_KEY) or []


def _peek_list(body):
    """Just the peek panel's <ul>, so a row's absence can be asserted without
    the undo strip above it (which names the role on purpose) answering for
    it."""
    start = body.index('class="peek-list"')
    return body[start:body.index("</ul>", start)]


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
# The peek panel: saying "not for me" about a named row
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_peek_offers_a_not_for_me_on_every_row_it_names(client):
    """The founder's report, as a test. The panel named four roles and gave
    the student one verb for all four: take them, or take none."""
    user = _student()
    for n in range(1, 4):
        _eligible_opp(n)
    client.force_login(user)

    body, _ = _offer(client)

    assert body.count('"status": "dismiss", "from": "peek"') == 3


@pytest.mark.django_db
def test_dismissing_from_the_peek_moves_all_three_numbers_together(client):
    """THE COUNT INVARIANT. What the banner says, what the confirm dialog
    says, and what the confirm would actually write are one fact. A
    dismissal that decremented the visible number and left the session stash
    holding the id would be the 206/209/208 bug wearing new clothes."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 5)]
    client.force_login(user)

    body, offered = _offer(client)
    assert "4 open roles name your class year" in body
    assert "Save 4 roles to My Applications?" in body
    assert sorted(offered) == sorted(o.id for o in opps)

    resp = _dismiss(client, opps[0], origin="peek")
    after = resp.content.decode()

    assert resp.status_code == 200
    # The banner, the confirm sentence, and the stash, in one breath.
    assert "3 open roles name your class year" in after
    assert "Save 3 roles to My Applications?" in after
    assert sorted(client.session[BULK_SAVE_OFFER_SESSION_KEY]) == sorted(
        o.id for o in opps[1:]
    )
    # And the row itself is gone from the list that named it. Checked by its
    # own veto button rather than by title: the undo strip above the panel
    # names the role too, and rightly so.
    assert f'/opportunities/{opps[0].id}/track/' not in _peek_list(after)
    assert f'/opportunities/{opps[1].id}/track/' in _peek_list(after)


@pytest.mark.django_db
def test_the_confirm_then_writes_exactly_what_the_banner_last_said(client):
    """End to end: dismiss one, then take the offer. The rows created must
    equal the number the screen was showing at the moment of the click —
    not the number it showed before the dismissal."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 5)]
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opps[0], origin="peek").content.decode()
    assert "Save 3 roles to My Applications?" in after

    client.post(reverse("track_eligible"), {"confirmed": "1"})

    saved = UserOpportunity.objects.for_user(user).filter(dismissed=False)
    assert saved.count() == 3
    assert opps[0].id not in set(saved.values_list("opportunity_id", flat=True))


@pytest.mark.django_db
def test_the_peek_dismissal_comes_back_with_an_undo_that_names_the_role(client):
    user = _student()
    opp = _eligible_opp(1, title="Quant Trading Intern")
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opp, origin="peek").content.decode()

    assert "Not interested in" in after
    assert "Quant Trading Intern" in after
    assert '"status": "undismiss", "from": "peek"' in after
    # And it points at the durable list, not just the button.
    assert reverse("my_applications") in after


@pytest.mark.django_db
def test_undo_from_the_peek_puts_the_role_back_in_the_offer(client):
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 3)]
    client.force_login(user)

    _offer(client)
    _dismiss(client, opps[0], origin="peek")
    assert "1 open role names your class year" in _offer(client)[0]

    after = _undismiss(client, opps[0], origin="peek").content.decode()

    assert "2 open roles name your class year" in after
    assert sorted(client.session[BULK_SAVE_OFFER_SESSION_KEY]) == sorted(
        o.id for o in opps
    )
    # Undo is a deletion, not a second flag: nothing is left behind claiming
    # the student ever had an opinion about this role.
    assert not UserOpportunity.objects.for_user(user).filter(
        opportunity=opps[0]
    ).exists()


@pytest.mark.django_db
def test_the_peek_stays_open_across_the_swap(client):
    """A student pruning eight rows should not reopen the panel between
    each one — and on a keyboard the button they were focused on has just
    been replaced, so `:focus-within` cannot hold it open on its own."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 4)]
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opps[0], origin="peek").content.decode()

    assert 'data-peek-state="open"' in after
    assert 'aria-expanded="true"' in after


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

    assert "rolecard-dismissed" in after
    assert "Markets Summer Analyst" in after
    assert '"status": "undismiss", "from": "card"' in after


@pytest.mark.django_db
def test_a_card_dismissal_also_corrects_the_banner_out_of_band(client):
    """The card is 600 rows below the banner, and the banner is what says
    how many roles one click is about to save. Patching only the card left
    it promising a number the confirm could no longer honour."""
    user = _student()
    opps = [_eligible_opp(n) for n in range(1, 4)]
    client.force_login(user)

    _offer(client)
    after = _dismiss(client, opps[0], origin="card").content.decode()

    assert 'id="cov-scope"' in after
    assert 'hx-swap-oob="true"' in after
    assert "2 open roles name your class year" in after
    assert "Save 2 roles to My Applications?" in after
    assert sorted(client.session[BULK_SAVE_OFFER_SESSION_KEY]) == sorted(
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

    assert 'class="rolecard' in after
    assert "rolecard-dismissed" not in after
    assert "Sales And Trading Analyst" in after
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
        {"status": "dismiss", "from": "peek", "firm": "alpha"}, **HX,
    )

    # `other` belongs to a firm the filter excluded, so the offer the refresh
    # stashes is empty — not "everything except the one just dismissed".
    assert client.session[BULK_SAVE_OFFER_SESSION_KEY] == []
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

@pytest.mark.django_db
def test_today_stops_calling_a_dismissed_role_news(client):
    """`_new_at_your_firms` was the last surface still arguing: a role
    dismissed in the feed on Monday came back on Tuesday as news from the
    firm."""
    from crm.today import _new_at_your_firms

    user = _student()
    user.tracks = ["ib"]
    user.regions = []
    user.save(update_fields=["tracks", "regions"])
    firm = Firm.objects.create(name="North Bank", slug="north-bank")
    # An older row so the firm is not read as a board DEBUT.
    Opportunity.objects.create(
        firm=firm, url="https://x/old", title="Old Role", bucket="internship",
        status="closed",
    )
    Opportunity.objects.filter(url="https://x/old").update(
        first_seen="2020-01-01T00:00:00Z"
    )
    opp = _eligible_opp(9, firm=firm,
                        title="2027 Investment Banking Summer Analyst")
    UserFirm.all_objects.create(user=user, firm=firm)

    assert [r["id"] for r in _new_at_your_firms(user)["roles"]] == [opp.id]

    UserOpportunity.all_objects.create(user=user, opportunity=opp, dismissed=True)

    assert _new_at_your_firms(user)["roles"] == []


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
