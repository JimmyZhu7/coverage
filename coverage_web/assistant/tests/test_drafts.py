"""Draft fences: parsing them, rendering them, and failing soft on them.

The fence is the only structure the advisor's prose carries, and the model
writes it unsupervised, so the cases that matter here are the ones where it
writes it slightly wrong: a fence it never closed, a fence with nothing in it,
an info string with a contact id it made up. None of those may lose a single
word the model wrote, and none of them may put unescaped model output into the
page.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from assistant import drafts
from assistant.models import ChatConversation, ChatMessage
from crm.models import Contact, Touch
from directory.models import Firm

User = get_user_model()


def _draft_block(info: str, body: str) -> str:
    return f"```draft {info}\n{body}\n```"


_SCRIPTS = re.compile(r"(?s)<script.*?</script>")


def _markup(response) -> str:
    """The page with its inline JS stripped.

    chat.html carries a JS mirror of the fence parser AND of the chip's own
    markup (it has to — a streamed reply is never re-fetched from the
    server), so those strings are present in the page source whether or not a
    chip actually rendered. Any assertion about what is NOT on the page has
    to read the markup alone.
    """
    return _SCRIPTS.sub("", response.content.decode())


# ---------------------------------------------------------------------------
# 1. The parser
# ---------------------------------------------------------------------------
def test_a_well_formed_fence_splits_into_prose_then_draft_then_prose():
    """The shape a real reply takes: a line of framing, the finished email,
    and a note about when to send it. All three survive, in order."""
    text = (
        "Here's the follow-up.\n\n"
        + _draft_block(
            "contact=482 channel=email kind=follow_up",
            "Subject: Catching up\n\nHi Yumna,\nHope the summer went well.\n\nBest,\nJimmy",
        )
        + "\n\nSend it Tuesday morning."
    )

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose", "draft", "prose"]
    assert segments[0]["text"] == "Here's the follow-up."
    assert segments[2]["text"] == "Send it Tuesday morning."
    draft = segments[1]
    assert draft["subject"] == "Catching up"
    assert draft["body"] == "Hi Yumna,\nHope the summer went well.\n\nBest,\nJimmy"
    assert (draft["contact_id"], draft["channel"], draft["kind"]) == (482, "email", "follow_up")


def test_a_reply_with_no_fence_is_exactly_one_prose_segment():
    """The 99% case, and the reason the template needs no "did this have a
    draft" branch: an ordinary answer comes back in the same shape."""
    assert drafts.split("Chase Morgan Stanley this week.") == [
        {"type": "prose", "text": "Chase Morgan Stanley this week."}
    ]


def test_a_draft_with_no_subject_still_renders_as_a_draft():
    """A LinkedIn message has no subject line, and refusing to card those
    would push exactly the drafts a student most wants to copy back into
    loose prose."""
    segments = drafts.split(_draft_block("contact=7 channel=linkedin kind=outreach", "Hi Sam, quick note."))

    assert len(segments) == 1
    assert segments[0]["subject"] == ""
    assert segments[0]["body"] == "Hi Sam, quick note."
    assert segments[0]["channel"] == "linkedin"


def test_a_fence_with_no_contact_key_still_cards_but_names_nobody():
    """The model wrote a draft for someone it never looked up. Worth
    rendering, not worth offering to log against a guess."""
    segments = drafts.split(_draft_block("channel=email kind=outreach", "Subject: Hi\n\nHello."))

    assert segments[0]["type"] == "draft"
    assert segments[0]["contact_id"] is None


def test_an_unknown_channel_or_kind_is_dropped_rather_than_trusted():
    """The chip writes a real Touch through the real ratchet. A value outside
    the enums crm.views.log_touch validates against is not something to guess
    at, so it becomes "" and the chip never appears."""
    segments = drafts.split(_draft_block("contact=5 channel=carrier_pigeon kind=vibes", "Subject: Hi\n\nHello."))

    assert segments[0]["contact_id"] == 5
    assert segments[0]["channel"] == ""
    assert segments[0]["kind"] == ""


def test_a_stray_code_fence_is_never_mistaken_for_a_draft():
    """`draft` immediately after the backticks is the whole guard."""
    text = "Try this:\n\n```python\nprint(1)\n```\n\nThat's it."

    assert [s["type"] for s in drafts.split(text)] == ["prose"]
    assert "```python" in drafts.split(text)[0]["text"]


def test_a_fence_the_model_never_closed_stays_ordinary_prose():
    """Fail soft, and specifically fail VISIBLE: the student still reads
    every word, backticks and all, rather than watching half a draft
    disappear."""
    text = "Here you go.\n```draft contact=1 channel=email kind=outreach\nSubject: Hi\n\nHello."

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose"]
    assert segments[0]["text"] == text


def test_an_empty_fence_is_not_a_draft_and_eats_nothing():
    text = "Note this.\n```draft contact=1 channel=email kind=outreach\n\n```\nDone."

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose"]
    assert "```draft" in segments[0]["text"]
    assert segments[0]["text"].endswith("Done.")


def test_two_drafts_in_one_reply_both_come_back_in_order():
    text = (
        _draft_block("contact=1 channel=email kind=outreach", "Subject: One\n\nFirst.")
        + "\nand\n"
        + _draft_block("contact=2 channel=linkedin kind=follow_up", "Subject: Two\n\nSecond.")
    )

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["draft", "prose", "draft"]
    assert [s["contact_id"] for s in segments if s["type"] == "draft"] == [1, 2]


def test_the_touch_marker_brackets_the_id_so_message_4_is_not_message_42():
    """The whole "already logged" query is a substring match on this string.
    Without the closing bracket, `[assistant:4` would match message 42 too and
    a fresh draft would render as already logged."""
    assert drafts.marker_for(4) == "[assistant:4]"
    assert drafts.marker_for(4) not in drafts.marker_for(42)


# ---------------------------------------------------------------------------
# 2. Rendered into the page
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="pw12345!")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def contact(user):
    firm = Firm.objects.create(slug="north-bank", name="North Bank")
    row = Contact(user=user, firm=firm, name="Yumna Rahman", role="Associate")
    row.save()
    return row


@pytest.fixture
def conversation(user):
    row = ChatConversation(user=user, title="Follow-ups")
    row.save()
    return row


def _reply(user, conversation, text):
    message = ChatMessage(
        user=user,
        conversation=conversation,
        role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "text", "text": text}],
    )
    message.save()
    return message


def test_a_drafted_email_renders_as_a_card_not_a_paragraph(signed_in, user, conversation, contact):
    """The change this whole feature is: a Subject that reads as a Subject, a
    body under it, and the two actions on the card itself."""
    _reply(
        user,
        conversation,
        "Here it is.\n\n"
        + _draft_block(
            f"contact={contact.id} channel=email kind=follow_up",
            "Subject: Catching up\n\nHi Yumna,\n\nBest,\nJimmy",
        ),
    )

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert 'class="as-draft"' in body
    assert "Catching up" in body
    assert "Log touch · Yumna Rahman · Email" in body
    assert "as-draft-copy" in body
    # The prose around it is untouched, still in an ordinary body div.
    assert '<div class="as-body">Here it is.</div>' in body


def test_html_inside_a_draft_is_escaped_not_executed(signed_in, user, conversation, contact):
    """A draft is untrusted model output rendered as HTML, and the model is
    quoting a job posting somebody else wrote. Escape first, always."""
    _reply(
        user,
        conversation,
        _draft_block(
            f"contact={contact.id} channel=email kind=outreach",
            "Subject: <script>alert(1)</script>\n\nHi <b>there</b>",
        ),
    )

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "Hi <b>there</b>" not in body
    assert "&lt;b&gt;there&lt;/b&gt;" in body


def test_a_draft_with_no_contact_offers_copy_and_no_chip(signed_in, user, conversation):
    _reply(user, conversation, _draft_block("channel=email kind=outreach", "Subject: Hi\n\nHello."))

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert 'class="as-draft"' in body
    assert "as-draft-copy" in body
    assert "Log touch" not in body


def test_a_draft_for_a_contact_the_student_does_not_have_loses_only_its_chip(
    signed_in, user, conversation
):
    """A contact deleted since the draft was written, or an id the model
    invented. The words are still worth reading; the write isn't safe."""
    _reply(user, conversation, _draft_block("contact=999999 channel=email kind=outreach", "Subject: Hi\n\nHello."))

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert 'class="as-draft"' in body
    assert "Log touch" not in body


