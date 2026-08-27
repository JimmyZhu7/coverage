"""billing — Coverage's credit ledger (docs/credit-system-plan.md).

An append-only audit trail for every credit grant, spend, and admin
adjustment, matching the convention `analytics.ProductEvent` already set
(see that model's docstring): no mutable balance column, because a running
total is a second copy of the truth that can silently drift from its own
history. At this scale, `Sum("delta")` over an indexed per-user queryset is
microseconds, and the ledger itself IS the audit trail — every grant, every
spend, every admin top-up is a row somebody can read back.

Three metered AI surfaces spend from this ONE pool: `assistant/agent.py`
("spend_chat", one row per student chat message), `capture/gmail_residue.py`
via `capture/gmail_live.py::run_rescan` ("spend_rescan", one row per
rescan's residue stage), and `crm/views.py::contact_ai_brief`
("spend_brief", one row per successfully generated coffee-chat brief).
Neither app owns credits — this app does, so a new metered surface is a
new `kind` constant, not a new ledger.

Private zone, like every other per-user table — `coverage_web.tenancy`'s
`PrivateModel` base, so `objects` refuses an unscoped query and the only
supported way to read a student's own rows is `.objects.for_user(user)`.
This app's own writer path (`billing/credits.py`) and Django admin reach for
`all_objects` explicitly, matching `analytics.record_event`'s posture: a
visible, greppable admission that a write/read is deliberately
cross-tenant, not a loophole around the guard.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from coverage_web.tenancy import PrivateModel


class CreditLedger(PrivateModel):
    KIND_GRANT = "grant"
    KIND_SPEND_CHAT = "spend_chat"
    KIND_SPEND_RESCAN = "spend_rescan"
    # Added 2026-08-19 (docs/founder-decisions-2026-08-20.md §2b): the
    # coffee-chat prep brief (`crm/ai_brief.py` via `complete_text`) is a
    # user-triggered model call behind a POST button, same shape as
    # `spend_chat`, so it joins the ledger's own metered surfaces rather
    # than staying free. One row per successful generation, never on a
    # failed/unconfigured call — see `crm/views.py::contact_ai_brief`.
    KIND_SPEND_BRIEF = "spend_brief"
    # The autopilot decide pass (`capture/autopilot.py`) — one grounded
    # Haiku verdict per pending proposal, debited like `spend_rescan`:
    # rows-per-credit, ceil-rounded, one row at the end of the pass for
    # what ACTUALLY ran. Same meter, new surface — docs/credit-system-plan
    # §1's "one ledger" rule is the reason this is a kind here rather than
    # a second counter somewhere in capture.
    KIND_SPEND_AUTOPILOT = "spend_autopilot"
    KIND_ADJUST = "adjust"
    # A pay-as-you-go top-up (billing/stripe_gateway.py), distinct from
    # KIND_GRANT: a grant is the free monthly allowance every plan carries,
    # a purchase is credits someone paid Stripe for — kept as separate
    # `kind`s so the Settings page and any future revenue reporting can
    # tell the two apart without parsing `props`.
    KIND_PURCHASE = "purchase"
    # A reversal of a specific earlier `spend()` row (billing/credits.py::
    # refund) — kept distinct from KIND_ADJUST on purpose, even though both
    # are positive founder/system-written rows: an adjustment is new
    # activity (a correction with no matching spend, e.g. undoing a
    # mistaken grant), while a refund cancels out a spend that already
    # happened. That distinction is what lets `daily_spent`/`month_usage`
    # net a refund against its matching spend — see this app's
    # `credits.py::_SPEND_KINDS` for why the two must not share a `kind`.
    KIND_REFUND = "refund"
    # A mid-period top-up when a user's plan outranks the plan their
    # period's `KIND_GRANT` row was written for (billing/credits.py::
    # reconcile_plan_grant) — the walk-in-day fix: a Free -> Pro flip mid-
    # month used to leave the displayed balance at the Free remainder until
    # the 1st, because `ensure_monthly_grant` only ever writes a period's
    # grant row once. Kept distinct from KIND_GRANT (this ISN'T "the
    # month's allowance" — that row already exists and is never touched,
    # per the ledger's append-only discipline) and from KIND_ADJUST (this
    # IS derived from a specific, detectable event — a plan change against
    # an existing grant — not a free-form founder correction with nothing
    # to key idempotency off of). The `(user, period)` uniqueness below is
    # what makes "did this period already get its upgrade top-up" a query,
    # not a guess.
    KIND_UPGRADE = "upgrade"
    KIND_CHOICES = [
        (KIND_GRANT, "Monthly grant"),
        (KIND_SPEND_CHAT, "Chat message"),
        (KIND_SPEND_RESCAN, "Rescan residue classification"),
        (KIND_SPEND_BRIEF, "Coffee-chat brief"),
        (KIND_SPEND_AUTOPILOT, "Autopilot review pass"),
        (KIND_ADJUST, "Admin adjustment"),
        (KIND_PURCHASE, "Credit top-up purchase"),
        (KIND_REFUND, "Refund of a failed turn"),
        (KIND_UPGRADE, "Mid-period plan upgrade top-up"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Positive for a grant or an admin top-up, negative for a spend. Never
    # zero — a zero-delta row would be a no-op audit entry with nothing to
    # record.
    delta = models.IntegerField()
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    # "2026-08", written only on grant rows. The UniqueConstraint below is
    # what actually enforces "at most one grant per user per month" — this
    # column exists so that constraint has something to key on, and so a
    # grant row can be found again ("did August already get its grant?")
    # without scanning every row and parsing `created`.
    period = models.CharField(max_length=7, blank=True, default="")
    # Free-form audit detail: {"model": "claude-sonnet-5"} on a chat spend,
    # {"threads": 37} on a rescan spend. Never a secret or message content.
    props = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "credit_ledger"
        ordering = ["-created"]
        indexes = [models.Index(fields=["user", "created"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "period"],
                condition=Q(kind="grant"),
                name="uniq_monthly_grant",
            ),
            # Same shape, one row per (user, period) — `reconcile_plan_grant`'s
            # own idempotency backstop, matching `ensure_monthly_grant`'s.
            models.UniqueConstraint(
                fields=["user", "period"],
                condition=Q(kind="upgrade"),
                name="uniq_monthly_upgrade",
            ),
        ]

    def __str__(self) -> str:
        sign = "+" if self.delta >= 0 else ""
        return f"{self.user_id}: {sign}{self.delta} ({self.kind})"


class ProWaitlist(PrivateModel):
    """Pro launch waitlist — the pricing page's Pro card is stamped "In the
    works" with no checkout yet (docs/pricing-rebalance-plan.md §4), so this
    is where the "Notify me when Pro opens" click actually goes. One row per
    interested email; `billing/views.py::waitlist_join` writes it alongside
    a `pro_waitlist_joined` product event, same posture as every other
    funnel signal (see `analytics.events.record_event`'s docstring).

    No email is ever sent from this table today — there is no provider
    configured (`settings.EMAIL_URL` only wires the password-reset and
    weekly-digest sends, both of which already print to logs instead when
    unset). This is intent capture, not a mailing list: the founder reads it
    by hand in admin until Pro has a real checkout to notify people into.

    `user` nullable for the same reason `analytics.ProductEvent.user` is
    (see that model and `coverage_web.tenancy.PrivateModel`'s own
    docstring): a logged-out visitor can join with just an email.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pro_waitlist_entries",
    )
    email = models.EmailField()
    # WHICH INTENT the click expressed, and part of the row's identity — see
    # the constraint below. "pricing_page" is the Pro card; "pricing_page_team"
    # is the Team card's "Run a club? Notify me". The allowlist of accepted
    # values lives in `billing/views.py::_WAITLIST_SOURCES` beside the
    # confirmation sentence each one gets.
    source = models.CharField(max_length=32, default="pricing_page")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "pro_waitlist"
        ordering = ["-created"]
        constraints = [
            # The dedupe half of "rate-limit/dedupe by email" — a second
            # join with the same address FOR THE SAME INTENT is a no-op, not
            # a second row.
            #
            # (email, source), not email alone. Wanting Pro and running a
            # club are two different asks from one person, and the whole
            # point of `source` is that the founder can read the Team list
            # without the Pro list in it. Keyed on email alone, the second
            # ask was a silent `get_or_create` no-op that left one row
            # labelled with whichever intent happened to come first — the
            # segmentation the field exists for, quietly discarded at the
            # only moment it mattered.
            models.UniqueConstraint(
                fields=["email", "source"], name="uniq_pro_waitlist_email_source"),
        ]

    def __str__(self) -> str:
        return self.email


class ProcessedStripeEvent(models.Model):
    """Idempotency record for `billing/stripe_gateway.py::handle_webhook_event`
    — Stripe's own docs guarantee at-least-once webhook delivery, so the
    same `checkout.session.completed` event can (and does, in practice)
    arrive twice. This table is the guard: the webhook handler does a
    `get_or_create(stripe_event_id=...)` inside `transaction.atomic()`
    BEFORE granting credits, so a duplicate delivery — or two concurrent
    deliveries racing each other — hits this row's unique constraint
    instead of writing a second grant.

    Deliberately NOT a `PrivateModel` like `CreditLedger` above: a webhook
    delivery has no request-time tenant context to scope against (Stripe
    calls this server-to-server, with no signed-in user at all) — the
    event ID it carries is the only identity available, and it names no
    user until the handler reads `session.metadata["user_id"]` out of the
    verified payload. A plain model with a global unique constraint is the
    right shape for "have we seen this event ID before," full stop.
    """

    stripe_event_id = models.CharField(max_length=255, unique=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.stripe_event_id
