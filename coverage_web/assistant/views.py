"""The "Talk to Coverage" page and its POSTs.

TWO TRANSPORTS for one send, on purpose. `send` is the original: a plain
synchronous htmx POST returning the re-rendered thread fragment, blocking
until the whole reply exists. `stream` is what the composer's own JS actually
calls now (see chat.html) — Server-Sent Events, the reply growing into the
page token by token the way a student already expects from every other AI
surface they use.

`send` was NOT deleted for `stream` — it stays as the fallback path: if a
browser has JS disabled, or `fetch`/`ReadableStream` isn't there, or the
stream fails to even open, the form's own `hx-post` still works exactly as
it always did, because it was never touched. Rebuilding a synchronous request
from a half-open stream is real complexity for a case that already has a
working answer; skipping straight to it is not corner-cutting.

POST, NEVER GET, for both, for the same reason `crm.views.contact_ai_brief`
is POST: every send costs real money, and a GET is something a browser
prefetcher, a link scanner, or a back button will happily fire on its own.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import agent
from . import attachments as attachments_mod
from .client import is_configured
from .models import ChatConversation, ChatFolder, ChatMessage

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


# How many past chats the history panel lists. A personal advisor thread,
# not a searchable archive — nothing here paginates past this.
HISTORY_LIMIT = 50


def _current_conversation(user, conversation_id: int | None = None) -> ChatConversation:
    """The conversation a request is about: a specific one if `conversation_id`
    names it (the history panel's own links, or a resubmitted composer),
    otherwise the most recently active one — creating the student's very
    first conversation if they have none yet.

    `for_user(user)` is what makes `conversation_id` safe to take from a
    request at all: a ChatConversation belonging to another student simply
    isn't IN this queryset, so `get_object_or_404` 404s rather than leaking
    it, the same tenancy guarantee every other private-zone lookup in this
    app relies on.
    """
    qs = ChatConversation.objects.for_user(user)
    if conversation_id is not None:
        return get_object_or_404(qs, pk=conversation_id)
    conversation = qs.first()  # Meta.ordering = -updated, -id: newest first
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
            rows.append(
                {
                    "role": "user",
                    "text": message.text,
                    "tools": [],
                    "notice": "",
                    "attachments": message.attachment_names,
                }
            )
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


# The name -> label map lives in agent.py (agent.TOOL_LABELS), which also
# uses it for the mid-stream "reading" hint — one map, not two that can
# quietly drift apart.
_tool_labels = agent.tool_labels


def _conversations(user):
    return ChatConversation.objects.for_user(user)[:HISTORY_LIMIT]


def _history_context(user, conversation: ChatConversation) -> dict:
    """The sidebar's own data: every folder (even an empty one — that's the
    one moment a student can drag/move something INTO it) paired with the
    chats sitting in it, plus everything with no folder at all. Grouped
    here, not in the template — Django's template language has no clean way
    to bucket a flat list by a foreign key, and the view is where "which
    folder is this chat in" is a one-line dict lookup instead of a
    {% regroup %} fighting HISTORY_LIMIT's slice.
    """
    conversations = list(_conversations(user))
    folders = list(ChatFolder.objects.for_user(user))
    grouped: dict[int, list[ChatConversation]] = {f.id: [] for f in folders}
    unfiled: list[ChatConversation] = []
    for c in conversations:
        bucket = grouped.get(c.folder_id)
        (bucket if bucket is not None else unfiled).append(c)
    return {
        "conversation": conversation,
        "all_folders": folders,  # flat list — the move-picker's own options
        "folders": [{"folder": f, "conversations": grouped[f.id]} for f in folders],
        "unfiled": unfiled,
    }


def _context(request: HttpRequest, conversation: ChatConversation) -> dict:
    return {
        "conversation": conversation,
        "rows": _thread_rows(request.user, conversation),
        "configured": is_configured(),
        "starters": STARTERS,
        "max_chars": MAX_MESSAGE_CHARS,
        "attach_accept": attachments_mod.ACCEPT_ATTRIBUTE,
        **_history_context(request.user, conversation),
    }


def _posted_int(request: HttpRequest, field: str) -> int | None:
    raw = request.POST.get(field)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _posted_conversation_id(request: HttpRequest) -> int | None:
    return _posted_int(request, "conversation")


@login_required
def chat(request: HttpRequest, conversation_id: int | None = None) -> HttpResponse:
    conversation = _current_conversation(request.user, conversation_id)
    return render(request, "assistant/chat.html", _context(request, conversation))


@login_required
@require_POST
def send(request: HttpRequest) -> HttpResponse:
    """Run one turn and return the thread fragment for an htmx swap.

    The conversation comes from the composer's own hidden field, not "the
    newest one" — once the history panel can open an older chat, sending
    from inside it must reply there, not silently start talking to whatever
    conversation happens to be most recent.
    """
    conversation = _current_conversation(request.user, _posted_conversation_id(request))
    text = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    blocks, errors = attachments_mod.blocks_for(request.FILES.getlist("file"))
    if errors:
        agent.reject_attachments(request.user, conversation, text, errors)
    elif text or blocks:
        agent.run_turn(request.user, conversation, text, attachment_blocks=blocks)
    return render(request, "assistant/_thread.html", _context(request, conversation))


@login_required
@require_POST
def stream(request: HttpRequest) -> HttpResponse:
    """The composer's real transport (see chat.html's JS): the same one turn
    as `send`, as Server-Sent Events instead of one blocking response.

    Django itself never streams to the model — `agent.stream_turn` is a
    generator, and this view's only job is turning each of its small dicts
    into one `data: <json>\\n\\n` frame. A synchronous `StreamingHttpResponse`
    over a generator is enough for that: gunicorn's sync workers flush each
    `yield` to the client as it happens, no ASGI/websocket machinery needed
    for a single request/response that just takes longer to finish.

    `X-Accel-Buffering: no` matters more than it looks like it should — an
    intermediary proxy that buffers the whole response before forwarding it
    (the default for a few common ones) turns a stream back into exactly the
    all-at-once response this endpoint exists to avoid.
    """
    conversation = _current_conversation(request.user, _posted_conversation_id(request))
    text = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    blocks, errors = attachments_mod.blocks_for(request.FILES.getlist("file"))

    def frames():
        if errors:
            # Same shape as any other terminal notice (unconfigured, capped)
            # — one frame, nothing streamed, no API call spent on a request
            # that was never going to be sent.
            reply = agent.reject_attachments(request.user, conversation, text, errors)
            yield f"data: {json.dumps({'type': 'notice', 'kind': 'failed', 'text': reply.text})}\n\n"
            return
        for event in agent.stream_turn(request.user, conversation, text, attachment_blocks=blocks):
            yield f"data: {json.dumps(event)}\n\n"

    response = StreamingHttpResponse(frames(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
def history_fragment(request: HttpRequest) -> HttpResponse:
    """The sidebar's own contents, GET-able on their own.

    `stream` cannot swap the sidebar the way `send`'s full-fragment response
    does — an SSE response is one long-lived stream, not something the
    client re-renders HTML from. The composer's JS re-fetches this once a
    stream finishes, which is also the ONE moment the sidebar can have
    changed: a fresh conversation just earned its title (see
    agent.stream_turn), or its position moved to the top of "-updated".
    """
    # A GET view reading its own query string, not `request.POST` — this is
    # a plain refetch, not an action, so it earns none of the POST-only
    # cost-guard reasoning the rest of this module's endpoints carry.
    raw = request.GET.get("conversation")
    try:
        conversation_id = int(raw) if raw else None
    except ValueError:
        conversation_id = None
    conversation = _current_conversation(request.user, conversation_id)
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def new_conversation(request: HttpRequest) -> HttpResponse:
    """Start fresh. Also the documented way out of the per-conversation
    tool-call cap — the loop tells the student to do exactly this."""
    ChatConversation(user=request.user).save()
    return redirect("assistant:chat")


@login_required
@require_POST
def rename_conversation(request: HttpRequest) -> HttpResponse:
    """Set a chat's title from the history panel's inline editor. Returns the
    panel's own fragment so the new title (and, since the list is ordered by
    `-updated`, the reordering a rename now also causes) show up in the same
    round trip."""
    conversation = get_object_or_404(
        ChatConversation.objects.for_user(request.user), pk=_posted_conversation_id(request) or 0
    )
    conversation.title = (request.POST.get("title") or "").strip()[:255]
    conversation.save(update_fields=["title", "updated"])
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def move_conversation(request: HttpRequest) -> HttpResponse:
    """Put a chat in a folder, or take it out (a blank/0 `folder` field is
    "no folder", not "leave it where it is" — the move control always
    submits an explicit choice, never a partial one)."""
    conversation = get_object_or_404(
        ChatConversation.objects.for_user(request.user), pk=_posted_conversation_id(request) or 0
    )
    folder_id = _posted_int(request, "folder")
    folder = (
        get_object_or_404(ChatFolder.objects.for_user(request.user), pk=folder_id)
        if folder_id
        else None
    )
    conversation.folder = folder
    conversation.save(update_fields=["folder"])
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def create_folder(request: HttpRequest) -> HttpResponse:
    name = (request.POST.get("name") or "").strip()[:100]
    if name:
        ChatFolder(user=request.user, name=name).save()
    conversation = _current_conversation(request.user, _posted_int(request, "current"))
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def rename_folder(request: HttpRequest) -> HttpResponse:
    folder = get_object_or_404(
        ChatFolder.objects.for_user(request.user), pk=_posted_int(request, "folder") or 0
    )
    name = (request.POST.get("name") or "").strip()[:100]
    if name:
        folder.name = name
        folder.save(update_fields=["name"])
    conversation = _current_conversation(request.user, _posted_int(request, "current"))
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def delete_folder(request: HttpRequest) -> HttpResponse:
    """Deletes the FOLDER only — every chat inside it becomes unfiled, never
    deleted (ChatConversation.folder is on_delete=SET_NULL). A folder is a
    label a student put on some chats; removing the label is not a reason
    to remove the chats."""
    folder = get_object_or_404(
        ChatFolder.objects.for_user(request.user), pk=_posted_int(request, "folder") or 0
    )
    folder.delete()
    conversation = _current_conversation(request.user, _posted_int(request, "current"))
    return render(request, "assistant/_history.html", _history_context(request.user, conversation))


@login_required
@require_POST
def delete_conversation(request: HttpRequest) -> HttpResponse:
    """Permanently remove a chat and every turn in it. No archive, no undo —
    unlike every other destructive-ish action in this app (contact archive,
    the daily cap's own "resets tomorrow"), there is no coming back from
    this one, on purpose: a rename slot getting reused elsewhere would be a
    worse footgun than plain deletion for a thread that may hold a
    student's candid read on a real person at a real firm. The one-time
    confirm() the history panel's delete button carries (see chat.html) is
    the entire safety net; this view does exactly what it's told.

    A plain redirect, not an htmx fragment swap: deleting the conversation
    the student is currently READING would otherwise leave the main thread
    pane showing a conversation that no longer exists. `current` (the page's
    own conversation, posted alongside `conversation`, the one being
    deleted) decides where the reload lands — stay put if a DIFFERENT chat
    was deleted from the sidebar, move on if it was this one.
    """
    conversation = get_object_or_404(
        ChatConversation.objects.for_user(request.user), pk=_posted_conversation_id(request) or 0
    )
    deleted_id = conversation.id
    conversation.delete()

    current_id = _posted_int(request, "current")
    if (
        current_id
        and current_id != deleted_id
        and ChatConversation.objects.for_user(request.user).filter(pk=current_id).exists()
    ):
        return redirect("assistant:chat_conversation", conversation_id=current_id)
    return redirect("assistant:chat")
