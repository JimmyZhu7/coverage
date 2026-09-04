"""Mail facts (capture/mailfacts.py) — regression tests built from the two
REAL messages that motivated the feature, verbatim from the founder's
mailbox (2026-08-24, read-only):

1. `sagarwal@allenco.com`, Exchange auto-reply (`auto-submitted:
   auto-generated`): "Somil Agarwal is no longer with Allen & Company. For
   matters with which Somil was involved, please contact Salima Vahabzadeh
   at salima@allenco.com." — the departed person must NOT stay proposed, and
   the named replacement MUST be proposed, as a referral, with the quote.
2. `postmaster@goldmansachs.onmicrosoft.com`, DSN 5.2.2: "The recipient's
   mailbox is full and can't accept messages now. Please try resending your
   message later" naming `Noah.Bauld@ny.ibd.email.gs.com` — NOT a hard
   bounce (the address works), and the routing address is recorded against
   the person instead of discarded.

Tests drive the WHOLE chain where they can: a Gmail-API-shaped message dict
through `gmail_live._classify_message`, its finding through
`capture.gmail.apply_findings`. `transaction=True` for the same reason as
test_gmail.py: `crm.services.log_touch` opens its own psycopg connection.
"""

from __future__ import annotations

import base64
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import discovery, mailfacts
from capture.gmail import apply_findings
from capture.gmail_live import _classify_message
from capture.models import ContactProposal, MailFact
from crm.models import Contact, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)

OWN = "jimmyz@usc.edu"


@pytest.fixture
def student():
    return User.objects.create_user(email="facts-student@example.com", password="x")


