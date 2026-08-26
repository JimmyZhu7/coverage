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
# The live miss, 2026-08-24 — a firm's own campus-recruiting sender
# --------------------------------------------------------------------------- #
#
# Everything in this block is the real message, verbatim, and the real
# marketing it has to be told apart from. `campuscareers.bofa.com` is a
# subdomain of a domain the directory registers; the subject carries no
# application word at all; the words are in the body. See the third gate arm
# in capture/appmail.py.

@pytest.fixture
def bank(db):
    """A firm the directory knows by domain, the way Bank of America is."""
    return Firm.objects.create(
        slug="bofa-test", name="Bank of America",
        domains=["bankofamerica.com", "bofa.com"],
    )


@pytest.fixture
def forum(bank):
    return Opportunity.objects.create(
        firm=bank, title="Bank of America Campus Insight Forum: The Power to "
                         "Lead - Fall 2026",
        status="open", url="https://bofa.example/jobs/forum",
    )


def bofa(**over):
    """The exact message that was filed as bulk noise."""
    base = {
        "name": "Bank of America Campus Careers",
        "email": "noreply@campuscareers.bofa.com",
        "subject": "Bank of America Action Required: Indicate Your Top "
                   "Choices for Campus Insight Forum",
        "snippet": "Bank of America Application Update. Congratulations on "
                   "advancing in the Campus Insight Forum process! To move "
                   "forward, you must complete the Program Preference Survey "
                   "by August 30, 2026 at 11:59 PM EST.",
        "thread_id": "1a034cf0a88ac917",
        "occurred_at": "2026-08-24T17:23:36+00:00",
        "bulk_reasons": "automated no-reply sender, application status update",
    }
    base.update(over)
    return ats(**base)


def test_the_firms_own_noreply_gates_on_a_subject_with_no_application_word(bank):
    """THE MISS. `campuscareers.bofa.com` is not an ATS vendor and the subject
    says nothing about an application, so both old arms refused it — and an
    advancement notice naming a deadline six days out was filed as bulk."""
    finding = bofa()
    assert appmail._APPLICATION_WORD_RE.search(finding["subject"]) is None

    detection = appmail.detect(finding, allow_ai=False)
    assert detection.gated is True
    assert detection.event_type == ApplicationEvent.ADVANCED
    assert "own domain" in " ".join(detection.reasons)


def test_advancing_claims_submitted_not_interviewing(bank):
    """The second deliberate under-claim. Advancing into a forum whose next
    step is a preference survey is not a rejection, is not an offer, and is
    emphatically not an interview — nobody has interviewed anybody."""
    assert appmail.TARGET_STATUS[ApplicationEvent.ADVANCED] == "submitted"
    detection = appmail.detect(bofa(), allow_ai=False)
    assert appmail.TARGET_STATUS[detection.event_type] == "submitted"


def test_the_stated_deadline_is_attached(bank):
    from datetime import date

    assert appmail.detect(bofa(), allow_ai=False).due_on == date(2026, 8, 30)


@pytest.mark.parametrize("finding", [
    # Marketing from the same unattended campus address. No application word
    # anywhere, so the third arm refuses it exactly as the old gate did.
    ats(email="noreply@campuscareers.bofa.com",
        name="Bank of America Campus Careers",
        subject="Bank of America Sophomore Series: an evening with our traders",
        snippet="Join us on campus for drinks and a markets panel. Register "
                "by August 30, 2026."),
    # A firm newsletter from the same shape of address.
    ats(email="no-reply@insights.bofa.com",
        name="Bank of America Insights",
        subject="Bank of America insights: the year ahead",
        snippet="Read our 2027 outlook."),
    # An event reminder. "Programme" is not "application", here either.
    ats(email="donotreply@campuscareers.bofa.com",
        name="Bank of America Campus Careers",
        subject="Reminder: Step into Finance insight day",
        snippet="Your seat is confirmed for Thursday."),
])
def test_marketing_from_the_same_firm_sender_is_still_refused(bank, finding):
    """The whole risk of the third arm in one test. A bank mails its
    marketing from exactly this address, and gating on the sender ALONE
    would have handed every blast to the paid model."""
    assert appmail.detect(finding, allow_ai=False).gated is False


