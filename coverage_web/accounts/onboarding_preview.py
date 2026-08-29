"""The live preview beside the onboarding wizard — "Feed takes shape".

Every number and every row on that panel is a query against the real
`opportunities` / `firms` tables, filtered by what the student has answered
so far. Nothing here is illustrative, nothing is a placeholder, and there is
no fallback that shows stale rows when a selection matches nothing: an empty
answer renders an empty state that says so. The landing page sells this
product on "Honest Dates"; a preview that invented a count to look busy
during setup would be the single worst thing to ship on this surface.

Two design rules the rest of this module exists to keep:

1. **The population is the feed's population.** `_campus()` is exactly the
   scope `directory.views` defaults to and `core.views.pricing` counts —
   `status="open"` over the three campus buckets. If it drifted, the preview
   would be promising a feed that does not exist.

2. **Cheap, always.** The Opportunities feed is this app's slowest page, and
   the panel re-queries every time a chip is toggled. So each builder is a
   `.count()`/`GROUP BY` plus at most one `[:4]` slice with the firm
   pre-joined — never `directory.views`' feed pipeline, which annotates,
   de-dupes and scores. Measured costs are in the module tests.

Tenancy: `Opportunity` and `Firm` are shared-zone (no `user_id`; see
directory/models.py's header) and are queried unscoped on purpose. Every
private-zone read here — `UserFirm`, `Contact` — goes through
`.for_user(user)`, which is the only way `PrivateModel.objects` yields a
queryset at all (coverage_web/tenancy.py).
"""

from __future__ import annotations

from django.db.models import Count, F, Q

from crm.models import Contact, UserFirm
from directory.classify import (
    REGION_LABELS,
    TARGET_BUCKETS,
    TRACK_LABELS,
    TRACKED_REGIONS,
)
from directory.models import Firm, Opportunity

# How many real roles the profile panel shows under its count. Four is the
# most that fits the panel without scrolling at 1280px, and the slice is the
# only row-fetching query on the whole step.
SAMPLE_LIMIT = 4


def _campus():
    """The live campus set — the Opportunities feed's own default scope.

    Deliberately the same two-clause filter as `core.views.pricing`'s
    `campus`, hitting `idx_opp_status_bucket`. Not a helper imported from
    `directory.views`: that module's `_apply_filters` carries eight more
    filters and a firm join this panel must never pay for.
    """
    return Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)


# ---------------------------------------------------------------------------
# What the student has said so far
# ---------------------------------------------------------------------------
def read_answers(request, user, step: str) -> dict:
    """The selections the preview filters on.

    Live from the form for the step the student is ON, and from the saved
    user for every other dimension — because the other steps' forms are not
    on the page to read. Getting this wrong is not cosmetic: an earlier cut
    of this function treated `?live=1` as global, so on the WORK step (whose
    form carries no region or track inputs) the profile filters came back
    empty and the panel counted the entire directory under a caption reading
    "roles matching your profile". Correct counts, false sentence — the exact
    failure this panel exists not to have. `live` is per-step now, and each
    dimension names the step whose form actually contains it.

    `?live=1` has to be an explicit marker rather than inferred from the
    presence of parameters: an unchecked chipset posts NO parameter at all,
    so "the student just cleared every region" and "this is the first server
    render" are the same absent key. Without the marker the panel would
    silently fall back to the saved profile the instant somebody unticked
    their last chip — showing a count for an answer they had just deleted.
    """
    live = request.GET.get("live") == "1"

    if live and step == "profile":
        regions = [r for r in request.GET.getlist("regions") if r in TRACKED_REGIONS]
        tracks = [t for t in request.GET.getlist("tracks") if t in TRACK_LABELS]
        raw_year = (request.GET.get("class_year") or "").strip()
        class_year = int(raw_year) if raw_year.isdigit() else None
    else:
        regions = [r for r in (getattr(user, "regions", None) or []) if r in TRACKED_REGIONS]
        tracks = [t for t in (getattr(user, "tracks", None) or []) if t in TRACK_LABELS]
        class_year = getattr(user, "class_year", None)

    if live and step == "work_auth":
        work_auth = {
            code: request.GET.get(f"work_auth_{code}")
            for code in TRACKED_REGIONS
            if request.GET.get(f"work_auth_{code}")
        }
    else:
        work_auth = dict(getattr(user, "work_authorization", None) or {})

    # Only the firms step needs the picked set, and reading it is a
    # tenant-scoped query — so it is not paid for on the three steps that
    # would throw it away.
    if step != "firms":
        firm_ids = []
    elif live:
        firm_ids = [int(f) for f in request.GET.getlist("firms") if f.isdigit()]
    else:
        firm_ids = list(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))

    return {
        "live": live,
        "regions": regions,
        "tracks": tracks,
        "class_year": class_year,
        "work_auth": work_auth,
        "firm_ids": firm_ids,
    }