@pytest.fixture
def allen():
    return Firm.objects.create(
        slug="allen-company", name="Allen & Company", domains=["allenco.com"]
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def gmail_message(*, from_header, subject, body, snippet=None, headers=(),
                  thread_id="t-1", internal_date="1787584244000"):
    all_headers = [
        {"name": "From", "value": from_header},
        {"name": "To", "value": f"Jimmy Zhu <{OWN}>"},
        {"name": "Subject", "value": subject},
        *[{"name": n, "value": v} for n, v in headers],
    ]
    return {
        "threadId": thread_id,
        "internalDate": internal_date,
        "snippet": snippet if snippet is not None else body[:180],
        "payload": {
            "headers": all_headers,
            "mimeType": "text/plain",
            "body": {"data": _b64(body)},
        },
    }


# The Somil auto-reply, headers and body as the real message carries them.
SOMIL_BODY = (
    "Somil Agarwal is no longer with Allen & Company. For matters with "
    "which Somil was involved, please contact Salima Vahabzadeh at "
    "salima@allenco.com.\nThis message is intended for the addressee only "
    "and may contain confidential or proprietary information."
)


def somil_auto_reply():
    return gmail_message(
        from_header="Somil Agarwal <sagarwal@allenco.com>",
        subject=(
            "Automatic reply: USC | Redwood | Allen & Company - USC Student "
            "Coffee Chat Request"
        ),
        body=SOMIL_BODY,
        # Gmail's snippet HTML-escapes the ampersand — the real payload does.
        snippet=(
            "Somil Agarwal is no longer with Allen &amp; Company. For matters "
            "with which Somil was involved, please contact Salima Vahabzadeh "
            "at salima@allenco.com. This message is intended for the addressee only"
        ),
        headers=(
            ("auto-submitted", "auto-generated"),
            ("X-Auto-Response-Suppress", "All"),
            ("In-Reply-To", "<orig@mail.gmail.com>"),
            ("References", "<orig@mail.gmail.com>"),
        ),
        thread_id="t-somil-auto",
    )


# Goldman's DSN, from the real message: postmaster sender, "Undeliverable:"
# subject, body naming the expanded routing address and the mailbox-full
# sentence, DSN status 5.2.2.
NOAH_DSN_BODY = (
    "Delivery has failed to these recipients or groups:\n\n"
    "Noah.Bauld@ny.ibd.email.gs.com<mailto:Noah.Bauld@ny.ibd.email.gs.com>\n"
    "The recipient's mailbox is full and can't accept messages now. Please "
    "try resending your message later, or contact the recipient directly.\n\n"
    "Diagnostic information for administrators:\n"
    "Status: 5.2.2\n"
)


def noah_dsn():
    return gmail_message(
        from_header="postmaster@goldmansachs.onmicrosoft.com",
        subject=(
            "Undeliverable: USC | Field Operations | Goldman Sachs - USC "
            "Student Coffee Chat Request"
        ),
        body=NOAH_DSN_BODY,
        # The real Gmail snippet mangles the address with spaces after dots —
        # kept mangled here so the test proves the body, not the snippet, is
        # what the address is read from.
        snippet=(
            "Delivery has failed to these recipients or groups: Noah. Bauld@ "
            "ny. ibd. email. gs. com The recipient&#39;s mailbox is full and "
            "can&#39;t accept messages now. Please try resending your message later,"
        ),
        thread_id="t-noah-send",
    )


def outreach_proposal(user, *, email, name, firm=None):
    return ContactProposal.all_objects.create(
        user=user, name=name, email=email, firm=firm,
        evidence="You wrote to them", evidence_kind="outreach",
        thread_id="t-out", status=ContactProposal.STATUS_PENDING,
    )


# --------------------------------------------------------------------------- #
# Regression 1: the departed contact and his named replacement
# --------------------------------------------------------------------------- #

class TestDepartedAndReferral:
    def test_somil_is_withdrawn_and_salima_proposed_with_quote(self, student, allen):
        somil = outreach_proposal(
            student, email="sagarwal@allenco.com", name="Somil Agarwal", firm=allen
        )

        finding = _classify_message(OWN, somil_auto_reply())
        assert finding["bulk"] is True
        assert finding["auto_reply"] is True

        result = apply_findings(student, [finding])

        # Somil is NOT proposed any more — withdrawn, restorably.
        somil.refresh_from_db()
        assert somil.status == ContactProposal.STATUS_DISMISSED

        # Salima IS proposed, flagged as a referral, at the right firm.
        salima = ContactProposal.objects.for_user(student).get(
            email="salima@allenco.com"
        )
        assert salima.status == ContactProposal.STATUS_PENDING
        assert salima.evidence_kind == "referral"
        assert salima.name == "Salima Vahabzadeh"
        assert salima.firm_id == allen.id

        # Both facts carry verbatim quotes from the message.
        departed = MailFact.objects.for_user(student).get(
            about_email="sagarwal@allenco.com", kind=MailFact.KIND_DEPARTED
        )
        assert departed.status == MailFact.STATUS_APPLIED
        assert "no longer with Allen & Company" in departed.quote
        referral = MailFact.objects.for_user(student).get(
            about_email="sagarwal@allenco.com", kind=MailFact.KIND_REFERRAL
        )
        assert "please contact Salima Vahabzadeh at salima@allenco.com" in referral.quote
        assert referral.proposal_id == salima.id
        assert result.referral_proposals == 1
        assert result.mail_facts_applied == 1

    def test_departed_contact_email_cleared_not_archived(self, student, allen):
        contact = Contact.all_objects.create(
            user=student, name="Somil Agarwal", email="sagarwal@allenco.com",
            firm=allen,
        )
        finding = _classify_message(OWN, somil_auto_reply())
        apply_findings(student, [finding])

        contact.refresh_from_db()
        assert contact.email == ""
        assert contact.archived is False
        assert "sagarwal@allenco.com" in contact.notes  # moved, not destroyed
        fact = MailFact.objects.for_user(student).get(
            about_email="sagarwal@allenco.com", kind=MailFact.KIND_DEPARTED
        )
        assert fact.prior_email == "sagarwal@allenco.com"

    def test_undo_restores_proposal_and_address(self, student, allen):
        somil = outreach_proposal(
            student, email="sagarwal@allenco.com", name="Somil Agarwal", firm=allen
        )
        finding = _classify_message(OWN, somil_auto_reply())
        apply_findings(student, [finding])
        fact = MailFact.objects.for_user(student).get(
            about_email="sagarwal@allenco.com", kind=MailFact.KIND_DEPARTED
        )
        mailfacts.undo(fact)
        somil.refresh_from_db()
        assert somil.status == ContactProposal.STATUS_PENDING
        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_UNDONE

    def test_accepting_referral_creates_cold_contact_no_touch(self, student, allen):
        finding = _classify_message(OWN, somil_auto_reply())
        apply_findings(student, [finding])
        salima = ContactProposal.objects.for_user(student).get(
            email="salima@allenco.com"
        )
        contact = discovery.accept(salima)
        assert contact is not None
        assert contact.warmth == "cold"
        assert contact.thread_state == "no_reply"
        # NO touch: she has neither written nor been written to.
        assert not Touch.objects.for_user(student).filter(contact=contact).exists()

    def test_second_scan_does_not_double_anything(self, student, allen):
        outreach_proposal(
            student, email="sagarwal@allenco.com", name="Somil Agarwal", firm=allen
        )
        finding = _classify_message(OWN, somil_auto_reply())
        apply_findings(student, [finding])
        result2 = apply_findings(student, [finding])
        assert result2.referral_proposals == 0
        assert result2.mail_facts_applied == 0
        assert MailFact.objects.for_user(student).count() == 2  # departed + referral
        assert ContactProposal.objects.for_user(student).filter(
            email="salima@allenco.com"
        ).count() == 1

    def test_salima_reply_upgrades_referral_not_second_card(self, student, allen):
        finding = _classify_message(OWN, somil_auto_reply())
        apply_findings(student, [finding])
        # She later genuinely replies — the same unique row upgrades in
        # place, no second proposal, no "wrote to you" duplicate.
        reply = {
            "name": "Salima Vahabzadeh",
            "email": "salima@allenco.com",
            "found": True,
            "bounced": False,
            "outreach_sent": False,
            "replied": True,
            "chat_status": "none",
            "bulk": False,
            "threaded_reply": True,
            "addressed_to_user": True,
            "subject": "Re: USC | Redwood | Allen & Company",
            "thread_id": "t-salima-reply",
        }
        outcome = discovery.consider_finding(student, reply)
        assert outcome == discovery.UPGRADED
        salima = ContactProposal.objects.for_user(student).get(
            email="salima@allenco.com"
        )
        assert salima.evidence_kind == "reply_received"
        assert ContactProposal.objects.for_user(student).filter(
            email="salima@allenco.com"
        ).count() == 1


# --------------------------------------------------------------------------- #
# Regression 2: the soft bounce carrying the real routing address
# --------------------------------------------------------------------------- #

class TestSoftBounce:
    def test_classified_soft_never_hard(self):
        finding = _classify_message(OWN, noah_dsn())
        assert finding is not None
        assert finding["bounced"] is False
        assert finding["soft_bounce"] is True
        # The address comes from the BODY (clean), not Gmail's dot-mangled
        # snippet.
        assert finding["email"] == "noah.bauld@ny.ibd.email.gs.com"

    def test_contact_keeps_address_and_gains_routing_note(self, student):
        contact = Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        finding = _classify_message(OWN, noah_dsn())
        result = apply_findings(student, [finding])

        contact.refresh_from_db()
        assert contact.email == "noah.bauld@gs.com"       # kept
        assert result.bounced_cleared == 0                 # nothing cleared
        assert "noah.bauld@ny.ibd.email.gs.com" in contact.notes
        fact = MailFact.objects.for_user(student).get(
            kind=MailFact.KIND_ROUTING
        )
        assert fact.status == MailFact.STATUS_APPLIED
        assert fact.new_email == "noah.bauld@ny.ibd.email.gs.com"
        assert "mailbox is full" in fact.quote

    def test_pending_proposal_survives_soft_bounce(self, student):
        proposal = outreach_proposal(
            student, email="noah.bauld@gs.com", name="Noah Bauld"
        )
        finding = _classify_message(OWN, noah_dsn())
        apply_findings(student, [finding])
        proposal.refresh_from_db()
        assert proposal.status == ContactProposal.STATUS_PENDING  # not withdrawn
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_ROUTING)
        assert fact.proposal_id == proposal.id

    def test_hard_bounce_still_clears(self, student):
        """The soft split must not soften REAL bounces."""
        contact = Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        message = gmail_message(
            from_header="MAILER-DAEMON@mx.example (Mail Delivery Subsystem)",
            subject="Returned mail: see transcript for details",
            body=(
                "The following addresses had permanent fatal errors:\n"
                "<lidia@wellsfargo.example>\n550 5.1.1 User unknown\n"
            ),
            thread_id="t-lidia",
        )
        finding = _classify_message(OWN, message)
        assert finding["bounced"] is True
        result = apply_findings(student, [finding])
        contact.refresh_from_db()
        assert contact.email == ""
        assert result.bounced_cleared == 1


