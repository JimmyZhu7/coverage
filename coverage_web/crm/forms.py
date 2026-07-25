"""Forms for the CRM. `ContactForm` is the hand-add / edit path — the entry
point the whole CRM lacked (before it, a contact could only arrive via CSV
import or inbound-email capture). It never touches warmth/thread_state: those
are owned entirely by the coverage_domain ratchet, not the form.
"""

from __future__ import annotations

from django import forms

from crm.models import Contact
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
