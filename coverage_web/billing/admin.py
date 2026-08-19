from django.contrib import admin

from .models import CreditLedger, ProcessedStripeEvent


@admin.register(CreditLedger)
class CreditLedgerAdmin(admin.ModelAdmin):
    """Founder-facing view of the ledger, plus the "admin can grant/adjust
    credits by hand" requirement (docs/credit-system-plan.md §8: "admin IS
    the billing system for now") — the standard Add form already does the
    job: pick the user, a positive `delta`, `kind="adjust"`, and a note in
    `props` if it matters, e.g. {"reason": "beta tester top-up"}.

    Existing rows are read-only, never editable: this is an append-only
    audit trail (billing/models.py's own docstring), and a row that could
    be silently edited after the fact stops being one. A mistaken grant is
    corrected with a second, opposite-sign row — same discipline
    `analytics.ProductEvent` already gets — not by rewriting history.
    """

    list_display = ("user", "delta", "kind", "period", "created")
    list_filter = ("kind",)
    search_fields = ("user__email",)
    date_hierarchy = "created"
    readonly_fields = ("created",)

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProcessedStripeEvent)
class ProcessedStripeEventAdmin(admin.ModelAdmin):
    """Read-only, like CreditLedgerAdmin above — a debugging aid for "did
    this webhook event actually land," never something to hand-edit.
    Deleting a row here would just make a legitimate redelivery grant
    credits a second time, so no delete permission either."""

    list_display = ("stripe_event_id", "created")
    search_fields = ("stripe_event_id",)
    readonly_fields = ("stripe_event_id", "created")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
