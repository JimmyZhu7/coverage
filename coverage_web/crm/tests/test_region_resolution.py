"""How a contact's recruiting region gets decided on the WRITE path.

The one rule underneath every test here: write only what a stated fact
entails, and ask for everything else. A wrong region silently mis-scopes the
cadence engine's pre-deadline re-ping and the student has no reason to doubt
it; a blank one is degraded but honest, and the engine's both-regions
fallback is built for exactly that.

Precedence (`Contact.resolve_region`), first match wins:

  1. a region already on the row                          -> untouched
  2. len(User.regions ∩ {us,hk}) == 1                     -> "declared"
  3. len(Firm.regions ∩ User.regions ∩ {us,hk}) == 1      -> "firm"
  4. len(Firm.regions ∩ {us,hk}) == 1                     -> "firm"
  5. nothing                                              -> blank

Nothing probabilistic is anywhere on this path, and that is a measured
decision rather than a missing feature — see `Contact.resolve_region`'s
docstring for the 174-message sample that killed the Date-header offset,
send-hour clustering and signature-block cities.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from coverage_domain import cadence
from crm.models import Contact
from crm.views import _in_scope, contact_region
from directory.models import Firm

User = get_user_model()


def _user(regions=None, email="student@example.com"):
    return User.objects.create_user(
        email=email, password="x", regions=list(regions or [])
    )


def _firm(regions, slug="f", name="A Firm"):
    return Firm.objects.create(slug=slug, name=name, regions=list(regions))


def _place(user, firm=None, **kw):
    """Create a contact the way every real write path does — through
    `save()`, which is where resolution lives."""
    return Contact.all_objects.create(user=user, name="Pat", firm=firm, **kw)


# ---------------------------------------------------------------------------
# Must infer.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_single_declared_market_beats_the_firms_two():
    """(1) THE CASE THE WHOLE TIER EXISTS FOR. A US-only student's contact at
    a bulge bracket is a US contact. The firm's Hong Kong desk is a fact
    about the firm, and irrelevant to somebody who is not recruiting there —
    which is why tier 2 sits ABOVE the firm rather than below it."""
    user = _user(["us"])
    c = _place(user, _firm(["hk", "us"]))
    assert (c.region, c.region_source) == ("us", "declared")


@pytest.mark.django_db
def test_a_single_declared_market_answers_with_no_firm_at_all():
    """(2) No firm, no problem: the student's own declaration is the stated
    fact, and it entails the region on its own."""
    user = _user(["us"])
    c = _place(user, None)
    assert (c.region, c.region_source) == ("us", "declared")


@pytest.mark.django_db
def test_an_unambiguous_firm_still_answers_for_a_two_market_student():
    """(3) Today's behaviour, and it must not regress: the student recruits
    in both markets, so their declaration decides nothing, and a firm that
    recruits in exactly one does."""
    user = _user(["hk", "us"])
    c = _place(user, _firm(["hk"]))
    assert (c.region, c.region_source) == ("hk", "firm")


@pytest.mark.django_db
def test_an_untracked_declared_market_does_not_make_the_declaration_ambiguous():
    """(4) `['us', 'sg']` still names exactly ONE deadline market. Singapore
    is a market `Contact.region` cannot express, so it is intersected away
    before anything is decided and the US half answers alone.

    DEVIATION FROM THE SPEC, stated plainly: the brief's test 4 expects
    source "firm" here. The precedence table it also calls non-negotiable
    gives "declared" — tier 2's test is `len(User.regions ∩ {us,hk}) == 1`,
    which `['us', 'sg']` satisfies, and tier 2 is explicitly stated to beat
    tier 3. The region is "us" under either reading; only the provenance
    differs, and the table wins. It matters for one thing: this row now
    blanks on a Settings change that adds Hong Kong, which is correct —
    the premise ("the US is the only market I recruit in") really did stop
    holding.
    """
    user = _user(["us", "sg"])
    c = _place(user, _firm(["hk", "us"]))
    assert c.region == "us"
    assert c.region_source == "declared"


# ---------------------------------------------------------------------------
# Must NOT infer. Blank has to stay reachable.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_bulge_bracket_case_writes_nothing():
    """(5) The student recruits in both markets; the firm recruits in both
    markets. Nothing stated entails a desk, so nothing is written. This is
    the case the founder's own account is made of, and the one a
    probabilistic signal would have quietly answered wrong."""
    user = _user(["hk", "us"])
    c = _place(user, _firm(["hk", "us"]))
    assert (c.region, c.region_source) == ("", "")


@pytest.mark.django_db
def test_a_singapore_only_student_is_never_mapped_to_other():
    """(6) `Contact.region` has three values and "sg" is not one of them.
    The tempting bug is to call that "other" — but "other" means KNOWN to be
    outside both markets, and a student's Singapore interest says nothing
    about where this person sits. Blank, in every firm shape that reaches
    tier 5.

    On the brief's wording ("+ any firm"): a firm carrying exactly one
    deadline market still answers here, through tier 4, exactly as it does
    today for a student who has declared nothing. That is the unchanged
    `default_region_from_firm` rule and the last test below pins it."""
    user = _user(["sg"])
    assert _place(user, None).region == ""
    assert _place(user, _firm(["sg"], slug="sg")).region == ""
    assert _place(user, _firm(["hk", "us"], slug="d")).region == ""
    # And the tier-4 corner the sentence above is about, stated rather than
    # left implied: an unambiguous firm is still an unambiguous firm.
    hk_only = _place(user, _firm(["hk"], slug="hkonly"))
    assert (hk_only.region, hk_only.region_source) == ("hk", "firm")


@pytest.mark.django_db
def test_a_firm_outside_both_markets_never_pins_anyone_to_other():
    """(7) A Singapore-only firm describes where the FIRM operates, never
    where this person sits. `default_region_from_firm`'s original refusal,
    preserved verbatim: "other" is only ever written by a human."""
    user = _user(["hk", "us"])
    c = _place(user, _firm(["sg"]))
    assert (c.region, c.region_source) == ("", "")


@pytest.mark.django_db
def test_apac_is_evidence_of_nothing():
    """(8) Jane Street carries ['us', 'apac'] and APAC contains Hong Kong.

    DEVIATION FROM THE SPEC, stated plainly: the brief expects "" here and
    calls it "existing rule, keep it". It is not the existing rule.
    `default_region_from_firm` has always intersected with {us, hk}, which
    drops "apac" on the floor and leaves {us} — an unambiguous firm — so
    this firm has always resolved to "us", both before this change and
    after. Keeping the existing rule and returning "" are two different
    instructions and the existing rule wins; changing it would silently
    unplace live rows at a Jane-Street-shaped firm.

    Where "apac" IS treated as evidence of nothing is the Network board's
    "Other countries" tab (`crm.views._OTHER_FIRM_REGIONS`), which is a
    different question: whether a firm's footprint puts an UNKNOWN contact
    outside both markets. That refusal is untouched.
    """
    user = _user(["hk", "us"])
    c = _place(user, _firm(["us", "apac"]))
    assert c.region == "us"
    assert c.region_source == "firm"


@pytest.mark.django_db
def test_a_student_who_has_declared_nothing_gets_nothing_from_the_declaration():
    """(9) Empty regions means the question has never been ANSWERED, not
    "interested in nothing" — so it entails nothing and tier 2 is skipped.
    An ambiguous firm then leaves the row blank."""
    user = _user([])
    assert _place(user, None).region == ""
    assert _place(user, _firm(["hk", "us"])).region == ""


# ---------------------------------------------------------------------------
# A human's answer is never overwritten.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_hand_set_region_survives_a_contradicting_declaration():
    """(10) The student set this person to Hong Kong and declares only the
    US. Tier 1 wins and nothing below it runs — the person knows where their
    contact sits and the rule does not."""
    user = _user(["us"])
    c = _place(user, _firm(["us"]), region="hk", region_source="user")
    c.save()
    c.refresh_from_db()
    assert (c.region, c.region_source) == ("hk", "user")


@pytest.mark.django_db
def test_a_partial_save_neither_moves_nor_clobbers_a_hand_set_region():
    """(11) Same thing under `save(update_fields=[...])` — the shape every
    quick action on the contact card uses. `region_source` must not be
    silently reset to blank by a save that never mentioned it."""
    user = _user(["us"])
    c = _place(user, _firm(["us"]), region="hk", region_source="user")
    c.warmth = "replied"
    c.save(update_fields=["warmth"])
    c.refresh_from_db()
    assert (c.region, c.region_source, c.warmth) == ("hk", "user", "replied")


@pytest.mark.django_db
def test_a_partial_save_still_persists_a_region_it_just_resolved():
    """The other half of the `update_fields` contract, inherited from the
    original firm-default: a partial save that RESOLVES a region has to
    widen its own field list, or the value vanishes on the way to the DB."""
    user = _user(["hk", "us"])
    firm = _firm(["hk", "us"])
    c = _place(user, firm)
    assert c.region == ""
    # The student narrows to one market; the row is re-resolved on its next
    # partial save.
    user.regions = ["us"]
    user.save(update_fields=["regions"])
    c.user = user
    c.warmth = "replied"
    c.save(update_fields=["warmth"])
    c.refresh_from_db()
    assert (c.region, c.region_source) == ("us", "declared")


@pytest.mark.django_db
def test_blank_source_and_blank_region_are_the_same_state():
    """The invariant, both ways round. A placed contact always knows who
    placed them, and an unplaced one never claims a provenance."""
    user = _user(["us"])
    placed = _place(user, None)
    unplaced = _place(_user([], email="b@example.com"), None)
    assert bool(placed.region) is bool(placed.region_source)
    assert bool(unplaced.region) is bool(unplaced.region_source)
    for c in Contact.all_objects.all():
        assert bool(c.region) == bool(c.region_source), c.pk


@pytest.mark.django_db
def test_a_region_set_by_hand_with_no_source_heals_to_user():
    """Every by-hand path — the edit form, the bulk verbs,
    `capture_discover --region` — writes a region and says nothing about
    provenance. That has to land as "user", not as a row that breaks the
    invariant or one a later re-derivation feels free to rewrite."""
    user = _user(["us"])
    c = _place(user, None, region="other")
    assert (c.region, c.region_source) == ("other", "user")


# ---------------------------------------------------------------------------
# The reversal: a declaration that stops holding.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_adding_a_second_market_unplaces_exactly_the_declared_rows():
    """(12) Every row filed under tier 2 rests on "this is my only market".
    The day that stops being true the premise is gone, and a silently-wrong
    region is worse than none. Rows a human placed, and rows a single-market
    firm answered, are untouched — neither premise changed."""
    from crm import regions as crm_regions

    user = _user(["us"])
    declared = _place(user, _firm(["hk", "us"], slug="d"))
    by_hand = _place(user, None, region="other", region_source="user")
    by_firm = Contact.all_objects.create(
        user=user, name="Firmy", firm=_firm(["hk"], slug="hk"),
        region="hk", region_source="firm",
    )
    assert (declared.region, declared.region_source) == ("us", "declared")

    user.regions = ["hk", "us"]
    user.save(update_fields=["regions"])
    result = crm_regions.unplace_declared_regions(user, previous_regions=["us"])

    assert result.count == 1
    declared.refresh_from_db()
    by_hand.refresh_from_db()
    by_firm.refresh_from_db()
    assert (declared.region, declared.region_source) == ("", "")
    assert (by_hand.region, by_hand.region_source) == ("other", "user")
    assert (by_firm.region, by_firm.region_source) == ("hk", "firm")


@pytest.mark.django_db
def test_narrowing_to_one_market_re_resolves_the_rows_left_blank(client):
    """(13) The other direction. A student who recruits in both markets
    leaves bulge-bracket contacts unplaced; narrowing to one market makes
    the declaration entail an answer, and those rows take it on their next
    save. Nothing is bulk-rewritten — a blank row is not a claim, so there
    is nothing to correct in a hurry."""
    user = _user(["hk", "us"])
    c = _place(user, _firm(["hk", "us"]))
    assert c.region == ""

    user.regions = ["us"]
    user.save(update_fields=["regions"])
    fresh = Contact.all_objects.get(pk=c.pk)
    fresh.save()
    fresh.refresh_from_db()
    assert (fresh.region, fresh.region_source) == ("us", "declared")


@pytest.mark.django_db
def test_the_reversal_reports_what_it_did_in_the_students_own_terms(client):
    """The message is the point of the feature. 143 contacts changing state
    with no word is the same bug as a wrong region — the student has no
    reason to look."""
    user = _user(["us"])
    for i in range(3):
        _place(user, _firm(["hk", "us"], slug=f"d{i}"))

    client.force_login(user)
    resp = client.post(
        reverse("accounts:settings"),
        {"section": "profile", "name": "", "school": "", "school_emails": "",
         "class_year": "", "regions": ["us", "hk"], "tracks": [],
         "target_cycles": [], "timezone": ""},
        follow=True,
    )
    assert resp.status_code == 200
    body = " ".join(m.message for m in resp.context["messages"])
    assert "Hong Kong" in body
    assert "3 contacts" in body
    assert "unplaced" in body.lower()
    assert not Contact.all_objects.filter(
        user=user, region_source=Contact.REGION_SOURCE_DECLARED
    ).exists()


# ---------------------------------------------------------------------------
# Regressions: blank has to keep behaving like blank everywhere.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_blank_contact_still_gets_the_engines_both_regions_fallback():
    """(14) The reason blank must stay reachable at all. `contact_region`
    returns None, and branch 3 of the cadence engine answers a None by
    matching the soonest close in ANY region rather than skipping the
    person."""
    user = _user(["hk", "us"])
    c = _place(user, _firm(["hk", "us"]))
    assert contact_region(c) is None
    assert cadence.contact_region({"region": c.region, "source": c.source}) is None


@pytest.mark.django_db
def test_a_blank_contact_still_shows_in_both_firm_tabs_as_a_flagged_guess(client):
    """(15) Unplaced is not hidden. The contact appears in every tab their
    firm suggests, counts toward the "shown on a guess" caveat, and renders
    no region pill — an admission of ignorance, never a regional claim."""
    user = _user(["hk", "us"])
    firm = _firm(["hk", "us"])
    c = _place(user, firm, warmth="replied")
    assert _in_scope(c, "us") and _in_scope(c, "hk")

    client.force_login(user)
    for scope in ("us", "hk"):
        resp = client.get(reverse("crm:contact_list"), {"scope": scope})
        names = {card["c"].name
                 for s in resp.context["sections"] for card in s["cards"]}
        assert "Pat" in names
        assert resp.context["unconfirmed_total"] == 1


@pytest.mark.django_db
def test_other_still_matches_no_us_or_hk_close_bucket():
    """(16) "other" names no per-region close bucket, so a person in London
    is scoped to no us/hk deadline at all — deliberately different from a
    blank, which matches either."""
    assert cadence.contact_region({"region": "other", "source": "manual"}) == "other"
    user = _user(["hk", "us"])
    c = _place(user, _firm(["hk", "us"]), region="other")
    assert not _in_scope(c, "us")
    assert not _in_scope(c, "hk")
    assert _in_scope(c, "other")


# ---------------------------------------------------------------------------
# Performance.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_importing_two_hundred_contacts_reads_the_declaration_once():
    """(17) Resolution needs the student's declared markets for every row it
    places. Read from `self.user` per contact that is 200 identical queries
    on a mailbox import; read once above the loop it is none. The assertion
    is on queries mentioning the user table, not on a total — the import
    does plenty of other legitimate work."""
    from accounts.services import parse_contacts_csv

    user = _user(["us"])
    rows = "\n".join(f"Person {i},Some Firm {i},Analyst,p{i}@x.com"
                     for i in range(200))
    csv_text = "name,firm,role,email\n" + rows

    with CaptureQueriesContext(connection) as ctx:
        result = parse_contacts_csv(user, csv_text)

    assert result.created == 200
    user_reads = [q for q in ctx.captured_queries
                  if ' FROM "users"' in q["sql"]]
    assert len(user_reads) == 0, user_reads
    assert set(
        Contact.all_objects.filter(user=user).values_list(
            "region", "region_source"
        )
    ) == {("us", "declared")}
