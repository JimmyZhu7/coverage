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
from django.db.models import Q
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.middleware import activate_for_user
from billing import credits as billing_credits
from coverage_domain.pipeline import CHANNELS, TOUCH_TRANSITIONS
from crm import services
from crm.models import Contact, Touch
from crm.utils import CHANNEL_LABELS  # a list of (value, label) pairs, not a dict

from . import agent
from . import attachments as attachments_mod
from . import drafts as drafts_mod
from .client import is_configured
from .models import AdvisorMemory, ChatConversation, ChatFolder, ChatMessage

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

# The same channel wording the CRM's own touch controls use, so a chip that
# says "Email" and a history row that says "Email" mean the one thing.
_CHANNEL_LABELS = dict(CHANNEL_LABELS)


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
                    # Same row-level primary key the assistant rows below
                    # carry, for the same kind of reason: it is what the
                    # edit control posts back to rewind the conversation to
                    # this point (`edit_message`).
                    "message_id": message.id,
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
                # The row's own primary key, not the model's message id: it is
                # what the log-touch chip posts back, and what the
                # `[assistant:<id>]` note marker is built from.
                "message_id": message.id,
                # Prose and finished drafts, in the order the model wrote them
                # (assistant/drafts.py). A reply with no draft fence in it is
                # exactly one prose segment, so the template has one path.
                "segments": drafts_mod.split(text),
                "tools": _tool_labels(pending_tools),
                "notice": message.notice,
            }
        )
        pending_tools = []
    _decorate_drafts(user, rows)
    return rows


def _decorate_drafts(user, rows: list[dict]) -> None:
    """Fill in what a draft card needs but the model never wrote: the
    contact's real name, and whether this draft's touch is already logged.

    TWO QUERIES FOR THE WHOLE THREAD, never one per message. The names come
    from a single `pk__in` over every contact any draft in the thread names.
    The logged state comes from a single OR of `[assistant:<id>]` substring
    matches — one clause per assistant message that actually carries a
    loggable draft, which in a real conversation is one or two, and which
    returns only the touches that matched rather than a student's whole touch
    history to be filtered in Python.

    A draft whose contact has since been deleted (or was never this student's)
    quietly loses its chip rather than 404ing the page: the words the model
    wrote are still worth reading and copying.
    """
    pending = [
        (row["message_id"], seg)
        for row in rows
        for seg in row.get("segments") or ()
        if seg["type"] == "draft" and seg["contact_id"]
    ]
    if not pending:
        return

    names = dict(
        Contact.objects.for_user(user)
        .filter(pk__in={seg["contact_id"] for _, seg in pending})
        .values_list("id", "name")
    )

    message_ids = {message_id for message_id, _ in pending}
    marker_match = Q()
    for message_id in message_ids:
        marker_match |= Q(note__contains=drafts_mod.marker_for(message_id))
    matched = list(Touch.objects.for_user(user).filter(marker_match).values_list("contact_id", "note"))
    # Keyed by (message, contact), not by message alone: one reply can hold
    # two drafts to two different people, and logging one of them must not
    # quietly mark the other done. The marker carries only the message id, so
    # the Touch's own contact is what separates them.
    logged = {
        (message_id, contact_id)
        for message_id in message_ids
        for contact_id, note in matched
        if drafts_mod.marker_for(message_id) in (note or "")
    }

    for message_id, seg in pending:
        seg["contact_name"] = names.get(seg["contact_id"], "")
        # The chip writes a real Touch through the real ratchet, so it only
        # appears when every field that write needs is known and valid.
        seg["loggable"] = bool(seg["contact_name"] and seg["channel"] and seg["kind"])
        seg["channel_label"] = _CHANNEL_LABELS.get(seg["channel"], seg["channel"])
        seg["message_id"] = message_id
        seg["logged"] = (message_id, seg["contact_id"]) in logged


