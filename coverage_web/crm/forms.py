"""Forms for the CRM. `ContactForm` is the hand-add / edit path — the entry
point the whole CRM lacked (before it, a contact could only arrive via CSV
import or inbound-email capture). It never touches warmth/thread_state: those
are owned entirely by the coverage_domain ratchet, not the form.
"""

from __future__ import annotations

from datetime import datetime, time
from django.utils import timezone
from django import forms

from crm.models import CalendarEvent, ChatDebrief, Contact
from directory.models import Firm


class ContactForm(forms.ModelForm):
    """Create or edit one contact. `firm` is an optional directory link; a
    contact at a firm outside the directory is captured in `firm_text`
    instead (same fallback the import + capture paths use)."""

    firm = forms.ModelChoiceField(
        queryset=Firm.objects.order_by("name"),
        required=False,
        empty_label="Not in the directory",
    )

    # Blank is a real choice, not a placeholder to be filled in: an unknown
    # region keeps the cadence engine's both-regions fallback, which is safer
    # than a wrong guess. `Contact.save()` fills this in from the firm when the
    # firm names exactly one region.
    region = forms.ChoiceField(
        choices=[("", "Unknown / set from firm"), *Contact.REGION_CHOICES],
        required=False,
    )

    # The override for "is this person part of the recruiting process?". Three
    # choices, not a checkbox, because the model column is nullable and NULL is
    # a real state: nobody has said, so the queue reads the role text. A
    # checkbox would collapse "not answered" into "no" for every contact ever
    # imported and quietly turn a guess into a stored fact.
    recruiting_contact = forms.ChoiceField(
        choices=[
            ("", "Work it out from their role"),
            ("yes", "Yes, recruiting contact"),
            ("no", "No, a normal networking contact"),
        ],
        required=False,
        label="Recruiting contact?",
        help_text="Recruiters never get a coffee-chat prompt. You can still "
                  "reply to them and track their deadlines.",
    )

    class Meta:
        model = Contact
        fields = [
            "name", "firm", "firm_text", "role", "email", "linkedin",
            "school", "region", "recruiting_contact", "angle", "opener", "notes",
        ]
        widgets = {
            "angle": forms.Textarea(attrs={"rows": 2}),
            "opener": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "firm_text": "Firm (if not listed)",
            "angle": "Angle (private)",
            "opener": "Opener",
            "linkedin": "LinkedIn URL",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stored = getattr(self.instance, "recruiting_contact", None)
        self.initial["recruiting_contact"] = (
            "" if stored is None else ("yes" if stored else "no")
        )

    def clean_recruiting_contact(self):
        """"" back to None, so a blank answer keeps the column NULL and the
        queue keeps reading the role text — rather than storing False, which
        would be the student asserting something they never said."""
        return {"yes": True, "no": False}.get(
            self.cleaned_data.get("recruiting_contact") or "", None
        )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("firm") and not (cleaned.get("firm_text") or "").strip():
            # Not fatal — a contact can have no firm — but nudge, since a
            # firm-less contact never appears on the coverage board.
            pass
        return cleaned


class ChatDebriefForm(forms.ModelForm):
    """The four questions asked after a coffee chat.

    A plain ModelForm over the ANSWER fields only. The derived columns
    (`intro_contact`, `intro_task`, `date_task`, `promoted`, `dismissed`)
    are written by `crm.debrief.record`, which owns the side effects and
    their idempotency — a form must never be able to point a debrief at an
    arbitrary contact or task id.

    Every field is optional. A debrief where the only true answer is "we
    talked, nothing actionable" is a valid debrief, and demanding more is
    how a helpful prompt turns into a chore the user routes around.
    """

    class Meta:
        model = ChatDebrief
        fields = [
            "learned", "intro_name", "intro_email",
            "tracked_date", "date_note", "advocate_answer",
        ]
        widgets = {
            "learned": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "Their team, what they care about, what "
                                   "they told you to do next…",
                }
            ),
            "tracked_date": forms.DateInput(attrs={"type": "date"}),
            "date_note": forms.TextInput(
                attrs={"placeholder": "e.g. Apps open for their desk"}
            ),
            "intro_name": forms.TextInput(attrs={"placeholder": "Full name"}),
            "advocate_answer": forms.RadioSelect(
                choices=ChatDebrief.ADVOCATE_ANSWERS
            ),
        }
        labels = {
            "learned": "What did you learn?",
            "intro_name": "Who?",
            "intro_email": "Their email (optional)",
            "tracked_date": "The date",
            "date_note": "What happens then?",
            "advocate_answer": "Would they advocate for you?",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model field is blank=True, so Django prepends an empty
        # "---------" choice — which RadioSelect renders as a real, and
        # worse, pre-CHECKED radio button. Drop it: "no answer" here is
        # simply no radio picked, not a fourth thing to pick.
        self.fields["advocate_answer"].choices = ChatDebrief.ADVOCATE_ANSWERS
        for field in self.fields.values():
            field.required = False

    def clean(self):
        cleaned = super().clean()
        # A date with no label is still trackable (the task gets a generic
        # title), but a label with no date has nowhere to land — say so
        # rather than silently dropping what the user typed.
        if cleaned.get("date_note") and not cleaned.get("tracked_date"):
            self.add_error("tracked_date", "Pick the date this refers to.")
        return cleaned


class CalendarEventForm(forms.ModelForm):
    """Add one event to the calendar by hand.

    The date and the time are SEPARATE inputs, and the time is optional. A
    single `datetime-local` would force a clock time on everything, and half
    of what belongs on a recruiting calendar genuinely has none — "Superday
    on the 14th" is a fact about a day. Leaving time blank stores local
    midnight with `all_day` set, so the row sorts with the day rather than
    claiming to happen at 00:00.
    """

    day = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    at = forms.TimeField(
        label="Time",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
        help_text="Leave blank for an all-day entry.",
    )

    class Meta:
        model = CalendarEvent
        fields = ["title", "kind", "contact", "location", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "title": forms.TextInput(attrs={"placeholder": "Coffee with…, Superday, flight"}),
            "location": forms.TextInput(attrs={"placeholder": "Zoom, their office, …"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].required = False
        self.fields["location"].required = False
        self.fields["description"].required = False
        # Only the user's own, unarchived contacts can be attached. Without
        # scoping, the dropdown would list every contact in the database.
        # `all_objects.none()`, not `objects.none()`: the tenant manager
        # raises on ANY queryset built through it, `.none()` included, and an
        # unbound form (no user) must still render.
        qs = Contact.all_objects.none()
        if user is not None:
            qs = Contact.objects.for_user(user).filter(archived=False).order_by("name")
        self.fields["contact"].queryset = qs
        self.fields["contact"].empty_label = "No one in particular"

    def clean(self):
        cleaned = super().clean()
        day, at = cleaned.get("day"), cleaned.get("at")
        if day:
            naive = datetime.combine(day, at or time.min)
            # Interpreted in the USER's timezone (TimezoneMiddleware activates
            # it per request), so "3pm" means 3pm where they are — storing the
            # server's 3pm would slide every entry for an HK user.
            cleaned["starts_at"] = timezone.make_aware(
                naive, timezone.get_current_timezone()
            )
            cleaned["all_day"] = at is None
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.starts_at = self.cleaned_data["starts_at"]
        obj.all_day = self.cleaned_data["all_day"]
        if commit:
            obj.save()
        return obj
