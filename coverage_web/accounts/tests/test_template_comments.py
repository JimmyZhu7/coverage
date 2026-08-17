"""Guard: no Django template comment may leak into rendered page text.

Django's brace-hash comment syntax is SINGLE-LINE ONLY. Written across two or
more lines it is not stripped — it renders as literal text.

This has now shipped twice, in two different failure modes:

  * Inside a `<style>` block it killed 103 of 185 CSS rules, because the CSS
    parser hit the stray brace and gave up on everything after it. That one is
    guarded by `directory/tests/test_styles_block.py`.
  * Inside page markup it simply printed itself. A paragraph reading
    "{# Explicit marker, like every other section below — accounts.views.
    settings_view now requires it before touching ProfileForm at all. #}"
    rendered directly under the "Profile" heading on the Settings page, in the
    product's own body copy, and was reported by the owner.

The second case is why this file exists: the CSS guard would never have caught
it, since the comment was nowhere near a `<style>` block. Both guards assert
the RENDERED OUTPUT rather than template source, so they catch the leak however
it gets in — including from an `{% include %}` several files away.

Multi-line notes belong in `{% comment %}...{% endcomment %}`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

pytestmark = pytest.mark.django_db

# Authed pages worth guarding: every one renders templates that carry
# substantial explanatory comments, which is exactly where the risk lives.
PATHS = [
    "/welcome/settings/",
    "/welcome/",
    "/app/",
    "/app/calendar/",
    "/app/contacts/",
    "/opportunities/",
    # Caught exactly this leak live in _thread.html's composer form — a
    # multi-line `{# #}` inside the <form> tag rendered as bogus attributes.
    "/assistant/",
]


@pytest.fixture
def student():
    User = get_user_model()
    return User.objects.create_user(
        email="comment-guard@example.com", password="x", capture_slug="cguardslug1"
    )


@pytest.mark.parametrize("path", PATHS)
def test_no_template_comment_leaks_into_the_page(client, student, path):
    client.force_login(student)
    body = client.get(path, follow=True).content.decode()
    assert "{#" not in body, (
        f"A Django template comment leaked into {path} and is showing as page "
        "text. The brace-hash form is single-line only — use "
        "{% comment %}...{% endcomment %} for anything longer."
    )
    # The closing half leaks on its own if someone splits a comment the other
    # way round, so check it independently rather than assuming they pair up.
    assert "#}" not in body, f"A dangling template-comment terminator rendered on {path}."


def _template_files() -> list[Path]:
    roots = [Path(d) for engine in settings.TEMPLATES for d in engine.get("DIRS", [])]
    return sorted({p for root in roots for p in Path(root).rglob("*.html")})


def test_no_template_opens_a_brace_hash_comment_it_does_not_close():
    """The same rule as above, read off the SOURCE of every template.

    The rendered-output guard is a hand-maintained list of five URLs, so it
    only ever protected pages someone remembered to add. It missed the leak
    twice over: a two-line comment printed itself across the whole Calendar
    page, and another sat unnoticed in the onboarding work-authorization step.
    A page that does not exist yet cannot be added to a list, so this one
    walks the template directories instead and needs no upkeep.

    It complements rather than replaces the render tests: those still catch a
    leak arriving through an `{% include %}` or through context data, which no
    amount of source scanning would see.
    """
    offenders = []
    for path in _template_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in re.finditer(r"\{#", line):
                if "#}" not in line[match.end():]:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()[:80]}")

    assert not offenders, (
        "Django's brace-hash comment is SINGLE-LINE ONLY; these open one that "
        "never closes on the same line, so it renders as literal page text. "
        "Use {% comment %}...{% endcomment %} instead:\n  " + "\n  ".join(offenders)
    )


def test_the_source_scan_is_actually_reading_templates():
    """Counterweight: an empty file list would make the scan above pass
    vacuously and keep passing forever if the template dirs ever move."""
    names = {p.name for p in _template_files()}
    assert len(names) > 20
    assert "base.html" in names


def test_the_settings_page_still_renders_its_profile_section(client, student):
    """Counterweight to the assertions above: proves they're checking a page
    that actually rendered, not an error page that happens to contain no
    braces."""
    client.force_login(student)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert "Work Authorization" in body
    assert "Cadence" in body
