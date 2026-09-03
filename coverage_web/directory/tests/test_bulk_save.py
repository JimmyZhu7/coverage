"""The Picked for you column's "Save all", and the guardrails around it.

THIS FILE USED TO PIN A BLUE BANNER. "N roles fit you and name your year...
Save them all" sat above the board, offered a list of its own, and disclosed
it through a "Which ones?" peek panel. On 2026-09-02 the founder merged the
two surfaces — "merge the two into the pick for you widget and take away the
blue banner on the top. Picked for you should have the function to save all"
— and the banner, its peek and its year gate went with it.

Nothing here was weakened for that. The three guarantees the banner earned
the hard way are all still pinned; two of them are now asked of the column,
and one of them (the peek showing you what you are agreeing to) is satisfied
structurally rather than by a panel, because the column IS the list. Every
rewritten test says which and why in its own docstring.

WHAT "ALL" MEANS, and the measurement that decided it (see
`directory.views.track_eligible` for the full argument). The banner's set was
"every open role whose text names your class year". In a ranked column that
test can no longer do the work it was written for: a stated WRONG year is
`blocking`, and `recommend()` refuses blocked candidates outright, so the
only thing the gate could still decide is whether SILENCE disqualifies — and
`_eligibility`'s own contract is that a posting which states no window gets
no verdict in either direction. Measured on the founder's column that day (6
picks, all unsaved): 2 stated a year, 4 were silent, and the silent four were
Nomura and HSBC investment banking and markets internships in Hong Kong,
dead centre of his stated tracks and region. Keeping the gate would have put
a "Save all" under a heading of six roles and written two.

Pinned here:

  * THE COUNT, THE LIST AND THE WRITE ARE ONE. The header's count, the
    confirm sentence's count, and the ids `track_eligible` writes come from
    one call. This is the 206/209/208 invariant and it is the reason this
    file exists.
  * THE BUTTON NEVER WRITES WHAT THE COLUMN IS NOT SHOWING. There is no
    second list to disclose: every row "Save all" writes is a card the
    student can read, save or dismiss on its own, directly below the button.
  * THE COLUMN'S GATES ARE THE SAVE'S GATES — `recommend()`'s blocking-
    verdict exclusion, its stated-class veto, its rung/study-level filter and
    its passed-deadline exclusion. A role the column may not recommend is a
    role this may not write.
  * ONE ROW PER JOB. A programme a firm files once per branch office is one
    card, folded on `_family_key` (the rule the feed already groups its firm
    columns on), and the card says how many places it stands for.
  * `track_eligible` refuses to write without `confirmed=1` — the `hx-confirm`
    dialog is a UI affordance, not the actual gate.
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
    """An open campus role whose own text names class year 2027.

    A stated class match is `W_CLASS_STATED` (30) against a `MIN_SCORE` of 25,
    so one of these is a PICK for a 2027 student on that alone — which is what
    puts it in the column "Save all" writes."""
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

    The GET is not ceremony. `track_eligible` writes the exact ids the column
    OFFERED (stashed under `PICK_SAVE_OFFER_SESSION_KEY` when the feed
    rendered) rather than re-deriving a set of its own, because those two
    used to be different questions with different answers: the confirm said
    206, the write made 209. No render, no offer, nothing to honour."""
    client.get(reverse("opportunities"))
    return client.post(reverse("track_eligible"), {"confirmed": "1"}, follow=follow)


def _column(client):
    """`(body, pick_cluster, stashed ids)` off one feed render.

    The three facts every count assertion in this file compares. They are
    read from ONE response on purpose: the bug this file exists for was three
    surfaces answering one question separately."""
    resp = client.get(reverse("opportunities"))
    return (resp.content.decode(), resp.context["pick_cluster"],
            client.session.get("pick_save_offer") or [])


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
def test_confirmed_post_saves_every_picked_role(client):
    user = _student()
    _eligible_opp(1)
    _eligible_opp(2)
    client.force_login(user)

    resp = _confirm_bulk_save(client, follow=True)

    assert resp.status_code == 200
    assert UserOpportunity.objects.for_user(user).count() == 2


