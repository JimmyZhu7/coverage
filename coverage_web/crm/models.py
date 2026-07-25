"""Private zone (docs/build-plan.md §2): `user_id` denormalized onto every
row, including `touches`. Every model here subclasses
`coverage_web.tenancy.PrivateModel` — see that module for the
tenant-scoped-manager contract.

Schema-compatibility note (critical — see the task brief and
coverage_domain/coverage_domain/pipeline.py's module docstring): `Contact`
and `Touch` below are read and written by pipeline.py's raw, unqualified
SQL (`SELECT ... FROM contacts`, `INSERT INTO touches (...)`). Their
`Meta.db_table` is pinned to the exact names pipeline.py expects
("contacts", "touches"), their `warmth`/`thread_state`/`channel`/`kind`/
`note`/`source` columns are plain, unconstrained CharField/TextField (no
Django `choices=`, no DB-level CHECK constraint) so the ported ratchet's
raw `UPDATE ... CASE` can never be rejected by application-level
validation it was never written to satisfy, and `user`/`contact` use
Django's default `<field>_id` column naming so `user_id` / `contact_id`
line up with pipeline.py's column references without any `db_column`
overrides.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from coverage_web.tenancy import PrivateModel
from directory.models import Firm


class UserFirm(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE)
    tier = models.SmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, blank=True, default="")

    class Meta(PrivateModel.Meta):
        db_table = "user_firms"
        constraints = [
            # Plan calls for PK(user_id, firm_id). A UniqueConstraint over
            # a surrogate auto `id` PK gives the identical "no duplicate
            # (user, firm) pair" guarantee without adopting Django 5.2's
            # very new CompositePrimaryKey — see report for the full
            # reasoning.
            models.UniqueConstraint(fields=["user", "firm"], name="uniq_user_firms_user_firm"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} × {self.firm_id}"


class Contact(PrivateModel):
    # The recruiting regions the product models. Deliberately NOT the same
    # vocabulary as `Firm.regions` (which also carries sg/eu): these two are
    # the only regions the cadence engine's deadline scoping and the
    # sponsorship rules actually reason about, so a contact only ever carries
    # one of them or blank ("unknown", the honest default).
    REGION_CHOICES = [("us", "United States"), ("hk", "Hong Kong")]
    REGION_VALUES = frozenset(value for value, _ in REGION_CHOICES)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    # Nullable with a firm_text fallback (§2's "Deliberate calls"):
    # students name firms outside the directory; capture must never block
    # on directory coverage.
    firm = models.ForeignKey(
        Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts"
    )
    firm_text = models.CharField(max_length=255, blank=True, default="")
    role = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    linkedin = models.URLField(max_length=512, blank=True, default="")
    # PROVENANCE ONLY — where this row came from ("manual", "import",
    # "capture", or a free-text campaign label on ported rows). It is NOT a
    # region field: `region` below is. See that field's comment.
    source = models.CharField(max_length=64, blank=True, default="")
    # Which recruiting region this person is being worked for. Explicit,
    # because the cadence engine scopes its pre-deadline re-ping by region and
    # used to infer that by substring-matching "hk" inside `source` — which
    # made every hand-added contact (source="manual") silently a US contact.
    # Blank means "unknown", and unknown is a real answer: the engine falls
    # back to matching either region rather than guessing.
    region = models.CharField(
        max_length=8, blank=True, default="", choices=REGION_CHOICES
    )
    # Plain, unconstrained — see module docstring. Values are managed
    # entirely by coverage_domain.pipeline's ratchet, not Django.
    warmth = models.CharField(max_length=32, default="cold")
    thread_state = models.CharField(max_length=32, default="no_reply")
    # PRIVATE — the user's own note ABOUT this person ("USC alum, super
    # responsive", "warm intro from a classmate"). It is shown on the contact
    # card and nowhere else. It must never be sent TO the contact: that is
    # what `opener` below is for. See crm.views._mailto.
    angle = models.TextField(blank=True, default="")
    # The draft body for an outbound email TO this person. Safe to put in a
    # mailto: URL — that is its whole purpose.
    opener = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    school_affiliation = models.BooleanField(default=False)
    # Display facts ported from the founder's campaign.db (both optional).
    school = models.CharField(max_length=64, blank=True, default="")
    gender = models.CharField(max_length=16, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    archived = models.BooleanField(default=False)
    email_pattern_recorded = models.BooleanField(default=False)
    # UI-only dismissal from the Today queue (Snooze / Skip). Not a touch and
    # not part of the pipeline ratchet — set directly via the ORM. A contact
    # is hidden from the cadence queue until this passes.
    snoozed_until = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "contacts"
        ordering = ["-created"]

    def __str__(self) -> str:
        return self.name

    def default_region_from_firm(self) -> str:
        """The region this contact's firm implies, or "" when it implies
        nothing. Only an UNAMBIGUOUS firm answers: exactly one modeled region
        (us/hk) on the firm. A firm that recruits in both, in neither, or only
        in a region this product doesn't model yields "" — guessing there is
        exactly the bug this field exists to kill, and a blank keeps the
        cadence engine's conservative both-regions fallback."""
        if self.firm_id is None:
            return ""
        known = {
            (r or "").strip().lower()
            for r in (self.firm.regions or [])
        } & self.REGION_VALUES
        return next(iter(known)) if len(known) == 1 else ""

    def save(self, *args, **kwargs):
        # Default region from the firm, but only when the user hasn't set one:
        # an explicit region always wins, and a blank one stays blank unless
        # the firm makes it unambiguous.
        if not self.region:
            inferred = self.default_region_from_firm()
            if inferred:
                self.region = inferred
                # A partial save() must still persist the column we just
                # filled in, or the value silently vanishes.
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = {*update_fields, "region"}
        super().save(*args, **kwargs)


