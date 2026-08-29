"""Adversarial invariant suite for the two modules around the identity ladder:
`crm.merge` (what happens once a duplicate is SUGGESTED) and `crm.sourcing`
(who to look for at a firm you have no way into).

Companion to `crm/tests/test_merge.py` and `crm/tests/test_sourcing.py`, which
pin the examples. This file pins the properties, and the two it cares most
about are the two that would be invisible to an example test:

  * A SUGGESTION IS NOT AN OPINION ABOUT ROW ORDER. `candidate_pairs` walks an
    O(n^2) cross-product of a queryset and truncates at `MAX_SUGGESTIONS`, and
    `suggestion_for` re-derives the pair on the POST that performs the merge.
    If the walk depended on insertion order, the tap could act on a different
    pair than the card the user read.
  * AN ANSWER IS FOREVER, WHICHEVER WAY ROUND IT WAS ASKED. Merged, undone and
    rejected all suppress, and the pair is unordered — a suggestion that came
    back with the two rows swapped would be the anti-nag contract broken.

`crm.sourcing`'s section is a HOLD-THE-LINE check, not a feature audit: the
module's own promise is that "who to find" stays a pure client-side link-out
to LinkedIn — no fetch, no cache, no server-side call — and these tests are
what make that promise fail loudly if anyone ever adds one.

Same discipline as `crm/tests/test_stress_crm.py`: no `hypothesis`, finite
spaces walked exhaustively, and the one order question uses a seeded shuffle.
"""

from __future__ import annotations

import inspect
import itertools
import random
from urllib.parse import urlparse

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm import merge as merge_service, sourcing
from crm.models import Contact, ContactMerge, Touch

SEED = 20260828

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="stress-merge@example.com", password="x")


def _contact(user, name, email="", **kw):
    return Contact.all_objects.create(user=user, name=name, email=email, **kw)


# The Ebba shape (one AWS account manager as two rows) plus the same-employer
# shape, because those are the two the suggestive rung actually fires on.
_DUP_POPULATION = [
    ("Ebba af Klercker", "ebbakler@amazon.com"),
    ("Ebba Kler", "ebbakler@amazon.es"),
    ("John Smith", "john.smith@gs.example"),
    ("John Smith", "j.smith@gs.example"),
    ("Amy Zhou", "amy.zhou@jefferies.example"),
    ("Warren Zhang", "warren.zhang@clsa.example"),
    ("Alvan Tay", ""),
    ("Alvan Tay", "alvan.tay@evercore.example"),
]


# ===========================================================================
# INVARIANT 1 — the suggested pair set does not depend on row insertion order.
#
# `Contact.Meta.ordering` is `["-created"]`, so creation order IS queryset
# order, and `candidate_pairs` returns early once it has `MAX_SUGGESTIONS`.
# Seeded, because "does this depend on order" is the unbounded question.
# ===========================================================================
@pytest.mark.django_db
def test_candidate_pairs_is_invariant_under_insertion_order(student):
    rng = random.Random(SEED)
    baseline = None
    for _ in range(15):
        Contact.all_objects.filter(user=student).delete()
        order = _DUP_POPULATION[:]
        rng.shuffle(order)
        for name, email in order:
            _contact(student, name, email)
        pairs = {
            frozenset((c.primary.email, c.duplicate.email))
            for c in merge_service.candidate_pairs(student)
        }
        if baseline is None:
            baseline = pairs
        assert pairs == baseline, f"order-dependent suggestions for {order}"
    assert baseline, "the population must produce at least one suggestion"


@pytest.mark.django_db
def test_the_keep_side_is_the_row_with_more_history_not_the_newer_one(student):
    """`_pick_primary` decides which card survives, and the survivor's warmth
    is the warmth that survives with it. History depth has to win over both
    insertion order and archived state, in either argument order."""
    thin = _contact(student, "Ebba Kler", "ebbakler@amazon.es")
    thick = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com")
    for i in range(3):
        Touch.all_objects.create(
            user=student, contact=thick, ts=timezone.now(),
            kind="reply_received", channel="email", note=f"t{i}",
        )
    counts = merge_service._touch_counts(student, [thin.id, thick.id])
    assert merge_service._pick_primary(thin, thick, counts)[0].id == thick.id
    assert merge_service._pick_primary(thick, thin, counts)[0].id == thick.id


