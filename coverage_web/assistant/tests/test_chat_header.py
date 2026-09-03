"""Talk to Coverage opens with the same header as every other nav page.

Reported live: "the spacing at the top is weird". Two numbers, both measured
in a browser at 1280x800 and 375x812 in both colour schemes.

1. Talk was the last page behind the nav still rendering a `.pagehead-eyebrow`
   ("ADVISOR"). The eyebrow and the sub came off every hero on 2026-08-29;
   `_pagehead.html` stopped rendering them at all, so the five pages using the
   include lost theirs mechanically. This page hand-writes its header, kept
   its eyebrow through that change, and then had one re-argued into it by a
   comment claiming the other five still open with a mono eyebrow. They do
   not.

2. `.as-title` overrode `.pagehead-title`'s `margin-top: var(--s1)` with
   `margin: 0`, so the title's 42px line box sat FLUSH against the eyebrow --
   0px where the shared header has 4. That is the gap the report was about.
   The rule is gone and the class with it, so the title now takes the shared
   header's own spacing.

The two pages that DO still carry an eyebrow are Contact detail and Debrief,
and both were kept on purpose: each states a fact printed nowhere else on its
page. "Advisor" over "Talk to Coverage" states nothing the title does not.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

# Every page reachable from the signed-in nav, by url name. Talk is measured
# against these rather than against a number written down here, so the day the
# shared header changes this test moves with it instead of failing.
NAV_PAGES = ["crm:week", "crm:contact_list", "crm:calendar", "accounts:settings"]


@pytest.fixture
def signed_in(client, django_user_model):
    user = django_user_model.objects.create_user(
        email="student@example.com", password="pw12345!")
    client.force_login(user)
    return client


def _markup(html: str) -> str:
    """The page with its stylesheets removed. `.pagehead-eyebrow` is NAMED in
    coverage.css and in this page's own style block, so a substring check on
    the raw body is true whether or not an eyebrow was drawn."""
    return re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)


def test_the_chat_header_renders_a_title_and_nothing_above_it(signed_in):
    markup = _markup(signed_in.get(reverse("assistant:chat")).content.decode())
    assert "Talk to Coverage" in markup
    assert 'class="pagehead-eyebrow"' not in markup, (
        "Talk was the last nav page with an eyebrow; it reads as a stray line "
        "crammed onto the title because no other page has one."
    )
    assert 'class="pagehead-sub"' not in markup


@pytest.mark.parametrize("name", NAV_PAGES)
def test_no_other_nav_page_has_an_eyebrow_either(signed_in, name):
    """The counterweight, and the reason the assertion above is not just a
    preference: it is what the rest of the nav already does."""
    markup = _markup(signed_in.get(reverse(name)).content.decode())
    assert 'class="pagehead-title"' in markup, f"{name} lost its header"
    assert 'class="pagehead-eyebrow"' not in markup, name


def test_the_chat_title_keeps_the_shared_headers_own_spacing(signed_in):
    """The measured defect. This page may say what is different about wearing
    the shared header inside an app shell -- no hairline, no accent stroke --
    but the type, the weight and the spacing above the title come from
    `.pagehead-title` or the page stops matching the others without anyone
    noticing. `.as-title` said them a second time and got them wrong twice:
    22px Fraunces where the token had moved on, then `margin: 0` where the
    shared rule has `var(--s1)`."""
    html = signed_in.get(reverse("assistant:chat")).content.decode()
    rules = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL))
    rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.DOTALL)
    assert not re.search(r"\.as-title\s*\{", rules), (
        "an .as-title rule is a second declaration of a shared title; the "
        "last one closed the gap above it to 0px"
    )
    # The title wears the shared class and nothing else.
    assert 'class="pagehead-title">Talk to Coverage<' in _markup(html)


def test_the_composer_textarea_owns_its_own_height(signed_in):
    """Reported live: "the box does not get bigger when there is more than
    one line, it should adjust with the word and have a cap".

    The cause was not the auto-grow script, which existed, ran on every
    value-setting path, and correctly computed 82px for a three-line "Talk
    about it" opener. It was that `.as-composer-inner` is a COLUMN flex
    container, so the main axis is vertical, and the textarea carried
    `flex: 1`. That resolves to `flex-basis: 0%` on the height, and
    flex-basis beats the `height` property for a flex item's main size, so
    the inline `height: 82px` sat in the DOM looking right while the used
    height stayed 36.5px and the sentence was clipped. Measured live, and
    `!important` did not move it either, because it was never a specificity
    fight.

    So the rule under test is the flex declaration, not the script: the
    textarea must not take a flex-basis on the axis it needs to grow along.
    """
    body = signed_in.get(reverse("assistant:chat")).content.decode()
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", body, re.S))
    rule = re.search(r"\.as-composer textarea \{(.*?)\}", css, re.S)
    assert rule, ".as-composer textarea lost its rule"
    decls = rule.group(1)

    assert "flex: 0 0 auto" in decls, (
        "the composer textarea took a flex-basis again. In this column "
        "container that governs its height and silently overrides the "
        "auto-grow script, which is the exact bug this pins."
    )
    # The cap the founder asked for, and the scroll past it.
    assert "max-height: 200px" in decls
    assert "overflow-y: auto" in decls