# --------------------------------------------------------------------------- #
# Out of office: not silence, not a reply
# --------------------------------------------------------------------------- #

OOO_BODY = (
    "I am out of the office with limited access to email. I will return on "
    "Monday, September 7. For urgent matters, please contact my assistant "
    "at assistant@allenco.com."
)


def ooo_message():
    return gmail_message(
        from_header="Peter Foggo <pfoggo@allenco.com>",
        subject="Automatic reply: USC | Stephens | Allen & Company",
        body=OOO_BODY,
        headers=(("auto-submitted", "auto-replied"),),
        thread_id="t-ooo",
        internal_date="1787584244000",  # 2026-08-24
    )


class TestOutOfOffice:
    def test_ooo_snoozes_to_return_date_no_warmth_no_reply(self, student, allen):
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
        )
        finding = _classify_message(OWN, ooo_message())
        assert finding["bulk"] is True and finding["auto_reply"] is True
        apply_findings(student, [finding])

        contact.refresh_from_db()
        # The follow-up clock waits for the stated return, on the user's own
        # calendar — not six business days of counting leave as silence.
        assert contact.snoozed_until is not None
        assert timezone.localtime(contact.snoozed_until).date() == date(2026, 9, 7)
        # No reply logged, warmth untouched: an OOO is not a person answering.
        assert contact.warmth == "cold"
        assert contact.thread_state == "no_reply"
        assert not Touch.objects.for_user(student).filter(
            contact=contact, kind="reply_received"
        ).exists()
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        assert fact.return_on == date(2026, 9, 7)
        assert "I will return on Monday, September 7" in fact.quote

    def test_ooo_redirect_is_not_proposed(self, student, allen):
        """"Contact my assistant" inside a plain OOO is temporary coverage,
        not a networking lead — nobody is proposed off it."""
        Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])
        assert not ContactProposal.objects.for_user(student).filter(
            email="assistant@allenco.com"
        ).exists()

    def test_ooo_never_shortens_a_later_snooze(self, student):
        later = timezone.now() + timezone.timedelta(days=60)
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com",
            snoozed_until=later,
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])
        contact.refresh_from_db()
        assert contact.snoozed_until == later

    def test_dismissed_ooo_card_is_not_resurrected_by_a_later_leave(self, student):
        """Regression: dismissing the no-date OOO card used to only last
        until the SAME sender's next auto-reply happened to state a return
        date. `_apply_ooo`'s forward-only update checked only the date, never
        the fact's own status, so a dismissed row silently flipped back to
        `applied` and `contact.snoozed_until` got overwritten again with no
        tap from the user in between. `dismiss`'s own docstring calls every
        dismissed row a permanent do-not-re-create memory; this pins that
        for the one capture surface that used to disagree."""
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com",
        )
        vague = gmail_message(
            from_header="Peter Foggo <pfoggo@allenco.com>",
            subject="Automatic reply: out",
            body="I am currently traveling and will respond when I return.",
            headers=(("auto-submitted", "auto-replied"),),
            thread_id="t-ooo-vague",
        )
        apply_findings(student, [_classify_message(OWN, vague)])
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        assert fact.status == MailFact.STATUS_PENDING

        mailfacts.dismiss(fact)
        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_DISMISSED

        dated = gmail_message(
            from_header="Peter Foggo <pfoggo@allenco.com>",
            subject="Automatic reply: still out",
            body=(
                "I am out of the office and will return on Monday, "
                "September 7, 2026."
            ),
            headers=(("auto-submitted", "auto-replied"),),
            thread_id="t-ooo-dated",
        )
        apply_findings(student, [_classify_message(OWN, dated)])

        fact.refresh_from_db()
        contact.refresh_from_db()
        assert fact.status == MailFact.STATUS_DISMISSED
        assert fact.return_on is None
        assert contact.snoozed_until is None

    def test_undone_ooo_snooze_is_not_reapplied_by_a_later_leave(self, student):
        """Same guard, the other terminal state: the user hit Undo on an
        automated snooze (`mailfacts.undo`), and a later dated auto-reply
        from the same sender must not put it back."""
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com",
        )
        apply_findings(student, [_classify_message(OWN, ooo_message())])
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        assert fact.status == MailFact.STATUS_APPLIED
        contact.refresh_from_db()
        assert contact.snoozed_until is not None

        mailfacts.undo(fact)
        fact.refresh_from_db()
        contact.refresh_from_db()
        assert fact.status == MailFact.STATUS_UNDONE
        assert contact.snoozed_until is None

        later = gmail_message(
            from_header="Peter Foggo <pfoggo@allenco.com>",
            subject="Automatic reply: still out",
            body=(
                "I am out of the office and will return on Monday, "
                "September 14, 2026."
            ),
            headers=(("auto-submitted", "auto-replied"),),
            thread_id="t-ooo-later",
        )
        apply_findings(student, [_classify_message(OWN, later)])

        fact.refresh_from_db()
        contact.refresh_from_db()
        assert fact.status == MailFact.STATUS_UNDONE
        assert contact.snoozed_until is None

    def test_headerless_ooo_subject_is_still_bulk(self, student):
        """An OOO with no RFC 3834 header used to fall through to
        `replied: True` and inflate warmth. The subject prefix now catches
        it."""
        message = gmail_message(
            from_header="Jane Doe <jane@northbank.example>",
            subject="Out of Office AutoReply: your note",
            body="I am on vacation, returning October 1.",
            thread_id="t-noheader",
        )
        finding = _classify_message(OWN, message)
        assert finding["bulk"] is True
        assert finding["auto_reply"] is True
        assert finding["replied"] is False


