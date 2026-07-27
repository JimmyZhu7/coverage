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

from datetime import date

from django import forms

from coverage_domain.cadence import CADENCE_DEFAULTS
from directory.classify import REGION_LABELS, REGION_ORDER
from directory.recommend import cycle_choices

# The cadence whitelist + its valid ranges live at the point of use, in
# crm.views — that module is what actually hands the overrides to
# `cadence.due_actions`, so it owns the definition of "safe to honor". This
# form imports it rather than restating the ranges: a copy here would be a
# second source of truth that drifts silently, and the failure mode of drift
# is a settings page that happily saves a value the engine then ignores.
from crm.views import TUNABLE_CADENCE_PARAMS

from .models import WORK_AUTH

# Human labels for the region tokens a student can state a preference for.
# Sourced from `classify.REGION_ORDER`/`REGION_LABELS` — the SAME six-market
# vocabulary the Opportunities feed's own Region filter uses — rather than a
# second, narrower hk/us-only list. That narrower list used to be sourced
# from `Firm.regions`, which really is hk/us-only in the seed data, but
# `Opportunity.region` (what a role's own location resolves to) is six wide:
# eu (204 open campus roles), sg (74), cn (30), jp (16) were all markets a
# student could not state a preference — or a work-authorization answer,
# since `WorkAuthorizationForm` below also iterates this list — for.
REGION_CHOICES: list[tuple[str, str]] = [
    (code, REGION_LABELS[code]) for code in REGION_ORDER
]

TRACK_CHOICES: list[tuple[str, str]] = [
    ("ib", "Investment Banking"),
    ("st", "Sales & Trading"),
    ("pe", "Private Equity"),
    ("am", "Asset Management"),
    ("consulting", "Consulting"),
    ("corp-strat", "Corporate Strategy"),
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
    avatar = forms.ImageField(required=False)
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
            }
        )

    def apply_to(self, user) -> None:
        """Persist validated values back onto the user row. Call only after
        `is_valid()`."""
        cd = self.cleaned_data
        update_fields = ["name", "school", "class_year", "target_cycle", "regions", "tracks"]
        user.name = cd["name"]
        user.school = cd["school"]
        user.class_year = cd["class_year"]
        user.target_cycle = cd["target_cycle"]
        user.regions = cd["regions"]
        user.tracks = cd["tracks"]
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


class OutreachAssetsForm(SectionForm):
    """`User.assets["angles"]` — the things the user leads with in outreach
    ("London M&A boutique internship (live deal exposure)").

    ONE TEXTAREA, ONE ANGLE PER LINE, deliberately — not a JS row manager.
    Add/remove/reorder are all just text editing, which every user already
    knows how to do; it degrades to a working form with JS off, it survives a
    validation round-trip without rebuilding DOM state, and it has no
    index-shuffling bugs. A row manager would buy drag handles and cost a few
    hundred lines of JS plus a POST format that has to be reassembled
    server-side. If ordering ever needs to be more than "the order you typed
    them", revisit; today it doesn't.
    """

    section = "assets"
    success_message = "Outreach assets saved."

    # A generous ceiling, not a product opinion: `assets` is a JSON column on
    # every row of `users`, so an accidental paste of a whole CV shouldn't be
    # able to grow it without bound. Nobody leads with 50 different angles.
    MAX_ANGLES = 50

    angles = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "One angle per line…"}),
    )

    @classmethod
    def initial_for(cls, user) -> dict:
        angles = (user.assets or {}).get("angles") or []
        return {"angles": "\n".join(str(a) for a in angles)}

    def clean_angles(self) -> list[str]:
        """Text -> list of non-empty lines, order preserved."""
        raw = self.cleaned_data.get("angles") or ""
        lines = [line.strip() for line in raw.splitlines()]
        angles = [line for line in lines if line]
        if len(angles) > self.MAX_ANGLES:
            raise forms.ValidationError(
                f"That's more than {self.MAX_ANGLES} angles. Keep the ones you "
                f"actually use."
            )
        return angles

    def apply_to(self, user) -> None:
        # Copy-then-set, never assign a fresh dict: `assets` also holds
        # languages / current_status / advocate_target, and this form owns
        # exactly one key of it.
        assets = dict(user.assets or {})
        angles = self.cleaned_data["angles"]
        if angles:
            assets["angles"] = angles
        else:
            # Blank means "no angles recorded", which is the absence of the
            # key — not an empty list sitting there looking like an answer.
            assets.pop("angles", None)
        user.assets = assets
        user.save(update_fields=["assets"])


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
            self.fields[f"{self.FIELD_PREFIX}{code}"] = forms.ChoiceField(
                label=label,
                choices=[("", "Not specified")] + list(WORK_AUTH),
                required=False,
                widget=forms.Select,
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
        "How long a cold contact sits without a reply before Coverage asks you "
        "to follow up the first time.",
    ),
    "second_followup_after_business_days": (
        "Second Follow-Up",
        "business days",
        "How long to wait after that follow-up before Coverage asks you to try "
        "once more. Only applies if Max Cold Touches is 3 or higher.",
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
        "How many times you'll reach out to someone who has never replied.",
    ),
    "advocate_touch_min_weeks": (
        "Advocate Check-In",
        "weeks",
        "How often your advocates get a keep-warm touch.",
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
                           "placeholder": CADENCE_DEFAULTS[key]}
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
        return {
            key: stored.get(key)
            for key in TUNABLE_CADENCE_PARAMS
            if isinstance(stored.get(key), int)
            and not isinstance(stored.get(key), bool)
        }

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
        user.save(update_fields=["cadence_params"])


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
            attrs={"min": 1, "max": MAX_GOAL, "step": 1, "placeholder": DEFAULT_GOAL}
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
