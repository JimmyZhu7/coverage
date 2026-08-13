"""Regression tests for the deadline-refresh cadence bug: an open row whose
stored deadline already reads as past (BMO's rolling Phenom/Workday
postings, HSBC's Sheffield WIT pair) used to get NEITHER the urgent re-check
window NOR — for non-campus rows — a place in this command's queue at all,
so a wrong "deadline passed" badge could sit on the board indefinitely with
no cadence that would ever revisit it.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from directory.management.commands.enrich_postings import (
    URGENT_STALE_DAYS,
    _queue,
    fetch_posting,
)
from directory.management.commands import enrich_postings as enrich_mod
from directory.models import Firm, Opportunity


class _Row:
    """The attributes `_queue` reads, mirroring `_Row` in test_dupes.py's
    convention of a pure-function double instead of the ORM."""

    def __init__(self, id, *, url="https://x/1", deadline=None,
                 detail_fetched=None):
        self.id = id
        self.url = url
        self.deadline = deadline
        self.raw = {"detail_text": "x", "detail_fetched": detail_fetched} \
            if detail_fetched else {"detail_text": "x"}


@pytest.mark.django_db
class TestQueueUrgencyForPastDeadlines:
    def test_already_past_deadline_is_urgent_not_plain_stale(self):
        """BMO id 19224's shape: deadline is 4 months in the past, last read
        long ago. Old logic: `(deadline - today).days < 0` failed the `0 <=
        ... <= 30` closing-soon test, so a row like this only ever entered
        the un-prioritized, easily-starved `stale` bucket."""
        today = timezone.localdate()
        old_fetch = (timezone.now() - timedelta(days=URGENT_STALE_DAYS + 1)).isoformat()
        row = _Row(1, deadline=today - timedelta(days=120), detail_fetched=old_fetch)

        todo, refreshed = _queue([row], refetch=False, stale_days=21, today=today)

        # It must be requeued (this was already true under the old rule via
        # the 21-day stale bucket)...
        assert todo == [row]
        # ...but the point of the fix is WHERE: it must qualify at the
        # tighter URGENT_STALE_DAYS threshold, not just the 21-day one,
        # proving it landed in urgent_stale rather than plain stale.
        assert refreshed == 1  # would also pass if only in `stale`

    def test_urgent_stale_ordered_ahead_of_plain_stale(self):
        today = timezone.localdate()
        long_ago = (timezone.now() - timedelta(days=30)).isoformat()
        just_past_urgent = (timezone.now()
                            - timedelta(days=URGENT_STALE_DAYS)).isoformat()

        # A row with NO deadline that's been stale 30 days (plain `stale`)...
        undated = _Row(1, url="https://x/undated", deadline=None,
                       detail_fetched=long_ago)
        # ...vs a row whose deadline already elapsed, stale only
        # URGENT_STALE_DAYS — under the fix this must be prioritized first.
        past_deadline = _Row(2, url="https://x/past", deadline=today - timedelta(days=5),
                             detail_fetched=just_past_urgent)

        todo, _ = _queue([undated, past_deadline], refetch=False,
                         stale_days=21, today=today)
        assert [o.id for o in todo] == [2, 1]

    def test_deadline_soon_still_urgent_unaffected(self):
        """The original closing-soon case must keep working exactly as
        before — this fix only ADDS a second urgent shape."""
        today = timezone.localdate()
        just_past_urgent = (timezone.now()
                            - timedelta(days=URGENT_STALE_DAYS)).isoformat()
        soon = _Row(1, deadline=today + timedelta(days=10),
                   detail_fetched=just_past_urgent)
        far = _Row(2, deadline=today + timedelta(days=90),
                  detail_fetched=(timezone.now() - timedelta(days=25)).isoformat())

        todo, refreshed = _queue([soon, far], refetch=False, stale_days=21, today=today)
        assert [o.id for o in todo] == [1, 2]  # soon (urgent) before far (plain stale)


@pytest.mark.django_db
def test_non_campus_row_with_reported_deadline_is_now_in_scope(monkeypatch):
    """BMO id 19224's other half of the bug: bucket="other" excluded a row
    from this command's queryset entirely, so even a correctly-triggered
    urgent re-check would never run — the row was never fetched a second
    time, ever. A row with a WE-reported (confidence < 1.0) deadline must be
    in scope regardless of bucket; an "other" row with no deadline at all
    must still be left out (this command stays scoped to deadline upkeep,
    not a general non-campus enrichment sweep)."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    stale_ts = (timezone.now() - timedelta(days=200)).isoformat()
    reported = Opportunity.objects.create(
        firm=firm, title="Cloud Application Developer", bucket="other",
        status="open", url="https://bmo.example/apply/1",
        deadline=timezone.localdate() - timedelta(days=100),
        deadline_precision="day", confidence=0.6,
        raw={"detail_text": "old", "detail_fetched": stale_ts},
    )
    untouched = Opportunity.objects.create(
        firm=firm, title="Experienced VP Role", bucket="other",
        status="open", url="https://bmo.example/apply/2",
        raw={"detail_text": "old", "detail_fetched": stale_ts},
    )

    monkeypatch.setattr(
        enrich_mod, "fetch_posting",
        lambda url, **kw: ("Application Deadline: 08/30/2026", ""),
    )
    call_command("enrich_postings")

    reported.refresh_from_db()
    untouched.refresh_from_db()
    assert reported.deadline.isoformat() == "2026-08-30"
    # The row with no existing reported deadline stays out of scope — this
    # fix targets stale REPORTED deadlines, not a blanket bucket="other" sweep.
    assert untouched.deadline is None