# --------------------------------------------------------------------------- #
# The grounding contract: no quote, no action
# --------------------------------------------------------------------------- #

class TestNoQuoteNoAction:
    def test_unreadable_auto_reply_takes_no_action(self, student, allen):
        somil = outreach_proposal(
            student, email="sagarwal@allenco.com", name="Somil Agarwal", firm=allen
        )
        contact = Contact.all_objects.create(
            user=student, name="Other Person", email="other@allenco.com"
        )
        message = gmail_message(
            from_header="Somil Agarwal <sagarwal@allenco.com>",
            subject="Automatic reply: USC | Redwood | Allen & Company",
            body="Thank you for your message.",
            headers=(("auto-submitted", "auto-generated"),),
            thread_id="t-vague",
        )
        finding = _classify_message(OWN, message)
        result = apply_findings(student, [finding])

        # Nothing acted on: proposal stands, contacts untouched.
        somil.refresh_from_db()
        assert somil.status == ContactProposal.STATUS_PENDING
        contact.refresh_from_db()
        assert contact.email == "other@allenco.com"
        assert result.mail_facts_applied == 0
        # ...but not dropped either: surfaced for the user's look.
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_REVIEW)
        assert fact.status == MailFact.STATUS_PENDING
        assert fact.quote == ""
        assert result.mail_facts_surfaced == 1

    def test_dry_run_writes_nothing(self, student, allen):
        somil = outreach_proposal(
            student, email="sagarwal@allenco.com", name="Somil Agarwal", firm=allen
        )
        finding = _classify_message(OWN, somil_auto_reply())
        result = apply_findings(student, [finding], dry_run=True)
        somil.refresh_from_db()
        assert somil.status == ContactProposal.STATUS_PENDING
        assert MailFact.objects.for_user(student).count() == 0
        assert not ContactProposal.objects.for_user(student).filter(
            email="salima@allenco.com"
        ).exists()
        # ...while still reporting what WOULD happen.
        assert result.mail_facts_applied == 1
        assert result.referral_proposals == 1


