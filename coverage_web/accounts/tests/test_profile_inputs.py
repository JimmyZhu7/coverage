"""The three profile inputs the research says gate roles and outreach —
`User.languages`, `User.study_level`, `User.affiliations` — and the column
they retired, `User.language` (accounts migration 0015, 2026-09-01).

Why they exist: the founder's `User.assets` JSON held `languages`,
`current_status` and `angles`, written by a cutover script, reachable from
no form and read by nothing. Mandarin gates Hong Kong IB (a Barclays HK
posting states it; practitioners put it on about 95% of first-year desks)
but appears in about 1 HK posting in 7, so it has to be a fact about the
STUDENT matched against the posting, never inferred from postings alone.
Nothing knew a sophomore was an undergraduate, so PhD and MBA roles reached
his picks. And the number-one outreach-draft disqualifier is a generic email
with no specific hook — his `angles` were exactly the hooks.

What these pin: the fields exist under EXACTLY the names other readers use
(`getattr(user, "languages", None)` and friends), the form round-trips them
without silently clearing anything, the language vocabulary is the
extractor's own, the data migration moves the founder's row and leaves
`assets` otherwise intact, both pages render the controls with their ledes,
and the dead column is gone.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from accounts.forms import (
    AFFILIATION_LIMIT, AFFILIATION_MAX_CHARS, LANGUAGE_CHOICES, ProfileForm,
)
from accounts.models import STUDY_LEVEL_CHOICES
from directory.facts import _LANGS

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="inputs@example.com", password="x")


def _post(**over):
    data = {"name": "", "school": "", "school_emails": "", "class_year": "",
            "study_level": "", "target_cycles": [], "regions": [], "tracks": [],
            "languages": [], "affiliations": "", "timezone": ""}
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# The model: names other readers depend on, defaults that mean "not stated"
# ---------------------------------------------------------------------------

def test_the_three_fields_exist_under_the_names_other_readers_use(user):
    """Concurrent readers reach these via `getattr(user, "languages", None)`
    etc., so the names are a contract, not a style choice."""
    for name in ("languages", "study_level", "affiliations"):
        User._meta.get_field(name)
    assert user.languages == []
    assert user.study_level == ""
    assert user.affiliations == []


def test_the_dead_language_column_is_gone(user):
    """`User.language` was written by a Settings control removed 2026-07-30
    and read by nothing; the demo row carried "fr" in it for a month on
    "harmless". No column, no attribute, nothing to write to."""
    with pytest.raises(FieldDoesNotExist):
        User._meta.get_field("language")
    assert not hasattr(user, "language")


def test_study_level_choices_are_the_four_levels_plus_not_stated():
    assert [v for v, _ in STUDY_LEVEL_CHOICES] == [
        "", "undergrad", "masters", "mba", "phd"]


# ---------------------------------------------------------------------------
# Languages: the extractor's vocabulary, round-trip, never a silent clear
# ---------------------------------------------------------------------------

def test_language_choices_are_the_extractors_own_vocabulary_plus_english():
    """A language the board can read is a language a student can claim, and
    nothing else: the values are `directory.facts._LANGS` lowercased, with
    English (which no posting ever gates on, and which the founder's row
    carried) in front."""
    assert [v for v, _ in LANGUAGE_CHOICES] == ["english"] + list(_LANGS)
    assert all(v == v.lower() for v, _ in LANGUAGE_CHOICES)
    assert dict(LANGUAGE_CHOICES)["mandarin"] == "Mandarin"


def test_languages_round_trip_through_the_form(user):
    form = ProfileForm(_post(languages=["mandarin", "english"]))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert sorted(user.languages) == ["english", "mandarin"]
    assert sorted(ProfileForm.from_user(user)["languages"].value()) == [
        "english", "mandarin"]


def test_a_language_the_matcher_would_not_recognise_is_refused(user):
    """Storing "klingon" would save a value `_language_fit` can never match
    — the same defect as a setting the engine ignores."""
    form = ProfileForm(_post(languages=["klingon"]))
    assert not form.is_valid()
    assert "languages" in form.errors


def test_a_stored_language_the_list_no_longer_carries_renders_checked_not_dropped(user):
    """The target_cycles rule, applied here: a value the vocabulary loses
    must stay visible and saveable, or the next save silently drops it."""
    user.languages = ["latin"]
    user.save(update_fields=["languages"])
    choices = dict(ProfileForm.from_user(user).fields["languages"].choices)
    assert choices["latin"] == "Latin (no longer listed)"
    bound = ProfileForm(_post(languages=["latin"]), initial={"languages": ["latin"]})
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["languages"] == ["latin"]


def test_a_post_cannot_invent_a_language_without_the_stored_row_behind_it(user):
    """`initial`, never `request.POST`, decides what is stale-but-allowed."""
    assert not ProfileForm(_post(languages=["latin"])).is_valid()


# ---------------------------------------------------------------------------
# Study level: blank stays blank, never guessed
# ---------------------------------------------------------------------------

def test_study_level_round_trips_and_blank_stays_blank(user):
    form = ProfileForm(_post(study_level="mba"))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert user.study_level == "mba"
    assert ProfileForm.from_user(user)["study_level"].value() == "mba"

    form = ProfileForm(_post(study_level=""))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert user.study_level == ""


def test_an_unknown_study_level_is_refused(user):
    assert not ProfileForm(_post(study_level="postdoc")).is_valid()


# ---------------------------------------------------------------------------
# Affiliations: one per line, a cap that is refused out loud
# ---------------------------------------------------------------------------

def test_affiliations_split_on_lines_collapse_whitespace_and_dedupe(user):
    form = ProfileForm(_post(affiliations=(
        "  Consulting club,   e-board  \n\n"
        "London M&A boutique, summer intern\n"
        "Consulting club, e-board\n"
        "Grew up in Hong Kong\n")))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["affiliations"] == [
        "Consulting club, e-board",
        "London M&A boutique, summer intern",
        "Grew up in Hong Kong",
    ]
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert user.affiliations == form.cleaned_data["affiliations"]
    assert ProfileForm.from_user(user)["affiliations"].value() == (
        "Consulting club, e-board\n"
        "London M&A boutique, summer intern\n"
        "Grew up in Hong Kong")


def test_more_than_the_cap_is_refused_not_truncated(user):
    lines = "\n".join(f"Tie number {i}" for i in range(AFFILIATION_LIMIT + 1))
    form = ProfileForm(_post(affiliations=lines))
    assert not form.is_valid()
    assert str(AFFILIATION_LIMIT) in str(form.errors["affiliations"])


def test_a_tie_too_long_to_open_an_email_with_is_refused(user):
    form = ProfileForm(_post(affiliations="x" * (AFFILIATION_MAX_CHARS + 1)))
    assert not form.is_valid()
    assert "affiliations" in form.errors


def test_blank_affiliations_stay_blank(user):
    user.affiliations = ["Old tie"]
    user.save(update_fields=["affiliations"])
    form = ProfileForm(_post(affiliations="  \n \n"))
    assert form.is_valid(), form.errors
    form.apply_to(user)
    user.save()
    user.refresh_from_db()
    assert user.affiliations == []


# ---------------------------------------------------------------------------
# Both pages: Settings and onboarding step 1 render and save them
# ---------------------------------------------------------------------------

def _assert_controls_and_ledes(body: str):
    assert 'name="languages"' in body
    assert 'id="id_study_level"' in body
    assert 'id="id_affiliations"' in body
    assert "Languages you can work in" in body
    assert "Some programmes are MBA- or PhD-only. This keeps them off your picks." in body
    assert "Specific ties get replies: a club, a prior employer, a hometown." in body


def test_the_settings_page_renders_the_three_controls_with_their_ledes(client, user):
    client.force_login(user)
    _assert_controls_and_ledes(client.get(reverse("accounts:settings")).content.decode())


def test_the_onboarding_profile_step_renders_the_three_controls_with_their_ledes(client, user):
    client.force_login(user)
    body = client.get(reverse("accounts:onboarding") + "?step=profile").content.decode()
    _assert_controls_and_ledes(body)


def test_the_ledes_carry_no_em_dash():
    """Repo copy style: minimal, punchy, no em dashes in what a student reads."""
    from pathlib import Path

    from django.conf import settings

    partial = Path(settings.BASE_DIR) / "templates" / "accounts" / "_profile_form.html"
    hints = [line for line in partial.read_text().splitlines() if "field-hint" in line]
    assert len(hints) >= 4, "the avatar hint plus the three new ledes"
    assert not any("—" in line for line in hints)


def test_the_settings_profile_save_round_trips_all_three(client, user):
    client.force_login(user)
    resp = client.post(reverse("accounts:settings"), _post(
        section="profile", study_level="undergrad",
        languages=["english", "mandarin"],
        affiliations="USC Consulting Club, e-board\nGrew up in Hong Kong",
    ))
    assert resp.status_code in (200, 302)
    user.refresh_from_db()
    assert user.study_level == "undergrad"
    assert sorted(user.languages) == ["english", "mandarin"]
    assert user.affiliations == ["USC Consulting Club, e-board", "Grew up in Hong Kong"]


def test_the_settings_htmx_save_keeps_a_no_longer_listed_language(client, user):
    """The bound-form `initial` carries `languages` from the stored row —
    without it the checkbox the page itself rendered ticked would fail with
    "Select a valid choice", the exact bug pinned for target_cycles."""
    user.languages = ["latin"]
    user.save(update_fields=["languages"])
    client.force_login(user)
    resp = client.post(
        reverse("accounts:settings"),
        _post(section="profile", languages=["latin", "mandarin"]),
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert "Profile saved" in resp.content.decode()
    user.refresh_from_db()
    assert sorted(user.languages) == ["latin", "mandarin"]


def test_the_onboarding_profile_step_saves_all_three_and_advances(client, user):
    client.force_login(user)
    resp = client.post(
        reverse("accounts:onboarding") + "?step=profile",
        _post(step="profile", class_year="2028", study_level="undergrad",
              languages=["mandarin"], affiliations="Rowed in college"),
    )
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.class_year == 2028
    assert user.study_level == "undergrad"
    assert user.languages == ["mandarin"]
    assert user.affiliations == ["Rowed in college"]


# ---------------------------------------------------------------------------
# The data migration, dry-run on a fixture copy of the founder's row
# ---------------------------------------------------------------------------

_BEFORE = ("accounts", "0014_user_school_emails")
_AFTER = ("accounts", "0015_languages_study_level_affiliations")


def _accounts_head(executor) -> tuple[str, str]:
    """The accounts app's current leaf migration.

    NOT `_AFTER`. This test migrates the real test database backwards and
    forwards, and its last step has to leave it at the HEAD for every test
    that runs after it in the same session. That step used to target
    `_AFTER` — correct only while 0015 was the newest accounts migration,
    and silently wrong the moment a 0016 landed: the whole suite from this
    file onwards then ran against a `users` table missing the new column,
    which presents as a cascade of unrelated `column ... does not exist`
    failures hundreds of tests later, in files that have nothing to do with
    migrations. Reading the leaf from the graph makes the test correct for
    every future migration instead of for one.
    """
    leaves = [node for node in executor.loader.graph.leaf_nodes("accounts")]
    assert len(leaves) == 1, f"accounts has {len(leaves)} leaf migrations: {leaves}"
    return leaves[0]


def _columns() -> set[str]:
    with connection.cursor() as cur:
        return {c.name for c in connection.introspection.get_table_description(cur, "users")}


@pytest.mark.django_db(transaction=True)
def test_the_data_migration_moves_assets_into_the_columns_and_leaves_the_rest():
    """What 0015 does to the founder's row, on a copy of it: `languages`
    moves (lowercased, trimmed), "rising sophomore" maps to undergrad,
    `angles` moves to `affiliations`, `advocate_target` stays where every
    reader expects it, and the three keys that moved are gone from `assets`.
    A `current_status` the map never saw is left alone rather than guessed
    at; a row with nothing to move is untouched; the dead column is dropped.
    Then back, to prove the move is reversible, and forward again so the
    test database ends where every other test expects it."""
    executor = MigrationExecutor(connection)
    executor.migrate([_BEFORE])
    OldUser = executor.loader.project_state([_BEFORE]).apps.get_model("accounts", "User")
    OldUser.objects.create(
        email="founder@example.com", language="en",
        assets={"angles": ["London M&A boutique internship (live deal exposure)",
                           "Consulting club e-board alumni network"],
                "languages": ["English", " mandarin "],
                "current_status": "rising sophomore",
                "advocate_target": 2})
    OldUser.objects.create(
        email="mba@example.com", language="fr",
        assets={"current_status": "MBA candidate", "advocate_target": 3})
    OldUser.objects.create(email="blank@example.com", assets={})
    assert "language" in _columns()

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([_AFTER])
    NewUser = executor.loader.project_state([_AFTER]).apps.get_model("accounts", "User")

    founder = NewUser.objects.get(email="founder@example.com")
    assert founder.languages == ["english", "mandarin"]
    assert founder.study_level == "undergrad"
    assert founder.affiliations == [
        "London M&A boutique internship (live deal exposure)",
        "Consulting club e-board alumni network"]
    assert founder.assets == {"advocate_target": 2}, "everything else in assets stays"

    postgrad = NewUser.objects.get(email="mba@example.com")
    assert postgrad.study_level == "", "wording the map never saw is not guessed at"
    assert postgrad.assets == {"current_status": "MBA candidate", "advocate_target": 3}
    assert postgrad.languages == [] and postgrad.affiliations == []

    blank = NewUser.objects.get(email="blank@example.com")
    assert (blank.languages, blank.study_level, blank.affiliations, blank.assets) == ([], "", [], {})

    columns = _columns()
    assert "language" not in columns
    assert {"languages", "study_level", "affiliations"} <= columns

    # Backwards: the columns write themselves back under the old keys.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([_BEFORE])
    OldUser = executor.loader.project_state([_BEFORE]).apps.get_model("accounts", "User")
    restored = OldUser.objects.get(email="founder@example.com")
    assert restored.assets["languages"] == ["english", "mandarin"]
    assert restored.assets["angles"][0].startswith("London M&A")
    assert restored.assets["current_status"] == "undergrad"
    assert restored.assets["advocate_target"] == 2
    assert "language" in _columns()

    # Forward again, leaving the database at the head for every later test —
    # the REAL head, read from the graph, not `_AFTER`. See `_accounts_head`.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([_accounts_head(executor)])
    assert "language" not in _columns()


@pytest.mark.django_db(transaction=True)
def test_the_migration_walk_above_left_the_database_at_the_head():
    """The guard the walk above needed and did not have.

    Defined immediately after it so pytest runs it immediately after it
    (definition order within a module), and it asserts the one thing the
    rest of the suite silently depends on: nothing is left unapplied. When
    0016 landed, the walk's final step was still hard-coded to 0015, so it
    handed every subsequent test a `users` table one column short — and the
    symptom surfaced as hundreds of `column ... does not exist` errors in
    unrelated apps, with nothing pointing back here. An empty migration plan
    is a one-line answer to "did this file put the database back".
    """
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())

    assert plan == [], (
        "the migration walk in this module left the test database behind "
        f"head: {[name for _, name in ((m.app_label, m.name) for m, _ in plan)]}"
    )
