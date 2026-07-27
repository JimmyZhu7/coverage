"""Business logic for onboarding, CSV import/export, and self-serve
deletion (task M5; docs/build-plan.md §7 M5, §8, §10).

Kept out of views.py so each piece is unit-testable without an HTTP
request. Everything here writes private-zone rows through the explicit
`all_objects` manager (creation with a known user) or reads through
`.objects.for_user(user)` (tenant-scoped) — the contract from
coverage_web/tenancy.py. Import/export use only the stdlib `csv` module
(no pandas), per the task's hard constraint.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from analytics.events import record_event
from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from crm.models import CaptureEvent, Contact, Task, Touch, UserFirm
from directory.models import Firm

# The tier assigned to every firm a user picks during onboarding. The plan
# (docs/build-plan.md §2) declares `user_firms.tier smallint` but leaves its
# semantics open; the task says "with a tier default". 2 is chosen as a
# neutral middle tier — it leaves tier 1 free as a later, manual "top
# priority" promotion and tier 3+ for stretch/backup firms — and pairs with
# status="target". Documented as a decision in the build report.
DEFAULT_FIRM_TIER = 2


# ---------------------------------------------------------------------------
# Capture address
# ---------------------------------------------------------------------------
def capture_address(user) -> str:
    """The per-user inbound capture address, `u-<slug>@<domain>` (§5).

    Domain is read via `getattr` with the documented default so the app
    works before `CAPTURE_INBOUND_DOMAIN` is set in any environment.
    """
    domain = getattr(settings, "CAPTURE_INBOUND_DOMAIN", "in.coverage.app")
    return f"u-{user.capture_slug}@{domain}"


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
    both for header matching and for building dedup / firm-match keys, so
    'J.P. Morgan' and 'JPMorgan' collapse to the same token."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


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
class ImportResult:
    created: int = 0
    skipped_duplicate: int = 0
    skipped_empty: int = 0
    firm_matched: int = 0
    total_rows: int = 0
    unmatched_columns: bool = False
    errors: list[str] = field(default_factory=list)

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
        }


def _firm_lookup() -> dict[str, Firm]:
    """Normalized firm name AND slug -> Firm, for text->firm matching."""
    lookup: dict[str, Firm] = {}
    for firm in Firm.objects.all():
        lookup[_norm(firm.name)] = firm
        lookup[_norm(firm.slug)] = firm
    return lookup


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

        matched_firm = firms.get(_norm(firm_raw)) if firm_raw else None

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
        Contact.all_objects.bulk_create(to_create)
        result.created = len(to_create)
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
    return result


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
# CSV export (the user's own data, portable — §10 trust feature)
# ---------------------------------------------------------------------------
CONTACT_EXPORT_COLUMNS = [
    "name", "email", "firm", "role", "region", "warmth", "thread_state",
    "angle", "opener", "notes", "source", "created",
]
TOUCH_EXPORT_COLUMNS = [
    "contact_name", "contact_email", "firm", "ts", "channel",
    "kind", "note", "source",
]


def _firm_label(contact: Contact) -> str:
    return contact.firm.name if contact.firm_id else contact.firm_text


def contacts_csv(user) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CONTACT_EXPORT_COLUMNS)
    for c in Contact.objects.for_user(user).select_related("firm"):
        writer.writerow(
            [
                c.name,
                c.email,
                _firm_label(c),
                c.role,
                c.region,
                c.warmth,
                c.thread_state,
                c.angle,
                c.opener,
                c.notes,
                c.source,
                c.created.isoformat() if c.created else "",
            ]
        )
    return buf.getvalue()


def touches_csv(user) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TOUCH_EXPORT_COLUMNS)
    touches = (
        Touch.objects.for_user(user)
        .select_related("contact", "contact__firm")
        .order_by("ts")
    )
    for t in touches:
        contact = t.contact
        writer.writerow(
            [
                contact.name if contact else "",
                contact.email if contact else "",
                _firm_label(contact) if contact else "",
                t.ts.isoformat() if t.ts else "",
                t.channel or "",
                t.kind,
                t.note or "",
                t.source,
            ]
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Self-serve deletion (§10 — a real, hard-delete path)
# ---------------------------------------------------------------------------
# Children before parents so each per-model count is the rows that model
# actually owns (touches are deleted before contacts/capture_events they
# reference, so no cascade double-counts them).
_DELETE_ORDER: list[tuple[str, type]] = [
    ("touches", Touch),
    ("fit_scores", FitScore),
    ("tasks", Task),
    ("user_firms", UserFirm),
    ("user_opportunities", UserOpportunity),
    ("imports", Import),
    ("capture_events", CaptureEvent),
    ("contacts", Contact),
    ("product_events", ProductEvent),
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
