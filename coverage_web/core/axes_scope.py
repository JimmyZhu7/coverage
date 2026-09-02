"""Which requests django-axes is allowed to count.

Axes exists in this project for exactly one form: Django's admin login,
which allauth's own rate limits cannot see (see the AXES_* block in
settings/base.py). `AXES_ONLY_ADMIN_SITE` looks like it says that, and it
half does — it is checked in `is_allowed`, the "may this authentication
proceed" question. It is NOT checked in the handler's `user_login_failed`,
which records the attempt and sets `request.axes_locked_out`. So with only
that setting, a failed sign-in on `/accounts/login/` still counts, and
still trips the middleware's 429 once the count is reached.

That is worse than it sounds, because axes reads the attempted identifier
from `AXES_USERNAME_FORM_FIELD` ("username"), which allauth's form does not
post — its field is "login", and with ACCOUNT_LOGIN_METHODS={"email"} the
credential reaches the backend as `email=`. Axes therefore records
`username=None` and the (username, IP) pair collapses to the IP alone: five
bad sign-ins from one shared campus NAT would lock out everyone behind it.
A test caught exactly that.

`AXES_WHITELIST_CALLABLE` is the one hook checked on BOTH paths — `is_allowed`
and `user_login_failed` both call `is_whitelisted` — so it is what actually
draws the line. Whitelisted here means "axes does not apply", not "trusted":
those requests are already covered by allauth's own limiter.
"""

from __future__ import annotations

from django.urls import NoReverseMatch, reverse


def outside_the_admin(request, credentials=None) -> bool:
    """True when this request is not the Django admin, i.e. not axes' job.

    Keyed on `reverse("admin:index")` rather than a literal "/admin/", the
    same way axes' own `is_admin_request` is, so it follows
    settings.ADMIN_URL_PREFIX wherever production puts the admin. A project
    with no admin routed at all reverses nothing, and then nothing is axes'
    job.
    """
    try:
        admin_root = reverse("admin:index")
    except NoReverseMatch:
        return True
    return not (getattr(request, "path", "") or "").startswith(admin_root)
