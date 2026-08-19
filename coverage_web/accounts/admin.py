from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Founder-facing CRUD for the one model that isn't itself
    tenant-scoped (it *is* the tenant) — see docs/build-plan.md §2.
    Subclasses Django's own `UserAdmin` for the well-tested
    password-change / permissions widgets, replacing every fieldset
    reference to the now-gone `username` field.
    """

    ordering = ("email",)
    list_display = (
        "email",
        "name",
        "school",
        "class_year",
        "target_cycles",
        "plan",
        "is_staff",
        "is_active",
        "onboarded_at",
        "created",
    )
    # `target_cycles` (an ArrayField, like regions/tracks below) dropped from
    # list_filter: Django's stock filter needs discrete Field choices, not a
    # Postgres array — the same reason regions/tracks were never in here.
    list_filter = ("plan", "is_staff", "is_active", "is_superuser", "school")
    search_fields = ("email", "name", "school")
    readonly_fields = ("created", "last_login", "date_joined")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Profile",
            {
                "fields": (
                    "name",
                    "first_name",
                    "last_name",
                    "school",
                    "class_year",
                    "target_cycles",
                    "regions",
                    "tracks",
                    "assets",
                )
            },
        ),
        (
            # These three feed the domain engines directly (fit-score
            # sponsorship, cadence windows, the Today pace ring) and have no
            # settings-page UI yet, so admin is the only place to set them.
            "Recruiting preferences",
            {
                "fields": (
                    "work_authorization",
                    "cadence_params",
                    "weekly_touch_goal",
                ),
                "description": (
                    "work_authorization is keyed by region, e.g. "
                    '{"us": "citizen", "hk": "sponsorship"}. cadence_params '
                    "only honors the keys in crm.views.TUNABLE_CADENCE_PARAMS; "
                    "anything else is ignored at read time."
                ),
            },
        ),
        (
            # No billing writes this yet (no payment processor in the
            # codebase) — admin IS the billing system for now. The advisor
            # page (assistant/plans.py) and the credit ledger
            # (billing/credits.py, billing/admin.py — grant or adjust a
            # student's balance there) both read it.
            "Plan",
            {
                "fields": ("plan",),
                "description": (
                    "Free: Haiku, 60 credits/month (60 messages, or mix with rescans). "
                    "Pro: Sonnet, 180 credits/month (60 messages at 3 credits each). "
                    "Set by hand until Stripe exists — see the Credit Ledger admin to "
                    "grant or adjust a student's balance directly."
                ),
            },
        ),
        ("Account", {"fields": ("google_sub", "onboarded_at")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined", "created", "deleted_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )
