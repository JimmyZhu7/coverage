"""Private zone, continued (docs/build-plan.md §2): the four remaining
private tables the task brief splits into a second app — scoring,
per-user opportunity tracking, product instrumentation, and import
bookkeeping. Same `PrivateModel` base as `crm` (see
coverage_web/tenancy.py); `ProductEvent.user` is the one nullable
tenant FK in the private zone (pre-signup/anonymous events), matching
`product_events (id, user_id NULL, ...)` in the plan verbatim.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from coverage_web.tenancy import PrivateModel
from directory.models import Opportunity


class UserOpportunity(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE)
    applied_status = models.CharField(max_length=32, blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)
    interview_dates = models.JSONField(default=list, blank=True)
    dismissed = models.BooleanField(default=False)

    class Meta(PrivateModel.Meta):
        db_table = "user_opportunities"
        constraints = [
            # PK(user_id, opportunity_id) in the plan — see UserFirm's
            # Meta docstring in crm/models.py for why a UniqueConstraint
            # over a surrogate `id` PK is used instead of a true
            # composite PK.
            models.UniqueConstraint(
                fields=["user", "opportunity"], name="uniq_user_opportunities_user_opp"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} × {self.opportunity_id}"


class FitScore(PrivateModel):
    SUBJECT_CHOICES = [("contact", "Contact"), ("firm", "Firm")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    subject_type = models.CharField(max_length=16, choices=SUBJECT_CHOICES)
    # Polymorphic reference (a Contact.id or a Firm.id depending on
    # subject_type) — plain integer, not an FK, matching the plan's
    # untyped `subject_id`.
    subject_id = models.PositiveIntegerField()
    axes = models.JSONField(default=dict, blank=True)
    composite = models.FloatField(null=True, blank=True)
    reasoning = models.TextField(blank=True, default="")
    params_version = models.CharField(max_length=32, blank=True, default="")
    inputs_hash = models.CharField(max_length=64, blank=True, default="")
    computed_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "fit_scores"
        indexes = [
            models.Index(fields=["subject_type", "subject_id"], name="fit_scores_subject_idx"),
        ]
        ordering = ["-computed_at"]

    def __str__(self) -> str:
        return f"{self.subject_type}:{self.subject_id} = {self.composite}"


class ProductEvent(PrivateModel):
    # Nullable per the plan: pre-signup/anonymous funnel events have no
    # user yet. See tenancy.py's PrivateModel docstring for why this is
    # still handled by the same tenant-scoped-manager base (for_user()
    # still works; all_objects is exactly what the founder's aggregate
    # metrics queries in §8 need).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="product_events",
    )
    event = models.CharField(max_length=128)
    props = models.JSONField(default=dict, blank=True)
    ts = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "product_events"
        ordering = ["-ts"]

    def __str__(self) -> str:
        return self.event


class Import(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=64, blank=True, default="")
    filename = models.CharField(max_length=255, blank=True, default="")
    row_stats = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "imports"
        ordering = ["-created"]

    def __str__(self) -> str:
        return self.filename or f"import #{self.pk}"
