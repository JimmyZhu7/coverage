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
from billing import credits as billing_credits
from billing import stripe_gateway as billing_stripe_gateway
from capture import gmail_live
from capture.models import ContactProposal, GmailConnection
from crm import campaigns as crm_campaigns, merge as crm_merge, recruitment as crm_recruitment
from crm.models import Campaign, Contact, ContactMerge, UserFirm
from directory.models import Firm

from .models import PushSubscription
from . import onboarding_preview, services, trials as pro_trials
from .forms import (
    CYCLE_SUGGESTIONS,
    REGION_CHOICES,
    TRACK_CHOICES,
    CadenceForm,
    NotificationsForm,
    ProfileForm,
    WeeklyPaceForm,
    WorkAuthorizationForm,
    known_timezones,
)

# The independently-saving sections of /welcome/settings/, keyed by the value
# their hidden `section` input posts. See accounts/forms.SectionForm.
SECTION_FORMS = {
    form_cls.section: form_cls
    for form_cls in (WorkAuthorizationForm, CadenceForm, WeeklyPaceForm, NotificationsForm)
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
ONBOARDING_STEPS = ["profile", "work_auth", "firms", "import"]

# Rail labels — the raw step keys don't all title-case into English
# ("work_auth"), and the rail is the user's map of how much is left.
ONBOARDING_STEP_LABELS = {
    "profile": "Profile",
    "work_auth": "Work",
    "firms": "Firms",
    # The step key stays `import` — it is an internal identifier baked into
    # URLs and tests, and renaming it would buy nothing. The LABEL moved
    # because the step did: it used to have one door (upload a CSV) and now
    # leads with connecting Gmail, which reads the student's own sent mail
    # once and OFFERS the people they have already written to. "Contacts" is
    # what both doors are for.
    "import": "Contacts",
}


# ---------------------------------------------------------------------------
# Onboarding funnel instrumentation.
#
# The wizard was instrumented at its bookends and nowhere else: `signup`
# (accounts/signals.py) and `onboarded` at the bottom of the last step. Between
# those two rows sat four steps, two validation refusals and two Skip links,
# and none of them wrote anything down. So "someone signed up and never
# finished" was a fact with no location — the event stream could not say
# whether they stalled on the profile form, bounced off an error they didn't
# understand, skipped everything, or simply closed the tab on step one. That
# is exactly the question a pilot has to answer about a stranger, and today it
# could not be answered at all.
#
# Four events close it, and the step travels as a PROP rather than in the event
# name (`onboarding_step_viewed` + {"step": "firms"}, never
# `onboarding_firms_viewed`). ONBOARDING_STEPS has already lost a step once and
# relabelled another; a per-step event name would have left the funnel as a set
# of unrelated series that silently stop, where a prop leaves it as one series
# with a value that stops appearing.
#
# Each fires only on the thing it names actually happening — `viewed` on a GET
# that renders a step, `completed` on the branch that really advances, `error`
# on an `is_valid()` refusal, `skipped` only when the Skip link itself was
# followed. Nothing here infers one event from another, so an "assumed
# completed" row can never enter the funnel.
EV_STEP_VIEWED = "onboarding_step_viewed"
EV_STEP_COMPLETED = "onboarding_step_completed"
EV_STEP_ERROR = "onboarding_step_error"
EV_STEP_SKIPPED = "onboarding_step_skipped"

# The Skip links carry `?from=skip&skipped=<step>` because a skip is otherwise
# indistinguishable from any other navigation: the link is a plain GET of the
# NEXT step, so without the marker the only trace is a `viewed` on a step the
# person could equally have reached by pressing Continue. `skipped` names the
# step being left behind — the landing step is already in the URL, and the
# interesting half of "they skipped" is which question they declined.
_SKIP_MARKER = "skip"


def _step_url(step: str) -> str:
    return f"{reverse('accounts:onboarding')}?step={step}"


def _next_step(step: str) -> str:
    """The step after `step` — used by both Continue and Skip, so a step can
    never be made non-skippable by a stale hardcoded target."""
    idx = ONBOARDING_STEPS.index(step)
    return ONBOARDING_STEPS[min(idx + 1, len(ONBOARDING_STEPS) - 1)]


# ---------------------------------------------------------------------------
# Onboarding wizard  (/welcome/)
def _bound_profile_form(request) -> ProfileForm:
    """`ProfileForm` bound to a POST — the one way it should ever be bound.

    The `initial=` is not a rendering nicety. `ProfileForm.__init__` keeps a
    no-longer-offered target cycle in the field's choices by reading
    `self.initial`, and a bound form has no initial of its own, so without
    this the stale value was absent from `choices` during validation and the
    save failed with "Select a valid choice" on a checkbox the form itself
    had rendered ticked and tickable — a profile that could not be saved at
    all. Caught on the demo account (`sa2028_ib`), 2026-08-25.

    Sourced from the STORED row, never from `request.POST`: reading the
    submitted data would be the easy fix and the wrong one, because it would
    let any POST invent its own cycle vocabulary and have it validate.

    `languages` rides along for the same reason: `ProfileForm.__init__` keeps
    a no-longer-listed language in that field's choices the same way, and a
    bound form has nothing else to read it from.
    """
    return ProfileForm(
        request.POST, request.FILES,
        initial={
            "target_cycles": list(request.user.target_cycles or []),
            "languages": list(request.user.languages or []),
        },
    )


def _apply_profile(request, form) -> None:
    """Save a valid ProfileForm, and handle the one side effect saving it can
    have on data the student did not touch.

    `Contact.resolve_region`'s tier 2 files contacts by the student's own
    declared markets, and it is allowed to only while exactly ONE of those
    markets is a deadline market — "the US is where I recruit" entails "this
    person is a US contact". Adding Hong Kong in Settings retires that
    premise, and every row written under it becomes a claim nothing supports:
    silently in a region tab, silently scoping the cadence engine's
    pre-deadline re-ping. Those rows go back to unplaced, and the student is
    told in the same breath as the change that caused it — see `crm.regions`.

    Rows a person set by hand, and rows a single-market firm answered, are
    untouched: neither premise moved.
    """
    from crm import regions as crm_regions

    previous = list(request.user.regions or [])
    form.apply_to(request.user)
    if crm_regions.declared_market(previous) and not crm_regions.declared_market(
        request.user.regions
    ):
        result = crm_regions.unplace_declared_regions(
            request.user, previous_regions=previous
        )
        if result.message:
            messages.info(request, result.message)


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
            form = _bound_profile_form(request)
            if form.is_valid():
                _apply_profile(request, form)
                record_event(EV_STEP_COMPLETED, user=request.user, step=step)
                return redirect(_step_url(_next_step(step)))
            # invalid → fall through and re-render this step with errors.
            # Field NAMES only, never the submitted values: the point is to
            # see which control a stranger got stuck on, and the values are
            # someone's name, school and photo.
            record_event(
                EV_STEP_ERROR, user=request.user, step=step,
                fields=sorted(form.errors),
            )
        elif step == "work_auth":
            # Both reuse the settings-page section forms, so onboarding and
            # Settings can never disagree about what's valid. Every field on
            # them is optional, so an untouched form validates and writes
            # nothing — "Continue" without answering is as skippable as the
            # Skip link, and neither leaves a guessed default behind.
            section_form = SECTION_FORMS[step](request.POST)
            if section_form.is_valid():
                section_form.apply_to(request.user)
                record_event(EV_STEP_COMPLETED, user=request.user, step=step)
                return redirect(_step_url(_next_step(step)))
            # invalid → re-render this step with errors
            record_event(
                EV_STEP_ERROR, user=request.user, step=step,
                fields=sorted(section_form.errors),
            )
        elif step == "firms":
            picked = request.POST.getlist("firms")
            services.set_target_firms(request.user, picked)
            # The count, not the firm ids. "Continue with nothing ticked" and
            # "Continue with eleven firms" are the same event today and read
            # as the same thing in the funnel, which is the difference between
            # a step that worked and a step that was walked past.
            record_event(
                EV_STEP_COMPLETED, user=request.user, step=step,
                firms=len(picked),
            )
            return redirect(_step_url(_next_step(step)))
        elif step == "import":
            # Last step — finishes onboarding. The CSV upload itself posts to
            # the separate import_contacts view; this step's own Continue
            # just closes the wizard out.
            record_event(EV_STEP_COMPLETED, user=request.user, step=step)
            if request.user.onboarded_at is None:
                request.user.onboarded_at = timezone.now()
                request.user.save(update_fields=["onboarded_at"])
                record_event("onboarded", user=request.user)
            messages.success(request, "You're all set. Welcome to Coverage.")
            # Land on Today — the working surface — not back in Settings.
            return redirect("/app/")

    if request.method == "GET":
        # GET only. The render below is shared with a fallen-through invalid
        # POST, and recording a `viewed` there would count every validation
        # error as a fresh page view — the one thing that would make the
        # stuck-user signal look like engagement.
        skipped = request.GET.get("skipped")
        if request.GET.get("from") == _SKIP_MARKER and skipped in ONBOARDING_STEPS:
            record_event(
                EV_STEP_SKIPPED, user=request.user, step=skipped, landed_on=step
            )
        record_event(EV_STEP_VIEWED, user=request.user, step=step)

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
    if step == "import":
        # The last step now leads with Connect Gmail, so it needs the same
        # availability fact the Settings page reads — an unconfigured deploy
        # must not render a button whose view raises Http404. Same helper,
        # so the two surfaces can never disagree about whether Gmail Live
        # exists here.
        context["gmail_live"] = _gmail_live_context(request.user)
    # The live panel's first paint, server-side. Rendering it here rather
    # than letting htmx fetch it on load is what keeps the wizard working
    # with JS off: the panel is correct before any script runs, and the
    # htmx refresh below only ever replaces it with a newer version of the
    # same partial.
    context["preview"] = onboarding_preview.build(step, request, request.user)
    return render(request, "accounts/onboarding.html", context)


@login_required
@require_GET
def onboarding_preview_view(request):
    """The live panel, on its own — the htmx swap target for step 1's chips,
    step 2's matrix and step 3's firm tiles.

    Read-only by construction: a GET, no form handling, no writes, and it
    shares not one line of the step machine above. Everything it renders is
    a real query (accounts/onboarding_preview.py); if the student's current
    answers match nothing it renders an empty state rather than the previous
    answer's rows.
    """
    step = request.GET.get("step") or "profile"
    if step not in ONBOARDING_STEPS:
        step = "profile"
    return render(
        request,
        "accounts/_onboarding_preview.html",
        {"preview": onboarding_preview.build(step, request, request.user),
         "step": step},
    )


# A walkthrough found the Firms step showing all 131 firms alphabetically
# with equal weight, no matter what the student had just told step 2
# (regions) and step 1 (tracks) — a US-only IB student saw Hong Kong quant
# funds and consulting shops with the same prominence as their own targets.
#
# REORDER, not filter. Two things rule filtering out:
#
# 1. `directory.recommend.role_matches_regions` already spells out why
#    `Firm.regions`/`Firm.tracks` are too coarse to gate ANYTHING on: they
#    are firm-level, and "a firm can run desks in five markets and post a
#    role in only one of them" is the documented reason the Opportunities
#    feed reads a ROLE's own region instead of trusting the firm's list for
#    that axis. A firm picker has no role to fall back on — the firm-level
#    list is all there is — so hard-filtering the board on it risks hiding a
#    firm the student actually wants over a list that was never precise
#    enough to exclude on.
# 2. The brief this fix answers says it plainly: "a student's targets
#    legitimately exceed their declared tracks." Browsing the full board and
#    picking what you want is a real position, not a bug — the bug is that
#    a US-only IB student has to scroll the SAME distance past HK quant
#    funds as a HK/quant student would.
#
# So both surfaces below only ever REGROUP: everything the student can pick
# stays reachable in one scroll, in the same alphabetical order it always
# was, and a firm outside their declared regions/tracks never disappears —
# it just isn't first. An already-picked firm is never affected by this at
# all, because nothing here removes rows; it only decides which group a row
# prints in.
def _split_by_declared_profile(firms, user):
    """`firms` split into (matches declared regions/tracks, everything else),
    order preserved within each half. Both halves together are still every
    firm passed in — see the module note above for why this never excludes.

    OR, not AND: a track hit with no region hit (or vice versa) is still
    worth surfacing first — the two axes are independent facts a student
    stated, and requiring both would bury a firm that matches exactly one
    of them behind firms that match neither."""
    regions = set(r.lower() for r in (getattr(user, "regions", None) or ()) if r)
    tracks = set(getattr(user, "tracks", None) or ())
    if not regions and not tracks:
        return [], list(firms)
    matches, rest = [], []
    for f in firms:
        f_regions = set(r.lower() for r in (f.regions or ()))
        f_tracks = set(f.tracks or ())
        hit = bool(regions and f_regions & regions) or bool(tracks and f_tracks & tracks)
        (matches if hit else rest).append(f)
    return matches, rest


def _firm_picker_context(user) -> dict:
    firms = list(Firm.objects.all().order_by("name"))
    matching_firms, other_firms = _split_by_declared_profile(firms, user)
    selected = set(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    return {
        # Kept alongside the split for anything that still wants the plain
        # full list (e.g. a JS-off screen reader announcement of the total).
        "firms": firms,
        "matching_firms": matching_firms,
        "other_firms": other_firms,
        "selected_firm_ids": selected,
        "region_choices": REGION_CHOICES,
        "track_choices": TRACK_CHOICES,
    }


# The three tiers shown as columns, in display order. Untiered UserFirm rows
# (tier=None — the Network board's own overflow bucket for a card dragged off
# without a home) are deliberately absent from this list: Settings' board is
# scoped to firms that are actually ON one of the three tiers, and a firm with
# no tier at all is one drag away on Network, not a fourth column here.
TARGET_FIRM_TIERS = (1, 2, 3)


def _target_firms_context(user) -> dict:
    """Firms grouped by tier for the editable board, plus every firm the
    user ISN'T tracking yet, for the add-a-firm search. Both queries key off
    the same `UserFirm` rows so the two halves of the section can never
    show a firm as simultaneously trackable and already-tracked."""
    tracked = list(
        UserFirm.objects.for_user(user)
        .filter(tier__in=TARGET_FIRM_TIERS)
        .select_related("firm")
        .order_by("tier", "firm__name")
    )
    by_tier: dict[int, list] = {tier: [] for tier in TARGET_FIRM_TIERS}
    tracked_ids = set()
    for uf in tracked:
        by_tier[uf.tier].append(uf.firm)
        tracked_ids.add(uf.firm_id)
    # Untracked means no UserFirm row AT ALL, tiered or not — a firm sitting
    # in the Network board's untiered overflow already has a row and must
    # not also offer itself as "add me", which would create a second row
    # violating the (user, firm) uniqueness constraint the instant tried.
    all_tracked_ids = set(
        UserFirm.objects.for_user(user).values_list("firm_id", flat=True)
    )
    untracked = list(Firm.objects.exclude(id__in=all_tracked_ids).order_by("name"))
    # Same regroup as the onboarding Firms step (`_split_by_declared_profile`)
    # applied to the same "add a firm" search — Settings must not form a
    # second opinion about what's relevant to this student just because it
    # is a different page reading the same profile.
    matching_untracked, other_untracked = _split_by_declared_profile(untracked, user)
    return {
        "firm_tiers": [
            {"tier": tier, "firms": by_tier[tier]} for tier in TARGET_FIRM_TIERS
        ],
        "untracked_firms": untracked,
        "matching_untracked_firms": matching_untracked,
        "other_untracked_firms": other_untracked,
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
        {
            "result": result,
            "from_onboarding": request.GET.get("from") == "welcome",
            # Only rendered when `result.unmatched_firms` is non-empty, but
            # cheap enough (the whole directory, ~127 rows) to just always
            # pass rather than special-case the query.
            "all_firms": Firm.objects.all().order_by("name"),
        },
    )


@login_required
@require_POST
def import_link_firm(request):
    """The import summary's "Link to..." fix-up. A plain redirect-after-POST
    back to the (file-less) import page — the just-parsed `result` from the
    upload that surfaced this group is gone either way once a new request
    starts, so there is nothing to gain from re-rendering it inline, and a
    redirect means a page refresh can't re-submit the link."""
    contact_ids = [cid for cid in request.POST.getlist("contact_id") if cid.isdigit()]
    firm_id = request.POST.get("firm_id")
    target = reverse("accounts:import")
    if request.GET.get("from") == "welcome" or request.POST.get("from") == "welcome":
        target += "?from=welcome"
    if not contact_ids or not firm_id:
        messages.error(request, "Choose a firm to link.")
        return redirect(target)
    linked = services.link_contacts_to_firm(request.user, contact_ids, firm_id)
    if linked:
        firm_name = Firm.objects.filter(pk=firm_id).values_list("name", flat=True).first()
        messages.success(
            request,
            f"Linked {linked} contact{'' if linked == 1 else 's'} to {firm_name}.",
        )
    else:
        messages.error(request, "Couldn't link those contacts. Try again.")
    return redirect(target)


@login_required
@require_GET
def import_template(request):
    resp = HttpResponse(services.import_template_csv(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="coverage-contacts-template.csv"'
    return resp


# ---------------------------------------------------------------------------
# Settings  (/welcome/settings/)
# ---------------------------------------------------------------------------
def _gmail_live_context(user) -> dict:
    """Cheap enough to compute on every settings render — the point is that
    a connection that quietly went `revoked` should be visible on the page a
    student would actually check, not discoverable only by the sync silently
    doing nothing."""
    if not gmail_live.is_configured():
        return {"available": False}
    # Real-time sync is Pro-only (docs/pricing-rebalance-plan.md §7) — the
    # template reads this to show the real-time toggle as Pro-gated while
    # keeping Connect/Scan Now open to every plan. Mirrors the exact check
    # `capture.gmail_live.connect_gmail`/`renew_watches` gate on.
    is_pro = getattr(user, "plan", "") == "pro"
    # "Pro trial · N days left" — None for a permanent Pro/Free account, an
    # int only while an actual trial (accounts.trials) is still running.
    trial_days_left = pro_trials.trial_days_left(user)
    connection = GmailConnection.all_objects.select_related("user").filter(user=user).first()
    if connection is None:
        return {
            "available": True,
            "connected": False,
            "is_pro": is_pro,
            "trial_days_left": trial_days_left,
        }
    return {
        "available": True,
        "connected": True,
        "is_pro": is_pro,
        "trial_days_left": trial_days_left,
        "gmail_address": connection.gmail_address,
        "status": connection.status,
        "last_notification_at": connection.last_notification_at,
        "backfill_status": connection.backfill_status,
        "backfill_stats": connection.backfill_stats,
        "rescan_status": connection.rescan_status,
        "rescan_completed_at": connection.rescan_completed_at,
        "rescan_stats": connection.rescan_stats,
        # Whether real-time is actually live right now — distinct from
        # `is_pro`: a Pro connection whose watch registration hasn't landed
        # yet (queued for the next `gmail_watch_renew` tick) is Pro but not
        # yet live; a Free connection is never live at all.
        "watch_active": connection.watch_expiration is not None,
        # The one thing the template actually branches on for the button's
        # disabled state — kept as a computed bool here rather than a
        # `{% if rescan_status == "pending" or rescan_status == "running" %}`
        # in the template, so the "what counts as in-flight" rule lives in
        # one place.
        "rescan_in_progress": connection.rescan_status in ("pending", "running"),
        # Free's once-per-GMAIL_FREE_RESCAN_INTERVAL_DAYS throttle
        # (settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS) — the SAME function
        # `capture.views.gmail_rescan` enforces server-side, so the button's
        # disabled state and the date it names can never quietly disagree
        # with what a POST would actually do. `None` for Pro/trial or a
        # never-scanned Free connection (both mean "not throttled").
        "rescan_unlocks_at": gmail_live.free_rescan_unlocks_at(connection),
    }


def _credits_context(user) -> dict:
    """Settings' own credit meter (docs/credit-system-plan.md §6 — the
    other half of "show the meter", alongside the chat composer's). Cheap
    enough to compute on every settings render, same posture as
    `_gmail_live_context` right above it."""
    plan = billing_credits.plan_config(user)
    return {
        "balance": billing_credits.balance(user),
        "plan_label": "Pro" if plan["plan"] == billing_credits.PRO else "Free",
        "monthly_grant": plan["monthly_grant"],
        "month_usage": billing_credits.month_usage(user),
        "refill_date": billing_credits.next_refill_date(user),
        # "Buy more credits" (billing/stripe_gateway.py). Always rendered,
        # like the rest of this card — the disabled/"Coming soon" state
        # IS the render when Stripe isn't configured, not a hidden block,
        # so Jimmy can see the feature exists before he's set Stripe up.
        "stripe_configured": billing_stripe_gateway.is_configured(),
        "credit_packs": billing_stripe_gateway.CREDIT_PACKS,
        # "Pro trial · N days left" (accounts.trials) — None outside an
        # active trial, so the plan line reads as a plain "Pro Plan"/"Free
        # Plan" for a permanent account exactly as it did before trials
        # existed.
        "trial_days_left": pro_trials.trial_days_left(user),
    }


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
    # changed nothing. The column followed on 2026-09-01 (migration 0015).
    # `User.languages`, plural, is a different fact and not a revival of this
    # one: the languages a student can WORK in, read by the feed's eligibility
    # lens, saved through the Profile section like every other fact about
    # them. The interface control returns with an actual i18n pass, not before.
    section = request.POST.get("section") if request.method == "POST" else None
    if section in SECTION_FORMS:
        bound = SECTION_FORMS[section](request.POST)
        if bound.is_valid():
            bound.apply_to(request.user)
            messages.success(request, bound.success_message)
            # PRG, same as the profile save: a refresh after saving must
            # not re-POST. `success_fragment` is empty for every section whose
            # save is a button press; a section that saves on change sets it so
            # the reader comes back to the card they were on.
            target = reverse("accounts:settings")
            if bound.success_fragment:
                target = f"{target}#{bound.success_fragment}"
            return redirect(target)
        # Invalid → fall through and re-render, this section showing errors.
        section_forms[section] = bound
    elif request.method == "POST" and section == "profile":
        # Requires the explicit marker, like every other section — see the
        # `elif request.method == "POST":` branch just below for why. The
        # profile <form> in settings.html now carries
        # `<input type="hidden" name="section" value="profile">` to match.
        form = _bound_profile_form(request)
        if form.is_valid():
            _apply_profile(request, form)
            saved = True
            if request.headers.get("HX-Request"):
                # htmx in-place save: swap back the form with a saved flag.
                # No `cycle_months` here (2026-08-29): the deadline-density
                # strip came off Settings entirely, and `_profile_form.html`
                # no longer renders one — see its own comment.
                return render(
                    request,
                    "accounts/_profile_form.html",
                    {"form": ProfileForm.from_user(request.user),
                     "saved": True, "cycle_suggestions": CYCLE_SUGGESTIONS},
                )
            messages.success(request, "Profile saved.")
            return redirect(reverse("accounts:settings"))
        if request.headers.get("HX-Request"):
            # An INVALID htmx save has to come back as the same partial the
            # valid one does. Falling through here re-rendered the whole
            # settings page and htmx swapped that entire document into
            # `#profile-fields` — a second nav, a second Profile card, a
            # second everything, nested inside the form the person was
            # trying to fix. Latent until now (a ProfileForm with every field
            # optional had almost no reachable failure), and reachable the
            # moment `school_emails` gained a real refusal.
            return render(
                request,
                "accounts/_profile_form.html",
                {"form": form, "cycle_suggestions": CYCLE_SUGGESTIONS},
            )
    elif request.method == "POST":
        # A POST that names neither a recognised `section` nor "profile" —
        # a stale form cached before a section was added/renamed, or a
        # hand-crafted request. This used to fall straight through to
        # `ProfileForm(request.POST, ...)` unconditionally: every ProfileForm
        # field is `required=False`, so an EMPTY POST validated, and its
        # `apply_to` blanked all six profile fields (name/school/class_year/
        # target_cycles/regions/tracks) with no error and no confirmation.
        # Requiring the explicit marker turns an unrecognised POST into a
        # no-op re-render instead of a silent profile wipe.
        pass
    if form is None:
        form = ProfileForm.from_user(request.user)

    contacts = Contact.objects.for_user(request.user)
    contact_count = contacts.count()
    # Split once here rather than in the template: Django templates have no
    # "any classified" or "filter by attribute" built in, and the settled
    # half of the list now renders behind a click-through (2026-08-29, "do
    # not show blatantly in the system") rather than inline with the ones
    # still asking a question.
    campaign_cards = crm_campaigns.campaign_cards(request.user)
    campaigns_settled = [c for c in campaign_cards if c["is_classified"]]
    return render(
        request,
        "accounts/settings.html",
        {
            "form": form,
            "saved": saved,
            "cycle_suggestions": CYCLE_SUGGESTIONS,
            "gmail_live": _gmail_live_context(request.user),
            # Bulk sends we detected in this user's own outbound mail, and the
            # one question they answer about each. Read-only here — the answer
            # POSTs to crm:classify_campaign. Detection itself is NOT run on a
            # page load: it walks every outbound touch, and a settings render
            # is the wrong place to pay for that. It runs at the end of a
            # capture sync and from `manage.py detect_campaigns`.
            "campaigns": campaign_cards,
            "campaigns_settled": campaigns_settled,
            # People the user dismissed from the "Found in your inbox" lane.
            # THE ONLY PLACE THEY EXIST AFTER THE TAP: dismissal is permanent
            # for the scan (capture.models.ContactProposal), so without a
            # surface like this one a mis-tap buries somebody with no name, no
            # date, and no way back. Newest first — a mis-tap is noticed
            # within minutes, not months. Capped at 100 as a rendering guard;
            # the export carries the full list either way. Restore POSTs to
            # crm:proposal_restore.
            "dismissed_proposals": list(
                ContactProposal.objects.for_user(request.user)
                .filter(status=ContactProposal.STATUS_DISMISSED)
                .select_related("firm")
                .order_by("-resolved_at", "-id")[:100]
            ),
            # Pairs of contact cards that read as one person (crm.merge —
            # the identity ladder's suggestive rung, capture.discovery.
            # duplicate_evidence). Computed live on render, never stored:
            # a stored suggestion goes stale three ways and every staleness
            # is a wrong card. Only the user's ANSWERS persist, as the
            # ContactMerge rows two keys down. Nothing merges without the
            # tap — a false merge fuses two histories with no clean undo.
            "merge_suggestions": crm_merge.candidate_pairs(request.user),
            # Merged pairs, newest first, each with its Undo — the same
            # mis-tap-visible-within-minutes posture as the dismissed list.
            "recent_merges": list(
                ContactMerge.objects.for_user(request.user)
                .filter(status=ContactMerge.STATUS_MERGED)
                .select_related("primary", "duplicate")
                .order_by("-resolved_at", "-id")[:50]
            ),
            "campaign_kind_other": Campaign.KIND_OTHER,
            "campaign_kind_recruiting": Campaign.KIND_RECRUITING,
            "campaign_kind_unclassified": Campaign.KIND_UNCLASSIFIED,
            "credits": _credits_context(request.user),
            "gmail_free_rescan_interval_days": django_settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS,
            # There is no `target_firm_count` here any more. Your Data used to
            # print it as a bare number three cards below the Target Firms
            # board, which states the same total as three live per-tier counts
            # you can drag firms between. One page saying one number twice, in
            # the weaker of the two places.
            "contact_count": contact_count,
            # Split out rather than folded in: "Contacts: 137" counted archived
            # rows while the Network page showed 112, so the two pages
            # disagreed about the same number. Stating the population is rule
            # D3 — a count must mean what it says.
            "archived_count": contacts.filter(archived=True).count(),
            # The other three ways a contact is off the Network board. These
            # used to be counted and linked from a meta strip above the board
            # itself; that strip was removed on 2026-08-28 ("take all of this
            # away, hide") and the guarantee it made — the board says what it
            # is not showing — landed here, on the page whose whole job is
            # stating what Coverage holds. Same hide-when-zero rule the strip
            # used: a student who has parked nobody is not shown a route to an
            # empty list.
            "parked_count": contacts.filter(
                archived=False, thread_state="parked"
            ).count(),
            "campaign_hidden_count": len(crm_campaigns.excluded_contact_ids(request.user)),
            "unrelated_count": len(crm_recruitment.hidden_contact_ids(request.user)),
            "work_auth_form": section_forms["work_auth"],
            "cadence_form": section_forms["cadence"],
            "pace_form": section_forms["pace"],
            "notifications_form": section_forms["notifications"],
            # Push notifications (deadline alerts). Blank key = the toggle
            # renders as unavailable rather than a button that dead-ends —
            # same "Setup Needed" posture the social sign-in buttons use for
            # an unconfigured provider. See accounts/push.py.
            "vapid_public_key": django_settings.VAPID_PUBLIC_KEY,
            "push_subscribed": PushSubscription.objects.for_user(request.user).exists(),
            **_security_context(request.user),
            **_target_firms_context(request.user),
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
# total and permanent. Signing out other devices is recoverable BY ACTION
# (sign in again), so a single honest confirm button is the right friction —
# more would train people to click through it.


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
#
# A key here that services._DELETE_ORDER does not produce is DEAD, silently:
# `_deletion_receipt` reads the counts with `counts.get(key)`, so the line can
# never render and nothing fails loudly to say so. That is exactly how the
# `capture_events` entry survived the BCC/forward capture pipeline's removal
# (2026-08-19) — the model it named was gone, its key stopped appearing in the
# counts dict, and the label sat here describing nothing. `test_delete_receipt.
# test_every_receipt_label_names_a_table_that_is_actually_deleted` is the guard
# that turns the next such drift into a failing test instead of dead weight.
_DELETED_LABELS: list[tuple[str, str, str]] = [
    ("contacts", "contact", "contacts"),
    ("touches", "touch", "touches"),
    ("user_firms", "target firm", "target firms"),
    ("user_opportunities", "tracked role", "tracked roles"),
    ("tasks", "task", "tasks"),
    ("chat_debriefs", "chat debrief", "chat debriefs"),
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
