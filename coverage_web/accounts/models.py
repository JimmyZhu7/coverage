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


# Study level — see `User.study_level`. Four named levels plus blank, which
# means NOT STATED and is a legitimate answer: never guessed from class year,
# because a 2028 class year is an undergraduate at one school and a master's
# candidate at another, and the whole point of the field is that nothing
# else on the row can tell those apart.
STUDY_UNDERGRAD = "undergrad"
STUDY_MASTERS = "masters"
STUDY_MBA = "mba"
STUDY_PHD = "phd"
STUDY_LEVEL_CHOICES = [
    ("", "Not stated"),
    (STUDY_UNDERGRAD, "Undergraduate"),
    (STUDY_MASTERS, "Master's"),
    (STUDY_MBA, "MBA"),
    (STUDY_PHD, "PhD"),
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
    # The student's OWN institutional email address(es) — the address they
    # email FROM at school, which is routinely NOT the address they signed up
    # with. Read by `capture.discovery._own_institution_domains`, and ONLY as
    # an exclusion: mail from the student's own institution is a campus
    # relationship (an RA, an advisor, the housing desk), not a networking
    # discovery. Verified on the founder's real mailbox (2026-08-22,
    # read-only): the two junk proposals no other gate could stop were
    # threaded personal replies from his own school's staff — and his account
    # email is freemail, so nothing in the product knew his school's domain.
    #
    # Why a stated fact and not a derived one: it is the student's answer,
    # visible and correctable in Settings, and it works with no mailbox
    # connected and no mail sent. Inferring the domain from `school` (a
    # display string) would need a name->domain table nobody maintains, and
    # inferring it from sent mail cannot run before the first scan — which is
    # exactly when the exclusion has to hold.
    #
    # PLURAL on purpose: a student can carry an undergrad address and a
    # graduate-program one, or keep a transferred school's account alive. A
    # single field would force a choice they are not making.
    #
    # Freemail is never excluded from here — a student who types their gmail
    # address in gets nothing, by `_FREEMAIL_DOMAINS`, and the form says so
    # rather than storing a value that would silently do nothing.
    school_emails = ArrayField(
        models.EmailField(max_length=254), default=list, blank=True
    )
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
    # Languages the student can WORK in, as lowercase names ("mandarin",
    # "french") — the vocabulary `directory.facts.extract_languages` emits
    # (title-cased there, lowercased here, compared case-blind), so a
    # posting's stated language and a student's stated language can meet.
    # Read by `directory.views._language_fit`, and only ever as a warning or
    # a match, never a wall. Two findings drive that: the gate is real (a
    # Barclays HK posting states "fluent in written Chinese and spoken
    # Mandarin if applying to the role in Hong Kong SAR"; practitioners put
    # Mandarin on about 95% of first-year HK IB desks) and it is mostly
    # UNSTATED (8 HK campus rows on the live board carry a Mandarin fact; the
    # rest are silent and no less gated). So it has to be a fact about the
    # student matched against the posting's own words, never inferred from
    # postings alone — and a posting naming a language the student lacks
    # cannot block, because the real gate lives in the rows that say nothing.
    # Formerly `assets["languages"]`, written by a cutover script, reachable
    # from no form and read by nothing; migration 0015 moved it here.
    languages = ArrayField(
        models.CharField(max_length=32), default=list, blank=True
    )
    # Undergraduate / master's / MBA / PhD, or blank for not stated. Exists
    # because nothing knew a sophomore was an undergraduate: PhD, MBA and
    # "Summer Associate" roles reached his picks and situation strip on class
    # year alone. Eligibility in UK/APAC postings is stated by year of study
    # ("penultimate year"), and "Class of 20XX" appears once in 177 postings,
    # so the year cannot carry this on its own. Formerly
    # `assets["current_status"]` ("rising sophomore"); migration 0015 mapped
    # the undergraduate class-standing words onto "undergrad" and left any
    # other wording where it was.
    study_level = models.CharField(
        max_length=16, choices=STUDY_LEVEL_CHOICES, blank=True, default=""
    )
    # Specific ties an outreach draft can open with — free text, a few
    # entries: a club, a prior employer, a hometown, a programme. Plural and
    # specific on purpose. A binary "alumni" flag is what a generic email is
    # built from, and the research puts a high-school-directory hook at 85%+
    # reply against ~25% for a bare college affiliation; the number-one draft
    # disqualifier is a generic email with no specific hook. Formerly
    # `assets["angles"]`; migration 0015 moved it here, and profile.csv now
    # exports it under this name.
    affiliations = ArrayField(
        models.CharField(max_length=160), default=list, blank=True
    )
    # Per-user overrides for coverage_domain.cadence's rule parameters. Only
    # the keys in crm.views.TUNABLE_CADENCE_PARAMS are honored, and only inside
    # their documented ranges — this column is user-writable data, so the
    # whitelist lives server-side at the point of use, not here.
    cadence_params = models.JSONField(default=dict, blank=True)
    # Touches-per-week target for the Today pace ring. NULL means "use the
    # product default" (crm.views.WEEKLY_TOUCH_GOAL) rather than "no goal".
    weekly_touch_goal = models.PositiveSmallIntegerField(null=True, blank=True)
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
    # turns it off. Choosing "This Device" turns it back on.
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
    # Pro trial (settings.PRO_TRIAL_DAYS / PRO_TRIAL_TRIGGER, accounts/
    # trials.py). `pro_trial_started_at` is set once, the first time
    # `accounts.trials.start_trial_if_eligible` fires for this account, and
    # stays set forever after — even once the trial ends and `plan` reverts
    # to free — because it alone is what "never start a second trial" reads:
    # a student who lets a first trial lapse must not get a second one just
    # by reconnecting Gmail again.
    pro_trial_started_at = models.DateTimeField(null=True, blank=True)
    # When this account's Pro trial ends. The daily `pro_trial_expire`
    # management command reverts `plan` back to "free" once this passes —
    # see that command and accounts/trials.py::trial_days_left. Stays set
    # (never cleared) after expiry so Settings' Credits and Gmail Live cards
    # can still say "your trial ended" honestly rather than looking like
    # there was never one. A Pro account with this left null was never on a
    # trial at all (admin-granted, e.g. the founder's own account) — the
    # expiry command's own selection query relies on exactly that
    # distinction to leave such an account alone.
    pro_trial_ends_at = models.DateTimeField(null=True, blank=True)
    # When the student dismissed the "your Pro trial ended" banner on
    # Settings (accounts/trials.py::trial_ended_notice, the sentence the two
    # comments above promised and nothing rendered until now). Stored on the
    # account rather than in the session or localStorage: what is being
    # acknowledged is a change to what this account can do, and it should
    # not come back on the laptop after it was closed on the phone. Null on
    # every account that never had a trial, and on one whose banner is still
    # standing.
    pro_trial_notice_dismissed_at = models.DateTimeField(null=True, blank=True)
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
