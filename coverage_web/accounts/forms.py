"""Forms for the onboarding wizard and account settings (task M5).

Deliberately plain `forms.Form` subclasses rather than a `ModelForm` on
`accounts.User`: two of the edited fields (`regions`, `tracks`) are
Postgres `ArrayField`s (see accounts/models.py), which a stock `ModelForm`
does not render as multi-select checkboxes. A hand-rolled form keeps the
array handling explicit and lets the templates control the (restrained)
markup, while still giving Django's validation for the scalar fields.

The region/track vocabularies are the ones actually present in the seeded
`directory.Firm` rows (`{hk, us}` and `{am, consulting, corp-strat, ib,
pe, st}`) — kept here as the single source of truth so the onboarding
firm-picker filters and the profile checkboxes never drift apart.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from functools import lru_cache
from zoneinfo import available_timezones

from django import forms
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, ImageOps

from coverage_domain.cadence import CADENCE_DEFAULTS
from directory.classify import (
    REGION_LABELS, TRACKED_REGIONS, TRACK_LABELS, TRACKED_TRACKS,
)
from directory.recommend import cycle_choices

# Same rule for both of these as for the ranges below: import the value from
# whatever module actually READS it, never restate it. `crm.coverage` is what
# falls back to DEFAULT_ADVOCATE_TARGET when the key is missing, so it owns
# the number the placeholder shows.
from crm.coverage import DEFAULT_ADVOCATE_TARGET

# The cadence whitelist + its valid ranges live at the point of use, in
# crm.views — that module is what actually hands the overrides to
# `cadence.due_actions`, so it owns the definition of "safe to honor". This
# form imports it rather than restating the ranges: a copy here would be a
# second source of truth that drifts silently, and the failure mode of drift
# is a settings page that happily saves a value the engine then ignores.
from crm.views import TUNABLE_CADENCE_PARAMS

from .models import WORK_AUTH

# Human labels for the region tokens a student can state a preference for.
# Sourced from `classify.TRACKED_REGIONS`/`REGION_LABELS` — the SAME six-market
# vocabulary the Opportunities feed's own Region filter uses — rather than a
# second, narrower hk/us-only list. That narrower list used to be sourced
# from `Firm.regions`, which really is hk/us-only in the seed data, but
# `Opportunity.region` (what a role's own location resolves to) is six wide:
# eu (204 open campus roles), sg (74), cn (30), jp (16) were all markets a
# student could not state a preference — or a work-authorization answer,
# since `WorkAuthorizationForm` below also iterates this list — for.
REGION_CHOICES: list[tuple[str, str]] = [
    (code, REGION_LABELS[code]) for code in TRACKED_REGIONS
]

# Sourced from classify.TRACKED_TRACKS/TRACK_LABELS — the SAME vocabulary
# directory/views.py's Opportunities track filter reads — rather than a
# second, independently-hardcoded copy. The two used to drift: this list
# called "pe" "Private Equity" while the filter called it "Private Equity /
# Credit" (the firms actually tagged "pe" include credit shops, so that is
# the accurate label a student sees both places now).
TRACK_CHOICES: list[tuple[str, str]] = [
    (code, TRACK_LABELS[code]) for code in TRACKED_TRACKS
]

# Class year (graduation year) options, anchored to the current year so the
# list always spans last year's grads through six years out — undergrad
# through most master's timelines.
_YEAR = date.today().year
CLASS_YEAR_CHOICES: list[tuple[str, str]] = [("", "Select graduation year")] + [
    (str(y), str(y)) for y in range(_YEAR - 1, _YEAR + 7)
]

# Recruiting-cycle options. `cycle_choices()` (directory.recommend) is the
# one place this vocabulary is built — this module-level binding is kept only
# for the couple of call sites that still import CYCLE_CHOICES/
# CYCLE_SUGGESTIONS directly (accounts.views' onboarding context, currently
# unused by any template). `ProfileForm.target_cycle` itself does NOT read
# this binding: see `ProfileForm.__init__`, which recomputes choices per
# instance so a long-lived worker process never serves a stale year — the
# same staleness this module-level constant is still subject to.
CYCLE_CHOICES: list[tuple[str, str]] = cycle_choices()

# Kept for backwards compatibility with any view still passing suggestions.
CYCLE_SUGGESTIONS: list[str] = [c for c, _ in CYCLE_CHOICES if c]

# Avatar limits. 8 MB is comfortably above any phone photo while still bounding
# what reaches the decoder; 512px is 8x the largest place the avatar renders
# (64px on this page, 22px in the nav), which keeps it crisp on a 2x display
# with room to spare and no reason to store more.
MAX_AVATAR_BYTES = 8 * 1024 * 1024
AVATAR_PX = 512

# Timezone shortlist — the zones this product's stated audience actually lives
# in (HK and US students, per docs/product-brief.md), plus the three other
# markets the six-market region vocabulary already covers. They ride above the
# fold of the select so the common case is one scroll-free click; the full
# `zoneinfo` list follows in a second optgroup, so nobody is locked out.
TIMEZONE_SHORTLIST: list[tuple[str, str]] = [
    ("Asia/Hong_Kong", "Hong Kong (HKT)"),
    ("America/New_York", "US Eastern (New York)"),
    ("America/Chicago", "US Central (Chicago)"),
    ("America/Denver", "US Mountain (Denver)"),
    ("America/Los_Angeles", "US Pacific (Los Angeles)"),
    ("Europe/London", "London (UK)"),
    ("Asia/Singapore", "Singapore"),
    ("Asia/Shanghai", "Mainland China (Shanghai)"),
    ("Asia/Tokyo", "Tokyo"),
]


@lru_cache(maxsize=1)
def known_timezones() -> frozenset[str]:
    """Every IANA zone the host's tzdata carries. Cached: `available_timezones`
    walks the tzdata tree on each call, and this is hit on every settings
    render AND every validation — the answer only changes when the OS package
    does, which is never within a process's life."""
    return frozenset(available_timezones())


