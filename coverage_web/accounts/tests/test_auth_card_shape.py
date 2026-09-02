"""One control shape per auth card, and targets a finger can hit.

Two defects from the 2026-09-01 UI audit, both on the first screen a student
who is not signed in ever reaches:

  * "Continue with Google" was a 999px capsule and "Sign In", forty pixels
    below it, was squared. `--r-ctl`'s own comment in coverage.css says the
    token is for "buttons, inputs"; a provider button and a submit button are
    the same kind of thing, so they are now the same shape.
  * measured at 375px: "Forgot?" 43x19, the "Keep me signed in" row 21px,
    "Create one" 16px, the Terms and Privacy links 15px. Every one of them a
    link on the product's front door, at under half the 44px floor the rest
    of the app keeps.

The touch rules are `pointer: coarse` only. On a mouse these are correctly
sized text links, and wrapping 44px of dead space around a word inside a
sentence would be worse, not better.

A NOTE FOR ANYONE MEASURING THESE WITH HEADLESS CHROMIUM. A `full_page=True`
screenshot re-applies device metrics and drops the context's mobile
emulation, so `matchMedia("(pointer: coarse)")` reads false on every page
that follows one. Measure before the shot, or on a fresh page, or these
rules will look like they never applied.
"""

from __future__ import annotations

import re

import pytest
from django.conf import settings


def _auth_css(client) -> str:
    body = client.get("/accounts/login/").content.decode()
    return " ".join(re.findall(r"<style>(.*?)</style>", body, re.S))


@pytest.mark.django_db
def test_provider_button_takes_the_control_radius(client):
    css = _auth_css(client)

    rule = re.search(r"\.auth-provider\s*\{(.*?)\}", css, re.S)
    assert rule, ".auth-provider should be styled on the auth pages"
    assert "border-radius: var(--r-ctl)" in rule.group(1)
    assert "border-radius: 999px" not in rule.group(1), (
        "a pill beside a squared submit button is two shapes for one job"
    )


@pytest.mark.django_db
def test_the_small_auth_links_get_a_finger_floor_on_coarse_pointers(client):
    css = _auth_css(client)

    coarse = re.search(r"@media \(pointer: coarse\)\s*\{(.*)", css, re.S)
    assert coarse, "the auth card needs a coarse-pointer block"
    block = coarse.group(1)

    # "Forgot?" is 43px wide as well as 19px tall, so it needs both floors.
    assert re.search(r"\.auth-mini\s*\{[^}]*min-height:\s*44px", block)
    assert re.search(r"\.auth-mini\s*\{[^}]*min-width:\s*44px", block)
    assert re.search(r"\.auth-check\s*\{[^}]*min-height:\s*44px", block)
    # The two links that sit INSIDE a sentence grow by padding and shrink the
    # line back with an equal negative margin, so the target grows and the
    # paragraph does not.
    assert re.search(r"\.auth-foot a, \.auth-legal a\s*\{[^}]*padding: 14px 6px", block)
    assert re.search(r"\.auth-foot a, \.auth-legal a\s*\{[^}]*margin: -14px -6px", block)


def test_the_shell_footer_links_get_the_same_floor():
    """`.site-footer` links measured 19px on every page in the product, and
    the footer is where a student goes looking for Privacy and Terms.

    READ FROM THE SHARED STYLESHEET, not from the page's inline `<style>`
    blocks. The rule shipped inside a `<style>` in base.html, with a note
    saying it belonged in `coverage.css` and would move there once that file
    was not being edited by another pass; it moved on 2026-09-01. The
    assertion follows it rather than being weakened: the floor is still
    pinned, on the same two selectors, under the same `pointer: coarse`
    query. What changed is only where the rule lives.

    Moving it also fixed a second problem the inline block caused. Two tests
    in `directory/tests/test_styles_block.py` read the FIRST
    `@media (pointer: coarse)` block on a rendered page as the feed's, and
    the shell's block was reaching them first.
    """
    css = (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()
    blocks = re.findall(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S)
    assert blocks, "no coarse-pointer block left in coverage.css"
    joined = "\n".join(blocks)
    assert ".site-footer-inner nav a" in joined
    assert re.search(r"\.site-auth a\s*\{[^}]*min-height: 44px", joined)
