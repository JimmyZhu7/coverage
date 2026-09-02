"""The role read off a reply's own signature block (WS-CRM-03).

`audit-crm-lifecycle.md` D2 and E1: role is blank on 136 of the founder's 151
capture rows and `role_hint` is set on 1 of 137 accepted proposals — 0.7% —
because the only source the Gmail door has ever had is a title the sender
happened to type inside their From display name. Ninety live rows carry
neither a role nor a region and 89 of those are cold and queue-eligible: the
rows the queue is about to ask him to chase are the rows it knows least
about.

It matters because referrals flow downward and the seniority a student can
reach is a function of connection strength, so cold outreach belongs with
analysts and associates (`research-networking-norms.md` §2b, §2d). Without a
role that rule cannot be applied at all.

The parser is deterministic and silent when unsure (P1). Every case below is
either a title the sender wrote under their own name, or a blank.
"""

from __future__ import annotations

import pytest

from capture import discovery
from capture.models import ContactProposal
from crm.models import Contact
from directory.models import Firm

from django.contrib.auth import get_user_model

User = get_user_model()


THREE_LINE = """Thanks, happy to chat next week.

Best,
Alex Banker
Investment Banking Analyst
North Bank
"""

NO_SIGNATURE = """Thanks, happy to chat next week. I used to be an Analyst
at another shop but I have moved on since.
"""


# ---------------------------------------------------------------------------
# The parser.
# ---------------------------------------------------------------------------
def test_a_three_line_signature_yields_the_role():
    role, _link = discovery.signature_facts(THREE_LINE, "Alex Banker")
    assert role == "Analyst"


def test_no_signature_yields_a_blank_even_when_the_word_is_in_the_prose():
    """THE ANCHOR IS THE WHOLE DESIGN. A keyword hunt over a body would type
    this sender as an Analyst on the strength of a sentence saying they are
    not one any more."""
    role, _link = discovery.signature_facts(NO_SIGNATURE, "Alex Banker")
    assert role == ""


def test_someone_elses_title_in_the_body_is_not_this_persons_title():
    body = ("Our Managing Director asked me to reach out.\n\n"
            "Alex Banker\nNorth Bank\n")
    assert discovery.signature_facts(body, "Alex Banker")[0] == ""


@pytest.mark.parametrize("line, expected", [
    ("Managing Director", "Managing Director"),
    ("Executive Director, Global Markets", "Executive Director"),
    ("Vice President | Leveraged Finance", "Vice President"),
    ("VP, M&A", "VP"),
    ("Associate", "Associate"),
    ("Summer Analyst", "Analyst"),
    ("Campus Recruiter", "Recruiter"),
    ("Head of Analytics", ""),      # not "Analyst"
    ("Vice Presidents Club", ""),   # not "Vice President"
    ("Chief of Staff", ""),
])
def test_the_title_table_is_short_and_word_bounded(line, expected):
    body = f"Best,\nAlex Banker\n{line}\nNorth Bank\n"
    assert discovery.signature_facts(body, "Alex Banker")[0] == expected


def test_a_title_more_than_three_lines_below_the_name_is_not_read():
    """Past the third line the text is address, disclaimer and pronouns."""
    body = ("Alex Banker\nNorth Bank\n1 Example Street\nNew York, NY\n"
            "Managing Director\n")
    assert discovery.signature_facts(body, "Alex Banker")[0] == ""


def test_a_decorated_name_line_still_anchors():
    body = "-- \nAlex Banker |\nAssociate\n"
    assert discovery.signature_facts(body, "Alex Banker")[0] == "Associate"


def test_an_empty_body_or_an_empty_name_answers_nothing():
    assert discovery.signature_facts("", "Alex Banker") == ("", "")
    assert discovery.signature_facts(THREE_LINE, "") == ("", "")


# ---------------------------------------------------------------------------
# The LinkedIn grab. Parsed and returned; nothing stores it yet — see the
# note in `_signature_role` and WS-CRM-03's report.
# ---------------------------------------------------------------------------
def test_a_profile_url_is_normalised_and_a_company_url_is_refused():
    body = ("Alex Banker\nAnalyst\nhttps://www.linkedin.com/in/alex-banker-123\n")
    assert discovery.signature_facts(body, "Alex Banker")[1] == (
        "https://www.linkedin.com/in/alex-banker-123"
    )
    company = "Alex Banker\nAnalyst\nhttps://www.linkedin.com/company/north-bank\n"
    assert discovery.signature_facts(company, "Alex Banker")[1] == ""


# ---------------------------------------------------------------------------
# The door.
# ---------------------------------------------------------------------------
@pytest.fixture
def student():
    return User.objects.create_user(email="sig@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(slug="north-bank", name="North Bank",
                               domains=["northbank.example"])


def _finding(**over):
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
        "thread_id": "t-sig-1",
        "occurred_at": "2026-08-20T10:00:00+00:00",
    }
    base.update(over)
    return base


@pytest.mark.django_db
def test_a_proposal_takes_the_signature_role_when_the_header_has_none(
        student, firm):
    discovery.consider_finding(student, _finding(body=THREE_LINE))
    p = ContactProposal.objects.for_user(student).get()
    assert p.role_hint == "Analyst"


@pytest.mark.django_db
def test_the_display_name_still_wins(student, firm):
    """The sender typed it in the header, about themselves, on purpose. The
    signature is the FALLBACK, because a body is a noisier place to read."""
    discovery.consider_finding(
        student,
        _finding(name="Alex Banker, Managing Director", body=THREE_LINE),
    )
    p = ContactProposal.objects.for_user(student).get()
    assert p.role_hint == "Managing Director"


@pytest.mark.django_db
def test_no_signature_leaves_the_hint_blank_exactly_as_today(student, firm):
    """P3. This is the whole degradation contract: a finding the parser
    cannot read produces the same proposal it produced before it existed."""
    discovery.consider_finding(student, _finding(body=NO_SIGNATURE))
    p = ContactProposal.objects.for_user(student).get()
    assert p.role_hint == ""
    assert p.recruiting_hint is False


# `transaction=True`: `accept` goes through `crm.services.log_touch`, which
# opens its own psycopg connection and can only see committed rows — the same
# posture as `test_discovery.py`.
@pytest.mark.django_db(transaction=True)
def test_a_recruiting_signature_sets_the_recruiting_hint(student, firm):
    body = "Best,\nAlex Banker\nCampus Recruiter\nNorth Bank\n"
    discovery.consider_finding(student, _finding(body=body))
    p = ContactProposal.objects.for_user(student).get()
    assert p.role_hint == "Recruiter"
    assert p.recruiting_hint is True
    # And it survives the accept, on the same three-state rule the field
    # already documents.
    contact = discovery.accept(p)
    assert isinstance(contact, Contact)
    assert contact.role == "Recruiter"