# The <option> value meaning "follow this device". Not a real IANA name and
# deliberately unlikely to collide with one, since it travels through the same
# select as ~600 genuine zone names.
AUTO_TIMEZONE = "__auto__"


@lru_cache(maxsize=1)
def timezone_choices() -> list:
    """Grouped choices for the Profile timezone select.

    Built from `zoneinfo.available_timezones()` — the host's own tzdata — so
    the validator and the widget can never disagree about what a valid zone
    is. Cached because that call walks the tzdata tree and the answer only
    changes when the OS package does.

    The leading option is AUTO, not blank. It is the default for every new
    account, and it is worded as what actually happens rather than as an
    absence — the browser reports its own IANA zone and Coverage follows it,
    so "not set" stopped being the honest label for the first entry the day
    auto-detection landed. Picking any explicit zone below turns following
    off; see `User.timezone_auto`.
    """
    every = sorted(known_timezones())
    return [
        (AUTO_TIMEZONE, "Detect automatically from this device"),
        ("Common", [(code, label) for code, label in TIMEZONE_SHORTLIST]),
        (
            "All timezones",
            # The shortlist entries are NOT filtered out of this group: a
            # student who knows they want "Asia/Hong_Kong" and types H-o-n
            # into an open select should find it where the alphabet says it
            # is. A duplicated <option> value selects identically either way.
            [(code, code.replace("_", " ")) for code in every],
        ),
    ]


class _StaleValueSelect(forms.Select):
    """A <select> where exactly one option — the student's own stored value,
    once it rolls off the current `cycle_choices()` window — renders
    `disabled`.

    Plain `choices` tuples have no way to mark a single option disabled, and
    the alternative (a stored-but-unlisted value just renders as nothing
    selected) is the silent-clear bug this widget exists to avoid: the
    student would see a blank "Select a cycle" with no sign their answer was
    ever recorded, and the next save would erase it for good."""

    def __init__(self, *args, disabled_value: str = "", **kwargs):
        self._disabled_value = disabled_value
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if self._disabled_value and str(value) == self._disabled_value:
            option["attrs"]["disabled"] = True
        return option


