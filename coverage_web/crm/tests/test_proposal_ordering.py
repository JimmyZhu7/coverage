"""Proposals lane ordering, and its agreement with the bulk verbs.

THE MEASURED GAP. `crm.today._cockpit_context` used to order pending
`ContactProposal` rows by `created` — when the SCAN wrote the row — capped at
`PROPOSALS_RENDER_CAP`. A first-connect mailbox sweep writes every proposal it
finds within the same second (205 people in one pass on the founder's real
mailbox), so `created` carries no signal at that volume and which 24 rendered
was effectively random. This module pins the replacement rule (evidence
recency: `occurred_at`, the date of the mail that produced the proposal) and
the contract it must not break: `crm.today.rendered_proposals_qs` is the ONE
query both `_cockpit_context` and `crm.views.proposals_bulk` read, so the lane
and the bulk buttons can never render/act on different slices (the bug this
repo already shipped once — "Dismiss all" burying 28 people a 24-cap lane
never showed).

`transaction=True`, matching `test_today.py`: `proposals_bulk`'s accept path
goes through `capture.discovery.accept`, which calls `crm.services.log_touch`
— a separate psycopg connection outside Django's test transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture.models import ContactProposal
from crm.today import PROPOSALS_RENDER_CAP, _cockpit_context, rendered_proposals_qs

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="proposals@example.com", **kw):
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _proposal(user, *, name, email, occurred_at=None):
    return ContactProposal.all_objects.create(
        user=user, name=name, email=email, occurred_at=occurred_at,
    )


def _backdate_created(proposal, when):
    # `created` is `auto_now_add`, so it can only be set with a raw update
    # after the row exists — exactly the shape a real sweep produces: it is
    # the WRITE time, and a fixture that only ever writes "now" could never
    # reproduce "everyone written this second, mail dates scattered across
    # months".
    ContactProposal.all_objects.filter(pk=proposal.pk).update(created=when)


# ---------------------------------------------------------------------------
# The ordering rule itself.
# ---------------------------------------------------------------------------
def test_evidence_recency_beats_scan_insertion_order():
    """A banker who replied last week must sort above a cold send from March
    — the exact inversion the brief describes — even when the database
    row for the OLD evidence was written to the table after the row for the
    recent one, which is the shape a mailbox sweep produces when it visits
    threads out of mail-date order."""
    user = _user()
    now = timezone.now()

    cold_send = _proposal(
        user, name="Cold March Send", email="cold@example.com",
        occurred_at=now - timedelta(days=150),
    )
    recent_reply = _proposal(
        user, name="Recent Reply", email="recent@example.com",
        occurred_at=now - timedelta(days=5),
    )
    # Both rows land in the SAME scan, `created` seconds apart, with the
    # recent evidence's row written FIRST — the case `created`-ordering
    # cannot tell apart from "the reply is newer".
    _backdate_created(recent_reply, now - timedelta(days=200))
    _backdate_created(cold_send, now - timedelta(days=200) + timedelta(seconds=1))

    ordered = list(rendered_proposals_qs(user))
    assert [p.name for p in ordered] == ["Recent Reply", "Cold March Send"]


def test_a_proposal_with_no_evidence_date_falls_back_to_created_not_the_top():
    """`occurred_at` is nullable (a Date header that failed to parse). The
    unparseable row must not jump to the front of a recency sort just
    because it has no date to compare — it falls back to `created`, the
    row's only other timestamp, and sorts among its peers rather than at
    either extreme."""
    user = _user()
    now = timezone.now()

    dated = _proposal(
        user, name="Has A Date", email="dated@example.com",
        occurred_at=now - timedelta(days=1),
    )
    undated = _proposal(
        user, name="No Date Parsed", email="undated@example.com",
        occurred_at=None,
    )
    _backdate_created(undated, now - timedelta(days=10))

    ordered = list(rendered_proposals_qs(user))
    assert [p.name for p in ordered] == ["Has A Date", "No Date Parsed"]


def test_cockpit_context_uses_the_shared_ordering():
    """`_cockpit_context`'s `proposals` list is not a second copy of the
    ordering rule — it is `rendered_proposals_qs` sliced at the render cap."""
    user = _user()
    now = timezone.now()
    older = _proposal(
        user, name="Older Evidence", email="older@example.com",
        occurred_at=now - timedelta(days=30),
    )
    newer = _proposal(
        user, name="Newer Evidence", email="newer@example.com",
        occurred_at=now - timedelta(days=1),
    )
    _backdate_created(older, now - timedelta(days=1))
    _backdate_created(newer, now - timedelta(days=30))

    ctx = _cockpit_context(user)
    assert [p.name for p in ctx["proposals"]] == ["Newer Evidence", "Older Evidence"]


# ---------------------------------------------------------------------------
# The lane and the bulk verbs must act on the identical slice.
# ---------------------------------------------------------------------------
def test_bulk_dismiss_acts_on_exactly_the_rendered_slice(client):
    """The regression this module exists to catch a second time: a sweep
    that leaves more pending than `PROPOSALS_RENDER_CAP` must not let
    "Dismiss all" touch anyone the lane didn't show. Acted-on names must
    equal rendered names, and the overflow must survive untouched."""
    user = _user()
    client.force_login(user)
    now = timezone.now()

    total = PROPOSALS_RENDER_CAP + 5
    proposals = []
    for i in range(total):
        p = _proposal(
            user, name=f"Person {i}", email=f"person{i}@example.com",
            occurred_at=now - timedelta(days=i),
        )
        proposals.append(p)

    ctx = _cockpit_context(user)
    rendered_names = {p.name for p in ctx["proposals"]}
    assert len(rendered_names) == PROPOSALS_RENDER_CAP

    resp = client.post(reverse("crm:proposals_bulk", args=["dismiss"]))
    assert resp.status_code == 200

    dismissed_names = set(
        ContactProposal.objects.for_user(user).filter(
            status=ContactProposal.STATUS_DISMISSED,
        ).values_list("name", flat=True)
    )
    still_pending_names = set(
        ContactProposal.objects.for_user(user).filter(
            status=ContactProposal.STATUS_PENDING,
        ).values_list("name", flat=True)
    )

    # Exactly what rendered was acted on — no more, no less.
    assert dismissed_names == rendered_names
    assert len(dismissed_names) == PROPOSALS_RENDER_CAP
    # And the overflow the lane never showed is untouched.
    assert still_pending_names == {p.name for p in proposals} - rendered_names
