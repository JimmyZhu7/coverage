"""Business logic for onboarding, CSV import/export, and self-serve
deletion (task M5; docs/build-plan.md §7 M5, §8, §10).

Kept out of views.py so each piece is unit-testable without an HTTP
request. Everything here writes private-zone rows through the explicit
`all_objects` manager (creation with a known user) or reads through
`.objects.for_user(user)` (tenant-scoped) — the contract from
coverage_web/tenancy.py. Import/export use only the stdlib-compatible `csv`
module (no pandas), per the task's hard constraint — `defusedcsv` (see the
import below) is a drop-in replacement of that module, not a departure from
it: same `reader`/`writer`/`DictReader` surface, with `writer.writerow`
additionally neutralising formula-leading cells before they reach the file.
"""

from __future__ import annotations

import io
import json as _json
import re
import zipfile
from dataclasses import dataclass, field

# Drop-in for the stdlib `csv` module used everywhere below (reader for
# import, writer for export) — same surface, but `writer`'s `writerow`/
# `writerows` neutralise a cell that would read as a spreadsheet formula
# before it reaches the file. Formerly hand-rolled as `_safe_cell()` (see
# git log "Stop a stranger's email subject running as a formula in the
# export"); replaced with the maintained library rather than carrying a
# bespoke implementation of a well-known defense. One gap versus the old
# guard, closed below rather than left silent: defusedcsv's own trigger set
# is `@+-=|%` and does not include a leading tab or carriage return, both of
# which a spreadsheet strips on paste before formula-detection runs, handing
# the character after them the same power as a leading `=`. See
# `_neutralise_tab_or_cr_lead` below and
# `test_a_formula_in_a_cell_exports_as_inert_text` in test_export.py, which
# exercises both defusedcsv's own set and this one live.
from defusedcsv import csv

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.utils import timezone

from analytics.events import record_event
from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from assistant.models import (
    AdvisorMemory, ChatConversation, ChatFolder, ChatMessage, DailyBrief,
)
from billing.models import CreditLedger, ProWaitlist
from capture import gmail_live
from capture.models import (
    ApplicationEvent, AutopilotDecision, AutopilotRun, ContactProposal,
    GmailConnection, MailFact,
)
from crm.models import (
    CalendarEvent, Campaign, CampaignContact, ChatDebrief, Contact, Task,
    Touch, UserFirm,
)
from directory.models import Firm

from .models import PushSubscription, User

# The tier assigned to every firm a user picks during onboarding. The plan
# (docs/build-plan.md §2) declares `user_firms.tier smallint` but leaves its
# semantics open; the task says "with a tier default". 2 is chosen as a
# neutral middle tier — it leaves tier 1 free as a later, manual "top
# priority" promotion and tier 3+ for stretch/backup firms — and pairs with
# status="target". Documented as a decision in the build report.
DEFAULT_FIRM_TIER = 2


def sign_out_other_sessions(user, *, keep_session_key: str | None = None) -> int:
    """Delete every DB-backed session belonging to `user` except the one they
    are asking from. Returns how many were ended.

    Sessions are opaque blobs, so this decodes each one to read its
    `_auth_user_id` rather than filtering in SQL. That is a full-table scan of
    `django_session`; at this product's scale (single-digit sessions per user,
    pre-launch) it costs nothing, and a correct answer beats a clever one.
    Revisit with a `user -> session` index table if the table ever grows past
    a few thousand live rows.

    Expired-but-unpurged rows are skipped: they cannot authenticate anyone, so
    counting them would inflate the "signed out on N devices" receipt.
    """
    ended = 0
    now = timezone.now()
    target = str(user.pk)
    for row in Session.objects.filter(expire_date__gt=now):
        if row.session_key == keep_session_key:
            continue
        if row.get_decoded().get("_auth_user_id") == target:
            row.delete()
            ended += 1
    if ended:
        record_event("sessions_signed_out", user=user, count=ended)
    return ended


# ---------------------------------------------------------------------------
# Onboarding: target-firm selection
# ---------------------------------------------------------------------------
def set_target_firms(user, firm_ids, *, tier: int = DEFAULT_FIRM_TIER) -> int:
    """Sync the user's `user_firms` rows to exactly `firm_ids` (idempotent —
    re-submitting the onboarding step reflects the current selection rather
    than piling up duplicates). Ignores ids that aren't real firms. Returns
    the resulting count of the user's target firms.
    """
    wanted = set(
        Firm.objects.filter(id__in=[i for i in firm_ids if str(i).isdigit()])
        .values_list("id", flat=True)
    )
    with transaction.atomic():
        existing = set(
            UserFirm.objects.for_user(user).values_list("firm_id", flat=True)
        )
        to_remove = existing - wanted
        to_add = wanted - existing
        if to_remove:
            UserFirm.objects.for_user(user).filter(firm_id__in=to_remove).delete()
        for firm_id in to_add:
            UserFirm.all_objects.create(
                user=user, firm_id=firm_id, tier=tier, status="target"
            )
    return len(wanted)


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------
# Canonical field -> the set of header spellings we accept for it. Headers
# are normalized (lowercased, non-alphanumerics stripped) before matching,
# so "E-mail", "Email Address", and "email" all collapse to "email".
_FIELD_ALIASES: dict[str, set[str]] = {
    "name": {"name", "fullname", "contact", "contactname", "person"},
    "email": {"email", "emailaddress", "mail"},
    "firm": {"firm", "company", "organization", "organisation", "employer", "org"},
    "role": {"role", "title", "position", "jobtitle"},
    "notes": {"notes", "note", "comments", "comment"},
    "angle": {"angle", "hook", "connection", "intro", "context"},
}

# Column order for the downloadable import template.
IMPORT_TEMPLATE_COLUMNS = ["name", "email", "firm", "role", "notes", "angle"]


