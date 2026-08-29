"""Adversarial invariant suite for `reverify`'s confidence tiering
(`_verify_confidence` and the `verified-open` branch in
`management/commands/reverify.py`) — the rule that a date `reverify`
corrects must carry the confidence the ANSWERING CONNECTOR actually earns,
not whatever confidence the row happened to hold before this pass.

WHAT MAKES THIS DIFFERENT from `test_reverify.py` next door. That file is
example-based: it pins the confirmed-live defect (29 of 31 open rows whose
deadline `reverify` most recently moved sat at confidence 1.0 while their
date came from a text-tier connector) against one BMO/Workday fixture. This
file asks the cross-cutting question: does EVERY connector this command can
ever hear from land on the correct tier, for EVERY combination of "did the
date move" and "what confidence did the row start with" — the shape where a
fix verified against one connector quietly leaves a sibling connector (or a
future one nobody has added to `_STRUCTURED_VERIFY_PROVIDERS` yet) on the
wrong side of the line.

Same discipline as `test_stress_facts.py`, `test_stress_ai_extract.py` and
`crm/tests/test_stress_crm.py`: NO `hypothesis`. The interesting space is a
small enumerated cross-product — every provider `coverage_connectors` ships
a `verify()` for for x 3 starting confidences x 2 (date moved / date
unchanged) — walked EXHAUSTIVELY at the pure-function level
(`_verify_confidence`), plus a smaller DB-backed cross-product proving the
command's own branch wires that answer to `Opportunity.confidence`
correctly in both the REPLACED and UNCHANGED cases.

THE INVARIANTS

  1. EVERY PROVIDER LANDS ON EXACTLY ONE TIER. `_verify_confidence` returns
     1.0 for the two connectors that read a genuine structured field
     (greenhouse, oracle) and 0.6 for every other provider `coverage_
     connectors` currently ships — including the text-tier ones
     (tal.net, Workday) and every provider that never actually populates
     `deadline_dates` (icims, eightfold, ...), because an unrecognised or
     silent provider must default to the CONSERVATIVE tier, never the
     confident one.

  2. A REPLACED DATE CARRIES ITS OWN CONNECTOR'S TIER — never the
     confidence the row held before this pass, whether that prior value was
     higher (the confirmed-live defect) or lower.

  3. AN UNCHANGED DATE NEVER LOSES CERTAINTY. Reconfirming the same date
     through a weaker-tier connector must not downgrade a row already
     sitting at a higher confidence — `max()`, not an overwrite.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands.reverify import (
    _STRUCTURED_VERIFY_PROVIDERS, _TEXT_VERIFY_CONFIDENCE, _verify_confidence,
)
from directory.management.commands import reverify as reverify_mod
from directory.models import Firm, Opportunity

# Every provider module `coverage_connectors` ships a `verify()` for, as of
# this suite's writing. A provider added later and left out of this tuple
# is exactly the gap invariant 1 exists to catch — this list is meant to be
# extended alongside `coverage_connectors`, not treated as closed.
ALL_VERIFY_PROVIDERS = (
    "greenhouse", "oracle",            # structured field -> 1.0
    "talnet", "workday",               # regex over the posting's own text -> 0.6
    "icims", "eightfold", "avature", "beisen",  # never populate deadline_dates -> 0.6
    "lever", "phenom", "socgen", "successfactors",
    "talentgateway", "talentsoft", "mckinsey", "goldmansachs",
    "some-future-connector-not-yet-classified",  # unknown -> conservative default
)


@pytest.mark.parametrize("provider", ALL_VERIFY_PROVIDERS)
def test_every_provider_lands_on_exactly_one_tier(provider):
    tier = _verify_confidence(provider)
    if provider in _STRUCTURED_VERIFY_PROVIDERS:
        assert tier == 1.0, provider
    else:
        assert tier == _TEXT_VERIFY_CONFIDENCE, provider


def test_the_structured_tier_is_exactly_greenhouse_and_oracle():
    """Locks the membership itself, not just the function's behaviour on it
    — a connector added to `_STRUCTURED_VERIFY_PROVIDERS` without actually
    reading a structured API field would pass invariant 1 above vacuously."""
    assert _STRUCTURED_VERIFY_PROVIDERS == frozenset({"greenhouse", "oracle"})


# ---------------------------------------------------------------------------
# DB-backed cross-product: REPLACED vs UNCHANGED x starting confidence, for
# one structured-tier and one text-tier connector — proving the command's
# branch actually wires `_verify_confidence`'s answer onto the row in both
# shapes, not just that the pure function returns the right number.
# ---------------------------------------------------------------------------

STARTING_CONFIDENCES = (0.0, 0.6, 1.0)
REPRESENTATIVE_PROVIDERS = ("greenhouse", "workday")  # one per tier


def _opp(firm, url, *, deadline, confidence):
    ts = timezone.now() - timedelta(days=10)
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket="internship", status="open",
        deadline=deadline, deadline_precision="day" if deadline else "",
        confidence=confidence,
    )
    Opportunity.objects.filter(pk=o.pk).update(
        last_checked=ts, last_verified=ts, deadline_checked_at=ts,
    )
    o.refresh_from_db()
    return o


@pytest.mark.django_db
@pytest.mark.parametrize("starting_confidence", STARTING_CONFIDENCES)
@pytest.mark.parametrize("provider", REPRESENTATIVE_PROVIDERS)
def test_a_replaced_date_always_carries_its_connectors_own_tier(
        monkeypatch, provider, starting_confidence):
    firm = Firm.objects.create(slug=f"f-{provider}-{starting_confidence}", name="Firm")
    old_deadline = date(2026, 5, 24)
    new_deadline = date(2026, 8, 30)
    row = _opp(firm, f"https://x/{provider}/{starting_confidence}",
               deadline=old_deadline, confidence=starting_confidence)

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: VerificationResult(
            provider=provider, url=url, result="verified-open", evidence="test",
            deadline_dates=[new_deadline.isoformat()]),
    )
    call_command("reverify", ids=str(row.id))
    row.refresh_from_db()

    assert row.deadline == new_deadline
    assert row.confidence == _verify_confidence(provider), (
        f"provider={provider} starting_confidence={starting_confidence}")


@pytest.mark.django_db
@pytest.mark.parametrize("starting_confidence", STARTING_CONFIDENCES)
@pytest.mark.parametrize("provider", REPRESENTATIVE_PROVIDERS)
def test_an_unchanged_date_never_loses_certainty(
        monkeypatch, provider, starting_confidence):
    firm = Firm.objects.create(slug=f"u-{provider}-{starting_confidence}", name="Firm")
    same_deadline = date(2026, 8, 30)
    row = _opp(firm, f"https://x/{provider}/{starting_confidence}/same",
               deadline=same_deadline, confidence=starting_confidence)

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: VerificationResult(
            provider=provider, url=url, result="verified-open", evidence="test",
            deadline_dates=[same_deadline.isoformat()]),
    )
    call_command("reverify", ids=str(row.id))
    row.refresh_from_db()

    assert row.deadline == same_deadline
    expected = max(starting_confidence, _verify_confidence(provider))
    assert row.confidence == expected, (
        f"provider={provider} starting_confidence={starting_confidence}")
