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
    # The regions a PERSON can carry. Deliberately NOT the same vocabulary as
    # `Firm.regions` (which also carries sg/eu): us and hk are the only two
    # markets the cadence engine's deadline scoping and the sponsorship rules
    # actually reason about. "other" is the third real answer — the founder's
    # network holds people in London and Singapore who used to have nowhere to
    # sit (reported 2026-08-25: "accurate sorting between united states and
    # hongkong and other countries"). It means "known to be OUTSIDE both
    # markets", the same stated-but-untracked meaning `directory.classify`'s
    # opportunity vocabulary gives the word — and it is NOT a synonym for
    # blank. Blank stays "unknown", the honest default: we don't know, so the
    # engine keeps its both-regions fallback and the board keeps saying it's
    # a guess. Backfills must never collapse the two.
    REGION_CHOICES = [
        ("us", "United States"),
        ("hk", "Hong Kong"),
        ("other", "Other countries"),
    ]
    REGION_VALUES = frozenset(value for value, _ in REGION_CHOICES)
    # The subset the deadline machinery scopes by. A contact marked "other"
    # matches NO us/hk close date (the engine's per-region bucket simply has
    # no entry for them) — which is correct: a person in London should not be
    # re-pinged because a Hong Kong deadline is near. Only an unknown keeps
    # the conservative match-either fallback.
    DEADLINE_MARKETS = frozenset({"us", "hk"})

    # Provenance for `region` below. Ordered by how much the product trusts
    # them: a person's own answer, then what their own declared markets
    # entail, then what their firm's markets entail. There is deliberately no
    # code for "we guessed from the mail" — see `region_source`'s comment and
    # `resolve_region` for why no probabilistic signal is allowed on the
    # write path at all.
    REGION_SOURCE_USER = "user"
    REGION_SOURCE_DECLARED = "declared"
    REGION_SOURCE_FIRM = "firm"
    REGION_SOURCE_CHOICES = [
        ("user", "Set by you"),
        ("declared", "From your target regions"),
        ("firm", "From the firm's markets"),
    ]

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
    # back to matching either region rather than guessing. "other" means the
    # opposite of blank — we DO know, and the answer is neither market — so
    # the engine skips deadline scoping for them entirely (see
    # DEADLINE_MARKETS above).
    region = models.CharField(
        max_length=8, blank=True, default="", choices=REGION_CHOICES
    )
    # WHERE the value in `region` above came from. Blank exactly when
    # `region` is blank (the invariant `resolve_region` below keeps, and
    # `crm/tests/test_region_resolution.py` asserts): a placed contact always
    # knows who placed them.
    #
    # It earns its place three ways, and the first is load-bearing rather than
    # informational: it is the only thing that makes the Settings reversal
    # possible. A row filed "us" because the student's ONLY declared market
    # was the US rests on a premise that stops holding the day they add Hong
    # Kong — and without provenance there is no way to tell those rows apart
    # from the ones a human placed by hand or a single-market firm answered.
    # With it, the reversal blanks exactly the `declared` rows and leaves
    # `user`/`firm` alone. Second, it is what lets the board say a region was
    # a guess rather than an answer. Third, it keeps a future re-derivation
    # from ever touching a value a person typed.
    region_source = models.CharField(
        max_length=16, blank=True, default="", choices=REGION_SOURCE_CHOICES
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
    # THE PER-CONTACT ESCAPE HATCH from a campaign classification (see
    # `Campaign` below and `crm.campaigns`). True means "keep this person in my
    # daily queue whatever the campaign they arrived in is classified as".
    #
    # Needed because a campaign answer is one answer about ~200 people and will
    # occasionally be wrong about one of them: the founder mail-merged a club
    # panel invitation to alumni across every industry, and one of those alumni
    # turns out to be an investment banker he genuinely wants to recruit
    # through. Without this the only way to rescue that person would be to
    # reclassify the whole campaign and let the other 200 back in.
    #
    # A PLAIN BOOLEAN, not the three-state `recruiting_contact` uses, and the
    # asymmetry is deliberate. `recruiting_contact` has a text fallback, so it
    # needs "nobody has said" to be distinguishable from "no". This one has no
    # fallback to defer to — the campaign's own classification IS the default —
    # so False and "not answered" mean the identical thing and a third state
    # would be a distinction with no consequence.
    #
    # Detection never writes this column. It is the user's word, and it stands.
    campaign_exempt = models.BooleanField(default=False)
    # Is this person related to the user's RECRUITMENT at all? The founder's
    # 2026-08-25 rule: "all contacts in coverage need to be related to
    # recruitment, any unrelated should not show up." NULL means "the rule
    # decides" (`crm.recruitment.contact_verdict` — a deterministic read of
    # role / notes / the firm's own tracks / the tier list); True/False is
    # the user's own word and wins permanently, in both directions — the
    # rescue for a person the rule wrongly hides AND the hammer for one it
    # wrongly keeps.
    #
    # THREE-STATE for the same reason `recruiting_contact` above is: the rule
    # is the fallback, so "nobody has said" must stay distinguishable from
    # "no". NOTE THE DELIBERATE REVERSAL this column's rule carries: the
    # blanket school-tie exemption (`crm.relevance.REL_SCHOOL` keeping every
    # alum in the queue) was an earlier founder decision, and judging his 24
    # school-only rows it kept two professors, the campus advising office and
    # tech/CPG alumni alongside the campus recruiters and finance-club peers
    # who belong. The founder overrode it: the person's own occupation is the
    # test now, and a school tie by itself decides nothing. See
    # `crm/recruitment.py` for the whole doctrine.
    #
    # No automated path ever writes this column.
    recruitment_related = models.BooleanField(null=True, blank=True, default=None)

    class Meta(PrivateModel.Meta):
        db_table = "contacts"
        ordering = ["-created"]

    def __str__(self) -> str:
        return self.name

    def firm_markets(self) -> set[str]:
        """The deadline markets (us/hk) this contact's firm recruits in.

        Intersects DEADLINE_MARKETS, not REGION_VALUES: a firm's non-us/hk
        footprint ("sg", "eu", Jane Street's "apac") describes where the FIRM
        operates, never where this person sits, so it must not auto-pin
        anyone to "other". "other" is only ever written by a human."""
        if self.firm_id is None:
            return set()
        return {
            (r or "").strip().lower()
            for r in (self.firm.regions or [])
        } & self.DEADLINE_MARKETS

    def default_region_from_firm(self) -> str:
        """The region this contact's firm implies, or "" when it implies
        nothing. Only an UNAMBIGUOUS firm answers: exactly one deadline market
        (us/hk) on the firm. A firm that recruits in both, in neither, or only
        in a region this product doesn't model yields "" — guessing there is
        exactly the bug this field exists to kill, and a blank keeps the
        cadence engine's conservative both-regions fallback."""
        known = self.firm_markets()
        return next(iter(known)) if len(known) == 1 else ""

    def resolve_region(self, *, user_regions=None) -> tuple[str, str]:
        """`(region, region_source)` for this row — the WHOLE write-path rule.

        Precedence, first match wins:

          1. A region already on the row. Never overwritten by anything below;
             a blank `region_source` on a filled `region` heals to "user",
             which is what every by-hand path (the edit form, the bulk verbs,
             `capture_discover --region`) produces.
          2. The student's own declared markets, when exactly ONE of them is a
             deadline market -> that region, "declared". A US-only student's
             contact at HSBC is a US contact: the firm's Hong Kong desk is
             irrelevant to somebody not recruiting there. This is why tier 2
             sits ABOVE the firm.
          3. Firm markets ∩ declared markets, when that leaves exactly one.
          4. Firm markets alone, when that is exactly one (the original
             `default_region_from_firm` rule, unchanged).
          5. Nothing -> ("", ""). Blank is a real answer and has to stay
             reachable: the cadence engine's both-regions fallback and the
             board's "shown on a guess" caveat are both built on it, and
             retiring `cadence.infer_region` — which returned a confident
             region 100% of the time — is what made it reachable at all.

        `user_regions` is the six-value Settings vocabulary
        (`directory.classify.TRACKED_REGIONS`), ALWAYS intersected with
        DEADLINE_MARKETS before it decides anything: `Contact.region` has
        three values and "sg" is not one of them. A student declaring ['sg']
        alone therefore reaches tier 5 and stays blank rather than being
        mapped to "other" — a firm's or a student's non-us/hk footprint
        describes a market, never a desk.

        Pass `user_regions` explicitly from any loop over many rows (see
        `accounts.services.parse_contacts_csv`); left None it reads
        `self.user.regions`, which is one query per row on a contact whose
        user isn't already loaded.

        NOTHING PROBABILISTIC IS CONSULTED HERE, deliberately. Measured on 174
        real inbound messages across 54 contacts: the Date header's UTC offset
        has 0% coverage on corporate senders (Exchange Online rewrites it —
        all eleven Hong Kong bankers carried +0000), send-hour clustering
        resolved 12% of contacts and only after a reply, and signature cities
        and phone country codes are firm-wide templates naming an office
        rather than a desk. A wrong region silently mis-scopes deadline
        warnings and the student has no reason to doubt it. Write only what a
        stated fact entails; ask for everything else.
        """
        if self.region:
            return self.region, self.region_source or self.REGION_SOURCE_USER
        if user_regions is None:
            user_regions = getattr(self.user, "regions", None) or []
        declared = {
            (r or "").strip().lower() for r in user_regions
        } & self.DEADLINE_MARKETS
        if len(declared) == 1:
            return next(iter(declared)), self.REGION_SOURCE_DECLARED
        firm = self.firm_markets()
        both = firm & declared
        if len(both) == 1:
            return next(iter(both)), self.REGION_SOURCE_FIRM
        if len(firm) == 1:
            return next(iter(firm)), self.REGION_SOURCE_FIRM
        return "", ""

    @classmethod
    def from_db(cls, db, field_names, values):
        """Remember the region this row was loaded with.

        The one thing `resolve_region` cannot see on its own: whether the
        `region` it is being handed came off the row or was just typed into
        it. A student who opens the edit form on a contact filed "us" by
        their own declaration and changes it to Hong Kong has stated a fact,
        and the row has to record that — otherwise it keeps `region_source`
        "declared" and the next Settings change unplaces an answer a person
        gave by hand. Compared in `save()` below.
        """
        obj = super().from_db(db, field_names, values)
        if "region" in field_names:
            obj._loaded_region = obj.region
        return obj

    def save(self, *args, **kwargs):
        user_regions = kwargs.pop("user_regions", None)
        loaded = getattr(self, "_loaded_region", None)
        if self.region and loaded is not None and self.region != loaded:
            # Changed by hand after loading — see `from_db`. The only
            # application paths that move a region without a person saying so
            # write it with `.update()`/`bulk_update()` and never reach here.
            self.region_source = self.REGION_SOURCE_USER
        region, source = self.resolve_region(user_regions=user_regions)
        if (region, source) != (self.region, self.region_source):
            self.region, self.region_source = region, source
            # A partial save() must still persist the columns we just filled
            # in, or the value silently vanishes.
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {
                    *update_fields, "region", "region_source",
                }
        super().save(*args, **kwargs)
        # What is on the row now is what a later save() must compare against —
        # without this, a second save() on the same instance would read the
        # region it was loaded with hours ago and call an unchanged value a
        # hand edit.
        self._loaded_region = self.region


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
    # The message's Subject header, when the capture path that produced this
    # touch actually saw one. Blank everywhere else, and blank is honest — a
    # hand-logged coffee chat has no subject line.
    #
    # WHY IT IS HERE AT ALL: `capture.gmail_live._classify_message` has always
    # read the Subject header and thrown it away, folding it into the prose of
    # `evidence` ("Sent: <subject>"). A mail merge's single defining fact is
    # that 201 threads share one subject, and that fact was being discarded one
    # line after it was read. `crm.campaigns` groups on this.
    #
    # WHY IT IS WRITTEN BY DJANGO AND NOT BY THE PIPELINE. `touches` rows are
    # INSERTed by `coverage_domain.pipeline.apply_touch`'s raw SQL, whose column
    # list is part of that package's contract with the ported ratchet (see this
    # module's own schema-compatibility docstring). Widening that INSERT to
    # carry a column only the web app cares about would put a view concern
    # inside the pure engine for no gain: `apply_touch` returns the new row's
    # id, so the capture layer stamps the subject on straight afterwards. The
    # `db_default` is load-bearing and not decoration. Django normally emits no
    # database-level default: `default=""` is applied in Python, so a column
    # added that way is NOT NULL with nothing behind it, and the FIRST raw
    # `INSERT INTO touches (...)` that does not name it fails with a
    # NotNullViolation. Every touch this product writes goes through that
    # INSERT. The DB default is what keeps this column invisible to the pure
    # package, which is the whole point of adding it here rather than there.
    subject = models.CharField(
        max_length=255, blank=True, default="", db_default=""
    )

    class Meta(PrivateModel.Meta):
        db_table = "touches"
        ordering = ["-ts"]

    def __str__(self) -> str:
        return f"{self.kind} @ {self.ts:%Y-%m-%d %H:%M}"


class Campaign(PrivateModel):
    """One bulk send the user made, and the one question they answer about it.

    WHY THIS TABLE EXISTS. The founder is a USC student recruiting for
    investment banking. He is ALSO "Associate of External Outreach" for USC's
    International Consulting Club, and in that role he mail-merged 201 threads
    with the subject "Fall 2026 ICC Alumni Digital Panel Outreach" — asking
    alumni across every industry to speak on a club panel. Recipients included
    an airline, a health insurer, a jeweller, a talent agency and a law firm.
    Coverage ingested every one of them as his personal recruiting network, and
    his Today queue filled up with club admin: "Propose a 15-min chat" to a
    person who had agreed to speak on a panel, "Follow up" with an HR manager
    who had ignored a panel invitation.

    Nothing about those relationships is wrong in the contact book. The mistake
    is entirely about WHOSE JOB the relationship is. So the fix is one question,
    asked once per campaign rather than 201 times per contact: is this bulk send
    my own recruiting, or something else I do?

    THE ANSWER IS DURABLE AND EDITABLE. `kind` starts at `unclassified`, which
    behaves exactly as today (everyone stays in the queue) — surfacing the
    question is the fix, and pre-emptively hiding 201 people on a guess would be
    a worse bug than the one being fixed. Once the user answers, `classified_at`
    is set and re-detection never touches `kind` again; only the user's own
    Settings control changes it.

    `signature` is the detector's grouping key, not a display string — see
    `crm.campaigns.normalize_subject`. It is unique per user, which is what lets
    a re-run of the detector find and update the same campaign rather than
    stacking a second copy of it every night.
    """

    # Nobody has answered yet. Behaves as `recruiting` for queue purposes (see
    # `crm.campaigns.excluded_contact_ids`) — the default must never remove
    # anyone, because a detector that hides contacts before the user has agreed
    # they should be hidden is a data-loss bug wearing a feature's clothes.
    KIND_UNCLASSIFIED = "unclassified"
    # "This was me job hunting." Nothing changes.
    KIND_RECRUITING = "recruiting"
    # "This was something else I do" — club work, an event, a survey, research.
    # Contacts whose relationship with the user STARTED here leave the daily
    # queue and keep every other surface in the product.
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_UNCLASSIFIED, "Not answered yet"),
        (KIND_RECRUITING, "My own recruiting"),
        (KIND_OTHER, "Something else I do"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # The normalized grouping key. Not shown to anyone.
    signature = models.CharField(max_length=255)
    # What the user reads on the Settings card. The best human-readable form of
    # the signature the detector could find — a real subject line where one was
    # stored, the evidence line otherwise. Display only; never matched on.
    label = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(
        max_length=16, choices=KIND_CHOICES, default=KIND_UNCLASSIFIED
    )
    # NULL until the user answers. The lock that stops re-detection overwriting
    # a human answer with a machine default — see `crm.campaigns.detect`.
    classified_at = models.DateTimeField(null=True, blank=True)
    # The window the detector matched, straight off the touches. Shown on the
    # card so the user can recognise the send ("3 Aug, 41 people") without
    # having to remember a subject line.
    first_sent = models.DateTimeField()
    last_sent = models.DateTimeField()
    # Distinct recipients at detection time. Denormalized rather than counted
    # on every render: the Settings card lists every campaign, and the honest
    # number is the one the detector actually matched on.
    recipient_count = models.PositiveIntegerField(default=0)
    detected_at = models.DateTimeField(auto_now_add=True)
    # NULL for every live campaign. Set when a detector run can no longer
    # produce this signature from any touch — i.e. the grouping key that
    # created the row has stopped qualifying, so the question on the card is
    # about a send that was never a send.
    #
    # WHY A COLUMN AND NOT A DELETE. Campaign 3 on the founder's account was
    # 41 people the detector had grouped out of Coverage's own boilerplate
    # (see `crm.campaigns`). Deleting it would have thrown away the record of
    # what the product asked him and which 41 contacts it asked about. A
    # timestamp retires the card and keeps the row: clearing it back to NULL
    # is a one-line reversal, and re-detection clears it by itself the moment
    # the signature qualifies again. Nothing reads a retired campaign except
    # the export, which is the user's own history and keeps everything.
    retired_at = models.DateTimeField(null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta(PrivateModel.Meta):
        db_table = "campaigns"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "signature"], name="uniq_campaigns_user_signature"
            ),
        ]
        ordering = ["-last_sent", "-id"]

    def __str__(self) -> str:
        return f"{self.label or self.signature} ({self.recipient_count})"

    @property
    def is_classified(self) -> bool:
        return self.kind != self.KIND_UNCLASSIFIED


