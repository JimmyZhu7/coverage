"""Views for onboarding, CSV import/export, settings, self-serve deletion,
and the static legal pages (task M5; docs/build-plan.md §7 M5, §10).

Everything except the two legal pages is `@login_required`. Business logic
lives in accounts/services.py; these views are thin request/response glue
plus the onboarding step machine. Templates extend coverage_web's
`base.html`. htmx is used for the in-place profile save on the settings
page and the live firm-search filter during onboarding.
"""

from __future__ import annotations

import json

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from analytics.events import record_event
from capture.services import capture_health
from crm.models import Contact, UserFirm
from directory.models import Firm

from .models import PushSubscription
from . import services
from .forms import (
    CYCLE_SUGGESTIONS,
    REGION_CHOICES,
    TRACK_CHOICES,
    CadenceForm,
    ProfileForm,
    WeeklyPaceForm,
    WorkAuthorizationForm,
    known_timezones,
)

# The independently-saving sections of /welcome/settings/, keyed by the value
# their hidden `section` input posts. See accounts/forms.SectionForm.
SECTION_FORMS = {
    form_cls.section: form_cls
    for form_cls in (WorkAuthorizationForm, CadenceForm, WeeklyPaceForm)
}

# Step order of the onboarding wizard.
#
# `work_auth` sits immediately after `profile` because it IS profile data —
# a structural fact about the person, in the same breath as school and target
# regions — and because the very next step (picking firms) is the first place
# the fit score it feeds becomes visible. Asking for it after the firm board
# is built would mean showing the user a set of scores computed without it.
# Five steps, not six. `survey` asked a brand-new account to RANK the firms
# it had picked ten seconds earlier — a judgement nobody can make before
# seeing a single deadline or contact, on a page whose only effect is a
# number they have never seen used. Tiering lives on the Network page, where
# it is a drag between columns with the board visible; it did not need a
# wizard step, and asking early is how a wizard gets abandoned.
ONBOARDING_STEPS = ["profile", "work_auth", "firms", "import", "capture"]

# Rail labels — the raw step keys don't all title-case into English
# ("work_auth"), and the rail is the user's map of how much is left.
ONBOARDING_STEP_LABELS = {
    "profile": "Profile",
    "work_auth": "Work",
    "firms": "Firms",
    "import": "Import",
    "capture": "Capture",
}


def _step_url(step: str) -> str:
    return f"{reverse('accounts:onboarding')}?step={step}"


def _next_step(step: str) -> str:
    """The step after `step` — used by both Continue and Skip, so a step can
    never be made non-skippable by a stale hardcoded target."""
    idx = ONBOARDING_STEPS.index(step)
    return ONBOARDING_STEPS[min(idx + 1, len(ONBOARDING_STEPS) - 1)]


