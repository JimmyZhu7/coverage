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
from django.test import Client

pytestmark = pytest.mark.django_db


def test_form_action_allows_googles_oauth_origin():
    resp = Client().get("/accounts/login/")
    header = resp.headers.get("Content-Security-Policy", "")
    assert "form-action" in header, "form-action directive is missing entirely"
    directive = next(d for d in header.split(";") if "form-action" in d)
    assert "'self'" in directive
    assert "https://accounts.google.com" in directive


def test_the_google_button_is_a_same_origin_form_not_a_cross_origin_one():
    """The fix belongs in form-action, not in loosening it to allow posting
    STRAIGHT to Google — confirms `_auth_providers.html` still posts to this
    app's own URL, which is what makes 'self' correct for the immediate
    target and Google's origin only necessary for the redirect it causes."""
    body = Client().get("/accounts/login/").content.decode()
    assert 'action="/accounts/google/login/"' in body