@pytest.mark.django_db
def test_confirmed_save_shows_an_undo_banner_naming_the_count(client):
    """The message says "picked" rather than "that name your year" since
    2026-09-02: the year gate went with the banner, so a message still
    naming it would be describing a rule that no longer runs."""
    user = _student()
    _eligible_opp(1)
    _eligible_opp(2)
    _eligible_opp(3)
    client.force_login(user)

    resp = _confirm_bulk_save(client, follow=True)

    body = resp.content.decode()
    assert "Saved 3 picked roles." in body
    assert "Undo" in body


# ---------------------------------------------------------------------------
# The number in the confirm is the number that happens
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_confirm_saves_exactly_what_the_column_counted(client):
    """The drift, reproduced: one page load, three numbers.

    The banner counted the FEED's materialised rows — folded for duplicates
    by `directory.dupes.fold_duplicates` — while `track_eligible` re-derived
    its own set from the whole open table, unfolded. On the live board that
    was 206 confirmed and 209 written; My Applications then folded them again
    and its tile read 208.

    Here: one requisition listed twice by the same firm, same title, same
    location, different URLs — the shape a board scraped twice in one week
    produces. The column shows one card. The confirm must write one row."""
    user = _student()
    firm = Firm.objects.create(name="Repeat Bank", slug="repeat-bank")
    for n in (1, 2):
        Opportunity.objects.create(
            firm=firm, url=f"https://repeat/{n}", title="Summer Analyst",
            bucket="internship", status="open", class_year="2027",
            location="New York")
    client.force_login(user)

    body, cluster, offered = _column(client)
    assert cluster["save_count"] == len(offered) == 1
    assert "Save 1" in body

    resp = _confirm_bulk_save(client, follow=True)
    assert "Saved 1 picked role." in resp.content.decode()
    assert UserOpportunity.objects.for_user(user).count() == 1


@pytest.mark.django_db
def test_a_confirm_with_no_offer_behind_it_writes_nothing(client):
    """A POST that never rendered the column has no number to honour. Same
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
def test_a_role_that_appeared_after_the_column_rendered_is_not_swept_in(client):
    """The other half of "exactly what was shown": saving MORE than the
    confirm named is the product doing something the student never agreed
    to. A role that lands between the render and the click waits for the
    next render."""
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
def test_a_role_that_started_blocking_between_render_and_click_is_dropped(client):
    """The per-row recheck can only ever REMOVE from the offered set, and
    what it rechecks is the COLUMN's own gate rather than the retired year
    test: a posting that began blocking this student between the render and
    the click is one `recommend()` would no longer rank, so it is one this
    must no longer write.

    Saving fewer than the confirm said is a fact about the last thirty
    seconds. Saving a role the product would now refuse to recommend is not.
    """
    user = _student()
    keep = _eligible_opp(1)
    turned = _eligible_opp(2)
    client.force_login(user)

    client.get(reverse("opportunities"))
    assert set(client.session["pick_save_offer"]) == {keep.id, turned.id}
    # The scrape fills in a graduation window that excludes this student,
    # which `_eligibility` reads as a blocking `year_out` verdict.
    Opportunity.objects.filter(pk=turned.pk).update(class_year="2031")

    client.post(reverse("track_eligible"), {"confirmed": "1"})

    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))
    assert written == {keep.id}


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


@pytest.mark.django_db
def test_an_htmx_confirm_redirects_the_browser_rather_than_swapping(client):
    """The button is an `hx-post`, because that is what makes `hx-confirm`
    fire the site's styled dialog (see `base.html`). A 302 body would be
    followed by the XHR and swapped into the page, so the response carries
    `HX-Redirect` instead and the browser navigates — landing on My
    Applications with the flash message and the Undo strip, exactly as the
    old form submit did."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    client.get(reverse("opportunities"))
    resp = client.post(reverse("track_eligible"), {"confirmed": "1"},
                       HTTP_HX_REQUEST="true")

    assert resp.status_code == 204
    assert resp["HX-Redirect"] == reverse("my_applications")
    assert UserOpportunity.objects.for_user(user).count() == 1


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_undo_removes_only_the_batchs_rows_and_leaves_hand_saved_ones(client):
    user = _student()
    hand_saved = _eligible_opp(1)  # not the bulk flow, just an ordinary save
    _eligible_opp(2)
    _eligible_opp(3)
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
    _eligible_opp(2)
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

    _eligible_opp(2)
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
# WHAT "ALL" MEANS: the column, and only the column
# ---------------------------------------------------------------------------
#
# The retired banner's set was "the posting names your class year AND the
# recommender would rank it" — a year gate plus a fit gate written for it
# (`_offer_fits`, gone with the banner). That fit gate existed because the
# banner had no ranking of its own: measured on the founder's board
# 2026-09-02, it offered 56 roles in one click, of which 48 named a function
# he does not recruit for or sat in a market he never named.
#
# The column has the ranking. `recommend()` applies the blocking-verdict
# exclusion, the stated-class veto, the rung/study-level filter, the
# passed-deadline exclusion, `MIN_SCORE` and `MAX_PER_FIRM`, then keeps six.
# So the save's gates are the column's gates, stated once, and the tests
# below pin exactly that rather than a second copy of the rules.


