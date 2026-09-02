# B2B2C: the minimum schema, sketched

Copied verbatim from §1.5 of the read-only billing/deploy audit of
2026-09-01, so the product plan can reference a fixed text rather than a
memory of one.

**Nothing here is built, and nothing here should be built off this file
alone.** This is a sketch with the audit's own caveats attached, not a
specification. It exists because the audit found the shape of the gap and
the plan needs somewhere to point at it.

---

## The gap it answers

There is no code. Grep for org/institution/agency/team/club/seat/membership
models across all apps: nothing. The only Team artifacts are
`ProWaitlist.source="pricing_page_team"` and the pricing card. `User.school`
and `User.affiliations` are free text with no FK. `User.plan` is a bare
string with no `ends_at` for a **paid** Pro cycle (only the trial has one),
so even self-serve Pro cannot be represented as a 6-month entitlement today;
it is an admin flip and a calendar reminder.

`coverage_web/tenancy.py` helps more than it hinders: `TenantManager.
get_queryset()` raises, `for_user()` is the only read path, `all_objects` is
the greppable escape hatch. Every private row carries `user_id` only; there
is no `org_id`, so org-wide views are a join through a membership table
(fine at 50 seats). The memory file's red line (a grant-checked read path,
never a tenancy loosening) maps onto this cleanly: add a second explicit
manager method rather than widening `for_user`.

## Minimum schema for the OSG pilot (50 seats, spring 2027), four tables

```
Organization        id, slug, name, kind (agency|club|career_center), status,
                    seat_cap, billing_email, stripe_customer_id, contract_starts, contract_ends
OrgMembership       org FK, user FK, role (admin|mentor|student), status (invited|active|removed),
                    invited_by FK, invited_email, joined_at, removed_at; unique (org, user)
Entitlement         user FK, plan ("pro"), source (org_seat|self_serve|trial|admin), org FK null,
                    starts_at, ends_at, stripe_ref; index (user, ends_at)
                    -> `plan_of(user)` becomes "an Entitlement with ends_at > now exists";
                       User.plan/pro_trial_* migrate into rows of source=admin/trial.
AccessGrant         student FK, grantee_user FK (mentor) or grantee_org FK, scopes JSON
                    (pipeline, contacts, touches; gmail_derived default False),
                    granted_at, revoked_at, consent_text_version
                    -> a new `PrivateModel.objects.for_grant(grant)` that verifies the grant
                       then returns `for_user(grant.student)`; mentor views never call for_user
                       on someone else's id directly.
```

## Stripe shape

Per the `stripe-best-practices` skill the audit had loaded: per-organization
seats belong on Billing (subscription with `quantity` = seats, or a one-time
Checkout `mode=payment` per cycle writing `Entitlement` rows with `ends_at`),
never on the credit-pack path that exists today.

## Caveats carried over from the audit

- The audit was **read-only**: no repo edits, no DB writes, no pytest, no
  network calls. Nothing in this sketch has been compiled, migrated or
  tested.
- It was written against a database with **4 users**, one of them the
  founder's admin-set Pro account. "Fine at 50 seats" is a judgement about
  the pilot's scale, not a measurement.
- `Entitlement` replacing `User.plan` is a migration of a field currently
  read by `assistant/plans.py`, `billing/credits.py`, `capture/gmail_live.py`
  (four gates), `capture/management/commands/gmail_poll.py` and two Settings
  cards. The sketch names the destination, not the path.
- The `AccessGrant` / `for_grant()` half is the compliance-sensitive one.
  `gmail_derived default False` is not a detail: the Limited Use posture the
  whole B2B plan rests on is that mail read on a student's behalf may
  propose and only the student's own tap may change their record (see
  `capture/models.py::AutopilotRun`). A mentor read path over
  Gmail-derived rows is a separate decision, not a scope flag to flip.
- The audit could not establish whether a Stripe account exists at all; the
  code assumes not.

## Ranked defects this sits on top of, from the same audit

1. **No paid-Pro representation.** No cycle `ends_at`, no Stripe path, no
   entitlement table. Pro today is `admin -> User -> plan`. Blocks
   self-serve and the agency pilot alike. *(Unfixed. This sketch is its
   shape.)*
2. Stripe webhook grants on `completed`, not `paid`. *(Fixed 2026-09-02 —
   `billing/stripe_gateway.py`.)*
3. Pricing page promises four unbuilt Pro features. *(Owned elsewhere.)*
4. Trial end is silent. *(Fixed 2026-09-02 — Settings banner plus an email
   behind the existing `EMAIL_URL` gate.)*
5. Credit clamp race between backfill rescan and autopilot. *(Fixed
   2026-09-02 — `billing.credits._spend_clamped`, plus a two-minute cron
   offset.)*
6. Cron order: watch-renew before trial-expire. *(Fixed 2026-09-02 —
   `render.yaml`.)*
7. Expired trialist's Scan Now locked for up to 7 days. *(Fixed 2026-09-02 —
   `accounts.trials.reset_free_rescan_throttle`.)*
8. `gmail_poll` in loop mode wrote no `JobRun`. *(Fixed 2026-09-02 —
   `ops.tracking.JobHeartbeat`.)*