# ===========================================================================
# INVARIANT 2 — every answer suppresses the pair forever, in both id orders,
# whatever the answer was. Exhaustive over the three statuses x both orders.
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize("status", [ContactMerge.STATUS_MERGED,
                                    ContactMerge.STATUS_UNDONE,
                                    ContactMerge.STATUS_REJECTED])
@pytest.mark.parametrize("swap", [False, True])
def test_an_answered_pair_never_resurfaces(student, status, swap):
    a = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com")
    b = _contact(student, "Ebba Kler", "ebbakler@amazon.es")
    assert merge_service.candidate_pairs(student), "precondition: it is suggested"
    p, d = (b, a) if swap else (a, b)
    ContactMerge.all_objects.create(
        user=student, primary=p, duplicate=d, status=status, evidence="x"
    )
    assert merge_service.candidate_pairs(student) == []
    assert merge_service.suggestion_for(student, a.id, b.id) is None
    assert merge_service.suggestion_for(student, b.id, a.id) is None


@pytest.mark.django_db
def test_rejecting_is_idempotent_at_the_door_that_can_be_tapped(student):
    """`reject` itself writes a row every time it is called — it is a ledger
    append, not an upsert. What must be idempotent is the DOOR: the second tap
    finds no standing suggestion and so never reaches `reject` at all."""
    a = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com")
    b = _contact(student, "Ebba Kler", "ebbakler@amazon.es")
    cand = merge_service.suggestion_for(student, a.id, b.id)
    assert cand is not None
    merge_service.reject(student, cand.primary, cand.duplicate, cand.evidence)
    assert merge_service.suggestion_for(student, a.id, b.id) is None
    assert ContactMerge.objects.for_user(student).count() == 1


@pytest.mark.django_db
def test_merging_twice_is_impossible_through_the_re_derivation(student):
    a = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com")
    b = _contact(student, "Ebba Kler", "ebbakler@amazon.es")
    cand = merge_service.suggestion_for(student, a.id, b.id)
    merge_service.merge(student, cand.primary, cand.duplicate, cand.evidence)
    assert merge_service.suggestion_for(student, a.id, b.id) is None
    assert merge_service.candidate_pairs(student) == []


# ===========================================================================
# INVARIANT 3 — `suggestion_for` and `candidate_pairs` agree, for every pair
# in the population and in both argument orders. The POST re-derives; if the
# two disagreed, a card the user can see would refuse the tap (or worse, a
# pair no card ever showed would accept one).
# ===========================================================================
@pytest.mark.django_db
def test_suggestion_for_agrees_with_candidate_pairs_over_every_pair(student):
    rows = [_contact(student, name, email) for name, email in _DUP_POPULATION]
    suggested = {
        frozenset((c.primary.id, c.duplicate.id))
        for c in merge_service.candidate_pairs(student)
    }
    for a, b in itertools.combinations(rows, 2):
        for p, d in ((a, b), (b, a)):
            got = merge_service.suggestion_for(student, p.id, d.id)
            assert (got is not None) == (frozenset((a.id, b.id)) in suggested), (
                a.email, b.email
            )
            if got is not None:
                assert {got.primary.id, got.duplicate.id} == {a.id, b.id}
                assert got.evidence


