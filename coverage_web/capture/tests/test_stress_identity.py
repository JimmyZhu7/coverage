"""Adversarial invariant suite for the identity ladder (`capture.discovery`).

Companion to `capture/tests/test_identity.py`, which pins the ladder's
acceptance and refusal EXAMPLES. This file pins its PROPERTIES, and it exists
because five of the examples turned out to be true of the suggestive rung and
false of the conclusive one — the ladder disagreed with itself, and no example
test could see that because each rung was only ever asked about its own cases.

Same discipline as `crm/tests/test_stress_crm.py`: no `hypothesis`, so the
finite input spaces here (name-pair shapes x address-pair shapes, rung x
outcome, contact-row states x evidence kinds) are walked EXHAUSTIVELY, and the
one genuinely unbounded question — does the answer depend on the order rows
came back in? — uses a seeded shuffle so a counterexample reproduces.

THE ONE RULE EVERY INVARIANT HERE SERVES. A false merge fuses two people's
histories and has no clean undo once later touches land on the fused card; a
false split costs one duplicate card that `crm.merge` is built to offer back.
So the ladder may act on conclusive evidence, offer on suggestive evidence,
and must ABSTAIN on ambiguous evidence. Nothing may guess.

Most of this file needs no database — `names_equivalent`, `routing_variant`,
`duplicate_evidence` and `split_display_name` are pure by design. The
`django_db` marker appears only where the row population is the thing under
test.
"""

from __future__ import annotations

import itertools
import random
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from capture import discovery
from crm.models import Contact
from directory.models import Firm

SEED = 20260828

User = get_user_model()


def row(name, email="", firm_id=None):
    """A Contact-shaped stand-in, same helper as `test_identity.py`."""
    return SimpleNamespace(name=name, email=email, firm_id=firm_id)


@pytest.fixture
def student(db):
    return User.objects.create_user(email="stress-ident@example.com", password="x")


# ===========================================================================
# INVARIANT 1 — the conclusive rungs are never LOOSER than the suggestive one.
#
# `duplicate_evidence`'s docstring is the ladder's written policy: a shared
# employer never suffices, a namesake at another firm is two people, two-letter
# localparts prove nothing. `_match_existing` is supposed to be strictly
# stricter than that, because it ACTS where the other only offers. Two rungs
# were looser, both on pairs `test_identity.py` already asserts the suggestive
# rung refuses:
#
#   routing_variant("jl@bnpparibas.com", "jl@asia.bnpparibas.com") -> True
#     while test_two_letter_localparts_prove_nothing asserts "" from
#     duplicate_evidence on that exact pair. `jl@bnpparibas.com` is a live row.
#   _match_existing(u, "xiang.li@ubs.com", "Xiang Li") -> the cicc.com.cn row
#     while test_same_name_at_unrelated_firms_is_a_namesake asserts "".
# ===========================================================================
REFUSED_BY_THE_SUGGESTIVE_RUNG = [
    # (name_a, email_a, name_b, email_b, why)
    ("Jinghan L", "jl@bnpparibas.com", "Jinghan Liu", "jl@asia.bnpparibas.com",
     "two-letter localparts prove nothing"),
    ("Jane Doe", "jane.doe@hotmail.com", "Jane Doe", "jane.doe@hotmail.es",
     "freemail org labels never relate domains"),
    ("Patina Chu", "patina@amazon.com", "Patina Zhu", "patina@amazon.es",
     "contradicting names refuse even with the same mailbox"),
    ("Warren Zhang", "warren.zhang@clsa.com", "Yuxiang Zhang", "yuxiang.zhang@clsa.com",
     "a shared surname at one firm is never sufficient"),
]


@pytest.mark.parametrize(
    "name_a,email_a,name_b,email_b,why", REFUSED_BY_THE_SUGGESTIVE_RUNG
)
def test_an_address_pair_the_suggestive_rung_refuses_is_never_one_mailbox(
    name_a, email_a, name_b, email_b, why
):
    assert discovery.duplicate_evidence(
        row(name_a, email_a), row(name_b, email_b)
    ) == "", why
    assert not discovery.routing_variant(email_a, email_b), why


