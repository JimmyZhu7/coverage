"""The first-run SENT SWEEP — `gmail_live._sent_sweep_message_ids` and
`backfill_connection(sweep_sent=True)`.

THE COLD START THIS EXISTS FOR: a student connects Gmail on an empty
Coverage. The per-contact backfill loop builds no queries (there are no
contacts), finds nothing, and the product's liveliest feature does nothing
on the day it matters most. The sweep reads the student's OWN recent sent
mail instead, and every recipient at a directory firm becomes a
`ContactProposal` waiting for a tap.

What the tests below are actually pinning, in order of how badly it would
hurt to lose:

1. Nothing is CREATED. Not one Contact, not one Touch, from a sweep alone.
   A previous agent bypassed the proposal and put 13 unreviewed people into
   the founder's CRM; that is what `test_the_sweep_creates_no_contacts_and_
   no_touches` is standing guard over.
2. Every refusal in `capture.discovery`'s ladder still holds when the
   evidence arrives from a sweep rather than from the live listener —
   including the two that only a batch can see (bounce, mail merge), which
   is why the sweep joins the per-contact ids in ONE `apply_findings` call.
3. Re-running proposes nobody twice.
4. The near-empty mailbox — a sophomore in September with three mails to
   friends — proposes nobody and errors on nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from capture import gmail_live
from capture.models import ContactProposal, GmailConnection
from crm.models import Contact, Touch
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

OWN = "sweep-student@usc.edu"


@pytest.fixture
def student():
    return User.objects.create_user(email=OWN, password="x")


@pytest.fixture
def connection(student):
    return GmailConnection.all_objects.create(
        user=student,
        gmail_address=OWN,
        refresh_token_encrypted="unused-in-these-tests",
        backfill_status="pending",
    )


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", domains=["northbank.example"]
    )


def _sent(*, to: str, subject: str, ms: int, from_addr: str = OWN, thread: str = ""):
    """One outbound message, exactly the shape `_classify_message` reads."""
    return {
        "threadId": thread or f"t-{ms}",
        "snippet": subject,
        "internalDate": str(ms),
        "labelIds": ["SENT"],
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
        },
    }


def _query_aware_client(by_query: dict[str, list[str]], messages: dict[str, dict]):
    """A fake Gmail client that answers `messages.list` DIFFERENTLY per `q`.

    The existing backfill fixture returns one canned list for every call,
    which cannot tell a per-contact search from the sweep's `in:sent` — and
    the whole claim under test is that the sweep finds mail the per-contact
    loop provably cannot. Queries are matched on substring, so a test can
    key on `"in:sent"` without reproducing the date arithmetic.
    """
    client = MagicMock()

    def _list(*, userId, q, pageToken=None):  # noqa: A002 — the real API's kwarg names
        ids: list[str] = []
        for fragment, matched in by_query.items():
            if fragment in q:
                ids = matched
                break
        mock = MagicMock()
        mock.execute.return_value = {"messages": [{"id": i} for i in ids]}
        return mock

    def _get(*, userId, id, format):  # noqa: A002
        mock = MagicMock()
        mock.execute.return_value = messages[id]
        return mock

    client.users.return_value.messages.return_value.list.side_effect = _list
    client.users.return_value.messages.return_value.get.side_effect = _get
    return client


def _now_ms(days_ago: int = 1) -> int:
    return int((timezone.now().timestamp() - days_ago * 86400) * 1000)


# --------------------------------------------------------------------------- #
# The query itself
# --------------------------------------------------------------------------- #

class TestSweepQuery:
    def test_it_asks_only_for_sent_mail_inside_the_window(self):
        client = _query_aware_client({"in:sent": ["m1"]}, {})
        gmail_live._sent_sweep_message_ids(client, now=timezone.now())

        (_, kwargs), = [
            call for call in
            client.users.return_value.messages.return_value.list.call_args_list
        ]
        assert "in:sent" in kwargs["q"]
        assert "after:" in kwargs["q"]
        # The only inbound this sweep reads is the two machine senders the
        # bounce guard needs (see `_sent_sweep_message_ids`). Nothing else
        # from the inbox.
        assert "from:mailer-daemon" in kwargs["q"]
        assert "from:postmaster" in kwargs["q"]
        assert "in:inbox" not in kwargs["q"]
        assert "in:anywhere" not in kwargs["q"]

    def test_paging_stops_at_the_cap_rather_than_truncating_after(self, monkeypatch):
        """The ceiling is on API CALLS. A ten-year mailbox must not cost ten
        years of `messages.list` pages just to throw the tail away."""
        monkeypatch.setattr(gmail_live, "SWEEP_MAX_MESSAGES", 3)
        client = MagicMock()
        calls = {"n": 0}

        def _list(*, userId, q, pageToken=None):
            calls["n"] += 1
            mock = MagicMock()
            mock.execute.return_value = {
                "messages": [{"id": f"m{calls['n']}-{i}"} for i in range(2)],
                "nextPageToken": "more",
            }
            return mock

        client.users.return_value.messages.return_value.list.side_effect = _list
        ids = gmail_live._sent_sweep_message_ids(client, now=timezone.now())

        assert len(ids) == 3
        assert calls["n"] == 2  # stopped mid-page-two, not "keep paging forever"


# --------------------------------------------------------------------------- #
# The whole path: sweep -> classify -> apply_findings -> discovery -> proposal
# --------------------------------------------------------------------------- #

class TestSweepProposes:
    def test_a_recipient_at_a_firm_domain_is_proposed(self, student, connection, firm):
        messages = {
            "m1": _sent(
                to="Alex Banker <alex.banker@northbank.example>",
                subject="USC | coffee chat | North Bank",
                ms=_now_ms(2),
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection, sweep_sent=True)

        assert result.proposals_created == 1
        proposal = ContactProposal.objects.for_user(student).get()
        assert proposal.email == "alex.banker@northbank.example"
        assert proposal.firm_id == firm.id
        assert proposal.status == ContactProposal.STATUS_PENDING
        # Warmth is never fabricated from the student's own enthusiasm.
        assert proposal.evidence_kind == "outreach"

    def test_the_sweep_creates_no_contacts_and_no_touches(
        self, student, connection, firm
    ):
        """THE RULE. A proposal is an offer; only a tap is consent."""
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            gmail_live.backfill_connection(connection, sweep_sent=True)

        assert not Contact.objects.for_user(student).exists()
        assert not Touch.objects.for_user(student).exists()
        assert ContactProposal.objects.for_user(student).count() == 1

    def test_it_finds_people_the_per_contact_loop_provably_cannot(
        self, student, connection, firm
    ):
        """The cold start in one assertion: with the sweep off, the same
        mailbox yields nothing, because there is no contact to search for."""
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            without = gmail_live.backfill_connection(
                connection, sweep_sent=False, update_backfill_status=False
            )
        assert without.findings == 0
        assert not ContactProposal.objects.for_user(student).exists()

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            withsweep = gmail_live.backfill_connection(connection, sweep_sent=True)
        assert withsweep.proposals_created == 1

    def test_dry_run_reports_but_writes_no_proposal(self, student, connection, firm):
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(
                connection, sweep_sent=True, dry_run=True
            )

        assert result.proposals_created == 1
        assert not ContactProposal.all_objects.filter(user=student).exists()
        connection.refresh_from_db()
        assert connection.backfill_status == "pending"

    def test_a_second_run_proposes_nobody_twice(self, student, connection, firm):
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            gmail_live.backfill_connection(connection, sweep_sent=True)
            second = gmail_live.backfill_connection(connection, sweep_sent=True)

        assert second.proposals_created == 0
        assert ContactProposal.objects.for_user(student).count() == 1

    def test_a_dismissed_person_is_never_re_proposed_by_a_later_sweep(
        self, student, connection, firm
    ):
        from capture import discovery

        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            gmail_live.backfill_connection(connection, sweep_sent=True)
        discovery.dismiss(ContactProposal.objects.for_user(student).get())

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            again = gmail_live.backfill_connection(connection, sweep_sent=True)

        assert again.proposals_created == 0
        assert ContactProposal.objects.for_user(student).count() == 1
        assert (
            ContactProposal.objects.for_user(student).get().status
            == ContactProposal.STATUS_DISMISSED
        )

    def test_one_note_to_three_recipients_proposes_all_three(
        self, student, connection, firm
    ):
        """`classify_message_findings` fans a multi-recipient send out per
        To:, and three under `MERGE_RECIPIENT_LIMIT` is a real small burst,
        not a merge."""
        messages = {
            "m1": _sent(
                to=(
                    "a.one@northbank.example, b.two@northbank.example, "
                    "c.three@northbank.example"
                ),
                subject="USC students visiting your desk",
                ms=_now_ms(2),
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection, sweep_sent=True)

        assert result.proposals_created == 3


# --------------------------------------------------------------------------- #
# The refusal ladder, reached through the sweep
# --------------------------------------------------------------------------- #

class TestSweepRefusals:
    def _run(self, connection, messages):
        client = _query_aware_client({"in:sent": list(messages)}, messages)
        with patch.object(gmail_live, "_gmail_client", return_value=client):
            return gmail_live.backfill_connection(connection, sweep_sent=True)

    def test_a_recipient_at_no_known_firm_is_refused(self, student, connection, firm):
        """The outbound bar is a directory-firm address and nothing less —
        otherwise the sweep becomes "everyone I have ever emailed"."""
        result = self._run(connection, {
            "m1": _sent(to="mom@gmail.com", subject="hi", ms=_now_ms(2)),
        })
        assert result.proposals_created == 0
        assert not ContactProposal.objects.for_user(student).exists()

    def test_a_role_account_at_a_firm_domain_is_refused(
        self, student, connection, firm
    ):
        result = self._run(connection, {
            "m1": _sent(
                to="careers@northbank.example", subject="Application", ms=_now_ms(2)
            ),
        })
        assert result.proposals_created == 0

    def test_an_ats_or_esp_domain_is_refused(self, student, connection, firm):
        Firm.objects.create(
            slug="ats-shaped", name="ATS Shaped", domains=["greenhouse.io"]
        )
        result = self._run(connection, {
            "m1": _sent(
                to="someone@greenhouse.io", subject="Application", ms=_now_ms(2)
            ),
        })
        assert result.proposals_created == 0

    def test_the_students_own_institution_is_refused(self, student, connection):
        """usc.edu is where the student's own mailbox lives — a reply from
        the housing desk is a campus relationship, not a networking find."""
        Firm.objects.create(slug="usc", name="USC", domains=["usc.edu"])
        result = self._run(connection, {
            "m1": _sent(
                to="housing@usc.edu", subject="Room change", ms=_now_ms(2)
            ),
        })
        assert result.proposals_created == 0

    def test_a_mail_merge_in_the_same_sweep_is_refused(
        self, student, connection, firm
    ):
        """The batch-level guard, and the reason the sweep must share ONE
        `apply_findings` call with the per-contact pass rather than running
        its own: more than `MERGE_RECIPIENT_LIMIT` distinct recipients under
        one normalized subject is a blast, whoever they are."""
        ms = _now_ms(2)
        messages = {
            f"m{i}": _sent(
                to=f"person{i}@northbank.example",
                subject="Fall 2026 ICC Alumni Digital Panel Outreach",
                ms=ms + i,
                thread=f"t{i}",
            )
            for i in range(6)
        }
        result = self._run(connection, messages)
        assert result.proposals_created == 0

    def test_a_send_that_bounced_in_the_same_sweep_is_refused(
        self, student, connection, firm
    ):
        """The send provably never reached a person. The bounce arrives as
        its own message in the same window, so one batch sees both."""
        import base64

        ms = _now_ms(2)
        dsn = (
            "Address not found\r\n"
            "Your message wasn't delivered to gone.person@northbank.example "
            "because the address couldn't be found.\r\n"
            "Final-Recipient: rfc822; gone.person@northbank.example\r\n"
        )
        bounce = {
            "threadId": "t-bounce",
            "snippet": "Address not found",
            "internalDate": str(ms + 1000),
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Mail Delivery Subsystem "
                                              "<mailer-daemon@googlemail.com>"},
                    {"name": "To", "value": OWN},
                    {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
                ],
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(dsn.encode()).decode(),
                },
            },
        }
        messages = {
            "m1": _sent(
                to="gone.person@northbank.example", subject="Hello", ms=ms
            ),
            "m2": bounce,
        }
        result = self._run(connection, messages)
        assert result.proposals_created == 0

    def test_an_archived_contact_is_reported_never_resurrected(
        self, student, connection, firm
    ):
        Contact.all_objects.create(
            user=student, name="Alex Banker",
            email="alex.banker@northbank.example", source="manual", archived=True,
        )
        result = self._run(connection, {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        })
        assert result.proposals_created == 0
        assert result.proposals_archived_match == 1
        assert Contact.all_objects.get(user=student).archived is True

    def test_someone_already_in_coverage_gets_a_touch_not_a_proposal(
        self, student, connection, firm
    ):
        contact = Contact.all_objects.create(
            user=student, name="Alex Banker",
            email="alex.banker@northbank.example", source="manual",
        )
        result = self._run(connection, {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        })
        assert result.proposals_created == 0
        assert not ContactProposal.objects.for_user(student).exists()
        assert Touch.objects.for_user(student).filter(contact=contact).exists()


# --------------------------------------------------------------------------- #
# The mailbox that has almost nothing in it
# --------------------------------------------------------------------------- #

class TestNearEmptyMailbox:
    def test_a_sophomore_with_three_personal_mails_gets_nothing_and_no_error(
        self, student, connection, firm
    ):
        """September, three emails, none to a bank. The honest answer is an
        empty proposals lane and Today's own "No contacts yet" state — NOT
        an error, and not a card for the roommate."""
        ms = _now_ms(3)
        messages = {
            "m1": _sent(to="roommate@gmail.com", subject="dinner?", ms=ms),
            "m2": _sent(to="prof@some-college.edu", subject="office hours", ms=ms + 1),
            "m3": _sent(to="mom@yahoo.com", subject="call later", ms=ms + 2),
        }
        client = _query_aware_client({"in:sent": list(messages)}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection, sweep_sent=True)

        assert result.proposals_created == 0
        assert not ContactProposal.objects.for_user(student).exists()
        assert not Contact.objects.for_user(student).exists()
        # And the run still COMPLETES — a quiet mailbox is not a failure.
        connection.refresh_from_db()
        assert connection.backfill_status == "done"

    def test_a_completely_empty_mailbox_completes(self, student, connection):
        client = _query_aware_client({}, {})
        with patch.object(gmail_live, "_gmail_client", return_value=client):
            result = gmail_live.backfill_connection(connection, sweep_sent=True)
        assert result.findings == 0
        connection.refresh_from_db()
        assert connection.backfill_status == "done"


# --------------------------------------------------------------------------- #
# Who turns the sweep on
# --------------------------------------------------------------------------- #

class TestSweepIsFirstConnectOnly:
    def test_the_backfill_command_sweeps(self, student, connection, firm):
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live"
                   ".is_configured", return_value=True):
            call_command("gmail_backfill")

        assert ContactProposal.objects.for_user(student).count() == 1

    def test_the_rescan_does_not_sweep(self, student, connection, firm):
        """"Scan Now" is repeatable and runs against an account that already
        has contacts. Re-reading the whole sent folder on every press is
        neither what the button says nor what it should cost."""
        connection.backfill_status = "done"
        connection.rescan_status = "pending"
        connection.save(update_fields=["backfill_status", "rescan_status"])
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": ["m1"]}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live"
                   ".is_configured", return_value=True):
            call_command("gmail_backfill")

        assert not ContactProposal.objects.for_user(student).exists()

    def test_the_import_triggered_scan_does_not_sweep(self, student, connection, firm):
        contact = Contact.all_objects.create(
            user=student, name="Known Person",
            email="known@northbank.example", source="import",
        )
        messages = {
            "m1": _sent(
                to="alex.banker@northbank.example", subject="Hello", ms=_now_ms(2)
            ),
        }
        client = _query_aware_client({"in:sent": []}, messages)

        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch.object(gmail_live, "is_configured", return_value=True):
            gmail_live.backfill_new_contacts(student, [contact])

        assert not ContactProposal.objects.for_user(student).exists()
