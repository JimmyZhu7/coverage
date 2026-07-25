"""Custom allauth account adapter — trims allauth's own flash messages.

allauth's `DefaultAccountAdapter.add_message` renders a message on every
login and logout ("Successfully signed in as x@y.com.", "You have signed
out."). Those fire on every session, stack up during normal use, and add
nothing a signed-in user doesn't already know from being on the page — pure
noise, unlike a one-time confirmation such as onboarding's "You're all set."
(accounts/views.py), which stays.

Only the two auth-flow templates are swallowed; everything else (password
changed, email confirmed, etc.) still flows through the base adapter
unchanged.
"""

from __future__ import annotations

from allauth.account.adapter import DefaultAccountAdapter

_SUPPRESSED_TEMPLATES = {
    "account/messages/logged_in.txt",
    "account/messages/logged_out.txt",
}


class CoverageAccountAdapter(DefaultAccountAdapter):
    def add_message(self, request, level, message_template=None, message_context=None,
                     extra_tags="", message=None):
        if message_template in _SUPPRESSED_TEMPLATES:
            return
        super().add_message(
            request, level,
            message_template=message_template, message_context=message_context,
            extra_tags=extra_tags, message=message,
        )
