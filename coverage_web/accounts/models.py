"""The custom user model (docs/build-plan.md §2's `users` table, §3's Auth).

Replaces Django's default `auth.User` with email-as-identifier auth from
the start (`AUTH_USER_MODEL = "accounts.User"` in settings/base.py) —
per the task brief, this is the one decision that's painful to reverse
once real data exists, so it's made now, before any does.

Subclasses `AbstractUser` rather than building on `AbstractBaseUser` from
scratch: it keeps `is_staff` / `is_superuser` / `is_active` / `password` /
`last_login` / `groups` / `user_permissions` — everything `django.contrib.
admin`, `django-allauth`, and `createsuperuser` need — for free, and only
the username field is removed/replaced. `first_name` / `last_name` are
also inherited and left in place (harmless, and Google's login-only
profile scope already yields given/family name for allauth to populate
automatically) alongside the plan's own `name` field.
"""

from __future__ import annotations

import secrets

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models


# The five most-spoken languages (by total speakers) as an interface-language
# preference. NOT rendered anywhere today — the Settings control that wrote
# `User.language` was removed on 2026-07-30 because nothing read the value
# (see that field below). Kept here, ready, for the i18n pass that brings the
# control back alongside actual translations.
LANGUAGES = [
    ("en", "English"),
    ("zh", "中文 (Chinese)"),
    ("hi", "हिन्दी (Hindi)"),
    ("es", "Español (Spanish)"),
    ("fr", "Français (French)"),
]


# Work-authorization status, per region (see `User.work_authorization`). Two
# values only, because only one distinction actually changes the fit score:
# does this student need the firm to sponsor a visa in that region, or not.
# "citizen" covers every no-sponsorship-needed status (citizen, PR, existing
# right to work); anything absent is UNKNOWN, which is a legitimate answer and
# is scored as neutral rather than guessed either way.
WORK_AUTH_CITIZEN = "citizen"
WORK_AUTH_SPONSORSHIP = "sponsorship"
WORK_AUTH = [
    (WORK_AUTH_CITIZEN, "No sponsorship needed"),
    (WORK_AUTH_SPONSORSHIP, "Needs sponsorship"),
]


