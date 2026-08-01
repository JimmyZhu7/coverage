"""The goodbye message after a self-serve delete (docs/specs/settings-page.md
Part 3C, "Delete account, two additions").

`delete_user_and_data` already returns a per-table count of everything it
destroyed and the view already threw it away, flashing "Your account and all
of your data have been deleted" — a sentence the user has to take on faith at
the exact moment they have the least reason to. The counts cost nothing extra
(they are the ORM's own return values) and turn the claim into a receipt.

The gap this file used to document is closed: home.html (the redirect
target) now renders a messages block, so the receipt is actually displayed
at the only moment it can be. `test_the_receipt_renders_on_the_landing_page`
pins that end to end; the earlier tests keep pinning the CONTENT.
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


def test_the_receipt_renders_on_the_landing_page(client, student):
    """End to end: delete, follow the redirect, and the receipt is IN THE
    RESPONSE BODY — not merely queued in the session. This is the half no
    content assertion covers: `get_messages` passing while the template
    dropped the flash on the floor is precisely the bug this file used to
    document as a known gap."""
    firm = Firm.objects.create(name="Evercore", slug="evercore")
    c = Contact.all_objects.create(user=student, name="A", firm=firm)
    Touch.all_objects.create(
        user=student, contact=c, kind="outreach", channel="email",
        ts=timezone.now(),
    )
    client.force_login(student)

    resp = client.post(
        reverse("accounts:delete"),
        {"confirm": "goodbye@example.com"},
        follow=True,
    )
    assert resp.status_code == 200
    assert resp.request["PATH_INFO"] == "/"
    body = resp.content.decode()
    assert "1 contact" in body and "1 touch" in body
