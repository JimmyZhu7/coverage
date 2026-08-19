"""One place that decides what a role's sponsorship answer IS, so the pill,
the feed filter, the eligibility verdict and the onboarding preview can never
quietly disagree about it again.

Until this module existed, `directory/views.py::_sponsorship_tag` (the pill)
was the only surface that fell back to `Firm.sponsors` when a posting stated
nothing — `_apply_sponsorship_filter`, `_eligibility` and
`accounts/onboarding_preview.py::work_preview` all read `opp.sponsorship`
alone. That meant a role could show a "No Sponsorship · firm policy" pill and
still pass a "Sponsors visas" filter, or clear a visa eligibility check the
pill itself was warning against. `docs/founder-decisions-2026-08-20.md`,
Decision 3, calls this out by name: 319 open campus rows have a firm-level
answer the pill already shows and everything else ignores.

THE PRECEDENCE, followed everywhere in this module: a posting's own stated
answer beats the firm's policy, which beats not stated at all. A posting
saying nothing is not the firm's problem to solve for every role at once —
`SIG` may sponsor HK visas broadly while one specific SIG HK posting states
it will not, and the posting wins.

THE VOCABULARY: "yes" / "no" / "unknown", the same three values
`Opportunity.sponsorship` already uses (see directory/classify.py's
`extract_sponsorship` and directory/models.py). `source` names where the
value came from: "posting" (the row's own `sponsorship` field), "firm" (no
posting statement, but `Firm.sponsors` answers for the row's region), or
"unknown" (neither side has said).

Firm-level "no" is deliberately NOT the same certainty as a posting's own
"no" — a firm's policy is a general fact about the firm, not a statement
about this specific role, and the product's rule is never to block on a
guess. Callers that gate on blocking behaviour (`_eligibility`) must treat
`source == "firm"` as a softer signal than `source == "posting"`; this module
only reports the fact, callers decide what to do with it.
"""

from __future__ import annotations

from django.db.models import Q

# Silence is stored two ways historically: the column defaults to "unknown",
# older rows carry "". One bucket everywhere a caller needs to test for it.
SILENT = ("", "unknown")


def _resolve_firm_fact(sponsors: dict | None, region: str) -> str:
    """"yes" / "no" / "unknown" from one firm's `sponsors` blob for one
    region. `sponsors` is `{"us": True, "hk": "unknown"}` — never a bare
    bool — so this always looks a REGION up, never tests the dict itself as
    truthy. Values are Python bool after the JSONField round-trip, but the
    "true"/"false" string forms are also accepted defensively (the seed data
    has carried both shapes historically)."""
    if not region:
        return "unknown"
    fact = (sponsors or {}).get(region)
    if fact is True or fact == "true":
        return "yes"
    if fact is False or fact == "false":
        return "no"
    return "unknown"


def effective_sponsorship(opp) -> tuple[str, str]:
    """The sponsorship answer for one opportunity, and where it came from.

    Returns (value, source):
      value  — "yes" / "no" / "unknown"
      source — "posting" / "firm" / "unknown"

    The posting's own `sponsorship` field wins outright. Only when it is
    silent does a firm-level answer for the posting's own region count, and
    only when the posting actually HAS a region — a firm that sponsors in HK
    but not the US must never answer for a role whose own market is unknown
    (the same guard `_sponsorship_tag` already enforced; see its history for
    the ~1,223 open rows with a blank region this protects)."""
    stated = (opp.sponsorship or "unknown").lower()
    if stated in ("yes", "no"):
        return stated, "posting"
    firm_value = _resolve_firm_fact(opp.firm.sponsors, opp.region or "")
    if firm_value in ("yes", "no"):
        return firm_value, "firm"
    return "unknown", "unknown"


def firm_policy_map() -> dict[tuple[int, str], str]:
    """Every (firm_id, region) pair with a stated firm policy, as "yes"/"no".

    A bulk companion to `effective_sponsorship` for call sites that need the
    answer for many rows at once (the feed filter, the facet counts, the
    onboarding preview's grouped query) without an N+1 firm lookup per row.
    Built from `Firm.sponsors` alone — the same data `effective_sponsorship`
    reads one row at a time — so a caller using this map and a caller using
    `effective_sponsorship` can never compute two different answers for the
    same (firm, region).

    Bounded and cheap: only firms carrying any policy data at all are
    scanned (58 on live data), and the result is a few hundred pairs at
    most — safe to build once per request.
    """
    from directory.models import Firm

    out: dict[tuple[int, str], str] = {}
    for firm_id, sponsors in Firm.objects.exclude(sponsors={}).values_list("id", "sponsors"):
        for region in sponsors or {}:
            value = _resolve_firm_fact(sponsors, region)
            if value in ("yes", "no"):
                out[(firm_id, region)] = value
    return out


def firm_policy_q(value: str, policy: dict[tuple[int, str], str] | None = None) -> Q:
    """A Q object matching opportunities whose FIRM (not posting) states
    `value` ("yes"/"no") for the opportunity's own region.

    Pass a pre-built `policy` (from `firm_policy_map()`) when the caller
    already has one, to avoid re-querying `Firm` for every value checked."""
    policy = policy if policy is not None else firm_policy_map()
    pairs = [(fid, region) for (fid, region), v in policy.items() if v == value]
    if not pairs:
        return Q(pk__in=[])
    q = Q()
    for fid, region in pairs:
        q |= Q(firm_id=fid, region=region)
    return q
