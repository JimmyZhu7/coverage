"""Private zone (docs/build-plan.md §2): `user_id` denormalized onto every
row, including `touches`. Every model here subclasses
`coverage_web.tenancy.PrivateModel` — see that module for the
tenant-scoped-manager contract.

Schema-compatibility note (critical — see the task brief and
coverage_domain/coverage_domain/pipeline.py's module docstring): `Contact`
and `Touch` below are read and written by pipeline.py's raw, unqualified
SQL (`SELECT ... FROM contacts`, `INSERT INTO touches (...)`). Their
`Meta.db_table` is pinned to the exact names pipeline.py expects
("contacts", "touches"), their `warmth`/`thread_state`/`channel`/`kind`/
`note`/`source` columns are plain, unconstrained CharField/TextField (no
Django `choices=`, no DB-level CHECK constraint) so the ported ratchet's
raw `UPDATE ... CASE` can never be rejected by application-level
validation it was never written to satisfy, and `user`/`contact` use
Django's default `<field>_id` column naming so `user_id` / `contact_id`
line up with pipeline.py's column references without any `db_column`
overrides.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from coverage_web.tenancy import PrivateModel
from directory.models import Firm


class UserFirm(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE)
    tier = models.SmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, blank=True, default="")

    class Meta(PrivateModel.Meta):
        db_table = "user_firms"
        constraints = [
            # Plan calls for PK(user_id, firm_id). A UniqueConstraint over
            # a surrogate auto `id` PK gives the identical "no duplicate
            # (user, firm) pair" guarantee without adopting Django 5.2's
            # very new CompositePrimaryKey — see report for the full
            # reasoning.
            models.UniqueConstraint(fields=["user", "firm"], name="uniq_user_firms_user_firm"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} × {self.firm_id}"


class Contact(PrivateModel):
    # The recruiting regions the product models. Deliberately NOT the same
    # vocabulary as `Firm.regions` (which also carries sg/eu): these two are
    # the only regions the cadence engine's deadline scoping and the
    # sponsorship rules actually reason about, so a contact only ever carries
    # one of them or blank ("unknown", the honest default).
    REGION_CHOICES = [("us", "United States"), ("hk", "Hong Kong")]
    REGION_VALUES = frozenset(value for value, _ in REGION_CHOICES)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    # Nullable with a firm_text fallback (§2's "Deliberate calls"):
    # students name firms outside the directory; capture must never block
    # on directory coverage.
    firm = models.ForeignKey(
        Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts"
    )
    firm_text = models.CharField(max_length=255, blank=True, default="")
    role = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    linkedin = models.URLField(max_length=512, blank=True, default="")
    # PROVENANCE ONLY — where this row came from ("manual", "import",
    # "capture", or a free-text campaign label on ported rows). It is NOT a
    # region field: `region` below is. See that field's comment.
    source = models.CharField(max_length=64, blank=True, default="")
    # Which recruiting region this person is being worked for. Explicit,
    # because the cadence engine scopes its pre-deadline re-ping by region and
    # used to infer that by substring-matching "hk" inside `source` — which
    # made every hand-added contact (source="manual") silently a US contact.
    # Blank means "unknown", and unknown is a real answer: the engine falls
    # back to matching either region rather than guessing.
    region = models.CharField(
        max_length=8, blank=True, default="", choices=REGION_CHOICES
    )
    # Plain, unconstrained — see module docstring. Values are managed
    # entirely by coverage_domain.pipeline's ratchet, not Django.
    warmth = models.CharField(max_length=32, default="cold")
    thread_state = models.CharField(max_length=32, default="no_reply")
    # PRIVATE — the user's own note ABOUT this person ("USC alum, super
    # responsive", "warm intro from a classmate"). It is shown on the contact
    # card and nowhere else. It must never be sent TO the contact: that is
    # what `opener` below is for. See crm.views._mailto.
    angle = models.TextField(blank=True, default="")
    # The draft body for an outbound email TO this person. Safe to put in a
    # mailto: URL — that is its whole purpose.
    opener = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    # AI-WRITTEN, and the only field on this row a model is ever allowed to
    # write. Two or three sentences on where this relationship stands,
    # regenerated on the student's say-so — see crm/ai_summary.py.
    #
    # Deliberately a THIRD field rather than an append to `notes` or `angle`:
    # those two are the student's own words about this person, and generated
    # prose landing in either is prose they could never afterwards tell from
    # something they wrote themselves. `ai_summary.py` reads both as context
    # and writes only here (`update_fields` names these two columns and
    # nothing else). Blank is the honest default — nothing generated yet, or
    # not enough history to say anything specific — and every surface that
    # renders this labels it as AI-drafted.
    ai_summary = models.TextField(blank=True, default="")
    # When the text above was written. Shown beside it, and the count of
    # touches logged since is what lets the page say plainly that a summary
    # has fallen behind the history it was written from. NULL means never
    # generated, which is exactly the state a blank `ai_summary` implies.
    ai_summary_generated_at = models.DateTimeField(null=True, blank=True)
    school_affiliation = models.BooleanField(default=False)
    # Is this person part of the RECRUITING PROCESS rather than someone you
    # network with? Campus recruiters, talent acquisition, HR coordinators,
    # program and event coordinators. They are real, worth tracking, and worth
    # answering — but "propose a 15-min chat" is the wrong ask to make of them,
    # and the queue used to make it. Reported by the founder 2026-08-22 on two
    # live rows: a "Manager, Talent Acquisition" whose mass programme invite
    # the capture pipeline read as a reply, and a national campus-recruiting
    # manager who had already made the introduction and handed him off.
    #
    # THREE-STATE ON PURPOSE. NULL means "nobody has said", and the queue then
    # reads `role` for one of a small set of unambiguous markers
    # (crm.relevance.is_recruiting_role). True/False is the user's own word and
    # always wins, in both directions: it is the override for the banker whose
    # title happens to contain a recruiting phrase AND for the recruiter whose
    # title says nothing at all. A two-state boolean could not express "not
    # answered yet" and would have had to default one way, which means
    # defaulting to a guess about every contact ever imported.
    recruiting_contact = models.BooleanField(null=True, blank=True, default=None)
    # Display facts ported from the founder's campaign.db (both optional).
    school = models.CharField(max_length=64, blank=True, default="")
    gender = models.CharField(max_length=16, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    archived = models.BooleanField(default=False)
    email_pattern_recorded = models.BooleanField(default=False)
    # UI-only dismissal from the Today queue (Snooze / Skip). Not a touch and
    # not part of the pipeline ratchet — set directly via the ORM. A contact
    # is hidden from the cadence queue until this passes.
    snoozed_until = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "contacts"
        ordering = ["-created"]

    def __str__(self) -> str:
        return self.name

    def default_region_from_firm(self) -> str:
        """The region this contact's firm implies, or "" when it implies
        nothing. Only an UNAMBIGUOUS firm answers: exactly one modeled region
        (us/hk) on the firm. A firm that recruits in both, in neither, or only
        in a region this product doesn't model yields "" — guessing there is
        exactly the bug this field exists to kill, and a blank keeps the
        cadence engine's conservative both-regions fallback."""
        if self.firm_id is None:
            return ""
        known = {
            (r or "").strip().lower()
            for r in (self.firm.regions or [])
        } & self.REGION_VALUES
        return next(iter(known)) if len(known) == 1 else ""

    def save(self, *args, **kwargs):
        # Default region from the firm, but only when the user hasn't set one:
        # an explicit region always wins, and a blank one stays blank unless
        # the firm makes it unambiguous.
        if not self.region:
            inferred = self.default_region_from_firm()
            if inferred:
                self.region = inferred
                # A partial save() must still persist the column we just
                # filled in, or the value silently vanishes.
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = {*update_fields, "region"}
        super().save(*args, **kwargs)


