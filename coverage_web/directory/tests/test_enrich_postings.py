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
    PAST_DEADLINE_STALE_HOURS,
    URGENT_STALE_DAYS,
    _queue,
    fetch_posting,
    has_live_api,
    microdata_jobposting_location,
    page_title,
    payload_text,
    plain_text_jobposting_location,
    stated_page_location,
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
        lambda url, **kw: ("Application Deadline: 08/30/2026", "", ""),
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
                           "Markham, Ontario, Canada; Scarborough, Ontario, Canada",
                           ""),
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
        lambda url, **kw: ("no deadline language here", "Somewhere Else", ""),
    )
    call_command("enrich_postings")

    opp.refresh_from_db()
    assert opp.location == "London, United Kingdom"


@pytest.mark.django_db
class TestSitemapTitleAndMicrodataLocationRecovery:
    """Regression tests for the confirmed HSBC title-truncation defect: a
    `sitemap` board (coverage_connectors/sitemap.py) has no title field of
    its own, so ingest reconstructs one from the posting URL's slug — and
    HSBC's own slugs truncate long titles mid-word ("...-Hong" instead of
    "...-Hong-Kong", confirmed live on 8 rows: ids 1615, 1621, 1626-1630,
    1632). HSBC's page states its real title in `og:title` and its location
    as schema.org MICRODATA (`itemprop="jobLocation"` with nested
    `<meta itemprop="addressLocality" ...>` tags) rather than the
    `<script type="application/ld+json">` block `jobposting_jsonld` reads —
    confirmed live: 0 of these pages carry an ld+json JobPosting block.

    Second round: the microdata is truncated the same way the slug is
    (`addressRegion` content="Hong"), so the location fix only moved the
    truncation — the 8 rows read "Central, Hong, HK" until the page's own
    visible "Location:" label was read as well. See LIVE_HSBC_PAGE below."""

    HSBC_PAGE = (
        '<html><head>'
        '<meta property="og:title" content="Investment Banking - Internship">'
        '</head><body>'
        '<span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">'
        '<span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
        '<meta itemprop="addressLocality" content="Central">'
        '<meta itemprop="addressRegion" content="Hong Kong Island">'
        '<meta itemprop="addressCountry" content="HK"></span></span>'
        '</body></html>'
    )

    def test_microdata_location_is_read_when_no_jsonld_block_exists(self):
        assert (microdata_jobposting_location(self.HSBC_PAGE)
                == "Central, Hong Kong Island, HK")

    def test_page_title_reads_og_title(self):
        assert page_title(self.HSBC_PAGE) == "Investment Banking - Internship"

    def test_page_title_is_blank_when_the_page_has_none(self):
        assert page_title("<html><body>no meta tags here</body></html>") == ""

    def test_a_sitemap_boards_truncated_slug_title_is_corrected(self, monkeypatch):
        """The exact live shape: the slug-reconstructed title ends mid-word
        ('...Hong'), and the page's own og:title states the real, short,
        untruncated title."""
        firm = Firm.objects.create(slug="hsbc", name="HSBC")
        opp = Opportunity.objects.create(
            firm=firm, title="Central Investment Banking Internship Hong",
            bucket="entry_level", status="open", source="sitemap",
            url="https://apply.careers.hsbc.com/emergingtalent/job/x/123/",
        )

        class _Resp:
            status_code = 200
            text = self.HSBC_PAGE

        monkeypatch.setattr(enrich_mod.requests, "get", lambda *a, **k: _Resp())
        call_command("enrich_postings")

        opp.refresh_from_db()
        assert opp.title == "Investment Banking - Internship"
        assert opp.location == "Central, Hong Kong Island, HK"

    def test_a_non_sitemap_boards_title_is_never_overwritten(self, monkeypatch):
        """Every other connector already carries a real title from its own
        ingest-time API payload — recovered og:title must never override it,
        even if the fetched page happens to word it differently."""
        firm = Firm.objects.create(slug="acme", name="Acme")
        opp = Opportunity.objects.create(
            firm=firm, title="Analyst Programme", bucket="entry_level",
            status="open", source="greenhouse",
            url="https://acme.example/job/1",
        )

        class _Resp:
            status_code = 200
            text = self.HSBC_PAGE

        monkeypatch.setattr(enrich_mod.requests, "get", lambda *a, **k: _Resp())
        call_command("enrich_postings")

        opp.refresh_from_db()
        assert opp.title == "Analyst Programme"

    # --- the microdata is truncated too -------------------------------------
    #
    # The fixture above states addressRegion="Hong Kong Island" — the value the
    # page OUGHT to carry. The live page states content="Hong", the identical
    # mid-word cut the URL slug makes, so reading the microdata replaced a
    # truncated title with a truncated location ("Central, Hong, HK") on all 8
    # rows. The page's own visible "Location:" label states it in full.
    LIVE_HSBC_PAGE = (
        '<html><head>'
        '<meta property="og:title" content="Investment Banking - Internship">'
        '</head><body>'
        '<div class="row"><div class="col-xs-12">'
        '<span class="joblayouttoken-label">Location:&nbsp;\n        </span>\n'
        '<span data-careersite-propertyid="location">'
        '<p id="job-location" class="jobLocation job-location-inline">'
        '<span class="jobGeoLocation">Central, Hong Kong Island, HK\n'
        '</span></p>'
        '<style type="text/css">#job-location { display: inline; }</style>'
        '</span></div></div>'
        '<div><span class="joblayouttoken-label">Programme Type:&nbsp;</span>'
        '<p>Internship</p></div>'
        '<span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">'
        '<span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
        '<meta itemprop="addressLocality" content="Central">'
        '<meta itemprop="addressRegion" content="Hong">'
        '<meta itemprop="addressCountry" content="HK"></span></span>'
        '</body></html>'
    )

    def test_the_live_pages_microdata_region_is_itself_truncated(self):
        """The defect this class's second round exists for, stated plainly."""
        assert (microdata_jobposting_location(self.LIVE_HSBC_PAGE)
                == "Central, Hong, HK")

    def test_visible_location_label_is_read_in_full(self):
        """Bounded by the block that holds it, so it stops at "…HK" and does
        not run on into the next label ("Programme Type: Internship")."""
        assert (plain_text_jobposting_location(self.LIVE_HSBC_PAGE)
                == "Central, Hong Kong Island, HK")

    def test_the_exact_live_shape_resolves_to_the_fuller_string(self):
        """Truncated microdata + full visible label -> the full one wins."""
        assert (stated_page_location(self.LIVE_HSBC_PAGE)
                == "Central, Hong Kong Island, HK")

    def test_a_label_free_page_still_falls_back_to_microdata(self):
        """The original fix must not regress: microdata is the only location
        some boards state at all."""
        assert plain_text_jobposting_location(self.HSBC_PAGE) == ""
        assert (stated_page_location(self.HSBC_PAGE)
                == "Central, Hong Kong Island, HK")

    def test_a_label_that_contradicts_the_microdata_does_not_win(self):
        """Only a strictly FULLER label supersedes the structured field — a
        label naming a different place is the page disagreeing with itself,
        and the structured field keeps the row."""
        page = (
            '<div>Location: London, England, GB</div>'
            '<meta itemprop="addressLocality" content="Singapore">'
            '<meta itemprop="addressCountry" content="SG">'
        )
        assert stated_page_location(page) == "Singapore, SG"

    def test_a_location_colon_inside_prose_is_not_mistaken_for_a_place(self):
        """The label reader is bounded by length as well as by markup, so a
        sentence that happens to open with "Location:" is discarded rather
        than written into the location column."""
        page = (
            "<p>Location: This role sits within our global banking division "
            "and the successful candidate will be expected to travel between "
            "several of our offices during the programme.</p>"
        )
        assert plain_text_jobposting_location(page) == ""
        assert stated_page_location(page) == ""

    def test_the_live_shape_end_to_end_stores_the_fuller_location(self, monkeypatch):
        """Same command run as the title test above, against the page as it
        actually reads live — `location` and `raw["detail_location"]` must
        both land on the full string, not "Central, Hong, HK"."""
        firm = Firm.objects.create(slug="hsbc", name="HSBC")
        opp = Opportunity.objects.create(
            firm=firm, title="Central Investment Banking Internship Hong",
            bucket="entry_level", status="open", source="sitemap",
            url="https://apply.careers.hsbc.com/emergingtalent/job/x/123/",
        )

        class _Resp:
            status_code = 200
            text = self.LIVE_HSBC_PAGE

        monkeypatch.setattr(enrich_mod.requests, "get", lambda *a, **k: _Resp())
        call_command("enrich_postings")

        opp.refresh_from_db()
        assert opp.location == "Central, Hong Kong Island, HK"
        assert opp.raw["detail_location"] == "Central, Hong Kong Island, HK"
        assert opp.title == "Investment Banking - Internship"


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
        text, location, title = fetch_posting(self.URL)

        assert text == "About the program"
        assert location == "Hong Kong, Hong Kong SAR"
        assert title == ""
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
        text, location, title = fetch_posting("https://higher.gs.com/search")
        assert text is None
        assert location == ""
        assert title == ""

    def test_graphql_error_status_reads_as_unreachable_not_answered(self, monkeypatch):
        """A non-200 must come back as `(None, "", "")` — retried next run —
        not as an empty-but-answered page."""
        monkeypatch.setattr(
            enrich_mod.requests, "post",
            lambda *a, **k: _FakeResponse(500, {}))
        assert fetch_posting(self.URL) == (None, "", "")

    def test_empty_description_reads_as_unreachable_not_answered(self, monkeypatch):
        """A 200 whose `role` is null (a GraphQL response can do this without
        raising, per goldmansachs.py's own `verify()` note) must not be
        recorded as a page that genuinely said nothing."""
        monkeypatch.setattr(
            enrich_mod.requests, "post",
            lambda *a, **k: _FakeResponse(200, {"data": {"role": None}}))
        assert fetch_posting(self.URL) == (None, "", "")