def test_an_unclosed_fence_reaches_the_page_as_plain_text_never_a_500(signed_in, user, conversation):
    _reply(user, conversation, "Here you go.\n```draft contact=1 channel=email kind=outreach\nSubject: Hi")

    response = signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id]))

    assert response.status_code == 200
    body = _markup(response)
    assert 'class="as-draft"' not in body
    assert "Here you go." in body


def test_a_student_pasting_a_fence_is_pasting_text_not_writing_a_draft(signed_in, user, conversation):
    """Segments are built for assistant turns only. A student who pastes a
    fence back in gets their own words in their own bubble."""
    message = ChatMessage(
        user=user,
        conversation=conversation,
        role=ChatMessage.ROLE_USER,
        content=[{"type": "text", "text": _draft_block("contact=1 channel=email kind=outreach", "Subject: Hi\n\nHello.")}],
    )
    message.save()

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert 'class="as-draft"' not in body
    assert "```draft" in body


# ---------------------------------------------------------------------------
# 3. "Already logged" on a fresh render
# ---------------------------------------------------------------------------
def test_a_reload_shows_logged_without_re_offering_the_button(signed_in, user, conversation, contact):
    """The reason the state lives in the CRM and not in the click: a student
    logs the touch, closes the tab, and comes back. The chip must already
    know."""
    message = _reply(
        user,
        conversation,
        _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: Hi\n\nHello."),
    )
    Touch(
        user=user, contact=contact, ts=timezone.now(), kind="follow_up", channel="email",
        note=drafts.marker_for(message.id), source="assistant",
    ).save()

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert "Logged" in body
    assert "Log touch ·" not in body


def test_an_unlogged_draft_still_offers_the_button(signed_in, user, conversation, contact):
    _reply(
        user,
        conversation,
        _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: Hi\n\nHello."),
    )

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert "Log touch · Yumna Rahman · Email" in body
    assert ">\n    Logged" not in body


def test_a_touch_from_another_draft_never_marks_this_one_logged(
    signed_in, user, conversation, contact
):
    """The marker is per-message, so two drafts to the same person in the same
    thread are two separate decisions."""
    first = _reply(
        user, conversation,
        _draft_block(f"contact={contact.id} channel=email kind=outreach", "Subject: One\n\nHi."),
    )
    _reply(
        user, conversation,
        _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: Two\n\nAgain."),
    )
    Touch(
        user=user, contact=contact, ts=timezone.now(), kind="outreach", channel="email",
        note=drafts.marker_for(first.id), source="assistant",
    ).save()

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert "Logged" in body
    assert "Log touch · Yumna Rahman · Email" in body


def test_the_logged_lookup_is_batched_not_one_query_per_message(
    signed_in, user, conversation, contact, django_assert_num_queries
):
    """A thread with ten drafted emails in it must not cost ten touch
    queries. Names come from one `pk__in`; logged state from one OR of
    markers. This test is the regression guard on that shape — it counts the
    queries `_thread_rows` itself runs."""
    from assistant import views as assistant_views

    for _ in range(10):
        _reply(
            user,
            conversation,
            _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: Hi\n\nHello."),
        )

    # 1 for the messages, 1 for the contact names, 1 for the touch markers.
    with django_assert_num_queries(3):
        rows = assistant_views._thread_rows(user, conversation)

    assert len(rows) == 10
    assert all(seg.get("loggable") for row in rows for seg in row["segments"] if seg["type"] == "draft")


# ---------------------------------------------------------------------------
# 4. What the streaming path is handed
# ---------------------------------------------------------------------------
def test_the_stream_gets_the_draft_metadata_the_tokens_cannot_carry(user, conversation, contact):
    """A streamed reply is drawn client-side from tokens and never re-fetched,
    so the browser has the draft's words but not the contact's NAME or its
    logged state. Those ride the terminal "done" frame instead."""
    from assistant import views as assistant_views

    message = _reply(
        user,
        conversation,
        "Here it is.\n\n"
        + _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: Hi\n\nHello."),
    )

    metadata = assistant_views._draft_segments(user, message.id)

    assert len(metadata) == 1
    assert metadata[0]["contact_name"] == "Yumna Rahman"
    assert metadata[0]["channel_label"] == "Email"
    assert metadata[0]["loggable"] is True
    assert metadata[0]["logged"] is False
    assert metadata[0]["message_id"] == message.id