@pytest.mark.django_db
def test_a_pick_whose_posting_never_states_a_year_is_still_saved(client):
    """THE DECIDING MEASUREMENT, as a test.

    The banner would only ever write a `year_ok` verdict — the posting states
    a graduation window and the student is in it. On the founder's column 4
    of 6 picks stated no window at all (Nomura and HSBC Hong Kong internships
    on his own tracks), so that gate would have written 2 of the 6 cards
    under the button.

    It is not protecting anything here either. A stated WRONG year is
    `blocking` and `recommend()` refuses blocked candidates outright, so the
    gate's only remaining job would be to treat SILENCE as disqualifying —
    which contradicts `_eligibility`'s own contract, and contradicts the Save
    button the card beside it already draws."""
    user = _student()
    firm = Firm.objects.create(name="Quiet Bank", slug="quiet-bank")
    # Silent on the class year; a pick on the strength of the student's own
    # target firm rather than on anything the posting says about cohorts.
    from crm.models import UserFirm
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    silent = Opportunity.objects.create(
        firm=firm, url="https://x/silent", title="Summer Analyst",
        bucket="internship", status="open")
    client.force_login(user)

    body, cluster, offered = _column(client)

    assert [r["id"] for r in cluster["roles"]] == [silent.id]
    assert offered == [silent.id]
    assert "Save 1" in body

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    assert set(UserOpportunity.objects.for_user(user)
               .values_list("opportunity_id", flat=True)) == {silent.id}


@pytest.mark.django_db
def test_a_role_stating_another_class_year_is_neither_picked_nor_saved(client):
    """The gate that actually protects a bulk write, and it is the column's.

    A posting naming a graduation window this student is not in carries a
    blocking verdict, `recommend()` skips it, so it never reaches the column
    — and what "Save all" writes is the column."""
    user = _student()
    mine = _eligible_opp(1)
    other_year = _eligible_opp(2, class_year="2031")
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert [r["id"] for r in cluster["roles"]] == [mine.id]
    assert offered == [mine.id]
    assert other_year.id not in offered


@pytest.mark.django_db
def test_the_save_never_reaches_a_role_the_column_is_not_showing(client):
    """THE PROPERTY THE MERGE BUYS, and the reason the peek panel is no
    longer needed. The banner named a number and asked for a commitment
    against it without saying which roles; a panel was built to disclose
    them. The column cannot have that problem: what the button writes is what
    is rendered underneath it, so the disclosure is the feature.

    Asserted as a subset relation rather than by eye: every id the confirm
    would write is an id with a card in the column."""
    user = _student()
    for n in range(1, 5):
        _eligible_opp(n)
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert set(offered) <= {r["id"] for r in cluster["roles"]}
    assert cluster["save_count"] == len(offered) == 4


@pytest.mark.django_db
def test_a_filter_that_hides_a_pick_takes_it_out_of_the_save(client):
    """The column responds to the filter bar (see the `pick_cluster` block in
    views.py), and the button sits inside the column. A save that reached a
    role the filters had removed from the screen would be writing something
    the student cannot see from the control they clicked."""
    user = _student()
    alpha = Firm.objects.create(name="Alpha", slug="alpha")
    beta = Firm.objects.create(name="Beta", slug="beta")
    kept = _eligible_opp(1, firm=alpha)
    hidden = _eligible_opp(2, firm=beta)
    client.force_login(user)

    resp = client.get(reverse("opportunities"), {"firm": "alpha"})
    cluster = resp.context["pick_cluster"]

    assert [r["id"] for r in cluster["roles"]] == [kept.id]
    assert cluster["hidden_by_filter"] == 1
    assert client.session["pick_save_offer"] == [kept.id]

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    assert set(UserOpportunity.objects.for_user(user)
               .values_list("opportunity_id", flat=True)) == {kept.id}


@pytest.mark.django_db
def test_a_role_already_tracked_or_dismissed_is_not_in_the_save(client):
    """Two exclusions the banner's own count already made, kept: a role the
    student already tracks has nothing to add, and a dismissed one they have
    already answered. "Not for me" outranks "picked for you"."""
    user = _student()
    mine = _eligible_opp(1)
    already = _eligible_opp(2)
    dismissed = _eligible_opp(3)
    client.force_login(user)
    client.post(reverse("track_opportunity", args=[already.id]), {"status": "saved"})
    client.post(reverse("track_opportunity", args=[dismissed.id]), {"status": "dismiss"})

    _, cluster, offered = _column(client)

    assert offered == [mine.id]
    # The tracked role is still SHOWN — it is still one of the best things on
    # the board for this student — it is simply not something to write again.
    assert {r["id"] for r in cluster["roles"]} == {mine.id, already.id}
    assert cluster["save_count"] == 1