class TestOracleDetailFetch:
    """Regression test for the confirmed Oracle coverage gap: a plain GET of
    a jpmc.fa.oraclecloud.com job URL is a JS shell whose only text node is
    the <title> "JPMC Candidate Experience page" (43 open rows carry that
    literal string as detail_text; another 79 have never been fetched at
    all). The public, unauthenticated `recruitingCEJobRequisitions` search
    endpoint oracle.py's own fetch()/verify() already call answers a
    re-query by requisition id (keyword=<id>) with real posting text —
    confirmed live for requisition 210765547 to be a 163-char
    ShortDescriptionStr with empty Responsibilities/Qualifications, real
    coverage even though short of a full posting body."""

    URL = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210765547"

    def _payload(self, short="We're looking for talented individuals...",
                 responsibilities="", qualifications=""):
        req = {"Id": "210765547", "PrimaryLocation": "New York, New York"}
        if short:
            req["ShortDescriptionStr"] = short
        if responsibilities:
            req["ExternalResponsibilitiesStr"] = responsibilities
        if qualifications:
            req["ExternalQualificationsStr"] = qualifications
        return {"items": [{"requisitionList": [req]}]}

    def test_short_description_is_read_through_the_search_endpoint(self, monkeypatch):
        captured = {}

        def fake_get(url, *, headers, timeout):
            captured["url"] = url
            return _FakeResponse(200, self._payload())

        monkeypatch.setattr(enrich_mod.requests, "get", fake_get)
        text, location, title = fetch_posting(self.URL)

        assert text == "We're looking for talented individuals..."
        assert location == "New York, New York"
        assert title == ""
        assert "keyword=210765547" in captured["url"]
        assert "siteNumber=CX_1001" in captured["url"]

    def test_has_live_api_recognizes_oracle_urls(self):
        assert has_live_api(self.URL, greenhouse_token=None) is True

    def test_bare_site_url_without_job_id_falls_through_to_generic_get(self, monkeypatch):
        """A URL that merely shares the oraclecloud.com host but has no
        `/job/<id>` path (a bare site listing) must not hit the
        recruitingCEJobRequisitions search branch — it must fall through to
        the generic plain-GET branch, requesting the URL as given rather
        than a keyword-search endpoint."""
        captured = {}

        class _PlainResponse:
            status_code = 200
            text = "<html>some page chrome</html>"

        def fake_get(url, *, headers, timeout):
            captured["url"] = url
            return _PlainResponse()

        monkeypatch.setattr(enrich_mod.requests, "get", fake_get)
        bare_url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"
        fetch_posting(bare_url)
        assert captured["url"] == bare_url

    def test_requisition_not_found_reads_as_unreachable_not_answered(self, monkeypatch):
        """A 200 whose requisitionList doesn't contain this Id (the search
        under-matched, or the id has since rolled off) must not be recorded
        as a page that genuinely said nothing."""
        monkeypatch.setattr(
            enrich_mod.requests, "get",
            lambda *a, **k: _FakeResponse(200, {"items": [{"requisitionList": []}]}))
        assert fetch_posting(self.URL) == (None, "", "")

    def test_both_description_fields_empty_reads_as_unreachable_not_answered(self, monkeypatch):
        """Matches the live-confirmed pattern for 2 of the 3 sampled JPM ids:
        a match is found but ShortDescriptionStr is also empty/absent — must
        not be recorded as answered with a blank string."""
        req = {"Id": "210765547", "PrimaryLocation": ""}
        monkeypatch.setattr(
            enrich_mod.requests, "get",
            lambda *a, **k: _FakeResponse(200, {"items": [{"requisitionList": [req]}]}))
        assert fetch_posting(self.URL) == (None, "", "")


