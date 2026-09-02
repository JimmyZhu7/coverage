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
`assets` otherwise intact, Settings renders the controls with their ledes,
and the dead column is gone.

WHERE THESE CONTROLS LIVE CHANGED ON 2026-09-01. Settings is now the only
page that asks for languages, affiliations and a timezone; onboarding step 1
stopped, because none of the three narrows the feed the wizard's preview
panel is showing while the student answers. The two render tests near the
bottom of this file say so in both directions, and the one that used to
assert the wizard renders them says why it now asserts the opposite.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from accounts.forms import (
    AFFILIATION_LIMIT, AFFILIATION_LONG_NOTE, AFFILIATION_MAX_CHARS,
    AFFILIATION_WORD_SOFT_MAX, LANGUAGE_CHOICES, ProfileForm,
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
# Affiliations: the shape readout (2026-09-02)
#
# The field's whole product effect is one substring search:
# `crm.relevance.specific_tie` asks whether a tie appears verbatim, case
# blind, inside a contact's own `school`/`angle`/`notes`, and a hit
# multiplies that contact's `ev` by 1.6. A description never appears in
# somebody else's text; a name constantly does. The box used to say nothing
# about that, so five résumé fragments on the founder's own row matched
# nothing at all across 306 contacts and he had no way to find out.
#
# What these pin is that the page now SAYS so, and that saying so never turns
# into doing something about it: no refusal, no truncation, no rewrite.
# ---------------------------------------------------------------------------

def test_the_readout_marks_a_long_tie_and_stays_quiet_about_a_short_one(user):
    """Word count can only catch one failure — a line too long to be a
    substring of anyone's text — so it reports that one and says nothing
    about the rest. A short tie gets a row and no verdict, deliberately:
    "PE deal-sourcing internship" is three words and still matches nobody,
    and a product that green-ticked it would be guessing out loud (P1)."""
    long_tie = "London M&A boutique internship (live deal exposure)"
    form = ProfileForm(_post(affiliations=f"USC\n{long_tie}"))
    rows = form.shape_report()

    assert [r["text"] for r in rows] == ["USC", long_tie]
    assert rows[0]["words"] == 1 and rows[0]["long"] is False
    assert rows[0]["note"] == ""
    assert rows[1]["words"] > AFFILIATION_WORD_SOFT_MAX
    assert rows[1]["long"] is True
    assert rows[1]["note"] == AFFILIATION_LONG_NOTE


def test_the_readout_reports_the_line_exactly_as_typed(user):
    """It quotes, it does not edit. The only thing it normalises is the run
    of whitespace the box itself would have collapsed on save."""
    form = ProfileForm(_post(affiliations="  Grew   up in Hong Kong  "))
    assert [r["text"] for r in form.shape_report()] == ["Grew up in Hong Kong"]


def test_a_long_tie_still_saves_exactly_as_typed(user):
    """THE LOAD-BEARING ONE. The readout is an opinion about shape; the row
    is the student's own answer. A tie over the word ceiling validates,
    saves, and comes back byte for byte — never shortened, never reordered,
    never dropped (P2, the student's data outranks the product's rule)."""
    typed = "Chinese securities firm internship (China market fluency)"
    form = ProfileForm(_post(affiliations=typed))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["affiliations"] == [typed]
    form.apply_to(user)
    user.refresh_from_db()
    assert user.affiliations == [typed]


def test_the_placeholder_teaches_names_not_resume_lines(user):
    """It used to read "London M&A boutique, summer intern", which taught by
    example the exact shape that cannot match. Every placeholder line is now
    a proper noun inside the word ceiling."""
    lines = ProfileForm().fields["affiliations"].widget.attrs["placeholder"].splitlines()
    assert lines, "the box still shows examples"
    for line in lines:
        assert len(line.split()) <= AFFILIATION_WORD_SOFT_MAX, line
    assert "USC" in lines


def test_the_readout_is_painted_by_the_server_not_the_script(client, user):
    """Progressive enhancement, and the reason the form owns the judgement:
    a student with JS off still gets told, at the field, which of their lines
    will not match. The script at the bottom of the partial only takes over
    once the text starts moving."""
    user.affiliations = ["USC", "Bilingual English/Mandarin, HK desk fit"]
    user.save(update_fields=["affiliations"])
    client.force_login(user)
    body = client.get(reverse("accounts:settings")).content.decode()

    # `<li class=` and not a bare class name: the enhancer at the bottom of
    # the partial builds the same classes in JS, so a looser needle would
    # pass on the script alone and prove nothing about the server render.
    assert body.count('<li class="aff-line') == 2
    assert '<li class="aff-line is-long"' in body
    # `escape`, because the sentence carries an apostrophe and Django is
    # doing its job.
    from django.utils.html import escape
    assert f'class="aff-line-note">5 words. {escape(AFFILIATION_LONG_NOTE)}<' in body
    # The short one is listed, and nothing is claimed about it.
    assert '<li class="aff-line"' in body
    assert 'class="aff-line-text">USC<' in body


def test_the_saved_shape_note_speaks_only_for_stored_long_ties(client, user):
    """The one-time note is about what is STORED, which is the case the
    redesign of the box cannot reach: a student who answered months ago and
    has no reason to scroll back down to it. Short ties, or none, and the
    page says nothing."""
    client.force_login(user)
    url = reverse("accounts:settings")
    # The markup's own needle. `data-aff-notice` on its own also appears in
    # the enhancer's selectors at the bottom of the partial.
    needle = 'data-aff-notice-key="aff-shape:'

    assert needle not in client.get(url).content.decode()

    user.affiliations = ["USC", "Bain & Company"]
    user.save(update_fields=["affiliations"])
    assert needle not in client.get(url).content.decode()

    user.affiliations = ["USC", "Consulting club e-board alumni network"]
    user.save(update_fields=["affiliations"])
    body = client.get(url).content.decode()
    assert needle in body
    assert 'href="#id_affiliations"' in body          # points at the field
    assert "One of your saved ties reads as a sentence." in body
    # It points. It does not act: the note carries no edited version of the
    # line, and nothing outside the box quotes it back.
    note = body.split(needle)[1].split("</div>")[0]
    assert "Consulting club" not in note


def test_the_note_key_tracks_the_exact_set_of_long_ties(user):
    """Dismissal is per-advice, not per-field. Trimming one long tie and
    leaving another changes the key, so the note comes back for what is
    left; waving off the same set twice does not."""
    long_a = "Consulting club e-board alumni network"
    long_b = "Chinese securities firm internship (China market fluency)"

    both = ProfileForm(initial={"affiliations": f"{long_a}\n{long_b}"})
    one = ProfileForm(initial={"affiliations": f"USC\n{long_a}"})
    same = ProfileForm(initial={"affiliations": f"{long_a}\n{long_b}\nUSC"})

    assert both.saved_long_ties() == [long_a, long_b]
    assert one.saved_long_ties() == [long_a]
    assert both.saved_shape_token() != one.saved_shape_token()
    # A short tie added or removed is not new advice.
    assert both.saved_shape_token() == same.saved_shape_token()
    assert ProfileForm(initial={"affiliations": "USC"}).saved_shape_token() == ""


def test_the_note_is_silent_while_the_student_is_mid_edit(client, user):
    """A bound form is a validation-error re-render: the person is looking at
    the box right now, with their own text in it, and being told about the
    row they are in the middle of replacing is noise. `saved_long_ties` reads
    `self.initial`, which `accounts.views._bound_profile_form` deliberately
    seeds with only `target_cycles` and `languages`."""
    user.affiliations = ["Consulting club e-board alumni network"]
    user.save(update_fields=["affiliations"])
    client.force_login(user)
    body = client.post(reverse("accounts:settings"), _post(
        section="profile",
        affiliations="Consulting club e-board alumni network",
        school_emails="me@gmail.com",          # refused: forces the re-render
    ), HTTP_HX_REQUEST="true").content.decode()

    assert '<li class="aff-line' in body, "the readout still describes the box"
    assert 'data-aff-notice-key="aff-shape:' not in body


def test_the_onboarding_step_carries_no_shape_note(client, user):
    """Step 1 does not render the field (it rides through as a hidden input),
    so a note whose only action is a link to that field would point at
    nothing. See the `compact` branch of the partial."""
    user.affiliations = ["Consulting club e-board alumni network"]
    user.save(update_fields=["affiliations"])
    client.force_login(user)
    body = client.get(reverse("accounts:onboarding") + "?step=profile").content.decode()
    assert 'data-aff-notice-key="aff-shape:' not in body


# ---------------------------------------------------------------------------
# Both pages: Settings and onboarding step 1 render and save them
# ---------------------------------------------------------------------------

def _assert_controls_and_ledes(body: str):
    """Rewritten 2026-09-02: the Study Level hint was cut. Every option in
    the select already names the level ("Undergraduate", "Master's",
    "PhD"), so "some programmes are MBA- or PhD-only" restated what the
    control itself already said, one line under it.

    Rewritten again the same day for Affiliations. The old lede — "Specific
    ties get replies: a club, a prior employer, a hometown." — named the
    CATEGORIES and left the shape to the reader, which is how five résumé
    fragments ended up in the box. The new one names the shape, because the
    shape is the whole thing that decides whether the field does anything.
    The assertion moves with the copy; it was never pinning a behaviour."""
    assert 'name="languages"' in body
    assert 'id="id_study_level"' in body
    assert 'id="id_affiliations"' in body
    assert "Languages you can work in" in body
    assert "Names, not sentences. A club, a firm, a school, a hometown." in body


def test_the_settings_page_renders_the_three_controls_with_their_ledes(client, user):
    client.force_login(user)
    _assert_controls_and_ledes(client.get(reverse("accounts:settings")).content.decode())


def test_the_onboarding_profile_step_asks_only_what_the_feed_reads(client, user):
    """REWRITTEN 2026-09-01. This test used to assert the wizard's step 1
    renders the same three controls Settings does, and it was right when the
    two pages were meant to be the same form.

    They are not any more. Step 1 measured 2080px tall — twelve controls
    before a student has watched Coverage do anything — and the wizard's own
    rule (accounts/views.py's ONBOARDING_STEPS comment: "asking early is how
    a wizard gets abandoned") applies inside a step as much as across them.
    Languages, affiliations and timezone came off it, because none of them
    narrows the Opportunities feed the preview panel is showing while the
    student answers: languages produce a WARNING on a role and never hide
    one, affiliations feed an outreach draft that cannot exist before there
    is a contact, and the timezone auto-follows the browser.

    So the assertion flips rather than being dropped: Settings still renders
    all three (the test above is unchanged and is what pins that), and step 1
    must NOT, while still asking for everything the feed does read. The
    behaviour this protects now is that the trim did not quietly take a
    feed-relevant field with it.

    accounts/tests/test_onboarding_chrome.py carries the other half: that the
    three values ride through as hidden inputs, so a save from this step
    cannot blank them.
    """
    client.force_login(user)
    body = client.get(reverse("accounts:onboarding") + "?step=profile").content.decode()
    visible = re.sub(r'<input type="hidden"[^>]*>', "", body)

    assert 'name="languages"' not in visible
    assert 'id="id_affiliations"' not in visible
    assert 'id="id_timezone"' not in visible
    assert "Languages, affiliations and timezone: set later in Settings." in body

    # Still asked, because the feed reads every one of them.
    assert 'id="id_study_level"' in body
    for field in ('name="school"', 'name="class_year"', 'name="target_cycles"',
                  'name="regions"', 'name="tracks"'):
        assert field in body


def test_the_ledes_carry_no_em_dash():
    """Repo copy style: minimal, punchy, no em dashes in what a student reads.

    Widened 2026-09-02. The Affiliations redesign added three more strings a
    student reads that are not `field-hint` lines — the shape note, the empty
    state, and the saved-shape banner — and the file's own `{% comment %}`
    blocks are full of em dashes, so the check cannot simply scan the file.
    It scans the copy-bearing lines by their classes instead, plus the one
    sentence that lives in Python because two renderers share it.
    """
    from pathlib import Path

    from django.conf import settings

    partial = Path(settings.BASE_DIR) / "templates" / "accounts" / "_profile_form.html"
    lines = partial.read_text().splitlines()
    hints = [line for line in lines if "field-hint" in line]
    assert len(hints) >= 4, "the avatar hint plus the three new ledes"

    student_copy = hints + [
        line for line in lines
        if "aff-empty" in line or "aff-notice-text" in line
        or "saved ties read as sentences" in line
    ]
    assert len(student_copy) > len(hints), "the affiliations copy is in scope"
    assert not any("—" in line for line in student_copy)
    assert "—" not in AFFILIATION_LONG_NOTE


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


# ---------------------------------------------------------------------------
# 0017: the stored cycle value no parser has ever recognised.
# ---------------------------------------------------------------------------
_BEFORE_0017 = ("accounts", "0016_user_pro_trial_notice_dismissed_at")
_AFTER_0017 = ("accounts", "0017_parseable_target_cycles")


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("sa2028_ib", "2028 Summer Internship"),
        ("SA2027_ST", "2027 Summer Internship"),
        ("sa2028", "2028 Summer Internship"),
        # Everything the mapper must refuse.
        ("2028 Summer Internship", None),
        ("Off-Cycle / Immediate", None),
        ("sa28_ib", None),
        ("summer 2028", None),
        ("", None),
    ],
)
def test_the_legacy_cycle_mapper_is_narrow_and_refuses_what_it_cannot_read(
    stored, expected
):
    """P1 in one function. `sa<year>` and `sa<year>_<track>` map; anything
    else — a value already in the dropdown's words, a blank, wording nobody
    here has seen — returns None and is left exactly as it was. A cycle this
    migration cannot read is not a cycle it may guess at."""
    import importlib

    module = importlib.import_module(
        "accounts.migrations.0017_parseable_target_cycles"
    )

    assert module.parse_legacy(stored) == expected