def _norm(text: str) -> str:
    """Lowercase and strip everything that isn't a letter or digit. Used
    both for header matching and for building dedup keys, so 'J.P. Morgan'
    and 'JPMorgan' collapse to the same token."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# Trailing legal-entity words stripped from a firm string before matching.
# Deliberately narrow and deliberately trailing-only: stripping from the END
# only, one recognized word at a time, is what keeps "Bain & Company" and
# "Bain Capital" from ever colliding — "Company" is a suffix here, "Capital"
# isn't, so it's never touched. Verified against the live directory (127
# firms, zero collisions) in accounts/tests/test_accounts.py.
_LEGAL_SUFFIXES = frozenset({
    "inc", "llc", "llp", "ltd", "co", "company", "corp", "corporation",
    "group", "holdings", "partners", "plc", "limited",
})
# '&' / 'and' / '+' all mean the same thing between a firm's two halves
# ("Bain & Company" / "Bain and Company" / "Bain + Company"); once split into
# tokens the joiner carries no identity, so it's dropped rather than kept.
_JOINERS = frozenset({"and"})


def normalize_firm_name(text: str) -> str:
    """Normalize a firm name/string for matching against the directory —
    the forgiving counterpart to `_norm` above, used ONLY for firm
    resolution (header matching and dedup keys still use `_norm`).

    Case-insensitive; '&'/'and'/'+' collapse to the same joiner and are then
    dropped; punctuation is stripped; whitespace is collapsed; a trailing
    run of legal-entity words (Inc, LLC, LLP, Ltd, Co, Company, Corp,
    Corporation, Group, Holdings, Partners, Plc, Limited) is peeled off the
    end, one word at a time, but never down to nothing.

    Deterministic and cheap on purpose — a dict lookup, not a fuzzy-match
    library (see `_firm_lookup`). The one thing it must never do is merge
    two real, distinct firms: 'Bain & Company' normalizes to 'bain', 'Bain
    Capital' normalizes to 'baincapital' — 'Capital' is not a legal suffix,
    so the distinguishing word is never touched. Checked against every real
    near-collision in the live directory (Bain & Company / Bain Capital,
    J.P. Morgan / Morgan Stanley, McKinsey & Company, Rothschild & Co, and
    every "... Partners" firm) with zero merges across all 127 firms.
    """
    if not text:
        return ""
    lowered = text.lower().replace("&", " and ").replace("+", " and ")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    tokens = [t for t in cleaned.split() if t and t not in _JOINERS]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _build_column_map(header: list[str]) -> dict[int, str]:
    """Map CSV column index -> canonical field name, tolerant of variants.
    First matching alias wins; unknown columns are ignored."""
    alias_to_field = {
        _norm(alias): field_name
        for field_name, aliases in _FIELD_ALIASES.items()
        for alias in aliases
    }
    col_map: dict[int, str] = {}
    for idx, raw in enumerate(header):
        field_name = alias_to_field.get(_norm(raw))
        if field_name and field_name not in col_map.values():
            col_map[idx] = field_name
    return col_map


@dataclass
class UnmatchedFirmGroup:
    """One firm string this import couldn't resolve, plus enough to offer a
    fix: which contacts carry it (so the "Link to..." POST can re-point
    exactly those rows) and a best-effort suggestion, when there is one."""
    firm_text: str
    count: int
    contact_ids: list[int] = field(default_factory=list)
    suggested_firm: Firm | None = None


@dataclass
class ImportResult:
    created: int = 0
    skipped_duplicate: int = 0
    skipped_empty: int = 0
    firm_matched: int = 0
    total_rows: int = 0
    unmatched_columns: bool = False
    errors: list[str] = field(default_factory=list)
    # Firm strings from THIS import's created contacts that didn't resolve
    # to a directory Firm — one entry per distinct firm (grouped by the same
    # forgiving key `normalize_firm_name` uses for matching, so "Foo Inc"
    # and "foo inc." on two different rows land in one group). Not part of
    # `as_stats()` for the same reason `created_contacts` isn't: it carries
    # live contact ids and a Firm reference, not JSON-safe summary data.
    unmatched_firms: list["UnmatchedFirmGroup"] = field(default_factory=list)
    # The actual `Contact` rows this parse created (with real pks — Postgres
    # returns them from `bulk_create`), not just the count. Not part of
    # `as_stats()` — that dict is what gets stored as JSON on an `Import`
    # row, and a list of model instances doesn't belong there. This exists
    # so `import_contacts()` below can scope the free Gmail enrichment scan
    # to exactly the contacts this one file created, instead of every
    # contact the user has.
    created_contacts: list = field(default_factory=list, repr=False)

    @property
    def skipped(self) -> int:
        return self.skipped_duplicate + self.skipped_empty

    def as_stats(self) -> dict:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_empty": self.skipped_empty,
            "firm_matched": self.firm_matched,
            "total_rows": self.total_rows,
            "unmatched_firm_groups": len(self.unmatched_firms),
        }


def _firm_lookup() -> dict[str, Firm]:
    """Normalized firm name/slug -> Firm, for text->firm matching. Built
    once per import (not per row) — a plain dict lookup, so matching stays
    O(1) per CSV row regardless of directory size.

    Two keys per name/slug: the strict `_norm` form (kept so an exact,
    already-normalized paste of the slug still hits) and the forgiving
    `normalize_firm_name` form (handles '&'/'and'/'+' and a trailing legal
    suffix — this is what fixes "Bain and Company" against a directory
    entry of "Bain & Company"). Verified collision-free across the whole
    directory in accounts/tests/test_accounts.py.
    """
    lookup: dict[str, Firm] = {}
    for firm in Firm.objects.all():
        for key in (
            _norm(firm.name),
            _norm(firm.slug),
            normalize_firm_name(firm.name),
            normalize_firm_name(firm.slug),
        ):
            if key:
                lookup.setdefault(key, firm)
    return lookup


def _guess_firm(key: str, firms: dict[str, Firm]) -> Firm | None:
    """A cheap, deterministic best-effort suggestion for a firm string that
    didn't match anything exactly — used only to prefill the "Link to..."
    select, never to auto-link. Fires only when `key` is an unambiguous
    prefix/suffix of exactly one directory firm's normalized key (e.g. a CSV
    firm string with a trailing word the directory entry doesn't carry, or
    vice versa). No scoring, no fuzzy distance — genuinely ambiguous or
    unrelated strings suggest nothing rather than guess wrong."""
    if not key:
        return None
    candidates = {
        firm for lookup_key, firm in firms.items()
        if lookup_key != key and (lookup_key.startswith(key) or key.startswith(lookup_key))
    }
    return candidates.pop() if len(candidates) == 1 else None


def _group_unmatched_firms(
    contacts: list[Contact], firms: dict[str, Firm]
) -> list[UnmatchedFirmGroup]:
    """Group this import's firm-text-only contacts by their normalized firm
    string, so two rows spelled slightly differently ('Foo Inc' / 'foo
    inc.') land in one fix-up card instead of two."""
    groups: dict[str, UnmatchedFirmGroup] = {}
    for contact in contacts:
        if contact.firm_id or not contact.firm_text:
            continue
        key = normalize_firm_name(contact.firm_text)
        if not key:
            continue
        group = groups.get(key)
        if group is None:
            group = UnmatchedFirmGroup(
                firm_text=contact.firm_text,
                count=0,
                suggested_firm=_guess_firm(key, firms),
            )
            groups[key] = group
        group.count += 1
        group.contact_ids.append(contact.pk)
    return sorted(groups.values(), key=lambda g: g.firm_text.lower())


def parse_contacts_csv(user, text: str) -> ImportResult:
    """Parse `text` as a contacts CSV and create `Contact` rows for `user`.

    Dedup rule (application-layer, per §2): a row is skipped when its
    normalized email already exists for the user, OR — when it has a name —
    its (normalized name, normalized firm) pair already exists. Dedup is
    also applied within the file itself, so re-importing the same file
    creates nothing new.
    """
    result = ImportResult()
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.errors.append("The file is empty.")
        return result

    col_map = _build_column_map(header)
    if not col_map:
        result.unmatched_columns = True
        result.errors.append(
            "No recognizable columns found. Expected some of: "
            + ", ".join(IMPORT_TEMPLATE_COLUMNS)
            + "."
        )
        return result

    firms = _firm_lookup()

    # Seed dedup sets from the user's existing contacts.
    existing_emails: set[str] = set()
    existing_name_firm: set[tuple[str, str]] = set()
    for c in Contact.objects.for_user(user).select_related("firm"):
        if c.email:
            existing_emails.add(c.email.strip().lower())
        firm_token = _norm(c.firm.name) if c.firm_id else _norm(c.firm_text)
        if c.name:
            existing_name_firm.add((_norm(c.name), firm_token))

    to_create: list[Contact] = []
    for row in reader:
        if not row:
            continue  # truly blank line (csv yields []) — not a data row
        result.total_rows += 1
        values = {
            field_name: (row[idx].strip() if idx < len(row) else "")
            for idx, field_name in col_map.items()
        }
        name = values.get("name", "")
        email = values.get("email", "")
        firm_raw = values.get("firm", "")

        if not name and not email:
            result.skipped_empty += 1
            continue
        # A row with an email but no name still imports; use the email as a
        # readable stand-in so Contact.name is never blank.
        if not name:
            name = email

        matched_firm = (
            firms.get(normalize_firm_name(firm_raw)) or firms.get(_norm(firm_raw))
            if firm_raw else None
        )

        email_key = email.strip().lower()
        firm_token = _norm(matched_firm.name) if matched_firm else _norm(firm_raw)
        name_key = (_norm(name), firm_token)

        is_dupe = False
        if email_key and email_key in existing_emails:
            is_dupe = True
        elif values.get("name") and name_key in existing_name_firm:
            # only use the name+firm key when the row actually had a name
            is_dupe = True
        if is_dupe:
            result.skipped_duplicate += 1
            continue

        # Record keys so intra-file duplicates are skipped too.
        if email_key:
            existing_emails.add(email_key)
        if values.get("name"):
            existing_name_firm.add(name_key)

        if matched_firm:
            result.firm_matched += 1

        contact = Contact(
            user=user,
            name=name[:255],
            firm=matched_firm,
            firm_text="" if matched_firm else firm_raw[:255],
            role=values.get("role", "")[:255],
            email=email[:254],
            notes=values.get("notes", ""),
            angle=values.get("angle", ""),
            source="import",
        )
        # bulk_create (below) never calls save(), so the firm-derived region
        # default has to be applied by hand here or imported contacts would be
        # the one path that silently keeps an unknown region. `matched_firm` is
        # already loaded, so this costs no extra query.
        contact.region = contact.default_region_from_firm()
        to_create.append(contact)

    if to_create:
        # bulk_create goes through the base manager; user is set on each row.
        # On Postgres this also populates each object's own `.pk` in place
        # (the RETURNING clause) — that's what makes `created_contacts`
        # usable by the caller without a second query.
        Contact.all_objects.bulk_create(to_create)
        result.created = len(to_create)
        result.created_contacts = to_create
        result.unmatched_firms = _group_unmatched_firms(to_create, firms)
    return result


def import_contacts(user, *, file_bytes: bytes, filename: str) -> ImportResult:
    """Decode an uploaded CSV, create contacts, and write the bookkeeping
    (`Import` row + `import_completed` event)."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1", errors="replace")

    result = parse_contacts_csv(user, text)

    Import.all_objects.create(
        user=user,
        kind="contacts",
        filename=(filename or "contacts.csv")[:255],
        row_stats=result.as_stats(),
    )
    # `import_completed` is a named funnel event (see analytics/events.py's
    # canonical list) — it must mean the import actually put rows in the
    # user's CRM. Firing it unconditionally meant an unreadable CSV, an
    # all-duplicate file, or an all-empty file counted as activation exactly
    # like a real import. A no-op import now records `import_failed`
    # instead, so the funnel number answers "did this work", not "was the
    # button clicked".
    if result.created > 0:
        record_event("import_completed", user=user, count=result.created)
    else:
        record_event(
            "import_failed", user=user, count=0, errors=list(result.errors)
        )

    # Free, zero-AI enrichment: if this user already has Gmail connected,
    # check its history for the people this import just created — a
    # student who imports 180 contacts they've already emailed should not
    # see all 180 as identical, un-contacted-looking cold rows just
    # because Coverage never checked. Scoped to only the new rows (not the
    # user's whole contact list) and never allowed to fail the import
    # itself — see `gmail_live.backfill_new_contacts`'s docstring.
    gmail_live.backfill_new_contacts(user, result.created_contacts)

    return result


def link_contacts_to_firm(user, contact_ids: list[int], firm_id) -> int:
    """The import summary's "Link to..." fix-up: re-point a batch of the
    user's own free-text-firm contacts at a real directory `Firm`, in one
    POST. Tenant-scoped through `.for_user` — structurally cannot touch
    another user's rows even if a stray id leaked into the form.

    `firm_text` is cleared once linked, matching the invariant the rest of
    this module keeps: a contact with `firm_id` set carries the directory
    name through `firm`, not a stale copy in `firm_text` (see
    `_firm_label`). Leaving it in place is only correct for the OTHER
    branch — unmatched and staying unmatched — where a name typed by hand
    is still worth having.

    Region is recomputed the same way `parse_contacts_csv` does for a fresh
    import: `.update()` never calls `save()`, so a now-known, unambiguous
    firm region has to be applied by hand or a contact linked here would
    stay "unknown region" forever even though the firm answers it.
    """
    try:
        firm = Firm.objects.get(pk=firm_id)
    except (Firm.DoesNotExist, ValueError, TypeError):
        return 0
    contacts = list(Contact.objects.for_user(user).filter(pk__in=contact_ids))
    if not contacts:
        return 0
    for contact in contacts:
        contact.firm = firm
        contact.firm_text = ""
        if not contact.region:
            contact.region = contact.default_region_from_firm()
    Contact.all_objects.bulk_update(contacts, ["firm", "firm_text", "region"])
    record_event(
        "import_firm_linked", user=user, firm=firm.name, count=len(contacts)
    )
    return len(contacts)


def import_template_csv() -> str:
    """The downloadable, header-plus-one-example-row import template."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(IMPORT_TEMPLATE_COLUMNS)
    writer.writerow(
        [
            "Jane Banker",
            "jane.banker@example.com",
            "Goldman Sachs",
            "Analyst",
            "Met at the spring info session",
            "Same hometown; both rowed in college",
        ]
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Data export (the user's own data, portable — §10 trust feature)
# ---------------------------------------------------------------------------
# THE RULE THIS SECTION EXISTS TO KEEP: whatever `delete_user_and_data` below
# destroys, the export must first be able to hand back. Those two lists were
# allowed to drift — deletion swept nine private tables while the export
# shipped two CSVs, under a privacy policy that promised "export everything as
# CSV at any time" and a Settings line that said "download all your data".
# `test_export.py` now asserts the containment (every model in `_DELETE_ORDER`
# has a file in the ZIP), so the next table added to one list fails loudly
# until it is added to the other.
#
# Shape follows the category norm (Huntr's "Download My Data"): ONE ZIP, one
# CSV per data type, plus a README naming what is and isn't inside. The two
# single-file buttons stay — a student who only wants their contacts in a
# spreadsheet shouldn't have to unzip anything.
CONTACT_EXPORT_COLUMNS = [
    # `linkedin`, `school`, `school_affiliation` and `archived` were missing.
    # `archived` is the one that made the export actively wrong rather than
    # merely thin: 25 of the founder's 137 contacts are archived, and without
    # the column a re-import resurrects every one of them as an active person
    # sitting back in the cadence queue.
    "name", "email", "linkedin", "firm", "role", "region", "school",
    "school_affiliation", "warmth", "thread_state", "angle", "opener",
    "notes", "source", "archived", "created",
]
TOUCH_EXPORT_COLUMNS = [
    "contact_name", "contact_email", "firm", "ts", "channel",
    "kind", "note", "source",
]
FIRM_EXPORT_COLUMNS = ["firm", "tier", "status"]
APPLICATION_EXPORT_COLUMNS = [
    "opportunity", "firm", "region", "deadline", "url",
    "applied_status", "applied_at", "interview_dates", "dismissed",
]
TASK_EXPORT_COLUMNS = ["title", "why", "due", "kind", "firm", "status", "created"]
CAMPAIGN_EXPORT_COLUMNS = [
    "label", "kind", "recipients", "first_sent", "last_sent", "classified_at",
]
CAMPAIGN_CONTACT_EXPORT_COLUMNS = [
    "campaign", "contact", "contact_email", "originates", "sent_at",
]
CONTACT_PROPOSAL_EXPORT_COLUMNS = [
    "name", "email", "firm", "role_hint", "recruiting_hint", "evidence",
    # What the person replied to, and whether it was a reply at all. Observed
    # off their message like every other column here, and the one that
    # explains WHY a row exists — an export that dropped it would be a list of
    # names with the reason removed.
    "thread_subject", "threaded_reply",
    "status", "occurred_at", "created", "resolved_at",
]
APPLICATION_EVENT_EXPORT_COLUMNS = [
    "firm", "role", "event", "target_status", "due_on", "evidence",
    "match_reason", "detected_by", "status", "occurred_at", "created",
    "resolved_at",
]
MAIL_FACT_EXPORT_COLUMNS = [
    # `quote` is the one sentence of body text the product deliberately
    # keeps (see `MailFact`'s §10 note) — it is the justification the user
    # audited the automated action against, so it is theirs to take with
    # them like everything else here.
    "kind", "about_name", "about_email", "new_name", "new_email",
    "return_on", "quote", "subject", "detected_by", "status", "action_note",
    "occurred_at", "created", "resolved_at",
]
AUTOPILOT_RUN_EXPORT_COLUMNS = [
    "created", "status", "model", "source_label", "accepts", "escalations",
    "skips", "deferred", "llm_calls", "credits_spent", "decided_at",
    "applied_at",
]
AUTOPILOT_DECISION_EXPORT_COLUMNS = [
    "run_created", "about", "decision", "confidence", "quote", "reason",
    "detected_by", "status", "overridden", "created", "applied_at",
]
DEBRIEF_EXPORT_COLUMNS = [
    "created", "contact", "learned", "intro_name", "intro_email",
    "tracked_date", "date_note", "advocate_answer", "promoted", "dismissed",
]
FIT_SCORE_EXPORT_COLUMNS = [
    "subject_type", "subject", "composite", "axes", "reasoning",
    "params_version", "computed_at",
]
IMPORT_EXPORT_COLUMNS = ["created", "kind", "filename", "row_stats"]
PRODUCT_EVENT_EXPORT_COLUMNS = ["ts", "event", "props"]
CALENDAR_EVENT_EXPORT_COLUMNS = [
    "title", "description", "starts_at", "ends_at", "all_day", "kind",
    "source", "contact", "location",
]
CHAT_FOLDER_EXPORT_COLUMNS = ["name", "created"]
CHAT_CONVERSATION_EXPORT_COLUMNS = ["title", "folder", "created", "updated"]
CHAT_MESSAGE_EXPORT_COLUMNS = ["conversation", "role", "text", "created"]
ADVISOR_MEMORY_EXPORT_COLUMNS = ["text", "created"]
DAILY_BRIEF_EXPORT_COLUMNS = ["date", "text", "created"]
# Deliberately excludes `refresh_token_encrypted` — a bearer credential to the
# user's own mailbox, not "your data" in the export sense (see
# `GmailConnection`'s docstring on why it's encrypted at rest at all). Losing
# this row on export/delete just means reconnecting; it is not a fact about
# the student.
GMAIL_CONNECTION_EXPORT_COLUMNS = [
    "gmail_address", "status", "connected_at", "last_notification_at",
    "backfill_status", "rescan_status",
]
# Deliberately excludes `p256dh`/`auth` — the Push API's own bearer secret for
# this browser's channel, same posture as the Gmail refresh token above.
PUSH_SUBSCRIPTION_EXPORT_COLUMNS = ["user_agent", "created"]
CREDIT_LEDGER_EXPORT_COLUMNS = ["created", "kind", "delta", "period", "props"]
PRO_WAITLIST_EXPORT_COLUMNS = ["email", "source", "created"]
PROFILE_EXPORT_COLUMNS = [
    "email", "name", "school", "school_emails", "class_year",
    "target_cycles", "regions",
    "tracks", "work_authorization", "angles", "advocate_target",
    "cadence_params", "weekly_touch_goal", "timezone", "language",
    "joined", "onboarded_at",
]


def _firm_label(contact: Contact) -> str:
    return contact.firm.name if contact.firm_id else contact.firm_text


def _json_cell(value) -> str:
    """JSON columns go into a cell as compact JSON rather than Python's repr:
    a spreadsheet shows it readably and a script can `json.loads` it back."""
    if value in (None, "", [], {}):
        return ""
    return _json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _dt(value) -> str:
    return value.isoformat() if value else ""


# The two formula-injection lead characters defusedcsv's own writer does
# NOT neutralise (checked live against defusedcsv 0.1.x: `writer.writerow`
# leaves a cell starting with either of these untouched, while it does
# prefix a leading `@`/`+`/`-`/`=`/`|`/`%` with a single quote). A
# spreadsheet strips a leading tab or carriage return on paste, before its
# own formula-detection runs, handing the character after them the same
# power as a leading `=` -- so a cell that starts with one of these still
# needs the guard applied by hand.
_UNCOVERED_FORMULA_LEAD = ("\t", "\r")


def _neutralise_tab_or_cr_lead(value):
    """The one gap in defusedcsv's own escaping (see `_UNCOVERED_FORMULA_LEAD`
    above), closed the same way defusedcsv closes the rest: prefix with a
    single quote, which defusedcsv's writer then passes through untouched
    (`'` is not in its own trigger set) rather than double-escaping.

    Not every character in an export is written by the student -- a third
    party's email Subject header reaches `Touch.note` verbatim through the
    Gmail capture path (capture/gmail_live.py builds evidence text like
    `f"Sent: {subject}"`), so anyone who can send that student mail can
    choose the first character of a cell in their export. See git log "Stop
    a stranger's email subject running as a formula in the export".
    """
    text = "" if value is None else str(value)
    if text[:1] in _UNCOVERED_FORMULA_LEAD:
        return "'" + text
    return value


def _csv(columns: list[str], rows) -> str:
    """Header + rows -> CSV text. Every builder below is this plus a query.

    `writer.writerow` is defusedcsv's, which neutralises a cell that would
    read as a spreadsheet formula (leading `@`/`+`/`-`/`=`/`|`/`%`) before it
    reaches the file -- see the module docstring's import comment. The one
    gap it leaves (a leading tab/carriage return) is closed by
    `_neutralise_tab_or_cr_lead` first, on every cell, so a new export
    builder cannot ship without either guard.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Column headers are literals declared in this module, never user text.
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_neutralise_tab_or_cr_lead(cell) for cell in row])
    return buf.getvalue()


def contacts_csv(user) -> str:
    return _csv(
        CONTACT_EXPORT_COLUMNS,
        (
            [
                c.name, c.email, c.linkedin, _firm_label(c), c.role, c.region,
                c.school, c.school_affiliation, c.warmth, c.thread_state,
                c.angle, c.opener, c.notes, c.source, c.archived, _dt(c.created),
            ]
            for c in Contact.objects.for_user(user).select_related("firm")
        ),
    )


def touches_csv(user) -> str:
    touches = (
        Touch.objects.for_user(user)
        .select_related("contact", "contact__firm")
        .order_by("ts")
    )
    return _csv(
        TOUCH_EXPORT_COLUMNS,
        (
            [
                t.contact.name if t.contact else "",
                t.contact.email if t.contact else "",
                _firm_label(t.contact) if t.contact else "",
                _dt(t.ts), t.channel or "", t.kind, t.note or "", t.source,
            ]
            for t in touches
        ),
    )


def firms_csv(user) -> str:
    """The Network board's tiering — the user's own statement of priority, and
    the single biggest input to the cadence engine's ordering."""
    rows = (
        UserFirm.objects.for_user(user).select_related("firm").order_by("firm__name")
    )
    return _csv(
        FIRM_EXPORT_COLUMNS,
        ([uf.firm.name if uf.firm_id else "", uf.tier, uf.status] for uf in rows),
    )


def applications_csv(user) -> str:
    """Tracked roles from the Opportunities feed, with the interview dates the
    student typed in — the only place those dates exist."""
    rows = (
        UserOpportunity.objects.for_user(user)
        .select_related("opportunity", "opportunity__firm")
        .order_by("-applied_at")
    )
    return _csv(
        APPLICATION_EXPORT_COLUMNS,
        (
            [
                uo.opportunity.title if uo.opportunity_id else "",
                uo.opportunity.firm.name if uo.opportunity_id else "",
                uo.opportunity.region if uo.opportunity_id else "",
                uo.opportunity.deadline.isoformat()
                if uo.opportunity_id and uo.opportunity.deadline else "",
                uo.opportunity.url if uo.opportunity_id else "",
                uo.applied_status, _dt(uo.applied_at),
                _json_cell(uo.interview_dates), uo.dismissed,
            ]
            for uo in rows
        ),
    )


def tasks_csv(user) -> str:
    rows = Task.objects.for_user(user).select_related("firm")
    return _csv(
        TASK_EXPORT_COLUMNS,
        (
            [
                t.title, t.why, t.due.isoformat() if t.due else "", t.kind,
                t.firm.name if t.firm_id else "", t.status, _dt(t.created),
            ]
            for t in rows
        ),
    )


def campaigns_csv(user) -> str:
    """The bulk sends Coverage detected, and the answer the student gave about
    each. `kind` is their own word about their own mail — an export that left
    it out would hand back the detection without the decision."""
    rows = Campaign.objects.for_user(user)
    return _csv(
        CAMPAIGN_EXPORT_COLUMNS,
        (
            [
                c.label, c.kind, c.recipient_count,
                _dt(c.first_sent), _dt(c.last_sent),
                _dt(c.classified_at) if c.classified_at else "",
            ]
            for c in rows
        ),
    )


def campaign_contacts_csv(user) -> str:
    """Who was in each campaign. `originates` carries the whole consequence of
    the answer (see `crm.models.CampaignContact`), so it is a column rather
    than something the reader has to re-derive."""
    rows = (
        CampaignContact.objects.for_user(user)
        .select_related("campaign", "contact")
    )
    return _csv(
        CAMPAIGN_CONTACT_EXPORT_COLUMNS,
        (
            [
                m.campaign.label if m.campaign_id else "",
                m.contact.name if m.contact_id else "",
                m.contact.email if m.contact_id else "",
                m.originates, _dt(m.sent_at),
            ]
            for m in rows
        ),
    )


def contact_proposals_csv(user) -> str:
    """People the mailbox scan proposed, and what the user said. Dismissed
    rows are the "never propose this person again" memory (see
    `capture.models.ContactProposal`), so they are data the user gave the
    product one tap at a time — exported, never silently dropped."""
    rows = ContactProposal.objects.for_user(user).select_related("firm")
    return _csv(
        CONTACT_PROPOSAL_EXPORT_COLUMNS,
        (
            [
                p.name, p.email,
                p.firm.name if p.firm_id else "",
                p.role_hint, p.recruiting_hint, p.evidence,
                p.thread_subject, p.threaded_reply,
                p.status, _dt(p.occurred_at), _dt(p.created), _dt(p.resolved_at),
            ]
            for p in rows
        ),
    )


def application_events_csv(user) -> str:
    """What the inbox said about each application, and what the user did
    about it. Dismissed rows are the "don't ask again" memory (see
    `capture.models.ApplicationEvent`), and `detected_by` is here on purpose:
    a student auditing a wrong row deserves to know whether a phrase list or
    a model read it."""
    rows = (
        ApplicationEvent.objects.for_user(user)
        .select_related("firm", "opportunity")
    )
    return _csv(
        APPLICATION_EVENT_EXPORT_COLUMNS,
        (
            [
                e.firm.name if e.firm_id else e.firm_text,
                e.opportunity.title if e.opportunity_id else "",
                e.get_event_type_display(), e.target_status,
                e.due_on.isoformat() if e.due_on else "", e.evidence,
                e.match_reason, e.detected_by, e.status,
                _dt(e.occurred_at), _dt(e.created), _dt(e.resolved_at),
            ]
            for e in rows
        ),
    )


def mail_facts_csv(user) -> str:
    """What the mail itself stated about people — departures, out-of-office
    returns, routing addresses — with the verbatim sentence each action stood
    on and what Coverage did about it (`capture.models.MailFact`). Dismissed
    and undone rows are the do-not-re-ask memory, exported like every other
    memory the user built one tap at a time."""
    rows = MailFact.objects.for_user(user)
    return _csv(
        MAIL_FACT_EXPORT_COLUMNS,
        (
            [
                f.get_kind_display(), f.about_name, f.about_email,
                f.new_name, f.new_email,
                f.return_on.isoformat() if f.return_on else "",
                f.quote, f.subject, f.detected_by, f.status, f.action_note,
                _dt(f.occurred_at), _dt(f.created), _dt(f.resolved_at),
            ]
            for f in rows
        ),
    )


def autopilot_runs_csv(user) -> str:
    """Every Autopilot decide pass: what it read, what it decided, what it
    cost. The audit trail behind the one-tap batch (`capture.autopilot`)."""
    rows = AutopilotRun.objects.for_user(user)
    return _csv(
        AUTOPILOT_RUN_EXPORT_COLUMNS,
        (
            [
                _dt(r.created), r.status, r.model, r.source_label,
                r.accepts, r.escalations, r.skips, r.deferred,
                r.llm_calls, r.credits_spent,
                _dt(r.decided_at), _dt(r.applied_at),
            ]
            for r in rows
        ),
    )


def autopilot_decisions_csv(user) -> str:
    """One row per Autopilot verdict, with the quote it stood on and whether
    the user overrode it — the same check-its-work surface as the log page,
    portable."""
    rows = (
        AutopilotDecision.objects.for_user(user)
        .select_related("run", "proposal", "app_event")
    )
    return _csv(
        AUTOPILOT_DECISION_EXPORT_COLUMNS,
        (
            [
                _dt(d.run.created) if d.run_id else "",
                (
                    f"{d.proposal.name} <{d.proposal.email}>"
                    if d.proposal_id else str(d.app_event or "")
                ),
                d.decision, d.confidence, d.quote, d.reason,
                d.detected_by, d.status, d.overridden,
                _dt(d.created), _dt(d.applied_at),
            ]
            for d in rows
        ),
    )


def chat_debriefs_csv(user) -> str:
    """What each coffee chat actually taught the student. Free text they wrote
    once and would have no other way to get back."""
    rows = ChatDebrief.objects.for_user(user).select_related("contact")
    return _csv(
        DEBRIEF_EXPORT_COLUMNS,
        (
            [
                _dt(d.created), d.contact.name if d.contact_id else "", d.learned,
                d.intro_name, d.intro_email,
                d.tracked_date.isoformat() if d.tracked_date else "",
                d.date_note, d.advocate_answer, d.promoted, d.dismissed,
            ]
            for d in rows
        ),
    )


def fit_scores_csv(user) -> str:
    """Derived and recomputable, but included anyway: `subject_id` alone is an
    opaque integer, so each row is resolved to the name it scored. Leaving
    this out would have meant the export page listing an exception, and an
    exception on a page whose whole claim is "everything" is worse than two
    lookups."""
    rows = list(FitScore.objects.for_user(user))
    contact_ids = {r.subject_id for r in rows if r.subject_type == "contact"}
    firm_ids = {r.subject_id for r in rows if r.subject_type == "firm"}
    names: dict[tuple[str, int], str] = {
        ("contact", c.id): c.name
        for c in Contact.objects.for_user(user).filter(id__in=contact_ids)
    }
    names.update(
        {("firm", f.id): f.name for f in Firm.objects.filter(id__in=firm_ids)}
    )
    return _csv(
        FIT_SCORE_EXPORT_COLUMNS,
        (
            [
                r.subject_type,
                names.get((r.subject_type, r.subject_id), str(r.subject_id)),
                r.composite, _json_cell(r.axes), r.reasoning,
                r.params_version, _dt(r.computed_at),
            ]
            for r in rows
        ),
    )


def imports_csv(user) -> str:
    rows = Import.objects.for_user(user)
    return _csv(
        IMPORT_EXPORT_COLUMNS,
        (
            [_dt(i.created), i.kind, i.filename, _json_cell(i.row_stats)]
            for i in rows
        ),
    )


def product_events_csv(user) -> str:
    """"What you do in the app" (legal/privacy.html) is data we hold about the
    student, so it is data the student gets back. No email bodies live here —
    see the same policy section."""
    rows = ProductEvent.objects.for_user(user)
    return _csv(
        PRODUCT_EVENT_EXPORT_COLUMNS,
        ([_dt(e.ts), e.event, _json_cell(e.props)] for e in rows),
    )


def calendar_events_csv(user) -> str:
    rows = (
        CalendarEvent.objects.for_user(user).select_related("contact").order_by("starts_at")
    )
    return _csv(
        CALENDAR_EVENT_EXPORT_COLUMNS,
        (
            [
                e.title, e.description, _dt(e.starts_at), _dt(e.ends_at), e.all_day,
                e.kind, e.source, e.contact.name if e.contact_id else "", e.location,
            ]
            for e in rows
        ),
    )


def chat_folders_csv(user) -> str:
    rows = ChatFolder.objects.for_user(user).order_by("name")
    return _csv(CHAT_FOLDER_EXPORT_COLUMNS, ([f.name, _dt(f.created)] for f in rows))


def chat_conversations_csv(user) -> str:
    rows = ChatConversation.objects.for_user(user).select_related("folder")
    return _csv(
        CHAT_CONVERSATION_EXPORT_COLUMNS,
        (
            [c.title, c.folder.name if c.folder_id else "", _dt(c.created), _dt(c.updated)]
            for c in rows
        ),
    )


def chat_messages_csv(user) -> str:
    """Every turn of every "Talk to Coverage" thread — the export's one
    genuinely large file for a heavy user, and exactly the kind of thing the
    privacy policy's "export everything" promise has to mean, since it is a
    student's own recruiting-strategy conversation, not bookkeeping."""
    rows = (
        ChatMessage.objects.for_user(user).select_related("conversation").order_by("created")
    )
    return _csv(
        CHAT_MESSAGE_EXPORT_COLUMNS,
        (
            [
                m.conversation.title or f"Conversation #{m.conversation_id}",
                m.role, m.text, _dt(m.created),
            ]
            for m in rows
        ),
    )


def advisor_memories_csv(user) -> str:
    rows = AdvisorMemory.objects.for_user(user)
    return _csv(ADVISOR_MEMORY_EXPORT_COLUMNS, ([m.text, _dt(m.created)] for m in rows))


def daily_briefs_csv(user) -> str:
    rows = DailyBrief.objects.for_user(user)
    return _csv(
        DAILY_BRIEF_EXPORT_COLUMNS,
        ([d.date.isoformat(), d.text, _dt(d.created)] for d in rows),
    )


def gmail_connection_csv(user) -> str:
    conn = GmailConnection.objects.for_user(user).first()
    rows = []
    if conn is not None:
        rows = [[
            conn.gmail_address, conn.status, _dt(conn.connected_at),
            _dt(conn.last_notification_at), conn.backfill_status, conn.rescan_status,
        ]]
    return _csv(GMAIL_CONNECTION_EXPORT_COLUMNS, rows)


def push_subscriptions_csv(user) -> str:
    rows = PushSubscription.objects.for_user(user).order_by("created")
    return _csv(PUSH_SUBSCRIPTION_EXPORT_COLUMNS, ([p.user_agent, _dt(p.created)] for p in rows))


def credit_ledger_csv(user) -> str:
    """The whole audit trail behind the Settings credit meter — every grant,
    spend, purchase, and admin adjustment. `props` carries only the audit
    detail `billing.credits`/`billing.stripe_gateway` write (thread counts,
    pack keys, Stripe event ids) — never a secret; see `CreditLedger`'s own
    docstring."""
    rows = CreditLedger.objects.for_user(user).order_by("created")
    return _csv(
        CREDIT_LEDGER_EXPORT_COLUMNS,
        ([_dt(r.created), r.kind, r.delta, r.period, _json_cell(r.props)] for r in rows),
    )


def pro_waitlist_csv(user) -> str:
    """Whether this account joined the Pro "notify me" waitlist
    (billing/models.py::ProWaitlist) — at most one row, since a join is
    deduped by email at write time. A logged-out join under the SAME email
    never links back here (ProWaitlist.user is only ever set for a join made
    while signed in), which is a limitation of email-only intent capture,
    not a gap in this export."""
    rows = ProWaitlist.objects.for_user(user)
    return _csv(
        PRO_WAITLIST_EXPORT_COLUMNS,
        ([w.email, w.source, _dt(w.created)] for w in rows),
    )


def profile_csv(user) -> str:
    """The `users` row itself — one header, one row. Everything Settings can
    set, including the answers that feed the fit score (`work_authorization`)
    and the engine (`cadence_params`, `advocate_target`, `weekly_touch_goal`).
    Never the password hash, and never the session."""
    assets = user.assets or {}
    return _csv(
        PROFILE_EXPORT_COLUMNS,
        [[
            user.email,
            user.name,
            user.school,
            _json_cell(list(user.school_emails or [])),
            user.class_year if user.class_year else "",
            _json_cell(list(user.target_cycles or [])),
            _json_cell(list(user.regions or [])),
            _json_cell(list(user.tracks or [])),
            _json_cell(user.work_authorization),
            _json_cell(assets.get("angles")),
            assets.get("advocate_target", ""),
            _json_cell(user.cadence_params),
            user.weekly_touch_goal if user.weekly_touch_goal else "",
            getattr(user, "timezone", "") or "",
            user.language,
            _dt(user.created),
            _dt(user.onboarded_at),
        ]],
    )


# Filename -> (builder, one-line description shown on the export page and in
# the ZIP's own README). Order is the order they're listed to the user:
# the things a student thinks of as "my data" first, bookkeeping last.
EXPORT_FILES: list[tuple[str, object, str]] = [
    ("contacts.csv", contacts_csv,
     "Every person in your CRM, including archived ones (flagged, not dropped)."),
    ("touches.csv", touches_csv,
     "Every logged interaction, with the contact and firm it belongs to."),
    ("firms.csv", firms_csv, "Your target firms and the tier you gave each."),
    ("applications.csv", applications_csv,
     "Roles you're tracking, their status, and any interview dates you added."),
    ("tasks.csv", tasks_csv, "Your tasks, open and done."),
    ("chat_debriefs.csv", chat_debriefs_csv,
     "What each coffee chat taught you, in your own words."),
    ("campaigns.csv", campaigns_csv,
     "Bulk sends we found in your mail, and what you said each one was."),
    ("campaign_contacts.csv", campaign_contacts_csv,
     "Who was in each bulk send, and whose relationship started there."),
    ("contact_proposals.csv", contact_proposals_csv,
     "People your inbox scan suggested, and what you decided about each."),
    ("application_events.csv", application_events_csv,
     "Application updates found in your mail, and what you decided about each."),
    ("mail_facts.csv", mail_facts_csv,
     "Facts your mail stated about people (departures, out-of-office, new "
     "addresses), the quoted sentence behind each, and what was done."),
    ("autopilot_runs.csv", autopilot_runs_csv,
     "Every Autopilot decide pass, with its counts and cost."),
    ("autopilot_decisions.csv", autopilot_decisions_csv,
     "Every Autopilot verdict, the quote it stood on, and whether you "
     "overrode it."),
    ("fit_scores.csv", fit_scores_csv,
     "Computed fit scores with the axes behind each one."),
    ("imports.csv", imports_csv, "Your CSV imports and what each one did."),
    ("calendar_events.csv", calendar_events_csv,
     "Coffee chats and events on your calendar, captured or hand-added."),
    ("chat_folders.csv", chat_folders_csv,
     "Your own groupings of Talk to Coverage conversations."),
    ("chat_conversations.csv", chat_conversations_csv,
     "Every Talk to Coverage conversation you've started, with its folder."),
    ("chat_messages.csv", chat_messages_csv,
     "Every message in every Talk to Coverage conversation."),
    ("advisor_memories.csv", advisor_memories_csv,
     "Facts the advisor has remembered about your search, in its own words."),
    ("daily_briefs.csv", daily_briefs_csv,
     "The advisor's daily briefing paragraph, one per day it ran."),
    ("gmail_connection.csv", gmail_connection_csv,
     "Your connected Gmail address and its sync status (never the access "
     "token itself)."),
    ("push_subscriptions.csv", push_subscriptions_csv,
     "Browsers/devices subscribed to deadline push alerts (never the "
     "subscription's own keys)."),
    ("credit_ledger.csv", credit_ledger_csv,
     "Every credit grant, spend, purchase, and adjustment on your account."),
    ("product_events.csv", product_events_csv,
     "Your own usage events — which pages and actions you used."),
    ("pro_waitlist.csv", pro_waitlist_csv,
     "Whether you joined the Pro launch waitlist, and when."),
    ("profile.csv", profile_csv,
     "Your profile row: school, cycle, regions, work authorization, angles, "
     "and every engine setting."),
]

# Named out loud rather than quietly skipped, on the page and in the README —
# rule D4: "everything" is only written when it is everything.
EXPORT_EXCLUSIONS: list[str] = [
    "Your password (we only ever store a one-way hash of it).",
    "Shared directory data — firms, roles, deadlines. It isn't yours; it's "
    "the same for every user.",
]


def export_manifest() -> list[tuple[str, str]]:
    """(filename, description) for the export page. Reads EXPORT_FILES so the
    page can never list a file the ZIP doesn't contain, or miss one it does."""
    return [(name, desc) for name, _builder, desc in EXPORT_FILES]


def _export_readme() -> str:
    lines = [
        "Coverage — your data",
        "=" * 20,
        "",
        "One CSV per table. Every row in here is yours; nothing is shared with",
        "any other user. JSON columns are written as compact JSON in one cell.",
        "",
        "Files",
        "-----",
    ]
    lines += [f"  {name:<22} {desc}" for name, desc in export_manifest()]
    lines += ["", "Not in this archive", "-------------------"]
    lines += [f"  - {item}" for item in EXPORT_EXCLUSIONS]
    lines.append("")
    return "\n".join(lines)


def export_zip(user) -> bytes:
    """Every CSV above plus a README, as one ZIP. Built in memory: the whole
    archive is a few hundred KB even for a heavy user (the founder's 137
    contacts / 131 touches come to well under 100 KB), so streaming it to a
    temp file would buy nothing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _export_readme())
        for name, builder, _desc in EXPORT_FILES:
            zf.writestr(name, builder(user))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Self-serve deletion (§10 — a real, hard-delete path)
# ---------------------------------------------------------------------------
# Children before parents so each per-model count is the rows that model
# actually owns (touches are deleted before contacts they reference, so no
# cascade double-counts them).
_DELETE_ORDER: list[tuple[str, type]] = [
    # `chat_debriefs` was already swept — it CASCADEs from both `touch` and
    # `contact` — but it was swept invisibly, so the returned counts (which the
    # goodbye flash now shows the user) under-reported by however many
    # debriefs they'd written. Deleting it explicitly, first, makes the receipt
    # honest; the cascade is now a no-op rather than the mechanism.
    ("chat_debriefs", ChatDebrief),
    # References both `campaign` and `contact` (CASCADE from either), so it
    # goes before both — same children-before-parents rule as everything here.
    ("campaign_contacts", CampaignContact),
    ("campaigns", Campaign),
    ("touches", Touch),
    ("fit_scores", FitScore),
    ("tasks", Task),
    # References `contact` (CASCADE) — deleted before `contacts` below for the
    # same "children before parents" reason as everything above it.
    ("calendar_events", CalendarEvent),
    # References `contact` and `contact_proposal` (both SET_NULL) — kept
    # child-before-parent like everything here, and its `quote` column is
    # the one sentence of mail body the product deliberately stores.
    ("mail_facts", MailFact),
    # MUST precede `contact_proposals` and `application_events`: its FKs to
    # both are on_delete=PROTECT (a decision is the audit trail for the row
    # it judged), so deleting either parent first raises ProtectedError and
    # 500s the whole account deletion. Also references `run` (CASCADE) and
    # `contact` (SET_NULL).
    ("autopilot_decisions", AutopilotDecision),
    ("autopilot_runs", AutopilotRun),
    # References `contact` (SET_NULL, so the order doesn't move the counts —
    # kept child-before-parent anyway, like the trio below), and it holds the
    # "never propose this address again" memory, which is the user's data
    # like everything else here.
    ("contact_proposals", ContactProposal),
    # References `opportunity` (CASCADE) and `firm` (SET_NULL) — before
    # `user_opportunities` below only for readability's sake (they are
    # siblings, not parent and child), and it holds the "don't propose this
    # role update again" memory, which is the user's data like everything
    # else here.
    ("application_events", ApplicationEvent),
    ("user_firms", UserFirm),
    ("user_opportunities", UserOpportunity),
    ("imports", Import),
    ("contacts", Contact),
    # Talk to Coverage: messages reference `conversation` (CASCADE), and
    # `conversation` optionally references `folder` (SET_NULL, so this trio's
    # OWN relative order among themselves doesn't affect the counts the way
    # `contact`'s children above do — kept messages-then-conversations-then-
    # folders anyway, for the same readability the rest of this list follows).
    ("chat_messages", ChatMessage),
    ("chat_conversations", ChatConversation),
    ("chat_folders", ChatFolder),
    ("advisor_memories", AdvisorMemory),
    ("daily_briefs", DailyBrief),
    ("gmail_connection", GmailConnection),
    ("push_subscriptions", PushSubscription),
    ("credit_ledger", CreditLedger),
    ("product_events", ProductEvent),
    ("pro_waitlist", ProWaitlist),
]


def delete_user_and_data(user) -> dict[str, int]:
    """Hard-delete every private-zone row belonging to `user`, then the
    account itself. Returns a per-table count of what was removed.

    Each per-model delete is scoped with `.for_user(user)`, so it is
    structurally incapable of touching another tenant's rows. The final
    `user.delete()` is a belt-and-suspenders cascade that also sweeps
    allauth's own per-user rows (email addresses, social tokens).
    """
    counts: dict[str, int] = {}
    with transaction.atomic():
        for label, model in _DELETE_ORDER:
            deleted, _ = model.objects.for_user(user).delete()
            counts[label] = deleted
        counts["account"] = 1
        user.delete()
    return counts
