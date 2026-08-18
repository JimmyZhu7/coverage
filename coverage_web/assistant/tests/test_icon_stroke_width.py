"""Talk to Coverage's own inline SVG icons must stay on the app-wide 1.5
stroke-width convention.

Round 11 converged every small inline SVG in the product onto
stroke-width="1.5" (icons one stroke-width, stroked in currentColor - see
STATE.md's Design benchmark standards) and round 13 caught the one outlier
that sweep missed (the JS-injected custom-select caret). Talk to Coverage
(assistant/, added after both sweeps by commits b8a2ca2/2a361b5) shipped its
own icon set at THREE different stroke-widths that had never been audited:
1.6 (sidebar close, new-chat), 1.3 (new-folder, attach, mic, folder/menu
icons across _history.html, _history_row.html and _message.html) and 1.8
(send arrow) — sitting directly beside the one icon that already matched the
standard (the 1.5 hamburger toggle). This test pins every stroked icon
rendered on the page to 1.5 so a future edit can't reintroduce an outlier.

The one deliberate exception is the Coverage brand mark (`_logo.html`,
stroke-width 2.6) used as the assistant avatar / empty-state mark — that is
the site's wordmark, rendered identically in the nav, footer, and here, not
a UI icon, and is explicitly out of scope for the icon-convergence rule.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from assistant.models import ChatConversation, ChatFolder, ChatMessage

User = get_user_model()

pytestmark = pytest.mark.django_db

# The brand mark (_logo.html) is a deliberate, product-wide exception to the
# icon-convergence rule -- exclude only its own stroke-width from the scan.
_LOGO_STROKE_WIDTH = "2.6"


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="pw12345!")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    return client


def test_every_talk_to_coverage_icon_ships_at_the_app_wide_stroke_width(signed_in, user):
    # A folder (renders _history.html's folder icon + its rename/delete
    # menu icons), a conversation inside it, and an unfiled conversation
    # with an attached-file turn (renders _history_row.html's own menu
    # icons and _message.html's attachment-chip icon) -- between them these
    # exercise every stroked SVG the page can render.
    folder = ChatFolder(user=user, name="BofA prep")
    folder.save()
    filed = ChatConversation(user=user, folder=folder, title="Filed chat")
    filed.save()
    conversation = ChatConversation(user=user, title="Unfiled chat")
    conversation.save()
    message = ChatMessage(
        user=user,
        conversation=conversation,
        role="user",
        content=[
            {"type": "text", "text": "here's my resume"},
            {"type": "document", "_filename": "resume.pdf"},
        ],
    )
    message.save()

    response = signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id]))
    assert response.status_code == 200
    html = response.content.decode()

    # Sanity: the fragments this test relies on actually rendered.
    assert 'class="as-folder"' in html
    assert "as-attachment-chip" in html

    # Scope the scan to the page body (nav/header through the {% block
    # content %} this page fills), not base.html's own trailing <script>
    # blocks -- e.g. its site-wide numeric-stepper widget ships an
    # unrelated, pre-existing stroke-width="2" arrow that is never
    # instantiated on this page (no numeric-step input here) and is out of
    # scope for Talk to Coverage's own icon set.
    body_start = html.index('id="as-app"')
    body_end = html.index("num-stepper")
    scoped_html = html[body_start:body_end]

    stroke_widths = {
        w for w in re.findall(r'stroke-width="([\d.]+)"', scoped_html)
        if w != _LOGO_STROKE_WIDTH
    }

    assert stroke_widths == {"1.5"}, (
        f"Talk to Coverage icon(s) drifted off the 1.5 stroke-width "
        f"convention: {sorted(stroke_widths)}"
    )
