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


def test_outreach_to_a_non_firm_address_never_proposes(student, firm):
    """The outbound revision (module docstring) moved the old blanket
    refusal, not the principle behind it: sent mail to an address outside
    the firm directory is still "someone they chose not to track" — a
    vendor, a professor, a friend on gmail — and still not this feature's
    call to make. Only a directory-firm recipient clears the outbound bar
    (see the section below)."""
    assert consider(student, finding(
        replied=False, outreach_sent=True, threaded_reply=False,
        email="somebody@randomvendor.example",
    )) is None


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


def test_own_institution_domain_never_proposes(firm):
    """Verified on live data: the two junk proposals nothing else caught
    were genuine threaded replies from the user's own school's staff. A
    sender at the user's own institutional domain is a campus relationship,
    not a discovery."""
    student = User.objects.create_user(email="jimmy@school.example", password="x")
    assert consider(
        student, finding(email="housing.desk@school.example", threaded_reply=True)
    ) is None


def test_freemail_account_does_not_exclude_freemail_senders(firm):
    """A user whose own account is @gmail.com must still get proposals for
    alumni replying from gmail — the own-domain rule is institutional only."""
    student = User.objects.create_user(email="jimmy.disc@gmail.com", password="x")
    assert consider(
        student, finding(email="alum.reply@gmail.com", threaded_reply=True)
    ) == discovery.PROPOSED


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


def test_a_reply_to_a_declassified_campaign_is_never_proposed(student, firm):
    """The campaign-aware suppression gap, closed. A merge recipient who was
    never in Coverage replies; their subject IS the campaign's signature. If
    the user has classified that send "not my recruiting", proposing the
    replier would quietly re-import a person from a send the user explicitly
    declassified — and accepting the card would make them a permanent
    queue-eligible contact `crm.campaigns` can never exclude (no outbound
    touch of theirs carries the signature). An unanswered or `recruiting`
    campaign changes nothing: status quo, still proposed."""
    from crm.campaigns import normalize_subject
    from crm.models import Campaign

    panel = "Fall 2026 ICC Alumni Digital Panel Outreach"
    now = timezone.now()
    campaign = Campaign.all_objects.create(
        user=student, signature=normalize_subject(panel), label=panel,
        kind=Campaign.KIND_OTHER, first_sent=now, last_sent=now,
        recipient_count=10,
    )
    f = finding(subject=f"Re: {panel}")

    assert consider(student, f) is None
    assert pending(student) == []

    # The same reply against an unanswered campaign is proposed — suppression
    # requires the user's explicit answer, never the detector's.
    campaign.kind = Campaign.KIND_UNCLASSIFIED
    campaign.save(update_fields=["kind"])
    assert consider(student, f) == discovery.PROPOSED


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
# End to end: the live path, message dicts to a tapped contact
# --------------------------------------------------------------------------- #

