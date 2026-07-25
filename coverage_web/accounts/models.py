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
# preference. Stored per user now; actual UI translation is a later i18n pass.
LANGUAGES = [
    ("en", "English"),
    ("zh", "中文 (Chinese)"),
    ("hi", "हिन्दी (Hindi)"),
    ("es", "Español (Spanish)"),
    ("fr", "Français (French)"),
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
    # Preferred interface language (code from accounts.LANGUAGES). Stored now;
    # actual UI translation is a later i18n pass.
    language = models.CharField(max_length=8, default="en", blank=True)
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
