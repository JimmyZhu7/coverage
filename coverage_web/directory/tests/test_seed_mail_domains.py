"""`seed_mail_domains`: the loader that puts firms' real EMAIL domains in the
directory, so `capture.discovery` can recognise a banker's address.

The bug these guard against was silent: `Firm.domains` held career-site hosts
(`careers.bcg.com`, `jobs.rbc.com`) because that is all a board connector ever
knows, so a mail from `@gs.com` matched nothing and the sender was refused.
The fix lived only in the founder's database and in a gitignored YAML, so the
first thing to pin is that it survives a fresh environment — and the second is
that re-running it never eats data someone else put there.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from directory._mail_domains import CREATABLE_FIRMS, MAIL_DOMAINS
from directory.models import Firm

pytestmark = pytest.mark.django_db


def _seed():
    call_command("seed_mail_domains", verbosity=0)


# ---------------------------------------------------------------------------
# The data itself
# ---------------------------------------------------------------------------
def test_no_career_site_host_is_listed_as_a_mail_domain():
    """The whole bug was a career site standing in for a mail domain. A row
    matching one of these shapes would re-introduce it."""
    bad = ("myworkdayjobs.com", "icims.com", "tal.net", "avature.net",
           "greenhouse.io", "lever.co", "oraclecloud.com", "phenompeople.com")
    for slug, domains in MAIL_DOMAINS.items():
        for domain in domains:
            assert not domain.startswith(("careers.", "jobs.", "job.")), (slug, domain)
            assert not any(domain.endswith(suffix) for suffix in bad), (slug, domain)


def test_every_creatable_firm_also_declares_its_mail_domains():
    """A firm created with nowhere to hang a domain is pointless."""
    assert set(CREATABLE_FIRMS) <= set(MAIL_DOMAINS)


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_running_twice_changes_nothing():
    Firm.objects.create(slug="gs", name="Goldman Sachs", domains=["goldmansachs.com"])
    _seed()
    after_first = {f.slug: list(f.domains) for f in Firm.objects.all()}
    count_first = Firm.objects.count()

    _seed()

    assert Firm.objects.count() == count_first
    assert {f.slug: list(f.domains) for f in Firm.objects.all()} == after_first


def test_a_database_that_already_has_the_fix_is_untouched():
    """The founder's own database. Nothing to do, and nothing done."""
    firm = Firm.objects.create(
        slug="gs", name="Goldman Sachs", domains=["goldmansachs.com", "gs.com"]
    )
    _seed()
    firm.refresh_from_db()
    assert firm.domains == ["goldmansachs.com", "gs.com"]


# ---------------------------------------------------------------------------
# Additive, never destructive
# ---------------------------------------------------------------------------
def test_it_appends_and_never_replaces_the_existing_list():
    firm = Firm.objects.create(
        slug="wf", name="Wells Fargo", domains=["wellsfargojobs.com"]
    )
    _seed()
    firm.refresh_from_db()
    # The career-site host the board connector depends on is still first.
    assert firm.domains[0] == "wellsfargojobs.com"
    assert "wellsfargo.com" in firm.domains


def test_a_hand_added_domain_is_never_dropped():
    firm = Firm.objects.create(
        slug="bofa", name="Bank of America", domains=["bankofamerica.com", "ml.com"]
    )
    _seed()
    firm.refresh_from_db()
    assert "ml.com" in firm.domains
    assert "bofa.com" in firm.domains and "baml.com" in firm.domains


def test_an_existing_firms_curated_tracks_are_not_overwritten():
    firm = Firm.objects.create(
        slug="liontree", name="LionTree", tracks=["ib", "st"], regions=["us", "hk"]
    )
    _seed()
    firm.refresh_from_db()
    assert firm.tracks == ["ib", "st"]
    assert firm.regions == ["us", "hk"]


# ---------------------------------------------------------------------------
# Create-or-update by slug
# ---------------------------------------------------------------------------
def test_it_creates_the_firms_that_are_missing():
    _seed()
    qatalyst = Firm.objects.get(slug="qatalyst")
    assert qatalyst.name == "Qatalyst Partners"
    assert qatalyst.domains == ["qatalyst.com"]
    assert qatalyst.tracks == ["ib"] and qatalyst.regions == ["us"]
    assert qatalyst.status == "active"


def test_a_firm_that_already_exists_by_slug_is_updated_not_duplicated():
    Firm.objects.create(slug="qatalyst", name="Qatalyst Partners")
    _seed()
    assert Firm.objects.filter(slug="qatalyst").count() == 1
    assert Firm.objects.get(slug="qatalyst").domains == ["qatalyst.com"]


def test_a_firm_present_under_the_same_name_is_adopted_whatever_its_slug():
    """The founder's database carries a blank-slug "Citadel Securities" row.
    Keying on slug alone would mint a second one beside it."""
    stray = Firm.objects.create(slug="", name="Citadel Securities", tracks=["st"])
    _seed()
    assert Firm.objects.filter(name="Citadel Securities").count() == 1
    stray.refresh_from_db()
    assert stray.slug == ""          # never re-slugged behind anyone's back
    assert "citadel.com" in stray.domains


def test_an_unknown_slug_is_skipped_not_invented():
    """Goldman only ever gets a domain APPENDED. Its canonical row is defined
    elsewhere (the seed set, the board catalog), so a missing one is reported
    and skipped rather than forked here."""
    _seed()
    assert not Firm.objects.filter(slug="gs").exists()


def test_dry_run_writes_nothing():
    Firm.objects.create(slug="gs", name="Goldman Sachs", domains=["goldmansachs.com"])
    call_command("seed_mail_domains", "--dry-run", verbosity=0)
    assert Firm.objects.get(slug="gs").domains == ["goldmansachs.com"]
    assert not Firm.objects.filter(slug="qatalyst").exists()
