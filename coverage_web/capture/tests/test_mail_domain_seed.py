"""The end the whole thing exists for: after the mail domains are loaded, a
real banker's address resolves to their firm.

`capture.discovery.FirmDomains` is the gate that refused nineteen of
forty-eight real bankers in the founder's mailbox, because `Firm.domains` held
career-site hosts and not the domains people actually send from. These tests
run the tracked loader and then ask the gate the same question the pipeline
asks it.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from capture.discovery import FirmDomains
from directory.models import Firm

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    # Two rows as the board connectors left them: Goldman with its front door
    # but not the domain its bankers write from, and Wells Fargo with nothing
    # but a career site.
    Firm.objects.create(slug="gs", name="Goldman Sachs", domains=["goldmansachs.com"])
    Firm.objects.create(slug="wf", name="Wells Fargo", domains=["wellsfargojobs.com"])
    call_command("seed_mail_domains", verbosity=0)
    return None


def test_a_goldman_banker_resolves_to_goldman(seeded):
    gs = Firm.objects.get(slug="gs")
    assert FirmDomains().match("jhanvi.lakhani@gs.com") == gs.id


def test_the_same_address_resolved_to_nobody_before_the_load():
    Firm.objects.create(slug="gs", name="Goldman Sachs", domains=["goldmansachs.com"])
    assert FirmDomains().match("jhanvi.lakhani@gs.com") is None


def test_a_career_site_only_firm_becomes_reachable_by_mail(seeded):
    wf = Firm.objects.get(slug="wf")
    assert FirmDomains().match("someone@wellsfargo.com") == wf.id
    # ...and the career-site host it already had is still there for the board.
    assert "wellsfargojobs.com" in wf.domains


def test_subdomain_matching_still_behaves(seeded):
    """A parent-domain match is allowed; the reverse is not. Adding mail
    domains must not have widened or narrowed that rule."""
    gs = Firm.objects.get(slug="gs")
    assert FirmDomains().match("someone@mail.gs.com") == gs.id
    # A registered domain may not claim a shorter parent it is a subdomain of:
    # `wellsfargojobs.com` alone never resolved `wellsfargo.com`, which is the
    # whole reason the loader has to add the mail domain outright.
    Firm.objects.filter(slug="wf").update(domains=["wellsfargojobs.com"])
    assert FirmDomains().match("someone@wellsfargo.com") is None


def test_a_created_boutique_is_reachable_too(seeded):
    liontree = Firm.objects.get(slug="liontree")
    assert FirmDomains().match("partner@liontree.com") == liontree.id
