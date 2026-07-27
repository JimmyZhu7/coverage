"""Guard: no Django template comment may leak into a rendered <style> block.

Django's brace-hash comment syntax is SINGLE-LINE only. Written across more
than one line inside a `<style>` block it is NOT stripped — it renders
literally into the stylesheet, the CSS parser hits the stray brace, gives up,
and discards every rule after it.

That is not hypothetical. It shipped: two such comments in
`templates/directory/_styles.html` silently killed 103 of the file's 185
rules, including `.rolecard` and `.firmcols`. The feed unwrapped into
unstyled blocks — while the top nav still looked perfectly fine, because its
styles live in `static/css/coverage.css` and had already parsed. Nothing
failed, no error was logged, and the page returned 200. The only symptom was
visual, on a page no test rendered.

These tests assert the rendered output, not the template source, so they
catch the leak however it gets in.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

# The rendered pages that carry their own <style> block.
PAGES = ["/opportunities/"]

_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)


def _style_blocks(path: str) -> list[str]:
    html = Client().get(path).content.decode()
    return _STYLE_RE.findall(html)


@pytest.mark.parametrize("path", PAGES)
def test_no_django_comment_leaks_into_a_style_block(path):
    """A leaked `{#` is the poison: everything after it is discarded."""
    for block in _style_blocks(path):
        assert "{#" not in block, (
            f"A Django template comment leaked into {path}'s <style> block. "
            "Use a CSS comment instead — the CSS parser stops at the stray "
            "brace and silently drops every rule that follows."
        )


@pytest.mark.parametrize("path", PAGES)
def test_style_block_braces_balance(path):
    """Balanced braces are the cheap proxy for 'a parser can read all of it'.
    An unbalanced block means some rules are unreachable even without a
    leaked template tag."""
    for block in _style_blocks(path):
        opens, closes = block.count("{"), block.count("}")
        assert opens == closes, (
            f"{path}'s <style> block has {opens} '{{' and {closes} '}}'. "
            "Unbalanced braces mean the parser cannot reach every rule."
        )


def test_the_feeds_core_layout_rules_survive_rendering():
    """The specific rules whose loss produced the incident. They live late in
    the file, so they are the first casualties of any early parse break —
    which makes them the canary worth naming explicitly."""
    blocks = _style_blocks("/opportunities/")
    assert blocks, "the feed should render its own <style> block"
    css = "\n".join(blocks)
    for selector in (".rolecard {", ".firmcols", ".fuse-passed", ".recbar"):
        assert selector in css, f"{selector} missing from the feed's rendered CSS"
