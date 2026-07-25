"""Import a recruiting-radar `targets.yaml` into Coverage as one user's firm list.

Run: `manage.py shell < scripts/import_targets.py` (mirrors scripts/demo_seed.py).

WHAT THIS DOES
--------------
The founder's single-user system defines its target universe in
`recruiting-radar/targets.yaml` — the list "coverage" is measured against. That
file is the closest thing to a ground-truth firm list this project has, so it
seeds a real (non-demo) user's `user_firms` rows rather than being retyped.

Mapping, deliberately narrow:
  targets.yaml `priority` (1 must-cover / 2 should / 3 nice)  ->  UserFirm.tier
  targets.yaml `name` + `aliases`                             ->  Firm lookup
  targets.yaml `tracks` / `regions`                           ->  Firm creation only

Firms already in the shared `firms` table are matched, never overwritten: the
shared zone is owned by the scrape worker (directory/models.py docstring), and a
personal target list has no business editing a firm's canonical tracks/regions.
Only genuinely absent firms are created, with `status="target-only"` so it is
queryable which rows came from here rather than from a connector.

A created firm with no board in `directory/boards.py` produces no opportunities,
so it never appears in the public feed — it exists purely so the user can hold a
tier and hang contacts off it. That is why importing the corp-strat targets does
not reverse the 2026-07-23 decision to cut tech from the app's scope: that
decision is enforced by the board catalog, not by the firms table.

Idempotent: re-running updates tiers in place and creates nothing twice.
"""
from __future__ import annotations

import os
import re
import sys

from django.contrib.auth import get_user_model
from django.utils.text import slugify

from crm.models import UserFirm
from directory.models import Firm

TARGETS_PATH = os.environ.get(
    "COVERAGE_TARGETS_PATH",
    "/Users/zhujimmy/Claude/Projects/Recruitment Opportunities/recruiting-radar/targets.yaml",
)
OWNER_EMAIL = os.environ.get("COVERAGE_TARGETS_OWNER", "jimmy@coverage.local")

# targets.yaml is machine-parsed by its own dashboard with a line regex and the
# file header mandates one target per line in strict flow style, so a regex is
# the format's contract here, not a shortcut around a YAML parser. `excludes:`
# is optional and always follows `aliases:`, so it is simply not captured.
LINE = re.compile(
    r"^\s*-\s*\{name:\s*(?P<name>[^,]+),\s*tracks:\s*\[(?P<tracks>[^\]]*)\],\s*"
    r"regions:\s*\[(?P<regions>[^\]]*)\],\s*priority:\s*(?P<priority>\d+),\s*"
    r"aliases:\s*\[(?P<aliases>[^\]]*)\]"
)


def _split(raw: str) -> list[str]:
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def parse_targets(path: str) -> list[dict]:
    targets = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = LINE.match(line)
            if not match:
                continue
            group = match.groupdict()
            targets.append(
                {
                    "name": group["name"].strip(),
                    "tracks": _split(group["tracks"]),
                    "regions": _split(group["regions"]),
                    "priority": int(group["priority"]),
                    "aliases": _split(group["aliases"]),
                }
            )
    return targets


def _normalise(value: str) -> str:
    """Fold the spelling differences that separate the two systems' firm names
    (``Rothschild & Co`` vs ``Rothschild and Co``, ``J.P. Morgan`` vs ``JP
    Morgan``) so an alias list does not have to enumerate punctuation."""
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("&", "and"))


def resolve_firm(target: dict, index: dict[str, Firm]) -> tuple[Firm, bool]:
    for candidate in [target["name"], *target["aliases"]]:
        firm = index.get(_normalise(candidate))
        if firm is not None:
            return firm, False
    firm = Firm.objects.create(
        slug=slugify(target["name"])[:128],
        name=target["name"],
        regions=target["regions"],
        tracks=target["tracks"],
        status="target-only",
    )
    index[_normalise(firm.name)] = firm
    return firm, True


def main() -> int:
    User = get_user_model()
    try:
        owner = User.objects.get(email=OWNER_EMAIL)
    except User.DoesNotExist:
        print(f"✗ no user {OWNER_EMAIL} — create the account first")
        return 1

    targets = parse_targets(TARGETS_PATH)
    if not targets:
        print(f"✗ parsed 0 targets from {TARGETS_PATH}")
        return 1

    index = {_normalise(f.name): f for f in Firm.objects.exclude(slug__startswith="zdemo")}
    created_firms, linked, retiered = [], 0, 0

    for target in targets:
        firm, was_created = resolve_firm(target, index)
        if was_created:
            created_firms.append(firm)
        # Read through `for_user`, write by instantiating the row with an
        # explicit `user=` — the two patterns tenancy.py sanctions. Every
        # manager method routes through the guard, so `objects.get_or_create`
        # and `objects.create` both raise TenantScopeError; constructing the
        # model directly keeps the write scoped by construction and avoids
        # reaching for the `all_objects` escape hatch to do an ordinary,
        # single-tenant insert.
        link = UserFirm.objects.for_user(owner).filter(firm=firm).first()
        if link is None:
            UserFirm(user=owner, firm=firm, tier=target["priority"]).save()
            linked += 1
        elif link.tier != target["priority"]:
            link.tier = target["priority"]
            link.save(update_fields=["tier"])
            retiered += 1

    print(f"targets parsed     : {len(targets)}")
    print(f"firms created      : {len(created_firms)}")
    for firm in created_firms:
        print(f"    + {firm.slug:24s} {firm.name}")
    print(f"user_firms linked  : {linked}")
    print(f"user_firms retiered: {retiered}")
    by_tier = {
        tier: UserFirm.objects.for_user(owner).filter(tier=tier).count() for tier in (1, 2, 3)
    }
    print(f"{OWNER_EMAIL} now tracks {sum(by_tier.values())} firms  "
          f"(tier1={by_tier[1]} tier2={by_tier[2]} tier3={by_tier[3]})")
    return 0


sys.exit(main())