def test_end_to_end_live_path(client):
    """A throwaway account, a synthetic Gmail batch through the REAL
    classify -> apply -> propose -> view-accept/dismiss -> re-detect loop,
    then deleted. The whole feature in one pass."""
    from capture import gmail_live

    user = User.objects.create_user(email="e2e-throwaway@example.test", password="x")
    firm = Firm.objects.create(
        slug="e2e-firm", name="E2E Firm", domains=["e2efirm.example"]
    )
    own = "student@example.test"

    def msg(headers, snippet="", thread="t-1"):
        return {
            "threadId": thread, "snippet": snippet,
            "internalDate": "1755600000000",
            "payload": {"headers": [
                {"name": k, "value": v} for k, v in headers.items()
            ]},
        }

    batch = [
        msg({"From": "Pat Analyst <pat.analyst@e2efirm.example>", "To": own,
             "Subject": "Re: quick intro", "In-Reply-To": "<mine@ex>"},
            snippet="Happy to find time.", thread="t-1"),
        msg({"From": "Riley Recruiter <riley.recruiter@e2efirm.example>",
             "To": own, "Subject": "Sophomore Series: join us",
             "List-Id": "<blast.example>", "Precedence": "bulk"},
            thread="t-2"),
        msg({"From": "Alum Friend <alum.e2e@gmail.com>", "To": own,
             "Subject": "Re: USC coffee", "In-Reply-To": "<mine2@ex>"},
            thread="t-3"),
        msg({"From": "Digest <digest@mail.beehiiv.com>", "To": own,
             "Subject": "This week"}, thread="t-4"),
    ]
    findings = [
        f for f in (gmail_live._classify_message(own, m) for m in batch) if f
    ]
    result = apply_findings(user, findings)
    assert result.proposals_created == 2  # the blast and the ESP never qualify

    rows = pending(user)
    firm_p = next(p for p in rows if p.firm_id == firm.id)
    personal_p = next(p for p in rows if p.firm_id is None)

    client.force_login(user)
    assert client.post(
        reverse("crm:proposal_act", args=[firm_p.id, "accept"])
    ).status_code == 200
    contact = Contact.objects.for_user(user).get(email="pat.analyst@e2efirm.example")
    assert contact.warmth == "replied"
    touch = Touch.objects.for_user(user).get(contact=contact)
    assert touch.kind == "reply_received" and "[gmail:t-1]" in touch.note

    assert client.post(
        reverse("crm:proposal_act", args=[personal_p.id, "dismiss"])
    ).status_code == 200

    # Same batch again: the accept deduped, the dismiss held.
    again = apply_findings(user, findings)
    assert again.proposals_created == 0
    assert pending(user) == []

    user_id = user.id
    user.delete()
    assert not Contact.all_objects.filter(user_id=user_id).exists()
    assert not ContactProposal.all_objects.filter(user_id=user_id).exists()


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


# --------------------------------------------------------------------------- #
# What the person replied TO — the card's triggering context.
#
# The bug these pin: a reply to the founder's club mail merge ("Fall 2026 ICC
# Alumni Digital Panel Outreach", see crm.models.Campaign) and a reply to
# genuine networking outreach produced IDENTICAL cards. Campaign-aware
# suppression only fires when the campaign was DETECTED, which needs the
# outbound sends in the database — so when it doesn't fire, the subject line
# is the only thing that tells a club panelist from a banker.
# --------------------------------------------------------------------------- #

def test_threaded_reply_stores_the_subject_it_replied_to(student, firm):
    consider(student, finding(subject="Re: Fall 2026 ICC Alumni Digital Panel Outreach"))
    (p,) = pending(student)
    # Reply prefix stripped: the thread is the club send, not "Re: the club
    # send". `threaded_reply` is kept as its own fact so the card can tell a
    # missing subject apart from a message that was never a reply.
    assert p.thread_subject == "Fall 2026 ICC Alumni Digital Panel Outreach"
    assert p.threaded_reply is True


def test_a_firm_first_contact_stores_no_replied_to_subject(student, firm):
    """"Replied to" is a false sentence about someone who wrote to you first.
    The subject is not repurposed into a line that misdescribes it."""
    consider(student, finding(threaded_reply=False, subject="Sophomore Series"))
    (p,) = pending(student)
    assert p.thread_subject == ""
    assert p.threaded_reply is False


def test_a_reply_with_no_subject_header_records_the_reply_and_no_subject(
    student, firm
):
    """Degrade honestly: the reply is still a fact, the subject is not
    invented, and the two are stored separately so the card can say so."""
    consider(student, finding(subject=""))
    (p,) = pending(student)
    assert p.thread_subject == ""
    assert p.threaded_reply is True


@pytest.mark.parametrize(
    "raw,shown",
    [
        ("Re: Fall 2026 Outreach", "Fall 2026 Outreach"),
        ("RE: Re: Fwd: Fall 2026 Outreach", "Fall 2026 Outreach"),
        ("Re[2]: Fall 2026 Outreach", "Fall 2026 Outreach"),
        ("回复: 秋招", "秋招"),
        # Case, digits and punctuation survive — this is a line a human
        # reads, not crm.campaigns' blunt grouping key.
        ("Q3 2026 Analyst Program - Intro", "Q3 2026 Analyst Program - Intro"),
        ("", ""),
        (None, ""),
    ],
)
def test_display_subject(raw, shown):
    assert discovery.display_subject(raw) == shown


def test_the_card_names_the_thread_the_person_replied_to(student, firm, client):
    consider(student, finding(subject="Re: Fall 2026 ICC Alumni Digital Panel Outreach"))
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Replied to:" in body
    assert "Fall 2026 ICC Alumni Digital Panel Outreach" in body