@pytest.mark.django_db
def test_the_two_letter_localpart_pair_is_no_longer_fused_by_the_ladder(student):
    """The sharpest of the four, given its own end-to-end test because
    `jl@bnpparibas.com` ("Jinghan L") is a LIVE row on the founder's board and
    the routing rung used to fuse it with any `jl@` at a bnpparibas subdomain —
    a different person's mailbox, at initials two people share easily."""
    c = Contact.all_objects.create(
        user=student, name="Jinghan L", email="jl@bnpparibas.com"
    )
    assert discovery._match_existing(student, "jl@asia.bnpparibas.com", "") is None
    # The long-localpart case it was written for is untouched.
    Contact.all_objects.create(
        user=student, name="Noah Bauld", email="noah.bauld@gs.com"
    )
    assert discovery._match_existing(
        student, "noah.bauld@ny.ibd.email.gs.com", ""
    ).email == "noah.bauld@gs.com"
    assert c.email == "jl@bnpparibas.com"


@pytest.mark.django_db
def test_a_namesake_at_another_directory_firm_is_never_matched(student):
    cicc = Firm.objects.create(name="CICC", slug="cicc-stress",
                               domains=["cicc.com.cn"])
    ubs = Firm.objects.create(name="UBS", slug="ubs-stress", domains=["ubs.com"])
    assert cicc.id != ubs.id
    Contact.all_objects.create(
        user=student, name="Xiang Li", email="xiang.li@cicc.com.cn"
    )
    # The suggestive rung calls this two people; the conclusive one must not
    # quietly disagree and fuse the reply onto the CICC card.
    assert discovery.duplicate_evidence(
        row("Xiang Li", "xiang.li@cicc.com.cn", firm_id=cicc.id),
        row("Xiang Li", "xiang.li@ubs.com", firm_id=ubs.id),
    ) == ""
    assert discovery._match_existing(student, "xiang.li@ubs.com", "Xiang Li") is None


@pytest.mark.django_db
def test_the_same_person_on_a_personal_address_still_matches(student):
    """The deliberate limit of the rule above, stated as a test so nobody
    tightens it into "unrelated domains never match". An alum replying from
    gmail is the commonest shape on a student's board, `duplicate_evidence`
    offers no card for employer-next-to-freemail, and refusing here would mint
    a duplicate with no path back."""
    Firm.objects.create(name="Jefferies", slug="jef-stress",
                        domains=["jefferies.com"])
    c = Contact.all_objects.create(
        user=student, name="Amy Zhou", email="amy.zhou@jefferies.com"
    )
    assert discovery._match_existing(student, "amyzhou@gmail.com", "Amy Zhou") == c


@pytest.mark.django_db
def test_the_known_limit_two_namesakes_on_personal_addresses_still_fuse(student):
    """A KNOWN, DELIBERATE LIMIT, written down so nobody has to rediscover it.

    Two different "Jane Doe"s, both on freemail, are still read as one person:
    the name rung's only sanctioned contradiction is two DIFFERENT directory
    firms, and neither hotmail address names a firm. The alternative — refusing
    every same-name pair at unrelated domains — would break the test above,
    and `duplicate_evidence` offers no card for an employer-next-to-personal
    pair, so the duplicate it created would have no path back.

    What bounds the damage: the moment BOTH namesakes are on the board, the
    ambiguity abstain fires and the ladder stops answering at all."""
    a = Contact.all_objects.create(
        user=student, name="Jane Doe", email="jane.doe@hotmail.com"
    )
    assert discovery._match_existing(student, "jane.doe@hotmail.es", "Jane Doe") == a
    Contact.all_objects.create(
        user=student, name="Jane Doe", email="jane.doe@hotmail.es"
    )
    assert discovery._match_existing(
        student, "jane.doe@yahoo.example", "Jane Doe"
    ) is None


