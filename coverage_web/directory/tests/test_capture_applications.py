"""Auto-detected applications: what may be marked, and what must not.

My Applications was a pipeline nobody used — the founder's own account
tracked zero roles — because it competed with the ATS's own tracking and
lost. The confirmation email is the evidence Coverage already reads twice a
day, so the board can fill itself.

That makes MATCHING the whole feature, and matching is where it could
quietly lie. An application wrongly marked submitted is worse than one not
marked at all: it tells a student a form is done that isn't, and the
deadline passes while the board says they are covered. Everything here pins
a refusal as hard as it pins a success.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from analytics.models import UserOpportunity
from directory.applications import (
    MIN_TITLE_SCORE, match_application, may_advance, title_score,
)
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        email="apps@example.com", password="x", capture_slug="appsslug123"
    )


@pytest.fixture
def gs():
    return Firm.objects.create(slug="gs", name="Goldman Sachs")


def _opp(firm, title, **kw):
    return Opportunity.objects.create(
        firm=firm, title=title, url=f"https://x/{title}", status="open",
        bucket=kw.pop("bucket", "internship"), **kw
    )


def _run(user, rows, dry=False):
    out = StringIO()
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(rows, fh)
    try:
        call_command("capture_applications", email=user.email, findings=path,
                     dry_run=dry, stdout=out)
    finally:
        os.unlink(path)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------
def test_the_same_posting_worded_differently_still_matches():
    a = "2027 Investment Banking Summer Analyst — New York"
    b = "Investment Banking Summer Analyst Program - 2027"
    assert title_score(a, b) >= MIN_TITLE_SCORE


def test_two_different_desks_at_one_firm_do_not_match():
    assert title_score(
        "Investment Banking Summer Analyst",
        "Global Markets Summer Analyst",
    ) < MIN_TITLE_SCORE


def test_titles_made_only_of_boilerplate_score_zero():
    """"Summer Analyst Program" twice carries no evidence that these are the
    same posting. Treating no-information as a perfect match is exactly how
    an auto-matcher marks the wrong role submitted."""
    assert title_score("Summer Analyst Program", "Summer Internship Programme") == 0.0


# ---------------------------------------------------------------------------
# Choosing, and refusing to choose
# ---------------------------------------------------------------------------
def test_a_clear_title_match_wins(user, gs):
    ib = _opp(gs, "Investment Banking Summer Analyst 2027")
    _opp(gs, "Global Markets Summer Analyst 2027")
    m = match_application([ib, *Opportunity.objects.filter(firm=gs).exclude(pk=ib.pk)],
                          "IB Summer Analyst 2027 - Investment Banking")
    assert m.matched and m.opportunity == ib


def test_two_equally_good_titles_refuse_rather_than_guess(user, gs):
    a = _opp(gs, "Technology Summer Analyst 2027")
    b = _opp(gs, "Technology Summer Analyst 2027 (London)")
    m = match_application([a, b], "Technology Analyst")
    # Whatever the scores, a guess between these two is not allowed to be
    # silent: either it picked a strictly better one, or it refused.
    if not m.matched:
        assert "match that title equally well" in m.reason or "did not match" in m.reason


def test_the_firms_only_open_role_is_a_safe_answer(user, gs):
    only = _opp(gs, "Summer Analyst Program")
    m = match_application([only], "")
    assert m.matched and m.opportunity == only
    assert "only open role" in m.reason


def test_a_saved_role_breaks_a_tie_the_title_cannot(user, gs):
    saved = _opp(gs, "Summer Analyst Program")
    _opp(gs, "Summer Internship Programme")
    m = match_application(list(Opportunity.objects.filter(firm=gs)), "",
                          tracked_ids=[saved.id])
    assert m.matched and m.opportunity == saved
    assert "saved" in m.reason


def test_many_roles_and_no_usable_title_refuses(user, gs):
    _opp(gs, "Summer Analyst Program")
    _opp(gs, "Summer Internship Programme")
    m = match_application(list(Opportunity.objects.filter(firm=gs)), "")
    assert not m.matched
    assert "2 open roles" in m.reason


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------
def test_detection_may_only_move_a_row_up_to_submitted():
    assert may_advance("") is True
    assert may_advance("saved") is True
    assert may_advance("submitted") is False
    assert may_advance("interview") is False, "an interview knows more than an email"
    assert may_advance("offer") is False
    assert may_advance("closed") is False, "and closed is the user's own decision"


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def test_a_confirmation_marks_the_role_applied(user, gs):
    opp = _opp(gs, "Investment Banking Summer Analyst 2027")
    out = _run(user, [{
        "firm": "gs", "title": "Investment Banking Summer Analyst 2027",
        "applied_at": "2026-08-01", "evidence": "Thank you for applying",
    }])
    assert "APPLIED" in out
    uo = UserOpportunity.objects.for_user(user).get(opportunity=opp)
    assert uo.applied_status == "submitted"
    assert uo.applied_at is not None


def test_running_twice_changes_nothing(user, gs):
    _opp(gs, "Investment Banking Summer Analyst 2027")
    rows = [{"firm": "gs", "title": "Investment Banking Summer Analyst 2027"}]
    _run(user, rows)
    first = UserOpportunity.objects.for_user(user).get()
    out = _run(user, rows)
    assert "already at submitted" in out
    again = UserOpportunity.objects.for_user(user).get()
    assert again.applied_at == first.applied_at, "the date it happened does not move"


def test_it_never_drags_an_interview_back_to_applied(user, gs):
    opp = _opp(gs, "Investment Banking Summer Analyst 2027")
    UserOpportunity.all_objects.create(
        user=user, opportunity=opp, applied_status="interview")
    _run(user, [{"firm": "gs", "title": "Investment Banking Summer Analyst 2027"}])
    assert UserOpportunity.objects.for_user(user).get().applied_status == "interview"


def test_an_ambiguous_confirmation_is_reported_not_guessed(user, gs):
    _opp(gs, "Summer Analyst Program")
    _opp(gs, "Summer Internship Programme")
    out = _run(user, [{"firm": "gs", "title": ""}])
    assert "Left for you to set by hand" in out
    assert UserOpportunity.objects.for_user(user).count() == 0


def test_an_unknown_firm_is_named_not_silently_dropped(user):
    out = _run(user, [{"firm": "Definitely Not A Bank", "title": "Analyst"}])
    assert "no such firm" in out
    assert UserOpportunity.objects.for_user(user).count() == 0


def test_a_dry_run_writes_nothing(user, gs):
    _opp(gs, "Investment Banking Summer Analyst 2027")
    out = _run(user, [{"firm": "gs", "title": "Investment Banking Summer Analyst 2027"}],
               dry=True)
    assert "APPLIED" in out, "but it still reports what it would do"
    assert UserOpportunity.all_objects.count() == 0


def test_one_users_applications_never_reach_another(user, gs, django_user_model):
    other = django_user_model.objects.create_user(
        email="other@example.com", password="x", capture_slug="otherapps1")
    _opp(gs, "Investment Banking Summer Analyst 2027")
    _run(user, [{"firm": "gs", "title": "Investment Banking Summer Analyst 2027"}])
    assert UserOpportunity.objects.for_user(other).count() == 0


def test_the_live_point72_miss_stays_fixed(user):
    """THE REGRESSION THAT MATTERS. Run against the founder's real mailbox,
    a genuine Point72 confirmation for the "2026 Spring Sessions" programme
    matched the "2026-2027 Investment Analyst Program for Experienced
    Professionals" posting at 40% — a different programme for a different
    audience. All four shared words were `point72`, `academy`, `2026`, `us`;
    nothing distinguishing overlapped.

    Note this is NOT solvable by filtering the firm's boilerplate: Point72
    has 232 open postings and "point72" appears in only 15, so no
    majority-based filter ever sees it. The fix is that the matcher refuses.
    """
    p72 = Firm.objects.create(slug="point72", name="Point72")
    wrong = _opp(p72, "Point72 Academy 2026-2027 Investment Analyst Program "
                      "for Experienced Professionals - US")
    # A pile of unrelated postings, as the real board has.
    for i in range(8):
        _opp(p72, f"Quantitative Researcher {i} - New York")

    m = match_application(list(Opportunity.objects.filter(firm=p72)),
                          "Point72 Academy 2026 Spring Sessions - US")
    assert not m.matched, "a different programme must never be auto-marked"
    assert m.opportunity is not wrong
    assert "did not match" in m.reason