@pytest.mark.django_db
class TestPastDeadlineGetsTighterRecheckThanClosingSoon:
    """Round 4 regression: HSBC Sheffield WIT (id 1618) was re-fetched today
    and STILL read a past deadline (Aug 9) because HSBC's own page moved to
    Aug 16 minutes after Coverage's morning scrape. The old code reused
    URGENT_STALE_DAYS (7 days) for an already-past deadline, the same
    threshold as merely "closing soon" — meaning a row caught showing an
    already-expired date could sit unconfirmed for up to a week. Round 4
    tightened that to one calendar day, but a whole-day gate (measured via
    `(now - at).days`) was STILL loose enough to let the same row survive an
    entire scheduled refresh pass: render.yaml's `coverage-scrape` cron runs
    this command every 6 hours, yet id 1618 — fetched 01:07 UTC, read as
    already past — was still unrefreshed at 12:58 UTC, after the 06:00
    refresh had already run, because `age.days` was still 0. This must
    requeue at the tighter PAST_DEADLINE_STALE_HOURS (6 hours), matching the
    cron's own cadence."""

    def test_past_deadline_requalifies_after_six_hours_not_one_day(self):
        today = timezone.localdate()
        six_hours_ago = (timezone.now()
                         - timedelta(hours=PAST_DEADLINE_STALE_HOURS)).isoformat()
        row = _Row(1, deadline=today - timedelta(days=5), detail_fetched=six_hours_ago)

        todo, refreshed = _queue([row], refetch=False, stale_days=21, today=today)

        assert todo == [row]
        assert refreshed == 1

    def test_past_deadline_stale_since_this_mornings_refresh_pass_is_requeued(self):
        """The exact HSBC Sheffield WIT shape: fetched 12.5h ago (this
        morning's refresh pass), already reading past. A whole-day gate
        (`age.days == 0`) would leave this sitting through the NEXT
        scheduled refresh pass too; the hour-based gate must catch it now."""
        today = timezone.localdate()
        this_mornings_fetch = (timezone.now() - timedelta(hours=12, minutes=30)).isoformat()
        row = _Row(1, deadline=today - timedelta(days=5),
                  detail_fetched=this_mornings_fetch)

        todo, refreshed = _queue([row], refetch=False, stale_days=21, today=today)

        assert todo == [row]
        assert refreshed == 1

    def test_past_deadline_fetched_just_now_is_not_yet_requeued(self):
        """The one gap this threshold cannot close: a row fetched THIS
        INSTANT that already reads past will not be looked at again until
        age >= PAST_DEADLINE_STALE_HOURS — no periodic-refresh pipeline can
        make this fetch see a site edit that hasn't happened yet. This pins
        that known limit so it can't silently regress to "never" again."""
        today = timezone.localdate()
        just_now = timezone.now().isoformat()
        row = _Row(1, deadline=today - timedelta(days=5), detail_fetched=just_now)

        todo, refreshed = _queue([row], refetch=False, stale_days=21, today=today)

        assert todo == []
        assert refreshed == 0

    def test_past_deadline_threshold_is_tighter_than_closing_soon(self):
        """A row stale exactly PAST_DEADLINE_STALE_HOURS with an ALREADY-PAST
        deadline must requeue; a row stale the same short duration but only
        "closing soon" (not yet past) must NOT requeue until it hits the
        much wider URGENT_STALE_DAYS -- proving the two shapes use different
        thresholds rather than one loosened for both."""
        today = timezone.localdate()
        stale = (timezone.now() - timedelta(hours=PAST_DEADLINE_STALE_HOURS)).isoformat()
        past = _Row(1, deadline=today - timedelta(days=1), detail_fetched=stale)
        soon = _Row(2, deadline=today + timedelta(days=10), detail_fetched=stale)

        todo, _ = _queue([past, soon], refetch=False, stale_days=21, today=today)

        assert [o.id for o in todo] == [1]


