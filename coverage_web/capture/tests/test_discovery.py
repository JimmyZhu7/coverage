"""Contact auto-discovery tests (capture/discovery.py, the apply_findings
hook, and the Today-page proposal views).

The centre of gravity is the REFUSALS: this feature exists because
gmail_live's docstring (point 2) and capture_discover's contract both
refused silent creation, and every rule they drew has a test here — bulk
never proposes, archived matches are never resurrected, warmth is never
fabricated, dismiss is permanent, and nothing at all is written to
Contact/Touch until the user's tap.

`transaction=True` throughout for the same reason as test_gmail.py: accepting
a proposal calls `crm.services.log_touch`, which opens its own psycopg
connection and can only see committed rows.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture import discovery
from capture.gmail import apply_findings
from capture.models import ContactProposal
from coverage_web.tenancy import TenantScopeError
from crm.models import Contact, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(email="disc-student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", domains=["northbank.example"]
    )


def finding(**over):
    """A genuine inbound reply from an unknown sender at a firm domain —
    the canonical candidate. Tests override away from it."""
    base = {
        "name": "Alex Banker",
        "email": "alex.banker@northbank.example",
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": True,
        "chat_status": "none",
        "bulk": False,
        "threaded_reply": True,
        "subject": "Re: coffee next week",
        "evidence": "snippet text",
        "thread_id": "t-disc-1",
        "occurred_at": "2026-08-20T10:00:00+00:00",
    }
    base.update(over)
    return base


def consider(user, f):
    return discovery.consider_finding(user, f)


def pending(user):
    return list(
        ContactProposal.objects.for_user(user).filter(status="pending")
    )


# --------------------------------------------------------------------------- #
# The judgment chain — who never gets proposed
# --------------------------------------------------------------------------- #

def test_bulk_never_proposes(student, firm):
    assert consider(student, finding(bulk=True)) is None
    assert pending(student) == []


def test_bounce_never_proposes(student, firm):
    assert consider(student, finding(bounced=True)) is None


def test_outreach_only_never_proposes(student, firm):
    """The user's own sent mail to someone they chose not to track is not
    this feature's call to make."""
    assert consider(student, finding(replied=False, outreach_sent=True)) is None


def test_noreply_sender_never_proposes(student, firm):
    assert consider(
        student, finding(email="no-reply@northbank.example")
    ) is None


def test_role_account_never_proposes(student, firm):
    """careers@ is a mailbox, not a person — even at a matched firm."""
    for local in ("careers", "info", "campus.recruiting", "events", "hr"):
        assert consider(
            student, finding(email=f"{local}@northbank.example")
        ) is None


def test_esp_domain_never_proposes(student, firm):
    assert consider(
        student, finding(email="jane@bounce.sendgrid.net")
    ) is None


def test_stranger_with_no_signal_never_proposes(student, firm):
    """No firm-domain match AND no reply pointer: a stranger cold-emailing
    the student is not a networking contact."""
    assert consider(
        student, finding(email="rando@coldmail.example", threaded_reply=False)
    ) is None


# --------------------------------------------------------------------------- #
# The judgment chain — who does
# --------------------------------------------------------------------------- #

def test_firm_domain_match_proposes_with_firm(student, firm):
    assert consider(student, finding(threaded_reply=False)) == discovery.PROPOSED
    (p,) = pending(student)
    assert p.firm_id == firm.id
    assert p.email == "alex.banker@northbank.example"
    assert p.evidence_kind == "reply_received"
    assert "Re: coffee next week" in p.evidence
    # A proposal writes NOTHING to the CRM.
    assert Contact.objects.for_user(student).count() == 0
    assert Touch.objects.for_user(student).count() == 0


def test_subdomain_of_firm_domain_matches(student, firm):
    assert consider(
        student, finding(email="alex@mail.northbank.example", threaded_reply=False)
    ) == discovery.PROPOSED
    assert pending(student)[0].firm_id == firm.id


def test_threaded_reply_from_personal_domain_proposes(student, firm):
    """The user emailed them first, from outside Coverage — still a
    candidate, with no firm attached."""
    assert consider(
        student, finding(email="alum@gmail.com", threaded_reply=True)
    ) == discovery.PROPOSED
    (p,) = pending(student)
    assert p.firm_id is None


def test_chat_evidence_carries_its_kind(student, firm):
    assert consider(
        student, finding(chat_status="scheduled")
    ) == discovery.PROPOSED
    assert pending(student)[0].evidence_kind == "chat_scheduled"


def test_recruiting_hint_from_display_name(student, firm):
    assert consider(
        student, finding(name="Casey Cruz, Campus Recruiting")
    ) == discovery.PROPOSED
    (p,) = pending(student)
    assert p.name == "Casey Cruz"
    assert p.role_hint == "Campus Recruiting"
    assert p.recruiting_hint is True


