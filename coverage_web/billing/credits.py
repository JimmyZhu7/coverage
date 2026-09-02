"""billing.credits — the one API both metered AI surfaces spend through
(docs/credit-system-plan.md §4-§7). Nothing outside this module should read
or write `CreditLedger` rows directly — see that model's docstring.

Mirrors `analytics.record_event`'s posture: this module IS the trusted
writer, so it reaches for `CreditLedger.all_objects` rather than the
tenant-scoped `.objects` (which refuses an unscoped query per
`coverage_web.tenancy`) — a visible, greppable admission that these
functions are the one deliberately cross-tenant path, always called with an
explicit `user` argument.

WHY THIS DOESN'T IMPORT `assistant` OR `capture`: both metered surfaces
import THIS module, never the other way — the ledger has no reason to know
what a "chat message" or a "residue thread" is beyond the `kind` string it's
handed. Plan resolution (which plan a user is on; unknown/blank -> free) is
kept as a small independent copy here rather than imported from
`assistant.plans`, on purpose: this app has to keep working if the assistant
app is ever disabled, and it is capture's dependency just as much as
assistant's.

CALLED FROM OUTSIDE A REQUEST, AND FROM INSIDE ONE MID-STREAM.
`assistant/agent.py` calls this mid-request, where
`accounts.middleware.TimezoneMiddleware` has already activated the
student's own timezone for the WHOLE request. `capture/gmail_live.py::
run_rescan` calls this from the `gmail_backfill` management command — a
bare cron tick with no request and no activated timezone at all. Every
"what day/month is it for this user" computation below resolves and
activates the user's own zone with `django.utils.timezone.override`
(a context manager, not a bare `activate()`/`deactivate()` pair like
`accounts.middleware.activate_for_user` uses) specifically so it restores
whatever was active before it ran, rather than unconditionally clearing it
— a bare `deactivate()` here would silently blow away the per-REQUEST
activation for everything that runs after this call in the same request
(a calendar-event tool call later in the same turn, for one), not just
reset to "nothing was ever activated" the way it does in a bare cron
process.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import CreditLedger

FREE = "free"
PRO = "pro"

#: Ordering for "does this plan outrank that one" — `reconcile_plan_grant`'s
#: own upgrade-vs-downgrade test. A tuple of names, not derived from
#: `_DEFAULTS`'s dict order, so the ranking is explicit and doesn't
#: silently depend on iteration order matching intent if a third tier is
#: ever added.
_PLAN_RANK = {FREE: 0, PRO: 1}

# Fallbacks if settings are somehow missing a tier — mirrors
# `assistant.plans._DEFAULTS`'s own posture so a bare test environment
# behaves the same as a configured one.
_DEFAULTS = {
    FREE: {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    PRO: {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
}
_DEFAULT_RESCAN_THREADS_PER_CREDIT = 10
# One autopilot verdict costs about what one residue classification does
# (same model, a similar few-hundred-token prompt — capture/autopilot.py),
# so the same 10-rows-per-credit rate is the honest default, overridable
# the same way.
_DEFAULT_AUTOPILOT_ROWS_PER_CREDIT = 10


def _plan_of(user) -> str:
    plan = (getattr(user, "plan", "") or "").strip().lower()
    return plan if plan in _DEFAULTS else FREE


def _plan_config(plan: str) -> dict:
    """Same shape `plan_config(user)` returns, but for an explicit plan key
    rather than one resolved from a user — what `reconcile_plan_grant`
    needs to compute "what would plan X's monthly grant have been",
    independent of which plan the user is on right now.

    Reads with `is not None`, not `or` — a plan config that explicitly sets
    a number to `0` (a Free tier with no daily burst allowance, say) must
    stay `0`, not silently fall back to the default because `0` is falsy.
    """
    configured = (getattr(settings, "CREDIT_PLANS", None) or {}).get(plan) or {}
    defaults = _DEFAULTS[plan]

    def _int_or_default(key: str) -> int:
        value = configured.get(key)
        return int(value) if value is not None else defaults[key]

    return {
        "plan": plan,
        "monthly_grant": _int_or_default("monthly_grant"),
        "message_cost": _int_or_default("message_cost"),
        "daily_burst": _int_or_default("daily_burst"),
    }


def plan_config(user) -> dict:
    """`monthly_grant` / `message_cost` / `daily_burst` for this user's
    plan, `settings.CREDIT_PLANS` overriding the defaults above — exactly
    the way `assistant.plans.limits_for` reads `ASSISTANT_PLANS`."""
    return _plan_config(_plan_of(user))


def rescan_threads_per_credit() -> int:
    value = getattr(settings, "CREDIT_RESCAN_THREADS_PER_CREDIT", None)
    return int(value) if value is not None else _DEFAULT_RESCAN_THREADS_PER_CREDIT


# ---------------------------------------------------------------------------
# "What day/month is it for this user" — see module docstring.
# ---------------------------------------------------------------------------
def _resolved_zone(user):
    """The `ZoneInfo` `user.timezone` names, or `None` (meaning "the project
    default") on anything unset or invalid. Same resolution rule as
    `accounts.middleware.activate_for_user`, kept as a small independent
    copy here because that function activates immediately rather than
    handing back a zone to use with `timezone.override` — see this
    module's docstring for why `override` and not a bare `activate()` is
    the one that's safe to call mid-request."""
    name = ""
    if user is not None and getattr(user, "is_authenticated", False):
        name = (getattr(user, "timezone", "") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None


def _user_localdate(user) -> date:
    with timezone.override(_resolved_zone(user)):
        return timezone.localdate()


def _period_for(user) -> str:
    return _user_localdate(user).strftime("%Y-%m")


def _day_window(user):
    today = _user_localdate(user)
    start = timezone.make_aware(datetime.combine(today, time.min))
    return start, start + timedelta(days=1)


def next_refill_date(user) -> date:
    """The 1st of next month, in this user's own timezone — what the
    zero-credit notice tells a student to expect (§6)."""
    today = _user_localdate(user)
    if today.month == 12:
        return today.replace(year=today.year + 1, month=1, day=1)
    return today.replace(month=today.month + 1, day=1)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _raw_balance(user) -> int:
    """Sum of every ledger row, unfloored — can run slightly negative from a
    Pro overdraw (see `spend`'s docstring on the zero-credit edge). Internal
    only: callers outside this module want `balance()`, which floors at
    zero for display."""
    total = CreditLedger.objects.for_user(user).aggregate(s=Sum("delta"))["s"]
    return total or 0


#: The `kind`s that count as "spend" for the burst guard and the
#: Settings usage line. Deliberately excludes `KIND_ADJUST`: a negative
#: admin adjustment (correcting an earlier mistaken grant, say) is not the
#: kind of activity the burst guard exists to catch, and letting it eat a
#: student's daily allowance would be a founder-support action silently
#: locking the student out of the chat they didn't overuse.
_SPEND_KINDS = (
    CreditLedger.KIND_SPEND_CHAT,
    CreditLedger.KIND_SPEND_RESCAN,
    CreditLedger.KIND_SPEND_BRIEF,
    CreditLedger.KIND_SPEND_AUTOPILOT,
)

#: `_SPEND_KINDS` plus `KIND_REFUND`, for the two usage counters below.
#: `refund()` exists for exactly one shape of event: a spend that already
#: landed in `_SPEND_KINDS` turned out to correspond to a turn the student
#: never got an answer to. Netting the refund's positive row against its
#: matching spend row in the SAME query is what makes `daily_spent` and
#: `month_usage` treat that turn as though it never happened — matching
#: `refund`'s own fairness rule (see its docstring) instead of only
#: half-honoring it by restoring the balance while still burning the
#: day's allowance and misreporting the month's usage. `KIND_ADJUST` stays
#: excluded: an admin adjustment has no matching spend row to net against.
_NET_SPEND_KINDS = _SPEND_KINDS + (CreditLedger.KIND_REFUND,)


def daily_spent(user) -> int:
    """Credits spent (a positive count), net of same-day refunds, since
    local midnight today — the burst guard's own counter (§4). Grants and
    admin adjustments never count against it; `spend()` rows do, and a
    `refund()` of one nets back out — see `_NET_SPEND_KINDS`."""
    start, end = _day_window(user)
    total = (
        CreditLedger.objects.for_user(user)
        .filter(kind__in=_NET_SPEND_KINDS, created__gte=start, created__lt=end)
        .aggregate(s=Sum("delta"))["s"]
    )
    return -(total or 0)


def month_usage(user) -> int:
    """Credits spent, net of refunds, since this user's local first-of-month
    — distinct from `daily_spent`: this resets on the calendar, that resets
    nightly. Powers the Settings page's "used N so far this month" line,
    which must reconcile with the balance shown right next to it — a
    refunded turn changes neither."""
    today = _user_localdate(user)
    start = timezone.make_aware(datetime.combine(today.replace(day=1), time.min))
    total = (
        CreditLedger.objects.for_user(user)
        .filter(kind__in=_NET_SPEND_KINDS, created__gte=start)
        .aggregate(s=Sum("delta"))["s"]
    )
    return -(total or 0)


def ensure_monthly_grant(user) -> None:
    """Write this month's grant row if it doesn't exist yet — lazy, no cron
    (§4/§7): the first balance check in a new month is what creates it.

    Idempotent under concurrency two ways at once: `select_for_update` on
    the user row serializes concurrent callers for the SAME user (cheap —
    it's a single-row lock, held only for the duration of this check), and
    the model's `UniqueConstraint` makes anything that still races (a
    second DB connection, a retry) a harmless `IntegrityError` this
    function swallows. Either guard alone would be enough; both together is
    what the plan calls for and what a double-submit on the streaming chat
    path (a request held open long enough for a real race) actually needs.

    Rollover: `delta = min(monthly_grant, 2*monthly_grant - current_balance)`,
    clamped to >= 0, so a dormant balance never compounds past 2x one
    month's grant (§7).
    """
    period = _period_for(user)
    if CreditLedger.objects.for_user(user).filter(
        kind=CreditLedger.KIND_GRANT, period=period
    ).exists():
        return

    # `get_user_model()`, not `type(user)`: `user` is usually
    # `request.user`, which Django wraps in a `SimpleLazyObject` — its
    # `type()` is the wrapper, not the real model, and has no `.objects`.
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        # Re-check inside the lock: another request may have written the
        # grant between the check above and acquiring it.
        if CreditLedger.objects.for_user(user).filter(
            kind=CreditLedger.KIND_GRANT, period=period
        ).exists():
            return
        plan = plan_config(user)
        current = _raw_balance(user)
        delta = max(0, min(plan["monthly_grant"], 2 * plan["monthly_grant"] - current))
        try:
            # A nested atomic (a savepoint) so a swallowed IntegrityError
            # doesn't poison the outer transaction the row-lock is held in.
            with transaction.atomic():
                CreditLedger.all_objects.create(
                    user=user, delta=delta, kind=CreditLedger.KIND_GRANT, period=period,
                    # `plan` recorded on the row itself — `reconcile_plan_grant`
                    # below is what reads this back to tell "this period's
                    # grant was written for Free, and the user is on Pro now"
                    # apart from "this period's grant already reflects Pro".
                    # A grant row written before this field existed has no
                    # `plan` in `props`; `reconcile_plan_grant` treats that as
                    # "no known baseline" and skips reconciling it rather than
                    # guessing (see that function's docstring).
                    props={"plan": plan["plan"]},
                )
        except IntegrityError:
            pass


def reconcile_plan_grant(user) -> None:
    """Top up THIS period's balance the moment the user's CURRENT plan
    outranks the plan their period's `KIND_GRANT` row was written for —
    the walk-in-day fix (a customer who paid for Pro mid-month saw no
    balance change until the 1st, because `ensure_monthly_grant` only
    ever writes a period's grant row once and never revisits it).

    Called from the same places `ensure_monthly_grant` is — `balance()`,
    `can_spend()`, `affordable_residue_threads()` — so a plan flip in
    admin is visible the moment ANY of those next runs, not just on the
    1st: reloading Settings or Talk in the SAME request cycle a plan
    change lands is enough.

    IDEMPOTENCY, BY QUERYING, NOT MUTATING. This module's ledger is
    append-only (see the module docstring) — the original `KIND_GRANT` row
    for the period is never touched, so "has this period already been
    reconciled" can't be answered by looking at that row. Instead: does a
    `KIND_UPGRADE` row already exist for this user, this period? A second
    (or concurrent) call in the same period is a no-op the moment the
    first one lands, guarded the identical two ways `ensure_monthly_grant`
    is — `select_for_update` on the user row serializes concurrent callers
    for the SAME user, and the model's `(user, period, kind="upgrade")`
    `UniqueConstraint` turns anything that still races into a harmless
    `IntegrityError` this function swallows.

    DOWNGRADE MID-PERIOD DOES NOT CLAW BACK — a deliberate decision, not an
    oversight. If a Pro user drops to Free mid-month, whatever they were
    already granted this period (the Pro amount, plus any earlier upgrade
    top-up) stays theirs for the rest of the period; nothing here writes a
    negative adjustment for it. Three reasons: (1) it mirrors §6's own
    "overdraw edge" posture — being a little generous costs less than the
    support conversation a clawback invites; (2) `user.plan` is admin-set,
    hand-flipped by the founder (§8) — a downgrade is at least as likely to
    be "undoing an accidental Pro flip" as a real cancellation, and this
    function has no way to tell the two apart; (3) taking credits away from
    a signed-in student's balance out from under them, mid-session, is
    exactly the kind of silent, ambush-y change this whole fix exists to
    prevent on the upgrade side — doing it on the downgrade side would
    trade one walk-in-day bug for another.

    ROLLOVER CAP, same shape `ensure_monthly_grant` enforces: the top-up is
    clamped so the balance right after it lands never exceeds 2x the NEW
    plan's monthly grant (`min(new_grant - old_grant, 2*new_grant -
    current_balance)`, floored at 0) — a dormant account that upgrades
    doesn't get to stack a full rollover AND a full upgrade top-up past the
    cap the grant path already holds everyone else to.
    """
    period = _period_for(user)
    grant_row = (
        CreditLedger.objects.for_user(user)
        .filter(kind=CreditLedger.KIND_GRANT, period=period)
        .order_by("created")
        .first()
    )
    if grant_row is None:
        # ensure_monthly_grant hasn't run yet this period — nothing to
        # reconcile against. Whichever plan writes that grant row (via
        # plan_config -> _plan_of(user), read fresh at write time) is
        # already the user's CURRENT plan, so there is nothing to top up.
        return

    granted_plan = (grant_row.props or {}).get("plan")
    current_plan = _plan_of(user)
    if granted_plan not in _PLAN_RANK:
        # A legacy grant row written before this field existed, or an
        # unrecognized plan string. No known baseline to compare against —
        # skip rather than guess an upgrade that may not have happened.
        return
    if _PLAN_RANK[current_plan] <= _PLAN_RANK[granted_plan]:
        return  # same plan as the grant, or a downgrade — see docstring

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        # Re-check inside the lock, same reason ensure_monthly_grant does:
        # another request may have written the upgrade row while this one
        # waited on the lock.
        if CreditLedger.objects.for_user(user).filter(
            kind=CreditLedger.KIND_UPGRADE, period=period
        ).exists():
            return
        new_grant = _plan_config(current_plan)["monthly_grant"]
        old_grant = _plan_config(granted_plan)["monthly_grant"]
        current_balance = _raw_balance(user)
        delta = max(0, min(new_grant - old_grant, 2 * new_grant - current_balance))
        if delta <= 0:
            return
        try:
            # A nested atomic (a savepoint), same reason ensure_monthly_grant
            # uses one: a swallowed IntegrityError must not poison the outer
            # transaction the row-lock is held in.
            with transaction.atomic():
                CreditLedger.all_objects.create(
                    user=user, delta=delta, kind=CreditLedger.KIND_UPGRADE, period=period,
                    props={"from_plan": granted_plan, "to_plan": current_plan},
                )
        except IntegrityError:
            pass


def balance(user) -> int:
    """This user's current credit balance — the number the UI shows.
    Ensures the month's grant exists first (the lazy write §4 describes),
    reconciles a mid-period plan upgrade second (see
    `reconcile_plan_grant`), then floors at zero for display (§6:
    "displayed balance floors at zero")."""
    ensure_monthly_grant(user)
    reconcile_plan_grant(user)
    return max(0, _raw_balance(user))


# ---------------------------------------------------------------------------
# Writes / the spend gate
# ---------------------------------------------------------------------------
def can_spend(user, cost: int) -> bool:
    """Whether an action costing `cost` credits is allowed to START — checked
    once, before it starts (§6: "hard stop before a turn starts, never
    mid-turn"). Two independent conditions, both from §4:

    - a positive balance. Deliberately NOT `balance >= cost` — a Pro
      student on their last 1-2 credits is still let through and may
      overdraw by the full `cost`, per §6's "overdraw edge": stranding 1-2
      credits every month for no benefit costs more in support
      conversations than being ~$0.04 generous.
    - today's spend hasn't already hit the daily burst guard, the abuse
      backstop that replaces the old flat daily-message cap.
    """
    ensure_monthly_grant(user)
    reconcile_plan_grant(user)
    plan = plan_config(user)
    return _raw_balance(user) > 0 and daily_spent(user) < plan["daily_burst"]


def spend(user, cost: int, kind: str, **props) -> None:
    """One negative ledger row. `cost` is always given as a positive number
    of credits; this stores its negation. Deliberately does not check
    affordability itself — `can_spend` is a separate, earlier call, so a
    debit always lands at the exact point the fairness rule requires: after
    the metered work actually happened (a chat turn's round 0 succeeded, a
    rescan's residue stage actually classified something), never before and
    never speculatively. A non-positive `cost` is a no-op — nothing metered
    ran, so nothing gets written.

    Written inside a transaction holding this user's row lock — the same
    single-row `select_for_update` `ensure_monthly_grant` takes, and for the
    same reason. Every read-then-write path in this module (the grant, the
    upgrade top-up, `_spend_clamped` below) serializes on that one lock, so
    two debits landing for the same user in the same instant can never each
    compute a balance the other is about to change. It costs one extra
    round-trip on a path that already writes; the alternative is a ledger
    whose arithmetic depends on cron timing.
    """
    if cost <= 0:
        return
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        CreditLedger.all_objects.create(user=user, delta=-int(cost), kind=kind, props=props or {})


def _spend_clamped(user, cost: int, kind: str, **props) -> int:
    """`spend()` for the two surfaces that PRE-CLAMPED their work against
    `affordable_residue_threads` / `affordable_autopilot_rows` before doing
    it. Re-runs that clamp inside the row lock and charges at most what the
    ledger can actually pay for at debit time. Returns the credits charged.

    THE RACE THIS CLOSES. Both clamps are pure reads with no lock, and
    render.yaml fires `coverage-gmail-backfill` and `coverage-autopilot` on
    the same tick. A student with 5 credits, one pending rescan and one
    queued autopilot run had both jobs clamp their work against the same 5,
    do it, and then both debit 5 — ending at -5, well past the "overdraw by
    at most one action's cost" edge `can_spend` documents, and past the
    daily burst guard that exists to bound exactly this.

    WHY THE OVERSPEND IS WRITTEN OFF RATHER THAN BILLED. The second job did
    real work that cost real API money, so charging for it is arguably
    correct. It is not what this module does anywhere else: the clamp is a
    promise made to the caller BEFORE the work started, and when two clamps
    raced, the second caller was told it could afford work it could not.
    Honouring the promise and eating the difference matches `can_spend`'s
    own "being a little generous costs less than the support conversation"
    posture, and it keeps the invariant that actually matters — a balance
    only ever goes as negative as one deliberate overdraw. `props` records
    `requested_credits` on any clamped row, so the write-off is a query, not
    a silent hole.

    The chat and brief debits deliberately do NOT come through here: their
    gate is `can_spend`, which is documented to let a Pro student on their
    last credit overdraw by the full message cost. Clamping them would
    quietly undo that.
    """
    if cost <= 0:
        return 0
    # Before the lock, and one `.exists()` in the overwhelmingly common case
    # where the caller's own pre-clamp already wrote it. It matters for the
    # minutes-wide window where a pass clamps on the last day of a month and
    # debits on the first of the next: without it the balance read below
    # would see a month with no grant row yet and clamp the debit to zero.
    ensure_monthly_grant(user)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        plan = plan_config(user)
        available_balance = max(0, _raw_balance(user))
        daily_left = max(0, plan["daily_burst"] - daily_spent(user))
        charge = max(0, min(int(cost), available_balance, daily_left))
        if charge <= 0:
            return 0
        row_props = dict(props or {})
        if charge != cost:
            row_props["requested_credits"] = int(cost)
        CreditLedger.all_objects.create(
            user=user, delta=-charge, kind=kind, props=row_props
        )
    return charge


def refund(user, cost: int, **props) -> None:
    """One positive ledger row reversing an earlier `spend()` that turned
    out not to correspond to work the student actually got anything for.

    Exists for exactly one shape of failure: a chat turn's round 0
    succeeded (charged, per `spend`'s own fairness rule above) but a LATER
    round then failed outright — a network hiccup mid-turn, or the round
    cap running out before the model ever landed an answer — so the turn
    still ends in a plain failure notice, the same as one that never
    reached the model at all. The round-0 charge already fired by the time
    that's known, so the fix is a compensating credit, not skipping the
    original debit.

    Written as its own `KIND_REFUND`, NOT `KIND_ADJUST` — this used to
    reuse `KIND_ADJUST` (the same kind an admin correction writes), which
    got the balance right but nothing else: `_SPEND_KINDS` excludes
    `KIND_ADJUST` from the daily burst guard and the Settings "used this
    month" line, so the ORIGINAL negative `spend_chat`/`spend_rescan` row
    kept counting against both even after the refund restored the balance.
    A run of turns that all fail after round 0 — an Anthropic outage, a
    flaky patch of tool-call network errors — could burn through a
    student's entire `daily_burst` and lock them out for the rest of the
    day while `balance()` showed them untouched, and
    `agent.py::_credit_block_notice` would then tell them "your credits for
    the month are still there" while they'd gotten zero answers. Settings
    would show the same mismatch: a full balance next to a nonzero "used N
    this month."

    The counter-argument — round 0 DID reach the API and cost real money,
    so the burst guard, an abuse backstop against a runaway loop or a
    shared password (docs/credit-system-plan.md §4), is arguably right to
    count it — doesn't hold up against this module's own fairness rule.
    `spend`'s docstring and the call sites in `assistant/agent.py` are
    explicit that the rule is "never charged for a request the student
    didn't get an answer to," full stop, not "...unless the API had
    already been called." A refunded turn is defined to be, from the
    student's side, exactly as if it never happened — the burst guard and
    the usage line have to agree with the balance on that, or the refund is
    only half-honored. (The actual model-spend accounting this
    counter-argument cares about already happens independently, in
    `assistant/agent.py::_log_usage`'s `assistant_usage` event, which
    is not touched by a refund — that's the one place a refunded round 0's
    real API cost is still visible.)

    `daily_spent`/`month_usage` net a `KIND_REFUND` row against its
    matching spend row in the same window (`_NET_SPEND_KINDS`, this
    module, above) — so a same-day refund fully cancels the burst-guard and
    monthly-usage impact of the spend it reverses, the same way it already
    cancels the balance impact. A non-positive `cost` is a no-op, same as
    `spend`.
    """
    if cost <= 0:
        return
    CreditLedger.all_objects.create(user=user, delta=int(cost), kind=CreditLedger.KIND_REFUND, props=props or {})


# ---------------------------------------------------------------------------
# Rescan-specific helpers (enforcement point 2 — capture/gmail_live.py)
# ---------------------------------------------------------------------------
def affordable_residue_threads(user, candidate_threads: int) -> int:
    """How many of `candidate_threads` residue messages this user's ledger
    can pay for right now (§5's enforcement point 2), computed BEFORE a
    single classification call is made. A pure read — `spend_rescan` below
    is what records what actually ran.

    Clamped by both the monthly balance and whatever's left of today's
    burst guard, converted from credits to threads via
    `CREDIT_RESCAN_THREADS_PER_CREDIT`. Callers still apply
    `gmail_residue.MAX_RESIDUE_THREADS` on top of this — the two caps are
    independent (one is a spend limit, the other a per-run API-cost
    ceiling) and the smaller one wins.
    """
    if candidate_threads <= 0:
        return 0
    ensure_monthly_grant(user)
    reconcile_plan_grant(user)
    plan = plan_config(user)
    available_balance = max(0, _raw_balance(user))
    daily_left = max(0, plan["daily_burst"] - daily_spent(user))
    affordable_credits = min(available_balance, daily_left)
    per_credit = rescan_threads_per_credit()
    return max(0, min(candidate_threads, affordable_credits * per_credit))


# ---------------------------------------------------------------------------
# Purchases (enforcement point 3 — billing/stripe_gateway.py's webhook)
# ---------------------------------------------------------------------------
def grant_purchase(user, pack_key: str, stripe_event_id: str) -> None:
    """One positive ledger row for a completed pay-as-you-go top-up
    (docs/credit-system-plan.md's "Stripe later" note: "paid top-up packs
    ... are just positive ledger rows with `kind='purchase'`"). Called
    exactly once per Stripe event, from inside the same `atomic()` block
    `stripe_gateway.handle_webhook_event` uses to write the
    `ProcessedStripeEvent` idempotency row — this function itself does not
    re-check for a duplicate `stripe_event_id`; that guard lives one layer
    up, same as `spend()` above trusting `can_spend()` to have already
    decided affordability.

    `stripe_gateway.CREDIT_PACKS` is the single source of truth for a
    pack's price and credit amount, imported lazily (inside the function
    body, not at module level) to avoid a circular import — `credits.py`
    is the lower-level module both `stripe_gateway.py` and every other
    caller build on, per this module's own docstring on not importing
    upward from its callers.
    """
    from . import stripe_gateway

    pack = stripe_gateway.CREDIT_PACKS[pack_key]
    CreditLedger.all_objects.create(
        user=user,
        delta=pack["credits"],
        kind=CreditLedger.KIND_PURCHASE,
        props={
            "pack": pack_key,
            "stripe_event_id": stripe_event_id,
            "price_cents": pack["price_cents"],
        },
    )


def autopilot_rows_per_credit() -> int:
    value = getattr(settings, "CREDIT_AUTOPILOT_ROWS_PER_CREDIT", None)
    return int(value) if value is not None else _DEFAULT_AUTOPILOT_ROWS_PER_CREDIT


def affordable_autopilot_rows(user, candidate_rows: int) -> int:
    """How many of `candidate_rows` pending rows this user's ledger can pay
    an autopilot verdict for right now — the same pre-clamp
    `affordable_residue_threads` performs for the rescan's residue stage,
    computed BEFORE a single model call is made. A pure read;
    `spend_autopilot` below records what actually ran. Callers still apply
    `capture.autopilot.MAX_ROWS_PER_RUN` on top — a spend limit and a
    per-run blast-radius ceiling are independent, and the smaller wins."""
    if candidate_rows <= 0:
        return 0
    ensure_monthly_grant(user)
    reconcile_plan_grant(user)
    plan = plan_config(user)
    available_balance = max(0, _raw_balance(user))
    daily_left = max(0, plan["daily_burst"] - daily_spent(user))
    affordable_credits = min(available_balance, daily_left)
    per_credit = autopilot_rows_per_credit()
    return max(0, min(candidate_rows, affordable_credits * per_credit))


def spend_autopilot(user, rows_decided: int) -> None:
    """Debit for one autopilot decide pass — one credit per
    `CREDIT_AUTOPILOT_ROWS_PER_CREDIT` rows the model ACTUALLY decided,
    ceil-rounded, `rows` in `props` for the audit trail. Zero rows (dry
    run, nothing pending, AI unconfigured) writes nothing — the same
    no-empty-debit rule `spend_rescan` holds.

    Goes through `_spend_clamped`, not `spend`: this pass clamped its work
    against `affordable_autopilot_rows` before starting, and that clamp
    races the rescan's — see `_spend_clamped`'s docstring."""
    if rows_decided <= 0:
        return
    per_credit = autopilot_rows_per_credit()
    cost = -(-rows_decided // per_credit)  # ceil division, no float math
    _spend_clamped(user, cost, CreditLedger.KIND_SPEND_AUTOPILOT, rows=rows_decided)


def spend_rescan(user, threads_classified: int) -> None:
    """Debit for one rescan's residue stage — one credit per
    `CREDIT_RESCAN_THREADS_PER_CREDIT` threads ACTUALLY classified, rounded
    up, with `threads` recorded in `props` for the audit trail (§1: "charged
    in one ledger row at the end of the pass"). A no-op at zero — a rescan
    whose residue stage never ran (no credits affordable, AI not
    configured, or no residue at all) must not write an empty debit row.

    Goes through `_spend_clamped`, not `spend`, for the reason
    `spend_autopilot` above gives: this pass clamped its work against
    `affordable_residue_threads` before starting, and that clamp races the
    autopilot worker's."""
    if threads_classified <= 0:
        return
    per_credit = rescan_threads_per_credit()
    cost = -(-threads_classified // per_credit)  # ceil division, no float math
    _spend_clamped(user, cost, CreditLedger.KIND_SPEND_RESCAN, threads=threads_classified)
