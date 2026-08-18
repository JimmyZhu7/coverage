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
from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from crm.models import ChatDebrief, Contact, Task, Touch, UserFirm
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
        assets={"angles": ["Rowed in college"], "advocate_target": 3},
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
    Contact.all_objects.create(
        user=student, name="Archived Person", firm=firm,
        email="archived@test-bank.com", archived=True,
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
        "chat_debriefs": "chat_debriefs.csv",
        "fit_scores": "fit_scores.csv",
        "imports": "imports.csv",
        "product_events": "product_events.csv",
    }
    for label, _model in services._DELETE_ORDER:
        assert label in covered, (
            f"`{label}` is deleted by delete_user_and_data but no export file "
            "claims it. Add a CSV builder, or the export's 'everything' is a lie."
        )
        assert covered[label] in exported


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
    row = _rows(_zip(student), "profile.csv")[0]
    assert row["email"] == "exporter@example.com"
    assert row["school"] == "Test University"
    assert "Rowed in college" in row["angles"]
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