@pytest.mark.django_db
def test_with_everything_saved_the_button_says_so_rather_than_vanishing(client):
    """A control that disappears reads as breakage. One that states what it
    would do and why it cannot is an answer — so at zero the button renders
    disabled, saying "All saved"."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    _confirm_bulk_save(client)
    body, cluster, offered = _column(client)

    assert cluster["save_count"] == 0
    assert offered == []
    assert 'class="btn pickcol-save"' in body, "the control is still drawn"
    assert "All saved" in body
    # The MARKUP, not the words: this page ships its own <style> block and
    # "Save all" appears in its comments, so a prose search reads the
    # stylesheet and fails for reasons that have nothing to do with the
    # feature (the same trap `.rr-places` carries a note about below). What
    # matters is that the disabled button cannot issue the write.
    assert reverse("track_eligible") not in body
    assert "disabled" in body[body.index("pickcol-save"):
                              body.index("</header>", body.index("pickcol-save"))]


# ---------------------------------------------------------------------------
# THE RUNG OF THE LADDER
# ---------------------------------------------------------------------------
#
# THE MEASURED DEFECT (founder's live board, 2026-09-02; class 2029, tracks
# ib+st, regions hk+us). Once the retired banner's offer was scored it came to
# fourteen roles, and two of the fourteen were Wells Fargo PhD internships —
# "2027 Quantitative Analytics Summer Internship Applied Computational
# Intelligence (ACI PhD)" and its Capital Markets sibling. Both state his
# graduation window, both sit in a market he named, both score in the
# seventies, and neither is a job a sophomore can take.
#
# THIS GATE SURVIVED THE MERGE, and is applied harder than the banner applied
# it: the banner ran `level_mismatch` alone, while `recommend()` runs
# `role_matches_level` with the student's `study_level` and falls back to
# `level_mismatch` only where the posting states its own window. So these
# tests are unchanged in substance; only the surface they read moved.


def _fit_student(email="fit@example.com"):
    """A student who has said what they want: IB in the US, class of 2027."""
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


def _undergrad(email="rung@example.com"):
    """A student who has stated the rung as well as the desk and the market.

    `study_level` is what turns the rung test on at all. Without it
    `student_level` falls back to reading the target cycles, and with neither
    stated it answers "" — which filters nothing."""
    u = _fit_student(email=email)
    u.study_level = "undergrad"
    u.save(update_fields=["study_level"])
    return u


@pytest.mark.django_db
def test_a_phd_internship_is_not_picked_for_an_undergraduate(client):
    """The posting's own title says who it is for, and it is not him.

    A bulk save is a decision made once for every row in it, so a role the
    student cannot take is not a row to be scrolled past, it is clutter
    committed to his own pipeline."""
    user = _undergrad()
    wanted = _titled_opp(1, "Investment Banking Summer Analyst")
    phd = _titled_opp(2, "Quantitative Analytics Summer Internship (PhD)")
    client.force_login(user)

    body, cluster, offered = _column(client)

    assert offered == [wanted.id]
    assert phd.id not in {r["id"] for r in cluster["roles"]}
    assert "Save 1" in body


@pytest.mark.django_db
def test_the_advanced_degree_rung_is_refused_under_its_other_name(client):
    """"Summer Associate" is the banks' name for the same rung, and the
    reason `role_level` reads shapes rather than the word "Associate" alone
    (bare "Associate" is the undergraduate entry title at PwC and Deloitte).
    An undergraduate is offered neither spelling."""
    user = _undergrad()
    analyst = _titled_opp(1, "Investment Banking Summer Analyst")
    _titled_opp(2, "Investment Banking Summer Associate")
    client.force_login(user)

    _, _, offered = _column(client)

    assert offered == [analyst.id]


@pytest.mark.django_db
def test_a_rung_nobody_stated_filters_nothing(client):
    """P3, degrade to the old behaviour on thin data. The rung test is silent
    the moment either side is silent, so a student who never stated a study
    level and a title that names no rung are both filtered on nothing.

    Two halves, because either one alone would pass for the wrong reason: a
    student with no level sees the PhD row, and a student WITH a level still
    sees a title that names no rung at all."""
    silent_student = _fit_student(email="nolevel@example.com")
    phd = _titled_opp(1, "Quantitative Analytics Summer Internship (PhD)")
    client.force_login(silent_student)
    assert _column(client)[2] == [phd.id]

    client.logout()
    stated = _undergrad(email="stated@example.com")
    quiet = _titled_opp(2, "Investment Banking Summer Programme")
    client.force_login(stated)
    assert _column(client)[2] == [quiet.id]


@pytest.mark.django_db
def test_a_passed_deadline_is_never_picked_and_so_never_saved(client):
    """`recommend()` skips a candidate whose deadline is already behind it,
    and that exclusion is what makes the abandoned-posting check the banner
    carried unnecessary here (see `pick_save_ids`): a pick's deadline is
    always future or absent, so there is no row for "the firm left this up
    and nobody took it down" to be true of."""
    import datetime as _dt

    user = _student()
    live = _eligible_opp(1)
    stale = _eligible_opp(2)
    Opportunity.objects.filter(pk=stale.pk).update(
        deadline=_dt.date(2020, 1, 1))
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert offered == [live.id]
    assert stale.id not in {r["id"] for r in cluster["roles"]}


