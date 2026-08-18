"""Turning an uploaded file into a Messages-API content block.

Claude's API accepts exactly three shapes of file input relevant here: an
`image` block (base64 — png/jpeg/gif/webp), a `document` block (base64 —
PDF only, read a page at a time internally), and nothing else. For anything
that isn't one of those but is still obviously useful in a recruiting chat —
a resume kept as .txt, a CSV export of contacts — this inlines the file's
own text as a `text` block prefixed with its name, since there is no
distinct "plain file" type in the API to reach for instead.

LIMITS, chosen here (nothing upstream — Django, Anthropic — enforces these
for us):

  - Images: 5 MB each. Anthropic's own API limit for an image block; a
    larger one is simply rejected server-side by them, so refusing early is
    strictly better than paying the base64 encode for nothing.
  - PDFs: 10 MB. Comfortably past any resume or one-pager, well short of
    "a whole deck" — and PDFs are billed per PAGE once decoded, so the real
    cost control is this ceiling, not a generous one.
  - Text (.txt/.csv/.md): 2 MB. Generous for a resume or a contacts export;
    only the first 50,000 characters are actually inlined, since past that
    it is competing with the conversation itself for context.
  - 3 files per message, 20 MB combined. Three is already more than one
    turn's worth of reading; the combined cap exists so three
    just-under-the-line files can't stack into one enormous request.

FAILURE POSTURE: never raises. A file that doesn't qualify becomes a plain
English reason in the `errors` list instead of a block — the caller's job is
to show those to the student and skip the API call entirely for this turn,
the same "capped"/"unconfigured" shape every other refusal on this page
already has. Bad input is the student's mistake, not a 500.
"""

from __future__ import annotations

import base64

MAX_FILES_PER_MESSAGE = 3
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_TEXT_INLINE_CHARS = 50_000

_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_PDF_BYTES = 10 * 1024 * 1024
_MAX_TEXT_BYTES = 2 * 1024 * 1024
_TEXT_EXTENSIONS = (".txt", ".csv", ".md")

# What the composer's file picker offers, and what the server actually
# honours — kept as one tuple so the two can never quietly drift apart.
ACCEPT_ATTRIBUTE = (
    "image/png,image/jpeg,image/gif,image/webp,application/pdf,.pdf,.txt,.csv,.md,text/plain,text/csv"
)


def _kind_for(upload) -> str:
    content_type = (upload.content_type or "").lower()
    name = (upload.name or "").lower()
    if content_type in _IMAGE_CONTENT_TYPES:
        return "image"
    if content_type == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if name.endswith(_TEXT_EXTENSIONS) or content_type.startswith("text/"):
        return "text"
    return "unknown"


def blocks_for(uploads: list) -> tuple[list[dict], list[str]]:
    """`uploads` is whatever `request.FILES.getlist(...)` handed back.
    Returns `(blocks, errors)` — `blocks` go straight into the turn's first
    content-block list; `errors`, if non-empty, mean the caller should show
    them and skip the API call, not send a partial set of attachments."""
    if not uploads:
        return [], []
    if len(uploads) > MAX_FILES_PER_MESSAGE:
        return [], [f"Only {MAX_FILES_PER_MESSAGE} files at a time — you attached {len(uploads)}."]

    blocks: list[dict] = []
    errors: list[str] = []
    total_bytes = 0
    limits = {"image": _MAX_IMAGE_BYTES, "pdf": _MAX_PDF_BYTES, "text": _MAX_TEXT_BYTES}

    for upload in uploads:
        kind = _kind_for(upload)
        limit = limits.get(kind)
        if limit is None:
            errors.append(f"{upload.name}: that file type isn't supported (images, PDFs, or .txt/.csv/.md).")
            continue
        if upload.size > limit:
            errors.append(
                f"{upload.name}: {upload.size / 1_048_576:.1f} MB is over the "
                f"{limit // 1_048_576} MB limit for this file type."
            )
            continue
        total_bytes += upload.size
        if total_bytes > MAX_TOTAL_BYTES:
            errors.append(
                f"{upload.name}: pushes this message's attachments over "
                f"{MAX_TOTAL_BYTES // 1_048_576} MB combined."
            )
            continue

        data = upload.read()
        if kind == "text":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{upload.name}: couldn't read this as text.")
                continue
            blocks.append(
                {"type": "text", "text": f"[Attached file: {upload.name}]\n\n{text[:MAX_TEXT_INLINE_CHARS]}"}
            )
            continue

        encoded = base64.b64encode(data).decode("ascii")
        if kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": upload.content_type, "data": encoded},
                    # Not read by the API — carried through so the thread
                    # can say what was attached without guessing.
                    "_filename": upload.name,
                }
            )
        else:  # pdf
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
                    "_filename": upload.name,
                }
            )

    return blocks, errors


def strip_private_fields(blocks: list[dict]) -> list[dict]:
    """`_filename` (see above) is this app's own bookkeeping, not part of the
    Messages API shape — the API rejects unknown keys on a content block, so
    it must never reach `messages.create`/`messages.stream`, only storage
    and the thread template."""
    cleaned = []
    for block in blocks:
        if isinstance(block, dict) and "_filename" in block:
            block = {k: v for k, v in block.items() if not k.startswith("_")}
        cleaned.append(block)
    return cleaned


def stub_old_blocks(blocks: list[dict]) -> list[dict]:
    """Replace an image/document block with a lightweight text stand-in —
    the filename, nothing else. `agent._api_messages` calls this on every
    turn EXCEPT the most recent one that carried an attachment: without it,
    a PDF attached early in a long-lived conversation (a folder is exactly
    the standing invitation to keep one alive for weeks) gets re-sent, and
    re-billed per page, on every later turn for as long as it sits inside
    the replay window. Once the model has actually answered a question
    about an attachment, the conversation rarely needs the original bytes
    again — the text of what it said about it is already in the transcript.
    """
    stubbed = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in ("image", "document") and block.get("_filename"):
            stubbed.append({"type": "text", "text": f"[{block['_filename']} was attached earlier in this conversation.]"})
        else:
            stubbed.append(block)
    return stubbed