@pytest.mark.django_db(transaction=True)
def test_the_data_migration_rewrites_an_unparseable_stored_cycle():
    """The measured defect (WS-AI-13). The demo account carried
    `["sa2028_ib"]`, which `directory.recommend.parse_target_cycle` returns
    None for — so the 15-point cycle bonus and the level gate were silently
    off on the account every demo the founder gives runs on.

    The track half is DROPPED rather than lost: `target_cycles` is a list of
    cycles and `User.tracks` is where a track belongs. Encoding one inside
    the other is what made the value unparseable in the first place.

    Then forward to head, so this file leaves the database where every later
    test expects it (see `_accounts_head`'s own note).
    """
    from directory.recommend import parse_target_cycle

    executor = MigrationExecutor(connection)
    executor.migrate([_BEFORE_0017])
    OldUser = executor.loader.project_state([_BEFORE_0017]).apps.get_model(
        "accounts", "User")
    OldUser.objects.create(email="legacy@example.com", target_cycles=["sa2028_ib"])
    OldUser.objects.create(email="already@example.com",
                           target_cycles=["2028 Summer Internship"])
    OldUser.objects.create(email="unknown@example.com",
                           target_cycles=["whatever I typed"])
    OldUser.objects.create(email="nothing@example.com", target_cycles=[])

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([_AFTER_0017])
    NewUser = executor.loader.project_state([_AFTER_0017]).apps.get_model(
        "accounts", "User")

    legacy = NewUser.objects.get(email="legacy@example.com")
    assert legacy.target_cycles == ["2028 Summer Internship"]
    assert parse_target_cycle(legacy.target_cycles[0]) == ("internship", 2028)

    assert NewUser.objects.get(
        email="already@example.com").target_cycles == ["2028 Summer Internship"]
    assert NewUser.objects.get(
        email="unknown@example.com").target_cycles == ["whatever I typed"]
    assert NewUser.objects.get(email="nothing@example.com").target_cycles == []

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([_accounts_head(executor)])
    assert executor.migration_plan(executor.loader.graph.leaf_nodes()) == []