def test_plain_name_has_no_hint(student, firm):
    assert consider(student, finding(name="Alex Banker")) == discovery.PROPOSED
    (p,) = pending(student)
    assert p.name == "Alex Banker"
    assert p.role_hint == ""
    assert p.recruiting_hint is False


# --------------------------------------------------------------------------- #
# Existing rows: never duplicate, never resurrect, never forget a dismiss
# --------------------------------------------------------------------------- #

def test_known_contact_never_proposes(student, firm):
    Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example"
    )
    assert consider(student, finding()) is None


def test_name_match_to_live_contact_never_proposes(student, firm):
    """Same person, second address: the alternate-email note is the right
    home for that fact, not a duplicate-person proposal."""
    Contact.all_objects.create(
        user=student, name="Alex  BANKER", email="alex@other.example"
    )
    assert consider(student, finding()) is None


def test_archived_match_reported_not_resurrected(student, firm):
    Contact.all_objects.create(
        user=student, name="Alex Banker",
        email="alex.banker@northbank.example", archived=True,
    )
    assert consider(student, finding()) == discovery.ARCHIVED_MATCH
    assert pending(student) == []


def test_dismissed_is_permanent(student, firm):
    assert consider(student, finding()) == discovery.PROPOSED
    (p,) = pending(student)
    discovery.dismiss(p)
    # The same person resurfacing next week must not come back.
    assert consider(student, finding()) is None
    assert pending(student) == []
    p.refresh_from_db()
    assert p.status == "dismissed"
    assert p.resolved_at is not None


def test_pending_is_not_duplicated(student, firm):
    assert consider(student, finding()) == discovery.PROPOSED
    assert consider(student, finding()) is None
    assert len(pending(student)) == 1


def test_dry_run_reports_without_writing(student, firm):
    assert discovery.consider_finding(
        student, finding(), dry_run=True
    ) == discovery.PROPOSED
    assert ContactProposal.all_objects.filter(user=student).count() == 0


# --------------------------------------------------------------------------- #
# Accept: creation through capture_discover's own contract
# --------------------------------------------------------------------------- #

def test_accept_creates_contact_with_earned_warmth(student, firm):
    consider(student, finding())
    (p,) = pending(student)
    contact = discovery.accept(p)

    assert contact is not None
    assert contact.email == "alex.banker@northbank.example"
    assert contact.firm_id == firm.id
    assert contact.source == "capture"
    # Warmth is earned through the ratchet by the one real touch, never set
    # by hand: a genuine reply lands the contact at "replied", no further.
    contact.refresh_from_db()
    assert contact.warmth == "replied"
    touches = list(Touch.objects.for_user(student).filter(contact=contact))
    assert [t.kind for t in touches] == ["reply_received"]
    # The thread marker rides the note, so later capture runs dedup against
    # this touch like any other.
    assert "[gmail:t-disc-1]" in touches[0].note
    # The touch carries the message's own time, not the tap's.
    assert touches[0].ts.date() == timezone.datetime(2026, 8, 20).date()

    p.refresh_from_db()
    assert p.status == "accepted"
    assert p.contact_id == contact.id


def test_accept_recruiting_hint_sets_recruiting_contact(student, firm):
    consider(student, finding(name="Casey Cruz, Talent Acquisition"))
    contact = discovery.accept(pending(student)[0])
    assert contact.recruiting_contact is True


def test_accept_without_hint_leaves_recruiting_unanswered(student, firm):
    consider(student, finding())
    contact = discovery.accept(pending(student)[0])
    # NULL means "nobody has said" — the three-state contract on the field.
    assert contact.recruiting_contact is None


def test_accept_is_idempotent(student, firm):
    consider(student, finding())
    (p,) = pending(student)
    first = discovery.accept(p)
    again = discovery.accept(p)
    assert again == first
    assert Contact.objects.for_user(student).count() == 1
    assert Touch.objects.for_user(student).count() == 1


def test_accept_attaches_to_contact_created_meanwhile(student, firm):
    """The user hand-added the person after the proposal appeared: accept
    attaches rather than duplicating, and fabricates no touch."""
    consider(student, finding())
    existing = Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example"
    )
    (p,) = pending(student)
    assert discovery.accept(p) == existing
    assert Contact.objects.for_user(student).count() == 1
    assert Touch.objects.for_user(student).count() == 0


def test_accept_never_resurrects_archived(student, firm):
    consider(student, finding())
    Contact.all_objects.create(
        user=student, name="Alex Banker",
        email="alex.banker@northbank.example", archived=True,
    )
    (p,) = pending(student)
    assert discovery.accept(p) is None
    p.refresh_from_db()
    assert p.status == "dismissed"
    assert Contact.objects.for_user(student).filter(archived=False).count() == 0


