"""The two duplicate-suppression mechanisms in `directory.dupes`.

Every URL below is a real one, copied off the live table (or, for the pairs,
off the two live rows that were the same posting). The point of using real
shapes is that these tests fail if a regex is tightened past the data it was
written for — a synthetic `https://x.tal.net/opp/1` would keep passing while
the actual boards stopped matching.

The negative cases matter more than the positive ones here. Three of them
(Oaktree, Huatai, and the differing-deadline pair) encode false positives that
this fix was specifically written to avoid, and each one is a shape an earlier
attempt at the same fix got wrong.
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone

import pytest

from coverage_connectors import FetchResult, TalnetBoard
from coverage_connectors.models import Opportunity as ConnOpp

from directory import ingest
from directory.dupes import (
    _title_bag,
    duplicate_key,
    fold_duplicates,
    identity_fragment,
    normalize_label,
    provider_identity,
)
from directory.models import Firm, Opportunity


# =========================================================== Class A: identity

class TestTalnetIdentity:
    """One posting listed in two candidate pools. Live-probed 2026-08-14:
    both urls serve the same posting, and neither can be rewritten to a
    pool-free form that resolves."""

    BOFA_POOL_1 = ("https://bankcampuscareers.tal.net/vx/lang-en-GB/mobile-0/brand-4"
                   "/candidate/so/pm/1/pl/1/opp/14594-Bank-of-America-Campus-Insight"
                   "-Forum-The-Power-to-Lead-Fall-2026/en-GB")
    BOFA_POOL_2 = ("https://bankcampuscareers.tal.net/vx/lang-en-GB/mobile-0/brand-4"
                   "/candidate/so/pm/1/pl/2/opp/14594-Bank-of-America-Campus-Insight"
                   "-Forum-The-Power-to-Lead-Fall-2026/en-GB")
    # Evercore varies the BRAND as well as the pool for one posting.
    EVERCORE_B5 = ("https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1"
                   "/appcentre-1/brand-5/candidate/so/pm/1/pl/3/opp/3145-Paris-Off"
                   "-Cycle-Internship-Telecoms-Team-January-June-2027/en-GB")
    EVERCORE_B6 = ("https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1"
                   "/appcentre-1/brand-6/candidate/so/pm/1/pl/2/opp/3145-Paris-Off"
                   "-Cycle-Internship-Telecoms-Team-January-June-2027/en-GB")
    # A DIFFERENT Evercore posting that also appears under two brands. Its opp
    # ids genuinely differ, so it must NOT collapse.
    EVERCORE_ASIA_3113 = ("https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1"
                          "/appcentre-1/brand-5/candidate/so/pm/1/pl/3/opp/3113"
                          "-Experienced-Analyst-Asia-Cross-Border-Advisory/en-GB")
    EVERCORE_ASIA_3128 = ("https://evercore.tal.net/vx/lang-en-GB/mobile-0/channel-1"
                          "/appcentre-1/brand-6/candidate/so/pm/1/pl/2/opp/3128"
                          "-Experienced-Analyst-Asia-Cross-Border-Advisory/en-GB")

    def test_pool_segment_does_not_change_identity(self):
        assert provider_identity(self.BOFA_POOL_1) == provider_identity(self.BOFA_POOL_2)

    def test_brand_and_pool_both_ignored(self):
        assert provider_identity(self.EVERCORE_B5) == provider_identity(self.EVERCORE_B6)

    def test_different_opp_ids_stay_different(self):
        """Same firm, same title, two brands — but two real postings."""
        assert (provider_identity(self.EVERCORE_ASIA_3113)
                != provider_identity(self.EVERCORE_ASIA_3128))

    def test_different_hosts_stay_different(self):
        other = self.BOFA_POOL_1.replace("bankcampuscareers", "morganstanley")
        assert provider_identity(other) != provider_identity(self.BOFA_POOL_1)

    def test_fragment_narrows_to_the_opp_id(self):
        assert identity_fragment(provider_identity(self.BOFA_POOL_1)) == "/opp/14594"


class TestIcimsIdentity:
    """SIG job 11260 was retitled; the slug is generated from the title, so the
    rename minted a second row and closed the first."""

    BEFORE = "https://careers-sig.icims.com/jobs/11260/operations-analyst%2c-hong-kong/job"
    AFTER = ("https://careers-sig.icims.com/jobs/11260/business-operations-analyst"
             "-%28middle-back-office%29%2c-hong-kong/job")

    def test_slug_does_not_change_identity(self):
        assert provider_identity(self.BEFORE) == provider_identity(self.AFTER)

    def test_different_job_numbers_stay_different(self):
        """SIG posts the same internship under two iCIMS numbers. Those are two
        requisitions with independent lifecycles — Class B, not Class A."""
        a = "https://careers-sig.icims.com/jobs/10849/quantitative-trader-internship/job"
        b = "https://careers-sig.icims.com/jobs/10718/quantitative-trader-internship/job"
        assert provider_identity(a) != provider_identity(b)


class TestWorkdayIdentity:
    """Workday picks one city for a multi-city posting's url, and that choice
    drifts between scrapes."""

    BLACKSTONE_OLD = ("https://blackstone.wd1.myworkdayjobs.com/Blackstone_Campus_Careers"
                      "/job/Berkeley-Square-House-London/XMLNAME-2027-Blackstone-Credit"
                      "-and-Insurance--Infrastructure-and-Asset-Based-Credit-Summer"
                      "-Analyst--London-_43862")
    BLACKSTONE_NEW = ("https://blackstone.wd1.myworkdayjobs.com/Blackstone_Campus_Careers"
                      "/job/London/XMLNAME-2027-Blackstone-Credit-and-Insurance"
                      "--Infrastructure-and-Asset-Based-Credit-Summer-Analyst--London-_43862")

    def test_location_segment_does_not_change_identity(self):
        assert (provider_identity(self.BLACKSTONE_OLD)
                == provider_identity(self.BLACKSTONE_NEW))

    def test_oaktree_year_prefixed_reqs_stay_distinct(self):
        """THE REGRESSION THIS FIX EXISTS TO AVOID.

        Oaktree's requisition ids are natively `YYYY-NNN`. Any rule that treats
        a trailing `-<digits>` as a collision suffix collapses 25 unrelated
        Oaktree postings into one group. The identity keeps the slug whole, so
        these stay apart.
        """
        base = "https://oaktree.wd1.myworkdayjobs.com/Oaktree/job/Hyderabad/"
        a = base + "AVP--Senior-Fabric-Application-Developer_2026-397"
        b = base + "Manager--Asian-Fund-Administration_2026-21"
        c = base + "Associate--Workday-Financials-Developer_2026-113"
        assert len({provider_identity(a), provider_identity(b),
                    provider_identity(c)}) == 3

    def test_slug_without_a_requisition_gets_no_identity(self):
        """Huatai ends every slug at a bare `_`. With no id in the url, the
        location segment is the ONLY thing distinguishing two rows — so
        ignoring it would merge genuinely different openings. Refuse instead."""
        hk = ("https://htsc.wd102.myworkdayjobs.com/Huatai_Careers/job/Hong-Kong"
              "/Private-Wealth-Management---Business-Analyst_")
        sg = ("https://htsc.wd102.myworkdayjobs.com/Huatai_Careers/job/Singapore"
              "/Private-Wealth-Management---Business-Analyst_")
        assert provider_identity(hk) is None
        assert provider_identity(sg) is None

    def test_different_portals_are_not_collapsed(self):
        """Bain Capital serves one requisition from External_Public and
        External_Private, and Workday appends its own `-1` to one of the slugs.
        We deliberately do NOT collapse this: undoing the suffix is the exact
        rule that breaks Oaktree. It falls through to display-time folding."""
        pub = ("https://baincapital.wd1.myworkdayjobs.com/External_Public/job/Boston"
               "/XMLNAME-2027-Analyst--Liquid-Structured-Credit_REQ_108333")
        priv = ("https://baincapital.wd1.myworkdayjobs.com/External_Private/job/Boston"
                "/XMLNAME-2027-Analyst--Liquid-Structured-Credit_REQ_108333-1")
        assert provider_identity(pub) != provider_identity(priv)

    def test_apply_variant_gets_no_identity(self):
        url = ("https://bmo.wd3.myworkdayjobs.com/External/job/Vancouver-BC-CAN"
               "/Personal-Banker_R260023366/apply")
        assert provider_identity(url) is None


class TestIdentityAbstains:
    @pytest.mark.parametrize("url", [
        "", None,
        "https://www.janestreet.com/join-jane-street/apply/8647260002?gh_jid=8647260002",
        "https://higher.gs.com/roles/155571_GS_CAMPUS",
        "https://example.com/whatever",
    ])
    def test_unknown_providers_return_none(self, url):
        """None is the honest answer, and it leaves `(firm, url)` behaviour
        exactly as it was."""
        assert provider_identity(url) is None


# ==================================================== Class A wired into ingest

TALNET_BOARD_POOL_1 = TalnetBoard(
    firm="Bank of America", kind="events",
    board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/1/adv/",
)
TALNET_BOARD_POOL_2 = TalnetBoard(
    firm="Bank of America", kind="events",
    board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/2/adv/",
)


def _conn_opp(url, *, title="Power to Lead Fall 2026", location="New York, NY",
              deadline=None):
    return ConnOpp(firm="Bank of America", title=title, location=location,
                   url=url, source="talnet", deadline=deadline)


def _result(board, opps):
    return FetchResult(board=board, ok=True, opportunities=list(opps),
                       raw_count=len(list(opps)), error=None)


@pytest.mark.django_db
class TestIngestIdentityDedup:
    P1 = TestTalnetIdentity.BOFA_POOL_1
    P2 = TestTalnetIdentity.BOFA_POOL_2

    def test_same_posting_in_two_pools_creates_one_row(self, monkeypatch):
        monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [
            _result(TALNET_BOARD_POOL_1, [_conn_opp(self.P1)]),
            _result(TALNET_BOARD_POOL_2, [_conn_opp(self.P2, location="")]),
        ])
        ingest.ingest_boards([TALNET_BOARD_POOL_1, TALNET_BOARD_POOL_2], label="talnet")

        rows = Opportunity.objects.filter(firm__name="Bank of America")
        assert rows.count() == 1
        # The FIRST url wins and is left alone, because it is a url that
        # resolves and that anything pointing at this row already uses.
        assert rows.first().url == self.P1

    def test_second_pool_does_not_close_the_first(self, monkeypatch):
        """The failure this guards: the identity match updates a row whose url
        was never in the second board's fetch, so unless that url is fed back
        into closed-detection the row is closed the moment it is deduped."""
        monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [
            _result(TALNET_BOARD_POOL_1, [_conn_opp(self.P1)]),
            _result(TALNET_BOARD_POOL_2, [_conn_opp(self.P2, location="")]),
        ])
        ingest.ingest_boards([TALNET_BOARD_POOL_1, TALNET_BOARD_POOL_2], label="talnet")

        row = Opportunity.objects.get(firm__name="Bank of America")
        assert row.status == "open"
        assert row.closed_at is None

    def test_retitled_icims_slug_updates_instead_of_forking(self, monkeypatch):
        from coverage_connectors import IcimsBoard
        board = IcimsBoard(firm="SIG", tenant="careers-sig")

        def sig(url, title):
            return ConnOpp(firm="SIG", title=title, location="Hong Kong",
                           url=url, source="icims", deadline=None)

        monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [
            _result(board, [sig(TestIcimsIdentity.BEFORE, "Operations Analyst, Hong Kong")]),
        ])
        ingest.ingest_boards([board], label="icims")

        monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [
            _result(board, [sig(TestIcimsIdentity.AFTER,
                                "Business Operations Analyst (Middle/Back Office), Hong Kong")]),
        ])
        ingest.ingest_boards([board], label="icims")

        rows = Opportunity.objects.filter(firm__name="SIG")
        assert rows.count() == 1
        row = rows.first()
        assert row.status == "open"
        assert row.title.startswith("Business Operations Analyst")

    def test_unrelated_postings_still_create_separate_rows(self, monkeypatch):
        """The dedup must not be able to swallow a genuinely new posting."""
        other = self.P1.replace("/opp/14594-", "/opp/14395-")
        monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [
            _result(TALNET_BOARD_POOL_1,
                    [_conn_opp(self.P1), _conn_opp(other, title="Global Operations SA 2027")]),
        ])
        ingest.ingest_boards([TALNET_BOARD_POOL_1], label="talnet")
        assert Opportunity.objects.filter(firm__name="Bank of America").count() == 2


# ======================================================= Class B: display fold

class _Row:
    """The attributes `fold_duplicates` reads, and nothing else — it is a pure
    function over rows, not over the ORM."""

    def __init__(self, id, firm_id=1, title="Summer Analyst", location="London",
                 deadline=None, first_seen=None, cohort=""):
        self.id = id
        self.firm_id = firm_id
        self.title = title
        self.location = location
        self.deadline = deadline
        self.first_seen = first_seen or datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        self.cohort = cohort


class TestNormalizeLabel:
    def test_case_and_whitespace_insensitive(self):
        assert normalize_label("  Summer   ANALYST ") == normalize_label("summer analyst")

    def test_dash_styles_unify(self):
        """Deutsche Bank's Jaipur intake is posted both ways."""
        assert (normalize_label("Apprentice hiring for 2026 – 2027")
                == normalize_label("Apprentice Hiring for 2026- 2027"))

    def test_genuinely_different_titles_stay_different(self):
        assert normalize_label("Legal Associate") != normalize_label("Fiscal Associate")

    def test_missing_space_after_comma_unifies(self):
        """DBS's own Workday board opened 'Assistant Vice President,
        Treasures Relationship Manager, Consumer Banking Group' twice (req
        ids WD86250 / WD86252, byte-identical descriptions); the second
        req's title is missing the space after the first comma. Neither
        Class A (different req ids) nor the old Class B (exact string match)
        caught the pair — collapsing comma spacing the same way dash spacing
        is already collapsed does."""
        assert (normalize_label(
            "Assistant Vice President, Treasures Relationship Manager, "
            "Consumer Banking Group")
            == normalize_label(
            "Assistant Vice President,Treasures Relationship Manager, "
            "Consumer Banking Group"))

    def test_stray_space_before_comma_unifies(self):
        """The one-tier-down DBS pair: 'Senior Associate ,Treasures...' has
        a stray space BEFORE the comma instead of a missing one after it."""
        assert (normalize_label("Senior Associate ,Treasures Relationship Manager")
                == normalize_label("Senior Associate, Treasures Relationship Manager"))

    def test_comma_and_dash_separators_unify(self):
        """DRW's own Greenhouse board posts the same requisition-shape title
        with a comma at one job id and a dash at another (ids 3402/3398 NYC,
        3401/3399 Greenwich); Fidelity International's Tokyo manager role the
        same way (ids 1363/1360). The old normalize_label canonicalized
        whitespace AROUND an existing dash or comma into two different fixed
        forms and never equated the two separator styles with each other."""
        assert (normalize_label("Software Engineer, Cumberland/FICCO Tools Engineering")
                == normalize_label("Software Engineer - Cumberland/FICCO Tools Engineering"))
        assert (normalize_label("Wholesale Sales Senior Manager")
                == normalize_label("Wholesale Sales, Senior Manager"))

    def test_slash_colon_and_pipe_separators_unify(self):
        """Morgan Stanley (comma+slash vs comma+' / '), SIG (dash vs pipe)."""
        assert (normalize_label("Associate, Risk/Policy Management")
                == normalize_label("Associate, Risk / Policy Management"))
        assert (normalize_label("Quantitative Systematic Trader- Experienced Hire")
                == normalize_label("Quantitative Systematic Trader | Experienced Hire"))

    def test_no_separator_at_all_unifies_with_a_dash(self):
        """MUFG: 'AVP-' (no space before the dash) vs 'AVP ' (no separator at
        all, just a space) — the words are the same either way."""
        assert (normalize_label(
            "AVP- Corporate Functions/Risk Management/ Issue Management- "
            "Internal Audit")
            == normalize_label(
            "AVP Corporate Functions/ Risk Management/ Issue Management- "
            "Internal Audit"))

    def test_three_separator_styles_of_the_same_words_all_unify(self):
        """Barclays Pune's 'Engineering Manager VP' cluster: no separator,
        a plain dash, and an en dash, across 7 live rows."""
        assert (normalize_label("Engineering Manager VP")
                == normalize_label("Engineering Manager - VP")
                == normalize_label("Engineering Manager – VP"))

    def test_different_words_joined_by_a_separator_still_stay_different(self):
        """The fix must not turn every separator into a wildcard: PwC
        Barcelona's two practice areas share the same boilerplate shape and
        are genuinely different roles."""
        assert (normalize_label("Barcelona - Legal Associate")
                != normalize_label("Barcelona - Fiscal Associate"))