class CampaignContact(PrivateModel):
    """One recipient of one campaign.

    `originates` is the field that carries the whole product decision. A
    campaign's classification applies to every contact whose relationship with
    the user STARTED in that campaign — not to everyone who happened to receive
    it. The banker the founder had already been recruiting for a month, who
    also got the club blast because he is a USC alum, is not club admin: his
    relationship began somewhere else and it stays in the queue.

    Concretely: the campaign's touch for this contact is also the earliest touch
    this contact has. Read off the touches, no guessing.

    Detection never deletes rows here and never flips `originates` after the
    first write. A membership is a historical fact about what happened in the
    mailbox; re-running the detector on a later day must not be able to rewrite
    history because a subsequent touch changed which one is earliest.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="memberships"
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="campaign_memberships"
    )
    originates = models.BooleanField(default=False)
    # The campaign touch's own timestamp for this contact — what `originates`
    # was decided from, kept so the decision is auditable without re-deriving it.
    sent_at = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "campaign_contacts"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "campaign", "contact"],
                name="uniq_campaign_contacts_user_campaign_contact",
            ),
        ]
        indexes = [models.Index(fields=["user", "contact"])]
        ordering = ["campaign_id", "contact_id"]

    def __str__(self) -> str:
        return f"{self.campaign_id} -> {self.contact_id}"


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


class ContactMerge(PrivateModel):
    """The ledger of one answer to "are these two cards one person?".

    WHY THIS TABLE EXISTS. The founder's own board held the proof that the
    identity matcher's two exact keys were not enough: "Ebba af Klercker"
    at ebbakler@amazon.com (replied, 3 touches) and "Ebba Kler" at
    ebbakler@amazon.es (cold, 1 touch) — one AWS account manager, tracked
    as two people, history split, and the queue one step from asking him to
    cold-email someone who had already replied. The evidence there is
    strong but not conclusive (same mailbox name, related domains, a
    truncated display name), and a FALSE merge is far worse than a missed
    one: it fuses two people's histories and there is no clean undo once
    later touches land on the fused card. So suggestive evidence only ever
    becomes a card in Settings (`crm.merge.candidate_pairs`, computed live,
    never stored), and this row records what the USER said about it.

    THREE ANSWERS, ALL DURABLE:
    - `merged`: the tap happened. `primary` kept the relationship,
      `duplicate`'s touches moved over (their ids in `moved_touch_ids`, so
      undo can move exactly those back and nothing else), blank fields
      copied across are in `field_changes` with before AND after (so undo
      restores a value only when it still holds what the merge wrote — a
      hand-edit since is the user's word), the alternate-address note line
      appended to `primary.notes` is in `note_line`, and the duplicate was
      archived (its prior state in `duplicate_was_archived`).
    - `undone`: the user reversed it. The pair is never re-suggested — the
      undo IS their answer.
    - `rejected`: the user said "different people". Never re-suggested;
      this row is the memory, exactly the ContactProposal-dismissal
      contract.

    Both FKs CASCADE: the ledger is ABOUT the two rows, and a ledger entry
    for a deleted contact audits nothing."""

    STATUS_MERGED = "merged"
    STATUS_UNDONE = "undone"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_MERGED, "Merged"),
        (STATUS_UNDONE, "Undone"),
        (STATUS_REJECTED, "Different people"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    primary = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="merges_kept"
    )
    duplicate = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="merges_folded"
    )
    # The sentence the card showed when the user answered — the same
    # show-what-you-knew rule every capture surface holds.
    evidence = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    # Touch ids moved from duplicate to primary, verbatim, for the undo.
    moved_touch_ids = models.JSONField(default=list, blank=True)
    # {field: {"before": ..., "after": ...}} for every primary field the
    # merge filled from the duplicate.
    field_changes = models.JSONField(default=dict, blank=True)
    # The exact line appended to primary.notes (the alternate address), so
    # undo can strip that line and no other.
    note_line = models.CharField(max_length=500, blank=True, default="")
    duplicate_was_archived = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "contact_merges"
        ordering = ["-created", "-id"]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self) -> str:
        return f"{self.duplicate_id} -> {self.primary_id} ({self.status})"


class PlayDismissal(PrivateModel):
    """One dismissed Today "play" — see `crm.today._plays`.

    A play is a dated world fact (a confirmed `FirmDate`) joined to the
    student's own people at that firm. Dismissal is remembered against the
    FACT — the tuple `(firm, event_kind, date)` — and never against the
    card's rendered shape or a row id. This is deliberate and load-bearing:
    the founder bulk-parked 113 contacts because an earlier surface asked too
    much, and a play that keeps coming back after being dismissed is exactly
    that bug wearing a new hat.

    WHY A VALUE TUPLE AND NOT AN FK TO `FirmDate`. A firm's own board gets
    re-scraped and `FirmDate` rows are updated IN PLACE (see that model's
    `history` field) — the same row can carry Aug 30 today and Sep 5 next
    week. An FK keyed on the row's id would still match after that edit and
    would wrongly keep the dismissal alive on a fact that no longer holds.
    Storing `date` as a plain value means a changed date makes a NEW tuple,
    which is exactly the anti-nag rule's escape hatch: "once dismissed it
    never renders again unless the date changes" (see the task brief this
    model was built against). `event_kind` is stored as the same raw string
    `FirmDate.event_kind` carries (not the human label), so a label wording
    change can never accidentally resurrect or re-dismiss a fact.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    firm = models.ForeignKey(Firm, on_delete=models.CASCADE)
    event_kind = models.CharField(max_length=64)
    date = models.DateField()
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta(PrivateModel.Meta):
        db_table = "play_dismissals"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "firm", "event_kind", "date"],
                name="uniq_play_dismissals_fact",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} dismissed {self.firm_id}/{self.event_kind}/{self.date}"