# ---------------------------------------------------------------------------
# Onboarding wizard  (/welcome/)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def onboarding(request):
    # A returning, already-onboarded user hitting bare /welcome/ (the nav's
    # "Settings" link, per base.html) lands on Settings rather than replaying
    # the wizard. Explicit ?step= navigation still re-enters any step.
    if (
        request.method == "GET"
        and not request.GET.get("step")
        and request.user.onboarded_at is not None
    ):
        return redirect(reverse("accounts:settings"))

    step = request.POST.get("step") or request.GET.get("step") or "profile"
    if step not in ONBOARDING_STEPS:
        step = "profile"

    form = None
    section_form = None
    if request.method == "POST":
        if step == "profile":
            form = ProfileForm(request.POST, request.FILES)
            if form.is_valid():
                form.apply_to(request.user)
                return redirect(_step_url(_next_step(step)))
            # invalid → fall through and re-render this step with errors
        elif step == "work_auth":
            # Both reuse the settings-page section forms, so onboarding and
            # Settings can never disagree about what's valid. Every field on
            # them is optional, so an untouched form validates and writes
            # nothing — "Continue" without answering is as skippable as the
            # Skip link, and neither leaves a guessed default behind.
            section_form = SECTION_FORMS[step](request.POST)
            if section_form.is_valid():
                section_form.apply_to(request.user)
                return redirect(_step_url(_next_step(step)))
            # invalid → re-render this step with errors
        elif step == "firms":
            services.set_target_firms(request.user, request.POST.getlist("firms"))
            return redirect(_step_url(_next_step(step)))
        elif step == "import":
            return redirect(_step_url(_next_step(step)))
        elif step == "capture":
            if request.user.onboarded_at is None:
                request.user.onboarded_at = timezone.now()
                request.user.save(update_fields=["onboarded_at"])
                record_event("onboarded", user=request.user)
            messages.success(request, "You're all set. Welcome to Coverage.")
            # Land on Today — the working surface — not back in Settings.
            return redirect("/app/")

    if form is None:
        form = ProfileForm.from_user(request.user)
    if section_form is None and step in SECTION_FORMS:
        section_form = SECTION_FORMS[step].from_user(request.user)

    context = {
        "step": step,
        "steps": [
            {"key": s, "label": ONBOARDING_STEP_LABELS[s]} for s in ONBOARDING_STEPS
        ],
        "step_number": ONBOARDING_STEPS.index(step) + 1,
        "step_total": len(ONBOARDING_STEPS),
        "next_step": _next_step(step),
        "form": form,
        "section_form": section_form,
        "cycle_suggestions": CYCLE_SUGGESTIONS,
    }
    if step == "firms":
        context.update(_firm_picker_context(request.user))
    if step == "capture":
        context["capture_address"] = services.capture_address(request.user)
    return render(request, "accounts/onboarding.html", context)


def _firm_picker_context(user) -> dict:
    firms = list(Firm.objects.all().order_by("name"))
    selected = set(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    return {
        "firms": firms,
        "selected_firm_ids": selected,
        "region_choices": REGION_CHOICES,
        "track_choices": TRACK_CHOICES,
    }


# ---------------------------------------------------------------------------
# CSV import  (/welcome/import/)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def import_contacts(request):
    result = None
    if request.method == "POST":
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose a CSV file to upload.")
        else:
            result = services.import_contacts(
                request.user,
                file_bytes=upload.read(),
                filename=upload.name,
            )
            if result.errors:
                for err in result.errors:
                    messages.error(request, err)
            else:
                messages.success(
                    request,
                    f"Imported {result.created} contact"
                    f"{'' if result.created == 1 else 's'}"
                    f" ({result.skipped} skipped).",
                )
    return render(
        request,
        "accounts/import.html",
        {"result": result, "from_onboarding": request.GET.get("from") == "welcome"},
    )


@login_required
@require_GET
def import_template(request):
    resp = HttpResponse(services.import_template_csv(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="coverage-contacts-template.csv"'
    return resp


# ---------------------------------------------------------------------------
# Settings  (/welcome/settings/)
# ---------------------------------------------------------------------------
def _cycle_months():
    from directory.views import cycle_months
    return cycle_months()


@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request):
    saved = False
    form = None
    # Every section starts as an unbound render of the user's current values;
    # a failing POST replaces just its own with the bound, error-carrying one,
    # so an invalid Cadence entry never blanks out the Work Authorization
    # selects sitting above it.
    section_forms = {
        name: cls.from_user(request.user) for name, cls in SECTION_FORMS.items()
    }

    # There used to be a Language branch here — its own <form>, sniffed by the
    # presence of a `language` key rather than a section marker. It was cut on
    # 2026-07-30 (docs/specs/settings-page.md audit #3): `User.language` was
    # written by this branch and read back only to re-render the same dropdown.
    # No LocaleMiddleware, no catalogs, no {% trans %} anywhere. Picking 中文
    # changed nothing. The column stays (harmless, already populated); the
    # control returns with an actual i18n pass, not before.
    section = request.POST.get("section") if request.method == "POST" else None
    if section in SECTION_FORMS:
        bound = SECTION_FORMS[section](request.POST)
        if bound.is_valid():
            bound.apply_to(request.user)
            messages.success(request, bound.success_message)
            # PRG, same as the profile save: a refresh after saving must
            # not re-POST.
            return redirect(reverse("accounts:settings"))
        # Invalid → fall through and re-render, this section showing errors.
        section_forms[section] = bound
    elif request.method == "POST" and section == "profile":
        # Requires the explicit marker, like every other section — see the
        # `elif request.method == "POST":` branch just below for why. The
        # profile <form> in settings.html now carries
        # `<input type="hidden" name="section" value="profile">` to match.
        form = ProfileForm(request.POST, request.FILES)
        if form.is_valid():
            form.apply_to(request.user)
            saved = True
            if request.headers.get("HX-Request"):
                # htmx in-place save: swap back the form with a saved flag.
                return render(
                    request,
                    "accounts/_profile_form.html",
                    {"form": ProfileForm.from_user(request.user),
                     "saved": True, "cycle_suggestions": CYCLE_SUGGESTIONS},
                )
            messages.success(request, "Profile saved.")
            return redirect(reverse("accounts:settings"))
    elif request.method == "POST":
        # A POST that names neither a recognised `section` nor "profile" —
        # a stale form cached before a section was added/renamed, or a
        # hand-crafted request. This used to fall straight through to
        # `ProfileForm(request.POST, ...)` unconditionally: every ProfileForm
        # field is `required=False`, so an EMPTY POST validated, and its
        # `apply_to` blanked all six profile fields (name/school/class_year/
        # target_cycle/regions/tracks) with no error and no confirmation.
        # Requiring the explicit marker turns an unrecognised POST into a
        # no-op re-render instead of a silent profile wipe.
        pass
    if form is None:
        form = ProfileForm.from_user(request.user)

    contacts = Contact.objects.for_user(request.user)
    contact_count = contacts.count()
    return render(
        request,
        "accounts/settings.html",
        {
            "form": form,
            "saved": saved,
            "cycle_suggestions": CYCLE_SUGGESTIONS,
            "capture_address": services.capture_address(request.user),
            # The cycle band under the target-cycle picker — the directory's
            # own deadline density, so the subject of the picker is visible
            # while you pick.
            "cycle_months": _cycle_months(),
            # `capture_health` is two aggregates over the user's own events —
            # cheap enough to run on every settings render, and the whole point
            # is that a student whose BCC has silently stopped working finds
            # out on the page they'd actually check (risk register #3).
            "capture": capture_health(request.user),
            "target_firm_count": UserFirm.objects.for_user(request.user).count(),
            "contact_count": contact_count,
            # Split out rather than folded in: "Contacts: 137" counted archived
            # rows while the Network page showed 112, so the two pages
            # disagreed about the same number. Stating the population is rule
            # D3 — a count must mean what it says.
            "archived_count": contacts.filter(archived=True).count(),
            "work_auth_form": section_forms["work_auth"],
            "cadence_form": section_forms["cadence"],
            "pace_form": section_forms["pace"],
            "default_weekly_goal": WeeklyPaceForm.DEFAULT_GOAL,
            # Push notifications (deadline alerts). Blank key = the toggle
            # renders as unavailable rather than a button that dead-ends —
            # same "Setup Needed" posture the social sign-in buttons use for
            # an unconfigured provider. See accounts/push.py.
            "vapid_public_key": django_settings.VAPID_PUBLIC_KEY,
            "push_subscribed": PushSubscription.objects.for_user(request.user).exists(),
            **_security_context(request.user),
        },
    )


def _security_context(user) -> dict:
    """Facts the Sign-In & Security card renders rows from.

    Honesty rule D6: every row must reflect THIS account. A "Change password"
    link on an account with no usable password dead-ends at a form that asks
    for a current password that doesn't exist; a "Connected accounts" link
    with no provider configured opens an empty page that reads like a bug. So
    both are conditional on the real state rather than always drawn.
    """
    has_password = user.has_usable_password()
    primary = (
        EmailAddress.objects.filter(user=user, primary=True).first()
        # A user created outside allauth's signup flow (createsuperuser, a
        # fixture, the cutover import) has no EmailAddress row at all, so
        # falling back to any row for this address keeps the badge truthful
        # instead of showing "unverified" for a state allauth never recorded.
        or EmailAddress.objects.filter(user=user, email__iexact=user.email).first()
    )
    return {
        "has_usable_password": has_password,
        "email_verified": bool(primary and primary.verified),
        "connected_accounts": SocialAccount.objects.filter(user=user).count(),
        # Read from the same env-derived list the auth pages use, so a provider
        # that isn't configured never gets a link on this page either.
        "social_providers_configured": bool(
            getattr(django_settings, "ENABLED_SOCIAL_PROVIDERS", [])
        ),
    }


# ---------------------------------------------------------------------------
# CSV export  (/welcome/export/)
# ---------------------------------------------------------------------------
@login_required
@require_GET
def export(request):
    kind = request.GET.get("kind")
    if kind == "all":
        # The everything-in-one-ZIP download the privacy policy's "export
        # everything" line promises. `record_event` so the founder can see
        # whether anyone actually exercises the portability promise.
        record_event("export_downloaded", user=request.user, kind="all")
        resp = HttpResponse(
            services.export_zip(request.user), content_type="application/zip"
        )
        resp["Content-Disposition"] = 'attachment; filename="coverage-data.zip"'
        return resp
    if kind == "contacts":
        record_event("export_downloaded", user=request.user, kind="contacts")
        return _csv_download(
            services.contacts_csv(request.user), "coverage-contacts.csv"
        )
    if kind == "touches":
        record_event("export_downloaded", user=request.user, kind="touches")
        return _csv_download(
            services.touches_csv(request.user), "coverage-touches.csv"
        )
    contacts = Contact.objects.for_user(request.user)
    return render(
        request,
        "accounts/export.html",
        {
            "contact_count": contacts.count(),
            "archived_count": contacts.filter(archived=True).count(),
            # Enumerated from the builders themselves (accounts.services
            # EXPORT_FILES), so the page cannot list a file the ZIP lacks.
            "export_manifest": services.export_manifest(),
            "export_exclusions": services.EXPORT_EXCLUSIONS,
        },
    )


def _csv_download(text: str, filename: str) -> HttpResponse:
    resp = HttpResponse(text, content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------
# Consequential account actions, each on its own confirm page
# ---------------------------------------------------------------------------
# The shared pattern (GitHub's Danger Zone, and the existing delete page):
# nothing consequential happens on a click within Settings. Every one of these
# is a GET that states — in plain bullets — what changes, what survives, and
# whether there is an undo, then a POST that does it. No JS-only modal; the
# no-JS path is the only path.
#
# Type-to-confirm is reserved for account deletion, where the blast radius is
# total and permanent. Regenerating the capture address and signing out other
# devices are each recoverable BY ACTION (re-save a mail filter, sign in
# again), so a single honest confirm button is the right friction — more would
# train people to click through it.
@login_required
@require_http_methods(["GET", "POST"])
def regenerate_capture_address(request):
    if request.method == "POST":
        new_address = services.regenerate_capture_address(request.user)
        messages.success(
            request,
            f"New capture address: {new_address}. The old one stops working now.",
        )
        return redirect(f"{reverse('accounts:settings')}#capture")
    return render(
        request,
        "accounts/capture_regenerate.html",
        {"capture_address": services.capture_address(request.user)},
    )


@login_required
@require_http_methods(["GET", "POST"])
def signout_other_sessions(request):
    if request.method == "POST":
        ended = services.sign_out_other_sessions(
            request.user, keep_session_key=request.session.session_key
        )
        # The count is the receipt: "signed out everywhere" with nothing to
        # sign out of is a claim the user can't check, and a student who
        # expected a library computer to appear in that number deserves to
        # see a zero rather than a reassuring sentence.
        messages.success(
            request,
            f"Signed out on {ended} other device{'' if ended == 1 else 's'}."
            if ended
            else "No other devices were signed in. This is your only session.",
        )
        return redirect(f"{reverse('accounts:settings')}#security")
    return render(request, "accounts/signout_all.html")


# ---------------------------------------------------------------------------
# Self-serve deletion  (/welcome/delete/)
# ---------------------------------------------------------------------------
# Labels for the per-table counts `delete_user_and_data` returns, so the
# goodbye flash is an itemised receipt rather than a reassuring sentence. Keyed
# by the same names as services._DELETE_ORDER.
_DELETED_LABELS: list[tuple[str, str, str]] = [
    ("contacts", "contact", "contacts"),
    ("touches", "touch", "touches"),
    ("user_firms", "target firm", "target firms"),
    ("user_opportunities", "tracked role", "tracked roles"),
    ("tasks", "task", "tasks"),
    ("chat_debriefs", "chat debrief", "chat debriefs"),
    ("capture_events", "capture event", "capture events"),
    ("fit_scores", "fit score", "fit scores"),
]


def _deletion_receipt(counts: dict[str, int]) -> str:
    """"Deleted 137 contacts, 138 touches, 69 target firms." Only non-zero
    tables are named — listing "0 tasks" would pad the receipt with things
    that were never there, which reads as boilerplate rather than as proof."""
    parts = [
        f"{counts[key]} {singular if counts[key] == 1 else plural}"
        for key, singular, plural in _DELETED_LABELS
        if counts.get(key)
    ]
    if not parts:
        return "Your account has been deleted. There was no other data on it."
    return "Deleted " + ", ".join(parts) + ", and your account. Nothing is retained."


@login_required
@require_http_methods(["GET", "POST"])
def delete_account(request):
    error = None
    if request.method == "POST":
        typed = (request.POST.get("confirm") or "").strip()
        if typed.lower() != request.user.email.lower():
            error = "That didn't match. Type your email address exactly to confirm."
        else:
            counts = services.delete_user_and_data(request.user)
            # The session's user row is gone; flush it and send them home.
            from django.contrib.auth import logout

            logout(request)
            messages.success(request, _deletion_receipt(counts))
            return redirect("/")
    return render(
        request,
        "accounts/delete.html",
        {"error": error},
    )


# ---------------------------------------------------------------------------
# Timezone auto-detection  (/welcome/timezone/)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["POST"])
def timezone_detect(request):
    """Store the browser's own timezone, when the account is set to follow it.

    The server cannot work out where somebody is — an IP guess is wrong on a
    VPN and creepy either way — but the BROWSER already knows, because the OS
    told it. `Intl.DateTimeFormat().resolvedOptions().timeZone` returns a real
    IANA name ("Asia/Hong_Kong"), which is exactly what `User.timezone`
    stores, so this is a copy rather than a translation.

    Three refusals, each deliberate:

    - `timezone_auto` off means the user chose a zone by hand. Their choice
      wins over their laptop, always, and this returns 204 without looking at
      the posted value.
    - An unrecognised zone name is dropped, not stored. `known_timezones()` is
      the same host tzdata the Settings validator uses, so the two can never
      disagree about what is valid.
    - No change means no write. This runs on every page load; the common case
      must not be a database write.

    Returns 200 only when something actually changed — the page script uses
    that to decide whether a reload is needed, and the 204s are what stop it
    looping.
    """
    posted = (request.POST.get("timezone") or "").strip()
    if (not request.user.timezone_auto
            or not posted
            or posted not in known_timezones()
            or posted == request.user.timezone):
        return HttpResponse(status=204)

    request.user.timezone = posted
    request.user.save(update_fields=["timezone"])
    record_event("timezone_autodetected", user=request.user, timezone=posted)
    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# Web Push subscriptions (deadline alerts) — accounts/push.py sends,
# accounts/models.py's PushSubscription stores. Settings' Notifications
# toggle POSTs here directly from JS; see the inline script it carries.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def push_subscribe(request):
    """Save (or refresh) one browser's Web Push subscription. Body is the
    Push API's own subscription shape, exactly what `PushSubscription.
    toJSON()` produces client-side:

        {"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}}

    `all_objects`, not `objects.for_user` (coverage_web/tenancy.py's
    documented escape hatch): this is the one deliberate cross-tenant write
    in the whole flow, because the row's owner is exactly what a
    re-subscribe on a shared device is allowed to change — `for_user` can't
    express "create for this user, possibly reassigning a row someone else
    currently owns."

    `update_or_create` keyed on `endpoint` (its own unique constraint): the
    Push API mints a fresh endpoint on every `subscribe()` call, so this is
    a plain create in the overwhelming case, and only ever an update when
    the SAME browser registration posts again — the same device asking a
    second time, or a different account signed into it since. Either way
    the newest POST is the authoritative owner, matching how the Push API
    itself treats "endpoint + keys" as a bearer credential for that
    browser's channel.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid JSON")

    endpoint = (payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip() if isinstance(keys, dict) else ""
    auth = (keys.get("auth") or "").strip() if isinstance(keys, dict) else ""
    if not endpoint or not p256dh or not auth:
        return HttpResponseBadRequest("endpoint and keys.p256dh/keys.auth are required")

    PushSubscription.all_objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
        },
    )
    record_event("push_subscribed", user=request.user)
    return HttpResponse(status=201)


@login_required
@require_POST
def push_unsubscribe(request):
    """Delete the CALLER'S OWN subscription for one endpoint.

    `for_user`, not `all_objects` — the write above is the one deliberate
    cross-tenant exception in this flow, not this one. A POST naming an
    endpoint that belongs to (or never belonged to) someone else deletes
    nothing rather than reaching across tenants; the queryset is
    user-scoped before the endpoint filter even runs.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid JSON")

    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint:
        return HttpResponseBadRequest("endpoint is required")

    PushSubscription.objects.for_user(request.user).filter(endpoint=endpoint).delete()
    record_event("push_unsubscribed", user=request.user)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Legal pages  (/welcome/privacy/, /welcome/terms/)  — no login required
# ---------------------------------------------------------------------------
@require_GET
def privacy(request):
    return render(request, "legal/privacy.html")


@require_GET
def terms(request):
    return render(request, "legal/terms.html")


# ---------------------------------------------------------------------------
# University autocomplete — served from a bundled world-universities dataset
# (accounts/data/universities.json, ~10k names). Returns <option> tags for a
# <datalist>, so the School field autocompletes with no external API call.
# ---------------------------------------------------------------------------
import json as _json
from functools import lru_cache as _lru_cache
from pathlib import Path as _Path

from django.utils.html import escape as _escape

_UNI_PATH = _Path(__file__).resolve().parent / "data" / "universities.json"


@_lru_cache(maxsize=1)
def _universities() -> list:
    """[[name, alpha_two_code], ...], sorted, loaded once per process."""
    with open(_UNI_PATH, encoding="utf-8") as fh:
        return _json.load(fh)


@require_GET
def university_search(request) -> HttpResponse:
    """Prefix/contains match on the university list; returns <option> rows for
    the School field's <datalist>. Prefix matches rank above contains."""
    q = (request.GET.get("school") or request.GET.get("q") or "").strip().lower()
    options: list[str] = []
    if len(q) >= 2:
        starts, contains = [], []
        for name, code in _universities():
            low = name.lower()
            if low.startswith(q):
                starts.append((name, code))
            elif q in low:
                contains.append((name, code))
            if len(starts) >= 12:
                break
        for name, code in (starts + contains)[:12]:
            label = f"{name} ({code})" if code else name
            options.append(f'<option value="{_escape(name)}">{_escape(label)}</option>')
    return HttpResponse("\n".join(options))
