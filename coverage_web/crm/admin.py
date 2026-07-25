"""Admin registration for the private-zone CRM models.

Every ModelAdmin below relies on Django's default
`ModelAdmin.get_queryset()`, which calls `self.model._default_manager`.
Because `coverage_web.tenancy.PrivateModel.Meta` pins
`default_manager_name = "all_objects"`, that resolves to the plain,
unscoped manager automatically — the founder gets a working cross-tenant
CRUD surface with no extra wiring, while application code reaching for
`Model.objects` still gets the tenant-scoped guard. See tenancy.py.
"""

from django.contrib import admin

from .models import CaptureEvent, ChatDebrief, Contact, Task, Touch, UserFirm


@admin.register(UserFirm)
class UserFirmAdmin(admin.ModelAdmin):
    list_display = ("user", "firm", "tier", "status")
    list_filter = ("status", "tier")
    search_fields = ("user__email", "firm__name")
    autocomplete_fields = ("firm",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "firm",
        "firm_text",
        "role",
        "region",
        "warmth",
        "thread_state",
        "archived",
        "created",
    )
    list_filter = ("warmth", "thread_state", "region", "archived")
    search_fields = ("name", "email", "firm_text", "user__email", "firm__name")
    autocomplete_fields = ("firm",)


@admin.register(Touch)
class TouchAdmin(admin.ModelAdmin):
    list_display = ("contact", "user", "kind", "channel", "source", "ts")
    list_filter = ("kind", "channel", "source")
    search_fields = ("contact__name", "user__email", "note")
    date_hierarchy = "ts"


@admin.register(CaptureEvent)
class CaptureEventAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "provider",
        "provider_ref",
        "direction",
        "counterparty_email",
        "status",
        "received_at",
    )
    list_filter = ("provider", "direction", "status")
    search_fields = ("user__email", "provider_ref", "counterparty_email", "counterparty_name")


@admin.register(ChatDebrief)
class ChatDebriefAdmin(admin.ModelAdmin):
    list_display = (
        "contact", "user", "advocate_answer", "promoted", "dismissed",
        "intro_contact", "tracked_date", "created",
    )
    list_filter = ("advocate_answer", "promoted", "dismissed")
    search_fields = ("contact__name", "user__email", "learned", "intro_name")
    # The derived side-effect rows: readable here for support, never
    # editable — `crm.debrief.record` owns them and their idempotency
    # markers. Editing one by hand would let a second referral contact or
    # task be created on the next submit.
    readonly_fields = ("intro_contact", "intro_task", "date_task", "created")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "firm", "kind", "due", "status")
    list_filter = ("status", "kind")
    search_fields = ("title", "user__email", "firm__name", "source_key")
    autocomplete_fields = ("firm",)