# --------------------------------------------------------------------------- #
# The apply_findings hook — both capture paths funnel through here
# --------------------------------------------------------------------------- #

def test_unmatched_genuine_finding_proposes(student, firm):
    result = apply_findings(student, [finding()])
    assert result.skipped_unmatched == 1
    assert result.proposals_created == 1
    assert len(pending(student)) == 1
    assert Contact.objects.for_user(student).count() == 0


def test_unmatched_bulk_finding_does_not_propose(student, firm):
    result = apply_findings(student, [finding(bulk=True, replied=False)])
    assert result.proposals_created == 0
    assert pending(student) == []


def test_unmatched_archived_match_counted(student, firm):
    Contact.all_objects.create(
        user=student, name="Alex Banker",
        email="alex.banker@northbank.example", archived=True,
    )
    result = apply_findings(student, [finding()])
    assert result.proposals_archived_match == 1
    assert result.proposals_created == 0


def test_matched_finding_never_reaches_discovery(student, firm):
    Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example"
    )
    result = apply_findings(student, [finding()])
    assert result.proposals_created == 0
    assert pending(student) == []


def test_apply_findings_dry_run_counts_but_writes_nothing(student, firm):
    result = apply_findings(student, [finding()], dry_run=True)
    assert result.proposals_created == 1
    assert ContactProposal.all_objects.filter(user=student).count() == 0


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #

def test_proposals_are_tenant_isolated(student, firm):
    other = User.objects.create_user(email="disc-other@example.com", password="x")
    consider(student, finding())
    # The same sender is independently proposable to another tenant.
    assert consider(other, finding()) == discovery.PROPOSED
    assert len(pending(student)) == 1
    assert len(pending(other)) == 1
    with pytest.raises(TenantScopeError):
        ContactProposal.objects.all()


def test_proposal_views_scope_by_user(student, firm, client):
    other = User.objects.create_user(email="disc-other2@example.com", password="x")
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(other)
    resp = client.post(reverse("crm:proposal_act", args=[p.id, "accept"]))
    assert resp.status_code == 404
    p.refresh_from_db()
    assert p.status == "pending"


# --------------------------------------------------------------------------- #
# The Today-page views
# --------------------------------------------------------------------------- #

def test_accept_view_creates_contact(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    resp = client.post(reverse("crm:proposal_act", args=[p.id, "accept"]))
    assert resp.status_code == 200
    assert Contact.objects.for_user(student).filter(
        email="alex.banker@northbank.example"
    ).exists()


def test_dismiss_view_hides_forever(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    resp = client.post(reverse("crm:proposal_act", args=[p.id, "dismiss"]))
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.status == "dismissed"
    assert Contact.objects.for_user(student).count() == 0


def test_bulk_accept(student, firm, client):
    consider(student, finding())
    consider(student, finding(
        email="jordan.lee@northbank.example", name="Jordan Lee", thread_id="t-2"
    ))
    client.force_login(student)
    resp = client.post(reverse("crm:proposals_bulk", args=["accept"]))
    assert resp.status_code == 200
    assert Contact.objects.for_user(student).count() == 2
    assert pending(student) == []


def test_bulk_dismiss(student, firm, client):
    consider(student, finding())
    consider(student, finding(
        email="jordan.lee@northbank.example", name="Jordan Lee", thread_id="t-2"
    ))
    client.force_login(student)
    resp = client.post(reverse("crm:proposals_bulk", args=["dismiss"]))
    assert resp.status_code == 200
    assert Contact.objects.for_user(student).count() == 0
    assert pending(student) == []
    assert ContactProposal.all_objects.filter(
        user=student, status="dismissed"
    ).count() == 2


def test_proposals_render_on_today(student, firm, client):
    consider(student, finding())
    client.force_login(student)
    resp = client.get(reverse("crm:week"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Found in your inbox" in body
    assert "Alex Banker" in body


# --------------------------------------------------------------------------- #
# Display-name parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,name,hint",
    [
        ("Jane Doe", "Jane Doe", ""),
        ("Jane Doe, Campus Recruiting", "Jane Doe", "Campus Recruiting"),
        ("Jane Doe | North Bank", "Jane Doe", "North Bank"),
        ("Jane Doe (she/her)", "Jane Doe", ""),
        ("Jane Doe (North Bank) - Talent Acquisition",
         "Jane Doe", "Talent Acquisition North Bank"),
        ("jane.doe", "jane.doe", ""),
    ],
)
def test_split_display_name(raw, name, hint):
    assert discovery.split_display_name(raw) == (name, hint)