class TestOooUndoRestoresPriorSnooze:
    def test_undo_puts_back_the_snooze_the_user_had_set(self, student, allen):
        """Regression: `_extend_snooze` only moves forward, so it can move
        the clock OVER a snooze the user set themselves — and undo cleared
        `snoozed_until` to None instead of restoring the user's value.

        `earlier` is derived from the RETURN DATE, not from `timezone.now()`.
        It used to be `now + 3 days`, which stopped being earlier than the
        Sept 7 return the moment the real clock reached Sept 4: `_extend_snooze`
        then correctly declined to move a snooze that already covered the
        return, recorded no prior, and the test failed on a product that was
        working. A scenario about "the user's snooze is earlier than the
        return" has to state that relationship, not approximate it from
        whatever day the suite happens to run.
        """
        earlier = mailfacts._snooze_datetime(student, date(2026, 9, 7)) - timezone.timedelta(days=1)
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com",
            firm=allen, snoozed_until=earlier,
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])

        contact.refresh_from_db()
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        assert timezone.localtime(contact.snoozed_until).date() == date(2026, 9, 7)
        assert fact.prior_snoozed_until == earlier

        mailfacts.undo(fact)
        contact.refresh_from_db()
        assert contact.snoozed_until == earlier, (
            "undo must restore the user's own earlier snooze, not destroy it"
        )

    def test_undo_with_no_prior_snooze_still_clears(self, student, allen):
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com",
            firm=allen,
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        mailfacts.undo(fact)
        contact.refresh_from_db()
        assert contact.snoozed_until is None


