"""The goodbye message after a self-serve delete (docs/specs/settings-page.md
Part 3C, "Delete account, two additions").

`delete_user_and_data` already returns a per-table count of everything it
destroyed and the view already threw it away, flashing "Your account and all
of your data have been deleted" — a sentence the user has to take on faith at
the exact moment they have the least reason to. The counts cost nothing extra
(they are the ORM's own return values) and turn the claim into a receipt.

KNOWN GAP, asserted here rather than hidden: the redirect target is the
marketing landing page, and neither `templates/core/home.html` nor
`templates/base.html` renders `{% for m in messages %}`. So this flash — and
the plainer one that preceded it — is queued and never displayed. Those two
templates were outside this change's ownership; the one-line fix is reported
alongside it. These tests pin the message's CONTENT so the fix is a one-liner
in the template and nothing here has to change.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from accounts.views import _deletion_receipt
from crm.models import Contact, Touch, UserFirm
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return User.objects.create_user(email="goodbye@example.com", password="x")


def _flashes(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


def test_the_receipt_itemises_what_was_destroyed(client, student):
    firm = Firm.objects.create(name="Receipt Bank", slug="receipt-bank")
    for i in range(3):
        contact = Contact.all_objects.create(user=student, name=f"Person {i}", firm=firm)
        Touch.all_objects.create(
            user=student, contact=contact, ts=timezone.now(), kind="outreach"
        )
    UserFirm.all_objects.create(user=student, firm=firm, tier=1, status="target")

    client.force_login(student)
    resp = client.post(
        reverse("accounts:delete"), {"confirm": "goodbye@example.com"}, follow=True
    )

    assert not User.objects.filter(email="goodbye@example.com").exists()
    flash = " ".join(_flashes(resp))
    assert "3 contacts" in flash
    assert "3 touches" in flash
    assert "1 target firm" in flash  # singular, not "1 target firms"
    assert "Nothing is retained" in flash


def test_an_empty_account_gets_an_honest_sentence_not_a_row_of_zeros(client, student):
    """Padding the receipt with "0 tasks, 0 fit scores" would read as
    boilerplate — the opposite of the point."""
    client.force_login(student)
    resp = client.post(
        reverse("accounts:delete"), {"confirm": "goodbye@example.com"}, follow=True
    )
    flash = " ".join(_flashes(resp))
    assert "no other data" in flash
    assert "0 " not in flash


def test_only_non_zero_tables_are_named():
    receipt = _deletion_receipt(
        {"contacts": 137, "touches": 138, "tasks": 0, "fit_scores": 0, "account": 1}
    )
    assert "137 contacts" in receipt
    assert "138 touches" in receipt
    assert "task" not in receipt
    assert "fit score" not in receipt


def test_a_wrong_confirmation_deletes_nothing(client, student):
    """The type-to-confirm gate, unchanged — pinned here because the receipt
    work touched this view."""
    client.force_login(student)
    resp = client.post(reverse("accounts:delete"), {"confirm": "not-my@email.com"})
    assert resp.status_code == 200
    # The apostrophe renders escaped, so match the half that doesn't.
    assert "t match. Type your email address exactly" in resp.content.decode()
    assert User.objects.filter(email="goodbye@example.com").exists()