class Touch(PrivateModel):
    """Append-only — no UPDATE path exists in application code (§2). This
    is a design/process invariant enforced by convention (only
    `coverage_domain.pipeline.apply_touch` / `set_state`, via
    `crm.services`, ever write here), not by a DB trigger — kept
    deliberately simple per the task's "pragmatic, not over-engineered"
    guidance.
    """

    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("capture", "Capture"),
        ("import", "Import"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="touches")
    ts = models.DateTimeField()
    # Nullable: pipeline.py's set_state() manual-override audit touch
    # inserts channel=NULL (there is no channel for a state override).
    channel = models.CharField(max_length=32, null=True, blank=True)
    # Plain, unconstrained — see module docstring.
    kind = models.CharField(max_length=64)
    note = models.TextField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default="manual")
    capture_event = models.ForeignKey(
        "CaptureEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="touches",
    )

    class Meta(PrivateModel.Meta):
        db_table = "touches"
        ordering = ["-ts"]

    def __str__(self) -> str:
        return f"{self.kind} @ {self.ts:%Y-%m-%d %H:%M}"


class CaptureEvent(PrivateModel):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("applied", "Applied"),
        ("needs_review", "Needs review"),
        ("ignored", "Ignored"),
    ]
    DIRECTION_CHOICES = [("outbound", "Outbound"), ("inbound", "Inbound")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    provider = models.CharField(max_length=32)
    provider_ref = models.CharField(max_length=255)
    direction = models.CharField(max_length=16, choices=DIRECTION_CHOICES, blank=True, default="")
    counterparty_email = models.EmailField(blank=True, default="")
    counterparty_name = models.CharField(max_length=255, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    raw_ref = models.CharField(max_length=512, blank=True, default="")
    signals = models.JSONField(default=dict, blank=True)
    extraction_version = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")

    class Meta(PrivateModel.Meta):
        db_table = "capture_events"
        constraints = [
            # Load-bearing per the task brief: makes at-least-once webhook
            # delivery idempotent.
            models.UniqueConstraint(
                fields=["user", "provider", "provider_ref"],
                name="uniq_capture_events_user_provider_ref",
            ),
        ]
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_ref}"


class Task(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    why = models.TextField(blank=True, default="")
    due = models.DateField(null=True, blank=True)
    kind = models.CharField(max_length=64, blank=True, default="")
    firm = models.ForeignKey(
        Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    # Dedup key the ported cadence/backward-planner (§4) uses for its
    # ≤3-day in-place update instead of spawning duplicate tasks.
    source_key = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="open")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "tasks"
        ordering = ["due", "-created"]

    def __str__(self) -> str:
        return self.title
