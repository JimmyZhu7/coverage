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

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models

from coverage_web.tenancy import PrivateModel


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
    # Which programme(s) the student is recruiting for RIGHT NOW — plural on
    # purpose: an underclassman routinely runs both a Spring Week/Insight
    # cycle and next year's SA cycle at once, and a single-select here forced
    # a choice the student wasn't actually making. Same ArrayField shape as
    # `regions`/`tracks` below, values are `directory.recommend.cycle_choices`
    # labels (e.g. "2028 Summer Internship").
    target_cycles = ArrayField(
        models.CharField(max_length=32), default=list, blank=True
    )
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
    # Read-only key for the ICS calendar feed. Its own token — the retired
    # BCC capture address used to be the reason this comment stressed "not
    # that slug"; now it's simply this feed's own secret. Leaking this one
    # leaks a read-only calendar, and regenerating it (drop the value, save)
    # revokes every stale subscription at once.
    calendar_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # Opts a user OUT of send_weekly_digest (crm/digest.py). Default False —
    # the digest already only sends when there's something real to report
    # (crm.digest's "nothing to report" rule), so the honest default is on,
    # not an empty checkbox nobody finds. Settings' Notifications card is the
    # one place this is set; the digest command's queryset excludes it.
    weekly_digest_opt_out = models.BooleanField(default=False)
    # Which plan this account is on. Today the ONLY thing that reads it is
    # the advisor page (assistant/plans.py: which model answers, how many
    # messages a day) — the pricing page's Pro list is otherwise still "in
    # the works". No billing sets this yet: there is no payment processor in
    # the codebase, so for now it is flipped by hand in admin (the founder's
    # own account for dogfooding, a beta tester's as a favour). When Stripe
    # arrives, its webhook writes this field and nothing downstream changes.
    PLAN_FREE = "free"
    PLAN_PRO = "pro"
    PLAN_CHOICES = [(PLAN_FREE, "Free"), (PLAN_PRO, "Pro")]
    plan = models.CharField(max_length=16, choices=PLAN_CHOICES, default=PLAN_FREE)
    onboarded_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        if not self.calendar_token:
            import secrets
            self.calendar_token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)


class PushSubscription(PrivateModel):
    """One row per browser/device a user has turned Web Push notifications
    on for (accounts/push.py, the Push API's own subscription object — see
    `PushManager.subscribe()` in MDN's docs). Private-zone, like every other
    per-user row (`coverage_web/tenancy.py`): a subscription is meaningless
    without the account it alerts.

    A user can hold several — a laptop and a phone are two independent
    subscriptions, deliberately not collapsed into one row on the user, so
    losing one (a browser profile wiped, a token expired) never touches the
    other. `send_deadline_push_alerts` sends to every active subscription a
    user has and deletes any single one the push service reports as gone
    (404/410 — see that command's docstring), never the whole account.

    `endpoint` is the actual uniqueness key, not `(user, endpoint)`: it names
    one specific browser's one specific registration with its push service,
    which can never legitimately belong to two rows at once — ours or
    another account's. A `TextField` rather than `URLField` because real
    endpoints (FCM's especially) routinely run past `URLField`'s 200-char
    default and Postgres has no meaningful length ceiling on a unique text
    column to worry about trading away.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    endpoint = models.TextField(unique=True)
    # The Push API subscription's `keys.p256dh` / `keys.auth` — the receiver
    # public key and auth secret AES128GCM payload encryption needs (RFC
    # 8291). Both arrive from the browser already base64url-encoded; stored
    # as-is, since `pywebpush.webpush()` expects exactly that string form.
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    # Free-text `navigator.userAgent`, for support/debugging only — never
    # parsed or relied on for anything the send path decides.
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "push_subscriptions"

    def __str__(self) -> str:
        return f"{self.user_id} · {self.endpoint[:40]}"
