"""Draft fences: the one piece of structure the advisor's prose is allowed.

WHY A FENCE AT ALL. When the model writes a follow-up email for a student it
used to arrive as one more paragraph of chat — no Subject, no boundary, and
the offer to log the outreach as a CRM touch was a *question* ("want me to log
this?") that scrolled out of view long before the student had actually pasted
the thing into Gmail and hit send. So a finished draft now comes back inside a
fence the page can recognise and render as a card, with the two actions that
moment actually needs (copy it, log it) sitting on the card itself, still there
on the next page load.

THE SYNTAX, and why it is this and not Markdown:

    ```draft contact=482 channel=email kind=follow_up
    Subject: Catching up + ICC outreach

    Hi Yumna,
    ...
    ```

A fenced block whose info string carries the contact the draft is FOR and the
touch it would BE. `draft` immediately after the backticks is what keeps a
stray code fence elsewhere in a reply from being mistaken for one of these.
This is not a Markdown renderer and does not want to become one — the sibling
`templatetags/assistant_extras.py` has the same posture for the same reason.

EVERYTHING HERE FAILS SOFT. A fence with a garbled info string still renders
as a card, just without the log-touch chip (there is nothing to log against).
A fence with nothing in it, or one the model never closed, is left alone as
ordinary prose — visible, ugly, and honest. Nothing in this module raises, and
nothing in it can swallow a message: the student always sees every word the
model wrote, card or no card.

Parsing runs on the RAW model text, before any escaping. That ordering is
deliberate: `escape()` rewrites quotes into entities, and an info string
parsed after that would have to know about `&quot;`. Each piece this module
returns is escaped downstream — the prose through `chat_format`, the draft's
own subject and body through the template's autoescaping.
"""

from __future__ import annotations

import re

from coverage_domain.pipeline import CHANNELS, TOUCH_TRANSITIONS

# ```draft <info>\n <body> \n``` — the closing fence must own its whole line,
# so a run of backticks inside the draft body can never end it early.
_FENCE_RE = re.compile(
    r"(?ms)^```draft[ \t]*(?P<info>[^\n]*)\n(?P<body>.*?)^```[ \t]*$",
)

# `key=value` pairs in the info string. Anything that isn't one is ignored
# rather than treated as an error — an info string the model half-wrote should
# cost the chip, not the card.
_ATTR_RE = re.compile(r"([A-Za-z_]+)=([\w.-]+)")

_SUBJECT_RE = re.compile(r"^Subject:[ \t]*(.*)$")

# ---------------------------------------------------------------------------
# The template guard. A card promises "paste this and send it", and the two
# things that make that promise false are cheap to see without a model: a
# placeholder the student was supposed to fill in (`[Firm]`, `{name}`,
# `[Your Name]`) and a body long enough that nobody at a bank reads past
# the first screen of it. `agent.SYSTEM_PROMPT` states both rules to the
# model; this is the page keeping them when the model does not. A block
# that trips either is rendered as ordinary prose — every word still
# visible, nothing to copy with one click.
#
# The caps are the prompt's own numbers, keyed on the fence's `kind`. A kind
# the info string did not name (or named wrongly) gets the looser cap: an
# unknown kind already costs the block its chip, and guessing "follow_up"
# to apply the tighter cap would punish the same mistake twice.
# ---------------------------------------------------------------------------
WORD_CAPS = {"outreach": 120, "follow_up": 80, "thank_you": 80}
DEFAULT_WORD_CAP = 120

# `[anything]` or `{anything}` on one line, up to 40 chars, no nesting. Long
# enough for "[hiring manager's name]", short enough that a bracketed aside
# spanning a sentence is not mistaken for a slot.
_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{1,40}\]|\{[^{}\n]{1,40}\}")

FLAG_PLACEHOLDER = "placeholder"
FLAG_TOO_LONG = "too_long"


def flag_reason(subject: str, body: str, kind: str) -> str | None:
    """Why this draft may not be a card — `FLAG_PLACEHOLDER`,
    `FLAG_TOO_LONG` — or None when it is fine. `kind` is the parsed fence
    kind ("" when the fence did not name one). Kept as its own function so
    the JS mirror in chat.html and the tests can pin the same rule."""
    if _PLACEHOLDER_RE.search(f"{subject}\n{body}"):
        return FLAG_PLACEHOLDER
    if len(body.split()) > WORD_CAPS.get(kind, DEFAULT_WORD_CAP):
        return FLAG_TOO_LONG
    return None


