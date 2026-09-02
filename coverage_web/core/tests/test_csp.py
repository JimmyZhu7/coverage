"""Guard: the CSP header must not silently break the sign-in flow it applies to.

`settings/base.py`'s CONTENT_SECURITY_POLICY was audited against every
`<script src>`/`<link href>` in templates/ and nothing else, because that
audit was framed as "what does this app load." It missed the one thing that
isn't a resource load: `_auth_providers.html` posts to this app's own
`/accounts/google/login/`, which then redirects to Google to run the actual
OAuth handshake. CSP3's `form-action` restricts not just a form's immediate
target but every URL a form submission is redirected to, so `form-action
'self'` alone silently killed the button — reproduced directly in a real
browser: Chrome blocked the request with no error shown to the user and no
network request even attempted, logging only "Sending form data to
'.../accounts/google/login/' violates ... form-action 'self'" to the
console, where nobody signing in would ever see it.

This pins the fix (Google's authorize origin added to `form-action`) against
the actual served header, not just the settings dict, so a future
"tightened the CSP" pass can't reintroduce the same silent breakage.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client

pytestmark = pytest.mark.django_db


def test_form_action_allows_googles_oauth_origin():
    resp = Client().get("/accounts/login/")
    header = resp.headers.get("Content-Security-Policy", "")
    assert "form-action" in header, "form-action directive is missing entirely"
    directive = next(d for d in header.split(";") if "form-action" in d)
    assert "'self'" in directive
    assert "https://accounts.google.com" in directive


def test_the_google_button_is_a_same_origin_form_not_a_cross_origin_one(settings):
    """The fix belongs in form-action, not in loosening it to allow posting
    STRAIGHT to Google — confirms `_auth_providers.html` still posts to this
    app's own URL, which is what makes 'self' correct for the immediate
    target and Google's origin only necessary for the redirect it causes.

    THIS USED TO BE `skipif`-ED, and that is the half worth explaining. The
    partial renders nothing for a provider without a client_id, so on a
    checkout with no `.env` this asserted markup that could not exist and was
    red on every full run; the skip was added to stop that. But a guard that
    evaluates to "skipped" on the machine most likely to run it is a guard
    that has stopped guarding — the very reason it existed (a CSP tightening
    silently breaking the button) is invisible on exactly the tree where the
    button is unconfigured. Making the provider list explicit runs the
    assertion everywhere instead, which is what a test of MARKUP should do:
    the markup does not depend on whether this laptop has credentials.
    """
    settings.ENABLED_SOCIAL_PROVIDERS = ["google"]
    body = Client().get("/accounts/login/").content.decode()
    assert 'action="/accounts/google/login/"' in body


# ---------------------------------------------------------------------------
# The placeholder client id (audit-security.md finding 17)
# ---------------------------------------------------------------------------

def test_a_placeholder_client_id_is_not_a_configured_provider():
    """`.env.example` ships `GOOGLE_OAUTH_CLIENT_ID=changeme` and render.yaml
    declares the var with no value, so "non-empty" was never the same question
    as "configured". A copied example file rendered a live Continue with
    Google button that took the student to Google's `invalid_client` page:
    a dead end that reads as Coverage being broken rather than as Coverage
    being unconfigured.

    Asserted through the same settings expression the app uses, not through a
    hand-rolled copy of it, so the two cannot drift.
    """
    def enabled_for(client_id: str) -> list[str]:
        providers = {"google": {"APP": {"client_id": client_id}}}
        return [
            p for p in ("google",)
            if (providers.get(p, {}).get("APP", {}).get("client_id") or "")
            .strip().lower() not in settings.SOCIAL_CLIENT_ID_PLACEHOLDERS | {""}
        ]

    assert enabled_for("changeme") == []
    assert enabled_for("CHANGEME") == []
    assert enabled_for("  changeme  ") == []
    assert enabled_for("") == []
    assert enabled_for("548469261092-real.apps.googleusercontent.com") == ["google"]


def test_the_login_page_renders_no_google_form_without_a_real_client_id(settings):
    """The consequence of the test above, at the surface a student sees. An
    unconfigured provider renders NOTHING (see `_auth_providers.html`), so the
    sign-in page offers only what actually works.
    """
    settings.ENABLED_SOCIAL_PROVIDERS = []
    body = Client().get("/accounts/login/").content.decode()
    # The submit URL, not the label: "Continue with Google" also appears in an
    # inlined stylesheet comment explaining the button's shape, and a test
    # that reads prose out of a CSS comment is a test that fails on an edit to
    # the comment.
    assert "/accounts/google/login/" not in body


def test_the_form_action_directive_stands_even_with_no_provider(settings):
    """The header is unconditional on purpose. `form-action` is a property of
    the CSP, not of whether this deploy happens to have Google credentials, so
    a tree with no `.env` still pins the fix that took a real browser session
    to find.
    """
    settings.ENABLED_SOCIAL_PROVIDERS = []
    header = Client().get("/accounts/login/").headers.get(
        "Content-Security-Policy", "")
    assert "https://accounts.google.com" in header
