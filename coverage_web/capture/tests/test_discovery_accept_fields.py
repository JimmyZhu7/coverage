"""What the Gmail door writes when a proposal is accepted — the three gaps
the 2026-09-01 CRM lifecycle audit measured on the founder's 306 contacts:

  * role blank on 136 of 151 capture rows, region blank on 94 of 265, and 90
    rows carrying neither, because nothing anywhere asked (L3);
  * 7 alumni sitting at free-text firm "usc" while their own email domain
    resolves to Bain, BCG, Deloitte or PwC (L2);
  * 136 accepted "outreach" proposals whose touch dropped the subject the
    proposal was already holding, so `crm.campaigns.detect` could not group
    39 sends from one night as one send (L6).

`transaction=True` for the same reason as `test_discovery.py`: accepting a
proposal calls `crm.services.log_touch`, which opens its own psycopg
connection and can only see committed rows.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from capture import discovery
from capture.models import ContactProposal
from crm.models import Contact, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(
        email="accept-fields@example.com", password="x",
        school="University of Southern California",
        school_emails=["someone@usc.edu"],
    )


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", domains=["northbank.example"]
    )


def _proposal(student, firm=None, **over):
    base = dict(
        user=student,
        name="Alex Banker",
        email="alex.banker@northbank.example",
        firm=firm,
        role_hint="",
        evidence="Replied to your email",
        evidence_kind="reply_received",
        thread_subject="Coffee next week",
        thread_id="t-fields-1",
    )
    base.update(over)
    return ContactProposal.all_objects.create(**base)


# ---------------------------------------------------------------------------
# L3 — the two facts the card can now carry.
# ---------------------------------------------------------------------------
def test_accept_writes_the_role_and_region_the_student_supplied(student, firm):
    contact = discovery.accept(_proposal(student, firm), role="VP, TMT", region="hk")
    assert contact.role == "VP, TMT"
    assert contact.region == "hk"
    # `user` provenance: a person just said so, and nothing — not the firm
    # rule, not a later Settings change — may overwrite that.
    assert contact.region_source == Contact.REGION_SOURCE_USER


def test_accept_without_them_behaves_exactly_as_before(student, firm):
    """The degradation clause. Untouched fields post empty strings, the role
    hint still becomes the role, and the deterministic region rule still gets
    first refusal (blank here: the firm declares no market)."""
    contact = discovery.accept(
        _proposal(student, firm, role_hint="Analyst"), role="", region="",
    )
    assert contact.role == "Analyst"
    assert contact.region == ""
    assert contact.region_source == ""


def test_a_typed_role_wins_over_the_signature_hint(student, firm):
    contact = discovery.accept(
        _proposal(student, firm, role_hint="Analyst"), role="Associate",
    )
    assert contact.role == "Associate"


def test_a_junk_region_is_a_blank_not_an_error(student, firm):
    """A select that posts something outside REGION_VALUES is a broken
    client, not a user decision worth recording."""
    contact = discovery.accept(_proposal(student, firm), region="mars")
    assert contact.region == ""


def test_the_proposal_card_posts_role_and_region_through_the_view(client, student, firm):
    proposal = _proposal(student, firm)
    client.force_login(student)
    client.post(
        reverse("crm:proposal_act", args=[proposal.id, "accept"]),
        {"role": "Campus Recruiting", "region": "us"},
    )
    contact = Contact.objects.for_user(student).get(email=proposal.email)
    assert (contact.role, contact.region) == ("Campus Recruiting", "us")


# ---------------------------------------------------------------------------
# L6 — the subject the proposal was already holding.
# ---------------------------------------------------------------------------
def test_the_accepted_touch_carries_the_thread_subject(student, firm):
    contact = discovery.accept(_proposal(student, firm))
    touch = Touch.objects.for_user(student).get(contact=contact)
    assert touch.subject == "Coffee next week"


def test_no_subject_on_the_proposal_leaves_the_column_blank(student, firm):
    """Blank is honest: a proposal with no subject line has none to carry,
    and an invented one would break exactly the grouping this fixes."""
    contact = discovery.accept(_proposal(student, firm, thread_subject=""))
    touch = Touch.objects.for_user(student).get(contact=contact)
    assert touch.subject == ""


def test_two_proposals_sharing_a_subject_are_groupable_afterwards(student, firm):
    """The whole point: a mail merge's defining fact is that N threads share
    one subject. Before this the column was blank on every accepted row."""
    for i in range(2):
        discovery.accept(_proposal(
            student, firm, email=f"person{i}@northbank.example",
            name=f"Person {i}", thread_id=f"t-merge-{i}",
            thread_subject="USC Consulting Club Student Panel Outreach",
        ))
    subjects = set(
        Touch.objects.for_user(student).values_list("subject", flat=True)
    )
    assert subjects == {"USC Consulting Club Student Panel Outreach"}


# ---------------------------------------------------------------------------
# L2 — the alum filed at their school.
# ---------------------------------------------------------------------------
def test_names_a_school_reads_the_students_own_institution(student):
    assert discovery.names_a_school("usc", student) is True
    assert discovery.names_a_school("USC", student) is True
    assert discovery.names_a_school("Boston College", student) is True
    assert discovery.names_a_school("Goldman Sachs", student) is False


def test_a_shared_stopword_is_not_a_shared_identity(student):
    """The founder's school is "University of Southern California", so "of"
    is a word it and "Bank of America" have in common. Without the stopword
    and length floors that overlap would re-file a banker as an alum — the
    exact false positive this rule must never produce."""
    assert discovery.names_a_school("Bank of America", student) is False
    assert "of" not in discovery.school_tokens(student)


def test_school_firm_fields_refiles_an_alum_under_their_employer(student):
    bain = Firm.objects.create(slug="bain", name="Bain & Company",
                               domains=["bain.com"])
    alum = Contact.all_objects.create(
        user=student, name="Nicole Park", email="nicole@bain.com",
        firm_text="usc", source="capture",
    )
    fields = discovery.school_firm_fields(alum, user=student)
    assert fields["firm_id"] == bain.id
    # The employer takes the FK; the school moves to the field that holds a
    # school, and stops being a firm name the coverage strip counts.
    assert fields["firm_text"] == ""
    assert fields["school_affiliation"] is True
    assert fields["school"] == "usc"


def test_a_firm_that_already_resolved_is_never_overwritten(student):
    """A resolved FK is either the directory's answer or a person's, and an
    email domain is weaker evidence than both."""
    Firm.objects.create(slug="bain", name="Bain & Company", domains=["bain.com"])
    other = Firm.objects.create(slug="north", name="North Bank", domains=[])
    contact = Contact.all_objects.create(
        user=student, name="Nicole Park", email="nicole@bain.com", firm=other,
    )
    assert discovery.school_firm_fields(contact, user=student) == {}


def test_a_real_employer_typed_by_hand_is_left_alone(student):
    """`firm_text` that names an employer, not a school, keeps the name the
    student typed even when the domain resolves."""
    Firm.objects.create(slug="bain", name="Bain & Company", domains=["bain.com"])
    contact = Contact.all_objects.create(
        user=student, name="Nicole Park", email="nicole@bain.com",
        firm_text="Bain Capital",
    )
    assert discovery.school_firm_fields(contact, user=student) == {}


def test_an_address_that_resolves_to_nothing_changes_nothing(student):
    contact = Contact.all_objects.create(
        user=student, name="Nicole Park", email="nicole@nowhere.example",
        firm_text="usc",
    )
    assert discovery.school_firm_fields(contact, user=student) == {}


def test_accepting_onto_an_existing_alum_row_refiles_it(student):
    """The repair reaches the live path too: an accept that reconciles onto
    a contact already on file fixes this one thing about them and nothing
    else."""
    bain = Firm.objects.create(slug="bain", name="Bain & Company",
                               domains=["bain.com"])
    alum = Contact.all_objects.create(
        user=student, name="Nicole Park", email="nicole@bain.com",
        firm_text="usc", source="capture",
    )
    proposal = _proposal(
        student, None, name="Nicole Park", email="nicole@bain.com",
        thread_id="t-alum",
    )
    contact = discovery.accept(proposal)
    assert contact.id == alum.id
    contact.refresh_from_db()
    assert contact.firm_id == bain.id
    assert contact.firm_text == ""
    assert contact.school_affiliation is True


def test_accept_fills_a_blank_firm_from_the_address(student):
    """A proposal that has sat pending since before the firm's `domains` were
    populated carries `firm=None` for no better reason than when it was
    written. Asked again when the row becomes real — to fill a blank only."""
    bank = Firm.objects.create(slug="northb", name="North Bank",
                               domains=["northbank.example"])
    contact = discovery.accept(_proposal(student, None))
    assert contact.firm_id == bank.id
