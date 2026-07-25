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

# Human labels for the raw region/track tokens stored on firms and users.
REGION_CHOICES: list[tuple[str, str]] = [
    ("hk", "Hong Kong"),
    ("us", "United States"),
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

# Recruiting-cycle options, anchored to the current year. Values double as
# labels (the model column is a short CharField), so every entry stays under
# 32 characters.
CYCLE_CHOICES: list[tuple[str, str]] = (
    [("", "Select a cycle")]
    + [(f"{y} Summer Internship", f"{y} Summer Internship")
       for y in (_YEAR, _YEAR + 1, _YEAR + 2)]
    + [(f"{y} Full-Time / Graduate", f"{y} Full-Time / Graduate")
       for y in (_YEAR, _YEAR + 1)]
    + [(f"{y} Spring Week / Insight", f"{y} Spring Week / Insight")
       for y in (_YEAR, _YEAR + 1)]
    + [("Off-Cycle / Immediate", "Off-Cycle / Immediate")]
)

# Kept for backwards compatibility with any view still passing suggestions.
CYCLE_SUGGESTIONS: list[str] = [c for c, _ in CYCLE_CHOICES if c]


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