def _draft_segments(user, message_id) -> list[dict]:
    """The decorated draft segments of one stored assistant message.

    Only the STREAM path needs this. A streamed reply is drawn client-side
    from tokens, so the browser has the draft's words but not the contact's
    name or its logged state — this rides along on the terminal "done" frame
    so the card the JS builds carries the same chip the server would have
    rendered (see chat.html).
    """
    message = ChatMessage.objects.for_user(user).filter(pk=message_id).first()
    if message is None or not message.text:
        return []
    row = {"message_id": message.id, "segments": drafts_mod.split(message.text)}
    _decorate_drafts(user, [row])
    return [seg for seg in row["segments"] if seg["type"] == "draft"]


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
        # The composer's quiet credit meter (docs/credit-system-plan.md §6 —
        # "the only UI work in the plan beyond the notice copy"). Read on
        # every non-streamed render so a debit is visible the instant the
        # reply lands; the streamed path can't re-render this fragment (an
        # SSE stream isn't HTML to swap from), so chat.html's JS calls
        # `assistant:credits` once a streamed turn finishes instead — see
        # that view below.
        "credit_balance": billing_credits.balance(request.user),
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
    context = _context(request, conversation)
    # Only the full page renders the memory dialog — _thread.html (send/
    # stream's own response) never reads this, so it stays out of the
    # shared _context() rather than costing every send an unused query.
    context["memories"] = AdvisorMemory.objects.for_user(request.user)
    return render(request, "assistant/chat.html", context)


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


def _editable_message_id(user, conversation, before_id: int | None = None) -> int | None:
    """The student's own message that a just-finished streamed turn answered.

    STREAM PATH ONLY, and the mirror image of `_draft_segments`: the browser
    drew that user bubble itself, straight from the textarea, so it holds the
    words but not the stored row's id — and the edit control needs the id to
    post back. Resolved here rather than added to the agent loop's own event
    contract, for the same reason the draft metadata is: the loop's job is
    the turn, not what the page hangs off it.

    The row is the newest non-`tool_result` user turn before the reply — or
    simply the newest one when there is no reply to look before, which is
    every way a turn can END BADLY (no API key, daily cap, model unreachable,
    a rejected attachment). Those are exactly the moments a student is most
    likely to want to reword and try again, so the pencil has to be there for
    them too, not only after a turn that worked.
    """
    rows = ChatMessage.objects.for_user(user).filter(
        conversation=conversation, role=ChatMessage.ROLE_USER
    )
    if before_id is not None:
        rows = rows.filter(pk__lt=before_id)
    for message in rows.order_by("-created", "-id"):
        if not message.is_tool_result:
            return message.id
    return None