class Touch(PrivateModel):
    """Append-only — no UPDATE path exists in application code (§2). This
    is a design/process invariant enforced by convention (only
    `coverage_domain.pipeline.apply_touch` / `set_state`, via
    `crm.services`, ever write here), not by a DB trigger — kept
    deliberately simple per the task's "pragmatic, not over-engineered"
    guidance.
    """

    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("capture", "Capture"),
        ("import", "Import"),
        # Logged by the advisor page's agent loop on the student's say-so
        # (assistant/tools.py). Its own value rather than "manual" so a touch
        # a model wrote is permanently distinguishable from one the student
        # clicked — the same posture as "capture", and the only way a later
        # audit of "what did the assistant actually do to my CRM" can be a
        # query rather than a guess.
        ("assistant", "Assistant"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="touches")
    ts = models.DateTimeField()
    # Nullable: pipeline.py's set_state() manual-override audit touch
    # inserts channel=NULL (there is no channel for a state override).
    channel = models.CharField(max_length=32, null=True, blank=True)
    # Plain, unconstrained — see module docstring.
    kind = models.CharField(max_length=64)
    note = models.TextField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default="manual")

    class Meta(PrivateModel.Meta):
        db_table = "touches"
        ordering = ["-ts"]

    def __str__(self) -> str:
        return f"{self.kind} @ {self.ts:%Y-%m-%d %H:%M}"


