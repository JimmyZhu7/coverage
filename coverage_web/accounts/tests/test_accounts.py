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
from crm.models import Contact, Task, Touch, UserFirm
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


def test_onboarding_import_step_sets_onboarded_at_and_records_event(client, user):
    client.force_login(user)
    assert user.onboarded_at is None
    resp = client.post(
        reverse("accounts:onboarding") + "?step=import", {"step": "import"}
    )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.onboarded_at is not None
    assert ProductEvent.all_objects.filter(user=user, event="onboarded").exists()


def test_onboarding_import_step_is_not_double_counted(client, user):
    client.force_login(user)
    client.post(reverse("accounts:onboarding") + "?step=import", {"step": "import"})
    user.refresh_from_db()
    stamp = user.onboarded_at
    # Hitting finish again must not move onboarded_at or re-emit the event.
    client.post(reverse("accounts:onboarding") + "?step=import", {"step": "import"})
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
    for step in ["profile", "work_auth", "firms", "import"]:
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
# normalize_firm_name — the pure normalizer (B6: "Bain and Company" vs
# "Bain & Company" silently failing to match)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "left,right",
    [
        ("Bain & Company", "Bain and Company"),
        ("Bain & Company", "bain & company"),
        ("Bain & Company", "Bain+Company"),
        ("Goldman Sachs", "goldman sachs"),
        ("Goldman Sachs", "GOLDMAN SACHS"),
        ("Morgan Stanley & Co.", "Morgan Stanley"),
        ("Morgan Stanley & Co", "Morgan Stanley"),
        ("Rothschild & Co", "Rothschild and Co."),
        ("McKinsey & Company", "McKinsey"),
        ("J.P. Morgan", "JP Morgan"),
        ("J.P. Morgan", "jp morgan"),
        ("  Bain   &   Company  ", "Bain & Company"),
    ],
)
def test_normalize_firm_name_treats_equivalent_spellings_as_equal(left, right):
    assert services.normalize_firm_name(left) == services.normalize_firm_name(right)


@pytest.mark.parametrize(
    "left,right",
    [
        # The exact near-collision the bug report was built on: stripping
        # "Company" as a legal suffix must never eat "Capital" too.
        ("Bain & Company", "Bain Capital"),
        ("Morgan Stanley", "J.P. Morgan"),
        ("Goldman Sachs", "Goldman Sachs Asset Management"),
        ("PJT Partners", "PJT Capital"),
    ],
)
def test_normalize_firm_name_does_not_merge_distinct_firms(left, right):
    assert services.normalize_firm_name(left) != services.normalize_firm_name(right)


def test_normalize_firm_name_blank_is_blank():
    assert services.normalize_firm_name("") == ""
    assert services.normalize_firm_name(None) == ""


def test_normalize_firm_name_never_strips_down_to_nothing():
    # A firm whose entire name IS a suffix word (edge case, not a real
    # directory entry) must not normalize to "".
    assert services.normalize_firm_name("Group") == "group"
    assert services.normalize_firm_name("Partners") == "partners"


def test_normalize_firm_name_is_collision_free_across_the_live_directory(db):
    """The strongest guarantee this function makes: run it over every real
    firm in the directory and confirm no two distinct firms ever produce the
    same key. Guards against a future suffix/joiner addition accidentally
    merging two real firms (e.g. the ~10 "... Partners" firms in the
    directory collapsing onto each other)."""
    names = list(Firm.objects.values_list("name", flat=True))
    if not names:
        pytest.skip("directory not seeded in this test DB")
    keys: dict[str, str] = {}
    collisions = []
    for name in names:
        key = services.normalize_firm_name(name)
        if key in keys and keys[key] != name:
            collisions.append((keys[key], name, key))
        keys.setdefault(key, name)
    assert collisions == []


# ---------------------------------------------------------------------------
# Forgiving firm matching during import (B6)
# ---------------------------------------------------------------------------
@pytest.fixture
def near_collision_firms(db):
    """The real near-collision pair from the bug report, created directly
    (not via the shared `firms` fixture) so these tests don't perturb the
    firm-picker/onboarding tests that assume exactly gs/jpm exist."""
    return {
        "bain_co": Firm.objects.create(slug="bain", name="Bain & Company"),
        "bain_cap": Firm.objects.create(slug="bain-capital", name="Bain Capital"),
        "ms": Firm.objects.create(slug="morgan-stanley", name="Morgan Stanley"),
        "gs": Firm.objects.create(slug="goldman-sachs", name="Goldman Sachs"),
    }