# --------------------------------------------------------------------------- #
# A dismissed or undone fact is a closed card, not a re-arming trigger
# --------------------------------------------------------------------------- #

def later_ooo_message():
    """The same person, a second OOO auto-reply naming a LATER return date —
    the ordinary shape of "still out, pushed my return back", not a
    contrived input. Different thread (a fresh auto-reply on a new note),
    same sender."""
    return gmail_message(
        from_header="Peter Foggo <pfoggo@allenco.com>",
        subject="Automatic reply: USC | Stephens | Allen & Company",
        body=(
            "I am out of the office with limited access to email. I will "
            "return on Monday, September 14. For urgent matters, please "
            "contact my assistant at assistant@allenco.com."
        ),
        headers=(("auto-submitted", "auto-replied"),),
        thread_id="t-ooo-2",
        internal_date="1789189844000",  # later than the first OOO
    )


class TestOooDismissedStaysDismissed:
    """`undo()` only acts on an `applied` row and `dismiss()` only accepts
    `pending`/`applied` — both treat `dismissed`/`undone` as closed states a
    later automated pass must not reopen. `_apply_ooo`'s "update the
    existing row" branch is the one path that never checked: a later OOO
    from the same sender rewrote `status` back to `applied` and pushed
    `contact.snoozed_until` forward again, with no new tap from the
    student. That is mail read on the student's behalf directly overwriting
    a decision the student already made — the propose-then-confirm posture
    every other write in this module holds itself to."""

    def test_dismissed_ooo_is_not_revived_by_a_later_return_date(self, student, allen):
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])

        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        assert fact.status == MailFact.STATUS_APPLIED
        mailfacts.dismiss(fact)
        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_DISMISSED
        contact.refresh_from_db()
        snoozed_after_dismiss = contact.snoozed_until

        # A second, genuinely later OOO from the same sender — the ordinary
        # "still out" case, not a duplicate scan of the same message.
        finding2 = _classify_message(OWN, later_ooo_message())
        apply_findings(student, [finding2])

        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_DISMISSED, (
            "a dismissed mail fact must stay dismissed — a later automated "
            "pass may not revive a card the student already closed"
        )
        contact.refresh_from_db()
        assert contact.snoozed_until == snoozed_after_dismiss, (
            "dismissing the card must stop it from moving the student's "
            "own follow-up clock on a later sync"
        )

    def test_undone_ooo_is_not_revived_by_a_later_return_date(self, student, allen):
        contact = Contact.all_objects.create(
            user=student, name="Peter Foggo", email="pfoggo@allenco.com", firm=allen
        )
        finding = _classify_message(OWN, ooo_message())
        apply_findings(student, [finding])

        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_OOO)
        mailfacts.undo(fact)
        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_UNDONE
        contact.refresh_from_db()
        assert contact.snoozed_until is None

        finding2 = _classify_message(OWN, later_ooo_message())
        apply_findings(student, [finding2])

        fact.refresh_from_db()
        assert fact.status == MailFact.STATUS_UNDONE, (
            "an undone mail fact must stay undone — the student's undo said "
            "the OOO snooze should not stand, and a later sync may not "
            "silently reapply it"
        )
        contact.refresh_from_db()
        assert contact.snoozed_until is None, (
            "undo must not be silently reversed by a later automated pass"
        )


