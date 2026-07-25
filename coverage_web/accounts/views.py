"""Views for onboarding, CSV import/export, settings, self-serve deletion,
and the static legal pages (task M5; docs/build-plan.md §7 M5, §10).

Everything except the two legal pages is `@login_required`. Business logic
lives in accounts/services.py; these views are thin request/response glue
plus the onboarding step machine. Templates extend coverage_web's
`base.html`. htmx is used for the in-place profile save on the settings
page and the live firm-search filter during onboarding.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from analytics.events import record_event
from crm.models import Contact, UserFirm
from directory.models import Firm

from . import services
from .forms import (
    CYCLE_SUGGESTIONS,
    REGION_CHOICES,
    TRACK_CHOICES,
    CadenceForm,
    OutreachAssetsForm,
    ProfileForm,
    WeeklyPaceForm,
    WorkAuthorizationForm,
)
from .models import LANGUAGES

# The independently-saving sections of /welcome/settings/, keyed by the value
# their hidden `section` input posts. See accounts/forms.SectionForm.
SECTION_FORMS = {
    form_cls.section: form_cls
    for form_cls in (OutreachAssetsForm, WorkAuthorizationForm, CadenceForm, WeeklyPaceForm)
}

# Step order of the onboarding wizard.
#
# `work_auth` sits immediately after `profile` because it IS profile data —
# a structural fact about the person, in the same breath as school and target
# regions — and because the very next step (picking firms) is the first place
# the fit score it feeds becomes visible. Asking for it after the firm board
# is built would mean showing the user a set of scores computed without it.
#
# `assets` sits after the firm steps and before `import`/`capture`: angles are
# outreach ammunition, so they only need to exist by the time the user starts
# sending, but they read as a natural close to "who you are and what you're
# going after" rather than as part of the mail plumbing at the end.
ONBOARDING_STEPS = ["profile", "work_auth", "firms", "survey", "assets", "import", "capture"]

# Rail labels — the raw step keys don't all title-case into English
# ("work_auth"), and the rail is the user's map of how much is left.
ONBOARDING_STEP_LABELS = {
    "profile": "Profile",
    "work_auth": "Work",
    "firms": "Firms",
    "survey": "Ranking",
    "assets": "Angles",
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
        elif step in ("work_auth", "assets"):
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
        elif step == "survey":
            # Tier ranking: tier-<firm_id> selects; only the user's own rows
            # are ever touched. Tier drives the cadence engine's priorities.
            for uf in UserFirm.objects.for_user(request.user):
                raw = request.POST.get(f"tier-{uf.firm_id}", "")
                tier = int(raw) if raw in ("1", "2", "3") else None
                if tier != uf.tier:
                    UserFirm.all_objects.filter(pk=uf.pk).update(tier=tier)
            record_event("survey_completed", user=request.user)
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
    if step == "survey":
        context["ranked_firms"] = list(
            UserFirm.objects.for_user(request.user).select_related("firm").order_by("firm__name")
        )
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

    # Language is a small standalone form (its own POST carries `language`).
    if request.method == "POST" and "language" in request.POST:
        lang = (request.POST.get("language") or "en").strip()
        if lang in {code for code, _ in LANGUAGES}:
            request.user.language = lang
            request.user.save(update_fields=["language"])
            messages.success(request, "Language updated.")
        return redirect(reverse("accounts:settings"))

    section = request.POST.get("section") if request.method == "POST" else None
    if section in SECTION_FORMS:
        bound = SECTION_FORMS[section](request.POST)
        if bound.is_valid():
            bound.apply_to(request.user)
            messages.success(request, bound.success_message)
            # PRG, same as the profile and language saves: a refresh after
            # saving must not re-POST.
            return redirect(reverse("accounts:settings"))
        # Invalid → fall through and re-render, this section showing errors.
        section_forms[section] = bound
    elif request.method == "POST":
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
    if form is None:
        form = ProfileForm.from_user(request.user)

    return render(
        request,
        "accounts/settings.html",
        {
            "form": form,
            "saved": saved,
            "cycle_suggestions": CYCLE_SUGGESTIONS,
            "capture_address": services.capture_address(request.user),
            "target_firm_count": UserFirm.objects.for_user(request.user).count(),
            "contact_count": Contact.objects.for_user(request.user).count(),
            "languages": LANGUAGES,
            "current_language": request.user.language or "en",
            "assets_form": section_forms["assets"],
            "work_auth_form": section_forms["work_auth"],
            "cadence_form": section_forms["cadence"],
            "pace_form": section_forms["pace"],
            "default_weekly_goal": WeeklyPaceForm.DEFAULT_GOAL,
        },
    )


# ---------------------------------------------------------------------------
# CSV export  (/welcome/export/)
# ---------------------------------------------------------------------------
@login_required
@require_GET
def export(request):
    kind = request.GET.get("kind")
    if kind == "contacts":
        return _csv_download(
            services.contacts_csv(request.user), "coverage-contacts.csv"
        )
    if kind == "touches":
        return _csv_download(
            services.touches_csv(request.user), "coverage-touches.csv"
        )
    return render(
        request,
        "accounts/export.html",
        {
            "contact_count": Contact.objects.for_user(request.user).count(),
        },
    )


def _csv_download(text: str, filename: str) -> HttpResponse:
    resp = HttpResponse(text, content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------
# Self-serve deletion  (/welcome/delete/)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def delete_account(request):
    error = None
    if request.method == "POST":
        typed = (request.POST.get("confirm") or "").strip()
        if typed.lower() != request.user.email.lower():
            error = "That didn't match. Type your email address exactly to confirm."
        else:
            services.delete_user_and_data(request.user)
            # The session's user row is gone; flush it and send them home.
            from django.contrib.auth import logout

            logout(request)
            messages.success(
                request, "Your account and all of your data have been deleted."
            )
            return redirect("/")
    return render(
        request,
        "accounts/delete.html",
        {"error": error},
    )


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
