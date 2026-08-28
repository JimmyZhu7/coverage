"""CRM UI views (docs/build-plan.md §4 weekly list, §5 mailto-BCC compose,
§6 fit score). Mounted at /app/ (see coverage_web/urls.py); every view is
login-required and scopes every private-zone read with
`.for_user(request.user)` (see coverage_web/tenancy.py).

The domain engines are PURE (they read no DB, no wall clock): this layer
fetches the user's rows, shapes them into the plain dicts the engines want,
calls them with an explicit `as_of=timezone.now()`, and renders the result.
State-mutating writes go through `crm.services` (the reviewed pipeline
adapter) only — never a hand-rolled UPDATE.
"""

from __future__ import annotations

import re
from datetime import timedelta
from math import ceil
from typing import Any
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count as models_Count, Max as models_Max, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from analytics.events import record_event
from analytics.models import UserOpportunity
from billing import credits as billing_credits
from billing.models import CreditLedger
from coverage_domain import cadence, scoring
from coverage_domain.pipeline import CHANNELS, MANUAL_OVERRIDE_KIND, TOUCH_TRANSITIONS
from crm.forms import ChatDebriefForm, ContactForm
from directory.classify import REGION_LABELS, TARGET_BUCKETS
from directory.models import Firm, FirmDate, Opportunity

from . import (
    ai_brief, ai_summary, campaigns, coverage, debrief as debrief_svc,
    recruitment, services, sourcing,
)
from .models import (
    CalendarEvent, Campaign, ChatDebrief, Contact, Touch, UserFirm,
)


# The Today engine lives in crm/today.py; the shared helpers in crm/utils.py.
# Both are re-exported here because this module is the import surface the
# URLconf and a few hundred test assertions already use — the split changes
# where code LIVES, not how it is reached.
from .today import (  # noqa: F401
    PACE_TOUCH_KINDS,
    PROPOSALS_RENDER_CAP,
    TODAY_PLAN_MAX,
    TODAY_PLAN_MIN,
    TUNABLE_CADENCE_PARAMS,
    WEEKLY_TOUCH_GOAL,
    _INBOUND_TOUCH_KINDS,
    _build_actions,
    _cadence_params,
    _cockpit_context,
    _daily_cap,
    _dashboard_context,
    _pace,
    _schedule,
    _today_class,
    _today_sort_key,
    _workdays_left,
    play_dismiss,
    today_act,
    today_park_all,
    week,
)
from .utils import (  # noqa: F401
    CHANNEL_LABELS,
    FIRM_DATE_LABELS as _FIRM_DATE_LABELS,
    TOUCH_KIND_LABELS,
    WARMTH_ORDER,
    _calendar_days_ago,
    _clock,
    _confidence_label,
    _mailto,
    _touch_dicts,
    _warmth_pct,
)

# ---------------------------------------------------------------------------
# 1b. Post-chat debrief — the structured capture of what a chat taught you.
# ---------------------------------------------------------------------------
@login_required
def debrief(request: HttpRequest, pk: int) -> HttpResponse:
    """The debrief form for one `chat` touch (`pk`), and the saved view of
    it afterwards. Scoped through `.for_user`, so another tenant's touch id
    404s exactly like a missing one.

    On save, `crm.debrief.record` does the bookkeeping (note append,
    referral contact, tasks) idempotently, then this view OFFERS the
    advocate promotion when the answer was "yes" — it never performs it.
    A warmth change is a claim about a relationship, and the user gets to
    make that claim on purpose (via `debrief_promote` below), not as a side
    effect of ticking a radio button."""
    touch = get_object_or_404(
        Touch.objects.for_user(request.user).select_related("contact"), pk=pk, kind="chat"
    )
    existing = ChatDebrief.objects.for_user(request.user).filter(touch=touch).first()

    if request.method == "POST":
        form = ChatDebriefForm(request.POST, instance=existing)
        if form.is_valid():
            saved, made = debrief_svc.record(
                request.user,
                touch,
                **{k: v for k, v in form.cleaned_data.items() if v not in (None, "")},
            )
            record_event("chat_debriefed", user=request.user)
            notes = []
            if made.get("intro_contact"):
                notes.append(f"added {made['intro_contact'].name}")
            if made.get("intro_task") or made.get("date_task"):
                n = bool(made.get("intro_task")) + bool(made.get("date_task"))
                notes.append(f"{n} task{'s' if n > 1 else ''} created")
            # `made` is empty exactly when `record` wrote nothing — an
            # unchanged resubmit. Saying "Debrief saved." there is a lie the
            # user can't check, and it was covering a real bug: the note
            # append used to be gated on `learned` being EMPTY, so every edit
            # to the text was silently discarded under this same green
            # banner. The gate is fixed (see crm.debrief.record); the message
            # now also only claims what happened.
            if made:
                messages.success(
                    request,
                    "Debrief saved" + (f": {', '.join(notes)}." if notes else "."),
                )
            else:
                messages.info(request, "No changes to save.")
            return redirect("crm:debrief", pk=touch.pk)
    else:
        form = ChatDebriefForm(instance=existing)

    # The promotion is offered only while it would actually change
    # something: answered yes, not taken yet, not already an advocate.
    offer_promotion = bool(
        existing
        and existing.advocate_answer == "yes"
        and not existing.promoted
        and touch.contact.warmth != "advocate"
    )
    return render(
        request,
        "crm/debrief.html",
        {
            "touch": touch,
            "contact": touch.contact,
            "form": form,
            "debrief": existing,
            "offer_promotion": offer_promotion,
        },
    )


@login_required
@require_POST
def daily_brief(request: HttpRequest) -> HttpResponse:
    """Generate (or return) today's brief, for the htmx node crm/week.html
    draws when nothing is cached yet.

    WHY THIS ENDPOINT EXISTS. `crm.today.week` used to call
    `assistant.brief.get_or_build` inline, which put a synchronous Anthropic
    request on the Today page's own response. Measured: 55.7ms with the row
    already present, 2079.9ms when the model took 2.0s — the latency landed
    on the page almost exactly 1:1 — and `assistant.client`'s 45s timeout
    made the worst case a 45-second Today page. It happened once per student
    per day, on the FIRST load of the day, which is the morning one.

    Now the page renders immediately and this fills the card in behind it.
    The cost of the split is that this request recomputes the cockpit to get
    the queue the prompt summarises; that is one extra background request per
    student per day, against an interactive page in ~50ms instead of seconds.

    POST for the reason `contact_ai_brief` gives: once ANTHROPIC_API_KEY is
    set this can spend money, so it must never fire from a prefetch, a
    reload, or a crawler. `get_or_build` is still the only writer and still
    caps itself at one model call per student per calendar day, so even a
    replayed POST costs nothing after the first.

    Renders the same partial the page does. Returns it EMPTY (not an error)
    when the feature is dark or there was nothing worth saying — the card
    simply never appears, which is the behaviour this has always had.
    """
    from assistant.brief import get_or_build
    from assistant.situation import build_situation

    cockpit = _cockpit_context(request.user)
    situation = build_situation(request.user)
    text = get_or_build(
        request.user,
        cockpit.get("_actions_for_brief") or [],
        situation=situation.get("events"),
    )
    return render(request, "crm/_daily_brief.html", {"daily_brief": text})


@login_required
@require_POST
def debrief_dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    """Skip this debrief. Re-renders the cockpit so the card disappears in
    place, like the other Today quick actions."""
    touch = get_object_or_404(
        Touch.objects.for_user(request.user).select_related("contact"), pk=pk, kind="chat"
    )
    debrief_svc.dismiss(request.user, touch)
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def debrief_promote(request: HttpRequest, pk: int) -> HttpResponse:
    """Accept the offered advocate promotion. The state change itself goes
    through `crm.services.set_contact_state`, which writes the audit touch
    — see `crm.debrief.promote`."""
    touch = get_object_or_404(Touch.objects.for_user(request.user), pk=pk, kind="chat")
    row = get_object_or_404(
        ChatDebrief.objects.for_user(request.user), touch=touch
    )
    debrief_svc.promote(row)
    record_event("advocate_promoted", user=request.user, source="debrief")
    messages.success(request, f"{row.contact.name} is now an advocate.")
    return redirect("crm:contact_detail", pk=row.contact_id)


def _dismiss_undo_offer(proposals: list) -> dict:
    """The one-shot Undo strip the cockpit draws right after a dismissal.

    IN THE RESPONSE, NOT IN THE SESSION — deliberately, and unlike
    `directory.views.BULK_SAVE_SESSION_KEY`. That batch had to survive a
    redirect to a different page, so it needed somewhere to live; this one is
    handed straight back in the same htmx swap that removed the card, so the
    ids can just ride in the markup. The consequence is the right lifetime for
    free: the offer exists for exactly as long as this render of the cockpit,
    and any later action (or a refresh) replaces it with a cockpit that has no
    strip. Nothing goes stale because nothing is stored.

    The durable path is `accounts` Settings > Dismissed from your inbox, which
    is what the strip's second sentence points at. This is the in-the-moment
    fix for a mis-tap, not the archive.
    """
    return {
        "ids": ",".join(str(p.pk) for p in proposals),
        "count": len(proposals),
        # Named while there is one name to say. Beyond that the count carries
        # it — "Dismissed 9 people" is a fact; listing nine names is a wall.
        "who": proposals[0].name if len(proposals) == 1 else "",
    }


@login_required
@require_POST
def proposal_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """One tap on a contact proposal: accept creates the contact through
    `capture.discovery.accept` (capture_discover's own creation contract —
    warmth earned via the ratchet, archived matches never resurrected), and
    dismiss hides it from every future scan. Re-renders the cockpit like every
    other Today quick action.

    A dismissal comes back with the Undo strip in the same swap (see
    `_dismiss_undo_offer`). Dismissal is permanent for the SCAN by design, so
    the one thing it must not also be is silent."""
    from capture import discovery
    from capture.models import ContactProposal

    if verb not in ("accept", "dismiss"):
        return HttpResponse(status=400)
    proposal = get_object_or_404(
        ContactProposal.objects.for_user(request.user), pk=pk,
        status=ContactProposal.STATUS_PENDING,
    )
    context = None
    if verb == "accept":
        contact = discovery.accept(proposal)
        record_event(
            "contact_proposal_accepted", user=request.user, source="today",
            contact_id=contact.id if contact else None,
        )
    else:
        discovery.dismiss(proposal)
        record_event("contact_proposal_dismissed", user=request.user, source="today")
        context = _cockpit_context(request.user)
        context["dismiss_undo"] = _dismiss_undo_offer([proposal])
    return render(
        request, "crm/_cockpit.html", context or _cockpit_context(request.user)
    )


@login_required
@require_POST
def mail_fact_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """One tap on a mail-fact card (capture.mailfacts): `undo` reverses an
    automatic action exactly (address restored, snooze cleared, note line
    removed, withdrawn proposal re-pended — see `mailfacts.undo` for the
    only-if-unchanged guards), `dismiss` acknowledges the card. Re-renders
    the cockpit like every other Today quick action."""
    from capture import mailfacts
    from capture.models import MailFact

    if verb not in ("undo", "dismiss"):
        return HttpResponse(status=400)
    fact = get_object_or_404(
        MailFact.objects.for_user(request.user), pk=pk,
        status__in=[MailFact.STATUS_PENDING, MailFact.STATUS_APPLIED],
    )
    if verb == "undo":
        mailfacts.undo(fact)
        record_event("mail_fact_undone", user=request.user, source="today")
    else:
        mailfacts.dismiss(fact)
        record_event("mail_fact_dismissed", user=request.user, source="today")
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def proposals_bulk(request: HttpRequest, verb: str) -> HttpResponse:
    """Accept or dismiss every pending proposal at once. Same per-row paths
    as the single-tap view — the bulk button is a loop, not a second
    contract. "Dismiss all" is the single most destructive tap in this lane,
    so it comes back with the same Undo strip a one-by-one dismiss does,
    naming the count it just buried."""
    from capture import discovery
    from capture.models import ContactProposal

    if verb not in ("accept", "dismiss"):
        return HttpResponse(status=400)
    # Exactly the slice the cockpit rendered — same status filter, same
    # ordering, same cap (`crm.today.PROPOSALS_RENDER_CAP`). The buttons
    # promise "everyone listed here", and acting on the unbounded pending
    # set instead once dismissed-forever 28 people the lane never showed
    # (52 pending on the founder's first whole-mailbox scan, 24 rendered).
    # Whatever remains past the cap stays pending and fills the lane on the
    # re-render this view returns.
    pending = list(
        ContactProposal.objects.for_user(request.user)
        .filter(status=ContactProposal.STATUS_PENDING)
        .order_by("created")[:PROPOSALS_RENDER_CAP]
    )
    for proposal in pending:
        if verb == "accept":
            discovery.accept(proposal)
        else:
            discovery.dismiss(proposal)
    if pending:
        record_event(
            f"contact_proposals_bulk_{verb}", user=request.user, source="today",
            count=len(pending),
        )
    context = _cockpit_context(request.user)
    if verb == "dismiss" and pending:
        context["dismiss_undo"] = _dismiss_undo_offer(pending)
    return render(request, "crm/_cockpit.html", context)