class TestTitleBag:
    """Round 5 regression: `normalize_label` unifies punctuation and
    separator style but leaves WORD ORDER a hard divider, so word-order
    variants of the same title never folded. Brookfield posted the identical
    opening as both 'Manager, Finance' (id=955) and 'Finance Manager'
    (id=10488), same firm, same Toronto location, both open. A full sweep of
    every open row found 18 such groups, including Point72's 'Quantitative
    Researcher - Macro' / 'Macro Quantitative Researcher' and Blue Owl's
    'Real Assets Accounting, Senior Associate' / 'Senior Associate - Real
    Assets Accounting'."""

    def test_reordered_comma_title_matches(self):
        assert _title_bag("Manager, Finance") == _title_bag("Finance Manager")

    def test_reordered_dash_title_matches(self):
        assert (_title_bag("Quantitative Researcher - Macro")
                == _title_bag("Macro Quantitative Researcher"))

    def test_longer_reordered_title_matches(self):
        assert (_title_bag("Real Assets Accounting, Senior Associate")
                == _title_bag("Senior Associate - Real Assets Accounting"))

    def test_genuinely_different_word_sets_still_differ(self):
        """The fix must not turn every same-length title into a match —
        Barcelona's Legal vs Fiscal practice areas share every word except
        one and must stay apart."""
        assert (_title_bag("Barcelona - Legal Associate")
                != _title_bag("Barcelona - Fiscal Associate"))

    def test_duplicate_key_uses_the_word_order_insensitive_title(self):
        row_a = _Row(1, title="Manager, Finance")
        row_b = _Row(2, title="Finance Manager")
        assert duplicate_key(row_a)[1] == duplicate_key(row_b)[1]
        assert duplicate_key(row_a)[0] == duplicate_key(row_b)[0]
        assert duplicate_key(row_a)[2] == duplicate_key(row_b)[2]