# ===========================================================================
# INVARIANT 4 — merge/undo is a true round trip. Exhaustive over which of the
# fillable fields the primary has blank, because "fill blanks from the
# duplicate" is the half of the merge undo has to reverse selectively.
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize("blank", list(merge_service._FILLABLE_FIELDS))
def test_merge_then_undo_restores_the_board(student, blank):
    filled = {"role": "Analyst", "region": "us", "linkedin": "https://x.example/a",
              "school": "USC", "firm_text": "Amazon"}
    primary_kw = {k: v for k, v in filled.items() if k != blank}
    dup_kw = dict(filled)
    if blank == "firm_id":
        primary_kw = dict(filled)
        dup_kw = dict(filled)
    p = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com", **primary_kw)
    d = _contact(student, "Ebba Kler", "ebbakler@amazon.es", **dup_kw)
    t = Touch.all_objects.create(
        user=student, contact=d, ts=timezone.now(), kind="outreach",
        channel="email", note="hi",
    )
    before = {f: getattr(p, f) for f in merge_service._FILLABLE_FIELDS}
    before_notes, before_archived = p.notes, d.archived

    record = merge_service.merge(student, p, d, "evidence")
    d.refresh_from_db()
    assert d.archived is True
    t.refresh_from_db()
    assert t.contact_id == p.id

    assert merge_service.undo(record) is True
    p.refresh_from_db()
    d.refresh_from_db()
    t.refresh_from_db()
    assert {f: getattr(p, f) for f in merge_service._FILLABLE_FIELDS} == before
    assert (p.notes or "") == (before_notes or "")
    assert d.archived == before_archived
    assert t.contact_id == d.id
    record.refresh_from_db()
    assert record.status == ContactMerge.STATUS_UNDONE
    # Idempotent: a second undo reverses nothing and says so.
    assert merge_service.undo(record) is False


@pytest.mark.django_db
def test_undo_never_overwrites_a_value_the_user_changed_since(student):
    p = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com", role="")
    d = _contact(student, "Ebba Kler", "ebbakler@amazon.es", role="Account Manager")
    record = merge_service.merge(student, p, d, "e")
    p.refresh_from_db()
    assert p.role == "Account Manager"
    p.role = "AWS Account Manager"       # the user's own word, after the merge
    p.save(update_fields=["role"])
    merge_service.undo(record)
    p.refresh_from_db()
    assert p.role == "AWS Account Manager"


# ===========================================================================
# INVARIANT 5 — tenancy. Every query in this module is scoped, and a pair that
# spans two students is not a duplicate, it is a leak.
# ===========================================================================
@pytest.mark.django_db
def test_two_students_with_the_identical_duplicate_pair_never_see_each_other(student):
    other = User.objects.create_user(email="stress-merge-2@example.com", password="x")
    mine = [_contact(student, n, e) for n, e in _DUP_POPULATION[:2]]
    theirs = [_contact(other, n, e) for n, e in _DUP_POPULATION[:2]]

    for user, own in ((student, mine), (other, theirs)):
        for cand in merge_service.candidate_pairs(user):
            assert cand.primary.user_id == user.pk
            assert cand.duplicate.user_id == user.pk
            assert {cand.primary.id, cand.duplicate.id} <= {c.id for c in own}

    # The cross-tenant pair is identical evidence and must still be nothing.
    assert merge_service.suggestion_for(student, mine[0].id, theirs[1].id) is None
    assert merge_service.suggestion_for(other, theirs[0].id, mine[1].id) is None
    # And one student's answer never silences the other's card.
    ContactMerge.all_objects.create(
        user=student, primary=mine[0], duplicate=mine[1],
        status=ContactMerge.STATUS_REJECTED, evidence="x",
    )
    assert merge_service.candidate_pairs(student) == []
    assert merge_service.candidate_pairs(other)


@pytest.mark.django_db
def test_two_archived_rows_ask_no_question(student):
    a = _contact(student, "Ebba af Klercker", "ebbakler@amazon.com", archived=True)
    b = _contact(student, "Ebba Kler", "ebbakler@amazon.es", archived=True)
    assert merge_service.candidate_pairs(student) == []
    assert merge_service.suggestion_for(student, a.id, b.id) is None
    b.archived = False
    b.save(update_fields=["archived"])
    assert merge_service.candidate_pairs(student), "one live row still splits history"