def test_another_students_message_id_yields_no_draft_metadata(user, contact):
    """The stream frame is built from a POSTed conversation, so the lookup
    that fills it is scoped like every other on this page."""
    from assistant import views as assistant_views

    other = User.objects.create_user(email="stream-other@example.com", password="pw12345!")
    their_conversation = ChatConversation(user=other)
    their_conversation.save()
    theirs = _reply(
        other, their_conversation,
        _draft_block("contact=1 channel=email kind=outreach", "Subject: Hi\n\nHello."),
    )

    assert assistant_views._draft_segments(user, theirs.id) == []


def test_two_drafts_to_different_people_in_one_reply_are_logged_separately(
    signed_in, user, conversation, contact
):
    """One reply can carry a note to two people. The `[assistant:<id>]` marker
    only names the MESSAGE, so keying the logged state on that alone would let
    logging one of them quietly mark the other done. The Touch's own contact
    is what separates them."""
    other = Contact(user=user, firm=contact.firm, name="Sam Okafor", role="Analyst")
    other.save()
    message = _reply(
        user,
        conversation,
        _draft_block(f"contact={contact.id} channel=email kind=follow_up", "Subject: One\n\nHi Yumna.")
        + "\nand for Sam:\n"
        + _draft_block(f"contact={other.id} channel=email kind=outreach", "Subject: Two\n\nHi Sam."),
    )
    Touch(
        user=user, contact=contact, ts=timezone.now(), kind="follow_up", channel="email",
        note=drafts.marker_for(message.id), source="assistant",
    ).save()

    body = _markup(signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])))

    assert "Logged" in body
    # Sam's draft is untouched and still offers its own button.
    assert "Log touch · Sam Okafor · Email" in body
    assert "Log touch · Yumna Rahman · Email" not in body


# ---------------------------------------------------------------------------
# 3. The template guard
#
# A card promises "paste this and send it". A bracketed placeholder or a body
# past its word cap makes that promise false, and the page can see both
# without a model: the block is rendered as prose — every word still there,
# no Copy button. `flag_reason`, `WORD_CAPS` and `_PLACEHOLDER_RE` are the
# rule; chat.html's JS mirror carries the same three (see the last test).
# ---------------------------------------------------------------------------
def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_a_draft_with_a_bracketed_placeholder_is_prose_not_a_card():
    text = (
        "Here's a first approach.\n\n"
        + _draft_block(
            "contact=482 channel=email kind=outreach",
            "Subject: Intro from a sophomore\n\nHi [Name],\n\nI study at [School].\n\nBest,\nJimmy",
        )
        + "\n\nSend it Tuesday."
    )

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose"]
    prose = segments[0]["text"]
    # Every word survives, the fence markers do not.
    assert "Subject: Intro from a sophomore" in prose
    assert "Hi [Name]," in prose
    assert "Send it Tuesday." in prose
    assert "```" not in prose


def test_a_curly_brace_placeholder_is_caught_too():
    text = _draft_block("contact=1 channel=email kind=outreach", "Hi {first_name},\n\nBest,\nJimmy")

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose"]
    assert "{first_name}" in segments[0]["text"]


def test_a_follow_up_over_eighty_words_is_prose_not_a_card():
    long_body = "Subject: Following up\n\nHi Yumna,\n\n" + _words(85) + "\n\nBest,\nJimmy"
    text = _draft_block("contact=1 channel=email kind=follow_up", long_body)

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose"]
    assert "w84" in segments[0]["text"]


def test_a_follow_up_under_the_cap_is_still_a_card():
    body = "Subject: Following up\n\nHi Yumna,\n\n" + _words(70) + "\n\nBest,\nJimmy"

    segments = drafts.split(_draft_block("contact=1 channel=email kind=follow_up", body))

    assert [s["type"] for s in segments] == ["draft"]
    assert segments[0]["contact_id"] == 1


def test_a_first_approach_gets_the_looser_cap():
    body = "Subject: Intro\n\nHi Yumna,\n\n" + _words(110) + "\n\nBest,\nJimmy"
    within = _draft_block("contact=1 channel=email kind=outreach", body)
    over = _draft_block(
        "contact=1 channel=email kind=outreach",
        "Subject: Intro\n\nHi Yumna,\n\n" + _words(125) + "\n\nBest,\nJimmy",
    )

    assert [s["type"] for s in drafts.split(within)] == ["draft"]
    assert [s["type"] for s in drafts.split(over)] == ["prose"]


