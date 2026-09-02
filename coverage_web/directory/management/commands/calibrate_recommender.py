"""Score the roles a student ACTED on, and print what each weight contributed.

WHY THIS EXISTS. `directory/recommend.py` carries sixteen weights and, until
this command, not one of them was backed by a measured outcome
(`audit-personalization-opportunities.md §Q8`). Four were justified by
arithmetic about the other weights and one (`MAX_PER_FIRM`) by a single
browser observation. P6 says every weight ships with what it encodes, why that
magnitude, and what would change it; this is the "what would change it" half
made runnable, so the next person to touch a weight starts from a number
instead of from a feeling.

WHAT IT MEASURES. Every `UserOpportunity` the student has saved, applied to or
dismissed is a labelled example: they looked at the board and said yes or no.
The command scores each of those roles through `recommend.score_candidate`
against the student's real profile, prints the per-axis contribution, and
prints the rank that role would have held among the whole open campus set on
the same day. If the weights are right, the saved and applied rows rank high
and the dismissed rows rank low.

WHAT IT CANNOT MEASURE, and says so rather than implying otherwise. The
founder has 18 `UserOpportunity` rows. Eighteen labelled examples cannot
separate sixteen weights: most axes will be constant across the whole sample
(every row is at a tiered firm, every row is in his tracks), and an axis that
never varies contributes nothing a fit could learn from. So the output ends
with an explicit per-weight verdict, and for most weights that verdict is
"no measured justification; sample too small". A number that says it is
unjustified is worth more than a number that quietly looks justified.

READ-ONLY, STRUCTURALLY. `--dry-run` is the only mode and it is the default;
there is no `--apply` to forget. Nothing here writes, and a test asserts zero
write queries.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.models import Contact, UserFirm
from directory.classify import TARGET_BUCKETS
from directory.dupes import fold_duplicates
from directory.models import Opportunity
from directory.recommend import (
    Candidate,
    Profile,
    _AXES,
    score_candidate,
)

# The axis functions in `_AXES`, named for the report. Derived from the tuple
# itself rather than typed out, so an axis added to the scorer turns up here
# on its own instead of being silently omitted from its own calibration.
AXIS_NAMES = tuple(a.__name__.lstrip("_") for a in _AXES)

# What the student's action means. `applied_status` is free text on
# `UserOpportunity` and blank means "saved, not applied"; `dismissed` is the
# explicit no. Saved and applied are both a yes and are kept apart anyway,
# because "worth a second look" and "I sent it" are different strengths of
# signal and a calibration that merged them would throw away the stronger one.
LABEL_APPLIED = "applied"
LABEL_SAVED = "saved"
LABEL_DISMISSED = "dismissed"


def _label(row: UserOpportunity) -> str:
    if row.dismissed:
        return LABEL_DISMISSED
    if (row.applied_status or "").strip():
        return LABEL_APPLIED
    return LABEL_SAVED


class Command(BaseCommand):
    help = (
        "Read-only: score every role a user saved, applied to or dismissed, "
        "and print the per-axis contributions and the rank each would have had."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True,
                            help="The account to calibrate against.")
        parser.add_argument(
            "--dry-run", action="store_true", default=True,
            help="The only mode. Present so the invocation says so out loud.",
        )

    def handle(self, *args, **options):
        from accounts.models import User

        email = options["email"]
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stderr.write(f"No account for {email!r}.")
            return

        today = timezone.localdate()
        tier_by_firm = {
            uf.firm_id: uf.tier
            for uf in UserFirm.objects.for_user(user) if uf.firm_id
        }
        # The same warm-per-firm collapse the feed builds before it scores, so
        # the numbers this command prints are the numbers the page produced.
        warm_by_firm: dict[int, str] = {}
        for fid, warmth in (
            Contact.objects.for_user(user)
            .filter(archived=False, firm__isnull=False,
                    warmth__in=("replied", "chatted", "advocate"))
            .values_list("firm_id", "warmth")
        ):
            rank = "warm" if warmth in ("chatted", "advocate") else "replied"
            if warm_by_firm.get(fid) != "warm":
                warm_by_firm[fid] = rank
        profile = Profile.from_user(user, tier_by_firm, warm_firms=warm_by_firm)

        acted = list(
            UserOpportunity.objects.for_user(user).select_related(
                "opportunity", "opportunity__firm")
        )
        self.stdout.write(f"Account: {email} (id {user.id})")
        self.stdout.write(f"Labelled examples (UserOpportunity rows): {len(acted)}")
        if not acted:
            self.stdout.write("Nothing to calibrate against.")
            return
        self.stdout.write(f"By label: {dict(Counter(_label(r) for r in acted))}")
        self.stdout.write("")

        # The whole open campus board, folded exactly as the feed folds it, so
        # "the rank this role would have had" is a rank among what the student
        # actually saw and not among raw rows.
        board = fold_duplicates(
            list(Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
                 .select_related("firm"))
        )[0]
        scored = sorted(
            ((score_candidate(profile, Candidate.from_opportunity(o))[0], o.id)
             for o in board),
            key=lambda pair: -pair[0],
        )
        rank_by_id = {oid: i + 1 for i, (_s, oid) in enumerate(scored)}
        self.stdout.write(f"Board scored: {len(board)} open campus roles")
        self.stdout.write("")

        per_label_axis: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: defaultdict(list))
        rows = []
        for row in acted:
            o = row.opportunity
            candidate = Candidate.from_opportunity(o)
            total, _reasons = score_candidate(profile, candidate)
            contributions = {}
            for axis, name in zip(_AXES, AXIS_NAMES):
                points, _why = axis(profile, candidate)
                contributions[name] = points
                per_label_axis[_label(row)][name].append(points)
            rows.append((total, rank_by_id.get(o.id), _label(row), o, contributions))

        rows.sort(key=lambda r: -r[0])
        for total, rank, label, o, contributions in rows:
            where = f"#{rank} of {len(board)}" if rank else "not on the open board"
            axes = "  ".join(f"{n}={contributions[n]:+d}" for n in AXIS_NAMES)
            self.stdout.write(
                f"[{label:9}] score {total:4d}  {where:18}  "
                f"{o.firm.name} — {o.title[:52]}"
            )
            self.stdout.write(f"             {axes}")
        self.stdout.write("")

        self.stdout.write("Per-axis spread by label (min/median/max):")
        for label in (LABEL_APPLIED, LABEL_SAVED, LABEL_DISMISSED):
            if label not in per_label_axis:
                continue
            for name in AXIS_NAMES:
                values = sorted(per_label_axis[label][name])
                if not values:
                    continue
                mid = values[len(values) // 2]
                self.stdout.write(
                    f"  {label:9} {name:12} {values[0]:+4d} / {mid:+4d} / "
                    f"{values[-1]:+4d}   (n={len(values)})"
                )
        self.stdout.write("")

        # THE VERDICT PER AXIS. An axis whose contribution never varies across
        # the sample cannot be calibrated by it — that is the whole finding on
        # a sample this size, and saying it plainly is the point of the
        # command.
        self.stdout.write("Verdict:")
        for name in AXIS_NAMES:
            all_values = [
                v for label in per_label_axis
                for v in per_label_axis[label][name]
            ]
            if len(set(all_values)) <= 1:
                self.stdout.write(
                    f"  {name:12} no measured justification; constant across "
                    f"the sample (n={len(all_values)})"
                )
            else:
                yes = [
                    v for label in (LABEL_APPLIED, LABEL_SAVED)
                    for v in per_label_axis.get(label, {}).get(name, [])
                ]
                no = per_label_axis.get(LABEL_DISMISSED, {}).get(name, [])
                if not yes or not no:
                    self.stdout.write(
                        f"  {name:12} varies, but only one side of the label "
                        f"is present; not separable (n={len(all_values)})"
                    )
                    continue
                self.stdout.write(
                    f"  {name:12} acted-on mean {sum(yes) / len(yes):+.1f} vs "
                    f"dismissed mean {sum(no) / len(no):+.1f} "
                    f"(n={len(yes)} vs {len(no)})"
                )
        self.stdout.write("")
        self.stdout.write(
            "Read-only. Nothing was written. A mean difference on a sample "
            "this size is a direction, not a calibration: treat any weight "
            "whose line says 'no measured justification' as still unjustified."
        )