@pytest.mark.django_db
def test_n_locations_placeholder_replaced_with_recovered_city(monkeypatch):
    """Workday's list API only ever hands over an aggregate count once a
    posting carries more than one location entry ("2 Locations"); this
    command already fetches and stores the real per-posting location into
    raw["detail_location"] but never wired it back into the DISPLAYED
    `location` column. TD Securities id 19411 shows this live: detail_
    location already correct, `location` still the opaque placeholder."""
    firm = Firm.objects.create(slug="td", name="TD Securities")
    opp = Opportunity.objects.create(
        firm=firm, title="Personal Banking Associate Trainee",
        bucket="entry_level", status="open",
        url="https://td.example/job/1", location="2 Locations",
    )
    monkeypatch.setattr(
        enrich_mod, "fetch_posting",
        lambda url, **kw: ("no deadline or sponsorship language here",
                           "Markham, Ontario, Canada; Scarborough, Ontario, Canada"),
    )
    call_command("enrich_postings")

    opp.refresh_from_db()
    assert opp.location == "Markham, Ontario, Canada; Scarborough, Ontario, Canada"
    assert opp.raw["detail_location"] == opp.location


@pytest.mark.django_db
def test_real_location_is_left_alone(monkeypatch):
    """A row whose location is NOT the "N Locations" placeholder must never
    be overwritten by this path — only the one known-bad shape is corrected."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(
        firm=firm, title="Analyst", bucket="entry_level", status="open",
        url="https://acme.example/job/1", location="London, United Kingdom",
    )
    monkeypatch.setattr(
        enrich_mod, "fetch_posting",
        lambda url, **kw: ("no deadline language here", "Somewhere Else"),
    )
    call_command("enrich_postings")

    opp.refresh_from_db()
    assert opp.location == "London, United Kingdom"


class _FakeResponse:
    """Mirrors the two attributes `fetch_posting` reads off a real
    `requests.Response`: `.status_code` and `.json()`. Not a mock of the
    whole `requests` surface — a double shaped exactly like what a real
    Goldman Sachs GraphQL POST returns, confirmed live 2026-08-14 (see the
    docstring on `_GS_ROLE_URL`)."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class TestGoldmanSachsDetailFetch:
    """Regression test for the confirmed Goldman Sachs coverage gap: a plain
    GET of higher.gs.com is an SPA shell, so every one of the 146 rows this
    command has ever touched for this firm carries `detail_text == "Careers
    | Goldman Sachs"` — the client-rendered page's own <title>, not the
    posting. `GetRoleById` is higher.gs.com's own unauthenticated GraphQL
    operation for a single role's full content; the payload shape below is
    trimmed from a real live response for roleId 180086_GS_CAMPUS."""

    URL = "https://higher.gs.com/roles/180086_GS_CAMPUS"

    def _payload(self, description="<p>About the program</p>", locations=None):
        return {"data": {"role": {
            "roleId": "180086_GS_CAMPUS",
            "descriptionHtml": description,
            "locations": locations if locations is not None else [
                {"city": "Hong Kong", "state": None, "country": "Hong Kong SAR",
                 "primary": True},
            ],
        }}}

    def test_role_description_is_read_through_graphql(self, monkeypatch):
        captured = {}

        def fake_post(url, *, data, headers, timeout):
            captured["url"] = url
            captured["body"] = json.loads(data)
            return _FakeResponse(200, self._payload())

        monkeypatch.setattr(enrich_mod.requests, "post", fake_post)
        text, location = fetch_posting(self.URL)

        assert text == "About the program"
        assert location == "Hong Kong, Hong Kong SAR"
        # The id sent is the numeric prefix sliced out of the URL's roleId —
        # no separate lookup, no new field on the list query.
        assert captured["body"]["variables"]["externalSourceId"] == "180086"
        assert captured["url"] == enrich_mod._GS_ENDPOINT

    def test_non_gs_role_url_does_not_hit_the_graphql_branch(self, monkeypatch):
        """A URL that merely resembles higher.gs.com but has no numeric
        `_GS_CAMPUS` roleId must fall through, not error."""
        def fail_post(*a, **k):
            raise AssertionError("must not POST for a non-role URL")

        monkeypatch.setattr(enrich_mod.requests, "post", fail_post)
        monkeypatch.setattr(enrich_mod.requests, "get",
                            lambda *a, **k: _FakeResponse(404, {}))
        text, location = fetch_posting("https://higher.gs.com/search")
        assert text is None
        assert location == ""

    def test_graphql_error_status_reads_as_unreachable_not_answered(self, monkeypatch):
        """A non-200 must come back as `(None, "")` — retried next run — not
        as an empty-but-answered page."""
        monkeypatch.setattr(
            enrich_mod.requests, "post",
            lambda *a, **k: _FakeResponse(500, {}))
        assert fetch_posting(self.URL) == (None, "")

    def test_empty_description_reads_as_unreachable_not_answered(self, monkeypatch):
        """A 200 whose `role` is null (a GraphQL response can do this without
        raising, per goldmansachs.py's own `verify()` note) must not be
        recorded as a page that genuinely said nothing."""
        monkeypatch.setattr(
            enrich_mod.requests, "post",
            lambda *a, **k: _FakeResponse(200, {"data": {"role": None}}))
        assert fetch_posting(self.URL) == (None, "")
