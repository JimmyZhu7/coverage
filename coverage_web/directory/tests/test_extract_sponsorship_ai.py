"""extract_sponsorship_ai — Decision 3's AI pass over the residue steps 1-3
of docs/founder-decisions-2026-08-20.md leave unanswered.

Every test here mocks `extract_sponsorship_ai` (the ai_extract function) or
`_post_json` beneath it — nothing in this file makes a real network call,
which matters because this command is founder-run-only and must never be
exercised against the real API in CI or in any automated session.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.test import override_settings

from directory import ai_extract
from directory.ai_extract import SponsorshipGuess
from directory.management.commands import extract_sponsorship_ai as cmd_mod
from directory.models import Firm, Opportunity


def _row(**kw):
    firm = kw.pop("firm", None) or Firm.objects.get_or_create(
        slug="jpm", defaults={"name": "JPMorgan"})[0]
    defaults = dict(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        region="us", url="https://jpm.example/apply/1",
        raw={"detail_text": "This role requires visa sponsorship eligibility to be confirmed."},
    )
    defaults.update(kw)
    return Opportunity.objects.create(**defaults)


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="")
def test_command_noop_when_not_configured(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: called.append(1))
    _row()
    call_command("extract_sponsorship_ai")
    assert called == []
    assert "not set" in capsys.readouterr().out


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_default_run_writes_nothing_but_still_calls_the_model(monkeypatch):
    """Dry-run is the DEFAULT for this command (unlike extract_deadlines_ai)
    — Decision 3 asks for a cost estimate before any spend commits, and
    --commit is required to actually write."""
    opp = _row()
    monkeypatch.setattr(
        cmd_mod, "extract_sponsorship_ai",
        lambda text, **kw: SponsorshipGuess(value="no", phrase="x", confidence=0.5),
    )
    call_command("extract_sponsorship_ai")
    opp.refresh_from_db()
    assert opp.sponsorship == "unknown"


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_commit_writes_the_answer_and_tags_its_source(monkeypatch):
    opp = _row()
    monkeypatch.setattr(
        cmd_mod, "extract_sponsorship_ai",
        lambda text, **kw: SponsorshipGuess(
            value="no", phrase="Visa sponsorship is not available for this role.",
            confidence=0.5),
    )
    call_command("extract_sponsorship_ai", "--commit")
    opp.refresh_from_db()
    assert opp.sponsorship == "no"
    assert opp.raw["sponsorship_source"] == "ai"
    assert opp.raw["sponsorship_quote"] == "Visa sponsorship is not available for this role."
    # The cached text itself must never be clobbered by the write.
    assert opp.raw["detail_text"] == "This role requires visa sponsorship eligibility to be confirmed."


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_row_with_no_sponsorship_keyword_is_never_sent(monkeypatch):
    """The keyword gate is the whole point of the cost cap: a row that never
    mentions sponsor/visa/work authorisation/etc. cannot possibly have an
    answer, and sending it would only buy a guaranteed 'no answer' at full
    price."""
    _row(raw={"detail_text": "This role focuses on equity research and financial modeling."})
    called = []
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: called.append(1))
    call_command("extract_sponsorship_ai", "--commit")
    assert called == []


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_row_the_posting_already_answers_is_never_sent(monkeypatch):
    """Fill-only: a posting that already states an answer must never be
    the AI pass's business, regardless of keyword presence."""
    _row(sponsorship="yes")
    called = []
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: called.append(1))
    call_command("extract_sponsorship_ai", "--commit")
    assert called == []


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_row_the_firm_policy_already_answers_is_never_sent(monkeypatch):
    """The AI pass sits AFTER step 3 (firm-policy plumbing) in the sequence
    — a row the firm's own per-region policy already answers must never
    reach the model, matching directory.sponsorship.effective_sponsorship's
    precedence."""
    firm = Firm.objects.create(slug="db3", name="Deutsche Bank Three",
                               regions=["us"], sponsors={"us": False})
    _row(firm=firm, raw={"detail_text": "Visa sponsorship eligibility will be assessed."})
    called = []
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: called.append(1))
    call_command("extract_sponsorship_ai", "--commit")
    assert called == []


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_row_with_no_cached_text_is_never_sent(monkeypatch):
    _row(raw={})
    called = []
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: called.append(1))
    call_command("extract_sponsorship_ai", "--commit")
    assert called == []


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_command_prints_an_estimated_cost_before_any_call(monkeypatch, capsys):
    _row()
    monkeypatch.setattr(
        cmd_mod, "extract_sponsorship_ai",
        lambda text, **kw: SponsorshipGuess(value="no", phrase="x", confidence=0.5),
    )
    call_command("extract_sponsorship_ai")
    out = capsys.readouterr().out
    assert "estimated cost $" in out


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_leaves_a_row_untouched_when_the_model_finds_nothing(monkeypatch):
    opp = _row()
    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", lambda *a, **kw: None)
    call_command("extract_sponsorship_ai", "--commit")
    opp.refresh_from_db()
    assert opp.sponsorship == "unknown"


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_respects_ids_and_ignores_limit(monkeypatch):
    a = _row(url="https://jpm.example/apply/a")
    b = _row(url="https://jpm.example/apply/b")
    monkeypatch.setattr(
        cmd_mod, "extract_sponsorship_ai",
        lambda text, **kw: SponsorshipGuess(value="no", phrase="x", confidence=0.5),
    )
    call_command("extract_sponsorship_ai", f"--ids={a.id}", "--limit=0", "--commit")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.sponsorship == "no"
    assert b.sponsorship == "unknown"


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_continues_past_an_api_failure(monkeypatch, capsys):
    a = _row(url="https://jpm.example/apply/a", raw={"detail_text": "row a visa sponsorship text"})
    b = _row(url="https://jpm.example/apply/b", raw={"detail_text": "row b visa sponsorship text"})

    def flaky(text, **kw):
        if "row a" in text:
            raise ai_extract.AIExtractError(RuntimeError("boom"))
        return SponsorshipGuess(value="no", phrase="x", confidence=0.5)

    monkeypatch.setattr(cmd_mod, "extract_sponsorship_ai", flaky)
    call_command("extract_sponsorship_ai", "--commit")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.sponsorship == "unknown"
    assert b.sponsorship == "no"
    assert "1 API failure" in capsys.readouterr().out
