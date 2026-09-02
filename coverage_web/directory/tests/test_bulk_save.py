"""The bulk-save banner ("N roles fit you and name your year... Save them
all") and the four guardrails a customer-perspective walk found missing:
one click used to dump 200+ roles into My Applications with no confirm, no
undo, no way to remove them except one Remove click per row, and — the last
one found, on 2026-09-02 — no test of FIT at all behind the number.

Pinned here:

  * The offer is the intersection of eligibility and fit: a role has to name
    the student's class year AND be one the recommender would rank (see
    `_offer_fits`). Naming your year is what you may apply for; a bulk save
    is what you intend.
  * "Save them all" is offered only while the peek has shown every role in
    the offer — `BULK_SAVE_PEEK_MAX`, the panel's own cap.
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
    assert "1 role fits you and names your year" in body

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


@pytest.mark.django_db
def test_a_concurrent_duplicate_save_does_not_500_the_confirm(client, monkeypatch):
    """A benign race, not a hypothetical one: `touched` (the ids already
    tracked) is read ONCE, before the write loop below it starts. A second
    tab confirming the same offer, or a double-click that fires the POST
    twice, can create the very same (user, opportunity) row in the gap
    between that read and this call's own write. `UserOpportunity` enforces
    uniqueness on exactly that pair, so a plain `.create()` there raises
    IntegrityError and 500s the whole confirm — including every OTHER role
    in the same batch that this request had already saved fine.
    `track_opportunity`'s own upsert already guards against this identical
    race with `get_or_create`; this confirm path must too."""
    from analytics.models import UserOpportunity as UO

    user = _student()
    opp = _eligible_opp(1)
    client.force_login(user)
    client.get(reverse("opportunities"))  # stashes the session offer

    # Simulates the other tab's write landing in the gap: the row exists in
    # the database by the time this request's loop reaches it, but `touched`
    # (read at the top of the view, before this happened) never saw it.
    UO.all_objects.create(user=user, opportunity=opp)
    monkeypatch.setattr(UO.all_objects, "filter", lambda **kw: UO.all_objects.none())

    resp = client.post(reverse("track_eligible"), {"confirmed": "1"})

    assert resp.status_code != 500
    # No duplicate row, and no unhandled IntegrityError bubbling out as one.
    assert UO.objects.for_user(user).filter(opportunity=opp).count() == 1


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


# ---------------------------------------------------------------------------
# FIT, not just eligibility
# ---------------------------------------------------------------------------
#
# THE MEASURED DEFECT (founder's live board, 2026-09-02; class 2029, tracks
# ib+st, regions hk+us). The banner offered to write 56 roles in one click on
# the strength of one test — "the posting names your class year" — which is a
# fact about who may APPLY. Of those 56: 16 sat on one of his tracks and 18
# named a function that was not one of them (16 outside the track vocabulary
# entirely, 2 on a track he does not recruit for), 14 were in Hong Kong or the
# US against 32 in a market he never named and 10 the product could not place
# at all. The board's own recommender ranked none of the 40. The offer is
# scored now: it is the intersection of the year verdict and `_offer_fits`.


def _fit_student(email="fit@example.com"):
    """A student who has said what they want: IB in the US, class of 2027.

    The other `_student()` in this file states only a class year, which is
    the thin profile P3 protects — every rule here has to degrade to the old
    behaviour for them, and the tests above are what hold that."""
    u = _student(email=email)
    u.tracks = ["ib"]
    u.regions = ["us"]
    u.save(update_fields=["tracks", "regions"])
    return u


def _titled_opp(n, title, *, region="us", class_year="2027"):
    firm = Firm.objects.create(name=f"Firm {n}", slug=f"firm-{n}")
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", title=title, bucket="internship",
        status="open", class_year=class_year, region=region,
    )


@pytest.mark.django_db
def test_a_year_match_on_another_function_is_not_offered(client):
    """Nomura's "2027 Operations Summer Analyst Program" states his year and
    scored 70 on the founder's board. It is still not a role he is recruiting
    for, and one click must not save it for him.

    The role's own title is what decides it (`role_function_cached`, the
    scorer's single definition), so the firm covering IB cannot lend its
    coverage to an Operations req — the same rule `_track_fit` ranks with."""
    user = _fit_student()
    wanted = _titled_opp(1, "Investment Banking Summer Analyst")
    ops = _titled_opp(2, "Operations Summer Analyst Program")
    client.force_login(user)

    resp = client.get(reverse("opportunities"))

    assert set(client.session["bulk_save_offer"]) == {wanted.id}
    assert ops.id not in client.session["bulk_save_offer"]
    assert "1 role fits you and names your year" in resp.content.decode()


@pytest.mark.django_db
def test_a_year_match_in_a_market_he_never_named_is_not_offered(client):
    """15 of the 56 were in the EU, 6 in China, 10 in a market Coverage does
    not track. A stated wrong region is a stated non-fit — `role_matches_
    regions`, the same function the feed's own region filter uses — and a
    blank region fails it too, for the reason written there: this must not be
    the one surface where "you said US" quietly includes rows nobody can
    place."""
    user = _fit_student()
    here = _titled_opp(1, "Investment Banking Summer Analyst", region="us")
    abroad = _titled_opp(2, "Investment Banking Summer Analyst", region="eu")
    nowhere = _titled_opp(3, "Investment Banking Summer Analyst", region="")
    client.force_login(user)

    client.get(reverse("opportunities"))

    offer = set(client.session["bulk_save_offer"])
    assert offer == {here.id}
    for gone in (abroad, nowhere):
        assert gone.id not in offer


@pytest.mark.django_db
def test_a_global_role_is_offered_because_the_scorer_does_not_call_it_wrong(client):
    """"Global" is the posting saying it has no single location, not the
    posting naming somewhere else. `_region_fit` scores it zero rather than
    penalising it (it is outside `_STATED_MARKETS`), so the offer treats it
    the same way — the gate must not be stricter than the ranker it claims
    to speak for."""
    user = _fit_student()
    everywhere = _titled_opp(1, "Investment Banking Summer Analyst",
                             region="global")
    client.force_login(user)

    client.get(reverse("opportunities"))

    assert set(client.session["bulk_save_offer"]) == {everywhere.id}


@pytest.mark.django_db
def test_a_role_under_the_recommenders_bar_does_not_fit(client):
    """The third test is the scorer's own floor, asked of `_offer_fits`
    directly.

    Directly, because on the offer's own path this floor cannot currently be
    the deciding test and a feed-level fixture would be pinning a coincidence.
    A role only reaches the gate by carrying a `year_ok` verdict, which means
    the posting STATED this student's class — worth `W_CLASS_STATED` (30) on
    its own, already past `MIN_SCORE` (25) before any other axis speaks.
    Measured on the founder's board 2026-09-02: track took 56 rows to 38,
    region took 38 to 8, and the bar took 8 to 8, the lowest survivor scoring
    56.

    That is a fact about today's weights, not a reason to leave the bar out.
    It is the same number the ranker applies before ordering anything, so
    while it is here the offer cannot drift below the column it sits above
    — whatever the weights become. Pinned on the function that owns it."""
    from directory.recommend import MIN_SCORE, Profile
    from directory.views import _offer_fits

    user = _fit_student()
    firm = Firm.objects.create(name="Unlisted Bank", slug="unlisted-bank")
    # A silent title, so the track test passes without scoring anything (the
    # firm covers nothing either), and no stated class year, so `_class_fit`
    # is silent too.
    o = Opportunity.objects.create(
        firm=firm, url="https://x/thin", title="Summer Programme",
        bucket="internship", status="open", region="us")

    # Track and region both pass; the region is his, and nothing else about
    # the row scores. `W_REGION_TARGET` (16) alone is under the bar.
    thin = Profile.from_user(user)
    assert _offer_fits(thin, o) is False

    # The same row for a student who targets the firm: tier 1 (26) plus the
    # region (16) clears it, and the offer follows the scorer up as well as
    # down.
    targeted = Profile.from_user(user, {firm.id: 1})
    assert _offer_fits(targeted, o) is True
    assert MIN_SCORE == 25, "the bar moved; re-read this test's arithmetic"


@pytest.mark.django_db
def test_a_student_who_stated_no_tracks_or_regions_is_filtered_on_neither(client):
    """P3, degrade to today's behaviour on thin data. A profile holding only
    a class year has named no desk for a title to be outside of and no market
    for a location to be wrong about, so the two axes filter nothing and the
    offer is what it always was."""
    user = _student()
    ops = _eligible_opp(1)
    abroad = _eligible_opp(2)
    Opportunity.objects.filter(pk=abroad.pk).update(region="eu")
    client.force_login(user)

    client.get(reverse("opportunities"))

    assert set(client.session["bulk_save_offer"]) == {ops.id, abroad.id}


# ---------------------------------------------------------------------------
# One click commits only to what the peek has shown
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_bulk_button_disappears_above_what_the_peek_can_show(client):
    """A commitment to more roles than the panel prints is a commitment to
    roles the student has not looked at. One over the cap and the button is
    gone; the sentence and the peek stay, because every one of those roles is
    still named and still savable one at a time."""
    from directory.views import BULK_SAVE_PEEK_MAX

    user = _student()
    for n in range(BULK_SAVE_PEEK_MAX + 1):
        _eligible_opp(n)
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()

    assert resp.context["eligible_unsaved"] == BULK_SAVE_PEEK_MAX + 1
    assert resp.context["bulk_save_all"] is False
    # The confirm's own markup, not the word "save": `.scope-act` is styled in
    # the page's <style> block on every render, button or no button.
    assert 'class="scope-confirm"' not in body
    assert 'name="confirmed"' not in body
    # The review path is untouched.
    assert "Which ones?" in body
    assert "+ 1 more not shown" in body


@pytest.mark.django_db
def test_the_bulk_button_is_there_at_the_cap(client):
    """Exactly at the threshold the panel names the whole offer, so the
    button is honest and renders. This is the founder's own board: his offer
    measured 8 on 2026-09-02, and 8 is the cap."""
    from directory.views import BULK_SAVE_PEEK_MAX

    user = _student()
    for n in range(BULK_SAVE_PEEK_MAX):
        _eligible_opp(n)
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()

    assert resp.context["eligible_unsaved"] == BULK_SAVE_PEEK_MAX
    assert resp.context["bulk_save_all"] is True
    assert 'class="scope-confirm"' in body
    assert 'name="confirmed"' in body
    assert resp.context["bulk_save_peek"]["more"] == 0


@pytest.mark.django_db
def test_the_threshold_is_the_peeks_own_cap_and_not_a_second_number(client):
    """Two constants that agree today would not agree after the first edit to
    either. The button renders exactly while the panel has nothing left over
    to count."""
    from directory.views import BULK_SAVE_PEEK_MAX

    user = _student()
    client.force_login(user)
    for n in range(BULK_SAVE_PEEK_MAX + 2):
        _eligible_opp(n)
        resp = client.get(reverse("opportunities"))
        assert resp.context["bulk_save_all"] is (
            resp.context["bulk_save_peek"]["more"] == 0), (
            f"offer of {resp.context['eligible_unsaved']} disagrees with the "
            f"peek's own remainder")


@pytest.mark.django_db
def test_what_the_offer_stashes_is_still_what_the_confirm_writes(client):
    """The 206/209/208 invariant, asked again now that a second filter stands
    between the rows and the offer. The banner's number, the panel's rows,
    the stashed ids and the created rows are one list resolved once — a fit
    rule applied in the view but not in the stash would put them back out of
    agreement by a new route."""
    user = _fit_student()
    offered = [_titled_opp(n, "Investment Banking Summer Analyst")
               for n in (1, 2, 3)]
    # Same year, same region, wrong desk: counted by the old banner, and it
    # must be absent from all four places now.
    ops = _titled_opp(4, "Internal Audit Summer Analyst")
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    stashed = set(client.session["bulk_save_offer"])
    peeked = {r["id"] for r in resp.context["bulk_save_peek"]["rows"]}

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))

    assert resp.context["eligible_unsaved"] == 3
    assert stashed == peeked == written == {o.id for o in offered}
    assert ops.id not in written


# ---------------------------------------------------------------------------
# The peek — which roles the banner is offering, before you agree to it
# ---------------------------------------------------------------------------
#
# The banner named a number and asked for a commitment against it without ever
# saying WHICH roles. These pin the one property that makes the panel worth
# having: it is a view of the SAME id list the confirm writes
# (`BULK_SAVE_OFFER_SESSION_KEY`), not a second answer to the same question.
# A count that disagrees is a bug; a role named in the panel and then not
# saved would be a promise broken by name.


def _peek(client):
    """The peek context off a fresh feed render, with the live offer beside
    it — the two facts every test in this section compares."""
    resp = client.get(reverse("opportunities"))
    return resp, resp.context["bulk_save_peek"], client.session["bulk_save_offer"]


@pytest.mark.django_db
def test_the_peek_lists_exactly_the_roles_in_the_offer(client):
    user = _student()
    offered = [_eligible_opp(n) for n in (1, 2, 3)]
    client.force_login(user)

    resp, peek, offer = _peek(client)

    assert set(offer) == {o.id for o in offered}
    assert {r["id"] for r in peek["rows"]} == set(offer)
    assert peek["total"] == resp.context["eligible_unsaved"] == 3
    body = resp.content.decode()
    for o in offered:
        assert o.title in body


@pytest.mark.django_db
def test_the_peek_never_names_a_role_the_offer_excludes(client):
    """Three exclusions the banner's own count already makes, held to the
    same standard now that the roles are named on screen: a role whose text
    names another class year, one the student already tracks, and one they
    dismissed. "Not for me" outranks "your year" in the panel too."""
    user = _student()
    mine = _eligible_opp(1)
    other_year = _eligible_opp(2, class_year="2031")
    already = _eligible_opp(3)
    dismissed = _eligible_opp(4)
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[already.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[dismissed.id]), {"status": "dismiss"})

    resp, peek, offer = _peek(client)

    assert [r["id"] for r in peek["rows"]] == [mine.id]
    assert set(offer) == {mine.id}
    body = resp.content.decode()
    for gone in (other_year, already, dismissed):
        assert f'class="peek-title">{gone.title}<' not in body


@pytest.mark.django_db
def test_the_peek_caps_its_rows_and_says_how_many_it_left_out(client):
    """A 200-row offer is a page, not a peek. The cap is fine; a cap that
    read as the whole offer would not be — so the remainder is stated."""
    from directory.views import BULK_SAVE_PEEK_MAX

    user = _student()
    extra = 4
    for n in range(BULK_SAVE_PEEK_MAX + extra):
        _eligible_opp(n)
    client.force_login(user)

    resp, peek, offer = _peek(client)

    assert len(peek["rows"]) == BULK_SAVE_PEEK_MAX
    assert peek["total"] == len(offer) == BULK_SAVE_PEEK_MAX + extra
    assert peek["more"] == extra
    assert f"+ {extra} more not shown" in resp.content.decode()


@pytest.mark.django_db
def test_a_peek_that_fits_says_nothing_about_more(client):
    user = _student()
    for n in (1, 2):
        _eligible_opp(n)
    client.force_login(user)

    resp, peek, _ = _peek(client)

    assert peek["more"] == 0
    assert "more not shown" not in resp.content.decode()


@pytest.mark.django_db
def test_the_peek_puts_the_soonest_deadline_first(client):
    """The cap only stays honest if the rows it keeps are the ones with the
    most to say about acting today. Dated roles lead, soonest first; undated
    ones follow, because "no date posted" is the least urgent thing a row
    can say."""
    from datetime import timedelta

    from django.utils import timezone

    user = _student()
    today = timezone.localdate()
    undated = _eligible_opp(1)
    far = _eligible_opp(2)
    near = _eligible_opp(3)
    Opportunity.objects.filter(pk=far.pk).update(deadline=today + timedelta(days=30))
    Opportunity.objects.filter(pk=near.pk).update(deadline=today + timedelta(days=2))
    client.force_login(user)

    _, peek, _ = _peek(client)

    assert [r["id"] for r in peek["rows"]] == [near.id, far.id, undated.id]


@pytest.mark.django_db
def test_the_peek_folds_a_repeat_listing_exactly_as_the_confirm_does(client):
    """The 206/209/208 defect, asked of the panel. One requisition listed
    twice is one role: the feed folds it, the confirm writes one row, and the
    panel must name it once — otherwise the student reads two promises and
    gets one."""
    user = _student()
    firm = Firm.objects.create(name="Repeat Bank", slug="repeat-bank")
    for n in (1, 2):
        Opportunity.objects.create(
            firm=firm, url=f"https://repeat/{n}", title="Summer Analyst",
            bucket="internship", status="open", class_year="2027",
            location="New York")
    client.force_login(user)

    resp, peek, offer = _peek(client)

    assert len(peek["rows"]) == peek["total"] == len(offer) == 1
    assert resp.content.decode().count('class="peek-title"') == 1

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))
    assert written == {r["id"] for r in peek["rows"]}


@pytest.mark.django_db
def test_what_the_panel_named_is_what_the_click_saves(client):
    """The whole invariant in one assertion, for an offer small enough that
    the panel names all of it: panel contents == offer set == what gets
    written."""
    user = _student()
    for n in (1, 2, 3):
        _eligible_opp(n)
    client.force_login(user)

    _, peek, offer = _peek(client)
    client.post(reverse("track_eligible"), {"confirmed": "1"})

    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))
    assert {r["id"] for r in peek["rows"]} == set(offer) == written


@pytest.mark.django_db
def test_the_peek_is_a_disclosure_a_keyboard_can_reach(client):
    """Hover is not an interface for everyone. The toggle is a real button
    wired to the panel it opens, so focus and a tap both work and a screen
    reader is told what the control does."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    body = client.get(reverse("opportunities")).content.decode()

    assert ('class="peek-toggle" aria-expanded="false" '
            'aria-controls="bulk-save-peek"') in body
    assert 'id="bulk-save-peek"' in body
