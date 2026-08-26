"""Shared zone (docs/build-plan.md §2): no `user_id` anywhere in this
file, by design — these tables are written only by the central scrape
worker/verification layer and read by every user via a `user_firms` join
(the "read-time query, not a fetch" design in §2's "The shared-cache
design, concretely"). Plain `models.Model` / default manager throughout;
`coverage_web.tenancy.PrivateModel` is deliberately not used here.
"""

from __future__ import annotations

from datetime import date as _date, timedelta

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone


class Firm(models.Model):
    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=255)
    # One list, two readers, and the difference between them is load-bearing.
    # A MAIL domain is one the firm's PEOPLE send from (`gs.com`); a
    # CAREER-SITE domain is where its postings live (`careers.bcg.com`,
    # `jobs.rbc.com`, `blackstone.wd1.myworkdayjobs.com`). Board connectors
    # only ever knew the second kind, so for a long time this column held
    # almost nothing but career sites — and `capture.discovery.FirmDomains`,
    # which matches a human's From: address against this list, refused real
    # bankers because of it. Both kinds live here together on purpose (logo
    # lookup and board resolution want the career hosts), so nothing may
    # REPLACE this list to fix one reader: append, and see
    # `directory/_mail_domains.py` + `manage.py seed_mail_domains`.
    domains = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        help_text=(
            "Domains associated with this firm — both the MAIL domains its "
            "people send from (e.g. 'gs.com', used to match an email address "
            "to the firm) and the CAREER-SITE hosts its postings live on "
            "(e.g. 'careers.bcg.com'). Nobody sends mail from a career site: "
            "adding one here will not make an address resolve."
        ),
    )
    regions = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    tracks = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    # The firm's own mark, fetched ONCE by `fetch_firm_logos` and served from
    # our own media — never hotlinked. A hotlinked logo would tell a third
    # party, on every page load, which firms this student is researching;
    # that is exactly the kind of leak the rest of this product refuses to
    # make. Blank is a first-class state: the board falls back to the
    # monogram, which is why nothing here can ever be missing-image-icon.
    logo = models.ImageField(upload_to="firm-logos/", blank=True, null=True)
    # Plan lists this column with no explicit type (unlike the text[]
    # columns above) — kept as a flexible JSON blob rather than a single
    # boolean since sponsorship status is realistically per-region/track
    # (e.g. a firm may sponsor HK visas but not US ones). See report for
    # the full reasoning.
    sponsors = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, blank=True, default="active")

    class Meta:
        db_table = "firms"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Opportunity(models.Model):
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE, related_name="opportunities")
    title = models.CharField(max_length=255)
    bucket = models.CharField(max_length=64, blank=True, default="")
    region = models.CharField(max_length=64, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    # The PROGRAMME/intake year the posting runs in ("2027 Summer Internship"
    # -> "2027"), derived from the title by classify.extract_cohort. Says
    # nothing about who is eligible — see `class_year` below.
    cohort = models.CharField(max_length=32, blank=True, default="")
    # The GRADUATION year the posting states out loud ("Class of 2028"), and
    # only that. Deliberately a separate column from `cohort` rather than a
    # reinterpretation of it: on live data cohort is a programme year on
    # essentially every row that has one, so treating it as a class year would
    # mislabel ~99% of the set. Blank means "the posting didn't say", which is
    # the common and honest case (~3 rows in 4,000 state one). Never derived —
    # see classify.extract_class_year.
    class_year = models.CharField(max_length=32, blank=True, default="")
    # The graduation year the programme's SHAPE implies, where the convention
    # has exactly one answer (a summer N internship graduates N+1; a graduate
    # programme starting N hires that year's finishing class). A separate
    # column from `class_year` on purpose, and the separation is load-bearing:
    # `class_year` means "the posting said so" and is trusted as such, this
    # means "Coverage worked it out". Rendered with its reasoning attached and
    # never permitted to produce a BLOCKING eligibility verdict — an inference
    # may surface a role, it may not hide one. See classify.derive_class_year.
    class_year_derived = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")
    deadline = models.DateField(null=True, blank=True)
    deadline_precision = models.CharField(max_length=32, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    # Tri-state (sponsors / does not sponsor / unknown), not a plain
    # boolean — §6's honesty theme ("labeled honestly ... because v1 has
    # no outcome data") applies here too: "unknown" is a legitimate,
    # common answer this early, and collapsing it into False would be a
    # silent lie.
    sponsorship = models.CharField(max_length=32, blank=True, default="unknown")
    url = models.URLField(max_length=1024)
    source = models.CharField(max_length=64, blank=True, default="")
    first_seen = models.DateTimeField(auto_now_add=True)
    last_verified = models.DateTimeField(null=True, blank=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    # When a DETAIL-level check last happened for this row — the kind that
    # can actually see a fresh deadline (`reverify`'s provider verify() call).
    # Deliberately separate from `last_checked`, which the routine `scrape`
    # list-refetch also bumps on every successful pass even for providers
    # whose list endpoint carries no deadline field at all (Workday's
    # `fetch()` docstring says so explicitly). Without the split, a firm
    # scraped on a tighter cadence than reverify's `--max-age-days` cutoff
    # never goes `last_checked`-stale, so reverify (which walks rows ordered
    # by staleness) never selects it as a candidate even though its
    # `deadline` was frozen at first ingest and never revisited — the
    # posting can sit weeks past its real deadline while `last_verified`
    # reads as today. NULL means "never deep-checked" and is treated as the
    # oldest possible value so brand-new and never-reverified rows surface
    # first.
    deadline_checked_at = models.DateTimeField(null=True, blank=True)
    # When this posting was last observed to CLOSE (open -> closed), cleared
    # again on reopen, so `status == "closed"` iff this is set. The scraper
    # had been flipping rows closed daily and recording nothing — every cycle
    # of "when do this firm's postings actually die" was measured and then
    # thrown away, which is precisely the timing evidence FirmDate estimates
    # need next cycle. Unrecoverable retroactively; captured from now on.
    closed_at = models.DateTimeField(null=True, blank=True)
    # The provider's own raw JSON for this posting, verbatim. Ingest used to
    # drop it ("no destination column"), and that one decision is why
    # sponsorship reads unknown on every scraped row and Workday multi-city
    # roles say "3 Locations": the real data was in the payload and nowhere
    # else. Stored as evidence — extraction stays in classify/ingest where it
    # is testable and re-runnable over rows fetched long ago.
    raw = models.JSONField(default=dict, blank=True)
    # The provider's "posted/updated" date string, evidence only — never a
    # deadline stand-in (see the connector package's docstring).
    posted_at = models.CharField(max_length=64, blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "opportunities"
        constraints = [
            models.UniqueConstraint(fields=["firm", "url"], name="uniq_opportunity_firm_url"),
        ]
        ordering = ["-first_seen"]
        # Until now this table carried exactly two indexes — the primary key
        # and `(firm, url)` — so EVERY query the Opportunities feed runs was a
        # sequential scan over the whole table: the feed itself, and each of
        # the cross-filtered facet counts. `status='open'` matches ~75% of
        # rows and is not selective on its own, which is why neither index
        # below leads with it alone.
        #
        # Measured on the live dev set (21,564 rows, 16,141 open), median of
        # 9 runs, with and without these two:
        #
        #   feed rows (status+bucket, firm join, ordered) 53.3 -> 47.9 ms
        #   region facet GROUP BY                          3.0 ->  0.5 ms
        #   bucket facet over open set                     5.6 ->  3.0 ms
        #
        # ~10ms per page load, and the planner adopts both (verified with
        # EXPLAIN — they are not indexes the optimiser ignores). The local
        # gain is modest only because this whole 58MB table currently fits in
        # cache on this machine; on the deployed 256MB Postgres a repeated
        # 30MB sequential scan is competing for buffer space with everything
        # else, which is where the difference between a scan and an index
        # stops being 10%. The table has also grown ~5x since the feed's own
        # comments were measured against a "4,342-row open set".
        #
        # A `first_seen` and a `deadline` index were measured too and are
        # deliberately absent: the planner either ignored them or they moved
        # nothing, and an index that earns nothing is still paid for on every
        # nightly scrape write.
        indexes = [
            models.Index(fields=["status", "bucket"], name="idx_opp_status_bucket"),
            models.Index(
                fields=["bucket", "deadline", "first_seen"],
                condition=models.Q(status="open"),
                name="idx_opp_open_campus",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.firm.name} — {self.title}"


class OpportunityChange(models.Model):
    """Append-only, row-level record of what actually MOVED on a posting.

    The scan has always known, per row, and always threw it away.
    `ingest._apply_opportunity` computes a `content_hash` and then decides
    two things outright on every one of the ~17,000 rows a full pass
    touches — `was_closed = existing.status == "closed"` and `changed =
    existing.content_hash != h` — before spending both on
    `stats["updated"] += 1` and moving on. `ScrapeRun.stats` was the only
    trace a scrape left behind, and it holds counts (`created`, `updated`,
    `unchanged`, `reopened`, `closed`) plus firm-level error dicts. Not one
    field in it can answer *which* posting, so nothing downstream could
    ever learn that a particular role's deadline had moved or that it had
    died. The evidence was computed and discarded, four times a day.

    The gap is not academic. A student tracks a role; the pass overwrites
    its `deadline` with a fresher one from the provider (`reverify` does
    exactly that, and the old date is gone the instant the new one is
    written) or flips `status` to "closed". Every consumer then reads the
    new row as though it had always said that. Nobody is told the date
    moved, because there was no table to tell them from.

    This is that table. One row per field that genuinely moved, written by
    the stage that moved it, carrying the value before and the value after.

    What it deliberately does NOT record:

    - **Unchanged postings.** The overwhelming majority of any pass is rows
      that did not move. A change row apiece would be ~17,000 writes a run
      recording, precisely, nothing. Only real moves land here.
    - **Creations.** A new posting has no "before" to record, and
      `first_seen` already dates it. Writing every field of every row on a
      fresh catalog's first scrape would bury the actual signal under the
      import.

    Values are stored as TEXT and rendered through `render_value`: a date
    becomes its ISO string, a status its own word, a title itself. That
    follows `FirmDate.history` — the one history-shaped structure that
    predates this model — which coerces every observation field with
    `str(...)` and stamps time with `.isoformat()` (see
    `import_firm_dates`). `""` means "no value" on either side, which is
    how a deadline dropped to NULL round-trips honestly instead of
    vanishing into an untyped null.
    """

    # Which pipeline stage wrote the row. The point of naming the stage is
    # that the same field moves for materially different reasons: a
    # `deadline` rewritten by `reverify` is the provider's own verify
    # endpoint stating a fresh date, while one dropped by `scrape` is our
    # unverified reading of prose being retracted (see ingest's
    # no-downgrade rules). A consumer that cannot tell them apart cannot
    # decide which is worth waking a student for.
    STAGE_SCRAPE = "scrape"
    STAGE_SCRAPE_CLOSE = "scrape.close"
    STAGE_REVERIFY = "reverify"

    # Half a year. The longest question anything asks of this table is "did
    # this move during my recruiting cycle", and a cycle runs from the
    # August kickoff to the following spring — 180 days covers one end to
    # end with room at both ends. Beyond that a row is history rather than
    # evidence: the posting it describes has almost certainly closed and
    # the student who tracked it has moved into the next cycle. At a few
    # hundred real moves per pass and four passes a day this settles at a
    # few hundred thousand rows and stops growing, which is the whole
    # point — an append-only table on a 6-hourly cron with no answer for
    # its own growth is a slow outage. `refresh` calls `prune` at the end
    # of every scheduled pass; `--prune-changes-older-than` overrides the
    # window, and 0 disables the sweep for an operator who wants to keep
    # a full season for analysis.
    RETENTION_DAYS = 180

    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.CASCADE, related_name="changes"
    )
    field = models.CharField(max_length=32)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    stage = models.CharField(max_length=32)
    # Free-text reason, where the field name alone understates what
    # happened — "absent from a complete fetch" is a very different close
    # from the provider's verify endpoint saying so out loud.
    note = models.TextField(blank=True, default="")
    # Passed in by the writer, never `auto_now_add`: one pass stamps a
    # single `now` across every row it touches, and that shared timestamp
    # is what makes "everything run N moved" a groupable question.
    observed_at = models.DateTimeField()

    class Meta:
        db_table = "opportunity_changes"
        # `-id` is the tiebreak that makes the order total: a whole pass
        # shares one `observed_at`, so sorting on it alone leaves the rows
        # within a run in whatever order the database feels like.
        ordering = ["-observed_at", "-id"]
        indexes = [
            # "what has moved on this posting" — the tracked-role timeline.
            models.Index(fields=["opportunity", "-observed_at"], name="opp_change_opp_seen_idx"),
            # "every deadline that moved since ..." — the alert/digest sweep.
            models.Index(fields=["field", "-observed_at"], name="opp_change_field_seen_idx"),
            # `prune`'s cutoff scan.
            models.Index(fields=["observed_at"], name="opp_change_seen_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.field}: {self.old_value or '∅'} -> {self.new_value or '∅'}"

    @staticmethod
    def render_value(value) -> str:
        """A stored value's honest text form. `None` -> `""` ("no value",
        the same convention `FirmDate.history` uses for a known-unknown
        date), dates -> ISO, everything else -> `str`."""
        if value is None:
            return ""
        if isinstance(value, _date):  # covers datetime, which subclasses date
            return value.isoformat()
        return str(value)

    @classmethod
    def entry(cls, opportunity_id: int, field: str, old, new, *,
              stage: str, at, note: str = "") -> "OpportunityChange":
        """An UNSAVED change row, values rendered. Callers accumulate these
        and `bulk_create` once per pass — a scrape that wrote them one at a
        time would add a query per move to a job already doing ~17,000."""
        return cls(
            opportunity_id=opportunity_id,
            field=field,
            old_value=cls.render_value(old),
            new_value=cls.render_value(new),
            stage=stage,
            note=note,
            observed_at=at,
        )

    @classmethod
    def prune(cls, *, older_than_days: int | None = None, now=None) -> int:
        """Delete change rows past the retention window; returns how many.

        `older_than_days=0` (or negative) is an explicit "keep everything"
        and deletes nothing, so an operator can disable the sweep without
        having to remove the call. In steady state each run only removes
        the slice that aged out since the last one, so the queryset
        `.delete()` walks is small however large the table gets.
        """
        days = cls.RETENTION_DAYS if older_than_days is None else older_than_days
        if days <= 0:
            return 0
        cutoff = (now or timezone.now()) - timedelta(days=days)
        return cls.objects.filter(observed_at__lt=cutoff).delete()[0]


class FirmDate(models.Model):
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE, related_name="firm_dates")
    cycle = models.CharField(max_length=16)
    region = models.CharField(max_length=64, blank=True, default="")
    event_kind = models.CharField(max_length=64)
    date = models.DateField(null=True, blank=True)
    precision = models.CharField(max_length=32, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    source_url = models.URLField(max_length=1024, blank=True, default="")
    found_on = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "firm_dates"
        constraints = [
            models.UniqueConstraint(
                fields=["firm", "cycle", "region", "event_kind"],
                name="uniq_firm_dates_firm_cycle_region_event",
            ),
        ]
        ordering = ["firm_id", "cycle", "event_kind"]

    def __str__(self) -> str:
        return f"{self.firm.name} {self.cycle} {self.event_kind}"


class EmailPatternStats(models.Model):
    """Aggregate-only, shared deliberately (§2): "Every user's bounces
    improve pattern confidence for everyone ... Raw bounce events stay
    private." `firm` is the primary key — one row per firm, matching the
    plan's `email_pattern_stats (firm_id PK, ...)`.
    """

    firm = models.OneToOneField(
        Firm, on_delete=models.CASCADE, primary_key=True, related_name="email_pattern_stats"
    )
    delivered = models.PositiveIntegerField(default=0)
    bounced = models.PositiveIntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_pattern_stats"

    def __str__(self) -> str:
        return f"{self.firm.name} pattern stats"


class ScrapeRun(models.Model):
    connector = models.CharField(max_length=128)
    started = models.DateTimeField()
    finished = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, blank=True, default="running")
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "scrape_runs"
        ordering = ["-started"]

    def __str__(self) -> str:
        return f"{self.connector} @ {self.started:%Y-%m-%d %H:%M}"