# ===========================================================================
# INVARIANT 2 — ambiguity abstains, at every rung, for every population size.
#
# The old code ended each weak rung in `next(...)`, so N rows answering to one
# name resolved to whichever the queryset yielded first — under
# `Contact.Meta.ordering = ["-created"]`, the newest. `capture.gmail`'s matcher
# names that exact behaviour as the bug it raises `AmbiguousContactError` to
# avoid; this one, which every proposal door uses, still did it.
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize("count", [2, 3, 4])
def test_n_rows_sharing_one_name_always_abstain(student, count):
    for i in range(count):
        Contact.all_objects.create(
            user=student, name="Michael Chen", email=f"m.chen{i}@firm{i}.example"
        )
    assert discovery._match_existing(
        student, "michael.chen@elsewhere.example", "Michael Chen"
    ) is None
    assert discovery._match_existing(student, "", "Chen, Michael") is None


@pytest.mark.django_db
def test_one_row_of_the_many_still_wins_on_the_exact_address(student):
    """Abstaining is about the WEAK rungs. The strong key still decides — an
    ambiguous name must not blind the matcher to an exact address hit."""
    for i in range(3):
        Contact.all_objects.create(
            user=student, name="Michael Chen", email=f"m.chen{i}@firm{i}.example"
        )
    hit = discovery._match_existing(student, "m.chen1@firm1.example", "Michael Chen")
    assert hit is not None and hit.email == "m.chen1@firm1.example"


@pytest.mark.django_db
def test_two_routing_forms_of_one_localpart_abstain(student):
    """`ny.ibd.email.gs.com` is an internal extension of BOTH `gs.com` and
    `email.gs.com`, so two rows answer the routing rung and neither is more
    right than the other."""
    Contact.all_objects.create(
        user=student, name="Noah Bauld", email="noah.bauld@gs.com"
    )
    Contact.all_objects.create(
        user=student, name="Noah Bauld", email="noah.bauld@email.gs.com"
    )
    assert discovery.routing_variant(
        "noah.bauld@ny.ibd.email.gs.com", "noah.bauld@gs.com"
    )
    assert discovery.routing_variant(
        "noah.bauld@ny.ibd.email.gs.com", "noah.bauld@email.gs.com"
    )
    assert discovery._match_existing(
        student, "noah.bauld@ny.ibd.email.gs.com", ""
    ) is None


# ===========================================================================
# INVARIANT 3 — the answer never depends on the order rows come back in.
#
# Seeded, because "does this depend on queryset order" is the one unbounded
# question in this file. Creation order is what `-created` ordering keys off,
# so shuffling creation order is shuffling the queryset.
# ===========================================================================
@pytest.mark.django_db
def test_match_existing_is_invariant_under_row_insertion_order(student):
    people = [
        ("Michael Chen", "mchen@gs.com"),
        ("Michael Chen", "michael.chen@jpmorgan.com"),
        ("Amy Zhou", "amy.zhou@jefferies.com"),
        ("Matt", "matt@nummo.com"),
        ("Noah Bauld", "noah.bauld@gs.com"),
        ("Ebba af Klercker", "ebbakler@amazon.com"),
    ]
    probes = [
        ("m.chen@citi.com", "Michael Chen"),
        ("amyzhou@gmail.com", "Amy Zhou"),
        ("matt@othershop.example", "Matt"),
        ("noah.bauld@ny.ibd.email.gs.com", ""),
        ("ebbakler@amazon.es", "Ebba Kler"),
        ("", "Chen, Michael"),
    ]
    rng = random.Random(SEED)
    baseline = None
    for _ in range(12):
        Contact.all_objects.filter(user=student).delete()
        order = people[:]
        rng.shuffle(order)
        for name, email in order:
            Contact.all_objects.create(user=student, name=name, email=email)
        got = tuple(
            (lambda m: m and m.email)(discovery._match_existing(student, e, n))
            for e, n in probes
        )
        if baseline is None:
            baseline = got
        assert got == baseline, f"order-dependent answer with order {order}"