def test_a_human_at_the_firm_is_still_never_gated(bank):
    """The third arm tests the LOCALPART through `capture.inbound`, so a real
    recruiter on the same domain is not a no-reply and never gates."""
    human = ats(
        name="Dana Recruiter", email="dana.recruiter@campuscareers.bofa.com",
        bulk=False, replied=True,
        subject="Following up on the forum",
        snippet="Congratulations on advancing! Let me know when you are free.",
    )
    human.pop("bulk_reasons")
    assert appmail.detect(human, allow_ai=False).gated is False


def test_an_unknown_firms_noreply_does_not_get_the_body_scope(db):
    """The arm is the sender AND the directory. A no-reply address on a
    domain no firm registers falls back to the old subject-only rule."""
    finding = bofa(email="noreply@campuscareers.unknownbank.example")
    assert appmail.detect(finding, allow_ai=False).gated is False


def test_marketing_from_the_firm_sender_never_reaches_the_model(bank, monkeypatch):
    """The cost half of the same argument, asserted rather than assumed."""
    from directory import ai_extract

    calls = []
    monkeypatch.setattr(
        ai_extract, "extract_application_event_ai",
        lambda *a, **k: calls.append(a) or None,
    )
    appmail.detect(ats(
        email="noreply@campuscareers.bofa.com",
        name="Bank of America Campus Careers",
        subject="Bank of America Sophomore Series: an evening with our traders",
        snippet="Join us on campus for drinks and a markets panel.",
    ))
    assert calls == []


def test_the_bofa_message_now_proposes_end_to_end(student, forum):
    result = apply_findings(student, [bofa()])

    assert result.app_events_proposed == 1
    row = only(student)
    assert row.event_type == ApplicationEvent.ADVANCED
    assert row.target_status == "submitted"
    assert row.opportunity_id == forum.id
    assert row.due_on.isoformat() == "2026-08-30"
    assert row.detected_by == "rules"
    # §10 holds through the new path too: the subject, never the body — even
    # though the body is what typed it and what carried the date.
    assert "Congratulations" not in row.evidence
    assert "Program Preference Survey" not in row.evidence
    # And still nothing in the pipeline until the tap.
    assert not UserOpportunity.all_objects.filter(user=student).exists()


def test_a_dated_event_is_carded_even_when_the_stage_stands_still(student, forum):
    """The founder had already marked this role submitted by hand. The stage
    test alone would have thrown the deadline away with the card — the same
    message lost twice, for a different reason the second time.

    The date is written relative to the clock rather than pinned to the real
    message's August 30: this rule turns on the deadline being STILL AHEAD,
    and a test that quietly stops testing that in September is worse than no
    test.
    """
    from datetime import timedelta

    from django.utils import timezone

    ahead = timezone.localdate() + timedelta(days=6)
    UserOpportunity.all_objects.create(
        user=student, opportunity=forum, applied_status="submitted"
    )
    result = apply_findings(student, [bofa(
        occurred_at=timezone.now().isoformat(),
        snippet="Bank of America Application Update. Congratulations on "
                "advancing in the Campus Insight Forum process! Complete the "
                f"Program Preference Survey by {ahead:%B} {ahead.day}, "
                f"{ahead.year} at 11:59 PM EST.",
    )])

    assert result.app_events_already == 0
    assert result.app_events_proposed == 1
    assert only(student).due_on == ahead


def test_an_undated_event_that_moves_no_stage_is_still_suppressed(student, forum):
    """The relaxation is the DATE, not a general loosening. Strip the
    deadline sentence and the same mail is noise again."""
    UserOpportunity.all_objects.create(
        user=student, opportunity=forum, applied_status="submitted"
    )
    result = apply_findings(student, [bofa(
        snippet="Bank of America Application Update. Congratulations on "
                "advancing in the Campus Insight Forum process! Watch your "
                "inbox for next steps.",
    )])

    assert result.app_events_proposed == 0
    assert result.app_events_already == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