@pytest.mark.django_db
def test_the_count_the_column_and_the_write_agree_across_the_rung_gate(client):
    """THE INVARIANT the whole file exists for, asked where a gate is
    actually doing something.

    The header's count, the cards the column renders, the ids stashed for the
    confirm and the rows it creates are one list from one call. A new gate is
    exactly where that breaks — 206/209/208 was a count and a write asking
    two slightly different questions — so all four are read here on a board
    with a PhD row present and excluded."""
    user = _undergrad()
    keep = {_titled_opp(1, "Investment Banking Summer Analyst").id,
            _titled_opp(2, "Investment Banking Off-Cycle Analyst").id}
    _titled_opp(3, "Quantitative Analytics Summer Internship (PhD)")
    client.force_login(user)

    body, cluster, offered = _column(client)

    assert cluster["save_count"] == 2
    assert {r["id"] for r in cluster["roles"]} == keep
    assert set(offered) == keep
    assert "Save all 2" in body
    assert "Save 2 picked roles to My Applications?" in body

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    assert set(UserOpportunity.objects.for_user(user)
               .values_list("opportunity_id", flat=True)) == keep


# ---------------------------------------------------------------------------
# One row per JOB, not one per branch office
# ---------------------------------------------------------------------------
#
# THE MEASURED DEFECT (founder's live offer, 2026-09-02; user 6, class 2029,
# tracks ib+st, regions hk+us). The retired banner's offer had just been
# narrowed from 56 roles to 20, which fixed relevance and did nothing about
# repetition: 9 of those 20 rows were one KeyBank programme filed as one
# requisition per branch — Bellingham, Toledo, Cleveland, Erie, Goshen,
# Cicero, Dayton, Boise, Pottstown — and five of the peek panel's eight
# visible rows were that same title. Nearly half an offer, and most of what a
# student could actually see of it, was one job.
#
# `fold_duplicates` cannot reach this and must not: it keys on the normalized
# title (nine titles naming nine cities are nine strings) and then treats a
# stated city as a hard divider, deliberately, because folding London into New
# York on a BOARD deletes a job from the catalogue. A shortlist of six
# decisions is not a catalogue, so it folds — on `_family_key`, the rule the
# feed's firm columns already group on, because one page may not hold two
# answers to "is this the same programme in another city?" (P5).
#
# THE FOLD MOVED WITH THE SAVE (2026-09-02) from the banner's offer to the
# column itself, which is what keeps it true of the thing the button writes.
# Pinned here: the fold happens, it happens AFTER the ranking, what gets saved
# is the survivor alone, the card says how many places it stands for, and the
# count/column/write invariant survives a fold.


def _branch_opp(firm, base, city, *, class_year="2027", region="us",
                deadline=None, cohort=""):
    """One requisition of a programme run in several branch offices.

    The title ends in the row's own city, which is the shape `_family_key`
    recognises and the shape every firm in the measurement used.
    """
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{base}/{city}".replace(" ", "-"),
        title=f"{base} - {city}", location=city, bucket="internship",
        status="open", class_year=class_year, region=region,
        deadline=deadline, cohort=cohort,
    )


@pytest.mark.django_db
def test_one_programme_in_six_cities_is_one_card(client):
    """The founder's complaint, at test scale: "this is still showing me so
    many of the same role from different locations from the same bank"."""
    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    for city in ("Boise", "Toledo", "Goshen", "Cicero", "Erie", "Pottstown"):
        _branch_opp(firm, "Summer Analyst Programme", city)
    client.force_login(user)

    body, cluster, offered = _column(client)

    assert len(cluster["roles"]) == len(offered) == 1
    assert "Save 1" in body


@pytest.mark.django_db
def test_the_confirm_writes_the_survivor_and_not_the_whole_family(client):
    """The fold has to mean the same thing to the write that it means to the
    number. Saving all six because the column counted one would be the
    206/209/208 defect rebuilt from the other end — the button naming a
    commitment smaller than the one the click makes."""
    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    for city in ("Boise", "Toledo", "Goshen", "Cicero", "Erie", "Pottstown"):
        _branch_opp(firm, "Summer Analyst Programme", city)
    client.force_login(user)

    client.get(reverse("opportunities"))
    offered = set(client.session["pick_save_offer"])
    client.post(reverse("track_eligible"), {"confirmed": "1"})

    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))
    assert len(offered) == 1
    assert written == offered


