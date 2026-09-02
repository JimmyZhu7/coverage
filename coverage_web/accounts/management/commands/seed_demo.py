"""seed_demo — create (or confirm) the demo student account, the safe place
to poke at the CRM by hand.

    python manage.py seed_demo

WHY THIS COMMAND EXISTS
------------------------
The logic below used to live only in `scripts/demo_seed.py`, runnable as
`manage.py shell < scripts/demo_seed.py` — a shape that quietly discourages
using it. Piping a script into a REPL is slower to reach for, and slower to
remember the invocation for, than typing a command name — and `manage.py
shell` is also the exact tool every documented case of fixture leakage in
this repo has come from (see `core/management/commands/audit_fixtures.py`'s
docstring for the receipts: a blank-slug Firm, four "ZZZ Smoke Test" contacts
in the founder's own CRM, a "Verify J.P. Morgan" firm rendering on the
founder's Today page). An agent already sitting in `manage.py shell` to poke
at something is one `Firm.objects.create(...)` away from writing straight
into the shared dev database — reaching for a *command* whose whole purpose
is "give me a safe account to test against" is meant to be the easier path,
not the harder one.

`demo@coverage.local` (password `demo1234`, see docs/see-it-locally.md) is
tenant-isolated like any other account — `UserFirm`/`Contact`/`Touch` rows
created here are `PrivateModel` rows scoped to this one user, so a feature
check against it never touches another account's data, and there is no
scenario where deleting or resetting this user's own rows harms anyone.
Prefer this account for any manual, browser-driven check of a CRM feature;
reach for `directory.Firm` / a new `accounts.User` only when the thing under
test is the shared directory itself, and even then prefer pytest's isolated
test database (`uv run pytest`, or `pytest coverage_web/<app> -q` per
CLAUDE.md-style repo convention) over the live one.

Idempotent — running this against an already-seeded database is a no-op and
says so. It does not accept a --reset flag on purpose: resetting would
delete this account's rows right before someone might be mid-demo with it,
and the safe way to get a clean slate is `Contact.objects.for_user(demo)
.delete()` by hand, which is a deliberate, visible action rather than a
button anyone can hit by accident.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm import services
from crm.models import Contact, UserFirm
from directory.models import Firm, FirmDate
from directory.recommend import CYCLE_LABELS, INTERNSHIP, cycle_choices

User = get_user_model()

EMAIL, PASSWORD = "demo@coverage.local", "demo1234"


def demo_cycle(today=None) -> str:
    """The demo student's target cycle, taken from the live dropdown.

    THE DEFECT THIS EXISTS FOR. This file wrote the literal `"sa2028_ib"`,
    which `directory.recommend.parse_target_cycle` has never recognised —
    the vocabulary is `"2027 Summer Internship"`, built by `cycle_choices()`
    from `CYCLE_LABELS`. An unparsed cycle costs the student nothing on
    purpose (that function returns None rather than guessing), so the cycle
    bonus and the level gate were simply OFF on the account every demo the
    founder gives runs on: measured 2026-09-01, 0 of the demo's picks
    carried a `W_CYCLE` reason, and no error anywhere said why.

    Read out of `cycle_choices()` rather than spelled again here, so the
    seed cannot drift from the dropdown the way the literal did.

    NEXT YEAR'S SUMMER, not the furthest one offered. The dropdown runs
    `base_year` to `base_year + 2`, and the intake actually in market is
    always the next one: measured on the live board 2026-09-02, 1,103 open
    campus rows carry a 2027 cohort against 4 carrying 2028. A demo account
    pointed two summers out is a demo account whose cycle bonus fires on
    nothing, which is the same silent zero the literal produced, arriving by
    a better-looking route.
    """
    summers = [
        value for value, _ in cycle_choices(base_year=today.year if today else None)
        if value.endswith(CYCLE_LABELS[INTERNSHIP])
    ]
    # `summers` is [this year, next year, the year after] in display order.
    return summers[1]


class Command(BaseCommand):
    help = "Create (or confirm) the demo student account — a safe place to test CRM features by hand."

    def handle(self, *args, **opts):
        if User.objects.filter(email=EMAIL).exists():
            demo = User.objects.get(email=EMAIL)
            self.stdout.write(self.style.SUCCESS(
                f"demo student already set up — {EMAIL} / {PASSWORD} "
                f"({Contact.objects.for_user(demo).count()} contacts). Nothing to do."))
            return

        now = timezone.now()
        cycle = demo_cycle(now.date())
        cycle_year = int(cycle.split()[0])
        demo = User.objects.create_user(email=EMAIL, password=PASSWORD)
        demo.name, demo.school = "Demo Student", "Demo University"
        # The penultimate summer, which is what a summer analyst intake IS:
        # a student graduating in 2028 does the 2027 internship. The class
        # year and the seeded firm date below are both derived from
        # `cycle_year` so the account cannot drift into recruiting for one
        # intake while graduating in a year that does not follow from it.
        demo.class_year = cycle_year + 1
        demo.target_cycles, demo.regions, demo.tracks = [cycle], ["us"], ["ib"]
        # ANSWERED FOR THE MARKET IT TARGETS. The live demo row carried
        # `{"hk": "citizen"}` against `regions=["us"]` — an answer about a
        # market this student does not recruit in, which leaves
        # `directory.views._eligibility` with nothing to say about the only
        # market they do. It is seeded here so it is part of the account's
        # definition rather than something somebody set by hand once.
        demo.work_authorization = {"us": "citizen"}
        demo.onboarded_at = now
        demo.save()

        firms = list(Firm.objects.order_by("name")[:3])
        while len(firms) < 3:  # safety if the directory wasn't seeded yet
            firms.append(Firm.objects.create(
                name=f"Demo Firm {len(firms) + 1}", slug=f"demo-firm-{len(firms) + 1}"))
        f1, f2, f3 = firms[0], firms[1], firms[2]
        for f, tier in ((f1, 1), (f2, 1), (f3, 2)):
            UserFirm.all_objects.get_or_create(
                user=demo, firm=f, defaults={"tier": tier, "status": "target"})

        # A confirmed close ~10 days out at f3 -> the weekly list shows a
        # re-ping. Tagged "seed:demo" so it renders as labeled sample data
        # rather than a real citation (directory/tests/test_firm_timeline.py
        # asserts this) and so audit_fixtures's FirmDate check excludes it
        # by name rather than by coincidence.
        FirmDate.objects.get_or_create(
            firm=f3, cycle=f"sa{cycle_year}", track="ib", region="us",
            event_kind="app_close",
            defaults={"date": now.date() + timedelta(days=10), "confidence": 1.0,
                      "source_url": "seed:demo"})

        def contact(name, email, firm, angle="", alum=False):
            return Contact.all_objects.create(
                user=demo, firm=firm, name=name, email=email,
                source="met at a US info session", angle=angle, school_affiliation=alum)

        # A story across the warmth ladder so scores + the weekly list have variety.
        maya = contact("Maya Chen", "maya@example.com", f1, "covered the semiconductor deal", alum=True)
        james = contact("James Okafor", "james@example.com", f1, "leads the FIG group")
        priya = contact("Priya Nair", "priya@example.com", f2, "USC alum, super responsive", alum=True)
        tom = contact("Tom Weiss", "tom@example.com", f2)
        contact("Sofia Reyes", "sofia@example.com", f3, "warm intro from a classmate")  # no touches -> first_outreach
        daniel = contact("Daniel Kim", "daniel@example.com", f3, "covers the account I'm targeting")

        def touch(c, *kinds):
            for k in kinds:
                services.log_touch(demo.id, c.id, k, "email")

        touch(maya, "outreach", "reply_received", "chat")  # chatted
        try:
            services.set_contact_state(demo.id, maya.id, warmth="advocate", note="offered to refer me")
        except Exception:
            pass  # advocate is a nice-to-have
        touch(james, "outreach", "reply_received", "chat")  # chat_done, no thank-you -> thank_you action
        touch(priya, "outreach", "reply_received")  # replied
        touch(tom, "outreach")  # cold / no_reply
        touch(daniel, "outreach", "reply_received")  # replied, at a firm closing soon -> re-ping

        self.stdout.write(self.style.SUCCESS(
            f"demo student ready: {EMAIL} / {PASSWORD} "
            f"({Contact.objects.for_user(demo).count()} contacts)"))
