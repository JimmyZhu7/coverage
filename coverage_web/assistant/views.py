"""The "Talk to Coverage" page and its one POST.

TRANSPORT: a plain synchronous htmx POST that returns the re-rendered thread
fragment. No streaming, no websockets, no polling. The agent loop can take a
few seconds and the page shows typing dots (`hx-indicator`) while it does —
which is the entire complexity budget this deserves at one user's scale.

POST, NEVER GET, for the same reason `crm.views.contact_ai_brief` is POST:
every send costs real money, and a GET is something a browser prefetcher, a
link scanner, or a back button will happily fire on its own.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import agent
from .client import is_configured
from .models import ChatConversation, ChatMessage

# What the student can type in one go. Long enough for a real situation
# ("I have two chats at Goldman and nothing at Morgan Stanley, plus..."),
# short enough that a paste of their whole CV isn't billed as one message.
MAX_MESSAGE_CHARS = 4000

# Openers on the empty state. Concrete on purpose: a blank box with a blinking
# cursor is the fastest way to make a student decide this page isn't for them.
STARTERS = [
    "Who should I follow up with today, and why?",
    "Where should I spend this week?",
    "Which roles close this month?",
    "How am I doing at my top firms?",
]


def _current_conversation(user) -> ChatConversation:
    conversation = ChatConversation.objects.for_user(user).first()  # newest first
    if conversation is None:
        conversation = ChatConversation(user=user)
        conversation.save()
    return conversation


def _thread_rows(user, conversation) -> list[dict]:
    """The thread as the template wants it: the student's messages, the
    advisor's prose, and a quiet line naming the lookups behind each answer.

    Tool-result turns never render — they are the machinery. Assistant turns
    that are pure `tool_use` (no prose) don't render either; their tool names
    are folded into the next answer's evidence line, so the thread reads as a
    conversation rather than a transcript of an inner monologue.
    """
    rows: list[dict] = []
    pending_tools: list[str] = []
    for message in ChatMessage.objects.for_user(user).filter(conversation=conversation):
        if message.role == ChatMessage.ROLE_USER:
            if message.is_tool_result:
                continue
            rows.append({"role": "user", "text": message.text, "tools": [], "notice": ""})
            continue
        pending_tools.extend(message.tool_names)
        text = message.text
        if not text:
            continue
        rows.append(
            {
                "role": "assistant",
                "text": text,
                "tools": _tool_labels(pending_tools),
                "notice": message.notice,
            }
        )
        pending_tools = []
    return rows


_TOOL_LABELS = {
    "get_today_queue": "your queue",
    "search_contacts": "your contacts",
    "get_contact": "a contact's history",
    "search_opportunities": "the roles board",
    "get_firm": "a firm",
    "get_calendar": "your calendar",
    "get_my_pipeline": "your pipeline",
    "log_touch": "logged a touch",
    "track_opportunity": "saved a role",
}


def _tool_labels(names: list[str]) -> list[str]:
    seen: list[str] = []
    for name in names:
        label = _TOOL_LABELS.get(name, name)
        if label not in seen:
            seen.append(label)
    return seen


def _context(request: HttpRequest, conversation: ChatConversation) -> dict:
    return {
        "conversation": conversation,
        "rows": _thread_rows(request.user, conversation),
        "configured": is_configured(),
        "starters": STARTERS,
        "max_chars": MAX_MESSAGE_CHARS,
    }


@login_required
def chat(request: HttpRequest) -> HttpResponse:
    conversation = _current_conversation(request.user)
    return render(request, "assistant/chat.html", _context(request, conversation))


@login_required
@require_POST
def send(request: HttpRequest) -> HttpResponse:
    """Run one turn and return the thread fragment for an htmx swap."""
    conversation = _current_conversation(request.user)
    text = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    if text:
        agent.run_turn(request.user, conversation, text)
    return render(request, "assistant/_thread.html", _context(request, conversation))


@login_required
@require_POST
def new_conversation(request: HttpRequest) -> HttpResponse:
    """Start fresh. Also the documented way out of the per-conversation
    tool-call cap — the loop tells the student to do exactly this."""
    ChatConversation(user=request.user).save()
    return redirect("assistant:chat")