class ChatDebrief(PrivateModel):
    """What a coffee chat actually taught you, captured once per chat.

    Before this, a `chat` touch recorded that a conversation happened and
    nothing about what was in it: the intro that was offered, the deadline
    that was mentioned, the read on whether this person would go to bat for
    you. All of it evaporated. This row is the structured landing place —
    deterministic fields, no model in the loop.

    Idempotency is a schema guarantee, not a convention: `UniqueConstraint`
    on (user, touch) means one debrief per chat touch, and the FK columns
    below (`intro_contact`, `intro_task`, `date_task`) double as
    already-done markers so a re-submitted form can never spawn a second
    referral contact or a second task. See `crm.debrief.record` for the
    first-write-wins rule each side effect follows.

    `dismissed` is the escape hatch: a debrief nobody wants to write must be
    dismissable, or the prompt becomes the wall of stale thank-yous the
    cadence engine already had to grow an expiry to fix.
    """

    ADVOCATE_ANSWERS = [("yes", "Yes"), ("no", "No"), ("unsure", "Unsure")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="debriefs"
    )
    # The specific `chat` touch being debriefed. CASCADE (not SET_NULL): a
    # debrief without its chat has no subject, and touches are append-only
    # anyway, so this only fires when the contact itself is deleted.
    touch = models.ForeignKey(
        Touch, on_delete=models.CASCADE, related_name="debriefs"
    )
    # Free text -> appended to Contact.notes under a dated header at save
    # time. Kept here too so the append can be made exactly once (see
    # crm.debrief.record) and so the raw answer survives note editing.
    learned = models.TextField(blank=True, default="")

    # "Did they offer an intro?" -> a new Contact plus a follow-up Task.
    intro_name = models.CharField(max_length=255, blank=True, default="")
    intro_email = models.EmailField(blank=True, default="")
    # Non-null once the referral contact exists — the idempotency marker.
    intro_contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="referred_by_debriefs",
    )
    intro_task = models.ForeignKey(
        "Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # "Did they mention a date worth tracking?" -> a Task on that date.
    tracked_date = models.DateField(null=True, blank=True)
    date_note = models.CharField(max_length=255, blank=True, default="")
    date_task = models.ForeignKey(
        "Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # "Would they advocate for you?" — recorded as an answer, never acted on
    # by itself. A "yes" OFFERS the promotion; `promoted` only turns true
    # once the user takes it, and the warmth move itself goes through
    # crm.services.set_contact_state so it lands in the touches audit trail
    # like every other state change.
    advocate_answer = models.CharField(
        max_length=16, blank=True, default="", choices=ADVOCATE_ANSWERS
    )
    promoted = models.BooleanField(default=False)

    dismissed = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "chat_debriefs"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "touch"], name="uniq_chat_debriefs_user_touch"
            ),
        ]
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"Debrief of {self.contact_id} @ {self.created:%Y-%m-%d}"


