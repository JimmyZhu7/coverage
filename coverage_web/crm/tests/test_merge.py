"""Duplicate cards (crm/merge.py + the Settings surface): suggest, merge on
a tap, undo exactly, remember every answer.

The invariants under test are the design's own rules:
- nothing merges without a tap (suggestions are offers, computed live);
- the merge is fully recorded in the ledger (`crm.models.ContactMerge`) and
  `undo` reverses exactly what it did — touches back, filled fields back
  only where the merge's value still stands, note line off, archived state
  restored to what it actually was;
- every answer (merged, undone, rejected) suppresses the pair forever;
- two genuinely different people at one firm are never suggested at all.

Plain `django_db` (not transaction=True): the merge moves EXISTING touch
rows through the ORM and never calls `crm.services.log_touch`, so no second
connection is involved. Touch rows are created directly here for the same
reason — what matters is which contact they sit on, not the ratchet.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import merge as merge_service
from crm.models import Contact, ContactMerge, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(email="merge-student@example.com", password="x")


@pytest.fixture
def amazon():
    return Firm.objects.create(slug="amazon", name="Amazon", domains=["amazon.com"])


@pytest.fixture
def ebba_pair(user, amazon):
    """The founder's live rows 707/706, reproduced: one AWS account manager
    tracked as two people."""
    primary = Contact.all_objects.create(
        user=user, name="Ebba af Klercker", email="ebbakler@amazon.com",
        firm=amazon, role="Account Manager, AWS", warmth="replied",
        thread_state="replied",
    )
    duplicate = Contact.all_objects.create(
        user=user, name="Ebba Kler", email="ebbakler@amazon.es",
        firm=amazon, warmth="cold",
    )
    now = timezone.now()
    for kind in ("outreach", "reply_received", "outreach"):
        Touch.all_objects.create(
            user=user, contact=primary, ts=now, kind=kind, channel="email",
        )
    Touch.all_objects.create(
        user=user, contact=duplicate, ts=now, kind="outreach", channel="email",
    )
    return primary, duplicate


def touch_ids(user, contact):
    return set(
        Touch.objects.for_user(user).filter(contact=contact).values_list("id", flat=True)
    )


# --------------------------------------------------------------------------- #
# Suggesting
# --------------------------------------------------------------------------- #

class TestCandidatePairs:
    def test_the_ebba_pair_is_suggested_primary_first(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        pairs = merge_service.candidate_pairs(user)
        assert len(pairs) == 1
        assert pairs[0].primary == primary  # 3 touches beats 1
        assert pairs[0].duplicate == duplicate
        assert pairs[0].evidence

    def test_two_people_at_one_firm_are_never_suggested(self, user, amazon):
        Contact.all_objects.create(
            user=user, name="Warren Zhang", email="warren.zhang@amazon.com",
            firm=amazon,
        )
        Contact.all_objects.create(
            user=user, name="Yuxiang Zhang", email="yuxiang.zhang@amazon.com",
            firm=amazon,
        )
        assert merge_service.candidate_pairs(user) == []

    def test_an_archived_row_still_suggests(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        duplicate.archived = True
        duplicate.save(update_fields=["archived"])
        assert len(merge_service.candidate_pairs(user)) == 1

    def test_two_archived_rows_ask_for_no_decision(self, user, ebba_pair):
        for c in ebba_pair:
            c.archived = True
            c.save(update_fields=["archived"])
        assert merge_service.candidate_pairs(user) == []

    def test_every_answer_suppresses_forever(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        record = merge_service.merge(user, primary, duplicate, "evidence")
        assert merge_service.candidate_pairs(user) == []
        merge_service.undo(record)
        # The undo is the user's word too — never re-asked.
        assert merge_service.candidate_pairs(user) == []

    def test_rejected_suppresses_in_both_orders(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        merge_service.reject(user, duplicate, primary, "evidence")
        assert merge_service.candidate_pairs(user) == []

    def test_other_tenants_rows_never_pair(self, user, amazon, ebba_pair):
        other = User.objects.create_user(email="merge-other@example.com", password="x")
        Contact.all_objects.create(
            user=other, name="Ebba af Klercker", email="ebbakler@amazon.com",
            firm=amazon,
        )
        assert len(merge_service.candidate_pairs(other)) == 0


# --------------------------------------------------------------------------- #
# Merging and undoing
# --------------------------------------------------------------------------- #

class TestMergeAndUndo:
    def test_merge_moves_history_and_archives_the_spare(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        moved = touch_ids(user, duplicate)
        record = merge_service.merge(user, primary, duplicate, "the evidence")

        primary.refresh_from_db()
        duplicate.refresh_from_db()
        assert touch_ids(user, duplicate) == set()
        assert moved <= touch_ids(user, primary)
        assert duplicate.archived is True
        assert "ebbakler@amazon.es" in primary.notes
        assert record.status == ContactMerge.STATUS_MERGED
        assert set(record.moved_touch_ids) == moved
        assert record.duplicate_was_archived is False
        assert record.evidence == "the evidence"

    def test_merge_fills_blanks_only(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        duplicate.role = "Should not overwrite"
        duplicate.region = "other"
        duplicate.save(update_fields=["role", "region"])
        record = merge_service.merge(user, primary, duplicate)
        primary.refresh_from_db()
        # role was already set on the primary — the user's own data wins.
        assert primary.role == "Account Manager, AWS"
        # region was blank — filled, and recorded for the undo.
        assert primary.region == "other"
        assert "region" in record.field_changes
        assert "role" not in record.field_changes

    def test_merge_never_touches_warmth(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        merge_service.merge(user, primary, duplicate)
        primary.refresh_from_db()
        assert primary.warmth == "replied"
        assert primary.thread_state == "replied"

    def test_undo_restores_exactly(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        before_primary = touch_ids(user, primary)
        before_duplicate = touch_ids(user, duplicate)
        notes_before = primary.notes

        record = merge_service.merge(user, primary, duplicate)
        assert merge_service.undo(record) is True

        primary.refresh_from_db()
        duplicate.refresh_from_db()
        assert touch_ids(user, primary) == before_primary
        assert touch_ids(user, duplicate) == before_duplicate
        assert duplicate.archived is False
        assert primary.notes == notes_before
        record.refresh_from_db()
        assert record.status == ContactMerge.STATUS_UNDONE

    def test_undo_respects_a_hand_edit_since(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        duplicate.region = "hk"
        duplicate.save(update_fields=["region"])
        record = merge_service.merge(user, primary, duplicate)
        primary.refresh_from_db()
        assert primary.region == "hk"
        # The user corrects the region by hand after the merge…
        primary.region = "us"
        primary.save(update_fields=["region"])
        merge_service.undo(record)
        primary.refresh_from_db()
        # …and the undo never overwrites their word.
        assert primary.region == "us"

    def test_undo_keeps_a_deliberately_archived_duplicate_archived(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        duplicate.archived = True
        duplicate.save(update_fields=["archived"])
        record = merge_service.merge(user, primary, duplicate)
        merge_service.undo(record)
        duplicate.refresh_from_db()
        # It was archived BEFORE the merge — undo restores that fact.
        assert duplicate.archived is True

    def test_undo_is_idempotent(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        record = merge_service.merge(user, primary, duplicate)
        assert merge_service.undo(record) is True
        assert merge_service.undo(record) is False

    def test_reject_writes_nothing_to_either_contact(self, user, ebba_pair):
        primary, duplicate = ebba_pair
        merge_service.reject(user, primary, duplicate, "ev")
        primary.refresh_from_db()
        duplicate.refresh_from_db()
        assert duplicate.archived is False
        assert touch_ids(user, duplicate)
        assert primary.notes == ""


# --------------------------------------------------------------------------- #
# The Settings taps
# --------------------------------------------------------------------------- #

class TestMergeViews:
    def _login(self, client, user):
        client.force_login(user)

    def test_merge_tap(self, client, user, ebba_pair):
        primary, duplicate = ebba_pair
        self._login(client, user)
        resp = client.post(
            reverse("crm:contact_merge_act", args=["merge"]),
            {"primary": primary.id, "duplicate": duplicate.id},
        )
        assert resp.status_code == 302
        duplicate.refresh_from_db()
        assert duplicate.archived is True
        assert ContactMerge.objects.for_user(user).filter(
            status=ContactMerge.STATUS_MERGED
        ).count() == 1

    def test_reject_tap(self, client, user, ebba_pair):
        primary, duplicate = ebba_pair
        self._login(client, user)
        client.post(
            reverse("crm:contact_merge_act", args=["reject"]),
            {"primary": primary.id, "duplicate": duplicate.id},
        )
        duplicate.refresh_from_db()
        assert duplicate.archived is False
        assert ContactMerge.objects.for_user(user).filter(
            status=ContactMerge.STATUS_REJECTED
        ).count() == 1

    def test_a_stale_or_invented_pair_refuses(self, client, user, amazon):
        # Two people at one firm: never suggested, so the tap must refuse
        # even if the POST names them directly.
        a = Contact.all_objects.create(
            user=user, name="Warren Zhang", email="warren.zhang@amazon.com",
            firm=amazon,
        )
        b = Contact.all_objects.create(
            user=user, name="Yuxiang Zhang", email="yuxiang.zhang@amazon.com",
            firm=amazon,
        )
        self._login(client, user)
        resp = client.post(
            reverse("crm:contact_merge_act", args=["merge"]),
            {"primary": a.id, "duplicate": b.id},
        )
        assert resp.status_code == 302
        assert ContactMerge.objects.for_user(user).count() == 0
        b.refresh_from_db()
        assert b.archived is False

    def test_undo_tap(self, client, user, ebba_pair):
        primary, duplicate = ebba_pair
        record = merge_service.merge(user, primary, duplicate)
        self._login(client, user)
        resp = client.post(reverse("crm:contact_merge_undo", args=[record.id]))
        assert resp.status_code == 302
        duplicate.refresh_from_db()
        assert duplicate.archived is False

    def test_undo_refuses_another_tenants_record(self, client, user, ebba_pair):
        primary, duplicate = ebba_pair
        record = merge_service.merge(user, primary, duplicate)
        other = User.objects.create_user(email="merge-intruder@example.com", password="x")
        client.force_login(other)
        resp = client.post(reverse("crm:contact_merge_undo", args=[record.id]))
        assert resp.status_code == 404

    def test_settings_page_shows_the_suggestion(self, client, user, ebba_pair):
        """"One Person, Two Cards?" was a question that named neither what
        the group holds nor what it's for (2026-08-29 presentation pass on
        Settings' Decisions card) — renamed to "Duplicate Contacts", which
        says what the rows below actually are. The behavior this pins is
        unchanged: the group renders and shows the duplicate's email."""
        self._login(client, user)
        resp = client.get(reverse("accounts:settings"))
        body = resp.content.decode()
        assert "Duplicate Contacts" in body
        assert "ebbakler@amazon.es" in body
