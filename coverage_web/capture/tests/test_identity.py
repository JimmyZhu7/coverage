"""The identity ladder (capture/discovery.py): `_match_existing`'s three
conclusive rungs and `duplicate_evidence`'s suggestive one.

The centre of gravity is rule 1 of the design: a false merge is far worse
than a false split. So the refusals get as many tests as the matches —
same firm never suffices, a shared surname at one firm never suffices, a
truncated name alone never auto-matches, and everything suggestive only
ever words an offer, never returns a Contact.

The Ebba shape (one AWS account manager as two rows: ebbakler@amazon.com
"Ebba af Klercker" and ebbakler@amazon.es "Ebba Kler" — live rows 707/706
on the founder's board) is the acceptance case for the suggestive rung, and
the corporate `Last, First` display forms and Goldman's routing subdomain
(`Noah.Bauld@ny.ibd.email.gs.com` for noah.bauld@gs.com) are the acceptance
cases for the conclusive ones.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from capture import discovery
from crm.models import Contact
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return User.objects.create_user(email="ident-student@example.com", password="x")


def row(name, email="", firm_id=None):
    """A Contact-shaped stand-in for the pure-function tests."""
    return SimpleNamespace(name=name, email=email, firm_id=firm_id)


# --------------------------------------------------------------------------- #
# names_equivalent — the conclusive name rung
# --------------------------------------------------------------------------- #

class TestNamesEquivalent:
    def test_plain_equality_still_holds(self):
        assert discovery.names_equivalent("Warren Zhang", "warren  zhang")

    def test_last_comma_first_inversion(self):
        assert discovery.names_equivalent("af Klercker, Ebba", "Ebba af Klercker")
        assert discovery.names_equivalent("Zhu, Leily", "Leily Zhu")

    def test_middle_initial_dropped_or_added(self):
        assert discovery.names_equivalent("Nunley, Vanessa N", "Vanessa Nunley")
        assert discovery.names_equivalent("Vanessa N. Nunley", "Vanessa Nunley")

    def test_disagreeing_initials_are_two_people(self):
        assert not discovery.names_equivalent("Vanessa A Nunley", "Vanessa B Nunley")

    def test_initial_never_expands_into_a_word(self):
        # Which Liu/Lau/Lee the L was is exactly the guess this refuses.
        assert not discovery.names_equivalent("Jinghan L", "Jinghan Liu")

    def test_bare_first_name_never_claims_a_fuller_name(self):
        assert not discovery.names_equivalent("Matt", "Matt R")
        assert not discovery.names_equivalent("Alexis", "Alexis Lu")

    def test_truncated_surname_is_not_equivalent(self):
        # Suggestive at best (see duplicate_evidence) — never conclusive.
        assert not discovery.names_equivalent("Ebba Kler", "Ebba af Klercker")

    def test_different_people_at_one_firm_never_equivalent(self):
        assert not discovery.names_equivalent("Warren Zhang", "Yuxiang Zhang")

    def test_diacritics_fold(self):
        assert discovery.names_equivalent("José García", "Jose Garcia")

    def test_empty_never_matches(self):
        assert not discovery.names_equivalent("", "")
        assert not discovery.names_equivalent("Warren Zhang", "")


# --------------------------------------------------------------------------- #
# _match_existing — the one conclusive opinion
# --------------------------------------------------------------------------- #

class TestMatchExisting:
    def test_exact_email_first(self, student):
        c = Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        assert discovery._match_existing(
            student, "Noah.Bauld@gs.com", "Somebody Else"
        ) == c

    def test_routing_subdomain_matches_the_canonical_address(self, student):
        c = Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        assert discovery._match_existing(
            student, "Noah.Bauld@ny.ibd.email.gs.com", ""
        ) == c

    def test_routing_works_in_both_directions(self, student):
        # The founder's own board stores the ROUTING form as canonical
        # (yuan.li@ny.email.gs.com, live row 310) — mail from the bare
        # domain must still be the same person.
        c = Contact.all_objects.create(
            user=student, name="Yuan Li", email="yuan.li@ny.email.gs.com"
        )
        assert discovery._match_existing(student, "yuan.li@gs.com", "") == c

    def test_same_localpart_at_an_unrelated_domain_is_not_routing(self, student):
        Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        assert discovery._match_existing(
            student, "noah.bauld@otherbank.example", ""
        ) is None

    def test_role_localpart_never_rideses_the_routing_rung(self, student):
        Contact.all_objects.create(
            user=student, name="Recruiting Desk", email="recruiting@gs.com"
        )
        assert discovery._match_existing(
            student, "recruiting@campus.gs.com", ""
        ) is None

    def test_inverted_display_name_matches(self, student):
        c = Contact.all_objects.create(
            user=student, name="Ebba af Klercker", email="ebbakler@amazon.com"
        )
        assert discovery._match_existing(
            student, "someone.new@nowhere.example", "af Klercker, Ebba"
        ) == c

    def test_archived_rows_are_still_matched(self, student):
        c = Contact.all_objects.create(
            user=student, name="Leily Zhu", email="leilyzhu@kpmg.com",
            archived=True,
        )
        assert discovery._match_existing(student, "", "Zhu, Leily") == c
        assert discovery._match_existing(student, "leilyzhu@kpmg.com", "") == c

    def test_truncated_name_never_auto_matches(self, student):
        # The Ebba shape: different TLD, truncated display name. Conclusive
        # rungs all refuse — this pair is duplicate_evidence's job.
        Contact.all_objects.create(
            user=student, name="Ebba af Klercker", email="ebbakler@amazon.com"
        )
        assert discovery._match_existing(
            student, "ebbakler@amazon.es", "Ebba Kler"
        ) is None

    def test_other_users_rows_are_invisible(self, student):
        other = User.objects.create_user(email="ident-other@example.com", password="x")
        Contact.all_objects.create(
            user=other, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        assert discovery._match_existing(student, "noah.bauld@gs.com", "Noah Bauld") is None


# --------------------------------------------------------------------------- #
# duplicate_evidence — the suggestive rung (offers only, never matches)
# --------------------------------------------------------------------------- #

class TestDuplicateEvidence:
    def test_the_ebba_shape_is_suggested(self):
        ev = discovery.duplicate_evidence(
            row("Ebba af Klercker", "ebbakler@amazon.com", firm_id=7),
            row("Ebba Kler", "ebbakler@amazon.es", firm_id=7),
        )
        assert ev
        assert "ebbakler" in ev

    def test_the_ebba_shape_without_firm_links_still_suggests(self):
        # amazon.com / amazon.es share the org label even when neither row
        # is linked to a directory firm.
        assert discovery.duplicate_evidence(
            row("Ebba af Klercker", "ebbakler@amazon.com"),
            row("Ebba Kler", "ebbakler@amazon.es"),
        )

    def test_same_firm_alone_is_never_sufficient(self):
        assert discovery.duplicate_evidence(
            row("Warren Zhang", "warren.zhang@clsa.com", firm_id=3),
            row("Yu Xie", "yu.xie@clsa.com", firm_id=3),
        ) == ""

    def test_shared_surname_at_one_firm_is_never_sufficient(self):
        assert discovery.duplicate_evidence(
            row("Warren Zhang", "warren.zhang@clsa.com", firm_id=3),
            row("Yuxiang Zhang", "yuxiang.zhang@clsa.com", firm_id=3),
        ) == ""

    def test_contradicting_names_refuse_even_with_same_mailbox(self):
        # Same localpart at related domains, but the names disagree:
        # 'chu' is not a shortening of 'zhu', and no edit-distance guess
        # gets a say.
        assert discovery.duplicate_evidence(
            row("Patina Chu", "patina@amazon.com"),
            row("Patina Zhu", "patina@amazon.es"),
        ) == ""

    def test_freemail_org_labels_never_relate_domains(self):
        # hotmail.com and hotmail.es are separately registered mailboxes.
        assert discovery.duplicate_evidence(
            row("Jane Doe", "jane.doe@hotmail.com"),
            row("Jane Doe", "jane.doe@hotmail.es"),
        ) == ""

    def test_two_letter_localparts_prove_nothing(self):
        assert discovery.duplicate_evidence(
            row("Jinghan L", "jl@bnpparibas.com"),
            row("Jinghan Liu", "jl@asia.bnpparibas.com"),
        ) == ""

    def test_same_name_at_unrelated_firms_is_a_namesake(self):
        assert discovery.duplicate_evidence(
            row("Xiang Li", "xiang.li@cicc.com.cn", firm_id=1),
            row("Xiang Li", "xiang.li@ubs.com", firm_id=2),
        ) == ""

    def test_same_name_where_one_row_has_no_address_is_suggested(self):
        # The import shape: a hand-added row without an email next to a
        # captured row with one.
        assert discovery.duplicate_evidence(
            row("Alvan Tay", ""),
            row("Alvan Tay", "alvan.tay@evercore.com", firm_id=4),
        )

    def test_same_name_at_the_same_firm_is_suggested_not_matched(self):
        ev = discovery.duplicate_evidence(
            row("Yuan Li", "yuan.li@gs.com", firm_id=9),
            row("Yuan Li", "yuan.li2@gs.com", firm_id=9),
        )
        assert ev  # offered for a tap; _match_existing would never fuse them