def test_a_passed_deadline_does_not_resurrect_a_settled_card(student, forum):
    """A date behind today is not a reason to ask again."""
    UserOpportunity.all_objects.create(
        user=student, opportunity=forum, applied_status="submitted"
    )
    result = apply_findings(student, [bofa(
        occurred_at="2024-01-02T09:00:00+00:00",
        snippet="Bank of America Application Update. Congratulations on "
                "advancing! Complete the Program Preference Survey by "
                "January 10, 2024.",
    )])
    assert result.app_events_already == 1
    assert not ApplicationEvent.all_objects.filter(user=student).exists()


def test_the_due_date_rides_the_card_onto_today(client, student, forum):
    apply_findings(student, [bofa()])
    client.force_login(student)
    body = client.get(reverse("crm:week")).content.decode()

    assert "Next step required" in body
    assert "Due Aug 30" in body


# --------------------------------------------------------------------------- #
# Reading a date: only one that is written down
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("complete the survey by August 30, 2026 at 11:59 PM EST", "2026-08-30"),
    ("Please respond before Sept 3, 2026.", "2026-09-03"),
    ("Submissions are due by 30 September 2026", "2026-09-30"),
    ("no later than the 3rd of... ", None),
    ("Your response is due on October 1, 2026", "2026-10-01"),
    ("deadline: November 15, 2026", "2026-11-15"),
])
def test_a_stated_deadline_is_read(text, expected):
    from datetime import date

    got = appmail._due_on(text, date(2026, 8, 24))
    assert (got.isoformat() if got else None) == expected


@pytest.mark.parametrize("text", [
    # Vague. The most common shape, and the one that must attach nothing.
    "please complete the survey by the end of next week",
    "you have 5 days to respond",
    "respond by Friday",
    # A year we would have to infer is a year we do not have.
    "complete the survey by August 30",
    # Numeric, and therefore a guess about which country wrote it.
    "complete the survey by 08/30/2026",
    "complete the survey by 2026-08-30",
    # A date with no obligation in front of it: an event date, a start date,
    # a posted-on date. This module cannot tell which, so it reads none.
    "The Campus Insight Forum takes place on August 30, 2026",
    "Copyright 2026 Bank of America. Sent September 1, 2026.",
    # Behind the message that announced it.
    "complete the survey by August 1, 2026",
])
def test_a_vague_or_inferred_deadline_is_refused(text):
    from datetime import date

    assert appmail._due_on(text, date(2026, 8, 24)) is None


def test_a_rejection_never_carries_a_deadline(student, forum):
    """A closed application has nothing left that is due."""
    apply_findings(student, [bofa(
        subject="Update on your Bank of America application",
        snippet="We regret to inform you that we will not be moving forward "
                "with your application. Please share feedback by "
                "September 30, 2026.",
    )])
    row = only(student)
    assert row.event_type == ApplicationEvent.REJECTED
    assert row.target_status == "closed"
    assert row.due_on is None


def test_a_named_stage_still_beats_an_advancement_phrase(bank):
    """Precedence. A mail that both congratulates you on advancing and books
    the HireVue IS the HireVue invite."""
    detection = appmail.detect(bofa(
        subject="Bank of America: complete your HireVue digital interview",
    ), allow_ai=False)
    assert detection.event_type == ApplicationEvent.VIDEO_INTERVIEW


def test_a_bare_congratulations_never_advances_an_application(bank):
    """Banks congratulate students on joining a mailing list. The phrase list
    wants the verb, not the pleasantry."""
    detection = appmail.detect(bofa(
        snippet="Congratulations! You are now subscribed to Bank of America "
                "campus recruiting updates. Applications open in September.",
    ), allow_ai=False)
    assert detection.event_type != ApplicationEvent.ADVANCED


def test_the_citadel_confirmation_still_types_as_applied():
    """The other real message in the same batch, unchanged by any of this:
    a firm's own no-reply whose SUBJECT already carried the word, typed by
    the first arm exactly as before."""
    detection = appmail.detect(ats(
        name="Citadel", email="no-reply@citadel.com",
        subject="Your application for Citadel's Citadel Associate Program | "
                "Pitch Competition Interest Form 2026 role has been received.",
        snippet="Thank you for your interest in Citadel | Citadel Securities. "
                "We've received your application, and our hiring team will "
                "carefully review your skills and experience.",
    ), allow_ai=False)
    assert detection.gated is True
    assert detection.event_type == ApplicationEvent.APPLIED
    assert detection.reasons[0] == "automated sender, application subject"


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
