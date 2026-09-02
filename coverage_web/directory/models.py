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

# The six preference-eligible desk slugs, imported rather than restated so
# `FirmDate.track`'s CHECK constraint and `User.tracks` can never disagree
# about what a track is. `classify` imports nothing from this module.
from directory.classify import TRACKED_TRACKS


class Firm(models.Model):
    # Unique, and never blank. `unique=True` alone permits exactly one blank
    # row, which is legal-but-useless: `Firm.objects.get(slug=...)` can never
    # address it, `seed_directory`'s `update_or_create(slug=...)` would collide
    # with it, and it silently drops out of every slug-keyed map in the app.
    # One such row existed (id 218, "Citadel Securities") — not from any code
    # path in this repo, all of which pass an explicit slug, but from a
    # `manage.py shell` insert that simply omitted the field and got Django's
    # implicit "" for it. Nothing in code could have prevented that, which is
    # why the guard is a database constraint rather than a validator: see
    # `Meta.constraints` below and migration `0011`.
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
    # HOW THIS FIRM ACTUALLY HIRES, as far as networking is concerned.
    #
    # `campus` (the default, and the blank-equivalent) is the funnel the rest
    # of the CRM is built for: coffee chats, referrals, an analyst who pushes
    # a name onto the shortlist. `assessment` is the funnel where none of that
    # moves the process, because the process IS a test — Jane Street's public
    # FAQ answers "Can I schedule a phone call or coffee?" with "unfortunately,
    # no"; Citadel Securities' campus funnel is Datathons and Invitationals;
    # the practitioner finding is "if you can't pass their tests it doesn't
    # matter who you know". Tagging a firm `assessment` tells three readers
    # to stop pretending otherwise: the Coverage Gaps strip (no networking
    # gap to close, the verb is "Apply"), the "Who to find" panel (a campus
    # recruiter and a referral, never "an analyst to chat with"), and the
    # Network summary that counts them.
    #
    # A firm-level fact, not a per-user one, for the same reason `tracks` and
    # `regions` are: it describes the employer. Seeded by migration
    # `directory/0017` for the prop-trading and quant names on the board;
    # multi-strat hedge funds that run analyst programmes with real
    # networking (Millennium, Point72, AQR) are deliberately left `campus`.
    RECRUITING_STYLE_CAMPUS = "campus"
    RECRUITING_STYLE_ASSESSMENT = "assessment"
    RECRUITING_STYLE_CHOICES = [
        (RECRUITING_STYLE_CAMPUS, "Campus: networking moves the process"),
        (RECRUITING_STYLE_ASSESSMENT, "Assessment: test-gated, a chat does not"),
    ]
    recruiting_style = models.CharField(
        max_length=16,
        choices=RECRUITING_STYLE_CHOICES,
        default=RECRUITING_STYLE_CAMPUS,
        blank=True,
        help_text=(
            "How the firm hires. 'campus' (default): coffee chats and "
            "referrals move the process. 'assessment': the process is a "
            "test or competition and networking does not move it."
        ),
    )
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
        constraints = [
            # Enforced in Postgres so it holds for `manage.py shell`, `dbshell`
            # and a raw `INSERT` too — the paths that produced the one blank
            # slug this table ever carried. A model validator would only fire
            # in a ModelForm, i.e. nowhere near where the row came from.
            models.CheckConstraint(
                condition=~models.Q(slug=""),
                name="firm_slug_not_blank",
            ),
        ]

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
    # 0.0-1.0 only — see `Meta.constraints`'s `opportunities_confidence_in_range`
    # for why that is a database CHECK and not a validator here.
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
            # See `firm_slug_not_blank` on `Firm` for the precedent: a
            # database CHECK rather than a field validator, because a
            # validator only runs inside a ModelForm's `full_clean()` and
            # every writer here (`ingest.py`, `enrich_postings`, admin,
            # `manage.py shell`) calls `.save()`/`.create()` directly.
            # `confidence` is a 0.0-1.0 float everywhere it's read
            # (`confidence_marker`, `deadline_provenance`'s `_CONFIRMED_AT`
            # comparison) — a stray percentage (`95` for "95%") would clear
            # every `>=` threshold those readers use while being 95x too
            # large, silently. See `firm_dates_confidence_in_range` below;
            # this is its sibling on the other table sharing the same column
            # shape and the same unrestricted admin `ModelAdmin`.
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0) & models.Q(confidence__lte=1.0),
                name="opportunities_confidence_in_range",
            ),
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