def _sse(request: HttpRequest, conversation, text: str, blocks, errors, *, resume=False) -> HttpResponse:
    """One streamed turn as `text/event-stream`, shared by the two POSTs that
    can start one: a plain send (`stream`) and a rewind (`edit_message`).

    Django itself never streams to the model — `agent.stream_turn` is a
    generator, and this function's only job is turning each of its small dicts
    into one `data: <json>\\n\\n` frame. A synchronous `StreamingHttpResponse`
    over a generator is enough for that: gunicorn's sync workers flush each
    `yield` to the client as it happens, no ASGI/websocket machinery needed
    for a single request/response that just takes longer to finish.

    `X-Accel-Buffering: no` matters more than it looks like it should — an
    intermediary proxy that buffers the whole response before forwarding it
    (the default for a few common ones) turns a stream back into exactly the
    all-at-once response this endpoint exists to avoid.
    """

    def frames():
        # `TimezoneMiddleware` already activated request.user.timezone — but
        # only for the SYNCHRONOUS part of the request/response cycle, which
        # this generator is not part of. `StreamingHttpResponse` wraps it
        # lazily: the view returns almost immediately, the middleware's own
        # `finally: timezone.deactivate()` runs right after, and only THEN
        # does the WSGI server actually iterate this generator to produce a
        # body — by which point the activation is already gone and every
        # `timezone.localdate()`/`get_current_timezone()` call made by a tool
        # mid-turn (the "what is today" the whole queue is built on, or a
        # calendar event's own clock time) silently reads the server's UTC
        # default instead of the student's real day. Measured live 2026-08-18:
        # a 6-8pm entry added through chat landed on the calendar at 2am the
        # NEXT day for an Asia/Shanghai student — 18:00 stored as UTC rather
        # than converted from it. Re-activating here, on the thread that
        # actually runs the tool loop, is the fix; deactivating in `finally`
        # keeps a reused worker thread from leaking one student's zone into
        # the next request it serves, same reasoning as the middleware itself.
        activate_for_user(request.user)
        try:
            if errors:
                # Same shape as any other terminal notice (unconfigured,
                # capped) — one frame, nothing streamed, no API call spent on
                # a request that was never going to be sent.
                reply = agent.reject_attachments(request.user, conversation, text, errors)
                rejected = {
                    "type": "notice",
                    "kind": "failed",
                    "text": reply.text,
                    "user_message_id": _editable_message_id(request.user, conversation),
                }
                yield f"data: {json.dumps(rejected)}\n\n"
                return
            for event in agent.stream_turn(
                request.user, conversation, text, attachment_blocks=blocks, resume=resume
            ):
                if event.get("type") == "notice":
                    # A turn that ended badly still leaves the question on
                    # screen, and still deserves a pencil on it.
                    event = {
                        **event,
                        "user_message_id": _editable_message_id(request.user, conversation),
                    }
                elif event.get("type") == "done" and event.get("message_id"):
                    # The two things the agent loop can't know and the browser
                    # can't derive: who each draft is for (and whether its
                    # touch is already on the record), and the stored id of
                    # the question this answered, which is what that bubble's
                    # edit control posts back.
                    event = {
                        **event,
                        "drafts": _draft_segments(request.user, event["message_id"]),
                        "user_message_id": _editable_message_id(
                            request.user, conversation, event["message_id"]
                        ),
                    }
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            timezone.deactivate()

    response = StreamingHttpResponse(frames(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
@require_POST
def stream(request: HttpRequest) -> HttpResponse:
    """The composer's real transport (see chat.html's JS): the same one turn
    as `send`, as Server-Sent Events instead of one blocking response. The
    transport itself is `_sse` above, shared with the rewind endpoint.
    """
    conversation = _current_conversation(request.user, _posted_conversation_id(request))
    text = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    blocks, errors = attachments_mod.blocks_for(request.FILES.getlist("file"))
    return _sse(request, conversation, text, blocks, errors)


def _messages_after(user, message):
    """Every row of the same conversation that comes AFTER this one, in the
    order the thread is actually read.

    `ChatMessage.Meta.ordering` is `["created", "id"]`, so "after" is that
    same COMPOUND comparison, not `created__gt` on its own. The difference is
    not theoretical: one turn writes an assistant row and the user row of
    `tool_result` blocks answering it back to back in the same request, and
    on SQLite (and on any clock the DB rounds) those two can land on an
    identical `created`. With `created__gt` alone the second one would
    survive a rewind that deleted the first — leaving a `tool_result` whose
    `tool_use` no longer exists, which is the one shape the Messages API
    rejects outright (see agent._replayable), i.e. a conversation that can
    never be answered again.
    """
    return (
        ChatMessage.objects.for_user(user)
        .filter(conversation_id=message.conversation_id)
        .filter(Q(created__gt=message.created) | Q(created=message.created, id__gt=message.id))
    )


@login_required
@require_POST
def edit_message(request: HttpRequest) -> HttpResponse:
    """Rewind: change one of the student's own past messages and carry on
    from there, the way Claude.ai's edit-and-resend does.

    DESTRUCTIVE, and honestly so. Everything after the edited message is
    deleted — not hidden, not branched. A branch would mean a tree, a tree
    means a version switcher on every turn, and this page's whole shape is a
    single readable conversation. The student is told plainly what will go
    (chat.html's confirm dialog, the same one a chat delete uses) and then it
    goes.

    Only the student's OWN messages, and never a `tool_result` row: editing
    what the model said would be editing the record of what happened, and
    the point of a rewind is to ask differently, not to rewrite history.

    A logged touch that came from a draft in one of the deleted replies is
    deliberately left alone. The email was really sent; the CRM row is a fact
    about the student's network, not a fact about this conversation, and the
    conversation is not what makes it true.

    TWO RESPONSES, the same split `send`/`stream` already have: with
    `stream=1` (what chat.html's JS posts) the fresh turn comes back as the
    same SSE frames a normal send does, so the reply grows into the page
    where the deleted ones used to be. Without it, the turn runs
    synchronously and the browser is redirected back to the conversation —
    the path a browser that can't `fetch`/`ReadableStream` takes.
    """
    message = get_object_or_404(
        ChatMessage.objects.for_user(request.user), pk=_posted_int(request, "message") or 0
    )
    if message.role != ChatMessage.ROLE_USER or message.is_tool_result:
        return HttpResponseBadRequest("Only your own messages can be edited.")
    text = (request.POST.get("text") or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return HttpResponseBadRequest("An edited message still needs something in it.")

    conversation = message.conversation
    # BEFORE the edit, while `message.created`/`message.id` still describe
    # where in the thread this row sits.
    is_first = not (
        ChatMessage.objects.for_user(request.user)
        .filter(conversation=conversation)
        .filter(Q(created__lt=message.created) | Q(created=message.created, id__lt=message.id))
        .exists()
    )
    _messages_after(request.user, message).delete()

    # Attachments survive the edit: the file is what it always was, only the
    # words about it changed. Every text block is replaced, never appended
    # to — an edit that left the old question in place would send the model
    # both versions and get an answer to neither.
    kept = [b for b in message.blocks() if isinstance(b, dict) and b.get("type") != "text"]
    message.content = kept + [{"type": "text", "text": text[:8000]}]
    message.save(update_fields=["content"])

    # A rewound FIRST message makes the conversation's title stale — it was
    # derived from words that no longer exist. Blanking it here is what makes
    # the turn below treat this as a first message again and re-title it
    # (agent._retitle_if_first_message), which is also why the title is not
    # saved separately: the turn saves the conversation itself.
    if is_first:
        conversation.title = ""

    if request.POST.get("stream"):
        return _sse(request, conversation, text, [], [], resume=True)
    agent.run_turn(request.user, conversation, text, resume=True)
    return redirect("assistant:chat_conversation", conversation_id=conversation.id)


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
@require_GET
def credits_fragment(request: HttpRequest) -> HttpResponse:
    """The composer's credit meter, GET-able on its own — same reason
    `history_fragment` exists: a streamed turn's SSE response isn't HTML the
    client can re-render `_thread.html`'s meter from, so chat.html's JS
    calls this once a stream finishes and writes the number in directly."""
    return JsonResponse({"balance": billing_credits.balance(request.user)})


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


@login_required
@require_POST
def log_draft_touch(request: HttpRequest) -> HttpResponse:
    """Log the touch a drafted message would be, in one click, with no model
    round trip at all.

    This is the whole point of the draft card. The advisor used to END a draft
    by ASKING ("want me to log this as a touch once you've sent it?"), which
    is a question arriving twenty minutes before the moment it's about — the
    student still has to switch to Gmail, paste, send, and come back, by which
    time the question is three messages up the thread. The chip sits on the
    draft instead and waits.

    NOTHING HERE IS TAKEN ON TRUST. The message and the contact are both
    fetched through `for_user`, so another student's ids 404 exactly like
    everywhere else in this app. `kind` and `channel` are checked against the
    same enums `crm.views.log_touch` and `assistant.tools._log_touch` check
    against. And the message itself must actually CONTAIN a draft addressed to
    that contact — the chip is a shortcut for a write the page already offered
    on screen, not a general-purpose "log anything" endpoint that happens to
    be POST-only.

    IDEMPOTENT, because a double-click and a retried request are both normal.
    The `[assistant:<message_id>]` marker is the identity: if a touch already
    carries this draft's marker, the student gets the same "Logged" chip back
    and the CRM gets nothing new. That marker is the same one the model's own
    `log_touch` tool writes, so a later audit of "what did the advisor do to
    my network" reads both paths identically.
    """
    message = get_object_or_404(
        ChatMessage.objects.for_user(request.user), pk=_posted_int(request, "message") or 0
    )
    contact = get_object_or_404(
        Contact.objects.for_user(request.user), pk=_posted_int(request, "contact") or 0
    )
    kind = (request.POST.get("kind") or "").strip()
    channel = (request.POST.get("channel") or "").strip()
    if kind not in TOUCH_TRANSITIONS or channel not in CHANNELS:
        return HttpResponseBadRequest("Unknown interaction kind or channel.")

    drafted_for = {
        seg["contact_id"]
        for seg in drafts_mod.split(message.text)
        if seg["type"] == "draft" and seg["contact_id"]
    }
    if contact.id not in drafted_for:
        return HttpResponseBadRequest("That message holds no draft for this contact.")

    marker = drafts_mod.marker_for(message.id)
    already = Touch.objects.for_user(request.user).filter(contact=contact, note__contains=marker).exists()
    if not already:
        services.log_touch(request.user.id, contact.id, kind, channel, marker, source="assistant")

    return render(
        request,
        "assistant/_draft_chip.html",
        {"draft": {"logged": True, "loggable": True, "contact_name": contact.name}},
    )


@login_required
@require_POST
def forget_memory(request: HttpRequest) -> HttpResponse:
    """Delete one remembered fact. No confirm() here, unlike a chat/folder
    delete — this is one sentence the model can re-learn in thirty seconds
    if it turns out to still be true, not a conversation's worth of
    history; the asymmetry in AdvisorMemory's own docstring is why this
    write skips it and delete_conversation doesn't."""
    memory = get_object_or_404(
        AdvisorMemory.objects.for_user(request.user), pk=_posted_int(request, "memory") or 0
    )
    memory.delete()
    return render(
        request, "assistant/_memories.html", {"memories": AdvisorMemory.objects.for_user(request.user)}
    )