# ===========================================================================
# INVARIANT 4 — `names_equivalent` is a symmetric equivalence over FULL names
# only, and one word is never a full name.
#
# `names_equivalent("Kevin", "Kevin")` used to be True. The founder's live
# board carries five one-word cards, and `consider_finding` falls back to the
# mailbox localpart when a sender has no display name, so a second Matt writing
# in from any address would have landed on the first Matt's card.
# ===========================================================================
LIVE_MONONYMS = ["Matt", "Diego", "Kirthi", "Daksh", "Alexis"]


@pytest.mark.parametrize("name", LIVE_MONONYMS)
def test_one_word_names_never_identify_anybody(name):
    assert not discovery.names_equivalent(name, name)
    assert not discovery.names_equivalent(name, name.lower())
    assert not discovery.names_equivalent(name, f"{name} Wu")


def test_names_equivalent_is_symmetric_over_the_whole_shape_space():
    shapes = [
        "", "Matt", "matt", "Matt R", "Matt Rowe", "Rowe, Matt", "Rowe, Matt R",
        "Matt R. Rowe", "Matt A Rowe", "Matt B Rowe", "matt  rowe", "Mat Rowe",
        "José García", "Jose Garcia", "af Klercker, Ebba", "Ebba af Klercker",
        "Ebba Kler", "A B", "Li Wei", "Wei Li", "jl", "jinghan.liu",
    ]
    for a, b in itertools.product(shapes, repeat=2):
        assert discovery.names_equivalent(a, b) == discovery.names_equivalent(b, a), (a, b)


_SHAPE_SPACE = [
    "Matt Rowe", "Rowe, Matt", "Matt R Rowe", "Matt R. Rowe", "matt  rowe",
    "Vanessa Nunley", "Nunley, Vanessa N", "Vanessa A Nunley",
    "Jose Garcia", "José García", "Garcia, Jose",
]


def test_the_only_break_in_transitivity_is_the_documented_initials_rule():
    """`names_equivalent` is NOT transitive, and cannot be made so without
    giving up something load-bearing. "Nunley, Vanessa N" ~ "Vanessa Nunley"
    (an initial may be dropped) and "Vanessa Nunley" ~ "Vanessa A Nunley" (an
    initial may be added), but the ends disagree — N is not A — and the rule
    that two stated initials must AGREE is the whole reason the middle rung is
    safe. Making it transitive would mean either refusing every dropped
    initial (breaking a documented, evidenced case) or accepting every
    disagreeing one (fusing two people).

    So the property worth pinning is not transitivity, it is that transitivity
    breaks for exactly ONE reason. Any other break would be an accident of the
    tokenizer, and this test is what would catch it.

    The concrete danger the break leaves — two people both fusing onto one
    middle row — is what the ambiguity abstain covers; see
    `test_the_initials_gap_is_covered_by_the_ambiguity_abstain`."""
    for a, b, c in itertools.product(_SHAPE_SPACE, repeat=3):
        if not (discovery.names_equivalent(a, b) and discovery.names_equivalent(b, c)):
            continue
        if discovery.names_equivalent(a, c):
            continue
        initials_a = sorted(t for t in discovery._name_tokens(a) if len(t) == 1)
        initials_c = sorted(t for t in discovery._name_tokens(c) if len(t) == 1)
        assert initials_a and initials_c and initials_a != initials_c, (
            f"{a!r} ~ {b!r} ~ {c!r} breaks transitivity for a reason that is "
            f"not the disagreeing-initials rule"
        )


@pytest.mark.django_db
def test_the_initials_gap_is_covered_by_the_ambiguity_abstain(student):
    """Both Vanessas on the board at once is the case where the broken
    transitivity above could fuse the wrong pair. It cannot: two rows answer
    to "Vanessa Nunley", so the matcher abstains and the user is asked."""
    Contact.all_objects.create(
        user=student, name="Vanessa A Nunley", email="va.nunley@gs.example"
    )
    Contact.all_objects.create(
        user=student, name="Vanessa N Nunley", email="vn.nunley@gs.example"
    )
    assert discovery._match_existing(
        student, "vanessa.nunley@elsewhere.example", "Vanessa Nunley"
    ) is None