# ===========================================================================
# `crm.sourcing` — HOLD THE LINE.
#
# The module's promise: "We hand over a query, not a person. Nothing is
# fetched, nothing is imported." These are the tests that make a future fetch
# or cache fail loudly instead of quietly shipping.
# ===========================================================================
_FORBIDDEN_IN_SOURCING = (
    "requests", "urlopen", "urllib.request", "http.client", "httpx", "aiohttp",
    "socket", "cache", "lru_cache", "fetch(", "session.get", "webdriver",
    "BeautifulSoup", "selenium",
)


def test_sourcing_carries_no_network_or_cache_machinery_at_all():
    """A source-level assertion on purpose. `urlencode` is the only thing this
    module is allowed to want from `urllib`, and a cached suggestion is a
    stored claim about people — which is the thing the disclosure says
    Coverage does not make."""
    src = inspect.getsource(sourcing)
    body = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    for token in _FORBIDDEN_IN_SOURCING:
        assert token not in body, f"crm.sourcing must not reach for {token!r}"
    assert sourcing.LINKEDIN_PEOPLE_SEARCH.startswith("https://www.linkedin.com/")


@pytest.mark.django_db
def test_suggestions_touch_the_database_exactly_zero_times(
    student, django_assert_num_queries
):
    student.tracks = ["ib", "st"]
    student.school = "USC"
    student.save(update_fields=["tracks", "school"])
    with django_assert_num_queries(0):
        rows = sourcing.suggestions_for({"name": "Goldman Sachs"}, student)
    assert rows


def test_every_generated_link_is_a_plain_linkedin_people_search():
    """Exhaustive over the whole reachable answer space: every track subset the
    round-robin can be given, with and without a school."""
    tracks = list(sourcing.TRACK_ARCHETYPES)
    subsets = [()] + [
        combo
        for size in range(1, len(tracks) + 1)
        for combo in itertools.combinations(tracks, size)
    ]
    for subset, school in itertools.product(subsets, ["", "USC", "S&P University"]):
        user = type("U", (), {"tracks": list(subset), "school": school})()
        for firm_name in ["Goldman Sachs", "Rothschild & Co", "S&P Global",
                          "Bank of America"]:
            rows = sourcing.suggestions_for({"name": firm_name}, user)
            assert len(rows) == sourcing.DEFAULT_LIMIT
            for r in rows:
                parsed = urlparse(r["linkedin_url"])
                assert parsed.scheme == "https"
                assert parsed.netloc == "www.linkedin.com"
                assert parsed.path == "/search/results/people/"
                assert parsed.query.startswith("keywords=")
                # The firm the student asked about is in every query, quoted,
                # so no row is a search about somebody else's employer.
                assert firm_name in r["query"]


def test_suggestions_are_pure_and_repeatable():
    """Called twice with the same inputs it returns the same rows — no
    memoisation, no counter, no hidden state to go stale."""
    user = type("U", (), {"tracks": ["st", "ib"], "school": "USC"})()
    first = sourcing.suggestions_for({"name": "UBS"}, user)
    second = sourcing.suggestions_for({"name": "UBS"}, user)
    assert first == second
    # ...and the analytics key of a row means the same seat whichever order
    # the student listed their tracks in.
    flipped = sourcing.suggestions_for(
        {"name": "UBS"}, type("U", (), {"tracks": ["ib", "st"], "school": "USC"})()
    )
    by_key = {r["key"]: r["label"] for r in first + flipped}
    assert by_key["st-0"] == sourcing.TRACK_ARCHETYPES["st"][0][0]
    assert by_key["ib-0"] == sourcing.TRACK_ARCHETYPES["ib"][0][0]


def test_no_suggestion_ever_claims_a_person_exists():
    """The disclosure is the product promise; the rows must not contradict it.
    A row names a SEAT and a search, never a person, so nothing in the answer
    may look like a name or an address."""
    user = type("U", (), {"tracks": ["ib"], "school": "USC"})()
    rows = sourcing.suggestions_for({"name": "Goldman Sachs"}, user)
    assert "Suggestions, not a list of people" in sourcing.DISCLOSURE
    for r in rows:
        assert "@" not in r["label"]
        assert "@" not in r["why"]
        assert set(r) == {"key", "label", "why", "query", "linkedin_url"}
