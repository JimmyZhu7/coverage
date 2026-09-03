"""Talk to Coverage's page-scoped CSS must keep two controls from being drawn
on top of each other, and must not hide a control from the keyboard.

Every rule pinned here was measured on the rendered page at a real viewport
before it was written, and each one failed that measurement first:

* `.as-folder-head` reserved no right padding, so `.as-folder-kebab` (24px
  wide, `right: 4px`) was positioned straight over `.as-folder-count`. At
  1280 the count occupied x 238.4-245 and the kebab x 229-253 -- the number
  100% covered, with `elementFromPoint` at the count's own centre returning
  the kebab's <svg>. The chat rows beside it already reserved 32px for the
  identical button (`.as-history-link`); the folder head is the one that
  didn't.
* `.as-empty` centred its content with no overflow rule, so on a phone the
  excess escaped the box in both directions instead of scrolling. Measured
  at 375x667 in the ordinary configured state: the box was 233px tall,
  `.as-starters` ended 46.8px below it, and those cards overlapped
  `.as-meta` by 34.8px, printing the credit meter through the fourth card.
* `.as-empty-mark` is a 44px circle that is also a column flex item, so
  `flex-shrink` was free to halve its height whenever the empty state was
  short -- measured 44px x 22px at 375x667, a `border-radius: 50%` ellipse.
* The two kebabs are `opacity: 0` until hovered, and opacity hides a focus
  ring exactly as well as it hides the button. Tabbing into the sidebar made
  `document.activeElement` a control with computed opacity "0".

These are text assertions against the page's own <style> block, the same
technique `test_icon_stroke_width.py` uses to pin a CSS convention that has
no other enforcement.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from assistant.models import ChatConversation, ChatFolder

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="pw12345!")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def page(signed_in, user):
    """The chat page with a folder rendered, so the folder rules are live."""
    folder = ChatFolder(user=user, name="BofA prep")
    folder.save()
    conversation = ChatConversation(user=user, folder=folder, title="Filed chat")
    conversation.save()
    response = signed_in.get(
        reverse("assistant:chat_conversation", args=[conversation.id])
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert 'class="as-folder"' in html, "folder markup did not render"
    return html


def _declaration(html: str, selector: str) -> str:
    """The body of the first rule whose selector list is exactly `selector`."""
    match = re.search(
        re.escape(selector) + r"\s*\{([^}]*)\}", html
    )
    assert match, f"no rule found for {selector!r}"
    return " ".join(match.group(1).split())


def test_folder_head_reserves_room_for_its_own_kebab(page):
    """The kebab sits at `right: 4px` and is 24px wide, so anything the row
    puts at its trailing edge needs at least 28px of right padding to clear
    it. `.as-history-link` uses 32; the folder head must not be less."""
    decl = _declaration(page, ".as-folder-head")
    padding = re.search(r"padding:\s*([^;]+);", decl)
    assert padding, f".as-folder-head lost its padding declaration: {decl}"
    parts = padding.group(1).split()
    assert len(parts) == 4, (
        ".as-folder-head needs an explicit right padding so its kebab does "
        f"not land on the folder count; got padding: {padding.group(1)!r}"
    )
    right_px = int(parts[1].removesuffix("px"))
    assert right_px >= 28, (
        f".as-folder-head reserves only {right_px}px on the right; the kebab "
        "occupies the trailing 28px and would cover .as-folder-count"
    )


def test_empty_state_scrolls_its_overflow_instead_of_spilling(page):
    """`.as-empty` is the sibling of `.as-log` in one `{% if rows %}`. The log
    scrolls its overflow; the empty state must too, or its content paints
    over the composer's meta line on a short viewport."""
    decl = _declaration(page, ".as-empty")
    assert "overflow-y: auto" in decl, (
        ".as-empty must scroll its overflow rather than spill over .as-meta: "
        f"{decl}"
    )
    assert "justify-content: safe center" in decl, (
        "plain `center` pushes overflowing content above the scroll origin "
        f"where it cannot be reached; `safe center` is required: {decl}"
    )


def test_empty_state_mark_cannot_be_squashed_into_an_ellipse(page):
    """A 44px circle that is also a column flex item needs `flex: none`, or
    `flex-shrink` halves its height and `border-radius: 50%` renders an
    ellipse."""
    decl = _declaration(page, ".as-empty-mark")
    assert re.search(r"flex:\s*none", decl), (
        ".as-empty-mark must opt out of flex-shrink or it renders as a "
        f"flattened ellipse when the empty state is short: {decl}"
    )


def test_the_four_starters_are_one_set_of_equal_tiles(page):
    """`grid-auto-rows: auto` sized each row to its own tallest label, so the
    top pair rendered 62.4px against the bottom pair's 44.2px purely because
    one label wraps. The four are peers; they get one height."""
    decl = _declaration(page, ".as-starters")
    assert "grid-auto-rows: 1fr" in decl, (
        "the starter grid must give every row the same height: " + decl
    )


@pytest.mark.parametrize("selector", [".as-history-kebab", ".as-folder-kebab"])
def test_hover_gated_controls_are_revealed_by_keyboard_focus(page, selector):
    """`opacity: 0` hides a focus ring as thoroughly as it hides the button,
    so every rule that reveals one of these on hover must also reveal it on
    `:focus-visible` -- the thread's own `.as-msg-actions:focus-within`
    already does this."""
    assert f"{selector}:focus-visible" in page, (
        f"{selector} is hover-gated with opacity 0 and never revealed on "
        "keyboard focus, leaving a tab stop the user cannot see"
    )


def test_sidebar_empty_state_shares_the_left_ruler_with_the_rows(page):
    """"No chats yet." / "Empty. Move a chat in." stand in for chat rows, so
    they start where a chat row's title starts. `.as-history-link` insets its
    title by 12px; this used 6, putting the two 6px apart."""
    decl = _declaration(page, ".as-history-empty")
    padding = re.search(r"padding:\s*([^;]+);", decl)
    assert padding, f".as-history-empty lost its padding declaration: {decl}"
    horizontal = padding.group(1).split()[1]
    assert horizontal == "12px", (
        ".as-history-empty must use the same 12px inset .as-history-link "
        f"gives a chat title; got {horizontal!r}"
    )