# ===========================================================================
# INVARIANT 5 — `routing_variant` relates one mailbox to itself, never two
# mailboxes to each other. Exhaustive over a domain cross-product.
# ===========================================================================
_ROUTING_DOMAINS = [
    "gs.com", "ny.ibd.email.gs.com", "ibd.gs.com",
    "notgs.com", "gs.com.evil.example", "gmail.com", "mail.gmail.com",
    "bnpparibas.com", "asia.bnpparibas.com",
]


@pytest.mark.parametrize("localpart", ["noah.bauld", "jl", "slu", "recruiting",
                                       "no-reply", "careers", "abcd"])
def test_routing_variant_is_symmetric_and_never_relates_unrelated_domains(localpart):
    for da, db in itertools.product(_ROUTING_DOMAINS, repeat=2):
        a, b = f"{localpart}@{da}", f"{localpart}@{db}"
        assert discovery.routing_variant(a, b) == discovery.routing_variant(b, a)
        if not discovery.routing_variant(a, b):
            continue
        # Whatever it DID relate must satisfy every documented precondition.
        assert da != db
        assert da.endswith("." + db) or db.endswith("." + da)
        assert discovery._personal_localpart(a), localpart
        assert discovery._org_label(da) not in discovery._FREEMAIL_ORG_LABELS
        assert discovery._org_label(db) not in discovery._FREEMAIL_ORG_LABELS


def test_routing_variant_never_relates_two_different_mailboxes():
    for la, lb in itertools.product(["noah.bauld", "amy.zhou", "mho", "dou"], repeat=2):
        if la == lb:
            continue
        for da, db in itertools.product(_ROUTING_DOMAINS, repeat=2):
            assert not discovery.routing_variant(f"{la}@{da}", f"{lb}@{db}")


# ===========================================================================
# INVARIANT 6 — `duplicate_evidence` only ever OFFERS, and its truthiness is
# symmetric: which row the scan happened to reach first cannot decide whether
# the user is asked at all.
# ===========================================================================
_DUP_ROWS = [
    ("Ebba af Klercker", "ebbakler@amazon.com", 7),
    ("Ebba Kler", "ebbakler@amazon.es", 7),
    ("Ebba Kler", "ebbakler@amazon.es", None),
    ("John Smith", "john.smith@gs.com", None),
    ("John Smith", "j.smith@gs.com", None),
    ("John Smith", "john.smith@jpmorgan.com", 2),
    ("John Smith", "", None),
    ("Jane Doe", "jane.doe@gmail.com", None),
    ("Jane Doe", "j.doe@gmail.com", None),
    ("Warren Zhang", "warren.zhang@clsa.com", 3),
    ("Yuxiang Zhang", "yuxiang.zhang@clsa.com", 3),
    ("Matt", "matt@nummo.com", None),
    ("Matt", "matt@other.example", None),
]


def test_duplicate_evidence_truthiness_is_symmetric():
    for a, b in itertools.product(_DUP_ROWS, repeat=2):
        ra, rb = row(*a), row(*b)
        assert bool(discovery.duplicate_evidence(ra, rb)) == bool(
            discovery.duplicate_evidence(rb, ra)
        ), (a, b)


def test_duplicate_evidence_never_fires_on_two_freemail_mailboxes():
    """Two "Jane Doe"s at gmail.com are two mailboxes at one provider. The
    same-employer-domain rung must not read a mail PROVIDER as an employer."""
    assert discovery.duplicate_evidence(
        row("Jane Doe", "jane.doe@gmail.com"), row("Jane Doe", "j.doe@gmail.com")
    ) == ""


