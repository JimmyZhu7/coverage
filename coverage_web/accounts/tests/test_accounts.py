"""Tests for the accounts app (task M5): onboarding, CSV import + dedup,
export, self-serve deletion (with a cross-tenant scope guard), and the
legal pages. Runs against the migrated schema (the same tables the
`coverage_acct` DB carries).

Uses the standard pytest-django `db` fixture: everything here goes through
the Django ORM on Django's own connection (unlike crm.services, which opens
a separate psycopg connection and therefore needs transaction=True).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from accounts import services
from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from crm.models import CaptureEvent, Contact, Task, Touch, UserFirm
from directory.models import Firm, Opportunity

User = get_user_model()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def user(db):
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="x")


@pytest.fixture
def firms(db):
    return {
        "gs": Firm.objects.create(slug="goldman-sachs", name="Goldman Sachs",
                                  regions=["us"], tracks=["ib"]),
        "jpm": Firm.objects.create(slug="jpmorgan", name="JPMorgan",
                                   regions=["us", "hk"], tracks=["ib"]),
    }


# ---------------------------------------------------------------------------
# capture address
# ---------------------------------------------------------------------------
def test_capture_address_format(user):
    addr = services.capture_address(user)
    assert addr == f"u-{user.capture_slug}@in.coverage.app"


# ---------------------------------------------------------------------------
# onboarding wizard
# ---------------------------------------------------------------------------
def test_onboarding_requires_login(client):
    resp = client.get(reverse("accounts:onboarding"))
    assert resp.status_code == 302
    assert "/accounts/login/" in resp["Location"]


def test_onboarding_profile_step_saves_and_advances(client, user):
    client.force_login(user)
    resp = client.post(
        reverse("accounts:onboarding") + "?step=profile",
        {
            "step": "profile",
            "school": "State U",
            "class_year": "2028",
            # Must be one of forms.CYCLE_CHOICES — the cycle field is a strict
            # checkbox group now, not free text.
            "target_cycles": ["2027 Summer Internship"],
            "regions": ["us", "hk"],
            "tracks": ["ib", "pe"],
        },
    )
    assert resp.status_code == 302
    # Work authorization now sits between profile and firms — it's profile data,
    # and the firm picker is the first place the score it feeds shows up.
    assert "step=work_auth" in resp["Location"]
    user.refresh_from_db()
    assert user.school == "State U"
    assert user.class_year == 2028
    assert user.target_cycles == ["2027 Summer Internship"]
    assert set(user.regions) == {"us", "hk"}
    assert set(user.tracks) == {"ib", "pe"}


def test_onboarding_firms_step_creates_userfirms_with_default_tier(client, user, firms):
    client.force_login(user)
    resp = client.post(
        reverse("accounts:onboarding") + "?step=firms",
        {"step": "firms", "firms": [firms["gs"].id, firms["jpm"].id]},
    )
    assert resp.status_code == 302
    # The firms step hands off to import. Tiering is NOT asked here any more:
    # ranking firms you picked ten seconds ago, before seeing a deadline or a
    # contact, is a judgement nobody can make — it lives on the Network page
    # as a drag, with the board visible.
    assert "step=import" in resp["Location"]
    rows = list(UserFirm.objects.for_user(user))
    assert {r.firm_id for r in rows} == {firms["gs"].id, firms["jpm"].id}
    assert all(r.tier == services.DEFAULT_FIRM_TIER for r in rows)
    assert all(r.status == "target" for r in rows)


def test_onboarding_firms_step_is_idempotent_sync(client, user, firms):
    """Re-submitting with a different selection reflects the new set, not a
    pile-up (set_target_firms syncs)."""
    services.set_target_firms(user, [firms["gs"].id, firms["jpm"].id])
    assert UserFirm.objects.for_user(user).count() == 2
    # Re-run with only one firm selected.
    services.set_target_firms(user, [firms["gs"].id])
    remaining = list(UserFirm.objects.for_user(user).values_list("firm_id", flat=True))
    assert remaining == [firms["gs"].id]


def test_onboarding_capture_step_sets_onboarded_at_and_records_event(client, user):
    client.force_login(user)
    assert user.onboarded_at is None
    resp = client.post(
        reverse("accounts:onboarding") + "?step=capture", {"step": "capture"}
    )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.onboarded_at is not None
    assert ProductEvent.all_objects.filter(user=user, event="onboarded").exists()


def test_onboarding_capture_step_is_not_double_counted(client, user):
    client.force_login(user)
    client.post(reverse("accounts:onboarding") + "?step=capture", {"step": "capture"})
    user.refresh_from_db()
    stamp = user.onboarded_at
    # Hitting finish again must not move onboarded_at or re-emit the event.
    client.post(reverse("accounts:onboarding") + "?step=capture", {"step": "capture"})
    user.refresh_from_db()
    assert user.onboarded_at == stamp
    assert ProductEvent.all_objects.filter(user=user, event="onboarded").count() == 1


def test_bare_welcome_redirects_onboarded_user_to_settings(client, user):
    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    client.force_login(user)
    resp = client.get(reverse("accounts:onboarding"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("accounts:settings")


def test_bare_welcome_shows_wizard_for_new_user(client, user):
    client.force_login(user)
    resp = client.get(reverse("accounts:onboarding"))
    assert resp.status_code == 200


def test_onboarding_all_steps_render(client, user, firms):
    client.force_login(user)
    for step in ["profile", "work_auth", "firms", "survey", "assets", "import", "capture"]:
        resp = client.get(reverse("accounts:onboarding") + f"?step={step}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CSV import + dedup + firm matching
# ---------------------------------------------------------------------------
CSV_BASIC = (
    "name,email,firm,role,notes,angle\n"
    "Jane Banker,jane@gs.com,Goldman Sachs,Analyst,met at info session,rowing\n"
    "Bob Trader,bob@jpm.com,JPMorgan,Associate,,\n"
)


def test_import_creates_contacts_and_matches_firms(user, firms):
    result = services.parse_contacts_csv(user, CSV_BASIC)
    assert result.created == 2
    assert result.firm_matched == 2
    jane = Contact.objects.for_user(user).get(name="Jane Banker")
    assert jane.firm_id == firms["gs"].id
    assert jane.firm_text == ""
    assert jane.email == "jane@gs.com"
    assert jane.role == "Analyst"
    assert jane.source == "import"


def test_import_falls_back_to_firm_text_when_unmatched(user):
    result = services.parse_contacts_csv(
        user, "name,firm\nCarol Buyside,Some Tiny Fund LP\n"
    )
    assert result.created == 1
    carol = Contact.objects.for_user(user).get(name="Carol Buyside")
    assert carol.firm_id is None
    assert carol.firm_text == "Some Tiny Fund LP"


def test_import_tolerates_column_variants(user, firms):
    csv_variant = (
        "Full Name,E-mail,Company,Job Title,Comments,Hook\n"
        "Dana Deal,dana@gs.com,goldman sachs,VP,follow up,alumni\n"
    )
    result = services.parse_contacts_csv(user, csv_variant)
    assert result.created == 1
    dana = Contact.objects.for_user(user).get(name="Dana Deal")
    assert dana.firm_id == firms["gs"].id  # "goldman sachs" -> Goldman Sachs
    assert dana.role == "VP"
    assert dana.notes == "follow up"
    assert dana.angle == "alumni"


def test_import_dedup_by_email_on_reimport(user, firms):
    first = services.parse_contacts_csv(user, CSV_BASIC)
    assert first.created == 2
    # Re-import the exact same file: nothing new.
    second = services.parse_contacts_csv(user, CSV_BASIC)
    assert second.created == 0
    assert second.skipped_duplicate == 2
    assert Contact.objects.for_user(user).count() == 2


def test_import_dedup_by_name_and_firm_when_no_email(user, firms):
    csv_no_email = "name,firm\nNo Email Person,Goldman Sachs\n"
    services.parse_contacts_csv(user, csv_no_email)
    second = services.parse_contacts_csv(user, csv_no_email)
    assert second.created == 0
    assert second.skipped_duplicate == 1
    assert Contact.objects.for_user(user).count() == 1


def test_import_dedup_within_a_single_file(user):
    csv_dupes = (
        "name,email\n"
        "Same Person,same@x.com\n"
        "Same Person,same@x.com\n"
    )
    result = services.parse_contacts_csv(user, csv_dupes)
    assert result.created == 1
    assert result.skipped_duplicate == 1


def test_import_skips_empty_rows(user):
    csv_empty = "name,email\n,\nReal Person,real@x.com\n"
    result = services.parse_contacts_csv(user, csv_empty)
    assert result.created == 1
    assert result.skipped_empty == 1


def test_import_unrecognized_columns_reports_error(user):
    result = services.parse_contacts_csv(user, "foo,bar\n1,2\n")
    assert result.created == 0
    assert result.unmatched_columns
    assert result.errors


def test_import_writes_import_row_and_event(user, firms):
    from django.core.files.uploadedfile import SimpleUploadedFile  # noqa

    result = services.import_contacts(
        user, file_bytes=CSV_BASIC.encode("utf-8"), filename="my.csv"
    )
    assert result.created == 2
    imp = Import.all_objects.get(user=user)
    assert imp.filename == "my.csv"
    assert imp.row_stats["created"] == 2
    assert ProductEvent.all_objects.filter(user=user, event="import_completed").exists()


def test_import_of_an_all_duplicate_file_does_not_fire_import_completed(user, firms):
    """B5: `import_completed` is a named funnel event and must mean the
    import actually created rows. An import that creates nothing (every row
    a duplicate) used to fire it anyway — indistinguishable in the funnel
    from a real import. It must fire `import_failed` instead."""
    services.import_contacts(user, file_bytes=CSV_BASIC.encode("utf-8"), filename="first.csv")
    assert ProductEvent.all_objects.filter(user=user, event="import_completed").count() == 1

    result = services.import_contacts(user, file_bytes=CSV_BASIC.encode("utf-8"), filename="again.csv")
    assert result.created == 0
    # Still only the one `import_completed` from the first, real import.
    assert ProductEvent.all_objects.filter(user=user, event="import_completed").count() == 1
    assert ProductEvent.all_objects.filter(user=user, event="import_failed").exists()


def test_import_of_an_unreadable_csv_fires_import_failed_not_completed(user):
    result = services.import_contacts(user, file_bytes=b"foo,bar\n1,2\n", filename="bad.csv")
    assert result.created == 0
    assert not ProductEvent.all_objects.filter(user=user, event="import_completed").exists()
    assert ProductEvent.all_objects.filter(user=user, event="import_failed").exists()


def test_import_view_upload_flow(client, user, firms):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(user)
    upload = SimpleUploadedFile("contacts.csv", CSV_BASIC.encode("utf-8"), content_type="text/csv")
    resp = client.post(reverse("accounts:import"), {"file": upload})
    assert resp.status_code == 200
    assert Contact.objects.for_user(user).count() == 2


def test_import_template_downloads(client, user):
    client.force_login(user)
    resp = client.get(reverse("accounts:import_template"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv"
    assert b"name,email,firm,role,notes,angle" in resp.content


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def test_export_contacts_returns_user_rows(client, user, other_user, firms):
    Contact.all_objects.create(user=user, name="Mine", email="mine@x.com", firm=firms["gs"])
    Contact.all_objects.create(user=other_user, name="Theirs", email="theirs@x.com")
    client.force_login(user)
    resp = client.get(reverse("accounts:export") + "?kind=contacts")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Mine" in body
    assert "Goldman Sachs" in body
    assert "Theirs" not in body  # never another tenant's rows


def test_export_touches_returns_user_rows(client, user, firms):
    c = Contact.all_objects.create(user=user, name="Mine", firm=firms["gs"])
    Touch.all_objects.create(user=user, contact=c, ts=timezone.now(), kind="outreach", channel="email")
    client.force_login(user)
    resp = client.get(reverse("accounts:export") + "?kind=touches")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "outreach" in body
    assert "Mine" in body


def test_export_landing_page_renders(client, user):
    client.force_login(user)
    resp = client.get(reverse("accounts:export"))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_settings_shows_capture_address(client, user):
    client.force_login(user)
    resp = client.get(reverse("accounts:settings"))
    assert resp.status_code == 200
    assert services.capture_address(user).encode() in resp.content


def test_settings_htmx_save_returns_partial(client, user):
    client.force_login(user)
    resp = client.post(
        reverse("accounts:settings"),
        {"section": "profile", "school": "HX School", "class_year": "2028",
         "target_cycles": [], "regions": ["us"], "tracks": ["ib"]},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "<html" not in body.lower()  # a fragment, not a full page
    assert "Profile saved" in body
    user.refresh_from_db()
    assert user.school == "HX School"


def test_settings_saves_profile(client, user):
    client.force_login(user)
    resp = client.post(
        reverse("accounts:settings"),
        {"section": "profile", "school": "New School", "class_year": "2029",
         "target_cycles": [], "regions": ["hk"], "tracks": ["consulting"]},
    )
    assert resp.status_code in (200, 302)
    user.refresh_from_db()
    assert user.school == "New School"
    assert user.class_year == 2029
    assert set(user.regions) == {"hk"}


def test_settings_post_without_a_recognised_section_is_a_noop(client, user):
    """Regression for the bug B2 fixes: every ProfileForm field is
    `required=False`, so before the explicit `section="profile"` marker was
    required, ANY POST that failed to name one of SECTION_FORMS's keys fell
    straight through to `ProfileForm(request.POST, ...)` — including a POST
    naming no section at all, or a stale/misspelled one. An empty POST
    validated as a legitimate (if blank) profile save and silently erased
    every profile field. It must now be a no-op instead."""
    user.school = "Original School"
    user.class_year = 2027
    user.save(update_fields=["school", "class_year"])

    resp = client.post(reverse("accounts:settings"), {})  # not logged in yet
    assert resp.status_code == 302  # login redirect, not even reaching the view

    client.force_login(user)
    resp = client.post(reverse("accounts:settings"), {})
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.school == "Original School"
    assert user.class_year == 2027

    # A stale/unrecognised section name is treated the same way.
    resp = client.post(
        reverse("accounts:settings"),
        {"section": "not-a-real-section", "school": "Should Not Save"},
    )
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.school == "Original School"


# ---------------------------------------------------------------------------
# deletion — the real path + cross-tenant scope guard
# ---------------------------------------------------------------------------
def _populate_private_rows(u, firm, opportunity):
    contact = Contact.all_objects.create(user=u, name=f"C-{u.email}")
    Touch.all_objects.create(user=u, contact=contact, ts=timezone.now(), kind="outreach", channel="email")
    UserFirm.all_objects.create(user=u, firm=firm)
    CaptureEvent.all_objects.create(user=u, provider="bcc", provider_ref=f"ref-{u.email}")
    Task.all_objects.create(user=u, title="Follow up")
    FitScore.all_objects.create(user=u, subject_type="contact", subject_id=contact.id)
    UserOpportunity.all_objects.create(user=u, opportunity=opportunity)
    ProductEvent.all_objects.create(user=u, event="signup")
    Import.all_objects.create(user=u, kind="csv", filename="x.csv")


def test_delete_removes_all_of_a_users_private_data(db, user, other_user):
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(firm=firm, url="https://acme.example/j", title="Analyst")
    _populate_private_rows(user, firm, opp)
    _populate_private_rows(other_user, firm, opp)

    user_id = user.id
    counts = services.delete_user_and_data(user)

    # Every table reported at least the one row we created.
    for label in ["touches", "contacts", "user_firms", "capture_events", "tasks",
                  "fit_scores", "user_opportunities", "product_events", "imports"]:
        assert counts[label] >= 1, label

    # The user is gone.
    assert not User.objects.filter(id=user_id).exists()
    # Every private table is empty for the deleted user.
    assert not Contact.all_objects.filter(user_id=user_id).exists()
    assert not Touch.all_objects.filter(user_id=user_id).exists()
    assert not UserFirm.all_objects.filter(user_id=user_id).exists()
    assert not CaptureEvent.all_objects.filter(user_id=user_id).exists()
    assert not Task.all_objects.filter(user_id=user_id).exists()
    assert not FitScore.all_objects.filter(user_id=user_id).exists()
    assert not UserOpportunity.all_objects.filter(user_id=user_id).exists()
    assert not ProductEvent.all_objects.filter(user_id=user_id).exists()
    assert not Import.all_objects.filter(user_id=user_id).exists()


def test_delete_does_not_touch_another_users_data(db, user, other_user):
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(firm=firm, url="https://acme.example/j", title="Analyst")
    _populate_private_rows(user, firm, opp)
    _populate_private_rows(other_user, firm, opp)

    services.delete_user_and_data(user)

    # other_user is fully intact.
    assert User.objects.filter(id=other_user.id).exists()
    assert Contact.all_objects.filter(user=other_user).count() == 1
    assert Touch.all_objects.filter(user=other_user).count() == 1
    assert UserFirm.all_objects.filter(user=other_user).count() == 1
    assert CaptureEvent.all_objects.filter(user=other_user).count() == 1
    assert Task.all_objects.filter(user=other_user).count() == 1
    assert FitScore.all_objects.filter(user=other_user).count() == 1
    assert UserOpportunity.all_objects.filter(user=other_user).count() == 1
    assert ProductEvent.all_objects.filter(user=other_user).count() == 1
    assert Import.all_objects.filter(user=other_user).count() == 1


def test_delete_view_requires_typed_confirmation(client, user):
    client.force_login(user)
    # Wrong confirmation → no deletion.
    resp = client.post(reverse("accounts:delete"), {"confirm": "not my email"})
    assert resp.status_code == 200
    assert User.objects.filter(id=user.id).exists()


def test_delete_view_with_correct_email_deletes(client, user):
    Contact.all_objects.create(user=user, name="Mine")
    client.force_login(user)
    resp = client.post(reverse("accounts:delete"), {"confirm": user.email})
    assert resp.status_code == 302
    assert not User.objects.filter(id=user.id).exists()


# ---------------------------------------------------------------------------
# legal pages (no login required)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["accounts:privacy", "accounts:terms"])
def test_legal_pages_200_without_login(client, db, name):
    resp = client.get(reverse(name))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# signup funnel event
# ---------------------------------------------------------------------------
def test_signup_signal_records_event(db):
    from allauth.account.signals import user_signed_up

    u = User.objects.create_user(email="new@example.com", password="x")
    user_signed_up.send(sender=User, request=None, user=u)
    assert ProductEvent.all_objects.filter(user=u, event="signup").exists()