def _matching(answers):
    """The campus set narrowed by everything the student has actually stated.

    Three filters, and each one is the SAME rule the feed applies, so the
    number here is a number the feed will reproduce:

    * region — `directory.views._apply_region_filter` matches one market at a
      time; a chipset is several, so this is the `IN` of them.
    * track — the feed's `firm__tracks__contains=[track]`, widened to
      `overlap` for the same reason: several chips mean "any of these".
    * class year — the decidable half of `directory.views._eligibility`'s
      title-stated branch. A posting that NAMES a graduating class other than
      yours is a blocking verdict on the feed, so it is out of this count
      too. Blank stays: "the posting didn't say" is not "the posting said
      no", and blank is 2,429 of the 2,435 live rows. The prose graduation
      windows in `raw.facts.grad` are deliberately NOT read here — they are a
      JSON walk per row, which is the feed pipeline this panel exists to
      avoid, and the footer never claims they were.
    """
    qs = _campus()
    if answers["regions"]:
        qs = qs.filter(region__in=answers["regions"])
    if answers["tracks"]:
        qs = qs.filter(firm__tracks__overlap=answers["tracks"])
    if answers["class_year"]:
        qs = qs.filter(Q(class_year="") | Q(class_year=str(answers["class_year"])))
    return qs


def _narrowed_by(answers) -> list[str]:
    """The dimensions that ACTUALLY narrowed the set, for the footer.

    Built from the answers rather than typed into the template, so the line
    can never name a filter the query did not apply — which is the exact way
    a truthful count acquires an untruthful caption.
    """
    dims = []
    if answers["regions"]:
        dims.append("region")
    if answers["tracks"]:
        dims.append("track")
    if answers["class_year"]:
        dims.append("class year")
    return dims


