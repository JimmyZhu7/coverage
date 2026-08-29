from django.contrib import admin

from .models import CreditLedger, ProcessedStripeEvent, ProWaitlist


@admin.register(CreditLedger)
class CreditLedgerAdmin(admin.ModelAdmin):
    """Founder-facing view of the ledger, plus the "admin can grant/adjust
    credits by hand" requirement (docs/credit-system-plan.md §8: "admin IS
    the billing system for now") — the standard Add form already does the
    job: pick the user, a positive `delta`, `kind="adjust"`, and a note in
    `props` if it matters, e.g. {"reason": "beta tester top-up"}.

    Existing rows are read-only, never editable, AND never deletable: this
    is an append-only audit trail (billing/models.py's own docstring), and a
    row that could be silently edited OR removed after the fact stops being
    one — deleting a row is a rewrite of history exactly like editing one,
    and a worse one here, since `billing.credits._raw_balance` sums `delta`
    over every row: delete a spend and the ledger hands a student back
    credits they already used, silently, with no error and no trace. A
    mistaken grant is corrected with a second, opposite-sign row — same
    discipline `analytics.ProductEvent` already gets — not by rewriting
    history in either direction.
    """

    list_display = ("user", "delta", "kind", "period", "created")
    list_filter = ("kind",)
    search_fields = ("user__email",)
    date_hierarchy = "created"
    readonly_fields = ("created",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
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


@admin.register(ProWaitlist)
class ProWaitlistAdmin(admin.ModelAdmin):
    """This IS the notification list until Pro has a real checkout — the
    founder reads it here by hand and reaches out manually. See the model's
    own docstring on why no email is sent automatically."""

    list_display = ("email", "user", "source", "created")
    list_filter = ("source",)
    search_fields = ("email", "user__email")
    date_hierarchy = "created"
    readonly_fields = ("email", "user", "source", "created")

    def has_add_permission(self, request):
        return False