def test_one_name_at_one_employer_domain_is_offered_even_without_a_firm_link():
    """The hole this closed: relatedness was computed only for two DIFFERENT
    domains, so the most obvious duplicate shape there is — one name, two
    addresses at one employer — produced nothing unless both rows also carried
    the same `firm_id`. A CSV import or hand-add leaves that FK NULL."""
    ev = discovery.duplicate_evidence(
        row("John Smith", "john.smith@gs.com"), row("John Smith", "j.smith@gs.com")
    )
    assert ev and "gs.com" in ev


def test_duplicate_evidence_never_returns_a_contact():
    """It words an offer. It cannot be mistaken for a match by a caller that
    checks truthiness — the return type is always a string."""
    for a, b in itertools.product(_DUP_ROWS, repeat=2):
        assert isinstance(discovery.duplicate_evidence(row(*a), row(*b)), str)


# ===========================================================================
# INVARIANT 7 — `split_display_name` never invents name text.
#
# Every word of the name it returns must be a word the sender actually typed.
# The address is corroboration for WHICH READING is right; it is never a source
# of characters.
# ===========================================================================
_DISPLAY_FORMS = [
    "", "Jane Doe", "Jane Doe, Campus Recruiting", "Doe, Jane",
    "Doe, Jane (Campus Recruiting)", "Jane Doe (she/her) - Talent Acquisition",
    "Liu , Lily : International Corporate Banking", "Nunley, Vanessa N",
    "af Klercker, Ebba", "Smith, John, CFA", "Jimmy Zhu | Goldman Sachs",
    "Mary-Jane O'Connor", "Hwang, Christine J", "lily.liu",
]
_ADDRESSES = [
    "", "jane.doe@gs.com", "lily.liu@barclays.com", "vanessa.nunley@gs.com",
    "vnunley@gs.com", "christine.j.hwang@citi.com", "john.smith@gs.com",
    "campus@gs.com", "ebbakler@amazon.com",
]


def test_split_display_name_only_ever_returns_words_the_sender_typed():
    for raw, email in itertools.product(_DISPLAY_FORMS, _ADDRESSES):
        name, hint = discovery.split_display_name(raw, email=email)
        typed = set(discovery._name_tokens(raw))
        for token in discovery._name_tokens(name):
            assert token in typed, (raw, email, name)
        assert len(hint) <= 255


def test_split_display_name_without_an_address_is_exactly_what_it_always_was():
    """The corroboration is additive. No address, no change — so every caller
    that cannot supply one keeps the behaviour its tests pin."""
    for raw in _DISPLAY_FORMS:
        assert discovery.split_display_name(raw) == discovery.split_display_name(
            raw, email=""
        )


def test_the_barclays_address_book_form_names_the_person_not_the_surname():
    """Live, read-only, 2026-08-28: Barclays renders one of the founder's real
    contacts as `Liu , Lily : International Corporate Banking`. That used to
    become the name "Liu" with the role "Lily : International Corporate
    Banking" — and, matching no full name, a second card for somebody already
    on his board."""
    assert discovery.split_display_name(
        "Liu , Lily : International Corporate Banking",
        email="lily.liu@barclays.com",
    ) == ("Lily Liu", "International Corporate Banking")
    assert discovery.names_equivalent("Lily Liu", "Lily Liu")


# (localpart, whether it corroborates "Hoffmann, Peter" as one person)
# Every real convention on the founder's live board, plus the near misses that
# must keep failing. Measured read-only 2026-08-28: 47 of his 182 live rows
# spell the surname with only the given name's INITIAL, which no word-set test
# could ever confirm — hence the run-together forms.
_ADDRESS_CONVENTIONS = [
    ("peter.hoffmann", True),    # first.last
    ("hoffmann.peter", True),    # last.first
    ("peter_hoffmann", True),
    ("phoffmann", True),         # <initial><surname>, 26% of the real board
    ("hoffmannp", True),
    ("peterhoffmann", True),
    ("hoffmannpeter", True),
    ("phoffmann2", True),        # trailing disambiguation digit
    ("hoffmann", False),         # surname only: says nothing about "Peter"
    ("bhoffmann", False),        # a different initial is a different person
    ("phoff", False),            # truncated: which Hoff is not ours to invent
    ("peter", False),
    ("campus", False),
    ("", False),
]