@pytest.mark.django_db
class TestLiveApiPrecedenceOverStalePayload:
    """Round 4 regression: BMO id=19224's Phenom list payload carries a
    descriptionTeaser snippet frozen near the posting's original
    dateCreated (2026-03-23, stating "04/19/2026"), while the SAME URL
    resolves into a live Workday tenant stating "08/30/2026" through the
    wday/cxs API this command already knows how to read. Because
    `payload_text()` used to be asked unconditionally before
    `fetch_posting()`, the live Workday answer was never reached for any of
    35/35 open Phenom-source rows carrying a deadline. `has_live_api()` and
    the reordered fetch in `handle()` fix this by asking the live API FIRST
    whenever one exists, falling back to the payload only when the live
    fetch fails outright."""

    STALE_PAYLOAD_TEXT = (
        "Application Deadline . 04/19/2026 . This is a great opportunity to "
        "join our team and build your career in banking technology while "
        "working with a talented group of engineers on real customer "
        "problems every single day of the internship program this summer"
    )
    WORKDAY_URL = ("https://bmo.wd3.myworkdayjobs.com/External/job/Toronto-ON-CAN/"
                   "Cloud-Application-Developer_R260005483/apply")

    def _stale_raw(self):
        # Mirrors Phenom's own nested shape (descriptionTeaser plus the
        # ml_job_parser sub-fields) closely enough to trip payload_text()'s
        # own prose-detection walk exactly as the live BMO payload does.
        raw = {
            "descriptionTeaser": self.STALE_PAYLOAD_TEXT,
            "ml_job_parser": {"descriptionTeaser_ats": self.STALE_PAYLOAD_TEXT},
        }
        assert payload_text(raw) is not None, "fixture must actually trip payload_text()"
        return raw

    def test_has_live_api_recognizes_workday_urls(self):
        assert has_live_api(self.WORKDAY_URL, greenhouse_token=None) is True
        assert has_live_api("https://careers.mckinsey.com/job/123",
                            greenhouse_token=None) is False

    def test_workday_url_prefers_live_fetch_over_stale_payload(self, monkeypatch):
        """The core fix: even though the stored payload answers (and would
        yield the stale 04/19/2026 date), a Workday-resolvable URL must
        still hit the live API and store ITS answer."""
        firm = Firm.objects.create(slug="bmo", name="BMO")
        opp = Opportunity.objects.create(
            firm=firm, title="Cloud Application Developer", bucket="other",
            status="open", url=self.WORKDAY_URL,
            deadline=timezone.localdate() + timedelta(days=1),
            deadline_precision="day", confidence=0.6,
            raw=self._stale_raw(),
        )
        live_payload = {"jobPostingInfo": {
            "jobDescription": "Application Deadline: 08/30/2026 Address: Toronto",
            "location": "Toronto, ON", "additionalLocations": [],
            "country": {"descriptor": "Canada"},
        }}
        monkeypatch.setattr(
            enrich_mod.requests, "get",
            lambda *a, **k: _FakeResponse(200, live_payload))
        call_command("enrich_postings", refetch=True)

        opp.refresh_from_db()
        assert opp.deadline.isoformat() == "2026-08-30"
        assert opp.raw["detail_source"] == "fetch"

    def test_blocked_board_still_falls_back_to_payload(self, monkeypatch):
        """A URL with no live API (the McKinsey shape: a plain GET that
        would fail) must still be answered from the payload, same as
        before this fix — this only reorders precedence for URLs that
        genuinely have a live API to prefer."""
        firm = Firm.objects.create(slug="mckinsey", name="McKinsey")
        opp = Opportunity.objects.create(
            firm=firm, title="Business Analyst", bucket="entry_level",
            status="open", url="https://careers.mckinsey.com/job/123",
            deadline=timezone.localdate() + timedelta(days=1),
            deadline_precision="day", confidence=0.6,
            raw=self._stale_raw(),
        )

        def fail_get(*a, **k):
            raise enrich_mod.requests.RequestException("connection reset")
        monkeypatch.setattr(enrich_mod.requests, "get", fail_get)
        call_command("enrich_postings", refetch=True)

        opp.refresh_from_db()
        assert opp.deadline.isoformat() == "2026-04-19"
        assert opp.raw["detail_source"] == "payload"