class Task(PrivateModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    why = models.TextField(blank=True, default="")
    due = models.DateField(null=True, blank=True)
    kind = models.CharField(max_length=64, blank=True, default="")
    firm = models.ForeignKey(
        Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    # Dedup key the ported cadence/backward-planner (§4) uses for its
    # ≤3-day in-place update instead of spawning duplicate tasks.
    source_key = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="open")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "tasks"
        ordering = ["due", "-created"]

    def __str__(self) -> str:
        return self.title


class CalendarEvent(PrivateModel):
    """Something with a DATE on it: a scheduled coffee chat, or anything the
    user puts on the calendar by hand.

    WHY THIS TABLE EXISTS. Coverage knew a chat had been *scheduled*
    (`thread_state="chat_scheduled"`) but never when it actually was — the
    Today page's "Coming up" says out loud that it reports when a chat was
    SET UP, "because we do not store a chat datetime anywhere, so any 'chat
    tomorrow' here would be invented". Meanwhile `capture.extractors` was
    already pulling a real `chat_scheduled_at` off calendar invites (DTSTART)
    and dropping it on the floor. This is the destination that was missing.

    TWO SOURCES, ONE SHAPE. A captured chat and a hand-added event are the
    same kind of row and are deliberately not split into two models: the
    calendar renders one timeline, and `source` records where each came from
    so a captured time can be told from a typed one.

    `contact` is nullable because a hand-added event ("Superday", "flight to
    HK") need not be about anyone. When it IS set, deleting the contact takes
    the event with them — an orphaned "chat with <deleted person>" is noise.
    """

    KIND_CHAT = "chat"
    KIND_EVENT = "event"
    KIND_CHOICES = [
        (KIND_CHAT, "Coffee chat"),
        (KIND_EVENT, "Event"),
    ]
    SOURCE_MANUAL = "manual"
    SOURCE_CAPTURE = "capture"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Added by you"),
        (SOURCE_CAPTURE, "From your mailbox"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    starts_at = models.DateTimeField()
    # Blank means "no stated end" — a coffee chat pulled off an invite often
    # has one, a hand-typed note usually doesn't, and inventing "+1 hour"
    # would put a fake block on the calendar.
    ends_at = models.DateTimeField(null=True, blank=True)
    # A date with no clock time: "Superday, the 14th". Stored as a datetime
    # at local midnight with this flag set, so ordering stays one comparison
    # rather than a union over two columns.
    all_day = models.BooleanField(default=False)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_EVENT)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.CASCADE,
        related_name="calendar_events",
    )
    location = models.CharField(max_length=255, blank=True, default="")
    # The Gmail thread a captured event came from. Its uniqueness per user is
    # what stops a twice-daily sync stacking the same chat every run.
    thread_id = models.CharField(max_length=128, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "calendar_events"
        ordering = ["starts_at", "id"]
        constraints = [
            # One event per (user, thread) for CAPTURED rows only. Hand-added
            # events carry a blank thread_id and must never collide with each
            # other — Postgres treats NULLs as distinct but NOT empty strings,
            # so the condition excludes blanks rather than relying on that.
            models.UniqueConstraint(
                fields=["user", "thread_id"],
                condition=~models.Q(thread_id=""),
                name="uniq_calendar_event_user_thread",
            ),
        ]
        indexes = [models.Index(fields=["user", "starts_at"])]

    def __str__(self) -> str:
        return f"{self.title} @ {self.starts_at:%Y-%m-%d %H:%M}"