def _as_prose(subject: str, body: str) -> str:
    """A flagged draft as the plain text the prose run absorbs: the Subject
    line and the body, fence markers dropped. The student still reads every
    word the model wrote — they just do not get a Copy button for it."""
    lines = []
    if subject:
        lines.append(f"Subject: {subject}")
    if body:
        lines.append(body)
    return "\n\n" + "\n\n".join(lines) + "\n\n"


def _parse_info(info: str) -> dict:
    attrs = dict(_ATTR_RE.findall(info or ""))

    contact_id = None
    raw_contact = attrs.get("contact", "")
    if raw_contact.isdigit():
        contact_id = int(raw_contact)

    channel = attrs.get("channel", "")
    kind = attrs.get("kind", "")
    # An unknown channel or kind is the same as none: the chip writes a real
    # Touch through the real ratchet, and the enums it validates against are
    # the ones `crm.views.log_touch` and `assistant.tools._log_touch` already
    # validate against. A value outside them is not something to guess at.
    if channel not in CHANNELS:
        channel = ""
    if kind not in TOUCH_TRANSITIONS:
        kind = ""

    return {"contact_id": contact_id, "channel": channel, "kind": kind}


def _parse_body(raw: str) -> tuple[str, str] | None:
    """`(subject, body)` for a usable draft, or None if there's nothing here.

    The subject is optional on purpose — a LinkedIn message or an in-thread
    reply genuinely has none, and refusing to render those as cards would put
    exactly the drafts a student most wants to copy back into loose prose.
    """
    lines = raw.strip("\n").split("\n")
    subject = ""
    match = _SUBJECT_RE.match(lines[0].strip()) if lines else None
    if match:
        subject = match.group(1).strip()
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)

    body = "\n".join(lines).strip("\n")
    if not body and not subject:
        return None
    return subject, body


def split(text: str) -> list[dict]:
    """One message as an ordered list of segments the template can render.

    Every segment is a dict with a "type":

      - "prose" {"text": str} — ordinary model prose, for `chat_format`.
      - "draft" {"subject", "body", "contact_id", "channel", "kind"} — a
        finished draft, for the card. `contact_id` is None (and `channel`/
        `kind` are "") whenever the info string didn't name a valid one; the
        card still renders, just with Copy and no chip.

    A message with no fence in it comes back as exactly one prose segment, so
    the caller never needs a separate "did this have a draft" branch.

    A fence that trips `flag_reason` (a bracketed placeholder, a body over
    its word cap) is not a draft either: its Subject and body join the
    surrounding prose with the fence markers dropped, so the student reads
    what was written and gets no card to copy a template from. Whole
    segments, not a flag on a card, because the stream path's JS mirror
    (chat.html) pairs its cards with this function's draft segments by
    index — a card that exists on one side and not the other would hang the
    next card's log-touch chip on the wrong person.
    """
    segments: list[dict] = []
    cursor = 0
    # Prose that flagged fences have already handed back, waiting to be
    # joined with whatever plain text follows them.
    carry = ""
    for match in _FENCE_RE.finditer(text or ""):
        parsed = _parse_body(match.group("body"))
        if parsed is None:
            # An empty fence is not a draft. Leaving it inside the prose run
            # means the student still sees whatever the model actually wrote.
            continue
        subject, body = parsed
        info = _parse_info(match.group("info"))
        if flag_reason(subject, body, info["kind"]):
            carry += text[cursor : match.start()] + _as_prose(subject, body)
            cursor = match.end()
            continue
        before = (carry + text[cursor : match.start()]).strip("\n")
        carry = ""
        if before:
            segments.append({"type": "prose", "text": before})
        segments.append({"type": "draft", "subject": subject, "body": body, **info})
        cursor = match.end()

    rest = carry + (text or "")[cursor:]
    if segments or carry:
        rest = rest.strip("\n")
    if rest or not segments:
        segments.append({"type": "prose", "text": rest})
    return segments


def marker_for(message_id) -> str:
    """The note prefix that ties a Touch back to the message it was logged
    from. Byte-identical to the one `assistant.tools._log_touch` writes for a
    touch the MODEL logged, so the two paths are indistinguishable in a
    student's own history — and so "has this draft already been logged" is a
    substring query rather than a join. The closing bracket is load-bearing:
    without it `[assistant:4` would match message 42 as well as message 4.
    """
    return f"[assistant:{message_id}]"