@login_required
@require_POST
def proposals_undo(request: HttpRequest) -> HttpResponse:
    """Undo the dismissal the Undo strip is offering — those ids and no
    others.

    Scoped three ways, because the ids arrive from the client: `.for_user`
    (another tenant's id 404s into nothing, same as a missing one), the
    `dismissed` status filter inside `discovery.restore` (an accepted row is
    real work and undo must never eat it), and the parse below, which drops
    anything that isn't an integer rather than letting it reach the ORM.

    `restore` does the reconciling — a person added by another door since the
    dismissal comes back as already-in-your-network rather than as a duplicate
    card. See its docstring."""
    from capture import discovery
    from capture.models import ContactProposal

    ids = []
    for raw in (request.POST.get("ids") or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            ids.append(int(raw))
    if not ids:
        return HttpResponse(status=400)

    rows = list(
        ContactProposal.objects.for_user(request.user).filter(
            id__in=ids, status=ContactProposal.STATUS_DISMISSED
        )
    )
    restored = sum(
        1 for row in rows if discovery.restore(row)[0] == discovery.RESTORED
    )
    if rows:
        record_event(
            "contact_proposals_dismiss_undone", user=request.user, source="today",
            count=restored,
        )
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def proposal_restore(request: HttpRequest, pk: int) -> HttpResponse:
    """Restore one dismissed proposal from Settings > Dismissed from your
    inbox — the durable half of the same undo, for the mis-tap nobody caught
    in the moment.

    Flashes what actually happened rather than a blanket "restored": the row
    may reconcile onto a contact that already exists, or refuse because the
    match is archived, and the user is owed the real sentence in both cases
    (see `capture.discovery.restore`)."""
    from capture import discovery
    from capture.models import ContactProposal

    proposal = get_object_or_404(
        ContactProposal.objects.for_user(request.user), pk=pk,
        status=ContactProposal.STATUS_DISMISSED,
    )
    outcome, contact = discovery.restore(proposal)
    record_event(
        "contact_proposal_restored", user=request.user, source="settings",
        outcome=outcome,
    )
    if outcome == discovery.RESTORED:
        messages.success(
            request, f"{proposal.name} is back on Today, waiting for your tap."
        )
    elif outcome == discovery.ALREADY_A_CONTACT:
        messages.info(
            request,
            f"{proposal.name} is already in your network"
            + (f" as {contact.name}." if contact else "."),
        )
    elif outcome == discovery.RESTORE_ARCHIVED:
        messages.info(
            request,
            f"{proposal.name} matches an archived contact. Unarchive them from "
            "Archived Contacts if you want them back.",
        )
    return redirect(reverse("accounts:settings") + "#dismissed-proposals")


@login_required
@require_POST
def contact_merge_act(request: HttpRequest, verb: str) -> HttpResponse:
    """One tap on a duplicate-card suggestion in Settings: `merge` folds the
    duplicate into the primary through `crm.merge` (touches moved, blanks
    filled, alternate address noted, duplicate archived — all in the
    ledger), `reject` records "different people" forever. The suggestion is
    RE-DERIVED server-side (`crm.merge.suggestion_for`): a pair that no
    longer clears the suggestive bar, or was answered on another tab,
    refuses politely instead of merging on stale evidence."""
    from crm import merge as merge_service

    if verb not in ("merge", "reject"):
        return HttpResponse(status=400)
    try:
        primary_id = int(request.POST.get("primary", ""))
        duplicate_id = int(request.POST.get("duplicate", ""))
    except (TypeError, ValueError):
        return HttpResponse(status=400)
    cand = merge_service.suggestion_for(request.user, primary_id, duplicate_id)
    if cand is None:
        messages.info(
            request,
            "That suggestion is no longer standing. Nothing was changed.",
        )
        return redirect(reverse("accounts:settings") + "#duplicates")
    if verb == "merge":
        merge_service.merge(
            request.user, cand.primary, cand.duplicate, cand.evidence
        )
        record_event("contact_merge_merged", user=request.user, source="settings")
        messages.success(
            request,
            f"{cand.duplicate.name} folded into {cand.primary.name}. "
            "Their history is one card now. Undo below if this was wrong.",
        )
    else:
        merge_service.reject(
            request.user, cand.primary, cand.duplicate, cand.evidence
        )
        record_event("contact_merge_rejected", user=request.user, source="settings")
        messages.success(
            request,
            f"Kept {cand.primary.name} and {cand.duplicate.name} as two "
            "people. Coverage will not ask about this pair again.",
        )
    return redirect(reverse("accounts:settings") + "#duplicates")


@login_required
@require_POST
def contact_merge_undo(request: HttpRequest, pk: int) -> HttpResponse:
    """Reverse one merge from the Settings ledger: the recorded touches move
    back, filled fields revert where the merge's value still stands, the
    alternate-address note line comes off, and the duplicate returns to the
    archived state it actually had (see `crm.merge.undo`)."""
    from crm import merge as merge_service
    from crm.models import ContactMerge

    record = get_object_or_404(
        ContactMerge.objects.for_user(request.user), pk=pk,
        status=ContactMerge.STATUS_MERGED,
    )
    merge_service.undo(record)
    record_event("contact_merge_undone", user=request.user, source="settings")
    messages.success(
        request,
        f"{record.duplicate.name} is back as their own card, history restored.",
    )
    return redirect(reverse("accounts:settings") + "#duplicates")


@login_required
@require_POST
def autopilot_apply(request: HttpRequest, pk: int) -> HttpResponse:
    """THE one tap — apply everything Autopilot decided in one reviewed run.

    This is the whole compliance design in a single view (see
    capture/autopilot.py's module docstring): the AI decided unattended and
    wrote nothing; this POST is the user's own act over the disclosed,
    quoted batch, and `apply_run` executes it through the same doors a
    card tap uses. NOT one database transaction, and it must not be — the
    warmth ratchet opens its own psycopg connection (see `apply_run`'s
    docstring for the per-decision completeness + resume contract that
    stands in for one). Re-renders the cockpit like every other Today
    action — the accepted cards leave, the escalations stay, and the
    strip is gone because the run is no longer `reviewed`."""
    from capture import autopilot
    from capture.models import AutopilotRun

    run = get_object_or_404(
        AutopilotRun.objects.for_user(request.user), pk=pk,
        status=AutopilotRun.STATUS_REVIEWED,
    )
    # Through, not just this run: the strip's count is the SUM across every
    # reviewed run (crm.today), so the tap applies every reviewed run up to
    # the one it named — an older reviewed batch left behind by a second
    # decide pass used to stay invisible and unapplied forever. Runs newer
    # than the tapped one are untouched; the tap never applies verdicts the
    # strip didn't disclose.
    outcome, applied = autopilot.apply_reviewed_through(run)
    record_event(
        "autopilot_applied", user=request.user, run_id=run.pk,
        count=applied, outcome=outcome,
    )
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
@require_POST
def autopilot_start(request: HttpRequest) -> HttpResponse:
    """Start a decide pass — the control that made this loop self-serve.

    Until this existed the AI pass was a management command: a student
    could APPLY a finished run and undo it, but could not start one. This
    is the missing half, and everything it must not do is enforced in
    `capture.autopilot.start_run` rather than here:

      - it never blocks on the model — this writes a QUEUED row and
        returns; the worker decides (see that module's "HOW A RUN STARTS");
      - it never double-runs — `uniq_autopilot_active` refuses a second
        active row at the database, so two taps land as one run plus one
        "already running";
      - it never spends what the ledger cannot cover — `preview` clamps and
        refuses BEFORE the row is written, let alone a model call;
      - it never applies anything. Deciding writes verdicts; the separate
        "Add all N" tap is still the only thing that touches the CRM, which
        is the Limited Use posture and not a UX preference.

    Re-renders the cockpit like every other Today action, so the strip the
    student sees next is the state they are actually in.
    """
    from capture import autopilot

    outcome, run = autopilot.start_run(request.user, source_label="Today")
    record_event(
        "autopilot_started", user=request.user, outcome=outcome,
        run_id=run.pk if run else None,
    )
    if outcome == autopilot.ALREADY_RUNNING:
        messages.info(request, "Autopilot is already reading your cards.")
    elif outcome == autopilot.INSUFFICIENT_CREDITS:
        messages.error(
            request,
            "Not enough credits to read those cards. Nothing was started "
            "and nothing was charged.",
        )
    elif outcome == autopilot.UNCONFIGURED:
        messages.error(request, "Autopilot isn't switched on for this deploy.")
    elif outcome == autopilot.NOTHING_TO_DECIDE:
        messages.info(request, "Nothing for Autopilot to read right now.")
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
def autopilot_state(request: HttpRequest) -> HttpResponse:
    """The poll target while a run is queued or deciding.

    Returns the whole cockpit, not a fragment, because the cockpit is the
    htmx swap target for every other Today action and re-rendering it is
    how this page has always stayed in sync. The polling attribute lives
    on the running strip itself, so the poll stops the moment the strip it
    was attached to is replaced by a finished one — no timer to cancel, no
    state to track, and a run that FAILED replaces the spinner with the
    reason rather than spinning forever.
    """
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


@login_required
def autopilot_log(request: HttpRequest) -> HttpResponse:
    """The activity ledger: every run, every decision, and the quote each
    one stands on — the same check-the-rule's-work surface as Not Related
    to Recruitment and Not Your Recruiting, for the same reason. Hands-off
    is only safe while "what did it do, and why" is one page, and the way
    back (one Undo per applied decision) sits on the row it reverses."""
    from capture.models import AutopilotRun

    runs = list(
        AutopilotRun.objects.for_user(request.user)
        # A run still queued or deciding has nothing to show yet — its
        # decisions are being written as the page renders. The Today strip
        # is where an in-flight run is disclosed; this page is the record
        # of finished ones.
        .exclude(status__in=AutopilotRun.ACTIVE_STATUSES)
        .prefetch_related(
            "decisions__proposal", "decisions__app_event", "decisions__contact",
        )
        .order_by("-created")[:20]
    )
    return render(request, "crm/autopilot_log.html", {"runs": runs})


@login_required
@require_POST
def autopilot_undo(request: HttpRequest, pk: int) -> HttpResponse:
    """One click back, and the user's word made permanent: the touch goes,
    a contact Autopilot created goes with it, the card returns to Today —
    and no future run ever re-decides this person (`overridden`)."""
    from capture import autopilot
    from capture.models import AutopilotDecision

    decision = get_object_or_404(
        AutopilotDecision.objects.for_user(request.user), pk=pk,
    )
    who = (
        decision.proposal.name if decision.proposal
        else str(decision.app_event or "")
    )
    outcome = autopilot.undo_decision(decision)
    record_event(
        "autopilot_undone", user=request.user, decision_id=decision.pk,
        outcome=outcome,
    )
    if outcome == autopilot.UNDONE:
        messages.success(
            request,
            f"Undone. {who} is back on Today as a card, and Autopilot will "
            "never decide about them again.",
        )
    else:
        messages.info(request, "Nothing to undo on that row.")
    return redirect("crm:autopilot_log")


def app_event_act(request: HttpRequest, pk: int, verb: str) -> HttpResponse:
    """One tap on an application-status card. Accept writes the pipeline
    move through `capture.appmail.accept` — the ONLY path from a detected
    email to `UserOpportunity`, and it holds capture_applications' refusals
    (never backwards, never over the student's own Done). Dismiss is
    remembered forever. Re-renders the cockpit like every other quick
    action."""
    from capture import appmail
    from capture.models import ApplicationEvent

    if verb not in ("accept", "dismiss"):
        return HttpResponse(status=400)
    event = get_object_or_404(
        ApplicationEvent.objects.for_user(request.user), pk=pk,
        status=ApplicationEvent.STATUS_PENDING,
    )
    if verb == "accept":
        appmail.accept(event)
        record_event(
            "application_event_accepted", user=request.user, source="today",
            event_type=event.event_type, status=event.target_status,
        )
    else:
        appmail.dismiss(event)
        record_event(
            "application_event_dismissed", user=request.user, source="today",
            event_type=event.event_type,
        )
    return render(request, "crm/_cockpit.html", _cockpit_context(request.user))


# ---------------------------------------------------------------------------
# 2. Contact list + detail.
# ---------------------------------------------------------------------------
# Contact-card sections below the coverage board, in display order.
_WARMTH_SECTIONS = [
    ("replied", "Replied"),
    ("chatted", "Chatted"),
    ("advocate", "Advocates"),
    ("no_reply", "Emailed, No Reply"),
    # FOUND WHILE AUDITING THE BOARD'S COUNTS (2026-08-25), and it is the same
    # class of bug as the one that audit was for: `no_reply` is cold AND
    # touched, so a contact who is cold and has never been touched matched no
    # section at all and rendered nowhere — while still being counted in
    # "Contacts N" at the top. On the demo account that was 24 of 61 people
    # present in the header and absent from the page. They are the ones a
    # student most needs to see, too: a name they added and never wrote to.
    ("not_contacted", "Not Contacted Yet"),
]

# The Network board's region scope tabs, in display order. A subset of
# directory.classify.TRACKED_REGIONS ("cn"/"jp" have no scope tab yet — no
# board has ever asked for one) — kept as its own tuple, not a slice of
# TRACKED_REGIONS, so a future market added there doesn't silently grow a
# board tab nobody designed the layout for. "other" is the founder's third
# bucket (2026-08-25: "accurate sorting between united states and hongkong
# and other countries") — the tab for a person KNOWN to sit outside both
# markets, backed by Contact.region="other", never a dumping ground for
# unknowns (those stay guesses, flagged as such, in the tabs their firm
# suggests).
NETWORK_SCOPE_REGIONS = ("hk", "us", "sg", "eu", "other")

# The one scope that is not a region: everyone the write path declined to
# place. Kept OUT of NETWORK_SCOPE_REGIONS on purpose — every branch keyed on
# that tuple ("is this a region tab?") would answer wrong for it: the firm
# board would filter itself by a region code that isn't one, and the
# "shown on a guess" caveat would fire on a tab where every row is a known
# unknown and nothing is being guessed at.
UNPLACED_SCOPE = "unplaced"

# Firm-region codes that place a firm's footprint unambiguously OUTSIDE both
# deadline markets — the only firm evidence the "other" tab's unknown-contact
# fallback accepts. "apac" is deliberately absent: Jane Street carries
# ['us', 'apac'] and APAC contains Hong Kong, so it is evidence of nothing.
_OTHER_FIRM_REGIONS = frozenset({"sg", "eu", "cn", "jp", "other"})

# Warmest-first tie-break for "who to work next at this firm" — chatted
# beats replied beats cold, an advocate is never a candidate (there's
# nothing left to convert them into).
_LEVER_RANK = {"chatted": 0, "replied": 1, "cold": 2}


def _pick_lever(contacts):
    """The single best contact to work next at a firm: the warmest
    non-advocate, ties broken by id so the pick is stable across renders.

    Shared by the Coverage Gaps strip and every firm card on the board
    below it — before this, each computed its own copy, so a firm's "top
    pick" in the gaps strip could disagree with itself if the two were
    ever edited separately."""
    candidates = [c for c in contacts if c.warmth != "advocate"]
    candidates.sort(key=lambda c: (_LEVER_RANK.get(c.warmth, 3), c.id))
    return candidates[0] if candidates else None


def contact_region(c) -> str | None:
    """The region a Contact row belongs to, or None when it genuinely isn't
    known — the SAME answer `cadence.contact_region` gives the engine.

    Delegated rather than reimplemented on purpose. The cadence engine already
    decides this question (branch 3 scopes its pre-deadline re-ping by region),
    and if the Network page answered it differently the product would show a
    person under "Hong Kong" while re-pinging them against US deadlines. One
    function, one answer: the explicit `Contact.region` column, or None.

    `source` is still passed for shape compatibility with the engine's input
    dicts, but nothing reads it — the legacy provenance-text inference was
    retired from the read path (see `cadence.contact_region`). That retirement
    is what finally lets `_in_scope`'s firm fallback below actually run: it
    was written for a None that the old inference never returned.
    """
    return cadence.contact_region({"region": c.region, "source": c.source})


def _group_unplaced(contacts) -> list[dict]:
    """Unplaced contacts bundled by firm, biggest bundle first.

    A contact with no directory firm still groups — by their typed
    `firm_text`, and failing that under one "No firm" bundle. Dropping them
    would be the quiet kind of wrong: they are exactly the people a student
    added by hand, which is to say the ones most likely to be unplaced.
    """
    groups: dict[str, dict] = {}
    for c in contacts:
        label = (c.firm.name if c.firm_id and c.firm else c.firm_text) or "No firm"
        group = groups.setdefault(
            label, {"label": label, "key": f"g{len(groups)}", "contacts": []}
        )
        group["contacts"].append(c)
    out = list(groups.values())
    for g in out:
        g["contacts"].sort(key=lambda c: (c.name or "").lower())
        g["count"] = len(g["contacts"])
    out.sort(key=lambda g: (-g["count"], g["label"].lower()))
    return out


def _in_scope(c, scope: str) -> bool:
    """Does contact `c` belong in the `scope` region tab?

    Precedence, mirroring `cadence.contact_region` exactly so the two can never
    disagree about a person:

      1. Resolved region (the explicit `Contact.region` column) matches the
         scope -> in, and ONLY in, that one tab.
      2. Resolved region is None — the contact has no region set, i.e.
         genuinely unknown — fall back to the firm's regions, which can put
         the contact in more than one tab.

    Step 2 is deliberately the LAST resort rather than the first, which is the
    whole fix. A firm's `regions` describes the FIRM, not the person: most
    bulge brackets carry ['us', 'hk'], so filtering on it put one contact in
    both tabs and made the two lists near-duplicates. A person works in one
    place; a firm recruits in several.

    Showing a genuinely-unknown contact in every tab is the honest answer (we
    do not know, so we do not hide them from the tab they might belong to), but
    it is an admission of ignorance, not a regional match — so the caller marks
    these unconfirmed and the contact card renders no region pill for them. It
    never asserts a region nobody set.
    """
    # Singapore and Europe are tabs the FIRM directory supports but the contact
    # vocabulary does not: a person can never resolve to "sg" and asking their
    # region would empty those tabs for everyone. There the firm is the only
    # evidence that exists, so it stays the whole test — which is also exactly
    # how these tabs behaved before.
    if scope not in Contact.REGION_VALUES:
        return bool(c.firm and scope in (c.firm.regions or []))
    region = contact_region(c)
    if region is not None:
        return region == scope
    # Unknown-region fallbacks, per tab. For "other" the firm evidence is any
    # footprint code that sits unambiguously outside both deadline markets
    # (see _OTHER_FIRM_REGIONS) — a bulge bracket's ['us', 'hk'] says nothing
    # about a third country, so its unknowns stay out of this tab.
    if scope == "other":
        return bool(
            c.firm and _OTHER_FIRM_REGIONS & {
                (r or "").strip().lower() for r in (c.firm.regions or [])
            }
        )
    return bool(c.firm and scope in (c.firm.regions or []))


# The keep-warm clock each warmth class runs on, in days — the same windows
# the cadence engine acts on (weeks * 7 for the two check-in clocks; cold
# contacts run on the follow-up window). The staleness ring divides elapsed
# silence by this, so a full ring means "the engine is about to nag you
# about this person", not an arbitrary redness.
def _stale_window_days(c, params) -> int:
    from coverage_domain.cadence import CADENCE_DEFAULTS

    merged = {**CADENCE_DEFAULTS, **params}
    warmth = (c.warmth or "").lower()
    if warmth == "advocate":
        return merged["advocate_touch_min_weeks"] * 7
    if warmth == "chatted":
        return merged["chatted_touch_min_weeks"] * 7
    # Cold and replied both run on the follow-up window; business days are
    # roughly seven-fifths of calendar days.
    return max(round(merged["followup_after_business_days"] * 7 / 5), 1)


def _contact_card(c, *, tier, today, cadence=None, as_of=None):
    """One full contact card (radar style): initials, pills, firm · role,
    note bullets in plain grammar, and days since the last touch.

    `as_of` mirrors `crm.debrief.pending`'s optional parameter of the same
    name: `None` means "real current time" (every live caller), and tests
    pin it to get a deterministic calendar-date boundary without mocking
    the clock."""
    parts = [p for p in (c.name or "").split() if p]
    initials = "".join(p[0] for p in parts[:2]).upper()
    bullets = []
    for raw in (c.notes, c.angle):
        for frag in (raw or "").replace(";", "\n").splitlines():
            frag = frag.strip().strip("-• ").rstrip(".")
            if frag:
                bullets.append(frag[0].upper() + frag[1:])
    last = c.last_touch_ts
    days_since = _calendar_days_ago(last, as_of=as_of) if last else None
    window = _stale_window_days(c, cadence or {})
    # 0.0 = touched today, 1.0 = the engine's clock has run out. Never-touched
    # contacts show a full ring: silence since forever is the stalest state.
    stale = (min(days_since / window, 1.0) if days_since is not None else 1.0)
    return {
        "c": c,
        "initials": initials or "?",
        "gender": (c.gender or "")[:1].upper(),
        "tier": tier,
        "school": c.school,
        # Blank when unknown — the chip simply doesn't render, rather than the
        # card asserting a region nobody set.
        "region": (c.region or "").upper(),
        "bullets": bullets[:3],
        "days_since": days_since,
        "stale_pct": round(stale * 100),
        # The tint threshold decided here, where the arithmetic lives, not by
        # a CSS substring hack that cannot tell 8% from 80%.
        "stale_tint": ("due" if stale >= 1.0 else
                       "warming" if stale >= 0.7 else "fresh"),
        # Compose surface: same rule as every other mailto: on the site —
        # body from `opener` ONLY, never `angle` (that's the user's private
        # note ABOUT the person, not a draft addressed TO them).
        "mailto": _mailto(c.email or "", body=(c.opener or "")),
    }


@login_required
def contact_list(request: HttpRequest) -> HttpResponse:
    """The Network board (radar layout): scope tabs (US / Hong Kong /
    School), Firm Coverage grouped by the user's own tiers (draggable —
    tier drives the cadence engine's priorities), then full contact cards
    sectioned by warmth. Bulk selection lives on those contact cards
    (`contacts_bulk`, below) — the cadence-verb queue this board used to
    duplicate now renders only on Today (`crm/today.py`)."""
    from .today import _cadence_params

    # One read of the user's cadence overrides for every card's staleness
    # ring, rather than one per contact.
    cadence_overrides = _cadence_params(request.user)
    user = request.user
    today = timezone.localdate()
    actions, _ = _build_actions(user)

    contacts = list(
        Contact.objects.for_user(user)
        .filter(archived=False)
        .select_related("firm")
        .annotate(
            last_touch_ts=models_Max("touches__ts"),
            touch_count=models_Count("touches"),
        )
    )

    # OFF THE BOARD, not just off the daily queue. People whose relationship
    # with the user began in a bulk send he answered "not my recruiting" — see
    # `crm/campaigns.py` for the 201-thread ICC club merge that made the
    # question exist. The rule used to stop at the queue and leave all of them
    # sitting here, and the founder opened his own board and asked why there
    # were still club people in his network. Twelve members, nine of them
    # originating, on his live data. An answer that leaves them in Firm
    # Coverage, the warmth sections and the contact count is not an answer to
    # the question Settings asked him.
    #
    # Removed BEFORE the scope filter and before every count below, so the
    # tier board, the Coverage Gaps strip, the action lanes, `contact_total`
    # and the warmth sections all derive from this one list rather than each
    # re-deciding. `hidden` is KEPT rather than dropped: a board that quietly
    # shrinks by nine is the same bug wearing better manners, so it has to be
    # able to say how many it is not showing and hand him a way to look.
    #
    # THE INBOUND OVERRIDE IS NOT REPRODUCED HERE, deliberately. A campaign
    # contact who wrote in and is owed a reply still gets a card — on Today,
    # which is the surface that exists to say "do this today"
    # (`crm/relevance.py`). This page is the standing picture of who he knows
    # for recruiting, and half-presence (an action lane card for somebody with
    # no contact card below it) would answer neither question.
    hidden_ids = campaigns.excluded_contact_ids(user)
    hidden = [c for c in contacts if c.id in hidden_ids]
    contacts = [c for c in contacts if c.id not in hidden_ids]

    # ALSO OFF THE BOARD: people who are not related to the user's recruiting
    # at all — the founder's 2026-08-25 rule, decided per PERSON by
    # `crm/recruitment.py` (a professor, the campus advising office, an AWS
    # account manager; see that module for why neither firm tier nor the
    # school tie is the test, and for the deliberate reversal of the old
    # school-tie exemption). Removed at the same point as the campaign
    # gate above and for the same reason: every count below — the header
    # total, the tier board, Coverage Gaps, the action lanes, the warmth
    # sections — derives from this one list, so nothing can disagree with
    # what renders. And exactly like the campaign gate, the hidden are KEPT,
    # counted, and listed at `crm:contact_unrelated` with a one-click way
    # back — a board that quietly shrinks is the bug class this repo has now
    # fixed three times.
    unrelated_tiers = {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(user)
        if uf.firm_id
    }
    unrelated_tracks = {
        c.firm_id: (c.firm.tracks or []) for c in contacts if c.firm_id and c.firm
    }
    unrelated_verdicts = {
        c.id: recruitment.contact_verdict(
            c, tiers=unrelated_tiers, firm_tracks=unrelated_tracks,
            firm_label=(c.firm.name if c.firm else ""),
        )
        for c in contacts
    }
    unrelated = [
        c for c in contacts
        if unrelated_verdicts[c.id].verdict == recruitment.HIDE
    ]
    contacts = [
        c for c in contacts
        if unrelated_verdicts[c.id].verdict == recruitment.KEEP
    ]

    scope = request.GET.get("scope", "").strip().lower()

    def _scoped(rows):
        if scope == "school":
            return [c for c in rows if c.school or c.school_affiliation]
        if scope == UNPLACED_SCOPE:
            return [c for c in rows if not c.region]
        if scope in NETWORK_SCOPE_REGIONS:
            return [c for c in rows if _in_scope(c, scope)]
        return rows

    # THE ASK, and the whole of it. Nothing in this product ever infers a
    # region it cannot entail (`Contact.resolve_region`), which means some
    # contacts stay unplaced — by design, not by failure — and the only way a
    # person says "London" is to say it. This is where they say it: a tab that
    # exists, grouped so twelve contacts are three taps. Counted BEFORE the
    # scope filter, because it is the size of the pool, not of the current
    # view.
    #
    # It is never an interruption. No badge, no Today card, no digest line, no
    # escalation — the caveat line under the Contacts header becoming a link
    # to this tab is the entire nag budget. Ignored forever, nothing breaks:
    # blank contacts keep the cadence engine's both-regions fallback and go on
    # appearing in their firm's tabs marked unconfirmed. That is the designed
    # steady state.
    unplaced_total = sum(1 for c in contacts if not c.region)
    contacts = _scoped(contacts)
    # The caveat's number goes through the SAME filter as the board it is a
    # caveat about. A US tab reading "9 hidden" while eight of them are in
    # Hong Kong is the same class of lie as hiding them silently.
    hidden_total = len(_scoped(hidden))
    # The tab, unlike the caveat, is NOT scoped: it is the way back to the
    # list, and a way back that vanishes on the School tab because none of the
    # hidden nine went to USC is a dead end rather than a filter.
    hidden_any = bool(hidden)
    # Same pair for the recruitment-relevance gate: a scoped count for the
    # caveat, an unscoped bool for the way back.
    unrelated_total = len(_scoped(unrelated))
    unrelated_any = bool(unrelated)
    # How many of the shown contacts are here on a guess rather than a set
    # region. Rendered as a one-line caveat under the Contacts header so a
    # region tab never silently passes off "unknown" as "confirmed".
    unconfirmed_total = (
        sum(1 for c in contacts if not c.region)
        if scope in NETWORK_SCOPE_REGIONS
        else 0
    )
    # Grouped by firm, biggest group first, and only on the tab that asks.
    # "12 unplaced: 6 Morgan Stanley, 4 Citi, 2 Jefferies" is three taps
    # rather than twelve, and it is also the shape in which the question is
    # answerable at all — people at one firm's Hong Kong desk came from one
    # thread, and a student remembers them as a group.
    unplaced_groups = (
        _group_unplaced(contacts) if scope == UNPLACED_SCOPE else []
    )
    scoped_ids = {c.id for c in contacts}

    # Region tabs, narrowed to the student's own Settings > Profile
    # "Regions of Interest" — a student recruiting only HK/US doesn't need
    # Singapore and Europe tabs sitting in the nav forever. `user.regions`
    # empty means the question has never been ANSWERED, not "interested in
    # nothing" — silence still shows every tab, same as an unset filter
    # elsewhere on this site never hides data, it just can't narrow it yet.
    interested_regions = set(user.regions or [])
    # "other" is exempt from the interest gate: it is not a market a student
    # declares in Settings (same split directory.classify.REGION_ORDER makes —
    # a place a person can BE, never a place you choose to target), and its
    # label says countries because people sit in countries; the directory's
    # "Other Markets" wording belongs to postings.
    region_scopes = [
        {"code": code,
         "label": "Other countries" if code == "other" else REGION_LABELS[code]}
        for code in NETWORK_SCOPE_REGIONS
        if code == "other"
        or not interested_regions or code in interested_regions
    ]

    # The "Contacts Needing Action" panel that used to render here (verb
    # lanes: first note / follow up / thank-you / others) is gone — it and
    # Today's own cockpit rendered the SAME `_build_actions` output under
    # different labels, one queue on two pages. The queue lives on Today
    # now; this board's job is who-do-I-know-and-where-are-the-gaps, not
    # what-do-I-do-now. See crm/today.py::_cockpit_context for the surface
    # that still owns it.
    #
    # `_build_actions` itself is STILL called above (`actions, _ =
    # _build_actions(user)`) — this scope filter is the one thing that
    # survives from the removed panel. `act_by_firm` below still needs a
    # per-firm count of open actions to break ties in the tier sort
    # (`cards.sort(key=lambda fc: (-fc["act_now"], ...))`); it does not
    # render, so it never has to agree with a lane that no longer exists.
    actions = [a for a in actions if a["contact"]["id"] in scoped_ids]

    # --- Firm Coverage, grouped by the user's tiers ---------------------
    user_firms = list(
        UserFirm.objects.for_user(user).select_related("firm")
    )
    firm_ids = [uf.firm_id for uf in user_firms]
    campus = Opportunity.objects.filter(
        status="open", bucket__in=TARGET_BUCKETS, firm_id__in=firm_ids
    )
    open_by_firm = dict(
        campus.values_list("firm_id").annotate(n=models_Count("id")).values_list("firm_id", "n")
    )
    # The soonest CONFIRMED close per firm. Computed here rather than further
    # down because the firm cards need it too now — the Coverage Gaps strip
    # below is no longer its only reader. Only CONFIRMED official close dates
    # count toward urgency, the same bar `cadence._closing_soon` holds:
    # anything rumored or merely reported must not raise an alarm.
    closes: dict[int, Any] = {}
    for fd in FirmDate.objects.filter(
        firm_id__in=firm_ids, event_kind="app_close", date__gte=today
    ):
        if _confidence_label(fd.confidence) != cadence.CONFIRMED:
            continue
        if fd.firm_id not in closes or fd.date < closes[fd.firm_id]:
            closes[fd.firm_id] = fd.date
    act_by_firm: dict[int, int] = {}
    for a in actions:
        fid = a["contact"].get("firm_id")
        if fid:
            act_by_firm[fid] = act_by_firm.get(fid, 0) + 1

    by_firm_contacts: dict[int, list] = {}
    for c in contacts:
        if c.firm_id:
            by_firm_contacts.setdefault(c.firm_id, []).append(c)

    # The advocates-per-firm yardstick every card and every tier-cost line
    # measures against (User.assets["advocate_target"], default 2).
    adv_target = coverage.advocate_target(user)

    def firm_card(uf):
        cs = by_firm_contacts.get(uf.firm_id, [])
        total = len(cs) or 1
        segments = [
            {"warmth": w, "pct": round(sum(1 for c in cs if c.warmth == w) * 100 / total)}
            for w in ("cold", "replied", "chatted", "advocate")
        ]
        advocates = sum(1 for c in cs if c.warmth == "advocate")
        adv_met = advocates >= adv_target
        close = closes.get(uf.firm_id)
        days_out = (close - today).days if close else None
        return {
            "firm": uf.firm,
            "tier": uf.tier,
            # `open` and `act_now` no longer render — they sort. The card used
            # to wear both as count badges ("48 Open", "1 Act Now") and the
            # ask was to take them off it: a status board should read as
            # progress and a next step, not as a scoreboard. Neither number is
            # LOST by coming off the card. Open roles are the Opportunities
            # feed's whole job and the Coverage Gaps strip above still names
            # the count for its worst four; every contact behind `act_now` is
            # listed BY NAME in the action lanes to the left of these cards,
            # on this same page. What the numbers still do is decide which
            # firm a student's eye lands on first, in the sort below.
            "open": open_by_firm.get(uf.firm_id, 0),
            "act_now": act_by_firm.get(uf.firm_id, 0),
            # NOT a count, and it stayed on the card when the counts came
            # off: this answers a question about the STUDENT — is this firm
            # open to me at all — rather than about the week, and no other
            # surface on this board answers it.
            #
            # CORRECT AS-IS, and deliberately not reconciled here. Two firms
            # carry a legacy blanket `true` in this column where the schema
            # documents a per-region dict, and `directory.sponsorship
            # .effective_sponsorship` now resolves that same blanket shape to
            # "unknown" (60b7998). So the two surfaces disagree for exactly
            # those two firms. Which reading is factually right is not
            # decidable from a bare `true` — it is a data question, and the
            # founder's call.
            "sponsors": uf.firm.sponsors is True or uf.firm.sponsors == "true",
            # The one thing that had no other home. A count of roles closing
            # "soon" was inventory with a date attached; a CONFIRMED close
            # date is a deadline, and a deadline is the one signal a progress
            # bar structurally cannot carry — the bar says how far along you
            # are, never how long you have. Same 30-day window the old badge
            # used, and the same red mono countdown the Coverage Gaps strip
            # above already draws, so the board keeps one vocabulary for a
            # deadline rather than inventing a second.
            #
            # Just "6d" here where the strip says "6d to close": this card is
            # 190px and shares its top line with the firm's name, and at the
            # strip's full wording the name is what gave way ("Goldman S…").
            # The sentence lives in `close_title` for the hover.
            "close_label": (
                None if days_out is None or days_out > 30
                else "today" if days_out == 0
                else f"{days_out}d"
            ),
            "close_title": (
                None if days_out is None or days_out > 30
                else "Applications close today (confirmed date)."
                if days_out == 0
                else f"Applications close in {days_out} day"
                     f"{'' if days_out == 1 else 's'} (confirmed date)."
            ),
            "contact_count": len(cs),
            "segments": segments,
            # "1/2 advocates" against the user's target. `met` is the whole
            # point of showing a fraction rather than a count: a firm that
            # has hit the target should read as finished, not as 2 more
            # things you haven't done.
            "advocates": advocates,
            "adv_target": adv_target,
            "adv_met": adv_met,
            # Socket booleans for the template: [True, False] is "one of two
            # filled". Capped at the target — a third advocate at a
            # two-target firm overfills nothing, it just keeps the ✓.
            "adv_slots": [i < advocates for i in range(adv_target)],
            # What the Coverage Gaps strip already computes for its worst 6
            # firms, extended to all 69: WHERE this firm sits on the warmth
            # ladder, and the one contact who moves it forward. A card used
            # to state status only ("2 Act Now", a bar, a fraction) with no
            # path from reading it to doing something about it — the click
            # target existed for six firms and nowhere else on the board.
            "gap_label": (None if adv_met else
                         coverage.GAP_LABELS[coverage.gap_state(
                             (c.warmth for c in cs), advocates, adv_target)]),
            "lever": None if adv_met else _pick_lever(cs),
        }

    tier_sections = []
    for tier, label in ((1, "Tier 1"), (2, "Tier 2"), (3, "Tier 3"), (None, "Unranked")):
        cards = [firm_card(uf) for uf in user_firms if uf.tier == tier]
        if scope in NETWORK_SCOPE_REGIONS:
            # CORRECT AS-IS — deliberately still `firm.regions`, and NOT the
            # per-contact rule above. A firm genuinely does span regions:
            # Goldman really recruits in both Hong Kong and the US, so it
            # belongs on both boards. Only a PERSON has one location, which is
            # why the contact filter had to stop asking the firm.
            #
            # The card's numbers are already region-correct without touching
            # this line: `by_firm_contacts` is built from the scoped `contacts`
            # list above, so under the HK tab Goldman's warmth bars, contact
            # count and advocate fraction now count only its HK people.
            cards = [fc for fc in cards if scope in (fc["firm"].regions or [])]
        if cards or tier in (1, 2, 3):
            cards.sort(key=lambda fc: (-fc["act_now"], -fc["open"], fc["firm"].name))
            tier_sections.append({
                "tier": tier,
                "label": label,
                "cards": cards,
                # What this tier is committing the user to, in advocates.
                # Only for real tiers: "Unranked" is not a commitment.
                "cost": coverage.tier_cost(cards, adv_target) if tier else None,
            })

    # --- Coverage Gaps strip (top of the page) ---------------------------
    # `closes` is computed above, with the firm-card inputs — the strip and
    # the cards read the same dict and the same CONFIRMED-only bar.
    gaps = coverage.rank_gaps(
        [
            {
                "firm_id": uf.firm_id,
                "name": uf.firm.name,
                "tier": uf.tier,
                "warmths": [c.warmth for c in by_firm_contacts.get(uf.firm_id, [])],
                "app_close": closes.get(uf.firm_id),
                # Goes IN to the ranking now rather than being attached to
                # the result afterwards, because it decides ORDER now rather
                # than being printed on the card. See `rank_gaps` — it breaks
                # ties and touches nothing else.
                "open": open_by_firm.get(uf.firm_id, 0),
            }
            for uf in user_firms
        ],
        today=today,
        target=adv_target,
    )
    # One click to act on each gap: somewhere to start when the firm is
    # empty, and the warmest person who isn't an advocate yet when it
    # isn't — that contact is the shortest path to closing the gap.
    # `_pick_lever` — same function every firm card below uses.
    firms_by_id = {uf.firm_id: uf.firm for uf in user_firms}
    for g in gaps:
        firm = firms_by_id.get(g["firm_id"])
        g["slug"] = firm.slug if firm else ""
        g["lever"] = _pick_lever(by_firm_contacts.get(g["firm_id"], []))
        # `g["open"]` is set by `rank_gaps` from the input above — it is a
        # ranking input now, not a display field bolted on afterwards. The
        # card no longer prints it; it shows up as the card's POSITION among
        # equally-exposed firms, and as one clause in the card's title=.
        # "Who to find" — the other half of the answer. `lever` covers the
        # firms where the student already knows someone; this covers the
        # ones where the only verb on the card is "Add" and the card can't
        # say WHO. Pure, in-memory, no query: `crm.sourcing` reads the
        # firm's name and two fields off the user, and hands back three
        # role archetypes with a prefilled LinkedIn search each. Nothing is
        # fetched and nothing is imported (see that module's docstring) —
        # these are suggestions and links out.
        g["sourcing"] = sourcing.suggestions_for(firm, user) if firm else []

    # --- Full contact cards ---------------------------------------------
    # Warmth sections, same four as every other scope — School used to
    # group by university instead, but with almost every School-scope
    # contact sharing the student's own school (that's the point of the
    # scope), the school-name header was doing no work: one giant "SCHOOL"
    # bucket with no way to scan it by priority. Warmth is what every other
    # tab already uses to answer "who do I work next", and School gains
    # nothing by being the one scope that answers a different question.
    tiers_by_firm = {uf.firm_id: uf.tier for uf in user_firms}
    sections = []
    for key, label in _WARMTH_SECTIONS:
        if key == "no_reply":
            members = [c for c in contacts if c.warmth == "cold" and c.touch_count]
        elif key == "not_contacted":
            # The other half of cold. Together these two partition it, which is
            # what makes the sections sum to `contact_total` — see
            # `_WARMTH_SECTIONS`.
            members = [
                c for c in contacts if c.warmth == "cold" and not c.touch_count
            ]
        else:
            members = [c for c in contacts if c.warmth == key]
        sections.append({
            "key": key,
            "label": label,
            "cards": [
                _contact_card(c, tier=tiers_by_firm.get(c.firm_id), today=today,
                              cadence=cadence_overrides)
                for c in members
            ],
        })

    return render(
        request,
        "crm/contact_list.html",
        {
            "scope": scope,
            "region_scopes": region_scopes,
            "unplaced_scope": UNPLACED_SCOPE,
            # Unscoped, like `hidden_any`: the tab is the way TO the pool, and
            # a way that vanishes depending on which tab you're standing on is
            # a dead end rather than a filter. Rendered only when somebody is
            # actually unplaced — a permanent tab reading "0" is a standing
            # reproach for a state that is allowed.
            "unplaced_total": unplaced_total,
            "unplaced_groups": unplaced_groups,
            "region_verbs": REGION_BULK_VERBS,
            "gaps": gaps,
            # Said once per panel, in the module that builds the links, so
            # the promise and the code can't drift apart.
            "sourcing_note": sourcing.DISCLOSURE,
            "adv_target": adv_target,
            "tier_sections": tier_sections,
            "firm_total": len(user_firms),
            "sections": sections,
            "contact_total": len(contacts),
            "unconfirmed_total": unconfirmed_total,
            "hidden_total": hidden_total,
            "hidden_any": hidden_any,
            "unrelated_total": unrelated_total,
            "unrelated_any": unrelated_any,
        },
    )


@login_required
@require_POST
def set_firm_tier(request: HttpRequest) -> HttpResponse:
    """Drag-and-drop target: move one of the user's firms to a new tier.
    Tier drives the cadence engine's prioritization (firm_meta in
    `_build_actions`), so a drag literally reorders tomorrow's queue.

    Also the write side of Settings' "Target Firms" add flow: a firm the
    user isn't tracking yet has no `UserFirm` row for `.filter().update()`
    to find, so posting a firm they've never seen before used to 404
    silently. `get_or_create` makes the same one endpoint serve both
    "move an existing firm" (drag, or the Settings tier buttons) and "start
    tracking this firm at this tier" (Settings' add-a-firm search) — one
    code path instead of two nearly-identical ones that could drift.
    """
    try:
        firm_id = int(request.POST.get("firm", ""))
    except ValueError:
        return HttpResponse(status=400)
    tier_raw = request.POST.get("tier", "")
    tier = int(tier_raw) if tier_raw in ("1", "2", "3") else None
    if not Firm.objects.filter(id=firm_id).exists():
        return HttpResponse(status=404)
    UserFirm.all_objects.update_or_create(
        user=request.user, firm_id=firm_id,
        defaults={"tier": tier},
        create_defaults={"tier": tier, "status": "target"},
    )
    record_event("firm_tier_set", user=request.user)
    return HttpResponse(status=204)


@login_required
@require_POST
def remove_target_firm(request: HttpRequest) -> HttpResponse:
    """Stop tracking a firm entirely — Settings' per-firm remove control.
    Deletes the `UserFirm` row outright rather than clearing its tier:
    tier=None already means something else (drag a card off the board with
    no tier assigned), and conflating the two would make "untiered" and
    "removed" indistinguishable on screen. Contacts already logged at this
    firm are untouched — this is a target-list edit, not a data deletion."""
    try:
        firm_id = int(request.POST.get("firm", ""))
    except ValueError:
        return HttpResponse(status=400)
    deleted, _ = UserFirm.objects.for_user(request.user).filter(firm_id=firm_id).delete()
    if not deleted:
        return HttpResponse(status=404)
    record_event("firm_target_removed", user=request.user)
    return HttpResponse(status=204)


# What the "Who to find" panel is allowed to say happened. Two moments,
# both of them the student's own click: opening the panel at all, and
# leaving for one of its searches.
_SOURCING_EVENTS = {
    "panel": "sourcing_panel_opened",
    "search": "sourcing_search_opened",
}


@login_required
@require_POST
def sourcing_event(request: HttpRequest) -> HttpResponse:
    """Log that a student opened a Coverage Gap card's "Who to find" panel,
    or clicked one of its LinkedIn searches.

    Fire-and-forget, exactly like the assistant's thumbs up/down
    (`assistant.views.feedback`): one append-only `record_event` row, no
    model of its own, no migration, and nothing on the page reads it back.
    A double-click logs two events, which is exactly as fine as two clicks
    meaning two data points to whoever reads the funnel.

    It exists because "contact sourcing is zero-assist" is a launch-gate
    question with no evidence behind it either way. Whether students open
    this panel, and whether they then leave for a search, is the only
    signal that distinguishes "the suggestions help" from "the suggestions
    are decoration" — and the click that leaves for LinkedIn is otherwise
    invisible to us by design (we hand over a query and see nothing after).

    The firm slug arrives in a POST body, so it gets the same `.for_user`
    treatment every other id in this app gets: a firm the student is not
    tracking 404s instead of writing a row about somebody else's board.
    """
    kind = (request.POST.get("kind") or "").strip()
    event = _SOURCING_EVENTS.get(kind)
    if not event:
        return HttpResponse(status=400)
    slug = (request.POST.get("firm") or "").strip()
    if not UserFirm.objects.for_user(request.user).filter(firm__slug=slug).exists():
        return HttpResponse(status=404)
    props: dict[str, Any] = {"firm": slug}
    if kind == "search":
        # Which of the three rows, e.g. "ib-0" or "alumni" — the whole
        # point of logging the click. Bounded so a hand-rolled POST can't
        # write an essay into the props blob.
        props["archetype"] = (request.POST.get("archetype") or "")[:64]
    record_event(event, user=request.user, **props)
    return JsonResponse({"ok": True})


@login_required
def contact_new(request: HttpRequest) -> HttpResponse:
    """Hand-add a contact — the coffee-chat path the CRM was missing. A
    ?firm=<slug> query pre-selects a firm (used by the firm page's button)."""
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.user = request.user
            contact.source = "manual"
            contact.save()
            record_event("contact_added", user=request.user, source="manual")
            messages.success(request, f"Added {contact.name}.")
            return redirect("crm:contact_detail", pk=contact.pk)
    else:
        initial = {}
        firm_slug = request.GET.get("firm")
        if firm_slug:
            initial["firm"] = Firm.objects.filter(slug=firm_slug).first()
        form = ContactForm(initial=initial)
    # ?quick renders the three-field fast path: the real moment this page
    # serves is "met someone, have ten seconds", and the full form is 11
    # fields of friction against a name you are about to forget. Same form
    # class, same POST, same validation — only the fields SHOWN change, and
    # everything hidden stays editable later on the contact page.
    quick = "quick" in request.GET or request.POST.get("quick") == "1"
    return render(request, "crm/contact_form.html",
                  {"form": form, "mode": "new", "quick": quick})


@login_required
def contact_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit an existing contact. Scoped through `.for_user`, so another
    tenant's id 404s indistinguishably from a missing one."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact)
        if form.is_valid():
            form.save()
            messages.success(request, "Contact updated.")
            return redirect("crm:contact_detail", pk=contact.pk)
    else:
        form = ContactForm(instance=contact)
    return render(request, "crm/contact_form.html",
                  {"form": form, "mode": "edit", "contact": contact})


# ---------------------------------------------------------------------------
# 2b. Archive / unarchive — the contact lifecycle's exit AND its way back.
# ---------------------------------------------------------------------------
# `Contact.archived` has existed since the first migration and every query in
# the app filters on it, but nothing could ever SET it from the UI and no page
# ever listed the rows it hid. That made it a one-way trapdoor operated only by
# automated paths: 25 of the founder's 137 contacts sat archived and invisible,
# and because both capture resolvers filter `archived=False`, a later genuine
# reply from one of them FORKED a new contact rather than resurrecting the old
# one — the history split in two and neither half was complete.
#
# The three views below are the missing half. Archiving is now something a
# person does on purpose and can undo; correspondingly, no automated path
# archives at all any more (see capture/gmail.py's bounce block).
def _set_archived(request: HttpRequest, pk: int, *, archived: bool) -> Contact:
    """Flip `archived` on one of the user's contacts. A plain ORM write on
    purpose: `archived` is a UI/lifecycle flag, not part of the
    warmth/thread_state ratchet that must go through `crm.services`. It
    changes nothing about the relationship's history — every touch stays on
    the row and comes back with it."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if contact.archived != archived:
        contact.archived = archived
        contact.save(update_fields=["archived"])
    return contact


# The bulk verbs the Network board offers over a multi-selection, and the
# past-tense word each reports back. All three are REVERSIBLE, and that is
# the selection criterion, not an accident of what was easy:
#
#   snooze  -> `snoozed_until`, expires by itself in 3 days.
#   park    -> `thread_state="parked"`; the contact leaves the cadence queue
#              but keeps every touch, and logging one more puts them back.
#   archive -> off the board and out of coverage counts, restored in one
#              click from Archived Contacts.
#
# There is deliberately no `delete`. The product has no hard-delete path for
# a contact ANYWHERE — `contact_archived.html` says so in as many words
# ("Nothing is deleted: every touch stays on the record and comes back with
# the person") — and a multi-select is the worst possible place to
# introduce the first one, because the same mis-click that snoozes three
# people would erase eighty-three and their entire correspondence history.
# Archive is what "get rid of these" means here, and it means it safely.
_BULK_VERBS = {
    "snooze": "snoozed for 3 days",
    "park": "taken out of the follow-up queue",
    "archive": "archived",
    # The ask. Three verbs, one per value `Contact.region` can hold, applied
    # to a hand-picked set exactly like every verb above.
    #
    # `region_other` is in the list even though NOTHING infers "other" and
    # nothing ever will — a firm's non-us/hk footprint describes the firm, and
    # no stated fact entails that a person sits in London. Which is precisely
    # why the verb has to exist: it is the only way a human says so.
    "region_us": "filed as United States",
    "region_hk": "filed as Hong Kong",
    "region_other": "filed as Other countries",
}

# verb -> the `Contact.region` value it writes. Membership in this dict is
# also what marks a verb as a region verb in `contacts_bulk` below.
REGION_BULK_VERBS = {
    "region_us": "us",
    "region_hk": "hk",
    "region_other": "other",
}


@login_required
@require_POST
def contacts_bulk(request: HttpRequest) -> HttpResponse:
    """Apply one reversible verb to a hand-picked set of contacts.

    Unlike `today_park_all`, which re-derives its ids from the engine, this
    one MUST trust the posted ids — the whole feature is the user choosing
    an arbitrary subset. The safety property is tenancy, not derivation:
    every id is resolved through `Contact.objects.for_user`, so another
    tenant's id is silently absent from the queryset rather than acted on,
    and a stale id from a re-rendered page simply matches nothing.
    """
    verb = (request.POST.get("verb") or "").strip()
    if verb not in _BULK_VERBS:
        return HttpResponse(status=400)

    ids = []
    for raw in request.POST.getlist("ids"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    scope = (request.POST.get("scope") or "").strip()
    back = reverse("crm:contact_list") + (f"?scope={quote(scope)}" if scope else "")
    if not ids:
        messages.info(request, "Nothing was selected.")
        return redirect(back)

    rows = list(
        Contact.objects.for_user(request.user)
        .filter(pk__in=ids, archived=False)
        .values_list("id", "name")
    )
    if not rows:
        messages.info(request, "Nothing was selected.")
        return redirect(back)

    if verb in REGION_BULK_VERBS:
        # `region_source="user"` because a person just said so, and that is
        # the one provenance nothing else is ever allowed to overwrite —
        # not the firm rule, not the declaration rule, not a later Settings
        # change. A plain `.update()` for the same reason it is safe here:
        # tier 1 of `resolve_region` would return this value unchanged
        # anyway, so there is nothing for `save()` to add.
        Contact.objects.for_user(request.user).filter(
            pk__in=[cid for cid, _ in rows]
        ).update(
            region=REGION_BULK_VERBS[verb],
            region_source=Contact.REGION_SOURCE_USER,
        )
    elif verb == "snooze":
        Contact.objects.for_user(request.user).filter(
            pk__in=[cid for cid, _ in rows]
        ).update(snoozed_until=timezone.now() + timedelta(days=3))
    elif verb == "park":
        # A LOOP over the audited override, not a bulk `.update()` — the
        # same call and the same reasoning as `today_park_all`: parking
        # moves `thread_state`, and only the override path is allowed to,
        # writing one `manual_override` touch per contact so the log never
        # has a gap about who left the queue or when. Slower, and correct.
        for cid, _ in rows:
            services.set_contact_state(
                request.user.id, cid,
                thread_state="parked",
                note="Parked from the Network board (bulk)",
            )
    else:  # archive
        # A plain ORM write, matching `_set_archived`: `archived` is a
        # UI/lifecycle flag, not part of the warmth/thread_state ratchet, so
        # it does not go through the pipeline.
        Contact.objects.for_user(request.user).filter(
            pk__in=[cid for cid, _ in rows]
        ).update(archived=True)

    record_event(
        f"contacts_bulk_{verb}", user=request.user,
        source="network", count=len(rows),
    )
    # Name them when it's a handful, count them when it isn't — a list of
    # eighty-three names is not a confirmation, it's a wall.
    who = (", ".join(name for _, name in rows[:3])
           + (f" and {len(rows) - 3} more" if len(rows) > 3 else ""))
    undo = ("They're in Archived Contacts if you want them back."
            if verb == "archive" else
            "Logging a touch puts anyone back in the queue."
            if verb == "park" else
            "Change it on any contact if you picked wrong."
            if verb in REGION_BULK_VERBS else "")
    messages.success(
        request,
        f"{len(rows)} contact{'' if len(rows) == 1 else 's'} "
        f"{_BULK_VERBS[verb]}: {who}. {undo}".strip(),
    )
    return redirect(back)


@login_required
@require_POST
def contact_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Archive a contact: off the Network board, out of the cadence queue,
    out of coverage counts — but not deleted, and one click from coming
    back."""
    contact = _set_archived(request, pk, archived=True)
    record_event("contact_archived", user=request.user)
    messages.success(
        request,
        f"Archived {contact.name}. They're in Archived Contacts if you want "
        "them back.",
    )
    return redirect("crm:contact_list")


@login_required
@require_POST
def contact_unarchive(request: HttpRequest, pk: int) -> HttpResponse:
    """Bring a contact back, with their whole touch history intact."""
    contact = _set_archived(request, pk, archived=False)
    record_event("contact_unarchived", user=request.user)
    messages.success(request, f"{contact.name} is back on your board.")
    return redirect("crm:contact_detail", pk=contact.pk)


@login_required
def contact_archived(request: HttpRequest) -> HttpResponse:
    """The archived list — the view that makes archiving reversible in
    practice rather than only in principle. Deliberately plain: this is a
    recovery surface, not a second Network board."""
    contacts = list(
        Contact.objects.for_user(request.user)
        .filter(archived=True)
        .select_related("firm")
        .annotate(last_touch_ts=models_Max("touches__ts"))
        .order_by("name")
    )
    return render(
        request,
        "crm/contact_archived.html",
        {"contacts": contacts, "contact_total": len(contacts)},
    )


@login_required
def contact_campaign_hidden(request: HttpRequest) -> HttpResponse:
    """The people the Network board is hiding because the user said the send
    they arrived on was not their recruiting.

    The counterpart to `contact_archived`, and deliberately the same plain
    ledger rather than a second Network board: somewhere to check the board's
    arithmetic and put one person back. Nothing here is hidden anywhere else —
    every one of them keeps their detail page, their whole touch history,
    search, and every export. This list is what makes the board's caveat line
    a claim he can audit instead of one he has to take on faith.

    `archived=False` for the same reason the board filters it: somebody who is
    both archived and campaign-hidden is already accounted for under Archived,
    and listing them in both places would make the two counts disagree about
    one person.
    """
    from .models import CampaignContact

    hidden_ids = campaigns.excluded_contact_ids(request.user)
    contacts = (
        list(
            Contact.objects.for_user(request.user)
            .filter(id__in=hidden_ids, archived=False)
            .select_related("firm")
            .annotate(last_touch_ts=models_Max("touches__ts"))
            .order_by("name")
        )
        if hidden_ids
        else []
    )
    # WHICH send each person arrived on. One `.for_user`-scoped query, and the
    # list is close to useless without it: "these nine are hidden" is a fact
    # about the software, "these nine arrived on Fall 2026 ICC Alumni Digital
    # Panel Outreach" is the fact he can check against his own memory.
    sends: dict[int, str] = {}
    if hidden_ids:
        for cc in (
            CampaignContact.objects.for_user(request.user)
            .filter(
                contact_id__in=hidden_ids,
                originates=True,
                campaign__kind=Campaign.KIND_OTHER,
            )
            .select_related("campaign")
        ):
            sends.setdefault(cc.contact_id, cc.campaign.label)
    for c in contacts:
        c.campaign_label = sends.get(c.id, "")
    return render(
        request,
        "crm/contact_campaign_hidden.html",
        {"contacts": contacts, "contact_total": len(contacts)},
    )


@login_required
@require_POST
def contact_campaign_keep(request: HttpRequest, pk: int) -> HttpResponse:
    """Put one person from a "not my recruiting" send back on the board.

    Writes the same `Contact.campaign_exempt` column the edit form's "Always
    keep in my daily queue" tick writes — one column, one meaning, so the
    hidden list and the form cannot drift into two rescue hatches that
    disagree. Detection never writes it (`crm/campaigns.py`), so the answer
    survives every re-run.

    It is what makes hiding reversible in practice rather than only in
    principle, the same job `contact_unarchive` does for Archived: the founder
    mail-merged alumni across every industry and one of them is genuinely a
    banker he wants to recruit through, and the alternative rescue is
    reclassifying the whole send and letting the other two hundred back in.
    """
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if not contact.campaign_exempt:
        contact.campaign_exempt = True
        contact.save(update_fields=["campaign_exempt"])
        record_event("campaign_contact_kept", user=request.user)
    messages.success(
        request,
        f"{contact.name} is back on your board and in your daily queue.",
    )
    return redirect("crm:contact_campaign_hidden")


@login_required
def contact_unrelated(request: HttpRequest) -> HttpResponse:
    """The people the board is hiding because nothing about THEM relates to
    the user's recruiting — the recruitment-relevance twin of
    `contact_campaign_hidden`, and deliberately the same plain ledger:
    somewhere to check the board's arithmetic and put one person back.

    The middle column carries the RULE'S OWN CITED REASON for each person
    ("Campus role, not recruiting: 'Professor (USC Dornsife, WRIT 150)'"),
    because "these eight are hidden" is a fact about the software and the
    quoted role text is the fact the user can check against the row. Nothing
    here is hidden anywhere else: detail page, touch history, search and
    every export all keep working.

    `archived=False` and campaign-hidden people excluded, same
    one-list-per-person rule as the other ledgers: somebody who is both is
    already accounted for on the list that hid them first.
    """
    tiers = {
        uf.firm_id: uf.tier
        for uf in UserFirm.objects.for_user(request.user)
        if uf.firm_id
    }
    campaign_ids = campaigns.excluded_contact_ids(request.user)
    rows = [
        c
        for c in Contact.objects.for_user(request.user)
        .filter(archived=False)
        .select_related("firm")
        .annotate(last_touch_ts=models_Max("touches__ts"))
        .order_by("name")
        if c.id not in campaign_ids
    ]
    firm_tracks = {
        c.firm_id: (c.firm.tracks or []) for c in rows if c.firm_id and c.firm
    }
    contacts = []
    for c in rows:
        v = recruitment.contact_verdict(
            c, tiers=tiers, firm_tracks=firm_tracks,
            firm_label=(c.firm.name if c.firm else ""),
        )
        if v.verdict == recruitment.HIDE:
            c.hide_reason = v.reason
            contacts.append(c)
    return render(
        request,
        "crm/contact_unrelated.html",
        {"contacts": contacts, "contact_total": len(contacts)},
    )


@login_required
@require_POST
def contact_unrelated_keep(request: HttpRequest, pk: int) -> HttpResponse:
    """Put one person the recruitment rule hid back on the board.

    Writes `Contact.recruitment_related = True` — the user's own word, which
    `crm.recruitment.contact_verdict` puts above every rung of its ladder, so
    this survives every future re-run of the rule permanently. The mirror of
    `contact_campaign_keep`, for the same reason it exists: a rule over a
    hundred-odd people will occasionally be wrong about one, and the rescue
    for that one must not be loosening the rule for everybody.
    """
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if contact.recruitment_related is not True:
        contact.recruitment_related = True
        contact.save(update_fields=["recruitment_related"])
        record_event("unrelated_contact_kept", user=request.user)
    messages.success(
        request,
        f"{contact.name} is back on your board and in your daily queue.",
    )
    return redirect("crm:contact_unrelated")


@login_required
def contact_detail(request: HttpRequest, pk: int) -> HttpResponse:
    # for_user() 404s cleanly for another tenant's id (indistinguishable from
    # a non-existent id — the tenancy guarantee, §2). Not filtered on
    # `archived`: an archived contact's page must stay reachable, or the
    # Archived list would have nowhere to link and unarchiving would have no
    # home.
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    context = _contact_live_context(request, contact)
    # §6: the fit score is computed on the fly and shown here — record the view.
    record_event("score_viewed", user=request.user)
    return render(request, "crm/contact_detail.html", context)


# ---------------------------------------------------------------------------
# 3. Log-a-touch (htmx) — the capture-rate hook (§5): visible warmth movement.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def log_touch(request: HttpRequest, pk: int) -> HttpResponse:
    """POST kind+channel, ratchet the state via the reviewed pipeline adapter,
    then re-render the live panel so the user SEES warmth move in the same
    session. Returns the `#contact-live` fragment for an htmx outerHTML swap."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)

    kind = (request.POST.get("kind") or "").strip()
    channel = (request.POST.get("channel") or "").strip()
    note = (request.POST.get("note") or "").strip() or None

    error = None
    updates: dict[str, str] = {}
    before_warmth, before_state = contact.warmth, contact.thread_state

    if kind not in TOUCH_TRANSITIONS:
        error = "Pick an interaction type."
    elif channel not in CHANNELS:
        error = "Pick a channel."
    else:
        updates = services.log_touch(request.user.id, contact.id, kind, channel, note)
        record_event("touch_logged", user=request.user, source="manual")
        contact.refresh_from_db()

    moved = {
        "logged": error is None,
        "error": error,
        "kind": kind,
        "kind_label": dict(TOUCH_KIND_LABELS).get(kind, kind),
        "changed": bool(updates),
        "from_warmth": before_warmth,
        "from_state": before_state,
        # Words for the flag: the enums ("no_reply → replied") had leaked
        # into the one sentence whose whole job is celebrating the change.
        "from_state_label": _STATE_LINES.get(before_state, before_state),
        "to_state_label": _STATE_LINES.get(contact.thread_state, contact.thread_state),
    }
    context = _contact_live_context(request, contact, moved=moved)
    return render(request, "crm/_contact_live.html", context)


# ---------------------------------------------------------------------------
# Shared context for the live panel (used by the detail page and the htmx
# fragment, so both render identically).
# ---------------------------------------------------------------------------
# Thread state, said in words. The page used to print the raw enum — a chip
# reading "no_reply" — right next to a warmth chip and a fit-score band that
# both said "cold", three spellings of one fact. One human sentence carries
# the pair; the enums stay in the database where they belong.
_STATE_LINES = {
    "no_reply": "No reply yet",
    "replied": "They replied",
    "chat_scheduled": "A chat is set up",
    "chat_done": "You have chatted",
    "advocate": "In your corner",
    "quiet": "Gone quiet",
    "parked": "Parked",
}

# (warmth, thread_state) pairs where the state sentence already implies the
# warmth — "Replied · They replied" says one thing twice, and the whole point
# of the sentence was replacing three chips that did exactly that.
_STATE_IMPLIES_WARMTH = {
    ("replied", "replied"),
    ("chatted", "chat_done"),
    ("advocate", "advocate"),
}


# Machine bookkeeping prefixes on touch notes. `[gmail:<thread>]` is the
# sync's idempotency marker (how a re-scanned thread knows it is already
# logged), `[capture:<event>]` is the BCC pipeline's provenance pointer, and
# `[assistant:<message>]` points a touch the advisor page logged back at the
# exact model turn that logged it (assistant/tools.py). All three are
# load-bearing IN THE DATABASE and meaningless ON THE PAGE — the owner's
# words: "the user doesn't learn anything". Stripped at display, never at
# rest; the export still carries them raw.
_NOTE_MARKER = re.compile(r"^\[(?:gmail|capture|assistant):[^\]]*\]\s*")

# `services.set_contact_state`'s audit trail (pipeline.py's `set_state`)
# writes every manual-override touch's note as "manual override:
# <column>=<value>, ..." optionally followed by " — <human note>". The
# column=value pairs are the same kind of machine bookkeeping as the bracket
# markers above — real for the audit trail, meaningless to a user reading
# their OWN history: "thread_state=chat_done" names a database column, not a
# fact about them, and it duplicated the row's own kind_label besides.
# Confirmed live: James Bai's contact page (id=312) rendered
# "thread_state=chat_done — Correction: ..." verbatim as a History
# entry. Stripped at display, same posture as `_NOTE_MARKER` — only the
# human-authored explanation after the dash (if any) is shown.
_MANUAL_OVERRIDE_PREFIX = re.compile(
    r"^manual override:[^—]*(?:—\s*)?", re.IGNORECASE)

# Same note, parsed rather than stripped — see `_override_label` below.
_MANUAL_OVERRIDE_PARSE = re.compile(
    r"^manual override:\s*(?P<fields>[^—]*)(?:—\s*(?P<human>.*))?$",
    re.IGNORECASE | re.DOTALL,
)
_ASSISTANT_NOTE = re.compile(r"^\[assistant:[^\]]*\]")


def _override_label(note: str | None) -> str:
    """Plain-language History label for a `manual_override` touch, replacing
    the raw kind name ("Manual override") which is engineering language for
    the audit MECHANISM, not a description of what happened. It read like
    "your profile didn't save" to a student, when it always means either
    "you parked this contact" or "your advisor parked this contact" (or,
    off the Park path, some other direct correction) — confirmed live.

    Reuses the same `set_state` note format `_MANUAL_OVERRIDE_PREFIX` strips:
    "manual override: <col>=<val>, ... [— <human note>]". WHO is read off
    the human note's own `[assistant:...]` marker (assistant/tools.py logs
    every override it makes with one, same posture as `_NOTE_MARKER` above)
    — present means the advisor acted for the student, absent means the
    student did it themselves (Park, or a direct correction). WHAT is read
    off the changed column: `thread_state=parked` names the one everyday
    case in plain words; anything else falls back to the honest generic.
    """
    m = _MANUAL_OVERRIDE_PARSE.match(note or "")
    if not m:
        return "Updated manually"
    fields = m.group("fields") or ""
    human = (m.group("human") or "").lstrip()
    who = "Your advisor" if _ASSISTANT_NOTE.match(human) else "You"
    verb = "parked this contact" if "thread_state=parked" in fields else "updated this contact"
    return f"{who} {verb}"


def _display_note(note: str | None) -> str:
    """Both markers stripped, OUTERMOST FIRST — and that order is the whole
    subtlety. `_NOTE_MARKER` is anchored to the start of the string, so a
    marker written INSIDE a manual-override note ("manual override:
    thread_state=parked — [assistant:msg_9] their words") only reaches the
    start once the override prefix in front of it is gone. Stripping in the
    other order left the bracket marker sitting on the student's own
    contact page, which is exactly what both of these regexes exist to
    prevent. A note carrying only one of the two is unaffected either way."""
    note = _MANUAL_OVERRIDE_PREFIX.sub("", note or "").strip()
    return _NOTE_MARKER.sub("", note).strip()


def _status_line(contact: Contact) -> str:
    state = _STATE_LINES.get(
        contact.thread_state, contact.thread_state.replace("_", " ").capitalize()
    )
    if (contact.warmth, contact.thread_state) in _STATE_IMPLIES_WARMTH:
        return state
    return f"{contact.warmth.capitalize()} · {state}"


def _contact_live_context(
    request: HttpRequest, contact: Contact, *, moved: dict | None = None
) -> dict[str, Any]:
    user = request.user
    now = timezone.now()

    touches = list(
        Touch.objects.for_user(user).filter(contact=contact).order_by("-ts")
    )
    kind_labels = dict(TOUCH_KIND_LABELS)
    channel_labels = dict(CHANNEL_LABELS)
    # Display rows for the history: the raw model rows leak enum spellings
    # ("reply_received · email") into the page. Labels here, once, instead of
    # a template filter per cell. manual_override gets its own plain-language
    # label (see `_override_label`) instead of the dict fallback, because
    # the generic "kind name, sentence-cased" reads as engineering jargon
    # for this one kind specifically — every other kind is already a plain
    # verb in TOUCH_KIND_LABELS.
    touch_rows = [
        {
            "ts": t.ts,
            "kind": t.kind,
            "kind_label": (
                _override_label(t.note) if t.kind == MANUAL_OVERRIDE_KIND
                else kind_labels.get(t.kind, t.kind.replace("_", " ").capitalize())
            ),
            "channel_label": channel_labels.get(t.channel, t.channel),
            "note": _display_note(t.note),
            "inbound": t.kind in _INBOUND_TOUCH_KINDS,
        }
        for t in touches
    ]
    touch_dicts = _touch_dicts(touches)

    contact_score = scoring.score_contact(
        {
            "id": contact.id,
            "role": contact.role,
            "school_affiliation": contact.school_affiliation,
        },
        touch_dicts,
        as_of=now,
    )

    # Optional firm-fit view (§6): only when the contact belongs to a
    # directory firm. Reuses the user's other contacts at that firm so the
    # network axis shares one definition of warmth.
    firm_score = None
    firm = contact.firm
    if firm is not None:
        firm_contacts = list(
            Contact.objects.for_user(user).filter(firm=firm, archived=False)
        )
        fc_ids = [c.id for c in firm_contacts]
        firm_touches = _touch_dicts(
            Touch.objects.for_user(user).filter(contact_id__in=fc_ids)
        )
        firm_dates = [
            {
                "event_kind": fd.event_kind,
                "region": fd.region,
                "date": fd.date,
                "confidence": _confidence_label(fd.confidence),
            }
            for fd in FirmDate.objects.filter(firm=firm)
        ]
        # The Network axis measures against `advocate_target` full-strength
        # advocates as its 100-point yardstick (scoring._score_network). Left
        # at `params=None` this silently falls back to
        # `scoring.DEFAULT_PARAMS["advocate_target"] = 2` — but
        # `coverage.advocate_target(user)` reads the user's own tunable
        # `User.assets["advocate_target"]`, which is what every OTHER
        # coverage number on this page (firm cards, "N/target advocates")
        # is measured against. Without building the params bundle here, a
        # firm would read as "covered" on the contact-detail fit score and
        # not-covered on the firm-coverage list the instant a user changed
        # their target — same firm, two different answers. `version` is
        # tagged with the target so a changed setting is a visible,
        # rehashable event rather than the same `inputs_hash` silently
        # meaning two different things.
        adv_target = coverage.advocate_target(user)
        firm_score = scoring.score_firm(
            {
                "id": user.id,
                "regions": user.regions,
                "tracks": user.tracks,
                # Derived per firm-region from the user's own work
                # authorization, so the structural axis actually moves. This
                # used to be a hardcoded None — "unknown" for every user
                # forever, which neutralized the sponsorship component of the
                # score permanently. `needs_sponsorship` still returns None
                # when the user has no entry for the regions in play; unknown
                # is a real answer, it just isn't the only one now.
                "needs_sponsorship": scoring.needs_sponsorship(
                    user.work_authorization, user.regions, firm.regions
                ),
            },
            {
                "id": firm.id,
                "regions": firm.regions,
                "tracks": firm.tracks,
                "sponsors": firm.sponsors,
            },
            [
                {
                    "id": c.id,
                    "role": c.role,
                    "school_affiliation": c.school_affiliation,
                }
                for c in firm_contacts
            ],
            firm_touches,
            firm_dates,
            as_of=now,
            params={
                **scoring.DEFAULT_PARAMS,
                "advocate_target": adv_target,
                "version": f"scoring-v1+at{adv_target}",
            },
        )
        # The Timeline axis's `next_event` is a DB enum (app_open / app_close
        # / insight_deadline — scoring.py's `event_kind`), and _contact_live
        # printed it unfiltered: "app_close in 77d, 2 warm". The same card
        # already says the same event in English two lines above it, because
        # the scorer's reasoning string runs it through a verb map ("closes in
        # ~3 months"), so one panel contradicted itself on a single screen.
        # Labelled here rather than in coverage_domain: the enum is what the
        # scorer matches on, and FIRM_DATE_LABELS is the crm surface's own
        # lowercase vocabulary — the same map the Today strip and the calendar
        # already read, so the three cannot drift.
        nxt = (firm_score.get("axes", {}).get("timeline") or {}).get("next_event")
        if nxt:
            firm_score["axes"]["timeline"]["next_event_label"] = (
                _FIRM_DATE_LABELS.get(nxt, nxt.replace("_", " "))
            )

    # Warmth-meter animation endpoints. On a plain GET both are the current
    # level (no visible motion); on a POST that ratcheted, `from` is the old
    # level so the fill animates the jump.
    to_pct = _warmth_pct(contact.warmth)
    from_pct = _warmth_pct(moved["from_warmth"]) if moved else to_pct

    return {
        "contact": contact,
        "touches": touches,
        "touch_rows": touch_rows,
        "state_line": _status_line(contact),
        "contact_score": contact_score,
        "firm_score": firm_score,
        "firm": firm,
        "warmth_from_pct": from_pct,
        "warmth_to_pct": to_pct,
        "warmth_order": WARMTH_ORDER,
        "moved": moved,
        "touch_kinds": TOUCH_KIND_LABELS,
        # Which interaction the log form opens on. Today's `confirm_chat`
        # card links here with ?log=chat instead of one-click-logging a chat
        # itself: "the chat happened" is too large a claim for a card whose
        # whole reason to exist is that we don't know whether it did. Landing
        # on a pre-filled form is a two-step, and the second step is a human
        # confirming it. Ignored unless it names a real touch kind.
        "preselect_kind": (
            request.GET.get("log") if request.GET.get("log") in TOUCH_TRANSITIONS else None
        ),
        "channels": CHANNEL_LABELS,
        # How far behind the stored AI summary has fallen. Counted off the
        # `touches` list already loaded above, so this costs no extra query —
        # and it is only ever a DISPLAY fact: nothing here generates, because
        # generation is a deliberate POST (crm.views.contact_ai_summary).
        "summary_new_touches": ai_summary.touches_since_summary(contact, touches),
        "mailto": _mailto(contact.email, body=(contact.opener or "")),
    }


@login_required
@require_POST
def contact_opener(request: HttpRequest, pk: int) -> HttpResponse:
    """Save the contact's opener — the draft their Compose button carries.

    This closes a loop that shipped half-built: Compose has always prefilled
    the email body from `contact.opener`, and the Today card wears a "draft
    ready" badge when one exists — but nothing in the product ever WROTE the
    field, so zero of 129 real contacts had one and the badge could never
    appear. One textarea, saved here, and both ends start working.
    """
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    contact.opener = (request.POST.get("opener") or "").strip()
    contact.save(update_fields=["opener"])
    record_event("opener_saved", user=request.user)
    return render(request, "crm/_contact_live.html",
                  _contact_live_context(request, contact))


@login_required
@require_POST
def contact_ai_brief(request: HttpRequest, pk: int) -> HttpResponse:
    """Draft a coffee-chat prep brief for `contact` from their own history
    (see crm/ai_brief.py). POST, not GET: this is a paid API call once
    ANTHROPIC_API_KEY is set, so it must never fire from a prefetch, a
    browser "Reload," or a crawler following a link — only a deliberate
    click. Renders inline via htmx; the panel states plainly this is
    AI-drafted and unconfigured/failed cases show a plain unavailable
    message rather than an error.

    CREDIT METERING (docs/founder-decisions-2026-08-20.md §2b), same shape
    as `assistant/agent.py::run_turn`'s chat-turn metering: `can_spend`
    checked once, before the model call, a hard stop rather than a mid-call
    one; the debit fires only after `generate_coffee_chat_brief` actually
    returns a brief, never on an unconfigured/failed call (that call is
    free — nothing metered ran) and never twice for the same click. At
    zero credits the panel renders the same honest notice the chat uses
    (`ai_brief.credit_block_notice`), not a 500 and not a silent brief."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    if not billing_credits.can_spend(request.user, ai_brief.BRIEF_COST):
        return render(request, "crm/_contact_ai_brief.html", {
            "contact": contact,
            "brief": None,
            "requested": True,
            "blocked": True,
            "credit_notice": ai_brief.credit_block_notice(request.user),
        })
    brief = ai_brief.generate_coffee_chat_brief(contact)
    if brief is not None:
        record_event("ai_brief_generated", user=request.user)
        billing_credits.spend(
            request.user, ai_brief.BRIEF_COST, CreditLedger.KIND_SPEND_BRIEF,
            contact_id=contact.pk,
        )
    return render(request, "crm/_contact_ai_brief.html",
                  {"contact": contact, "brief": brief, "requested": True, "blocked": False})


@login_required
@require_POST
def contact_ai_summary(request: HttpRequest, pk: int) -> HttpResponse:
    """(Re)write the AI relationship summary on `contact` — see
    crm/ai_summary.py, which owns both the prompt and the rule that the
    student's own `notes`/`angle` are read as context and never written.

    POST, not GET, and for exactly the reason `contact_ai_brief` above gives:
    this is a paid API call once ANTHROPIC_API_KEY is set, so it fires only
    on a deliberate click, never on a prefetch, a reload, or a crawl of the
    contact list. That is also why the summary is NOT generated lazily when
    the detail page renders — the page shows the stored note and counts how
    far behind it has fallen instead.

    Renders the same fragment the page renders inline. A generation that
    produced nothing (thin history, key unset, API down) leaves the previous
    summary in place and says so plainly rather than erroring."""
    contact = get_object_or_404(Contact.objects.for_user(request.user), pk=pk)
    summary = ai_summary.regenerate(contact)
    if summary is not None:
        record_event("ai_summary_generated", user=request.user)
    return render(request, "crm/_contact_ai_summary.html", {
        "contact": contact,
        "requested": True,
        "generated": summary is not None,
        "summary_new_touches": ai_summary.touches_since_summary(contact),
    })


# ---------------------------------------------------------------------------
# Campaigns — the one question the user answers about a bulk send.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def classify_campaign(request: HttpRequest) -> HttpResponse:
    """Settings' Campaigns card: record whether one detected bulk send was the
    user's own recruiting or something else they do. See `crm/campaigns.py`
    for what a campaign is and the 201-thread club mail merge that made this
    necessary.

    A full-page POST-redirect-GET rather than an htmx swap, and that is the
    point rather than a shortcut: answering this question changes who is in
    tomorrow's queue, and the honest feedback for it is the page coming back
    with the new answer on it and a message saying how many people moved. A
    silent in-place toggle would be the smallest possible acknowledgement of
    the largest change any control on this page makes.

    Re-answering is allowed and expected. `crm.campaigns.classify` is scoped
    with `.for_user`, so a campaign id belonging to somebody else is simply not
    found — the same 404-shaped no-op as every other private-zone write here.
    """
    try:
        campaign_id = int(request.POST.get("campaign", ""))
    except ValueError:
        return HttpResponse(status=400)
    kind = request.POST.get("kind", "")
    campaign = campaigns.classify(request.user, campaign_id, kind)
    if campaign is None:
        return HttpResponse(status=404)
    record_event("campaign_classified", user=request.user, kind=kind)
    name = campaign.label or "That send"
    if kind == Campaign.KIND_OTHER:
        # Counted AFTER the write, off the same function the queue itself
        # calls, so the number on screen is the number that will be applied
        # rather than a second opinion about it — intersected with THIS
        # campaign's own originating members, because the sentence names this
        # send. It used to state the total across every campaign classified
        # `other`: with the 9-recipient ICC merge already answered, answering
        # an 8-recipient send said "17 contacts affected".
        from .models import CampaignContact

        members = set(
            CampaignContact.objects.for_user(request.user)
            .filter(campaign=campaign, originates=True)
            .values_list("contact_id", flat=True)
        )
        moved = len(members & campaigns.excluded_contact_ids(request.user))
        messages.success(
            request,
            f"Got it. {name} is off your daily queue and your network board. "
            f"{moved} contact{'' if moved == 1 else 's'} hidden, all still in "
            "your contacts and your history.",
        )
    elif kind == Campaign.KIND_RECRUITING:
        messages.success(request, f"Kept. {name} stays in your daily queue.")
    else:
        messages.success(request, f"Cleared. We will ask about {name} again.")
    return redirect(reverse("accounts:settings") + "#campaigns")