class UserManager(BaseUserManager):
    """`AbstractUser`'s default manager assumes a `username` field. This
    is the same manager shape Django's own docs recommend when swapping
    `USERNAME_FIELD` to email — `create_user`/`create_superuser` take
    `email` as the first positional arg so `createsuperuser` works
    unchanged."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("User.email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


def _generate_capture_slug() -> str:
    """A short, unguessable, URL-safe token — the `<slug>` half of the
    per-user inbound capture address `u-<slug>@in.coverage.app` (§5).
    Generated eagerly at user creation (see `User.save` below) so the
    column is never blank in practice, even though the capture pipeline
    itself is out of scope for this milestone (§4's "Build new"). §10
    requires capture slugs to be unguessable — `secrets.token_urlsafe`
    is the stdlib's CSPRNG-backed choice for exactly that."""
    return secrets.token_urlsafe(9)  # 12 base64url chars, ~72 bits of entropy


class User(AbstractUser):
    username = None
    email = models.EmailField("email address", unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    # --- docs/build-plan.md §2 `users` table, beyond AbstractUser's own ---
    google_sub = models.CharField(
        "Google subject ID", max_length=255, blank=True, default=""
    )
    name = models.CharField(max_length=255, blank=True, default="")
    # Settings' "Profile picture". Local disk storage (MEDIA_ROOT) is fine at
    # this project's current stage — local-only, pre-launch (see
    # docs/product-brief.md) — but does NOT survive a Render redeploy (its
    # filesystem is ephemeral): swap to S3-compatible storage before or at
    # the point this ships to real users.
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    school = models.CharField(max_length=255, blank=True, default="")
    class_year = models.PositiveSmallIntegerField(null=True, blank=True)
    target_cycle = models.CharField(max_length=32, blank=True, default="")
    regions = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    tracks = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    # Work authorization PER REGION, e.g. {"us": "citizen", "hk": "sponsorship"}
    # — keyed by the same region codes as `regions`, valued by WORK_AUTH.
    # Deliberately not one global boolean: a student can be free to work in one
    # of their target regions and need a visa in the other, and collapsing that
    # into a single flag is what forced the fit score to pass
    # `needs_sponsorship=None` for everyone. A region with no entry stays
    # UNKNOWN (the scorer treats unknown as neutral, never as a penalty).
    work_authorization = models.JSONField(default=dict, blank=True)
    # Per-user overrides for coverage_domain.cadence's rule parameters. Only
    # the keys in crm.views.TUNABLE_CADENCE_PARAMS are honored, and only inside
    # their documented ranges — this column is user-writable data, so the
    # whitelist lives server-side at the point of use, not here.
    cadence_params = models.JSONField(default=dict, blank=True)
    # Touches-per-week target for the Today pace ring. NULL means "use the
    # product default" (crm.views.WEEKLY_TOUCH_GOAL) rather than "no goal".
    weekly_touch_goal = models.PositiveSmallIntegerField(null=True, blank=True)
    # Preferred interface language (code from accounts.LANGUAGES). Stored, and
    # currently READ BY NOTHING: there is no LocaleMiddleware, no catalogs, and
    # no {% trans %} in any template. The Settings control was removed on
    # 2026-07-30 for exactly that reason (docs/specs/settings-page.md audit #3
    # — a setting that saves a value the engine ignores is the same defect as
    # the old target_cycle). The column stays because it is harmless and
    # already populated; the control comes back WITH the i18n pass, not before.
    language = models.CharField(max_length=8, default="en", blank=True)
    # IANA zone name ("Asia/Hong_Kong"). Blank means UNSET, and unset means
    # UTC — which is what the whole product ran on before this column existed.
    #
    # Why it matters: every "today" in the product is `timezone.localdate()`
    # (crm/views.py), so with TIME_ZONE="UTC" a Hong Kong student's cadence
    # queue, pace week, and follow-up windows all rolled over at 8 a.m. their
    # time, and Sunday-evening logging landed in the wrong week.
    # `accounts.middleware.TimezoneMiddleware` activates this per request, and
    # every localdate() call site becomes correct with no change of its own
    # (coverage_domain.cadence already takes the as-of date as a parameter).
    #
    # Never guessed from `regions`: a timezone silently moving someone's week
    # boundary on an inference is exactly the bug class Settings exists to
    # avoid. Unset stays UTC, and the field says so out loud.
    timezone = models.CharField(max_length=64, blank=True, default="")
    # Whether `timezone` is kept in step with the browser's own zone
    # (`Intl.DateTimeFormat().resolvedOptions().timeZone`, posted by the
    # snippet in base.html) or is a choice the user made and owns.
    #
    # This exists because the two cases genuinely differ. A student who flies
    # LA -> Hong Kong for a networking trip wants every "today" to follow
    # them without visiting Settings; a student who deliberately set a zone —
    # working US hours from abroad, say — would be furious to find the
    # product quietly overruling them on the next page load. So: auto is the
    # default for accounts that never chose, and picking any zone by hand
    # turns it off. Choosing "Detect automatically" turns it back on.
    timezone_auto = models.BooleanField(default=True)
    assets = models.JSONField(default=dict, blank=True)
    # Unique but nullable: multiple NULLs are fine in Postgres, and every
    # real user gets one assigned at creation time (see save() below), so
    # in practice this is never blank — nullable only leaves room for a
    # pre-slug-era row/import edge case rather than forcing a placeholder.
    capture_slug = models.SlugField(max_length=32, unique=True, null=True, blank=True)
    onboarded_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        if not self.capture_slug:
            self.capture_slug = _generate_capture_slug()
        super().save(*args, **kwargs)
