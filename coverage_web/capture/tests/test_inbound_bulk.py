"""The genuine-reply test, and what a bulk verdict does downstream.

Regression cover for the founder-reported bug: a mass programme invitation
from West Monroe's talent-acquisition manager — someone he had never
written to — was logged as `reply_received`, which ratcheted her warmth to
`replied` and put "ask her for a coffee chat" in his Today queue.

Split in two halves. `TestClassifyInbound` and
`TestClassifyMessageIntegration` are pure header logic and touch no
database at all (mirroring `test_gmail_live.py`'s posture);
`TestBulkFindingsThroughApplyFindings` does, and the module-level
`transaction=True` is there for the usual reason (see `test_gmail.py`'s
module docstring): `log_touch` opens its own psycopg connection, which
cannot see rows written inside pytest's wrapping transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import gmail_live, inbound
from capture.gmail import apply_findings
from coverage_domain.pipeline import BULK_RECEIVED_KIND
from crm.models import CalendarEvent, Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

OWN = "jimmy@example.com"


def _message(headers: dict, snippet: str = "", **extra) -> dict:
    payload = {"headers": [{"name": k, "value": v} for k, v in headers.items()]}
    base = {"threadId": "t1", "snippet": snippet, "payload": payload}
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# Pure header logic
# --------------------------------------------------------------------------- #

class TestClassifyInbound:
    def test_threaded_genuine_reply_stays_genuine(self):
        """The load-bearing case. A real person hitting Reply carries
        `In-Reply-To`, and nothing about that message may be demoted."""
        message = _message({
            "From": "Nick Tehle <nicholas.tehle@jpmorgan.com>",
            "To": OWN,
            "Subject": "Re: USC alumni panel",
            "In-Reply-To": "<abc123@mail.gmail.com>",
            "References": "<abc123@mail.gmail.com>",
        }, snippet="Happy to help with a panel, before 10pm ET.")
        verdict = inbound.classify_inbound(OWN, message)
        assert verdict.is_bulk is False
        assert verdict.threaded_reply is True

    def test_plain_personal_first_contact_is_not_bulk(self):
        """No list headers, no reply pointer — an unsolicited but personal
        note. Left genuine: erring toward 'a person wrote to me' is the
        direction this module is deliberately wrong in."""
        message = _message({
            "From": "Kristin Welty <kwelty@deloitte.com>",
            "To": OWN,
            "Subject": "Introducing you to our campus recruiter",
        }, snippet="Looping in Myra, our USC campus recruiter.")
        assert inbound.classify_inbound(OWN, message).is_bulk is False

    def test_list_unsubscribe_blast_is_bulk(self):
        message = _message({
            "From": "Caroline Baenen <cbaenen@westmonroe.com>",
            "To": OWN,
            "Subject": "West Monroe Sophomore Series",
            "List-Unsubscribe": "<https://westmonroe.example/u/1>",
        }, snippet="As a participant in West Monroe's Sophomore Series...")
        verdict = inbound.classify_inbound(OWN, message)
        assert verdict.is_bulk is True
        assert "unsubscribe" in verdict.reason_text.lower()

    def test_no_reply_sender_is_bulk_even_inside_a_thread(self):
        """Tier 1: an unattended address is not a person, so the
        `In-Reply-To` escape hatch must not rescue it."""
        message = _message({
            "From": "no-reply@careers.example.com",
            "To": OWN,
            "Subject": "Re: Your application",
            "In-Reply-To": "<x@y>",
        })
        verdict = inbound.classify_inbound(OWN, message)
        assert verdict.is_bulk is True
        assert "unattended" in verdict.reason_text

    @pytest.mark.parametrize("localpart", [
        "noreply", "no.reply", "do-not-reply", "donotreply", "notifications",
    ])
    def test_unattended_localpart_variants(self, localpart):
        assert inbound.looks_like_noreply(f"{localpart}@example.com") is True

    @pytest.mark.parametrize("address", [
        "careers@bank.example",       # a human answers this one
        "recruiting@bank.example",
        "info@bank.example",
        "replyall@bank.example",      # contains "reply", is not "no-reply"
    ])
    def test_addresses_a_human_answers_are_not_unattended(self, address):
        assert inbound.looks_like_noreply(address) is False

    def test_out_of_office_is_bulk_despite_being_a_real_thread_reply(self):
        message = _message({
            "From": "Jane Banker <jane@bank.example>",
            "To": OWN,
            "Subject": "Automatic reply: coffee chat",
            "In-Reply-To": "<x@y>",
            "Auto-Submitted": "auto-replied",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is True

    def test_precedence_bulk_is_bulk(self):
        message = _message({
            "From": "Jane Banker <jane@bank.example>",
            "To": OWN, "Subject": "Newsletter", "Precedence": "bulk",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is True

    def test_machine_only_separates_a_robot_from_a_list(self):
        """"Software composed this" and "software sent this to a list" are
        two different statements, and only the first one can describe a
        calendar server relaying one person's invite to one other person.
        `machine_only` is the fact `capture.gmail_live._ics_rsvp` needs to
        tell them apart; it is False for every list-shaped signal, including
        one riding on the SAME message as the auto-reply header (the tier-1
        early return used to mean the list evidence was never even read)."""
        robot = _message({
            "From": "Alice Ng <alice@firm.example>", "To": OWN,
            "Subject": "Accepted: Coffee chat @ Wed Sep 2, 2026",
            "Auto-Submitted": "auto-replied",
        })
        assert inbound.classify_inbound(OWN, robot).machine_only is True

        blast = _message({
            "From": "Programme <programme@firm.example>", "To": OWN,
            "Subject": "Invitation: Sophomore Series",
            "Auto-Submitted": "auto-generated",
            "List-Unsubscribe": "<https://firm.example/u/1>",
        })
        assert inbound.classify_inbound(OWN, blast).is_bulk is True
        assert inbound.classify_inbound(OWN, blast).machine_only is False

        listed = _message({
            "From": "Someone <someone@list.example>",
            "To": "usc-finance@list.example", "Subject": "Weekly digest",
            "List-Id": "<usc-finance.list.example>",
            "Auto-Submitted": "auto-generated",
        })
        assert inbound.classify_inbound(OWN, listed).machine_only is False

    def test_machine_only_is_false_when_nothing_is_bulk(self):
        message = _message({
            "From": "Alice Ng <alice@firm.example>", "To": OWN,
            "Subject": "Re: coffee chat",
        })
        verdict = inbound.classify_inbound(OWN, message)
        assert verdict.is_bulk is False
        assert verdict.machine_only is False

    def test_mailing_list_headers_are_bulk(self):
        message = _message({
            "From": "Someone <someone@list.example>",
            "To": "usc-finance@list.example", "Subject": "Weekly digest",
            "List-Id": "<usc-finance.list.example>",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is True

    def test_corporate_reply_carrying_unsubscribe_is_rescued_by_the_thread(self):
        """The false-positive this module is most afraid of: a recruiter
        typing a genuine answer from a system that stamps List-Unsubscribe
        on everything. `In-Reply-To` keeps her a reply."""
        message = _message({
            "From": "Kristin Welty <kwelty@deloitte.com>",
            "To": OWN,
            "Subject": "RE: USC panel",
            "List-Unsubscribe": "<mailto:u@deloitte.example>",
            "In-Reply-To": "<jimmy-sent@mail.gmail.com>",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is False

    def test_wide_recipient_list_alone_is_not_enough(self):
        """A weak signal on its own describes plenty of genuine mail — a
        looped-in thread, a shared intro. It takes two."""
        many = ", ".join(f"p{i}@example.com" for i in range(9)) + f", {OWN}"
        message = _message({
            "From": "Jane Banker <jane@bank.example>", "To": many,
            "Subject": "Panel logistics",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is False

    def test_wide_list_plus_owner_not_addressed_is_bulk(self):
        many = ", ".join(f"p{i}@example.com" for i in range(9))
        message = _message({
            "From": "Jane Banker <jane@bank.example>", "To": many,
            "Subject": "Programme invitation",
        })
        assert inbound.classify_inbound(OWN, message).is_bulk is True


class TestClassifyMessageIntegration:
    def test_blast_becomes_a_bulk_finding_not_a_reply(self):
        message = _message({
            "From": "Caroline Baenen <cbaenen@westmonroe.com>",
            "To": OWN,
            "Subject": "West Monroe Sophomore Series",
            "List-Unsubscribe": "<https://westmonroe.example/u/1>",
        }, snippet="As a participant in West Monroe's Sophomore Series...")
        finding = gmail_live._classify_message(OWN, message)
        assert finding["bulk"] is True
        assert finding["replied"] is False
        assert finding["chat_status"] == "none"
        assert finding["bulk_reasons"]
        # The message is kept, not discarded — subject survives.
        assert "Sophomore Series" in finding["evidence"]

    def test_genuine_reply_still_produces_a_reply_finding(self):
        message = _message({
            "From": "Nick Tehle <nicholas.tehle@jpmorgan.com>",
            "To": OWN, "Subject": "Re: USC alumni panel",
            "In-Reply-To": "<abc@mail.gmail.com>",
        }, snippet="Happy to help with a panel.")
        finding = gmail_live._classify_message(OWN, message)
        assert finding["replied"] is True
        assert finding["bulk"] is False

    def test_a_bounce_is_still_a_bounce(self):
        """The bounce branch runs BEFORE the bulk test and must keep
        winning: a DSN carries Auto-Submitted, and calling it 'bulk' would
        lose the failed-address information entirely."""
        message = _message({
            "From": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
            "To": OWN, "Subject": "Delivery Status Notification (Failure)",
            "Auto-Submitted": "auto-replied",
        }, snippet="Your message to typo@bank.example could not be delivered.")
        finding = gmail_live._classify_message(OWN, message)
        assert finding["bounced"] is True
        assert finding.get("bulk") is not True


# --------------------------------------------------------------------------- #
# What a bulk finding does to a contact
# --------------------------------------------------------------------------- #

@pytest.fixture
def student(db):
    return User.objects.create_user(email="bulk-student@example.com", password="x")


@pytest.fixture
def contact(student):
    return Contact.all_objects.create(
        user=student, name="Caroline Baenen",
        email="cbaenen@westmonroe.example", source="manual",
    )


def _finding(**over):
    base = {
        "name": "Caroline Baenen",
        "email": "cbaenen@westmonroe.example",
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": False,
        "chat_status": "none",
        "evidence": "Bulk/automated email: Sophomore Series",
        "thread_id": "t-bulk",
        "bulk": True,
        "bulk_reasons": "unsubscribe headers (list-unsubscribe)",
    }
    base.update(over)
    return base


class TestBulkFindingsThroughApplyFindings:
    def test_bulk_logs_a_touch_and_moves_nothing(self, student, contact):
        result = apply_findings(student, [_finding()])
        assert result.bulk_logged == 1
        assert result.touches_logged == 0

        contact.refresh_from_db()
        assert contact.warmth == "cold"
        assert contact.thread_state == "no_reply"

        touch = Touch.objects.for_user(student).get(contact=contact)
        assert touch.kind == BULK_RECEIVED_KIND
        # Honest, not silent: the message AND the reason are both kept.
        assert "Sophomore Series" in touch.note
        assert "unsubscribe" in touch.note

    def test_bulk_overrides_a_replied_flag_on_the_same_finding(self, student, contact):
        apply_findings(student, [_finding(replied=True, chat_status="completed")])
        contact.refresh_from_db()
        assert contact.warmth == "cold"
        assert (
            Touch.objects.for_user(student).get(contact=contact).kind
            == BULK_RECEIVED_KIND
        )

    def test_bulk_does_not_put_a_chat_on_the_calendar(self, student, contact):
        apply_findings(student, [_finding(
            chat_status="scheduled",
            chat_scheduled_at=(timezone.now() + timedelta(days=3)).isoformat(),
        )])
        assert CalendarEvent.all_objects.filter(user=student).count() == 0

    def test_bulk_banks_no_email_pattern_evidence(self, student, contact):
        result = apply_findings(student, [_finding()])
        assert result.pattern_delivered == 0
        contact.refresh_from_db()
        assert contact.email_pattern_recorded is False

    def test_bulk_is_deduped_per_thread(self, student, contact):
        apply_findings(student, [_finding()])
        second = apply_findings(student, [_finding()])
        assert second.bulk_logged == 0
        assert second.skipped_already_logged == 1
        assert Touch.objects.for_user(student).filter(contact=contact).count() == 1

    def test_bulk_never_blocks_a_later_genuine_reply_on_the_same_thread(
        self, student, contact
    ):
        """The reason `bulk_received` is off THREAD_STAGE_RANK. A blast
        landing on a thread must not become a stage the real reply then
        has to outrank."""
        apply_findings(student, [_finding()])
        apply_findings(student, [_finding(
            bulk=False, replied=True, evidence="Sure, happy to chat",
        )])
        contact.refresh_from_db()
        assert contact.warmth == "replied"
        assert contact.thread_state == "replied"
        kinds = set(
            Touch.objects.for_user(student)
            .filter(contact=contact).values_list("kind", flat=True)
        )
        assert kinds == {BULK_RECEIVED_KIND, "reply_received"}

    def test_findings_without_the_bulk_key_behave_exactly_as_before(
        self, student, contact
    ):
        """Every finding the daily agent-run sync has ever produced lacks
        the key entirely; absence must mean 'not bulk', never 'unknown'."""
        legacy = _finding()
        del legacy["bulk"]
        del legacy["bulk_reasons"]
        legacy["replied"] = True
        apply_findings(student, [legacy])
        contact.refresh_from_db()
        assert contact.warmth == "replied"


# --------------------------------------------------------------------------- #
# addressed_to_user — the To:/Cc: half of "someone wrote to YOU".
#
# Live case (2026-08-25, founder's mailbox, read-only): a West Monroe
# coordinator's "RE:" follow-up to their own mass invite carried In-Reply-To
# (it threads) and named only the firm's own people on To:/Cc:. The verdict
# reports the addressing fact on its own so contact discovery can make it
# decisive without re-parsing headers.
# --------------------------------------------------------------------------- #

class TestAddressedToUser:
    def test_a_reply_addressed_to_the_user_reports_true(self):
        verdict = inbound.classify_inbound(OWN, _message({
            "From": "Lily Liu <lily.liu@barclays.com>",
            "To": OWN,
            "Subject": "RE: USC | HSBC | Barclays - Coffee Chat Request",
            "In-Reply-To": "<mine@usc.edu>",
        }))
        assert verdict.is_bulk is False
        assert verdict.addressed_to_user is True

    def test_a_threaded_reply_that_never_names_the_user_reports_false(self):
        verdict = inbound.classify_inbound(OWN, _message({
            "From": "Coordinator <ttrinh@westmonroe.com>",
            "To": "campusrecruiting@westmonroe.com",
            "Cc": "cbaenen@westmonroe.com",
            "Subject": "RE: You're Invited: Ask Me Anything",
            "In-Reply-To": "<blast@westmonroe.com>",
        }))
        # One weak signal alone is not bulk, and the reply pointer holds —
        # but the addressing fact is reported for stricter surfaces.
        assert verdict.threaded_reply is True
        assert verdict.addressed_to_user is False

    def test_empty_recipients_default_open(self):
        """Absence of To:/Cc: is absence of evidence, not evidence of
        absence — an undisclosed-recipients message must not flip the fact
        to False."""
        verdict = inbound.classify_inbound(OWN, _message({
            "From": "Someone <someone@bank.example>",
            "Subject": "Re: hello",
            "In-Reply-To": "<mine@usc.edu>",
        }))
        assert verdict.addressed_to_user is True

    def test_cc_counts_as_addressed(self):
        verdict = inbound.classify_inbound(OWN, _message({
            "From": "Banker <banker@bank.example>",
            "To": "colleague@bank.example",
            "Cc": OWN,
            "Subject": "Re: intro",
            "In-Reply-To": "<mine@usc.edu>",
        }))
        assert verdict.addressed_to_user is True

    def test_the_live_finding_carries_the_fact(self):
        finding = gmail_live._classify_message(OWN, _message({
            "From": "Coordinator <ttrinh@westmonroe.com>",
            "To": "campusrecruiting@westmonroe.com",
            "Subject": "RE: You're Invited",
            "In-Reply-To": "<blast@westmonroe.com>",
        }, internalDate="1787604316000"))
        assert finding is not None
        assert finding["replied"] is True
        assert finding["addressed_to_user"] is False