@pytest.mark.parametrize("localpart,corroborates", _ADDRESS_CONVENTIONS)
def test_only_an_exact_address_convention_confirms_an_inverted_name(
    localpart, corroborates
):
    got = discovery.split_display_name(
        "Hoffmann, Peter", email=f"{localpart}@williamblair.example"
    )
    assert got == (("Peter Hoffmann", "") if corroborates else ("Hoffmann", "Peter"))


def test_a_role_after_a_comma_is_still_a_role():
    """The other half of the same string shape. Nothing in the TEXT separates
    "Nunley, Vanessa N" from "Jane Doe, Campus Recruiting"; the address is what
    separates them, and when it does not corroborate, the role reading wins."""
    assert discovery.split_display_name(
        "Jane Doe, Campus Recruiting", email="jane.doe@gs.com"
    ) == ("Jane Doe", "Campus Recruiting")
    assert discovery.split_display_name(
        "Jane Doe, Campus Recruiting", email="campus@gs.com"
    ) == ("Jane Doe", "Campus Recruiting")
    assert discovery.split_display_name(
        "Doe, Jane", email="someone.else@gs.com"
    ) == ("Doe", "Jane")


# ===========================================================================
# INVARIANT 8 — tenancy. No rung of the ladder can see another student's rows,
# whatever the evidence, and a scoped miss is never an unscoped hit.
# ===========================================================================
@pytest.mark.django_db
def test_no_rung_ever_reaches_another_students_contacts(student):
    other = User.objects.create_user(email="stress-other@example.com", password="x")
    for name, email in [
        ("Noah Bauld", "noah.bauld@gs.com"),
        ("Michael Chen", "mchen@jpmorgan.com"),
        ("Ebba af Klercker", "ebbakler@amazon.com"),
    ]:
        Contact.all_objects.create(user=other, name=name, email=email)
    for email, name in [
        ("noah.bauld@gs.com", "Noah Bauld"),
        ("noah.bauld@ny.ibd.email.gs.com", ""),
        ("", "Chen, Michael"),
        ("ebbakler@amazon.com", ""),
    ]:
        assert discovery._match_existing(student, email, name) is None
    # And the other student's own lookups still work, so the scoping is a
    # filter and not an accident of an empty table.
    assert discovery._match_existing(other, "noah.bauld@gs.com", "") is not None


# ===========================================================================
# INVARIANT 9 — the proposal door. Exhaustive over (existing row state) x
# (what the ladder decides), asserting the outcome is always one of the four
# documented ones and never a silent write to a contact.
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize(
    "sender_name,sender_email",
    [
        ("Michael Chen", "michael.chen@jpmorgan.example"),   # namesake, other firm
        ("Chen, Michael", "michael.chen@jpmorgan.example"),  # inverted namesake
        ("Somebody Else", "someone@jpmorgan.example"),       # a stranger
    ],
)
def test_the_proposal_door_never_writes_to_a_contact(
    student, archived, sender_name, sender_email
):
    from capture.models import ContactProposal

    existing = Contact.all_objects.create(
        user=student, name="Michael Chen", email="mchen@gs.example",
        archived=archived,
    )
    before = (existing.name, existing.email, existing.role, existing.notes,
              existing.archived)
    outcome = discovery.consider_finding(
        student,
        {"email": sender_email, "name": sender_name, "replied": True,
         "threaded_reply": True, "subject": "Re: coffee chat"},
    )
    assert outcome in (discovery.PROPOSED, discovery.ARCHIVED_MATCH,
                       discovery.UPGRADED, None)
    existing.refresh_from_db()
    assert (existing.name, existing.email, existing.role, existing.notes,
            existing.archived) == before
    assert Contact.objects.for_user(student).count() == 1
    assert ContactProposal.objects.for_user(student).count() <= 1
