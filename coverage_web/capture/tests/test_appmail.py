"""Application-mail detection (capture/appmail.py, the apply_findings hook,
and the Today-page card).

The centre of gravity is the REFUSALS, for the same reason as
test_discovery.py: this feature reads a student's mail and offers to change
their application record, so every rule it draws has a test here. Marketing
never proposes, a human reply never proposes, an unresolvable role is
reported rather than guessed, dismiss is permanent, nothing at all is
written to UserOpportunity until the tap, and the paid model is never called
for mail the free layer already excluded.

`transaction=True` throughout, matching the other capture tests: apply_findings
reaches crm.services.log_touch, which opens its own psycopg connection.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from analytics.models import UserOpportunity
from capture import appmail
from capture.gmail import apply_findings
from capture.models import ApplicationEvent
from coverage_web.tenancy import TenantScopeError
from directory.models import Firm, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def student():
    return User.objects.create_user(email="appmail-student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", domains=["northbank.example"]
    )


@pytest.fixture
def role(firm):
    return Opportunity.objects.create(
        firm=firm, title="2027 Investment Banking Summer Analyst",
        status="open", url="https://northbank.example/jobs/1",
    )


def ats(**over):
    """A Greenhouse-shaped application confirmation: the canonical case.

    Real header shape — `no-reply@` localpart on the ATS's own sending
    domain, `bulk: True` because `capture.inbound` correctly calls it bulk
    (List-Unsubscribe + no-reply sender), the firm in the display name, the
    role in the subject.
    """
    base = {
        "name": "North Bank Careers",
        "email": "no-reply@us.greenhouse-mail.io",
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": False,
        "chat_status": "none",
        "bulk": True,
        "bulk_reasons": "sender is an unattended address (no-reply@us.greenhouse-mail.io)",
        "subject": "Thank you for applying to North Bank — 2027 Investment Banking Summer Analyst",
        "snippet": "We have received your application and will be in touch.",
        "evidence": "Bulk/automated email",
        "thread_id": "t-app-1",
        "occurred_at": "2026-08-20T09:00:00+00:00",
    }
    base.update(over)
    return base


def only(user) -> ApplicationEvent:
    rows = list(ApplicationEvent.objects.for_user(user))
    assert len(rows) == 1, rows
    return rows[0]


# --------------------------------------------------------------------------- #
# What each ATS actually sends
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("email,subject,snippet,expected", [
    # Greenhouse — confirmation.
    ("no-reply@us.greenhouse-mail.io",
     "Thank you for applying to North Bank",
     "Your application has been received.",
     ApplicationEvent.APPLIED),
    # Workday — the tenant-per-localpart sender shape.
    ("northbank@myworkday.com",
     "North Bank: Your application was submitted",
     "Thanks for your interest.",
     ApplicationEvent.APPLIED),
    # Lever.
    ("no-reply@hire.lever.co",
     "North Bank application confirmation",
     "We received your application.",
     ApplicationEvent.APPLIED),
    # iCIMS.
    ("donotreply@talent.icims.com",
     "Your application to North Bank has been received",
     "",
     ApplicationEvent.APPLIED),
    # HackerRank — the vendor IS the stage; no subject phrase needed.
    ("noreply@hackerrank.com",
     "North Bank invites you to a test",
     "Please complete within 5 days.",
     ApplicationEvent.ASSESSMENT),
    # Codility, through the firm's own ATS wording.
    ("no-reply@us.greenhouse-mail.io",
     "North Bank — invitation to complete an online assessment",
     "",
     ApplicationEvent.ASSESSMENT),
    # HireVue.
    ("no-reply@hirevue.com",
     "Your North Bank interview",
     "Record your responses by Friday.",
     ApplicationEvent.VIDEO_INTERVIEW),
    # A named HireVue through the ATS.
    ("no-reply@myworkday.com",
     "North Bank: complete your HireVue digital interview",
     "",
     ApplicationEvent.VIDEO_INTERVIEW),
    # Live interview scheduling.
    ("campus@northbank.example",
     "North Bank interview invitation — 2027 Summer Analyst",
     "",
     ApplicationEvent.INTERVIEW),
    # Assessment centre reads as a real interview stage.
    ("no-reply@tal.net",
     "North Bank assessment centre invitation",
     "",
     ApplicationEvent.INTERVIEW),
    # Rejection: the subject is deliberately neutral, the body is decisive.
    ("no-reply@us.greenhouse-mail.io",
     "Your application to North Bank",
     "After careful review we regret to inform you that we will not be "
     "moving forward with your application at this time.",
     ApplicationEvent.REJECTED),
    # Rejection, second phrasing.
    ("no-reply@myworkday.com",
     "Update on your North Bank application",
     "We have decided to move forward with other candidates for this role.",
     ApplicationEvent.REJECTED),
    # Offer.
    ("campus@northbank.example",
     "Your North Bank offer",
     "We are delighted to offer you a place on the 2027 Summer Analyst "
     "programme.",
     ApplicationEvent.OFFER),
])
def test_each_event_kind_is_typed_by_the_free_layer(email, subject, snippet, expected):
    """No API key, no network, no model — every one of these is a phrase or
    a sender the deterministic layer already knows."""
    detection = appmail.detect(
        ats(email=email, subject=subject, snippet=snippet), allow_ai=False
    )
    assert detection.gated is True
    assert detection.event_type == expected


# --------------------------------------------------------------------------- #
# What must never be detected
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("finding", [
    # A newsletter. Bulk, no-reply, and nothing about an application.
    ats(email="newsletter@substack.com", subject="This week in markets",
        snippet="Five charts on the rates repricing.", name="Markets Weekly"),
    # Marketing from a firm's own domain.
    ats(email="marketing@northbank.example",
        subject="North Bank insights: the year ahead",
        snippet="Read our 2027 outlook.", name="North Bank Insights"),
    # A job ALERT from a real ATS — the sender gates in, the subject does not.
    ats(email="no-reply@us.greenhouse-mail.io",
        subject="New roles at North Bank this week",
        snippet="3 new postings match your saved search."),
    # An event invitation. "Programme" is not "application".
    ats(email="events@northbank.example",
        subject="North Bank Sophomore Series: an evening with our traders",
        snippet="Join us on campus."),
])
def test_non_application_bulk_mail_is_never_gated(finding):
    assert appmail.detect(finding, allow_ai=False).gated is False


def test_a_genuine_human_reply_is_never_gated():
    """The escape hatch `capture.inbound` draws holds here too: a real
    banker typing a real answer is not bulk and is not a no-reply address,
    so the gate never opens even though the subject says "application"."""
    human = ats(
        name="Alex Banker", email="alex.banker@northbank.example",
        bulk=False, replied=True, threaded_reply=True,
        subject="Re: your application — happy to chat about it",
        snippet="Happy to talk it through, are you free Thursday?",
    )
    human.pop("bulk_reasons")
    assert appmail.detect(human, allow_ai=False).gated is False


def test_the_users_own_sent_mail_is_never_gated():
    sent = ats(outreach_sent=True, replied=False, bulk=False)
    assert appmail.detect(sent, allow_ai=False).gated is False


def test_a_bounce_is_about_delivery_not_about_an_application():
    assert appmail.detect(ats(bounced=True), allow_ai=False).gated is False


def test_a_bare_unfortunately_never_closes_an_application():
    """The most dangerous false positive this module could produce.
    Confirmations say "unfortunately we cannot respond to every applicant";
    reading that as a rejection would mark a live application Done."""
    detection = appmail.detect(ats(
        subject="Thank you for applying to North Bank",
        snippet="Unfortunately we are not able to respond to every applicant "
                "individually.",
    ), allow_ai=False)
    assert detection.event_type == ApplicationEvent.APPLIED


# --------------------------------------------------------------------------- #
# Firm and role resolution
# --------------------------------------------------------------------------- #

def test_a_short_firm_name_is_not_matched_on_a_syllable(student):
    """`Citi` lives inside `Citizens`. n-grams, not substrings — a card that
    names the wrong bank is worse than no card at all."""
    Firm.objects.create(slug="citi", name="Citi", domains=[])
    resolver = appmail.Resolver(student)
    found = appmail._ngram_firm("Your application to Citizens Advice", resolver.names)
    assert found is None


def test_the_longest_firm_ngram_wins(student):
    Firm.objects.create(slug="bain-company", name="Bain & Company")
    Firm.objects.create(slug="bain-capital", name="Bain Capital")
    resolver = appmail.Resolver(student)
    firm, _text = appmail._ngram_firm(
        "Thank you for applying to Bain Capital", resolver.names
    )
    assert firm.slug == "bain-capital"


def test_the_senders_own_domain_beats_the_subject(student, firm):
    other = Firm.objects.create(slug="south-bank", name="South Bank")
    resolver = appmail.Resolver(student)
    found, _text = appmail.resolve_firm(
        ats(email="campus@northbank.example",
            subject="South Bank mentioned us — your application"),
        resolver,
    )
    assert found.pk == firm.pk
    assert other.pk != firm.pk


def test_role_title_strips_the_message_boilerplate():
    title = appmail.role_title(
        "Thank you for applying to North Bank — 2027 Investment Banking "
        "Summer Analyst", "North Bank",
    )
    assert "applying" not in title.lower()
    assert "Investment Banking" in title


# --------------------------------------------------------------------------- #
# The hook: what apply_findings does with it
# --------------------------------------------------------------------------- #

def test_a_confirmation_proposes_and_writes_nothing_to_the_pipeline(student, role):
    result = apply_findings(student, [ats()])

    assert result.app_events_proposed == 1
    row = only(student)
    assert row.event_type == ApplicationEvent.APPLIED
    assert row.target_status == "submitted"
    assert row.opportunity_id == role.id
    assert row.status == ApplicationEvent.STATUS_PENDING
    assert row.detected_by == "rules"
    # The subject, and only the subject — never the snippet.
    assert row.evidence.startswith("Thank you for applying")
    assert "will be in touch" not in row.evidence
    # THE CONTRACT: nothing in the pipeline until the tap.
    assert not UserOpportunity.all_objects.filter(user=student).exists()


def test_an_ambiguous_role_is_reported_and_never_carded(student, firm):
    """Two open roles at the firm and a subject that names neither. The
    matcher refuses, so this module refuses — `directory.applications`'
    rule, held rather than bent."""
    for n in (1, 2):
        Opportunity.objects.create(
            firm=firm, title=f"2027 Global Markets Desk {n} Summer Analyst",
            status="open", url=f"https://northbank.example/jobs/{n}",
        )
    result = apply_findings(student, [ats(subject="Thank you for applying to North Bank")])

    assert result.app_events_proposed == 0
    assert result.app_events_unresolved == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


def test_an_unknown_firm_is_reported_and_never_carded(student, role):
    result = apply_findings(student, [ats(
        name="Unknown Partners",
        subject="Thank you for applying to Unknown Partners",
    )])
    assert result.app_events_unresolved == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


def test_the_same_message_never_proposes_twice(student, role):
    apply_findings(student, [ats()])
    result = apply_findings(student, [ats()])

    assert result.app_events_proposed == 0
    only(student)  # still exactly one


def test_the_ats_renotifying_the_same_event_proposes_once(student, role):
    """Workday confirms an application and then reminds you about it. Two
    messages, two subjects, one thing that happened."""
    apply_findings(student, [ats()])
    result = apply_findings(student, [ats(
        thread_id="t-app-2",
        subject="Reminder: your application to North Bank was received",
    )])

    assert result.app_events_proposed == 0
    only(student)


def test_a_later_stage_gets_its_own_card(student, role):
    apply_findings(student, [ats()])
    apply_findings(student, [ats(
        thread_id="t-app-3",
        email="no-reply@hirevue.com",
        subject="North Bank — your video interview",
    )])
    kinds = set(
        ApplicationEvent.objects.for_user(student).values_list("event_type", flat=True)
    )
    assert kinds == {ApplicationEvent.APPLIED, ApplicationEvent.VIDEO_INTERVIEW}


def test_a_role_already_past_the_stage_is_not_re_proposed(student, role):
    UserOpportunity.all_objects.create(
        user=student, opportunity=role, applied_status="interview"
    )
    result = apply_findings(student, [ats()])

    assert result.app_events_proposed == 0
    assert result.app_events_already == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


def test_a_dry_run_reports_without_writing(student, role):
    result = apply_findings(student, [ats()], dry_run=True)
    assert result.app_events_proposed == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


# --------------------------------------------------------------------------- #
# The tap
# --------------------------------------------------------------------------- #

def test_accept_writes_the_stage_the_mail_supports(student, role):
    apply_findings(student, [ats()])
    row = only(student)
    appmail.accept(row)

    uo = UserOpportunity.objects.for_user(student).get(opportunity=role)
    assert uo.applied_status == "submitted"
    assert uo.dismissed is False
    # The mail's own date, not the tap's.
    assert uo.applied_at.isoformat().startswith("2026-08-20")
    row.refresh_from_db()
    assert row.status == ApplicationEvent.STATUS_ACCEPTED


def test_an_assessment_invite_claims_submitted_not_interviewing(student, role):
    """The one deliberate under-claim. A HackerRank invite proves the form
    was submitted; it does not prove anybody interviewed anybody."""
    apply_findings(student, [ats(
        email="noreply@hackerrank.com",
        subject="North Bank invites you to a test",
    )])
    row = only(student)
    assert row.event_type == ApplicationEvent.ASSESSMENT
    assert row.target_status == "submitted"
    appmail.accept(row)
    assert UserOpportunity.objects.for_user(student).get(
        opportunity=role).applied_status == "submitted"


def test_a_rejection_moves_the_row_to_done(student, role):
    apply_findings(student, [ats(
        subject="Your application to North Bank",
        snippet="We regret to inform you that we will not be moving forward "
                "with your application.",
    )])
    row = only(student)
    assert row.event_type == ApplicationEvent.REJECTED
    assert row.target_status == "closed"
    appmail.accept(row)
    assert UserOpportunity.objects.for_user(student).get(
        opportunity=role).applied_status == "closed"


def test_the_rejection_card_never_says_rejected(student, role):
    """Copy is part of the design here, not decoration. The card states the
    fact once, in sentence case, and does not repeat a word the student has
    already read in their own inbox."""
    label = appmail.event_label(ApplicationEvent.REJECTED)
    action = appmail.action_label(ApplicationEvent.REJECTED)
    assert label == "Not moving forward"
    assert action == "Mark done"
    for text in (label, action):
        assert "reject" not in text.lower()
        assert "unfortunate" not in text.lower()
        assert "unsuccessful" not in text.lower()
        # Sentence case, no em dashes — the house style, held on the one
        # card most likely to be read on a bad day.
        assert "—" not in text and "–" not in text
        assert text[1:] == text[1:].lower()


def test_accept_never_moves_a_row_backwards(student, role):
    """The student got there first, by hand. The card's answer is still
    "yes, that happened" — there is simply nothing left to write."""
    apply_findings(student, [ats()])
    row = only(student)
    UserOpportunity.all_objects.create(
        user=student, opportunity=role, applied_status="offer"
    )
    appmail.accept(row)

    assert UserOpportunity.objects.for_user(student).get(
        opportunity=role).applied_status == "offer"
    row.refresh_from_db()
    assert row.status == ApplicationEvent.STATUS_ACCEPTED


def test_accept_is_idempotent(student, role):
    apply_findings(student, [ats()])
    row = only(student)
    appmail.accept(row)
    appmail.accept(row)
    assert UserOpportunity.objects.for_user(student).count() == 1


def test_dismiss_is_remembered_forever(student, role):
    apply_findings(student, [ats()])
    row = only(student)
    appmail.dismiss(row)

    result = apply_findings(student, [ats(thread_id="t-app-9")])
    assert result.app_events_proposed == 0
    row.refresh_from_db()
    assert row.status == ApplicationEvent.STATUS_DISMISSED
    assert not UserOpportunity.all_objects.filter(user=student).exists()


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #

def test_application_events_are_tenant_scoped(student, role):
    other = User.objects.create_user(email="appmail-other@example.com", password="x")
    apply_findings(student, [ats()])

    assert ApplicationEvent.objects.for_user(other).count() == 0
    assert ApplicationEvent.objects.for_user(student).count() == 1
    with pytest.raises(TenantScopeError):
        ApplicationEvent.objects.all()


def test_one_students_card_cannot_be_tapped_by_another(client, student, role):
    other = User.objects.create_user(email="appmail-thief@example.com", password="x")
    apply_findings(student, [ats()])
    row = only(student)

    client.force_login(other)
    response = client.post(reverse("crm:app_event_act", args=[row.id, "accept"]))
    assert response.status_code == 404
    row.refresh_from_db()
    assert row.status == ApplicationEvent.STATUS_PENDING
    assert not UserOpportunity.all_objects.filter(user=student).exists()


# --------------------------------------------------------------------------- #
# The views, end to end
# --------------------------------------------------------------------------- #

def test_accept_through_the_view_writes_the_pipeline_row(client, student, role):
    apply_findings(student, [ats()])
    row = only(student)

    client.force_login(student)
    response = client.post(reverse("crm:app_event_act", args=[row.id, "accept"]))
    assert response.status_code == 200

    uo = UserOpportunity.objects.for_user(student).get(opportunity=role)
    assert uo.applied_status == "submitted"


def test_dismiss_through_the_view_writes_nothing(client, student, role):
    apply_findings(student, [ats()])
    row = only(student)

    client.force_login(student)
    response = client.post(reverse("crm:app_event_act", args=[row.id, "dismiss"]))
    assert response.status_code == 200
    assert not UserOpportunity.all_objects.filter(user=student).exists()


def test_the_card_renders_on_today(client, student, role):
    apply_findings(student, [ats()])
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()

    assert "From your applications" in body
    assert "Mark applied" in body
    assert role.title in body


def test_end_to_end_through_the_real_views(client, student, firm, role):
    """The whole promise, once, on one throwaway account: two real ATS
    messages arrive, two cards appear, one is accepted and moves the
    pipeline, the other is dismissed and never comes back."""
    second = Opportunity.objects.create(
        firm=firm, title="2027 Global Markets Summer Analyst",
        status="open", url="https://northbank.example/jobs/2",
    )
    UserOpportunity.all_objects.create(user=student, opportunity=second)

    apply_findings(student, [
        ats(subject="Thank you for applying to North Bank — 2027 Investment "
                    "Banking Summer Analyst"),
        ats(thread_id="t-e2e-2",
            subject="Thank you for applying to North Bank — 2027 Global "
                    "Markets Summer Analyst"),
    ])
    cards = {e.opportunity_id: e for e in ApplicationEvent.objects.for_user(student)}
    assert set(cards) == {role.id, second.id}

    client.force_login(student)
    assert client.get(reverse("crm:week")).status_code == 200

    accepted = client.post(
        reverse("crm:app_event_act", args=[cards[role.id].id, "accept"]))
    assert accepted.status_code == 200
    assert UserOpportunity.objects.for_user(student).get(
        opportunity=role).applied_status == "submitted"

    dismissed = client.post(
        reverse("crm:app_event_act", args=[cards[second.id].id, "dismiss"]))
    assert dismissed.status_code == 200
    # Dismissing left the second role exactly where it was.
    assert UserOpportunity.objects.for_user(student).get(
        opportunity=second).applied_status == ""

    # The same mail again: neither card comes back.
    result = apply_findings(student, [
        ats(thread_id="t-e2e-3",
            subject="Reminder — your North Bank 2027 Investment Banking "
                    "Summer Analyst application"),
        ats(thread_id="t-e2e-4",
            subject="Reminder — your North Bank 2027 Global Markets Summer "
                    "Analyst application"),
    ])
    assert result.app_events_proposed == 0
    assert ApplicationEvent.objects.for_user(student).filter(
        status=ApplicationEvent.STATUS_PENDING).count() == 0


def test_an_unknown_verb_is_refused(client, student, role):
    apply_findings(student, [ats()])
    row = only(student)
    client.force_login(student)
    response = client.post(reverse("crm:app_event_act", args=[row.id, "delete"]))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# The AI layer stays off the cheap path
# --------------------------------------------------------------------------- #

def test_ungated_mail_never_reaches_the_model(monkeypatch):
    """The whole cost design in one assertion: a newsletter must not cost a
    fraction of a cent."""
    from directory import ai_extract

    calls = []
    monkeypatch.setattr(
        ai_extract, "extract_application_event_ai",
        lambda *a, **k: calls.append(a) or None,
    )
    appmail.detect(ats(
        email="newsletter@substack.com", subject="This week in markets",
        snippet="Five charts.",
    ))
    assert calls == []


def test_typed_mail_never_reaches_the_model(monkeypatch):
    from directory import ai_extract

    calls = []
    monkeypatch.setattr(
        ai_extract, "extract_application_event_ai",
        lambda *a, **k: calls.append(a) or None,
    )
    assert appmail.detect(ats()).event_type == ApplicationEvent.APPLIED
    assert calls == []


def test_only_gated_untyped_mail_reaches_the_model(monkeypatch):
    from directory import ai_extract

    calls = []

    def fake(subject, snippet, **kwargs):
        calls.append((subject, snippet))
        return ai_extract.ApplicationEventGuess(
            value="rejected", phrase="pursue other applicants", confidence=0.5
        )

    monkeypatch.setattr(ai_extract, "extract_application_event_ai", fake)
    detection = appmail.detect(ats(
        subject="Regarding your North Bank application",
        snippet="We have reviewed your candidacy and will pursue other "
                "applicants for this position.",
    ))
    assert len(calls) == 1
    assert detection.event_type == ApplicationEvent.REJECTED
    assert detection.detected_by == "ai"


def test_the_model_stays_dark_without_a_key(settings):
    """Same posture as every other AI path here: no key, no call, no crash
    — and the deterministic layer keeps working on its own."""
    from directory import ai_extract

    settings.ANTHROPIC_API_KEY = ""
    assert ai_extract.extract_application_event_ai("anything", "at all") is None


def test_an_ungrounded_quote_is_discarded(monkeypatch, settings):
    """Inherited whole from `ai_extract`: a model that free-associates an
    answer is worse than one that says nothing."""
    from directory import ai_extract

    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(
        ai_extract, "_post_json",
        lambda *a, **k: {"content": [{"type": "text", "text":
            '{"event": "rejected", "quote": "a sentence that was never sent"}'}]},
    )
    assert ai_extract.extract_application_event_ai(
        "Regarding your application", "We will be in touch shortly."
    ) is None


def test_a_grounded_quote_is_kept(monkeypatch, settings):
    from directory import ai_extract

    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(
        ai_extract, "_post_json",
        lambda *a, **k: {"content": [{"type": "text", "text":
            '{"event": "offer", "quote": "We would like to offer you a place."}'}]},
    )
    guess = ai_extract.extract_application_event_ai(
        "Your North Bank application", "We would like to offer you a place."
    )
    assert guess is not None and guess.value == "offer"


def test_an_unknown_event_name_from_the_model_is_discarded(monkeypatch, settings):
    from directory import ai_extract

    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(
        ai_extract, "_post_json",
        lambda *a, **k: {"content": [{"type": "text", "text":
            '{"event": "ghosted", "quote": "We would like to offer you a place."}'}]},
    )
    assert ai_extract.extract_application_event_ai(
        "Your application", "We would like to offer you a place."
    ) is None


# --------------------------------------------------------------------------- #
# Export / deletion completeness
# --------------------------------------------------------------------------- #

def test_the_events_are_in_the_export_and_the_deletion(student, role):
    from accounts import services

    apply_findings(student, [ats()])
    csv_text = services.application_events_csv(student)
    assert "North Bank" in csv_text
    assert "2027 Investment Banking Summer Analyst" in csv_text

    counts = services.delete_user_and_data(student)
    assert counts["application_events"] == 1
    assert not ApplicationEvent.all_objects.filter(user_id=student.id).exists()