def test_a_thank_you_shares_the_follow_ups_cap():
    over = _draft_block(
        "contact=1 channel=email kind=thank_you",
        "Subject: Thank you\n\nHi Yumna,\n\n" + _words(85) + "\n\nBest,\nJimmy",
    )

    assert [s["type"] for s in drafts.split(over)] == ["prose"]


def test_an_unknown_kind_gets_the_default_cap_not_the_tight_one():
    """An unknown kind already costs the block its chip; guessing
    `follow_up` to apply the tighter cap would punish the same mistake
    twice."""
    body = "Subject: Hello\n\nHi Yumna,\n\n" + _words(100) + "\n\nBest,\nJimmy"

    segments = drafts.split(_draft_block("contact=1 channel=email", body))

    assert [s["type"] for s in segments] == ["draft"]
    assert segments[0]["kind"] == ""


def test_a_flagged_first_draft_does_not_shift_the_second_drafts_identity():
    """The stream path pairs cards with these segments BY INDEX. A flagged
    block must vanish from the draft list entirely, so the next real card
    still carries its own contact — not the flagged one's."""
    text = (
        _draft_block("contact=1 channel=email kind=outreach", "Subject: One\n\nHi [Name],\n\nBest,\nJimmy")
        + "\nand for Sam:\n"
        + _draft_block("contact=2 channel=email kind=outreach", "Subject: Two\n\nHi Sam,\n\nBest,\nJimmy")
    )

    segments = drafts.split(text)

    assert [s["type"] for s in segments] == ["prose", "draft"]
    assert segments[1]["contact_id"] == 2
    assert "Hi [Name]," in segments[0]["text"]
    assert "and for Sam:" in segments[0]["text"]


def test_a_flagged_draft_with_prose_after_it_keeps_that_prose():
    text = (
        "Before.\n\n"
        + _draft_block("contact=1 channel=email kind=outreach", "Hi [Name],")
        + "\n\nAfter."
    )

    segments = drafts.split(text)

    assert len(segments) == 1
    assert segments[0]["text"].startswith("Before.")
    assert segments[0]["text"].endswith("After.")


def test_flag_reason_names_the_rule_it_tripped():
    assert drafts.flag_reason("", "Hi [Firm] team,", "outreach") == drafts.FLAG_PLACEHOLDER
    assert drafts.flag_reason("Intro [draft]", "Hi Yumna,", "outreach") == drafts.FLAG_PLACEHOLDER
    assert drafts.flag_reason("", _words(81), "follow_up") == drafts.FLAG_TOO_LONG
    assert drafts.flag_reason("", _words(80), "follow_up") is None
    assert drafts.flag_reason("", _words(121), "outreach") == drafts.FLAG_TOO_LONG
    assert drafts.flag_reason("", _words(120), "outreach") is None
    assert drafts.flag_reason("Catching up", "Hi Yumna,\n\nGood to see you Tuesday.\n\nBest,\nJimmy", "follow_up") is None


def test_a_bracketed_aside_longer_than_a_slot_is_not_a_placeholder():
    """The regex is for `[Firm]`-sized slots. A parenthetical aside in square
    brackets that runs a whole sentence is a stylistic choice, not a blank
    the student was meant to fill in."""
    aside = "[" + _words(12) + " which is well past the forty characters a slot gets]"
    assert drafts.flag_reason("", f"Hi Yumna, {aside}", "outreach") is None


def test_the_js_mirror_carries_the_same_caps_and_placeholder_rule():
    """chat.html re-implements the fence parser for the stream path and
    pairs its cards with `split`'s draft segments by index, so its guard
    has to agree with this one byte for byte on the two inputs that decide
    whether a block is a card."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "templates" / "assistant" / "chat.html").read_text()

    assert drafts._PLACEHOLDER_RE.pattern in source
    caps = ", ".join(f"{kind}: {cap}" for kind, cap in drafts.WORD_CAPS.items())
    assert f"DRAFT_WORD_CAPS = {{ {caps} }}" in source
    assert f"DRAFT_WORD_CAP_DEFAULT = {drafts.DEFAULT_WORD_CAP}" in source
