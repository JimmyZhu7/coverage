"""Re-tiering a firm from the Network board.

The board's tier lanes were drag-and-drop only for a long time: cards
carried `draggable="true"` and the lanes listened for
`dragstart`/`dragover`/`drop`. HTML5 drag-and-drop does not fire on a
touchscreen and has no keyboard equivalent, so on a phone or tablet the
whole board was read-only. A per-card `<select>` (`.fc-tier`) was added to
cover that gap, then removed again 2026-08-31 at the founder's direct
call — the board is drag-only once more.

What this file still guarantees: the drag itself still works, the hover
text still does not lie to a device that cannot drag, and the endpoint
both gestures write through (`crm:set_firm_tier`) still takes what it
always took — it is also the endpoint Settings' Target Firms board posts
to, from its OWN independent picker (`.tf-tier`), which is unaffected by
anything in this file and carries touch/keyboard support for the whole
product even though this one page no longer does. See
`accounts/tests/test_settings_page.py` for that board's half of the
guarantee.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import UserFirm
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def student(db):
    return User.objects.create_user(email="tiers@example.com", password="x" * 14)


def test_the_drag_still_works(client, student):
    """The one in-page route left. Cards stay draggable and the lanes stay
    wired to accept a drop."""
    firm = Firm.objects.create(slug="drag", name="Drag Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert '<div class="firm-card kin-reveal" draggable="true"' in body
    for handler in ("dragstart", "dragover", "drop"):
        assert f'"{handler}"' in body


def test_the_hints_do_not_send_touch_to_a_gesture_it_cannot_make(client, student):
    """The board used to tell a phone to drag, full stop. Whatever the
    hover text says now, it must not name dragging as the ONLY way to
    change a tier — it has to name Settings' own picker as the route a
    device that cannot drag actually has."""
    firm = Firm.objects.create(slug="hint", name="Hint Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    body = client.get(reverse("crm:contact_list")).content.decode()
    for banned in (
        "Drag a firm card here to set its tier",
        "Drag a card between lanes to change a firm's tier",
        "Drag to another tier.",
    ):
        assert banned not in body
    assert "Settings" in body and "Target Firms" in body, (
        "the hint must point a device that cannot drag somewhere real"
    )


def test_the_tier_endpoint_takes_a_plain_post(client, student):
    """The endpoint this board's drag and Settings' own picker both write
    through, tested directly against a raw POST rather than through
    either UI — including the empty string Settings' Unranked option
    sends, which the view reads as tier=None."""
    firm = Firm.objects.create(slug="post", name="Post Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    url = reverse("crm:set_firm_tier")
    for value, expected in (("2", 2), ("3", 3), ("", None), ("1", 1)):
        resp = client.post(url, {"firm": firm.id, "tier": value})
        assert resp.status_code == 204, value
        assert UserFirm.all_objects.get(user=student, firm=firm).tier == expected
