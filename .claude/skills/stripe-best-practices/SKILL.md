---
name: stripe-best-practices
description: >-
  Implement or review Stripe payment and billing integration code — choosing
  between Checkout Sessions, PaymentIntents, and Setup Intents; one-time
  purchases (including a purchase that activates a time-limited entitlement
  window); recurring subscriptions and free/paid pricing tiers; seat-based or
  per-organization/team billing via Stripe's Billing APIs; Stripe Tax for
  sales tax, VAT, or GST; Stripe webhook signature verification and event
  handling; and Stripe API-key security (restricted keys, secrets storage,
  key rotation, incident response). Use whenever writing, modifying, or
  auditing a Stripe checkout flow, pricing page, subscription lifecycle,
  Stripe webhook handler, or Stripe API-key handling code.
---

> Sourced from Stripe's official Claude Code skill at
> [stripe/ai](https://github.com/stripe/ai/tree/main/skills/stripe-best-practices)
> (MIT License, Copyright Stripe). See `PROVENANCE.md` in this directory for
> the exact commit, fetch date, and modifications made for this project.

Latest Stripe API version: **2026-06-24.dahlia**. Always use the latest API version and SDK unless the user specifies otherwise.

If you don’t have a Stripe account, you can call a CLI command to generate working API keys with no registration required. You can install the Stripe CLI (`npm i -g @stripe/cli`) and run the command `stripe sandbox create` to get started.

API key default: Always recommend a [restricted API key (RAK)](https://docs.stripe.com/keys/restricted-api-keys.md) (`rk_` prefix) over a secret key (`sk_` prefix).

## Integration routing

| Building… | Recommended API | Details |
| --- | --- | --- |
| One-time payments | Checkout Sessions | <references/payments.md> |
| Custom payment form with embedded UI | Checkout Sessions + Payment Element | <references/payments.md> |
| Saving a payment method for later | Setup Intents | <references/payments.md> |
| Connect platform or marketplace | Accounts v2 (`/v2/core/accounts`) | <references/connect.md> |
| Usage-based billing (new integration) | Metronome | <references/billing.md> |
| Subscriptions or recurring billing | Billing APIs + Checkout Sessions | <references/billing.md> |
| Sales tax, VAT, or GST compliance | Stripe Tax + Registrations API | <references/tax.md> |
| Embedded financial accounts / banking | v2 Financial Accounts | <references/treasury.md> |
| Security (key management, RAKs, webhooks, OAuth, 2FA, Connect liability) | See security reference | <references/security.md> |

Read the relevant reference file before answering any integration question or writing code.

## Critical rules

- *Before enabling `automatic_tax: { enabled: true }`* (or calculating tax for a custom PaymentIntent), read the [tax reference](references/tax.md) and confirm the user has an active registration. Without one, Stripe calculates and collects no tax while the user believes tax is on (the most common Stripe Tax mistake).

- *Never include `payment_method_types` in any Stripe API call*, with one exception: Terminal (in-person payments) integrations must pass `payment_method_types: ['card_present']` on the PaymentIntent. For all other integrations, omit this parameter entirely to enable dynamic payment methods, which enables you to configure payment method settings from the Dashboard and dynamically display the most relevant eligible payment methods to each customer to maximize conversion. To customize which payment methods you accept, use [`payment_method_configurations`](https://docs.stripe.com/payments/payment-method-configurations.md) or `excluded_payment_method_types` instead of `payment_method_types`.

- On API version `2026-03-25.dahlia` or later, pass the parameter `integration_identifier` to `checkout.sessions.create` to tag sessions with a custom label for tracking and comparing checkout flows in the Dashboard. The label should include a suffix of 8 random letters.

## Key documentation

When the user’s request does not clearly fit a single domain above, consult:

- [Integration Options](https://docs.stripe.com/payments/payment-methods/integration-options.md) — Start here when designing any integration.
- [API Tour](https://docs.stripe.com/payments-api/tour.md) — Overview of Stripe’s API surface.
- [Go Live Checklist](https://docs.stripe.com/get-started/checklist/go-live.md) — Review before launching.