# `FirmDate.precision`'s closed vocabulary. Module-level so both the field's
# own `PRECISIONS` alias and the CheckConstraint inside `Meta` can name one
# list — a nested `Meta` cannot see its outer class's attributes, and two
# hand-kept copies of a vocabulary is how a vocabulary stops being closed.
# "" means the row never stated a precision, which every renderer treats as
# day-precise (the date is a real day, nobody qualified it).
FIRM_DATE_PRECISIONS = ("", "day", "month", "estimated")


class FirmDate(models.Model):
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE, related_name="firm_dates")
    # The recruiting cycle this date belongs to: `sa2028`, `ft2027`, or "" for
    # a date whose cycle was never stated. Shape enforced by
    # `firm_dates_cycle_vocabulary` below; the parser and the reasoning for a
    # shape rather than an enumeration live in `directory.timeline`.
    cycle = models.CharField(max_length=16, blank=True, default="")
    # The desk the date is scoped to, from `classify.TRACKED_TRACKS` — the same
    # six slugs `User.tracks` holds — or "" for a cycle-wide date. Split out of
    # `cycle` (which used to carry `sa2028_ib`) so that a firm's programmes can
    # be grouped across firms and matched against a student's stated tracks;
    # see `directory.timeline`.
    track = models.CharField(max_length=32, blank=True, default="")
    region = models.CharField(max_length=64, blank=True, default="")
    event_kind = models.CharField(max_length=64)
    date = models.DateField(null=True, blank=True)
    # How exactly `date` locates the event, NOT how sure we are it happens —
    # that is `confidence`. A CLOSED vocabulary, and the closure is what makes
    # it safe to read: every renderer branches on the three known values and
    # falls through to an exact "Sep 22, 2026" for anything else
    # (`deadline_marker`, `_firm_date_row`, `crm.utils.CONFIRMED_PRECISIONS`).
    # So an unrecognised string does not degrade to "unknown precision", it
    # silently CLAIMS DAY PRECISION — a date whose own column says it is a
    # guess, printed as a specific day. See `Meta.constraints`'s
    # `firm_dates_precision_vocabulary`.
    PRECISIONS = FIRM_DATE_PRECISIONS
    precision = models.CharField(max_length=32, blank=True, default="")
    # 0.0-1.0 — the three-band vocabulary `import_firm_dates.CONFIDENCE_BAND` /
    # `seed_directory._CONFIDENCE_BAND` map onto (rumor 0.3, reported 0.6,
    # confirmed_official 1.0), and the only vocabulary `crm.utils._confidence_label`
    # and every `>= 0.8`/`== 1.0` reader downstream understands. See
    # `Meta.constraints`'s `firm_dates_confidence_in_range` for why this is a
    # database CHECK rather than a validator on the field.
    confidence = models.FloatField(default=0.0)
    # The TIME OF DAY the firm stated, and the zone it stated it in. Both
    # nullable, both populated only where a `confirmed_official` row's own
    # source says the hour out loud, and NEVER derived from anything.
    #
    # WHY THE PAIR EXISTS. `date` is a bare `DateField`, so every renderer
    # has been reading a deadline as "the whole of that day, in the reader's
    # own zone". The real closes are not: HSBC's Hong Kong CIB close is
    # 30 October, Citi HK's is "Friday, October 30, 2026 at 23:59 HKT", and
    # Morgan Stanley HK ran two staged deadlines both at 23:55 HKT
    # (`seeds/timeline_hk.yaml`, all Grade A). For a Los Angeles student
    # 23:59 HKT on the 30th is 08:59 on the 30th local, so the row sat on
    # the deadlines rail for fifteen more hours after the door had shut.
    #
    # WHY NOT ONE AWARE `DateTimeField`. Because the two halves are not
    # equally known. A row can have a day and no hour — that is 25 of the 41
    # rows this decision was measured against — and an aware datetime cannot
    # express "this day, hour unknown" without inventing midnight, which is
    # a time no firm stated. Keeping `date` authoritative and hanging an
    # OPTIONAL instant off it means the absence stays visible in the schema
    # rather than being papered over with a default. `closes_at()` below is
    # the one place the two are combined.
    #
    # WHY AN IANA KEY AND NOT "HKT". An abbreviation cannot be converted:
    # `zoneinfo` has no "HKT", "CST" is three different zones on two
    # continents, and half of them shift by an hour twice a year. The zone
    # key can produce the abbreviation (`%Z` gives "HKT", and "PDT" or "PST"
    # for the reader depending on the date); the abbreviation cannot produce
    # the zone. So the column stores the convertible fact and the label is
    # rendered from it — P5, one definition per fact.
    close_time = models.TimeField(null=True, blank=True)
    close_tz = models.CharField(max_length=64, blank=True, default="")
    source_url = models.URLField(max_length=1024, blank=True, default="")
    found_on = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "firm_dates"
        constraints = [
            # `track` joined this key when it was split out of `cycle`, and it
            # had to: `sa2028_ib` and `SA 2028` normalise to the SAME cycle, so
            # without the desk in the key Goldman's two US `app_open` rows (id
            # 11, "~ Mar 2027" estimated from past cycles; id 33, Aug 15 2026
            # confirmed off goldmansachs.com) would have collided on migration.
            #
            # Widening the key is also what makes the old duplicate-scope class
            # of bug unreachable rather than merely detected. `_flag_conflicting_
            # closes` in directory/views.py exists because two spellings of one
            # cycle both satisfied the OLD four-column key and then printed
            # identically — the founder's jpm page carried two `app_close` rows
            # both badged "confirmed" with nothing to separate them. With the
            # cycle vocabulary closed, two rows that PRINT the same scope now
            # necessarily collide on this constraint and the second one cannot
            # be written at all. The read-path guard is kept regardless: it is
            # cheap, and a constraint only covers the writers that go through
            # the ORM.
            models.UniqueConstraint(
                fields=["firm", "cycle", "track", "region", "event_kind"],
                name="uniq_firm_dates_firm_cycle_track_region_event",
            ),
            # `cycle` was the last free-text key on this model, and free text in
            # a key is the same latent bug `firm_dates_confidence_in_range`
            # below was added for — one writer's spelling silently failing to
            # match another's. Four spellings of "SA 2028" were live.
            #
            # A regex rather than an `__in` whitelist because the vocabulary is
            # a SHAPE that moves with the calendar: an enumeration would need a
            # migration every autumn, which is the same staleness
            # `recommend.cycle_choices` is a function to avoid. `directory.
            # timeline.CYCLE_RE` is the same pattern in Python, and
            # `is_valid_cycle` is what the writers check BEFORE they save, so a
            # bad finding is skipped with the firm named rather than raising an
            # IntegrityError halfway through an import.
            models.CheckConstraint(
                condition=models.Q(cycle="") | models.Q(cycle__regex=r"^(sa|ft)[0-9]{4}$"),
                name="firm_dates_cycle_vocabulary",
            ),
            # The desk half, closed against the SAME six slugs a student can
            # state a preference for. A seventh spelling here would silently
            # stop matching `User.tracks` — the join this column exists for.
            models.CheckConstraint(
                condition=models.Q(track="") | models.Q(track__in=list(TRACKED_TRACKS)),
                name="firm_dates_track_vocabulary",
            ),
            # A row (id 44, J.P. Morgan, app_close) once carried
            # `confidence=95.0` — someone meant "95%" and typed the number
            # the column's own scale disagrees with. `history=[]` and
            # `found_on=None` on that row rule out both real writers
            # (`import_firm_dates.py`, `seed_directory.py` — both look the
            # label up in a `{"rumor": 0.3, ...}` dict, so neither can ever
            # emit anything but 0.0/0.3/0.6/1.0); it came in through
            # `FirmDateAdmin`, which places no bounds on the raw float, or an
            # equivalent `manage.py shell` write — the same class of path
            # `firm_slug_not_blank` was added for on `Firm`, and for the same
            # reason a validator is the wrong tool: `full_clean()` never runs
            # on a bare `.save()`, which is what the admin's own change form
            # and every management command here use.
            #
            # The corruption was not cosmetic. `_firm_date_row` in
            # `directory/views.py` treats `confidence >= 0.8` as "confirmed"
            # and `confidence_marker` renders it as a percentage — 95.0 cleared
            # both, rendering a "confirmed · 9500%" badge on the firm's public
            # timeline page. It happened to fall short of the *exact* `== 1.0`
            # checks the calendar (`crm/calendar_views.py`, `crm/today.py`) and
            # the cadence/Coverage-Gaps engine (`crm.utils._confidence_label`,
            # keyed off `round(value, 1)`) use, so neither the calendar nor a
            # re-ping ever fired on it — but a 0.3 or 0.6 fat-fingered as 30 or
            # 60 the same way would only need to land on a different reader to
            # do worse, which is why the guard is on the column, not on a
            # handful of the callers.
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0) & models.Q(confidence__lte=1.0),
                name="firm_dates_confidence_in_range",
            ),
            # The precision vocabulary, on the column for the same reason
            # `firm_dates_confidence_in_range` is: the writers that CAN put a
            # bad value here are the ones a field validator never runs for.
            # `import_firm_dates` passes `str(entry.get("precision", ""))`
            # straight from a hand-written YAML/JSON findings file with no
            # vocabulary check at all (the sibling `event_kind` IS checked
            # against `EVENT_KINDS` two lines earlier, and `confidence` is
            # looked up in `CONFIDENCE_BAND` — `precision` is the one field
            # of the four that passes through raw), `FirmDateAdmin` places no
            # bounds on it, and `full_clean()` runs on none of those paths.
            #
            # The failure is quiet and it is a lying date, which is worse
            # than the loud one: a typo'd "aproximate" does not render as
            # "unknown", it renders as an exact day (see the field comment).
            # All 41 live rows and both seed files already use only these
            # four values, so this constrains nothing that is actually in
            # use — it stops the fifth from being writable.
            models.CheckConstraint(
                condition=models.Q(precision__in=FIRM_DATE_PRECISIONS),
                name="firm_dates_precision_vocabulary",
            ),
            # A time without its zone is not a fact, it is a number. "23:59"
            # is 15 hours apart depending on where it was said, and the whole
            # reason these columns exist is that the product was reading a
            # deadline in the wrong zone. A zone without a time is the same
            # emptiness wearing a label. So: both, or neither.
            #
            # On the column rather than in a validator for the reason every
            # other constraint on this model is: `full_clean()` runs on none
            # of the writers here — not `FirmDateAdmin`'s change form, not
            # `import_firm_dates`, not a `manage.py shell` write.
            models.CheckConstraint(
                condition=(
                    (models.Q(close_time__isnull=True) & models.Q(close_tz=""))
                    | (models.Q(close_time__isnull=False) & ~models.Q(close_tz=""))
                ),
                name="firm_dates_close_time_needs_a_zone",
            ),
            # A time may only sit on a row that locates a real day. "month"
            # is a legitimate confirmed precision — Goldman's "Applications
            # will open in the fall 2026" is stored as 2026-09 — and an hour
            # on a month is not a more precise fact, it is a fabricated one:
            # combining it with `date` would produce an instant on the first
            # of the month that nobody stated. "estimated" is worse again,
            # and 25 of the 41 rows this was measured against are estimates.
            #
            # `date IS NULL` is covered too: a row whose day is "to be
            # confirmed" cannot carry an hour either.
            models.CheckConstraint(
                condition=(
                    models.Q(close_time__isnull=True)
                    | (models.Q(precision__in=["", "day"]) & models.Q(date__isnull=False))
                ),
                name="firm_dates_close_time_needs_a_day",
            ),
        ]
        ordering = ["firm_id", "cycle", "event_kind"]

    def __str__(self) -> str:
        return f"{self.firm.name} {self.cycle} {self.event_kind}"

    def closes_at(self):
        """The instant this date closes, aware, or None when the firm never
        said an hour.

        None is the common and correct answer: only a `confirmed_official`
        row whose own source states a time is ever populated, so every
        estimate, every rumour and every row whose posting simply did not say
        answers None here and its readers fall back to local midnight. That
        fallback is today's behaviour exactly (P3) — the countdown a student
        sees on a row with no stated hour does not move.

        A `close_tz` that `zoneinfo` cannot resolve answers None rather than
        raising. A renderer is the wrong place to discover that a zone
        database is missing a key, and the honest degradation is the same one
        a row with no time gets.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if self.close_time is None or not self.close_tz or self.date is None:
            return None
        try:
            zone = ZoneInfo(self.close_tz)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return timezone.datetime.combine(self.date, self.close_time, tzinfo=zone)

    def close_time_label(self, viewer_tz: str = "") -> str:
        """"23:59 HKT, 08:59 your time" — the firm's stated hour, and the
        reader's own clock beside it.

        Empty string when the firm stated no time, which is what every caller
        renders as nothing at all. The second half appears only when the
        reader's zone is known AND differs from the firm's: "23:59 HKT, 23:59
        your time" is noise, and a student in Hong Kong reading a Hong Kong
        deadline should just see the deadline.

        Both zone abbreviations are read off the zone AT THIS DATE rather
        than stored, so a close in November prints PST and one in October
        prints PDT without anybody maintaining a table of which is which.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        instant = self.closes_at()
        if instant is None:
            return ""
        firm_half = f"{instant:%H:%M} {instant:%Z}".strip()
        if not viewer_tz or viewer_tz == self.close_tz:
            return firm_half
        try:
            local = instant.astimezone(ZoneInfo(viewer_tz))
        except (ZoneInfoNotFoundError, ValueError):
            return firm_half
        if local.utcoffset() == instant.utcoffset():
            return firm_half
        return f"{firm_half}, {local:%H:%M} your time"


