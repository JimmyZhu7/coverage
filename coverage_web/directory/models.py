"""Shared zone (docs/build-plan.md §2): no `user_id` anywhere in this
file, by design — these tables are written only by the central scrape
worker/verification layer and read by every user via a `user_firms` join
(the "read-time query, not a fetch" design in §2's "The shared-cache
design, concretely"). Plain `models.Model` / default manager throughout;
`coverage_web.tenancy.PrivateModel` is deliberately not used here.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models


class Firm(models.Model):
    slug = models.SlugField(max_length=128, unique=True)
    name = models.CharField(max_length=255)
    domains = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        help_text="Email domains associated with this firm, e.g. ['jpmorgan.com'].",
    )
    regions = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    tracks = ArrayField(models.CharField(max_length=64), default=list, blank=True)
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

    def __str__(self) -> str:
        return f"{self.firm.name} — {self.title}"


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
