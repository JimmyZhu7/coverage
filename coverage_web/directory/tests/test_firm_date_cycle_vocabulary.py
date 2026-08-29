"""`firm_dates.cycle` is a closed vocabulary, and the timeline knows the student.

Two things are covered here, and they are one thing.

PART 1 — the vocabulary. `cycle` was the last free-text key on `FirmDate`, and
the live 41 rows held four spellings of one cycle (`sa2028_ib`, `SA 2028`,
`sa2028_hk`, `sa2028_pe`) plus one value that was not a cycle at all. Migration
0014 closed it to a season+year shape and moved the desk into its own `track`
column. `event_kind` was already closed; `confidence` was closed by 0012.

PART 2 — the reason it was worth closing. The founder's stated target cycle
("2028 Summer Internship") matches 2 of the 2,617 open campus rows, because SA
2028 postings do not exist yet — while 38 of the 41 firm_dates rows are exactly
that cycle. Closing the vocabulary is what lets a stated preference reach the
one dataset that can honour it. `test_the_students_own_cycle_is_marked` is that
join, end to end.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.db import transaction
from django.db.utils import IntegrityError

from accounts.models import User
from directory.models import Firm, FirmDate
from directory.timeline import (
    EVENT_LABELS, cycle_slug_for_target, cycle_text, is_valid_cycle,
    is_valid_track, parse_cycle,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def firm():
    return Firm.objects.create(slug="gs", name="Goldman Sachs")


# ---------------------------------------------------------------------------
# The parser: every spelling the corpus and the two writers produced
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("sa2028_ib", ("sa2028", "ib")),      # 18 live rows: cycle AND desk
    ("sa2028_pe", ("sa2028", "pe")),      # 2
    ("SA 2028", ("sa2028", "")),          # 11, the importer's own default
    ("sa2028", ("sa2028", "")),
    ("ft2027", ("ft2027", "")),
    ("", ("", "")),                       # 2, and a real state
])
def test_every_live_spelling_reads_as_one_pair(raw, expected):
    assert parse_cycle(raw) == expected


def test_a_market_suffix_is_dropped_because_region_already_holds_it():
    """The 7 `sa2028_hk` rows all carry `region="hk"` too. Keeping the suffix
    would be a second copy of one fact — which is how the firm page came to
    print the market twice."""
    assert parse_cycle("sa2028_hk") == ("sa2028", "")


@pytest.mark.parametrize("raw", ["insight", "garbage", "2028", "sa2028_xx", "SA twenty"])
def test_a_value_that_names_no_cycle_returns_none_not_blank(raw):
    """None and `("", "")` are different answers and must stay different. A
    blank cycle is information ("nobody stated one"); an unreadable string is a
    broken finding, and a writer that collapsed the two would store garbage as
    though it were a known-unknown — the same distinction
    `import_firm_dates._parse_date` draws for dates."""
    assert parse_cycle(raw) is None


# ---------------------------------------------------------------------------
# The constraints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cycle", ["sa2028", "ft2027", ""])
def test_the_vocabulary_accepts_what_it_should(firm, cycle):
    fd = FirmDate.objects.create(firm=firm, cycle=cycle, event_kind="app_close")
    fd.refresh_from_db()
    assert fd.cycle == cycle
    assert is_valid_cycle(cycle)


@pytest.mark.parametrize("cycle", ["SA 2028", "sa2028_ib", "sa2028_hk", "insight", "2028"])
def test_the_old_spellings_are_now_unwritable(firm, cycle):
    """Each of these was live in the table before 0014."""
    assert not is_valid_cycle(cycle)
    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.create(firm=firm, cycle=cycle, event_kind="app_close")


@pytest.mark.parametrize("track", ["ib", "st", "pe", "am", "consulting", "corp-strat", ""])
def test_the_track_column_takes_the_preference_vocabulary(firm, track):
    """The same six slugs `User.tracks` holds, which is the whole point of
    giving the desk its own column."""
    assert is_valid_track(track)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", track=track, event_kind="app_close")


@pytest.mark.parametrize("track", ["IB", "pipeline", "banking"])
def test_a_track_outside_that_vocabulary_is_rejected(firm, track):
    """A seventh spelling would silently stop matching `User.tracks`."""
    assert not is_valid_track(track)
    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.create(
            firm=firm, cycle="sa2028", track=track, event_kind="app_close")


def test_the_desk_is_part_of_the_uniqueness_key(firm):
    """Goldman's two live US `app_open` rows differ ONLY by desk once their
    cycles normalise (id 11, estimated from past cycles; id 33, confirmed off
    goldmansachs.com). Without `track` in the key the migration could not have
    kept both."""
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", track="ib", region="us", event_kind="app_open",
        date=dt.date(2027, 3, 1), precision="estimated", confidence=0.6)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", track="", region="us", event_kind="app_open",
        date=dt.date(2026, 8, 15), confidence=1.0)
    assert FirmDate.objects.filter(firm=firm, event_kind="app_open").count() == 2

    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.create(
            firm=firm, cycle="sa2028", track="ib", region="us",
            event_kind="app_open", date=dt.date(2027, 4, 1))


# ---------------------------------------------------------------------------
# The label bug
# ---------------------------------------------------------------------------
def test_insight_deadline_has_a_label(client, firm):
    """`EVENT_LABELS` spelled it `insight_close`; `import_firm_dates.EVENT_KINDS`
    accepts `insight_deadline`, and that is what the live Morgan Stanley row
    (id 32) holds. With no entry the row fell through to a `.capitalize()` of
    its own slug and rendered as "Insight deadline" — sentence-cased shorthand
    beside six title-cased labels in the same table."""
    assert EVENT_LABELS["insight_deadline"] == "Insight Programme Deadline"
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="insight_deadline",
        date=dt.date(2026, 8, 6), confidence=1.0)
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Insight Programme Deadline" in body
    assert "Insight deadline" not in body


def test_the_scope_still_prints_the_desk(firm):
    assert cycle_text("sa2028", "ib") == "SA 2028 · IB"
    assert cycle_text("sa2028", "") == "SA 2028"
    assert cycle_text("", "") == ""


# ---------------------------------------------------------------------------
# The join: a stated preference reaching the corpus that can honour it
# ---------------------------------------------------------------------------
def test_a_stated_target_cycle_maps_onto_the_corpus_key():
    """`recommend.parse_target_cycle("2028 Summer Internship")` is
    `("internship", 2028)`; the 38 live rows are filed under `sa2028`. This is
    the bridge."""
    assert cycle_slug_for_target("internship", 2028) == "sa2028"
    assert cycle_slug_for_target("entry_level", 2027) == "ft2027"


def test_an_insight_target_has_no_cycle_slug_and_matches_nothing():
    """Insight programmes are an `event_kind` here, not a cycle. "" must read
    downstream as "cannot be matched", never as "matches everything"."""
    assert cycle_slug_for_target("insight", 2027) == ""


def _student(**kw):
    kw.setdefault("target_cycles", ["2028 Summer Internship"])
    user = User.objects.create_user(email="s@example.com", password="pw123456", **kw)
    return user


def test_the_students_own_cycle_is_marked(client, firm):
    """The founder's exact shape: a stated SA 2028 target, and a firm page
    whose dates are SA 2028 dates."""
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", track="ib", region="us", event_kind="app_open",
        date=dt.date(2027, 3, 1), precision="estimated", confidence=0.6)
    client.force_login(_student())
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "your cycle" in body
    assert "You told us you&#x27;re recruiting for SA 2028 · IB" in body


def test_a_different_cycle_is_not_marked(client, firm):
    FirmDate.objects.create(
        firm=firm, cycle="ft2027", region="us", event_kind="app_close",
        date=dt.date(2027, 3, 1), confidence=1.0)
    client.force_login(_student())
    assert "your cycle" not in client.get(f"/firms/{firm.slug}/").content.decode()


def test_a_row_with_no_cycle_is_never_marked(client, firm):
    """Two live rows are dated confirmed closes with no cycle on file (J.P.
    Morgan, Goldman Sachs — hand-entered by the founder). They render, because
    they are real deadlines, but a blank cycle can never BE the student's
    cycle. `manage.py review_firm_date_cycles` is where they surface."""
    FirmDate.objects.create(
        firm=firm, cycle="", region="", event_kind="app_close",
        date=dt.date(2026, 9, 22), confidence=1.0)
    client.force_login(_student())
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Sep 22, 2026" in body
    assert "your cycle" not in body


def test_a_student_who_stated_no_cycle_is_told_nothing(client, firm):
    """Mark nothing is the honest default. The page must not decide a cycle is
    "theirs" on the strength of anything they did not say — not their tiered
    firms, not who they email, not what they saved."""
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_open",
        date=dt.date(2027, 3, 1), confidence=0.6)
    client.force_login(_student(target_cycles=[]))
    assert "your cycle" not in client.get(f"/firms/{firm.slug}/").content.decode()


def test_a_signed_out_visitor_is_told_nothing(client, firm):
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_open",
        date=dt.date(2027, 3, 1), confidence=0.6)
    assert "your cycle" not in client.get(f"/firms/{firm.slug}/").content.decode()


def test_the_marker_does_not_reorder_the_timeline(client, firm):
    """A timeline is chronological. Marking a row changes what it SAYS, not
    where it sits — a nearer deadline must never fall below a further one
    because the further one happens to be the student's cycle."""
    FirmDate.objects.create(
        firm=firm, cycle="", region="us", event_kind="app_close",
        date=dt.date(2026, 9, 22), confidence=1.0)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_open",
        date=dt.date(2027, 3, 1), confidence=0.6)
    client.force_login(_student())
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert body.index("Sep 22, 2026") < body.index("Mar 1, 2027")