class FirmCycleObservation(models.Model):
    """A MEASURED distribution of when a firm's postings opened and closed
    for one (firm, region), rebuilt from `OpportunityChange` + `ScrapeRun` —
    never a single curated date, and never hand-edited.

    This is NOT a second `FirmDate`. `FirmDate` stores one asserted date per
    (firm, cycle, track, region, event_kind) with a confidence band, because
    someone READ a claim somewhere and is vouching for it. What this table
    holds is the opposite shape of fact: N postings, each with its own
    first-seen or closed-at timestamp, and the honest answer is the spread of
    those timestamps, not a single date standing in for all of them.
    Collapsing "14 postings opened between Aug 3 and Aug 12" down to one
    `FirmDate` row would have to either pick one of the 14 dates and discard
    the other 13, or invent a summary date ("Aug 7ish") nobody actually
    observed — both destroy exactly the count-and-spread that make this
    evidence credible instead of a rumor with an official-looking column
    next to it. Two tables, two provenances: a reader who sees a `FirmDate`
    row knows a human found a claim; a reader who sees a row here knows the
    scraper watched it happen. Merging them would erase that distinction for
    every consumer downstream, permanently.

    Every close counted here has already passed `directory.cycle_trust`'s
    TRUSTED check — a close attributed to a board the same pass reported as
    failed, or to a run that wiped out most of a firm's open postings in one
    shot, is excluded rather than averaged in, because a distribution built
    from a mix of real closes and connector failures would look identical to
    one built from real closes alone; there is no statistical tell that
    would let a consumer discount the noise later. `opened_at` facts carry
    no such filter: `Opportunity.first_seen` is stamped once, by the row's
    own creation, and a board fetch that fails or goes dark can only cause a
    posting to go MISSING from a pass, never to spuriously appear in one —
    so there is no failure mode here to guard against, and inventing one
    would be inventing an asymmetry the code doesn't have.

    A firm's very first scrape batch is deliberately excluded from
    `opened_count`/`open_window_*`: `first_seen` on those rows means "this
    posting already existed the day Coverage started watching this firm's
    board," not "we watched it open" — the two are easy to conflate and only
    the second is evidence a recruiting cycle actually started on that day.
    See `build_cycle_observations`'s `_onboarding_cutoff` for how the
    cutoff is found. A firm with no evidence on one side (nothing observed
    to open, or every close excluded as suspect) gets that side left at its
    zero/blank default rather than a fabricated value — see the field
    comments below and the module's constraint against ever implying a
    prediction: there has not yet been an observed October, and this table
    must never look like there has.
    """

    firm = models.ForeignKey(Firm, on_delete=models.CASCADE, related_name="cycle_observations")
    # Not TRACKED_REGIONS-restricted on purpose: "" (the posting's location
    # didn't resolve to a known market) and "global"/"other" are all real,
    # observed values on live rows, and dropping them would silently shrink
    # the population every count below is measured against.
    region = models.CharField(max_length=64, blank=True, default="")

    # Scoped to `classify.TARGET_BUCKETS` only (insight/internship/
    # entry_level) — this table exists to describe RECRUITING cycles, and a
    # firm's non-campus volume (retail-branch hiring, IT support reqs) would
    # otherwise swamp the very sparse campus signal it is meant to surface.
    # See `build_cycle_observations` for the query this mirrors.
    opened_count = models.PositiveIntegerField(default=0)
    open_window_first = models.DateField(null=True, blank=True)
    open_window_last = models.DateField(null=True, blank=True)

    # TRUSTED closes only (see class docstring). `excluded_suspect_closes`
    # is not a footnote — it is the number a consumer needs to judge whether
    # a THIN close window is thin because few postings have closed, or thin
    # because most of the evidence was thrown out as unreliable, which read
    # identically in `closed_count` alone.
    closed_count = models.PositiveIntegerField(default=0)
    close_window_first = models.DateField(null=True, blank=True)
    close_window_last = models.DateField(null=True, blank=True)
    excluded_suspect_closes = models.PositiveIntegerField(default=0)

    # A live snapshot at compute time, NOT an observation-window fact —
    # deliberately kept separate from `opened_count`/`closed_count` so a
    # reader never mistakes "how many are open right now" for "how many we
    # watched open." Lets a consumer sanity-check the other two columns
    # against the board's current shape without re-querying `Opportunity`.
    currently_open_count = models.PositiveIntegerField(default=0)

    # The excluded onboarding day itself (see class docstring), kept for
    # transparency rather than silently discarded — a firm whose entire
    # visible history is one onboarding batch has `onboarded_at` set and
    # `opened_count == 0`, which is a materially different, and much more
    # honest, state than "no data" with no explanation on offer.
    onboarded_at = models.DateField(null=True, blank=True)

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "firm_cycle_observations"
        constraints = [
            models.UniqueConstraint(
                fields=["firm", "region"], name="uniq_firm_cycle_observation_firm_region"
            ),
        ]
        ordering = ["firm_id", "region"]

    def __str__(self) -> str:
        return f"{self.firm.name} ({self.region or 'unstated region'}) cycle observation"


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