def test_import_matches_ampersand_firm_from_an_and_spelling(user, near_collision_firms):
    result = services.parse_contacts_csv(
        user, "name,firm\nAlex Consultant,Bain and Company\n"
    )
    assert result.created == 1
    assert result.firm_matched == 1
    contact = Contact.objects.for_user(user).get(name="Alex Consultant")
    assert contact.firm_id == near_collision_firms["bain_co"].id
    assert contact.firm_text == ""
    assert result.unmatched_firms == []


def test_import_matches_lowercase_and_trailing_legal_suffix(user, near_collision_firms):
    result = services.parse_contacts_csv(
        user,
        "name,firm\n"
        "Casey Analyst,goldman sachs\n"
        "Drew Banker,Morgan Stanley & Co.\n",
    )
    assert result.created == 2
    assert result.firm_matched == 2
    casey = Contact.objects.for_user(user).get(name="Casey Analyst")
    drew = Contact.objects.for_user(user).get(name="Drew Banker")
    assert casey.firm_id == near_collision_firms["gs"].id
    assert drew.firm_id == near_collision_firms["ms"].id


def test_import_does_not_merge_bain_and_company_with_bain_capital(
    user, near_collision_firms
):
    result = services.parse_contacts_csv(
        user,
        "name,firm\n"
        "Pat Consultant,Bain and Company\n"
        "Sam Investor,Bain Capital\n",
    )
    assert result.firm_matched == 2
    pat = Contact.objects.for_user(user).get(name="Pat Consultant")
    sam = Contact.objects.for_user(user).get(name="Sam Investor")
    assert pat.firm_id == near_collision_firms["bain_co"].id
    assert sam.firm_id == near_collision_firms["bain_cap"].id
    assert pat.firm_id != sam.firm_id


def test_import_reports_genuinely_unknown_firm_as_unmatched(user, near_collision_firms):
    result = services.parse_contacts_csv(
        user, "name,firm\nTaylor Unknown,Definitely Not A Real Fund LP\n"
    )
    assert result.created == 1
    assert result.firm_matched == 0
    assert len(result.unmatched_firms) == 1
    group = result.unmatched_firms[0]
    assert group.firm_text == "Definitely Not A Real Fund LP"
    assert group.count == 1
    contact = Contact.objects.for_user(user).get(name="Taylor Unknown")
    assert group.contact_ids == [contact.pk]
    assert group.suggested_firm is None


def test_import_groups_unmatched_firm_variants_together(user, near_collision_firms):
    """Two rows spelled slightly differently but normalizing the same way
    land in one fix-up card, not two."""
    result = services.parse_contacts_csv(
        user,
        "name,firm\n"
        "One Person,Totally Unknown Fund\n"
        "Two Person,totally unknown fund\n",
    )
    assert len(result.unmatched_firms) == 1
    assert result.unmatched_firms[0].count == 2


def test_import_verify_scenario_three_link_one_unmatched(user, near_collision_firms):
    """The exact CSV from the bug walkthrough: three near-miss spellings
    that should now match, and one genuinely unknown firm that shouldn't."""
    csv_text = (
        "name,email,firm\n"
        "A One,a1@x.com,Bain and Company\n"
        "A Two,a2@x.com,goldman sachs\n"
        "A Three,a3@x.com,Morgan Stanley & Co.\n"
        "A Four,a4@x.com,Definitely Not A Real Fund LP\n"
    )
    result = services.parse_contacts_csv(user, csv_text)
    assert result.created == 4
    assert result.firm_matched == 3
    assert len(result.unmatched_firms) == 1
    assert result.unmatched_firms[0].firm_text == "Definitely Not A Real Fund LP"


# ---------------------------------------------------------------------------
# link_contacts_to_firm — the unmatched-firm fix-up
# ---------------------------------------------------------------------------
def test_link_contacts_to_firm_repoints_firm_and_clears_firm_text(user, firms):
    contact = Contact.all_objects.create(
        user=user, name="Free Text Person", firm_text="Some Tiny Fund LP"
    )
    linked = services.link_contacts_to_firm(user, [contact.pk], firms["gs"].id)
    assert linked == 1
    contact.refresh_from_db()
    assert contact.firm_id == firms["gs"].id
    assert contact.firm_text == ""