@pytest.mark.django_db
def test_the_card_says_how_many_places_a_folded_row_stands_for(client):
    """Fold, but never silently (P4). Six branch offices behind one card is a
    fact the student needs in order to know the choice exists and that the
    firm's own column is where to make it — the alternative is a column that
    reads as if the other five addresses were never posted, and a "Save all"
    that quietly commits to one of six towns."""
    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    for city in ("Boise", "Toledo", "Goshen", "Cicero", "Erie", "Pottstown"):
        _branch_opp(firm, "Summer Analyst Programme", city)
    client.force_login(user)

    body, cluster, _ = _column(client)

    assert [r["places"] for r in cluster["roles"]] == [6]
    assert "6 cities" in body


@pytest.mark.django_db
def test_a_row_standing_only_for_itself_says_nothing_about_places(client):
    """The chip is a fact about a fold, so a row that folded nothing carries
    none. A "1 city" on every ordinary role would be noise dressed as
    information."""
    user = _student()
    _eligible_opp(1)
    client.force_login(user)

    body, cluster, _ = _column(client)

    assert [r["places"] for r in cluster["roles"]] == [None]
    # The chip's rendered ATTRIBUTE, not the word and not the bare class name:
    # both "cities" and `.rr-places` appear in this page's own stylesheet, and
    # a test that reads prose out of a CSS block fails for reasons that have
    # nothing to do with the feature.
    assert 'class="rr-places"' not in body


@pytest.mark.django_db
def test_the_count_the_column_and_the_write_still_agree_over_a_fold(client):
    """THE INVARIANT, asked of the surface that folds.

    206/209/208 happened because three surfaces answered one question
    separately. A fold is exactly the kind of change that reintroduces it —
    it is easy to fold the number and not the list, or the list and not the
    write. All four figures here come from the single `picked_roles` call:
    the header's count, the cards rendered, the ids stashed, and the rows the
    confirm creates."""
    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    for city in ("Boise", "Toledo", "Goshen", "Cicero"):
        _branch_opp(firm, "Summer Analyst Programme", city)
    # Two roles that are nobody's city variant, so the offer is a mix.
    solo = Firm.objects.create(name="Solo Bank", slug="solo-bank")
    Opportunity.objects.create(
        firm=solo, url="https://x/solo-a", title="Markets Summer Analyst",
        bucket="internship", status="open", class_year="2027")
    Opportunity.objects.create(
        firm=solo, url="https://x/solo-b", title="Research Summer Analyst",
        bucket="internship", status="open", class_year="2027")
    client.force_login(user)

    body, cluster, offered = _column(client)

    assert len(offered) == 3
    assert cluster["save_count"] == 3
    assert {r["id"] for r in cluster["roles"]} == set(offered)
    assert "Save all 3" in body
    assert "Save 3 picked roles to My Applications?" in body

    client.post(reverse("track_eligible"), {"confirmed": "1"})
    written = set(UserOpportunity.objects.for_user(user)
                  .values_list("opportunity_id", flat=True))
    assert written == set(offered)


@pytest.mark.django_db
def test_two_different_roles_at_one_firm_are_not_folded_together(client):
    """The fold reads a city, not a firm. Moelis runs five different Virtual
    Discovery Series events — Ask the Analysts, Acing the Interview, Life
    Sciences, Capital Markets, Moelis 101 — all on the founder's live board
    and every one of them a different thing to attend. A per-firm cap would
    have eaten four of them; this fold must not.

    `MAX_PER_FIRM` still caps the COLUMN at two per firm, which is a
    different and deliberate rule (a shortlist of six from one bank is a
    correct ranking and a useless shortlist). What this pins is that the fold
    itself is not doing the eating: two distinct titles survive it, where a
    title-blind fold would have left one."""
    user = _student()
    firm = Firm.objects.create(name="Series Bank", slug="series-bank")
    for n, topic in enumerate(("Ask the Analysts", "Acing the Interview",
                               "Capital Markets Overview", "Bank 101"), 1):
        Opportunity.objects.create(
            firm=firm, url=f"https://x/series/{n}",
            title=f"Virtual Discovery Series: {topic}",
            bucket="internship", status="open", class_year="2027")
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert len(offered) == 2
    assert [r["places"] for r in cluster["roles"]] == [None, None]


