from django.contrib import admin

from .models import FitScore, Import, ProductEvent, UserOpportunity


@admin.register(UserOpportunity)
class UserOpportunityAdmin(admin.ModelAdmin):
    list_display = ("user", "opportunity", "applied_status", "dismissed")
    list_filter = ("applied_status", "dismissed")
    search_fields = ("user__email", "opportunity__title", "opportunity__firm__name")


@admin.register(FitScore)
class FitScoreAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "subject_type",
        "subject_id",
        "composite",
        "params_version",
        "computed_at",
    )
    list_filter = ("subject_type", "params_version")
    search_fields = ("user__email", "inputs_hash")


@admin.register(ProductEvent)
class ProductEventAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "ts")
    list_filter = ("event",)
    search_fields = ("event", "user__email")
    date_hierarchy = "ts"


@admin.register(Import)
class ImportAdmin(admin.ModelAdmin):
    list_display = ("filename", "user", "kind", "created")
    list_filter = ("kind",)
    search_fields = ("filename", "user__email")