def test_link_contacts_to_firm_infers_region_from_the_linked_firm(user):
    us_only_firm = Firm.objects.create(slug="us-only", name="US Only Firm", regions=["us"])
    contact = Contact.all_objects.create(
        user=user, name="Region Unknown", firm_text="US Only Firm Inc"
    )
    assert contact.region == ""
    services.link_contacts_to_firm(user, [contact.pk], us_only_firm.id)
    contact.refresh_from_db()
    assert contact.region == "us"


def test_link_contacts_to_firm_is_tenant_scoped(user, other_user, firms):
    """A contact_id belonging to another user must never be re-pointed,
    even if it leaked into the POST body (tampered form, stale page)."""
    theirs = Contact.all_objects.create(
        user=other_user, name="Not Yours", firm_text="Whatever Inc"
    )
    linked = services.link_contacts_to_firm(user, [theirs.pk], firms["gs"].id)
    assert linked == 0
    theirs.refresh_from_db()
    assert theirs.firm_id is None
    assert theirs.firm_text == "Whatever Inc"


def test_link_contacts_to_firm_with_bad_firm_id_is_a_noop(user):
    contact = Contact.all_objects.create(user=user, name="Someone", firm_text="X Corp")
    assert services.link_contacts_to_firm(user, [contact.pk], 999999) == 0
    assert services.link_contacts_to_firm(user, [contact.pk], "not-an-id") == 0
    contact.refresh_from_db()
    assert contact.firm_id is None


def test_link_contacts_to_firm_records_event(user, firms):
    contact = Contact.all_objects.create(user=user, name="Someone", firm_text="X Corp")
    services.link_contacts_to_firm(user, [contact.pk], firms["gs"].id)
    event = ProductEvent.all_objects.filter(user=user, event="import_firm_linked").first()
    assert event is not None
    assert event.props["firm"] == "Goldman Sachs"
    assert event.props["count"] == 1


def test_import_link_firm_view_links_and_redirects(client, user, firms):
    contact = Contact.all_objects.create(
        user=user, name="View Person", firm_text="Some Tiny Fund LP"
    )
    client.force_login(user)
    resp = client.post(
        reverse("accounts:import_link_firm"),
        {"contact_id": [str(contact.pk)], "firm_id": str(firms["gs"].id)},
    )
    assert resp.status_code == 302
    assert resp.url == reverse("accounts:import")
    contact.refresh_from_db()
    assert contact.firm_id == firms["gs"].id


def test_import_link_firm_view_requires_login(client, firms):
    resp = client.post(reverse("accounts:import_link_firm"), {"firm_id": firms["gs"].id})
    assert resp.status_code in (302, 403)


def test_import_link_firm_view_with_no_firm_chosen_does_not_crash(client, user):
    contact = Contact.all_objects.create(user=user, name="Someone", firm_text="X Corp")
    client.force_login(user)
    resp = client.post(
        reverse("accounts:import_link_firm"),
        {"contact_id": [str(contact.pk)], "firm_id": ""},
    )
    assert resp.status_code == 302
    contact.refresh_from_db()
    assert contact.firm_id is None
    assert contact.firm_text == "X Corp"


def test_import_page_renders_unmatched_firms_with_link_controls(client, user, near_collision_firms):
    client.force_login(user)
    csv_text = "name,firm\nUnmatched Person,Definitely Not A Real Fund LP\n"
    upload = _csv_upload(csv_text)
    resp = client.post(reverse("accounts:import"), {"file": upload})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Definitely Not A Real Fund LP" in body
    assert "match the directory" in body
    assert 'name="firm_id"' in body
    assert 'name="contact_id"' in body


def _csv_upload(text: str):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("contacts.csv", text.encode("utf-8"), content_type="text/csv")


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
    for label in ["touches", "contacts", "user_firms", "tasks",
                  "fit_scores", "user_opportunities", "product_events", "imports"]:
        assert counts[label] >= 1, label

    # The user is gone.
    assert not User.objects.filter(id=user_id).exists()
    # Every private table is empty for the deleted user.
    assert not Contact.all_objects.filter(user_id=user_id).exists()
    assert not Touch.all_objects.filter(user_id=user_id).exists()
    assert not UserFirm.all_objects.filter(user_id=user_id).exists()
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
