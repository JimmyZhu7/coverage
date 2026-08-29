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


class ChatFolder(PrivateModel):
    """A student's own grouping of chats — "BofA prep", "General strategy" —
    nothing more than a name and an owner. Deleting one never deletes the
    chats in it (see ChatConversation.folder's on_delete=SET_NULL): a folder
    is organisation, not a container a conversation's existence depends on."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "assistant_folders"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class ChatConversation(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Free text. Seeded from the first thing the student typed the instant
    # the turn starts (has to exist synchronously for the sidebar), then
    # usually overwritten by a short model-written title once that first
    # turn actually finishes — see agent._retitle_if_first_message. Never
    # regenerated after that: a rename (assistant.views.rename_conversation)
    # is the student overriding it on purpose, and a later turn has no
    # business quietly undoing that.
    title = models.CharField(max_length=255, blank=True, default="")
    # NULL = "not in a folder", the default and the common case — most
    # students will never make one. SET_NULL, not CASCADE: deleting a folder
    # is tidying up an organisational label, not a reason to destroy
    # conversations that happen to be in it.
    folder = models.ForeignKey(
        ChatFolder, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )
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
    def attachment_names(self) -> list[str]:
        """Filenames of any image/document blocks in this turn — `_filename`
        is this app's own bookkeeping (assistant/attachments.py), stripped
        before the block ever reaches the model, but kept here in storage
        precisely so the thread can still say what was attached."""
        return [
            b["_filename"]
            for b in self.blocks()
            if isinstance(b, dict) and b.get("type") in ("image", "document") and b.get("_filename")
        ]

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


class AdvisorMemory(PrivateModel):
    """One durable fact the model chose to carry forward — "targeting HK
    over US", "ruled out PE" — via the `remember` tool (assistant/tools.py).
    The one thing conversations, being siloed per-thread, could not do on
    their own: a fact told to the model in one chat is otherwise invisible
    to it in every other one, including a brand new one in a different
    folder. Injected into every conversation's preamble
    (agent.build_preamble), never sent as a tool result — this is
    background the model always has, not something it looked up.

    A safe write, same posture as log_touch/track_opportunity: capped
    (assistant.tools.MAX_MEMORIES) so it cannot grow into an unbounded
    context tax, and the student can forget any one of them from the Talk
    page itself — no confirm-card machinery, because getting this wrong
    costs nothing more than the model re-learning it next time it's true."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "assistant_memories"
        ordering = ["-created", "-id"]

    def __str__(self) -> str:
        return self.text


class DailyBrief(PrivateModel):
    """One advisor-written paragraph, generated at most once per student
    per calendar day and cached here — see assistant/brief.py. Surfaced at
    the top of the Today page (crm/week.html), the one habit loop this
    feature otherwise lacks: everything else on Talk to Coverage requires
    the student to remember to open it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField()
    text = models.CharField(max_length=600)
    created = models.DateTimeField(auto_now_add=True)
    # Ids of the contacts this text actually named (the queue slice
    # `_summarize_actions` was given, see assistant/brief.py) — not a
    # display field, a staleness fingerprint. The brief is written once and
    # cached for the rest of the day, but the founder can park, exclude, or
    # archive a named contact an hour later; without recording who was named
    # there is nothing to notice that the cached sentence now tells him to
    # chase someone he just deliberately set aside. Empty for a brief with no
    # queue contacts behind it (situation-only) and for any row written
    # before this field existed — `_is_stale` treats an empty list as
    # "nothing to check", never as "everybody named is gone".
    contact_ids = models.JSONField(default=list, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "assistant_daily_briefs"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="uniq_daily_brief_user_date"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.date}"