@pytest.mark.django_db
def test_one_programme_in_two_markets_is_two_cards(client):
    """THE REGRESSION THIS TERM WAS ADDED FOR, measured on the founder's own
    column 2026-09-02 while the merge was being built.

    Nomura runs its 2027 Global Markets programme in Hong Kong (6293) and
    Singapore (6292), and its Investment Banking programme in the same two.
    The retired bulk-save offer folded AFTER a hard region gate, so a family
    could never span two markets; this column has no hard region gate — the
    scorer charges a wrong market and lets a strong row survive it — so the
    first version of the fold saw one four-city family, `_survivor_rank`
    (blind to this student) kept the Singapore copy, Singapore is a market he
    never named, and a Hong Kong Global Markets internship he had been shown
    all week dropped out of his column.

    A false fold costs a job never seen. So the market is part of the family:
    within one market the members differ only by town and score identically,
    across markets they are different decisions."""
    user = _student()
    firm = Firm.objects.create(name="Two Market Bank", slug="two-market-bank")
    hk = _branch_opp(firm, "Global Markets Summer Internship", "Hong Kong",
                     region="hk")
    sg = _branch_opp(firm, "Global Markets Summer Internship", "Singapore",
                     region="sg")
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert set(offered) == {hk.id, sg.id}
    # Neither is standing for the other, so neither claims a city count.
    assert [r["places"] for r in cluster["roles"]] == [None, None]


@pytest.mark.django_db
def test_branches_inside_one_market_still_fold(client):
    """The other side of the same term, so it cannot be widened into "never
    fold". Four towns in ONE market is one decision, and the card says four.
    """
    user = _student()
    firm = Firm.objects.create(name="One Market Bank", slug="one-market-bank")
    for city in ("Boise", "Toledo", "Goshen", "Cicero"):
        _branch_opp(firm, "Summer Analyst Programme", city, region="us")
    client.force_login(user)

    _, cluster, offered = _column(client)

    assert len(offered) == 1
    assert [r["places"] for r in cluster["roles"]] == [4]


@pytest.mark.django_db
def test_a_family_with_two_stated_deadlines_is_left_whole(client):
    """`_competing_claims`, the same veto `fold_duplicates` applies and for
    the same reason: two different act-by dates is a repeating series, not one
    job listed twice, and hiding either costs a date the student could still
    make. The vetoes may not mean one thing on the board and another here, so
    both folds read the one definition."""
    import datetime as _dt

    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    _branch_opp(firm, "Summer Analyst Programme", "Boise",
                deadline=_dt.date(2099, 3, 1))
    _branch_opp(firm, "Summer Analyst Programme", "Toledo",
                deadline=_dt.date(2099, 9, 1))
    client.force_login(user)

    assert len(_column(client)[2]) == 2


@pytest.mark.django_db
def test_the_fold_runs_after_the_ranking_and_not_before(client):
    """ORDER OF OPERATIONS, and it is load-bearing.

    Fold first and a family's survivor can be a row the ranker would have
    rejected, which then takes the qualifying sibling down with it. Here the
    2031 copy would win `_survivor_rank` outright — it states a deadline and
    the other does not — and it names a class year this student is not in, so
    `recommend()` refuses it. Ranking first means only qualifying rows ever
    reach the fold, so the survivor is always a role that could have been
    picked on its own."""
    import datetime as _dt

    user = _student()
    firm = Firm.objects.create(name="Branch Bank", slug="branch-bank")
    wrong = _branch_opp(firm, "Summer Analyst Programme", "London",
                        class_year="2031", deadline=_dt.date(2099, 3, 1))
    here = _branch_opp(firm, "Summer Analyst Programme", "New York")
    client.force_login(user)

    offered = _column(client)[2]

    assert offered == [here.id]
    assert wrong.id not in offered
