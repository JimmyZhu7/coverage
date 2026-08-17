"""Template filters for rendering the advisor's replies.

The model writes plain prose with occasional Markdown emphasis (the system
prompt never asks for it, but every Claude/GPT-family model reaches for
`**bold**` on its own to flag a name or a recommendation). `linebreaksbr`
alone left those asterisks sitting in the page as literal text, which reads
as broken rather than as a person's note. This is deliberately not a real
Markdown renderer — one inline rule (bold) covers what the model actually
produces here, and anything more is surface area this page doesn't need.
"""

from __future__ import annotations

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


@register.filter(name="chat_format")
def chat_format(text: str):
    """Escape untrusted model output first, THEN layer safe markup on top —
    never the other way round, or the escaping would neuter the `<strong>`
    tags this function itself writes."""
    safe_text = escape(text or "")
    bolded = _BOLD_RE.sub(r"<strong>\1</strong>", safe_text)
    return mark_safe(bolded.replace("\n", "<br>"))
