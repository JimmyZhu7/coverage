"""Forms for the CRM. `ContactForm` is the hand-add / edit path — the entry
point the whole CRM lacked (before it, a contact could only arrive via CSV
import or inbound-email capture). It never touches warmth/thread_state: those
are owned entirely by the coverage_domain ratchet, not the form.
"""

from __future__ import annotations

from django import forms

from crm.models import ChatDebrief, Contact
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

    class Meta:
        model = Contact
        fields = [
            "name", "firm", "firm_text", "role", "email", "linkedin",
            "school", "region", "angle", "opener", "notes",
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
