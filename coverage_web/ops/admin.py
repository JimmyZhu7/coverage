from django.contrib import admin

from .models import JobRun


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    """Read-only, same posture as billing.ProcessedStripeEventAdmin — a
    debugging aid for "did this cron actually run", never something to
    hand-edit. A row rewritten after the fact would defeat the point of
    /ops/health/cron/ trusting it."""

    list_display = ("name", "status", "started_at", "finished_at")
    list_filter = ("name", "status")
    date_hierarchy = "started_at"
    readonly_fields = ("name", "started_at", "finished_at", "status")

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