# --------------------------------------------------------------------------- #
# Hard bounce: the address is dead, and now there is a row that says so
# --------------------------------------------------------------------------- #
#
# WHAT A HARD BOUNCE DID BEFORE 2026-09-02. `capture.gmail.apply_findings`
# cleared the address and appended a sentence to `Contact.notes`, and
# `mailfacts.consider_finding` returned early on every `bounced` finding, so
# the only artifact was prose. Nothing downstream could ask "is this person
# reachable?", and the Today queue went on producing `follow_up` cards for
# people whose address the receiving server had permanently rejected —
# measured read-only on the founder's account the same day: five live
# contacts with no address, four of them cleared by a bounce on Aug 30, zero
# rows anywhere recording it.
#
# These tests pin the record. The queue's use of it is pinned in
# crm/tests/test_undeliverable_queue.py, and the SOFT/HARD split is pinned in
# both places, because losing it would clear working addresses.


def hard_bounce_message(address="lidia@wellsfargo.example", thread_id="t-hard"):
    """A sendmail-shaped DSN, the same shape as the JPMorgan bounce that hit
    the founder's mailbox: no hyphen in the daemon, "Returned mail" subject,
    permanent 5.1.1 in the transcript."""
    return gmail_message(
        from_header="mailerdaemon@mx.example (Mail Delivery Subsystem)",
        subject="Returned mail: see transcript for details",
        body=(
            "The following addresses had permanent fatal errors:\n"
            f"<{address}>\n550 5.1.1 User unknown\n"
        ),
        thread_id=thread_id,
    )


