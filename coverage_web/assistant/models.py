"""Conversation state for "Talk to Coverage" (the advisor page).

Private zone, like every other per-user table — `coverage_web.tenancy`'s
`PrivateModel` base, so `objects` refuses an unscoped query and the only way
to read a row is `.objects.for_user(user)`. A conversation is somebody's
private recruiting strategy discussion held over their own CRM; there is no
legitimate cross-tenant read of one.

WHY `content` IS A JSONField OF CONTENT BLOCKS, not a text column
-----------------------------------------------------------------
The agent loop (assistant/agent.py) replays prior turns back to the model
verbatim. The Messages API requires that every `tool_use` block in an
assistant turn is answered by a `tool_result` block carrying the same
`tool_use_id` in the very next user turn — flattening a turn to its prose
would break that pairing and the next request would be rejected. So each row
stores the EXACT content-block list that was sent or received, and replay is
a copy, not a reconstruction.

`notice` marks a row this app wrote itself rather than one that came from (or
went to) the model — "the assistant isn't configured", "you've hit today's
message cap". Those are shown in the thread and deliberately EXCLUDED from
replay: they are the product talking about itself, not part of the
conversation the model is having.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from coverage_web.tenancy import PrivateModel


class ChatConversation(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Free text, currently seeded from the first thing the student typed.
    # There is no conversation list UI yet; this exists so one is possible
    # without a migration.
    title = models.CharField(max_length=255, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta(PrivateModel.Meta):
        db_table = "assistant_conversations"
        ordering = ["-updated", "-id"]

    def __str__(self) -> str:
        return self.title or f"Conversation #{self.pk}"


class ChatMessage(PrivateModel):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [(ROLE_USER, "User"), (ROLE_ASSISTANT, "Assistant")]

    # Locally-generated rows, never replayed to the model. See module docstring.
    NOTICE_UNCONFIGURED = "unconfigured"
    NOTICE_CAPPED = "capped"
    NOTICE_FAILED = "failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    conversation = models.ForeignKey(
        ChatConversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    # The verbatim Messages-API content-block list for this turn.
    content = models.JSONField(default=list, blank=True)
    notice = models.CharField(max_length=32, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "assistant_messages"
        ordering = ["created", "id"]
        indexes = [models.Index(fields=["user", "conversation", "created"])]

    def __str__(self) -> str:
        return f"{self.role} @ {self.created:%Y-%m-%d %H:%M}"

    # --- Read helpers used by the thread template and the replay builder ---

    def blocks(self) -> list[dict]:
        return self.content if isinstance(self.content, list) else []

    @property
    def text(self) -> str:
        """Every text block in this turn, joined. Blank for a pure tool turn."""
        return "\n\n".join(
            (b.get("text") or "").strip()
            for b in self.blocks()
            if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
        )

    @property
    def tool_names(self) -> list[str]:
        return [
            b.get("name") or ""
            for b in self.blocks()
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]

    @property
    def is_tool_result(self) -> bool:
        """A user-role turn that only carries tool results — machinery, never
        shown in the thread."""
        blocks = self.blocks()
        return bool(blocks) and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
        )