def test_the_card_says_so_when_there_is_no_subject_to_show(student, firm, client):
    consider(student, finding(subject=""))
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Replied to an email you sent" in body
    assert "No subject line on it" in body
    # And never a bare label with nothing after it. Asserted on the span the
    # subject would render INTO rather than on the label text or the bare
    # class name: `_styles.html` is inlined into this same page and both its
    # comment and its rule mention "Replied to:" / `act-replied-subj`, so a
    # looser check would pass for the wrong reason.
    assert '<span class="act-replied-subj">' not in body


# --------------------------------------------------------------------------- #
# Undo a dismissal, and the durable restore surface.
# --------------------------------------------------------------------------- #

def test_dismiss_offers_an_undo_in_the_same_swap(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    body = client.post(
        reverse("crm:proposal_act", args=[p.id, "dismiss"])
    ).content.decode()
    assert "Dismissed Alex Banker." in body
    assert reverse("crm:proposals_undo") in body


def test_the_undo_offer_does_not_survive_the_next_render(student, firm, client):
    """One-shot by construction: the offer rides in the response to the
    dismissal and nowhere else, so a later action or a reload cannot show a
    stale Undo pointing at a decision the user has already moved past."""
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    client.post(reverse("crm:proposal_act", args=[p.id, "dismiss"]))
    body = client.get(reverse("crm:week")).content.decode()
    assert "Dismissed Alex Banker." not in body


def test_undo_puts_the_person_back_on_today(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    client.post(reverse("crm:proposal_act", args=[p.id, "dismiss"]))
    resp = client.post(reverse("crm:proposals_undo"), {"ids": str(p.id)})
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.status == "pending"
    assert p.resolved_at is None
    assert "Alex Banker" in resp.content.decode()


def test_bulk_dismiss_offers_one_undo_for_the_whole_batch(student, firm, client):
    consider(student, finding())
    consider(student, finding(
        email="jordan.lee@northbank.example", name="Jordan Lee", thread_id="t-2"
    ))
    client.force_login(student)
    body = client.post(
        reverse("crm:proposals_bulk", args=["dismiss"])
    ).content.decode()
    assert "Dismissed 2 people." in body
    ids = ",".join(
        str(p.id) for p in ContactProposal.objects.for_user(student).order_by("id")
    )
    resp = client.post(reverse("crm:proposals_undo"), {"ids": ids})
    assert resp.status_code == 200
    assert len(pending(student)) == 2


def test_undo_is_scoped_to_the_signed_in_user(student, firm, client):
    other = User.objects.create_user(email="disc-other3@example.com", password="x")
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    client.force_login(other)
    client.post(reverse("crm:proposals_undo"), {"ids": str(p.id)})
    p.refresh_from_db()
    assert p.status == "dismissed"


def test_undo_never_reverses_an_accept(student, firm, client):
    """The ids arrive from the client, so the status filter is the real
    guard: an accepted proposal is real work and undo must not eat it."""
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    client.post(reverse("crm:proposal_act", args=[p.id, "accept"]))
    client.post(reverse("crm:proposals_undo"), {"ids": str(p.id)})
    p.refresh_from_db()
    assert p.status == "accepted"


def test_undo_rejects_junk_ids(student, firm, client):
    client.force_login(student)
    assert client.post(
        reverse("crm:proposals_undo"), {"ids": "abc, ,"}
    ).status_code == 400


# --- The Settings restore surface ------------------------------------------ #

def test_settings_lists_dismissed_people_with_their_evidence(student, firm, client):
    consider(student, finding(subject="Re: Fall 2026 ICC Alumni Digital Panel Outreach"))
    (p,) = pending(student)
    discovery.dismiss(p)
    client.force_login(student)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert "Dismissed From Your Inbox" in body
    assert "Alex Banker" in body
    assert "Fall 2026 ICC Alumni Digital Panel Outreach" in body
    assert reverse("crm:proposal_restore", args=[p.id]) in body


def test_settings_hides_the_card_when_nothing_was_dismissed(student, firm, client):
    consider(student, finding())
    client.force_login(student)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert "Dismissed From Your Inbox" not in body


def test_restore_from_settings_returns_the_person_to_pending(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    client.force_login(student)
    resp = client.post(reverse("crm:proposal_restore", args=[p.id]), follow=True)
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.status == "pending"
    assert len(pending(student)) == 1


def test_restore_is_scoped_by_user(student, firm, client):
    other = User.objects.create_user(email="disc-other4@example.com", password="x")
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    client.force_login(other)
    assert client.post(
        reverse("crm:proposal_restore", args=[p.id])
    ).status_code == 404
    p.refresh_from_db()
    assert p.status == "dismissed"


# --- Restore reconciles rather than duplicating ---------------------------- #

def test_restore_reconciles_onto_a_contact_added_in_the_meantime(student, firm):
    """Dismissal is permanent, so the gap before a restore is unbounded and
    the person may have arrived by another door. Restoring must not put a
    "Not in your network" card on Today for somebody who is in it."""
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    hand_added = Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example",
    )
    outcome, contact = discovery.restore(p)
    assert outcome == discovery.ALREADY_A_CONTACT
    assert contact == hand_added
    p.refresh_from_db()
    assert p.status == "accepted"
    assert p.contact_id == hand_added.id
    assert pending(student) == []
    assert Contact.objects.for_user(student).count() == 1


def test_restore_matches_on_name_when_the_address_differs(student, firm):
    """The same match rule accept and consider_finding use — email first,
    then normalized name — so the three cannot disagree about who is a
    duplicate."""
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    Contact.all_objects.create(
        user=student, name="alex  banker", email="a.banker@personal.example",
    )
    outcome, _ = discovery.restore(p)
    assert outcome == discovery.ALREADY_A_CONTACT
    assert Contact.objects.for_user(student).count() == 1


def test_restore_refuses_to_resurrect_an_archived_contact(student, firm):
    """capture_discover's rule, unchanged: archiving was a deliberate user
    action and a restore is not consent to undo it."""
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example",
        archived=True,
    )
    outcome, contact = discovery.restore(p)
    assert outcome == discovery.RESTORE_ARCHIVED
    assert contact is None
    p.refresh_from_db()
    assert p.status == "dismissed"


def test_restore_is_idempotent(student, firm):
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    assert discovery.restore(p)[0] == discovery.RESTORED
    assert discovery.restore(p)[0] == discovery.RESTORE_NOOP
    p.refresh_from_db()
    assert p.status == "pending"


def test_a_scan_still_never_re_proposes_a_dismissed_person(student, firm):
    """The guarantee undo must not weaken: restoring is a USER action, and
    nothing automatic ever writes `pending` back. A dismissed row still blocks
    re-proposal on the mere existence of the row, whatever its status."""
    consider(student, finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    assert consider(student, finding(thread_id="t-later")) is None
    p.refresh_from_db()
    assert p.status == "dismissed"
    assert pending(student) == []


def test_a_restored_person_can_be_accepted_normally(student, firm, client):
    consider(student, finding())
    (p,) = pending(student)
    client.force_login(student)
    client.post(reverse("crm:proposal_act", args=[p.id, "dismiss"]))
    client.post(reverse("crm:proposals_undo"), {"ids": str(p.id)})
    client.post(reverse("crm:proposal_act", args=[p.id, "accept"]))
    p.refresh_from_db()
    assert p.status == "accepted"
    contact = Contact.objects.for_user(student).get(
        email="alex.banker@northbank.example"
    )
    # Warmth still earned through the ratchet, never gifted by the round trip.
    assert Touch.objects.for_user(student).filter(contact=contact).exists()


def test_the_export_carries_the_replied_to_subject(student, firm):
    from accounts import services as account_services

    consider(student, finding(subject="Re: Fall 2026 ICC Alumni Digital Panel Outreach"))
    csv = account_services.contact_proposals_csv(student)
    assert "thread_subject" in csv.splitlines()[0]
    assert "Fall 2026 ICC Alumni Digital Panel Outreach" in csv


# --------------------------------------------------------------------------- #
# The outbound revision: people the user deliberately reached out to.
#
# The gap these pin, measured on the founder's real mailbox (read-only,
# 2026-08-25): two days, ~50 personalised coffee-chat requests to bankers at
# directory firms — and Coverage captured exactly the two who replied within
# minutes, because this module demanded inbound evidence. The other people he
# chose to build relationships with had no card and no follow-up clock. The
# bar for outbound is HIGHER than inbound (firm domain only, no threaded
# escape, merge- and bounce-guarded); everything the inbound ladder refuses
# stays refused.
# --------------------------------------------------------------------------- #

def outreach_finding(**over):
    """The canonical outbound coffee-chat request: the user's own sent mail
    to a banker at a directory firm, nothing back yet."""
    base = {
        "name": "Alex Banker",
        "email": "alex.banker@northbank.example",
        "found": True,
        "bounced": False,
        "outreach_sent": True,
        "replied": False,
        "chat_status": "none",
        "bulk": False,
        "threaded_reply": False,
        "subject": "USC | Beta Sigma | North Bank - USC Student Coffee Chat Request",
        "evidence": "Sent: USC | Beta Sigma | North Bank - USC Student Coffee Chat Request",
        "thread_id": "t-out-1",
        "occurred_at": "2026-08-24T15:08:00+00:00",
    }
    base.update(over)
    return base


def test_outreach_to_a_firm_banker_proposes(student, firm):
    assert consider(student, outreach_finding()) == discovery.PROPOSED
    (p,) = pending(student)
    assert p.firm_id == firm.id
    assert p.evidence_kind == "outreach"
    assert p.threaded_reply is False
    # The card leads with the user's own act and names the thread.
    assert p.thread_subject == (
        "USC | Beta Sigma | North Bank - USC Student Coffee Chat Request"
    )
    assert p.evidence.startswith("You wrote to them")
    # A proposal writes NOTHING to the CRM.
    assert Contact.objects.for_user(student).count() == 0
    assert Touch.objects.for_user(student).count() == 0


def test_accepting_an_outreach_proposal_starts_the_clock_not_the_warmth(
    student, firm
):
    """Accept logs one `outreach` touch at the send's own time through the
    normal ratchet: the contact lands COLD (no fabricated warmth — the
    counterparty has done nothing) and the cadence engine's follow-up clock
    runs from the real send date."""
    consider(student, outreach_finding())
    (p,) = pending(student)
    contact = discovery.accept(p)
    assert contact is not None
    contact.refresh_from_db()
    assert contact.warmth == "cold"
    touches = list(Touch.objects.for_user(student).filter(contact=contact))
    assert [t.kind for t in touches] == ["outreach"]
    assert "[gmail:t-out-1]" in touches[0].note
    assert touches[0].ts.date() == timezone.datetime(2026, 8, 24).date()
    assert "Found in your sent mail" in contact.notes


def test_outreach_with_a_calendar_invite_still_logs_only_outreach(student, firm):
    """An .ics the USER attached is still only the user's own act — logging
    chat_scheduled off it would gift warmth `replied` to somebody who has
    never typed a word."""
    consider(student, outreach_finding(chat_status="scheduled"))
    (p,) = pending(student)
    assert p.evidence_kind == "outreach"


def test_outreach_to_role_accounts_never_proposes(student, firm):
    for local in ("careers", "campus.recruiting", "info", "events"):
        assert consider(student, outreach_finding(
            email=f"{local}@northbank.example"
        )) is None


def test_outreach_that_bounced_in_the_same_batch_never_proposes(student, firm):
    """Both real bounces in the founder's 2026-08-24 burst arrived seconds
    behind their sends, in the same batch: the send finding must not
    propose a person the bounce proves does not exist."""
    send = outreach_finding()
    bounce = outreach_finding(
        bounced=True, outreach_sent=False,
        evidence="Bounced", thread_id="t-out-1",
    )
    result = apply_findings(student, [send, bounce])
    assert result.proposals_created == 0
    assert pending(student) == []


def test_a_merge_shaped_burst_never_proposes_but_personalised_sends_do(
    student, firm
):
    """More than MERGE_RECIPIENT_LIMIT distinct recipients on ONE normalized
    subject is a mail merge (the ICC panel send was 201); the founder's real
    bursts personalise the subject per person and pass."""
    merge = [
        outreach_finding(
            email=f"person{i}@northbank.example", name=f"Person {i}",
            subject="Fall 2026 ICC Alumni Digital Panel Outreach",
            thread_id=f"t-merge-{i}",
        )
        for i in range(5)
    ]
    personal = outreach_finding(
        email="solo.banker@northbank.example", name="Solo Banker",
        subject="USC | Chess Club | North Bank - USC Student Coffee Chat Request",
        thread_id="t-solo",
    )
    result = apply_findings(student, merge + [personal])
    assert result.proposals_created == 1
    (p,) = pending(student)
    assert p.email == "solo.banker@northbank.example"


def test_outreach_from_a_declassified_campaign_never_proposes(student, firm):
    from crm.campaigns import normalize_subject
    from crm.models import Campaign

    panel = "Fall 2026 ICC Alumni Digital Panel Outreach"
    now = timezone.now()
    Campaign.all_objects.create(
        user=student, signature=normalize_subject(panel), label=panel,
        kind=Campaign.KIND_OTHER, first_sent=now, last_sent=now,
        recipient_count=201,
    )
    assert consider(student, outreach_finding(subject=panel)) is None


def test_outreach_from_an_unanswered_campaign_waits_for_the_answer(
    student, firm
):
    """The asymmetry with inbound, pinned: a REPLY from an unanswered
    campaign still proposes (a human engaged), but outreach-only evidence
    from one is exactly the mass send the open question is about."""
    from crm.campaigns import normalize_subject
    from crm.models import Campaign

    panel = "Fall 2026 ICC Alumni Digital Panel Outreach"
    now = timezone.now()
    campaign = Campaign.all_objects.create(
        user=student, signature=normalize_subject(panel), label=panel,
        kind=Campaign.KIND_UNCLASSIFIED, first_sent=now, last_sent=now,
        recipient_count=10,
    )
    assert consider(student, outreach_finding(subject=panel)) is None

    # The user says the campaign IS their recruiting: its recipients at firm
    # domains are their recruiting network, and the send now proposes.
    campaign.kind = Campaign.KIND_RECRUITING
    campaign.save(update_fields=["kind"])
    assert consider(
        student, outreach_finding(subject=panel)
    ) == discovery.PROPOSED


def test_outreach_never_duplicates_and_dismiss_still_holds(student, firm):
    assert consider(student, outreach_finding()) == discovery.PROPOSED
    assert consider(student, outreach_finding()) is None
    (p,) = pending(student)
    discovery.dismiss(p)
    assert consider(student, outreach_finding(thread_id="t-later")) is None
    assert pending(student) == []


def test_outreach_to_an_archived_contact_is_reported_not_resurrected(
    student, firm
):
    Contact.all_objects.create(
        user=student, name="Alex Banker",
        email="alex.banker@northbank.example", archived=True,
    )
    assert consider(student, outreach_finding()) == discovery.ARCHIVED_MATCH
    assert pending(student) == []


def test_outreach_to_an_existing_contact_never_proposes(student, firm):
    Contact.all_objects.create(
        user=student, name="Alex Banker", email="alex.banker@northbank.example"
    )
    assert consider(student, outreach_finding()) is None


def test_outreach_dry_run_reports_without_writing(student, firm):
    assert discovery.consider_finding(
        student, outreach_finding(), dry_run=True
    ) == discovery.PROPOSED
    assert ContactProposal.all_objects.filter(user=student).count() == 0


def test_the_card_says_you_reached_out(student, firm, client):
    consider(student, outreach_finding())
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()
    assert "You reached out:" in body
    assert "USC Student Coffee Chat Request" in body
    # And never the reply sentence: "Replied to:" appears in this page only
    # inside _styles.html's own commentary, not on any card. Asserted on the
    # rendered paragraph, since the class rides both sentences.
    assert '>Replied to: <span class="act-replied-subj">' not in body


# --------------------------------------------------------------------------- #
# The upgrade: outreach proposal, then the person writes back.
# --------------------------------------------------------------------------- #

def test_a_reply_upgrades_a_pending_outreach_proposal_in_place(student, firm):
    """Batch order must not decide what the card says: the sent mail is
    scanned before the reply that answered it. One row, upgraded evidence,
    and accept logs the reply — warmth `replied`, earned."""
    result = apply_findings(student, [
        outreach_finding(),
        finding(
            subject="Re: USC | Beta Sigma | North Bank - USC Student Coffee Chat Request",
            thread_id="t-out-1",
        ),
    ])
    assert result.proposals_created == 1
    assert result.proposals_upgraded == 1
    (p,) = pending(student)
    assert p.evidence_kind == "reply_received"
    assert p.threaded_reply is True
    assert p.thread_subject == (
        "USC | Beta Sigma | North Bank - USC Student Coffee Chat Request"
    )
    contact = discovery.accept(p)
    contact.refresh_from_db()
    assert contact.warmth == "replied"
    assert [
        t.kind for t in Touch.objects.for_user(student).filter(contact=contact)
    ] == ["reply_received"]


def test_a_bulk_blast_never_upgrades_an_outreach_proposal(student, firm):
    consider(student, outreach_finding())
    assert consider(student, finding(bulk=True, thread_id="t-out-1")) is None
    (p,) = pending(student)
    assert p.evidence_kind == "outreach"


def test_a_reply_never_upgrades_a_dismissed_proposal(student, firm):
    consider(student, outreach_finding())
    (p,) = pending(student)
    discovery.dismiss(p)
    assert consider(student, finding(thread_id="t-out-1")) is None
    p.refresh_from_db()
    assert p.status == "dismissed"
    assert p.evidence_kind == "outreach"


# --------------------------------------------------------------------------- #
# A threaded "reply" that never addressed the user.
#
# Live case (2026-08-25): a West Monroe coordinator's "RE:" follow-up to
# their own mass invite carried In-Reply-To and named only the firm's own
# people on To:/Cc:. A reply pointer proves someone hit Reply; only To:/Cc:
# proves it was aimed at the user.
# --------------------------------------------------------------------------- #

def test_a_threaded_reply_not_addressed_to_the_user_never_proposes(
    student, firm
):
    assert consider(student, finding(
        email="coordinator@westmonroe.example",
        addressed_to_user=False,
    )) is None


def test_findings_without_the_addressed_fact_keep_behaving_as_before(
    student, firm
):
    """Every finding written before `addressed_to_user` existed carries no
    such key — absence is unknown, not refusal."""
    assert consider(student, finding(
        email="alum@gmail.com", threaded_reply=True
    )) == discovery.PROPOSED


# --------------------------------------------------------------------------- #
# The noise roster — every real sender from the founder's own 3-day window,
# and the layer that stops each. None may ever become a networking proposal.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "email,extra",
    [
        # ATS / application mail: noreply localparts, whatever the domain.
        ("no-reply@citadel.com", {}),
        ("noreply@campuscareers.bofa.com", {}),
        # ESP / job-board sending domains.
        ("handshake@mail.joinhandshake.com", {}),
        ("handshake@g.joinhandshake.com", {}),
        ("tis.presidents@301933.mailchimpapp.com", {}),
        # Role-account localparts.
        ("hello@anythings.app", {}),
        # No firm match, no reply pointer: campus mail, receipts, vendors,
        # competitor marketing.
        ("notifications-noreply@mail.brightspace.usc.edu", {}),
        ("mobileorder@transactcampus.com", {}),
        ("spamdigest@usc.edu", {}),
        ("uscpublicsafety@msg.adm.usc.edu", {}),
        ("ugcareers@marshall.usc.edu", {}),
        ("VSPVisionCareVCM@e.vsp.com", {}),
        ("streetsmart@streetsmartcareers.com", {}),
        ("julia.hornstein@mail.recruitu.com", {}),
        ("alerts@factset.com", {}),
        # The ICC application blast: a genuine human sender, 16 students on
        # To:, no reply pointer, gmail domain — no firm, no thread.
        ("laicc.usc@gmail.com", {"threaded_reply": False}),
    ],
)
def test_the_noise_roster_is_refused(student, firm, email, extra):
    over = {"threaded_reply": False, **extra}
    assert consider(student, finding(email=email, **over)) is None
    assert pending(student) == []


def test_bulk_flagged_mass_invites_are_refused_before_anything_else(
    student, firm
):
    """The West Monroe "Sophomore Series" shape: whatever else is true of
    the sender, `bulk` refuses first."""
    assert consider(student, finding(
        email="cbaenen@westmonroe.example", bulk=True
    )) is None
