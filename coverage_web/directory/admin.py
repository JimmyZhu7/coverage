from django.contrib import admin

from .models import EmailPatternStats, Firm, FirmDate, Opportunity, ScrapeRun


@admin.register(Firm)
class FirmAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "regions", "tracks")
    list_filter = ("status",)
    search_fields = ("name", "slug", "domains")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "firm",
        "bucket",
        "region",
        "status",
        "deadline",
        "confidence",
        "last_verified",
    )
    list_filter = ("status", "bucket", "region", "sponsorship")
    search_fields = ("title", "firm__name", "url")
    autocomplete_fields = ("firm",)
    date_hierarchy = "deadline"


@admin.register(FirmDate)
class FirmDateAdmin(admin.ModelAdmin):
    list_display = ("firm", "cycle", "region", "event_kind", "date", "confidence")
    list_filter = ("cycle", "event_kind", "region")
    search_fields = ("firm__name",)
    autocomplete_fields = ("firm",)


@admin.register(EmailPatternStats)
class EmailPatternStatsAdmin(admin.ModelAdmin):
    list_display = ("firm", "delivered", "bounced", "last_updated")
    search_fields = ("firm__name",)
    autocomplete_fields = ("firm",)


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = ("connector", "started", "finished", "status")
    list_filter = ("connector", "status")
    search_fields = ("connector",)
    date_hierarchy = "started"