def _phrase(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# Step 1 — Profile: the feed, narrowing
# ---------------------------------------------------------------------------
def profile_preview(user, answers) -> dict:
    qs = _matching(answers)
    match_count = qs.count()
    total = _campus().count()

    rows = []
    if match_count:
        # Local import: `directory.views` imports plenty and this module is
        # loaded by `accounts.urls` at startup.
        from directory.views import _eligibility, _eligibility_profile

        profile = _eligibility_profile(user)
        # `deadline` ascending with nulls last, then newest — the partial
        # index `idx_opp_open_campus` is (bucket, deadline, first_seen) over
        # exactly this scope, so the sort is the index's own order. The header
        # above these rows says "closing soonest", which is what this is.
        sample = (
            qs.select_related("firm")
            .order_by(F("deadline").asc(nulls_last=True), "-first_seen")[:SAMPLE_LIMIT]
        )
        for o in sample:
            verdict = _eligibility(o, profile)
            rows.append({
                "firm": o.firm.name,
                "title": o.title,
                "region": REGION_LABELS.get(o.region, ""),
                # The firm's tracks, labelled, capped at two — a firm carrying
                # five would push the title off the row.
                "tracks": [TRACK_LABELS[t] for t in (o.firm.tracks or [])
                           if t in TRACK_LABELS][:2],
                "deadline": o.deadline,
                # None on most rows, and that is the honest common case: a
                # verdict needs BOTH sides to have stated something.
                "verdict": verdict,
            })

    dims = _narrowed_by(answers)
    return {
        "kind": "profile",
        "count": match_count,
        "total": total,
        "rows": rows,
        "dims": _phrase(dims),
        # Which chip to suggest when nothing matches. Naming the missing
        # answer beats "no results": the student can act on it.
        "suggest": ("a region" if not answers["regions"]
                    else "another region or track"),
    }


# ---------------------------------------------------------------------------
# Step 2 — Work: who actually answers the sponsorship question
# ---------------------------------------------------------------------------
# `Opportunity.sponsorship` is tri-state and the third state is the honest
# majority (2,304 of 2,435 live campus rows say nothing). Blank and "unknown"
# are the same fact stored two ways — the column defaults to "unknown", older
# rows carry "" — so they are one bucket here exactly as they are in
# `directory.views._SPONSOR_SILENT`.
_SILENT = ("", "unknown")


def work_preview(user, answers) -> dict:
    """How many of the student's matching roles answer the visa question.

    Four bars, not three, per docs/founder-decisions-2026-08-20.md,
    Decision 3: a posting saying yes, a posting saying no, a firm's stated
    per-region policy where the posting itself said nothing, and genuine
    silence. `directory.sponsorship.effective_sponsorship` is the one place
    that precedence (posting beats firm policy beats unknown) is decided —
    the feed's sponsorship filter and `_eligibility`'s visa verdict read the
    same rule, so this panel can't quote a number those surfaces disagree
    with.

    One GROUP BY over (region, sponsorship, firm) carries everything this
    panel says: the four totals AND the per-region breakdown the "ruled out"
    line needs. Firm joins into the grouping only to key the small firm-policy
    map built once per call — still a couple of dozen-to-low-hundreds tuples
    for any profile-narrowed set, not a per-row query.

    The framing is deliberately unflattering. 13 roles in the whole live set
    SAY they sponsor. Rounding that up into a reassuring sentence would be
    the lie this panel exists not to tell, so the "not stated" bar is drawn
    at its real, dominant width and captioned as unanswered rather than as
    open. A firm's policy is real information and earns its own bar — but it
    is not a posting's own word, so it never merges into "yes"/"no" here.
    """
    from directory.sponsorship import firm_policy_map

    qs = _matching(answers)
    grid = list(
        qs.values("region", "sponsorship", "firm_id").annotate(n=Count("id"))
    )
    policy = firm_policy_map()

    yes = sum(g["n"] for g in grid if g["sponsorship"] == "yes")
    no = sum(g["n"] for g in grid if g["sponsorship"] == "no")
    firm_known = sum(
        g["n"] for g in grid
        if g["sponsorship"] in _SILENT
        and policy.get((g["firm_id"], g["region"])) in ("yes", "no")
    )
    silent = sum(
        g["n"] for g in grid
        if g["sponsorship"] in _SILENT
        and policy.get((g["firm_id"], g["region"])) not in ("yes", "no")
    )
    total = yes + no + firm_known + silent

    # The regions where this student has said, in the matrix they are looking
    # at, that they need a visa. That plus a POSTING saying "no" is
    # `_eligibility`'s `visa_out` — the one blocking verdict this step can
    # produce. A firm-level "no" is deliberately excluded from this count:
    # `_eligibility` treats it as a non-blocking warning, never a wall, and
    # this panel's "ruled out" line must mean the same thing `_eligibility`
    # means by it.
    needs = [code for code, value in answers["work_auth"].items()
             if value == "sponsorship"]
    blocked = sum(g["n"] for g in grid
                  if g["sponsorship"] == "no" and g["region"] in needs)
    return {
        "kind": "work",
        "count": total,
        "yes": yes,
        "no": no,
        "firm_known": firm_known,
        "silent": silent,
        # Widths as integer percents so the template does no arithmetic.
        "yes_pct": round(100 * yes / total) if total else 0,
        "no_pct": round(100 * no / total) if total else 0,
        "firm_known_pct": round(100 * firm_known / total) if total else 0,
        "silent_pct": round(100 * silent / total) if total else 0,
        "blocked": blocked,
        "needs_labels": _phrase([REGION_LABELS[c] for c in needs if c in REGION_LABELS]),
        "answered": yes + no + firm_known,
    }


# ---------------------------------------------------------------------------
# Step 3 — Firms: the picked set, surfacing
# ---------------------------------------------------------------------------
def firms_preview(user, answers) -> dict:
    """Each picked firm and how many live campus roles it has RIGHT NOW.

    One query: the count is a filtered aggregate on the join rather than a
    per-firm `.count()` in a loop, so ticking a twentieth firm costs the same
    as ticking the first.
    """
    ids = answers["firm_ids"]
    firms = []
    if ids:
        firms = [
            {"name": f.name, "n": f.n, "regions": [REGION_LABELS.get(r, r)
                                                   for r in (f.regions or [])][:2]}
            for f in Firm.objects.filter(id__in=ids)
            .annotate(n=Count("opportunities", filter=Q(
                opportunities__status="open",
                opportunities__bucket__in=TARGET_BUCKETS,
            )))
            .order_by("-n", "name")[:8]
        ]
    return {
        "kind": "firms",
        "picked": len(ids),
        "firms": firms,
        "roles": sum(f["n"] for f in firms),
        # The list is capped at eight rows; when it is capped the panel has to
        # say so rather than let a student read eight as their whole set.
        "more": max(0, len(ids) - len(firms)),
    }


# ---------------------------------------------------------------------------
# Step 4 — Import: the Network board, as it stands
# ---------------------------------------------------------------------------
def import_preview(user, answers) -> dict:
    """The board this wizard has been building, with its real contents.

    For a brand-new account that is an empty board and the panel says so —
    which is the point of showing it. Both reads are tenant-scoped; neither
    model here is shared-zone.
    """
    return {
        "kind": "import",
        "contacts": Contact.objects.for_user(user).filter(archived=False).count(),
        "firms": UserFirm.objects.for_user(user).count(),
    }


BUILDERS = {
    "profile": profile_preview,
    "work_auth": work_preview,
    "firms": firms_preview,
    "import": import_preview,
}


def build(step: str, request, user) -> dict:
    """The panel's context for one step. Unknown steps fall back to the first
    one, matching the step machine's own `if step not in ONBOARDING_STEPS`
    posture — a stale bookmark gets the profile panel, never a traceback."""
    if step not in BUILDERS:
        step = "profile"
    builder = BUILDERS[step]
    answers = read_answers(request, user, step)
    ctx = builder(user, answers)
    ctx["answers"] = answers
    return ctx