class ProfileForm(forms.Form):
    """Step 1 of onboarding, and the editable core of /welcome/settings/."""

    # Display name shown in the top nav and anywhere else a person's name
    # reads better than their login — NOT the auth identifier, which stays
    # the immutable email (USERNAME_FIELD, accounts/models.py). Optional:
    # falls back to the email locally in templates when blank.
    name = forms.CharField(max_length=255, required=False, strip=True)
    avatar = forms.ImageField(
        required=False,
        # accept= is a hint to the file picker, not a security control — the
        # real check is clean_avatar below, which decodes the file rather than
        # trusting its name or its Content-Type header.
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
    )
    # Onboarding's step-1 render of this form has no existing avatar to
    # clear, so this checkbox only really does anything on the settings
    # page — Django's ClearableFileInput would add its own "Clear" checkbox
    # automatically if the field were bound to an initial file, but this is
    # a plain Form (not a ModelForm), so there is no such initial value for
    # the widget to know about; this field does that job explicitly instead.
    remove_avatar = forms.BooleanField(required=False)
    school = forms.CharField(max_length=255, required=False, strip=True)
    class_year = forms.TypedChoiceField(
        choices=CLASS_YEAR_CHOICES,
        coerce=int,
        empty_value=None,
        required=False,
        widget=forms.Select,
    )
    target_cycle = forms.ChoiceField(
        choices=CYCLE_CHOICES,
        required=False,
        widget=forms.Select,
    )
    regions = forms.MultipleChoiceField(
        choices=REGION_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    tracks = forms.MultipleChoiceField(
        choices=TRACK_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    # Lives in Profile rather than a section of its own: it is a fact about the
    # student, in the same breath as school and target regions. Choices are set
    # per instance in __init__ (see timezone_choices' cache note).
    timezone = forms.ChoiceField(required=False, widget=forms.Select)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timezone"].choices = timezone_choices()
        # `target_cycle`'s choices are recomputed HERE, per instance, rather
        # than left at the class-level `CYCLE_CHOICES` binding above: that
        # binding is a module-level constant frozen at import (`_YEAR =
        # date.today().year`, evaluated once), so a long-lived worker process
        # would keep serving whatever year it started in.
        choices = cycle_choices()
        # A stored value the current choices no longer list — e.g. a past
        # year that rolled off the window, or the live `demo@coverage.local`
        # row's `target_cycle='sa2028_ib'`, which matches nothing this
        # dropdown has ever offered — must not vanish without a trace.
        # Django's Select renders an unlisted value as nothing selected, so
        # the student would see a blank "Select a cycle" and the next save
        # would silently clear the field for good. Appending the value back
        # in, disabled, keeps the loss visible instead.
        current = (self.initial.get("target_cycle") or "").strip()
        known_values = {value for value, _ in choices}
        stale = current if current and current not in known_values else ""
        if stale:
            choices = choices + [(stale, f"{stale} (no longer offered)")]
            self.fields["target_cycle"].widget = _StaleValueSelect(disabled_value=stale)
        self.fields["target_cycle"].choices = choices

    def clean_avatar(self):
        """Validate and NORMALISE the upload: never store what was handed to us.

        Four things happen here, and each exists for a concrete reason:

        * **Size ceiling before decode.** A modern phone photo is 3-8 MB and a
          deliberately-crafted one can be far larger; refusing early means we
          never hand an unbounded buffer to the decoder.
        * **EXIF orientation is applied, then dropped.** Phone cameras store
          the sensor's raw pixels plus an "actually, rotate this" tag. Browsers
          honour it in an `<img>`; `<canvas>`, thumbnails, and most downstream
          tools do not — so an unrotated portrait shows sideways in half the
          places it appears. `exif_transpose` bakes the rotation into the
          pixels. Dropping the rest of the block is a privacy decision, not a
          size one: EXIF routinely carries GPS coordinates, and a student's
          home location has no business riding along with their profile photo.
        * **Centre-cropped square, capped at 512px.** Every surface renders
          this in a circle (22px in the nav, 64px here), so a non-square image
          would be squashed or arbitrarily cropped by CSS at each size. Doing
          it once, server-side, means every surface gets the same face.
        * **Re-encoded.** The bytes we store are ones Pillow wrote, not ones a
          stranger uploaded — which also strips anything hiding in the
          original container.
        """
        upload = self.cleaned_data.get("avatar")
        if not upload:
            return upload

        if upload.size > MAX_AVATAR_BYTES:
            raise forms.ValidationError(
                f"That image is {upload.size / 1_048_576:.1f} MB. "
                f"Please use one under {MAX_AVATAR_BYTES // 1_048_576} MB."
            )

        # ImageField already confirmed this decodes; reopen for real work.
        try:
            img = Image.open(upload)
            img = ImageOps.exif_transpose(img)
        except Exception:
            raise forms.ValidationError(
                "That file didn't read as an image. Try a JPEG or PNG."
            ) from None

        has_alpha = img.mode in ("RGBA", "LA", "P")
        img = img.convert("RGBA" if has_alpha else "RGB")
        # Centre-crop to a square, then downscale. `ImageOps.fit` does both in
        # one resample, so the result stays sharp rather than being resized
        # twice.
        img = ImageOps.fit(img, (AVATAR_PX, AVATAR_PX), Image.LANCZOS, centering=(0.5, 0.5))

        buf = io.BytesIO()
        if has_alpha:
            img.save(buf, format="PNG", optimize=True)
            ext, content_type = "png", "image/png"
        else:
            img.save(buf, format="JPEG", quality=88, optimize=True, progressive=True)
            ext, content_type = "jpg", "image/jpeg"
        buf.seek(0)

        # Random basename, not the user's: an uploaded filename is attacker-
        # controlled text that would otherwise end up in a public URL, and two
        # users uploading "profile.jpg" should not collide or be able to probe
        # for each other's files.
        name = f"{uuid.uuid4().hex}.{ext}"
        return InMemoryUploadedFile(
            buf, "avatar", name, content_type, buf.getbuffer().nbytes, None
        )

    @classmethod
    def from_user(cls, user) -> "ProfileForm":
        """Bind the form to a user's current values for a GET render."""
        return cls(
            initial={
                "name": user.name,
                "school": user.school,
                "class_year": user.class_year,
                "target_cycle": user.target_cycle,
                "regions": list(user.regions or []),
                "tracks": list(user.tracks or []),
                # A stored zone the host's tzdata no longer carries would
                # render as nothing selected — the same silent-clear the
                # `target_cycle` stale-value widget exists to prevent. Here the
                # fix is simpler because the vocabulary is huge and stable:
                # show it only if it is still real, and let the honest "Not
                # set" option speak for the rest.
                # Auto shows as auto, whatever zone the browser last filled
                # in — the stored value is a consequence of the setting, not
                # the setting itself, and showing "Asia/Hong_Kong" selected
                # would invite the user to "confirm" it and silently turn
                # following off.
                "timezone": (
                    AUTO_TIMEZONE if user.timezone_auto
                    else (user.timezone if user.timezone in known_timezones() else "")
                ),
            }
        )

    def clean_timezone(self) -> str:
        """AUTO and blank both pass through untouched; anything else must name
        a zone `zoneinfo` actually knows, so the middleware's read can never be
        handed a string it cannot construct."""
        value = (self.cleaned_data.get("timezone") or "").strip()
        if value == AUTO_TIMEZONE:
            return value
        if value and value not in known_timezones():
            raise forms.ValidationError(
                "That isn't a timezone we recognise. Pick one from the list."
            )
        return value

    def apply_to(self, user) -> None:
        """Persist validated values back onto the user row. Call only after
        `is_valid()`."""
        cd = self.cleaned_data
        update_fields = ["name", "school", "class_year", "target_cycle",
                         "regions", "tracks", "timezone", "timezone_auto"]
        user.name = cd["name"]
        user.school = cd["school"]
        user.class_year = cd["class_year"]
        user.target_cycle = cd["target_cycle"]
        user.regions = cd["regions"]
        user.tracks = cd["tracks"]
        # AUTO turns following back on and leaves `timezone` alone: whatever
        # the browser last reported stays correct until the next page load
        # says otherwise, and clearing it here would give the user a UTC day
        # for the rest of this request for no reason.
        #
        # Any explicit pick turns following OFF — that is the whole contract
        # of the flag, and it is what stops the next page load overruling a
        # deliberate choice. Blank still means UNSET → UTC, stored as "" so
        # "chose UTC" and "never answered" stay distinguishable.
        if cd["timezone"] == AUTO_TIMEZONE:
            user.timezone_auto = True
        else:
            user.timezone_auto = False
            user.timezone = cd["timezone"]
        # remove_avatar wins over a simultaneous upload — the widget can't
        # produce both in one real submission, but a wrong-order check here
        # would silently discard whichever loses, so ties go to the more
        # deliberate, explicitly-checked action rather than to whichever
        # `if` happened to run second.
        if cd["remove_avatar"]:
            if user.avatar:
                user.avatar.delete(save=False)
            user.avatar = None
            update_fields.append("avatar")
        elif cd["avatar"]:
            user.avatar = cd["avatar"]
            update_fields.append("avatar")
        user.save(update_fields=update_fields)


# ---------------------------------------------------------------------------
# Independently-saving settings sections
# ---------------------------------------------------------------------------
# Each section of /welcome/settings/ POSTs on its own, following the pattern
# the Language form established (its own <form>, distinguished server-side by
# a field only it submits). Language sniffed for the *field name* itself;
# with four more sections that heuristic stops scaling — two sections could
# plausibly share a field name — so these carry an explicit `section=<name>`
# hidden input instead. The view dispatches on it (accounts/views.py
# SECTION_FORMS) and re-renders only the failing section with its errors.
#
# They share this base so the view can treat them uniformly: build with
# `from_user`, validate, `apply_to`, flash `success_message`.
class SectionForm(forms.Form):
    """A settings section that saves on its own POST."""

    # Value of the hidden `section` input, and the key in views.SECTION_FORMS.
    section = ""
    success_message = "Saved."

    @classmethod
    def from_user(cls, user) -> "SectionForm":
        """Unbound form carrying the user's current values (GET render)."""
        return cls(initial=cls.initial_for(user))

    @classmethod
    def initial_for(cls, user) -> dict:
        raise NotImplementedError

    def apply_to(self, user) -> None:
        """Persist validated values. Call only after `is_valid()`."""
        raise NotImplementedError


class WorkAuthorizationForm(SectionForm):
    """`User.work_authorization` — one select per region in REGION_CHOICES.

    Blank ("Not specified") stores NOTHING for that region. That is the whole
    point: `scoring.needs_sponsorship` treats a missing entry as unknown and
    scores it neutral, whereas any guessed default would move every firm's
    structural score on a fact the user never gave us.
    """

    section = "work_auth"
    success_message = "Work authorization saved."

    # Field name prefix, so the per-region fields can't collide with anything
    # else posted in this section.
    FIELD_PREFIX = "work_auth_"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for code, label in REGION_CHOICES:
            # required=False alone would let an unknown value through as ""
            # — ChoiceField still validates membership, so a hand-crafted
            # POST of e.g. "green-card" is rejected rather than stored.
            name = f"{self.FIELD_PREFIX}{code}"
            self.fields[name] = forms.ChoiceField(
                label=label,
                # Blank LAST, not first: the widget is a three-column radio
                # matrix now (see accounts/_work_auth_matrix.html), and the
                # columns read best as answer / answer / abstain. The blank
                # is still a real, selectable state — an initial of "" checks
                # it, so "Unspecified" renders as a deliberate choice rather
                # than an absence.
                choices=list(WORK_AUTH) + [("", "Not specified")],
                required=False,
                # Radios, not a select: three states per region is a visible
                # 6x3 matrix, and one column caption can label a state ONCE
                # for every region instead of each closed dropdown repeating
                # the whole vocabulary. Native radios keep the keyboard and
                # screen-reader behaviour for free; the template's radiogroup
                # wrapper carries the accessible description.
                widget=forms.RadioSelect,
            )

    @classmethod
    def initial_for(cls, user) -> dict:
        auth = user.work_authorization or {}
        return {
            f"{cls.FIELD_PREFIX}{code}": auth.get(code, "")
            for code, _ in REGION_CHOICES
        }

    @property
    def rows(self) -> list:
        """Bound fields in REGION_CHOICES order, for the template."""
        return [self[f"{self.FIELD_PREFIX}{code}"] for code, _ in REGION_CHOICES]

    def apply_to(self, user) -> None:
        # Keys for regions this form doesn't render (a region added to the
        # firm directory later, or one set in the admin) are left alone —
        # same reasoning as the assets dict: own your own keys only.
        auth = dict(user.work_authorization or {})
        for code, _ in REGION_CHOICES:
            value = self.cleaned_data.get(f"{self.FIELD_PREFIX}{code}") or ""
            if value:
                auth[code] = value
            else:
                auth.pop(code, None)
        user.work_authorization = auth
        user.save(update_fields=["work_authorization"])


# Presentation for the tunable cadence knobs: label, unit, and the one-line
# "what does this actually change" description. Keyed by the same names as
# crm.views.TUNABLE_CADENCE_PARAMS, which stays the authority on WHICH keys
# exist and what range each accepts — this map only dresses them.
CADENCE_LABELS: dict[str, tuple[str, str, str]] = {
    "followup_after_business_days": (
        "First Follow-Up",
        "business days",
        "How long a cold contact sits without a reply before Coverage asks "
        "you to follow up.",
    ),
    "park_after_business_days": (
        "Park After",
        "business days",
        "Silence after your last touch before the contact is parked and stops "
        "surfacing.",
    ),
    "max_cold_touches": (
        "Max Cold Touches",
        "touches",
        "How many times you'll reach out to someone who's never replied, "
        "before Coverage parks them. Capped at 2 — one note, one "
        "follow-up, never a second.",
    ),
    "advocate_touch_min_weeks": (
        "Advocate Check-In",
        "weeks",
        "How often your advocates get a keep-warm touch.",
    ),
    # Paired with crm.views.TUNABLE_CADENCE_PARAMS — this dict is looked up by
    # key with no fallback, so the two must be added and removed together.
    "chatted_touch_min_weeks": (
        "Keep-Warm Check-In",
        "weeks",
        "How long after a coffee chat before Coverage reminds you to circle "
        "back, for people you've met who aren't advocates yet.",
    ),
    "pre_deadline_reping_days": (
        "Pre-Deadline Re-Ping",
        "days",
        "How far ahead of a confirmed deadline warm contacts get re-pinged.",
    ),
}


class CadenceForm(SectionForm):
    """`User.cadence_params` — per-key overrides of coverage_domain's
    `CADENCE_DEFAULTS`, restricted to crm.views.TUNABLE_CADENCE_PARAMS.

    Clearing an input REMOVES the override (falls back to the default) rather
    than storing a zero: `_cadence_params` would drop a 0 as out-of-range
    anyway, so storing one would leave the settings page showing a value the
    engine silently ignores.
    """

    section = "cadence"
    success_message = "Cadence updated."

    # `User.assets["advocate_target"]` — the advocates-per-firm yardstick
    # `crm.coverage.advocate_target()` already reads and `crm.views` already
    # feeds into the gap ladder and the network axis of the firm fit score. It
    # was a live engine parameter with no control anywhere: the founder's row
    # carries the key (ported at cutover) and no UI could change it.
    #
    # Range: the read side rejects anything below 1 (a target of 0 makes every
    # firm permanently "covered"), so 1 is the floor. 5 is the ceiling because
    # above it the gap ladder's top rung is unreachable for any real student —
    # nobody lands six advocates at one firm — and a target you can never meet
    # renders every firm permanently short.
    ADVOCATE_TARGET_RANGE = (1, 5)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        low, high = self.ADVOCATE_TARGET_RANGE
        self.fields["advocate_target"] = forms.IntegerField(
            label="Advocate Target",
            required=False,
            min_value=low,
            max_value=high,
            widget=forms.NumberInput(
                attrs={"min": low, "max": high, "step": 1,
                       "placeholder": DEFAULT_ADVOCATE_TARGET,
                       "aria-describedby":
                           "id_advocate_target-desc id_advocate_target-err"},
            ),
            error_messages={
                "min_value": f"Advocate Target must be between {low} and {high} advocates.",
                "max_value": f"Advocate Target must be between {low} and {high} advocates.",
                "invalid": "Advocate Target must be a whole number of advocates.",
            },
        )
        for key, (low, high) in TUNABLE_CADENCE_PARAMS.items():
            label, unit, _desc = CADENCE_LABELS[key]
            # min_value/max_value mirror the server-side whitelist exactly, so
            # an out-of-range value is a form error the user sees, never a
            # saved-then-ignored number.
            self.fields[key] = forms.IntegerField(
                label=label,
                required=False,
                min_value=low,
                max_value=high,
                widget=forms.NumberInput(
                    attrs={"min": low, "max": high, "step": 1,
                           "placeholder": CADENCE_DEFAULTS[key],
                           "aria-describedby": f"id_{key}-desc id_{key}-err"}
                ),
                error_messages={
                    "min_value": f"{label} must be between {low} and {high} {unit}.",
                    "max_value": f"{label} must be between {low} and {high} {unit}.",
                    "invalid": f"{label} must be a whole number of {unit}.",
                },
            )

    @classmethod
    def initial_for(cls, user) -> dict:
        stored = user.cadence_params or {}
        initial = {}
        for key, (low, high) in TUNABLE_CADENCE_PARAMS.items():
            value = stored.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                initial[key] = value
            # An out-of-range stored value (e.g. a max_cold_touches of 3 from
            # before that range was tightened to (1, 2)) is dropped here for
            # the same reason crm.views._cadence_params drops it on the read
            # side: showing it pre-filled in a field whose own widget caps at
            # `high` would render a number the input itself rejects. Since
            # `apply_to` below treats every unposted field as "clear the
            # override" (matching this form's own clearing-an-input
            # contract), the next time the Cadence section ITSELF is saved,
            # even untouched, also erases the stale value from the column:
            # the same value the engine was already ignoring, now no longer
            # left behind to confuse a human reading Settings or the raw
            # column.
        # Same drop-what-the-engine-would-drop rule for the advocate target:
        # `crm.coverage.advocate_target` falls back on a bool, a non-int, or
        # anything below 1, so a stored 0 must not render as if honoured.
        low, high = cls.ADVOCATE_TARGET_RANGE
        stored_target = (user.assets or {}).get("advocate_target")
        if (
            isinstance(stored_target, int)
            and not isinstance(stored_target, bool)
            and low <= stored_target <= high
        ):
            initial["advocate_target"] = stored_target
        return initial

    @property
    def rows(self) -> list[dict]:
        """One row per tunable knob, carrying the default to show inline."""
        rows = []
        for key in TUNABLE_CADENCE_PARAMS:
            _label, unit, desc = CADENCE_LABELS[key]
            rows.append({
                "field": self[key],
                "unit": unit,
                "description": desc,
                "default": CADENCE_DEFAULTS[key],
            })
        # Sits with the other engine knobs rather than in a card of its own —
        # it is the same class of thing (a number that changes what Coverage
        # asks of you), and it saves on the same section POST.
        rows.append({
            "field": self["advocate_target"],
            "unit": "advocates",
            "description": (
                "Advocates at a firm before Coverage calls it covered. Feeds "
                "the gap ladder on Network and your firm fit score."
            ),
            "default": DEFAULT_ADVOCATE_TARGET,
        })
        return rows

    def apply_to(self, user) -> None:
        # Non-tunable keys (defaults an admin pinned by hand) survive: this
        # form owns the whitelisted keys and nothing else.
        params = dict(user.cadence_params or {})
        for key in TUNABLE_CADENCE_PARAMS:
            value = self.cleaned_data.get(key)
            if value is None:
                params.pop(key, None)
            else:
                params[key] = int(value)
        user.cadence_params = params
        # `advocate_target` lives in `assets`, not `cadence_params`, because
        # that is the key `crm.coverage.advocate_target()` already reads —
        # moving it would be a migration for no gain. Copy-then-set, owning
        # exactly this one key — copy-then-set, never a fresh dict: blank
        # REMOVES it so the product default applies, rather than storing a
        # number that merely happens to equal the default.
        target = self.cleaned_data.get("advocate_target")
        assets = dict(user.assets or {})
        if target is None:
            assets.pop("advocate_target", None)
        else:
            assets["advocate_target"] = int(target)
        user.assets = assets
        user.save(update_fields=["cadence_params", "assets"])


class WeeklyPaceForm(SectionForm):
    """`User.weekly_touch_goal` — the Today pace ring's target."""

    section = "pace"
    success_message = "Weekly pace saved."

    # crm.views.WEEKLY_TOUCH_GOAL, restated here as the placeholder/hint only.
    # Imported rather than hardcoded would be tidier, but the number is a
    # display string in two templates already; the view passes it in context.
    DEFAULT_GOAL = 10
    # Floor of 1, not 0: crm.views reads the goal with `or`, so a stored 0
    # behaves exactly like NULL. Rejecting it with a message beats saving a
    # number that quietly does nothing. Ceiling is a sanity bound well above
    # any real week (the column itself would take 32767).
    MAX_GOAL = 200

    weekly_touch_goal = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_GOAL,
        widget=forms.NumberInput(
            attrs={"min": 1, "max": MAX_GOAL, "step": 1, "placeholder": DEFAULT_GOAL,
                   "aria-describedby":
                       "id_weekly_touch_goal-desc id_weekly_touch_goal-err"}
        ),
        error_messages={
            "min_value": f"Pick a goal between 1 and {MAX_GOAL} touches a week.",
            "max_value": f"Pick a goal between 1 and {MAX_GOAL} touches a week.",
            "invalid": "Your weekly goal must be a whole number of touches.",
        },
    )

    @classmethod
    def initial_for(cls, user) -> dict:
        return {"weekly_touch_goal": user.weekly_touch_goal}

    def apply_to(self, user) -> None:
        # Blank -> NULL, which crm.views reads as "use the product default"
        # rather than "no goal".
        user.weekly_touch_goal = self.cleaned_data.get("weekly_touch_goal")
        user.save(update_fields=["weekly_touch_goal"])


class NotificationsForm(SectionForm):
    """`User.weekly_digest_opt_out` — the one settings-writable field the
    Notifications card owns. Push Alerts' own on/off state lives in
    `accounts.PushSubscription` rows, not a User column, and is set via its
    own JS/endpoint pair (accounts/push.py) rather than this form — the two
    controls share a card because they answer the same question ("how do you
    want to hear about a closing deadline"), not because they share a save
    path.

    The form field is named and valued the OPPOSITE of the column it writes,
    on purpose: a checkbox next to "Weekly Email Digest" has to mean "send
    it" when checked, or a student reads a checked box under a feature name
    as that feature being ON and gets the opposite of what they asked for.
    The column is named `_opt_out` (matching send_weekly_digest's own
    docstring and query), so this form is the one place that translation
    happens — `weekly_digest_enabled` in, negated on the way to `apply_to`."""

    section = "notifications"
    success_message = "Notification preferences saved."

    # required=False, initial=True: an unchecked BooleanField posts nothing
    # at all, so a first-time GET render must supply the default explicitly
    # rather than relying on the field's own `initial` (which only applies
    # to an unbound form with no `initial_for` override).
    weekly_digest_enabled = forms.BooleanField(required=False, initial=True)

    @classmethod
    def initial_for(cls, user) -> dict:
        return {"weekly_digest_enabled": not user.weekly_digest_opt_out}

    def apply_to(self, user) -> None:
        user.weekly_digest_opt_out = not self.cleaned_data["weekly_digest_enabled"]
        user.save(update_fields=["weekly_digest_opt_out"])