class TestFoldDuplicates:
    def test_identical_rows_fold_to_one(self):
        kept, folded = fold_duplicates([_Row(1), _Row(2)])
        assert folded == 1
        assert [r.id for r in kept] == [1]

    def test_different_titles_are_left_alone(self):
        kept, folded = fold_duplicates([_Row(1, title="Legal Associate"),
                                        _Row(2, title="Fiscal Associate")])
        assert folded == 0
        assert len(kept) == 2

    def test_different_firms_are_left_alone(self):
        kept, folded = fold_duplicates([_Row(1, firm_id=1), _Row(2, firm_id=2)])
        assert folded == 0

    def test_brookfield_word_order_pair_does_not_fold(self):
        """The live feed stays word-order STRICT. Brookfield's ids 955
        ('Finance Manager', Infrastructure) and 10488 ('Manager, Finance',
        Energy) are anagram titles at the same firm and same Toronto, Ontario
        location, but they are two different real open requisitions — and
        neither states a deadline, so the deadline veto cannot protect them.
        Folding them hid a live Energy role from students. `_title_bag`
        word-order matching belongs to `duplicate_key()`'s report-only
        LOOKALIKE listing, never to `fold_duplicates()`."""
        kept, folded = fold_duplicates([
            _Row(955, firm_id=46, title="Finance Manager", location="Toronto, Ontario"),
            _Row(10488, firm_id=46, title="Manager, Finance", location="Toronto, Ontario"),
        ])
        assert folded == 0
        assert [r.id for r in kept] == [955, 10488]

    def test_word_order_pair_at_different_locations_still_stays_apart(self):
        """Word order alone never folds, and location is a second, independent
        divider — the same title reordered at two different real cities is
        unambiguously two jobs."""
        kept, folded = fold_duplicates([
            _Row(1, title="Finance Manager", location="Toronto, Ontario"),
            _Row(2, title="Manager, Finance", location="London, United Kingdom"),
        ])
        assert folded == 0
        assert len(kept) == 2

    def test_dbs_comma_space_variant_folds(self):
        """End-to-end regression for the confirmed DBS defect: two real
        Workday req ids for the same posting, titles differing only by the
        space after the first comma, now fold at the display layer."""
        kept, folded = fold_duplicates([
            _Row(13814, firm_id=202, location="New Delhi",
                 title="Assistant Vice President, Treasures Relationship "
                       "Manager, Consumer Banking Group"),
            _Row(13815, firm_id=202, location="New Delhi",
                 title="Assistant Vice President,Treasures Relationship "
                       "Manager, Consumer Banking Group"),
        ])
        assert folded == 1
        assert len(kept) == 1

    def test_drw_comma_vs_dash_variant_folds(self):
        """End-to-end regression for the confirmed DRW defect: two real
        Greenhouse job ids for the same posting, titles differing only by
        comma vs dash, now fold at the display layer."""
        kept, folded = fold_duplicates([
            _Row(3402, firm_id=86, location="New York City",
                 title="Software Engineer, Cumberland/FICCO Tools Engineering"),
            _Row(3398, firm_id=86, location="New York City",
                 title="Software Engineer - Cumberland/FICCO Tools Engineering"),
        ])
        assert folded == 1
        assert len(kept) == 1

    def test_different_locations_are_left_alone(self):
        kept, folded = fold_duplicates([_Row(1, location="London"),
                                        _Row(2, location="New York")])
        assert folded == 0

    def test_blank_location_absorbs_into_the_only_stated_city(self):
        """tal.net's second pool serves the same posting with the city
        dropped. A blank is a missing answer, not a different place."""
        kept, folded = fold_duplicates([_Row(1, location="New York, NY"),
                                        _Row(2, location="")])
        assert folded == 1
        assert kept[0].id == 1

    def test_blank_location_stays_out_when_the_title_spans_cities(self):
        """With two candidate cities, assigning the blank row to either one
        would be a guess — so it is left visible."""
        kept, folded = fold_duplicates([_Row(1, location="London"),
                                        _Row(2, location="New York"),
                                        _Row(3, location="")])
        assert folded == 0
        assert len(kept) == 3

    def test_same_city_still_folds_when_another_city_exists(self):
        """A title running in two cities must still deduplicate WITHIN each
        city — vetoing the whole title would lose real folds."""
        kept, folded = fold_duplicates([
            _Row(1, location="London"), _Row(2, location="London"),
            _Row(3, location="New York"),
        ])
        assert folded == 1
        assert [r.id for r in kept] == [1, 3]

    def test_workday_n_locations_placeholder_folds_like_a_blank(self):
        """End-to-end regression for the confirmed Franklin Templeton defect:
        one real global 'Head of Global Talent Acquisition' hire, split into
        a US-tagged Workday req (location '4 Locations') and a UK-tagged req
        ('2 Locations') for candidate sourcing. Neither placeholder names a
        real city (`raw["detail_location"]` is unset on both live rows), so
        the two counts must not act as two different stated places."""
        kept, folded = fold_duplicates([
            _Row(6513, firm_id=301, location="4 Locations",
                 title="Head of Global Talent Acquisition"),
            _Row(6230, firm_id=301, location="2 Locations",
                 title="Head of Global Talent Acquisition"),
        ])
        assert folded == 1
        assert len(kept) == 1

    def test_n_locations_placeholder_does_not_fold_across_real_cities(self):
        """The placeholder is a blank, not a wildcard: with a genuinely
        stated city also in the group, an ambiguous placeholder row must
        stay its own cluster rather than guess which city it belongs to —
        same rule as `test_blank_location_stays_out_when_the_title_spans_cities`."""
        kept, folded = fold_duplicates([
            _Row(1, location="London"),
            _Row(2, location="New York"),
            _Row(3, location="3 Locations"),
        ])
        assert folded == 0
        assert len(kept) == 3

    def test_dbs_country_qualifier_folds_with_the_bare_city(self):
        """End-to-end regression for the confirmed DBS defect: two real
        Workday req ids for the same Shanghai posting, one location carrying
        a 'PRC - ' country qualifier the other lacks."""
        kept, folded = fold_duplicates([
            _Row(15061, firm_id=202, location="PRC - Shanghai",
                 title="Trade Operations Specialist"),
            _Row(15030, firm_id=202, location="Shanghai",
                 title="Trade Operations Specialist"),
        ])
        assert folded == 1
        assert len(kept) == 1

    def test_dbs_entity_code_qualifier_folds_with_the_bare_city(self):
        """The same DBS pattern recurs with the bank's own entity code
        (DBIL = DBS Bank India Limited) instead of a country qualifier, on
        two independent title-groups."""
        kept, folded = fold_duplicates([
            _Row(13932, firm_id=202, location="Vadodara-DBIL",
                 title="Senior Associate, Treasures Relationship Manager, "
                       "Consumer Banking Group"),
            _Row(19428, firm_id=202, location="Vadodara",
                 title="Senior Associate, Treasures Relationship Manager, "
                       "Consumer Banking Group"),
        ])
        assert folded == 1
        assert len(kept) == 1

        kept, folded = fold_duplicates([
            _Row(13933, firm_id=202, location="Ahmedabad-DBIL",
                 title="Senior Associate, Treasures Relationship Manager, "
                       "Consumer Banking Group"),
            _Row(13720, firm_id=202, location="Ahmedabad",
                 title="Senior Associate, Treasures Relationship Manager, "
                       "Consumer Banking Group"),
        ])
        assert folded == 1
        assert len(kept) == 1

    def test_ghatkopar_and_new_taipei_stay_genuinely_different_places(self):
        """The qualifier allow-list is curated, not a bare word-superset
        rule, precisely because a superset rule also fires here: Ghatkopar is
        a real Mumbai suburb and New Taipei City is a real, administratively
        separate city from Taipei City. Neither 'ghatkopar' nor 'new' is a
        known non-place qualifier, so both stay hard dividers."""
        kept, folded = fold_duplicates([
            _Row(1, firm_id=202, location="Ghatkopar Mumbai", title="Analyst"),
            _Row(2, firm_id=202, location="Mumbai", title="Analyst"),
        ])
        assert folded == 0
        assert len(kept) == 2

        kept, folded = fold_duplicates([
            _Row(3, firm_id=202, location="New Taipei", title="Analyst"),
            _Row(4, firm_id=202, location="Taipei", title="Analyst"),
        ])
        assert folded == 0
        assert len(kept) == 2

    def test_pwc_barcelona_practice_areas_are_not_duplicates(self):
        """Explicitly ruled out: same boilerplate shape, different practices."""
        kept, folded = fold_duplicates([
            _Row(1, title="Barcelona - Legal Associate", location="Barcelona"),
            _Row(2, title="Barcelona - Fiscal Associate", location="Barcelona"),
        ])
        assert folded == 0
        assert len(kept) == 2

    # ---- the veto

    def test_two_stated_deadlines_veto_the_fold(self):
        """Bank of America runs 'Quantitative Strategies & Data Group |
        Recruitment' on two dates. Same title, same city, two real events —
        folding one away costs the student a date they could still make."""
        kept, folded = fold_duplicates([
            _Row(1, deadline=date(2026, 8, 18)),
            _Row(2, deadline=date(2026, 9, 9)),
        ])
        assert folded == 0
        assert len(kept) == 2

    def test_three_copies_with_two_dates_are_all_kept(self):
        kept, folded = fold_duplicates([
            _Row(1, deadline=date(2026, 12, 31)),
            _Row(2, deadline=date(2026, 12, 31)),
            _Row(3, deadline=date(2027, 7, 7)),
        ])
        assert folded == 0
        assert len(kept) == 3

    def test_one_stated_deadline_still_folds(self):
        """One posting recorded twice at different completeness is not two
        events — and the dated copy is the one worth keeping."""
        kept, folded = fold_duplicates([_Row(1), _Row(2, deadline=date(2026, 9, 30))])
        assert folded == 1
        assert [r.id for r in kept] == [2]

    def test_two_stated_cohorts_veto_the_fold(self):
        """Confirmed live at Goldman Sachs: role IDs 170880 (cohort=2027) and
        150658 (cohort=2026) share firm+title+location byte-for-byte, both
        deadline=None, so nothing else in `fold_duplicates` distinguishes the
        pair — until the cohort veto fires. A firm reposting the identical
        title+location for its NEXT admissions cohort is two real postings,
        not one recorded twice; folding one away silently deleted the newer
        cohort from every fold_duplicates-consuming page, not just from
        behind a fold-count badge."""
        kept, folded = fold_duplicates([
            _Row(1, cohort="2026"),
            _Row(2, cohort="2027"),
        ])
        assert folded == 0
        assert len(kept) == 2

    def test_one_stated_cohort_still_folds(self):
        """A blank cohort is a posting that didn't say, not a competing
        claim — exactly the deadline veto's own asymmetric case. (Which copy
        survives is governed by `_survivor_rank`'s other rules, not this
        test — only the fold count is the veto's own concern.)"""
        kept, folded = fold_duplicates([_Row(1), _Row(2, cohort="2027")])
        assert folded == 1
        assert len(kept) == 1

    def test_three_copies_with_two_cohorts_are_all_kept(self):
        kept, folded = fold_duplicates([
            _Row(1, cohort="2026"),
            _Row(2, cohort="2026"),
            _Row(3, cohort="2027"),
        ])
        assert folded == 0
        assert len(kept) == 3

    # ---- the tiebreak, in its documented order

    def test_deadline_beats_no_deadline(self):
        kept, _ = fold_duplicates([_Row(1), _Row(2, deadline=date(2026, 9, 30))])
        assert kept[0].id == 2

    def test_location_beats_blank_location(self):
        """tal.net's second pool routinely drops the city the first one states."""
        kept, folded = fold_duplicates([_Row(1, location=""),
                                        _Row(2, location="New York, NY")])
        assert folded == 1
        assert kept[0].id == 2

    def test_older_first_seen_wins_when_otherwise_equal(self):
        older = _Row(1, first_seen=datetime(2026, 1, 1, tzinfo=dt_timezone.utc))
        newer = _Row(2, first_seen=datetime(2026, 6, 1, tzinfo=dt_timezone.utc))
        kept, _ = fold_duplicates([newer, older])
        assert kept[0].id == 1

    def test_a_tracked_copy_outranks_a_more_complete_one(self):
        """Whatever else is true, showing the copy the student never touched
        would render their own pipeline state as if it were never set."""
        plain = _Row(1)
        richer = _Row(2, deadline=date(2026, 9, 30))
        kept, folded = fold_duplicates([plain, richer], sticky_ids={1})
        assert folded == 1
        assert kept[0].id == 1

    # ---- shape guarantees

    def test_input_order_is_preserved(self):
        rows = [_Row(3, title="C"), _Row(1, title="A"), _Row(2, title="B")]
        kept, folded = fold_duplicates(rows)
        assert folded == 0
        assert [r.id for r in kept] == [3, 1, 2]

    def test_result_is_independent_of_input_order(self):
        a, b = _Row(1), _Row(2)
        assert fold_duplicates([a, b])[0][0].id == fold_duplicates([b, a])[0][0].id

    def test_empty_input(self):
        assert fold_duplicates([]) == ([], 0)