@pytest.mark.django_db
class TestSelfWrittenDetailTextNeverShortCircuitsTheLiveFetch:
    """Round 7 regression: for any board with `has_live_api()==False`
    (icims, lever, talentgateway, beisen, ...), the FIRST enrichment pass
    writes its fetched text into `raw["detail_text"]`. On every later run,
    `payload_text(o.raw)` walked that exact same cached string, found it
    prose-length, and returned it as if it were the board's OWN list
    payload — permanently short-circuiting `fetch_posting()` before
    `location` is ever computed. Confirmed live: 0 of 75 open icims rows
    that already carried `detail_text` had ever recovered a
    `detail_location`, including rows fetched within the prior 24-48h
    (SIG id=19383). `detail_fetched` kept advancing every run while
    `location` stayed permanently blank."""

    ICIMS_URL = "https://sig-icims.icims.com/jobs/11191/discovery-program/job"

    ICIMS_PAGE_WITH_LOCATION = (
        '<html><head>'
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "description": "<p>Grow with our equity '
        'research team on real trading desks, mentored by senior '
        'analysts every single day of this rotational internship '
        'program.</p>", '
        '"jobLocation": {"address": {"addressLocality": "Bala Cynwyd", '
        '"addressRegion": "PA", "addressCountry": "US"}}}'
        '</script></head><body></body></html>'
    )

    def _raw_with_own_prior_detail_text(self):
        # Mirrors exactly what `handle()` itself writes at the bottom of a
        # prior pass: no board-native prose field, only this command's own
        # bookkeeping keys. Long enough that, before this fix, it would have
        # tripped the old `payload_text()` walk (>= _PAYLOAD_MIN after HTML
        # stripping) and been mistaken for a fresh board payload.
        prose = ("Grow with our equity research team on real trading desks, "
                 "mentored by senior analysts every single day of this "
                 "rotational internship program designed to build the next "
                 "generation of investment professionals across every desk "
                 "in the firm, from macro trading to systematic strategies "
                 "and everything in between this summer.")
        return {
            "detail_text": prose,
            "detail_fetched": (timezone.now() - timedelta(days=30)).isoformat(),
            "detail_source": "fetch",
        }

    def test_own_prior_detail_text_is_not_read_back_as_a_board_payload(self):
        """Direct unit check on `payload_text()`: the command's own
        bookkeeping keys must never be mistaken for the board's payload,
        regardless of how long the cached string is."""
        assert payload_text(self._raw_with_own_prior_detail_text()) is None

    def test_prior_own_detail_text_no_longer_blocks_the_live_fetch(self, monkeypatch):
        firm = Firm.objects.create(slug="sig", name="Susquehanna")
        opp = Opportunity.objects.create(
            firm=firm, title="Discovery Program: Growth Equity",
            bucket="entry_level", status="open", source="icims",
            url=self.ICIMS_URL, location="",
            raw=self._raw_with_own_prior_detail_text(),
        )
        assert has_live_api(self.ICIMS_URL, greenhouse_token=None) is False

        class _Resp:
            status_code = 200
            text = self.ICIMS_PAGE_WITH_LOCATION

        monkeypatch.setattr(enrich_mod.requests, "get", lambda *a, **k: _Resp())
        call_command("enrich_postings", refetch=True)

        opp.refresh_from_db()
        # Before the fix: payload_text(o.raw) returned the cached prose,
        # fetch_posting() was never called, and location stayed "".
        assert opp.location == "Bala Cynwyd, PA, US"
        assert opp.raw["detail_source"] == "fetch"
