from __future__ import annotations

import pytest
from django.core.management import call_command
from django.test import override_settings

from directory import ai_extract
from directory.ai_extract import DeadlineGuess
from directory.management.commands import extract_deadlines_ai as cmd_mod
from directory.models import Firm, Opportunity


def _row(**kw):
    firm = kw.pop("firm", None) or Firm.objects.get_or_create(slug="jpm", defaults={"name": "JPMorgan"})[0]
    defaults = dict(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://jpm.example/apply/1",
        raw={"detail_text": "Applications close November 3, 2026."},
    )
    defaults.update(kw)
    return Opportunity.objects.create(**defaults)


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="")
def test_command_noop_when_not_configured(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cmd_mod, "extract_deadline_ai", lambda *a, **kw: called.append(1))
    _row()
    call_command("extract_deadlines_ai")
    assert called == []
    assert "not set" in capsys.readouterr().out


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_dry_run_writes_nothing(monkeypatch):
    opp = _row()
    monkeypatch.setattr(
        cmd_mod, "extract_deadline_ai",
        lambda text, **kw: DeadlineGuess(value="2026-11-03", phrase="x", confidence=0.5),
    )
    call_command("extract_deadlines_ai", "--dry-run")
    opp.refresh_from_db()
    assert opp.deadline is None


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_writes_deadline_and_tags_source(monkeypatch):
    opp = _row()
    monkeypatch.setattr(
        cmd_mod, "extract_deadline_ai",
        lambda text, **kw: DeadlineGuess(value="2026-11-03", phrase="Applications close November 3, 2026.",
                                          confidence=0.5),
    )
    call_command("extract_deadlines_ai")
    opp.refresh_from_db()
    assert opp.deadline.isoformat() == "2026-11-03"
    assert opp.deadline_precision == "day"
    assert opp.confidence == 0.5
    assert opp.raw["deadline_source"] == "ai"
    assert opp.raw["detail_text"] == "Applications close November 3, 2026."  # not clobbered


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_skips_a_row_that_already_has_a_deadline(monkeypatch):
    """This command only fills silence — it must never be the second writer
    a regex- or provider-stated deadline has to fight with."""
    opp = _row(deadline="2026-09-01", deadline_precision="day", confidence=1.0)
    called = []
    monkeypatch.setattr(cmd_mod, "extract_deadline_ai", lambda *a, **kw: called.append(1))
    call_command("extract_deadlines_ai")
    assert called == []
    opp.refresh_from_db()
    assert opp.deadline.isoformat() == "2026-09-01"


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_skips_a_row_with_no_cached_text(monkeypatch):
    _row(raw={})
    called = []
    monkeypatch.setattr(cmd_mod, "extract_deadline_ai", lambda *a, **kw: called.append(1))
    call_command("extract_deadlines_ai")
    assert called == []


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_leaves_a_row_untouched_when_the_model_finds_nothing(monkeypatch):
    opp = _row()
    monkeypatch.setattr(cmd_mod, "extract_deadline_ai", lambda *a, **kw: None)
    call_command("extract_deadlines_ai")
    opp.refresh_from_db()
    assert opp.deadline is None


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_respects_ids_and_ignores_limit(monkeypatch):
    a = _row(url="https://jpm.example/apply/a")
    b = _row(url="https://jpm.example/apply/b")
    monkeypatch.setattr(
        cmd_mod, "extract_deadline_ai",
        lambda text, **kw: DeadlineGuess(value="2026-11-03", phrase="x", confidence=0.5),
    )
    call_command("extract_deadlines_ai", f"--ids={a.id}", "--limit=0")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.deadline is not None
    assert b.deadline is None


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_command_continues_past_an_api_failure(monkeypatch, capsys):
    a = _row(url="https://jpm.example/apply/a")
    b = _row(url="https://jpm.example/apply/b")

    def flaky(text, **kw):
        if "a" in text:
            raise ai_extract.AIExtractError(RuntimeError("boom"))
        return DeadlineGuess(value="2026-11-03", phrase="x", confidence=0.5)

    # Distinguish the two rows via their cached text so the fake can fail
    # on exactly one without depending on call order.
    a.raw = {"detail_text": "row a text"}
    a.save(update_fields=["raw"])
    b.raw = {"detail_text": "row b text"}
    b.save(update_fields=["raw"])
    monkeypatch.setattr(cmd_mod, "extract_deadline_ai", flaky)

    call_command("extract_deadlines_ai")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.deadline is None
    assert b.deadline is not None
    assert "1 API failure" in capsys.readouterr().out