@pytest.mark.django_db
class TestFoldDuplicatesOnRealRows:
    def test_works_against_orm_instances(self):
        """The pure function is fed Opportunity rows in the view, so its
        attribute expectations have to match the model."""
        firm = Firm.objects.create(slug="sig", name="SIG", status="active")
        a = Opportunity.objects.create(
            firm=firm, title="Quantitative Trader Internship: Summer 2027",
            location="", url="https://careers-sig.icims.com/jobs/10849/x/job",
            source="icims", status="open")
        b = Opportunity.objects.create(
            firm=firm, title="Quantitative Trader Internship: Summer 2027",
            location="", url="https://careers-sig.icims.com/jobs/10718/x/job",
            source="icims", status="open")

        kept, folded = fold_duplicates([a, b])
        assert folded == 1
        # Both rows survive in the database — they are separate requisitions
        # and each has to keep being close-tracked on its own.
        assert Opportunity.objects.filter(firm=firm).count() == 2
        assert kept[0].id in {a.id, b.id}


@pytest.mark.django_db
class TestFirmDetailFoldsDuplicates:
    """firm_detail() is the one place that used to build its row list
    straight off the ORM without ever calling fold_duplicates — Browse
    Openings, My Applications, and the calendar all fold, so a firm page
    showed the same byte-identical requisition twice and its own "Open
    Roles" count disagreed with the feed's count for the same firm. This
    reproduces the live HSBC shape: two `Women in Technology` rows that
    differ only in the req number buried in the URL.
    """

    def test_two_req_numbers_for_one_posting_render_as_one_card(self, client):
        firm = Firm.objects.create(slug="hsbc", name="HSBC", status="active")
        Opportunity.objects.create(
            firm=firm, title="Women in Technology - Women Who Code - Insight Programme",
            location="Sheffield, GB, S1 4NB", bucket="insight", status="open",
            url="https://mycareer.hsbc.com/en_GB/external/OpportunityDetail"
                "?opportunityId=1365759057",
        )
        Opportunity.objects.create(
            firm=firm, title="Women in Technology - Women Who Code - Insight Programme",
            location="Sheffield, GB, S1 4NB", bucket="insight", status="open",
            url="https://mycareer.hsbc.com/en_GB/external/OpportunityDetail"
                "?opportunityId=1365759357",
        )
        # A genuinely distinct role must survive untouched.
        Opportunity.objects.create(
            firm=firm, title="Global Banking Summer Analyst", location="London, GB",
            bucket="internship", status="open",
            url="https://mycareer.hsbc.com/en_GB/external/OpportunityDetail"
                "?opportunityId=9999999999",
        )

        res = client.get("/firms/hsbc/")
        assert res.status_code == 200
        body = res.content.decode()

        assert body.count("Women in Technology - Women Who Code") == 1
        # The header count must match what's actually on the page, and match
        # what the feed (which already folds) would say for this firm: 2
        # distinct roles, not 3 raw rows.
        assert Opportunity.objects.filter(firm=firm, status="open").count() == 3
        assert '<span class="scrub-count">2</span>' in body

    def test_a_real_repeating_series_still_shows_every_date(self, client):
        """The veto in fold_duplicates: two DIFFERENT stated deadlines on an
        otherwise-identical title/location must not collapse — a firm page
        that hid one would cost the student a date they could still make."""
        firm = Firm.objects.create(slug="boci", name="BOCI", status="active")
        Opportunity.objects.create(
            firm=firm, title="Graduate Programme", location="Hong Kong",
            bucket="entry_level", status="open", deadline=date(2026, 9, 1),
            url="https://boci.example/req/1",
        )
        Opportunity.objects.create(
            firm=firm, title="Graduate Programme", location="Hong Kong",
            bucket="entry_level", status="open", deadline=date(2026, 10, 1),
            url="https://boci.example/req/2",
        )

        res = client.get("/firms/boci/")
        body = res.content.decode()
        assert body.count("Graduate Programme") == 2
        assert '<span class="scrub-count">2</span>' in body

    def test_a_new_admissions_cohort_still_shows_both_postings(self, client):
        """The cohort veto in fold_duplicates: a firm reposting the identical
        title+location for its NEXT admissions cohort, with neither copy
        stating a deadline, is two real postings — reproduces the confirmed
        live Goldman Sachs shape exactly (role IDs 170880/150658, London,
        cohort 2027 vs 2026, both deadline=None)."""
        firm = Firm.objects.create(slug="gs", name="Goldman Sachs", status="active")
        Opportunity.objects.create(
            firm=firm,
            title="Global Investment Research — Macro Research, Economics — "
                  "Seasonal/Off-cycle Internship",
            location="London", bucket="internship", status="open", cohort="2026",
            url="https://higher.gs.com/roles/150658_GS_CAMPUS",
        )
        Opportunity.objects.create(
            firm=firm,
            title="Global Investment Research — Macro Research, Economics — "
                  "Seasonal/Off-cycle Internship",
            location="London", bucket="internship", status="open", cohort="2027",
            url="https://higher.gs.com/roles/170880_GS_CAMPUS",
        )

        res = client.get("/firms/gs/?role=all")
        body = res.content.decode()
        assert body.count("Global Investment Research") == 2
        assert "170880_GS_CAMPUS" in body
        assert "150658_GS_CAMPUS" in body