class TestHardBounceIsRecorded:
    def test_bounce_writes_a_fact_naming_the_dead_address(self, student):
        contact = Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        finding = _classify_message(OWN, hard_bounce_message())
        assert finding["bounced"] is True
        apply_findings(student, [finding])

        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_BOUNCED)
        assert fact.about_email == "lidia@wellsfargo.example"
        assert fact.contact_id == contact.id
        # PENDING, never APPLIED: this module did not do the clearing, so it
        # must not offer an Undo for it (see `_record_bounce`).
        assert fact.status == MailFact.STATUS_PENDING
        # The grounding is structural, so no prose quote — the subject is the
        # message's own words and the card renders it in the quote's place.
        assert fact.quote == ""
        assert fact.subject == "Returned mail: see transcript for details"
        # The action the OTHER module takes still happens, unchanged.
        contact.refresh_from_db()
        assert contact.email == ""

    def test_bounce_with_no_contact_still_records(self, student):
        """A bounce off an address nobody in the network holds is still a
        fact about the mailbox. It lands with no contact, which is what keeps
        it out of the queue's reach and in the "Your mail said" lane."""
        finding = _classify_message(OWN, hard_bounce_message())
        apply_findings(student, [finding])
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_BOUNCED)
        assert fact.contact_id is None

    def test_second_bounce_writes_nothing_new(self, student):
        Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        for thread in ("t-a", "t-b"):
            finding = _classify_message(
                OWN, hard_bounce_message(thread_id=thread)
            )
            apply_findings(student, [finding])
        assert MailFact.objects.for_user(student).filter(
            kind=MailFact.KIND_BOUNCED
        ).count() == 1

    def test_soft_bounce_never_writes_a_bounced_fact(self, student):
        """THE DISTINCTION, pinned at the source. Goldman's postmaster said a
        real banker's mailbox was full. The address works; only today's
        message did not land. It must stay a `routing_address` fact, and it
        must never join `DEAD_ADDRESS_KINDS`."""
        Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        finding = _classify_message(OWN, noah_dsn())
        apply_findings(student, [finding])
        assert not MailFact.objects.for_user(student).filter(
            kind=MailFact.KIND_BOUNCED
        ).exists()
        assert MailFact.objects.for_user(student).filter(
            kind=MailFact.KIND_ROUTING
        ).exists()

    def test_dry_run_records_nothing(self, student):
        Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        finding = _classify_message(OWN, hard_bounce_message())
        out = mailfacts.consider_finding(student, finding, dry_run=True)
        assert out.surfaced == 1
        assert not MailFact.all_objects.filter(kind=MailFact.KIND_BOUNCED).exists()


class TestDeadAddresses:
    """`mailfacts.dead_addresses` — the one definition of "unreachable"."""

    def test_bounce_and_departure_both_count_soft_bounce_does_not(
        self, student, allen
    ):
        bounced = Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        soft = Contact.all_objects.create(
            user=student, name="Noah Bauld", email="noah.bauld@gs.com"
        )
        departed = Contact.all_objects.create(
            user=student, name="Somil Agarwal", email="sagarwal@allenco.com"
        )
        for message in (hard_bounce_message(), noah_dsn(), somil_auto_reply()):
            apply_findings(student, [_classify_message(OWN, message)])

        dead = mailfacts.dead_addresses(student)
        assert set(dead) == {bounced.id, departed.id}
        assert soft.id not in dead
        assert dead[bounced.id].kind == MailFact.KIND_BOUNCED
        assert dead[departed.id].kind == MailFact.KIND_DEPARTED

    def test_dismissed_still_counts_undone_does_not(self, student):
        contact = Contact.all_objects.create(
            user=student, name="Lidia M", email="lidia@wellsfargo.example"
        )
        apply_findings(student, [_classify_message(OWN, hard_bounce_message())])
        fact = MailFact.objects.for_user(student).get(kind=MailFact.KIND_BOUNCED)

        # Dismiss is "I have seen this card", not "the mail started arriving".
        mailfacts.dismiss(fact)
        assert contact.id in mailfacts.dead_addresses(student)

        # Undo is the user saying the address stands. Same asymmetry
        # `address_is_departed` makes.
        fact.status = MailFact.STATUS_UNDONE
        fact.save(update_fields=["status"])
        assert contact.id not in mailfacts.dead_addresses(student)
