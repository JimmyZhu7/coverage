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

    class Meta:
        model = Contact
        fields = [
            "name", "firm", "firm_text", "role", "email", "linkedin",
            "school", "angle", "notes",
        ]
        widgets = {
            "angle": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "firm_text": "Firm (if not listed)",
            "angle": "Angle",
            "linkedin": "LinkedIn URL",
        }

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("firm") and not (cleaned.get("firm_text") or "").strip():
            # Not fatal — a contact can have no firm — but nudge, since a
            # firm-less contact never appears on the coverage board.
            pass
        return cleaned
