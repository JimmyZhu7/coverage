"""capture_autopilot — decide a scan's pending cards unattended, for one tap.

    python manage.py capture_autopilot --email you@example.com --findings findings.json
    python manage.py capture_autopilot --email you@example.com --findings f.json --dry-run
    python manage.py capture_autopilot --email you@example.com --findings f.json \\
        --context counterevidence.json

THE STEP THIS IS. `capture_gmail` applies a findings batch and leaves the
unmatched remainder as pending `ContactProposal` cards — a queue the user
works by thumb. This command is the step after: `capture.autopilot` reads
every pending card, gets one grounded verdict per row (add it / needs you),
and stores the reviewed batch. Nothing in the CRM moves — the batch waits on
Today for the user's single tap ("Add all N"), which is `apply_run` and the
Limited Use posture in one gesture. See `capture/autopilot.py`'s module
docstring for the compliance decision in full; this command never applies.

--findings is the SAME file `capture_gmail` just applied — passed again so
counter-evidence in the batch (a bounce or bulk row about a proposed
address or its thread) reaches the verdict that needs it.

--context is the sidecar for counter-evidence the scan surfaced outside the
findings shape — departure auto-replies, out-of-office referrals, alternate
addresses. A JSON list of {"email" and/or "thread_id", "text": "<verbatim
sentence(s)>"}. Whatever the unusual-replies capture layer learns lands
here, and a verdict must quote it verbatim or it cannot act on it.

--dry-run prints the full decision table — every accept and escalation with
the quote it stands on — and writes NOTHING: no run, no decisions, no
credit debit. The model calls are real (that is the point: the table is the
acceptance test), so it still needs ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture import autopilot


class Command(BaseCommand):
    help = "Run the Autopilot decide pass over one user's pending cards."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--findings", default=None,
            help="The findings JSON the scan produced ('-' for stdin) — "
            "optional, but without it batch counter-evidence can't be seen.",
        )
        parser.add_argument(
            "--context", default=None,
            help="Sidecar JSON list of counter-evidence notes "
            '({"email"/"thread_id", "text"}).',
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--model", default=autopilot.DEFAULT_MODEL)

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(email=options["email"])
        except User.DoesNotExist:
            raise CommandError(f"No user with email {options['email']!r}.")

        findings = None
        source_label = ""
        if options["findings"]:
            if options["findings"] == "-":
                findings = json.load(sys.stdin)
                source_label = "stdin"
            else:
                with open(options["findings"]) as fh:
                    findings = json.load(fh)
                source_label = options["findings"].rsplit("/", 1)[-1]
            if not isinstance(findings, list):
                raise CommandError("Findings must be a JSON list.")

        context_notes = None
        if options["context"]:
            with open(options["context"]) as fh:
                context_notes = json.load(fh)
            if not isinstance(context_notes, list):
                raise CommandError("Context must be a JSON list.")

        report = autopilot.run_autopilot(
            user,
            findings=findings,
            context_notes=context_notes,
            dry_run=options["dry_run"],
            model=options["model"],
            source_label=source_label,
        )

        if report.reason == "unconfigured":
            self.stdout.write(self.style.WARNING(
                "Autopilot is dark — ANTHROPIC_API_KEY is not set. "
                "Nothing was decided and nothing was spent."
            ))
            return

        mode = "DRY RUN — nothing written" if report.dry_run else (
            "FAILED MID-DECIDE — run marked failed, nothing applied"
            if report.reason == "failed"
            else "reviewed — waiting for the one tap on Today"
        )
        self.stdout.write(f"Autopilot: {mode}")
        for line in report.lines:
            label = {
                "accept": "ACCEPT  ",
                "escalate": "NEEDS YOU",
                "skip": "skip    ",
                "defer": "defer   ",
            }.get(line.decision, line.decision)
            self.stdout.write(
                f"  {label}  {line.who}"
                + (f"  ({line.confidence:.2f})" if line.detected_by == "ai" else "")
            )
            if line.quote:
                self.stdout.write(f'            quote: "{line.quote}"')
            if line.reason:
                self.stdout.write(f"            {line.reason}")
        self.stdout.write(
            f"Decided {report.llm_calls} row(s) by model: "
            f"{report.count('accept')} accept, "
            f"{report.count('escalate')} need you, "
            f"{report.count('skip')} skipped, {report.count('defer')} deferred."
            + (
                f" Spent {report.credits_spent} credit(s)."
                if not report.dry_run else ""
            )
        )
