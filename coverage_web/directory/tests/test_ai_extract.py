"""ai_extract.py: the grounding rule is the whole point of this module, so
most of these tests exist to prove an ungrounded/malformed model answer is
rejected, not to prove a well-formed one is accepted."""

from __future__ import annotations

import json
import urllib.error

import pytest
from django.test import override_settings

from directory import ai_extract
from directory.ai_extract import AIExtractError, DeadlineGuess, complete_text, extract_deadline_ai, is_configured


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _api_text_response(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@override_settings(ANTHROPIC_API_KEY="")
def test_is_configured_false_by_default():
    assert is_configured() is False


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_is_configured_true_when_key_set():
    assert is_configured() is True


@override_settings(ANTHROPIC_API_KEY="")
def test_extract_deadline_ai_no_ops_without_a_key(monkeypatch):
    """No network attempt at all when unconfigured — the whole point of the
    settings-gated default."""
    called = []
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: called.append(1))
    assert extract_deadline_ai("Applications close October 30, 2026.") is None
    assert called == []


@override_settings(ANTHROPIC_API_KEY="")
def test_extract_deadline_ai_empty_text_short_circuits():
    assert extract_deadline_ai("") is None
    assert extract_deadline_ai(None) is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_accepts_a_grounded_quote(monkeypatch):
    text = "Role details below.\nApplications must be received by October 30, 2026 at 11:59pm.\nMore text."
    reply = _api_text_response(json.dumps({
        "deadline_iso": "2026-10-30",
        "quote": "Applications must be received by October 30, 2026 at 11:59pm.",
    }))
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)

    guess = extract_deadline_ai(text)
    assert guess == DeadlineGuess(
        value="2026-10-30",
        phrase="Applications must be received by October 30, 2026 at 11:59pm.",
        confidence=0.5,
    )


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_tolerates_reflowed_whitespace_in_the_quote(monkeypatch):
    """A model that copies a quote but collapses the source's internal
    newlines/double-spaces should not lose an otherwise-real quote — only
    whitespace normalization is forgiven, nothing else."""
    text = "Applications   must be\nreceived by October 30, 2026."
    reply = _api_text_response(json.dumps({
        "deadline_iso": "2026-10-30",
        "quote": "Applications must be received by October 30, 2026.",
    }))
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)
    assert extract_deadline_ai(text) is not None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_rejects_an_ungrounded_quote(monkeypatch):
    """THE core defense: a model that free-associates a plausible date and
    sentence must never reach the database, even with a well-formed date."""
    text = "This posting says nothing about when applications close."
    reply = _api_text_response(json.dumps({
        "deadline_iso": "2026-10-30",
        "quote": "Applications must be received by October 30, 2026.",
    }))
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)
    assert extract_deadline_ai(text) is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_rejects_no_answer(monkeypatch):
    text = "Rolling applications, no fixed deadline."
    reply = _api_text_response(json.dumps({"deadline_iso": None, "quote": None}))
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)
    assert extract_deadline_ai(text) is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_rejects_malformed_json(monkeypatch):
    reply = _api_text_response("not json at all")
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)
    assert extract_deadline_ai("Deadline: whenever.") is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_strips_a_markdown_code_fence(monkeypatch):
    text = "Please apply by November 3, 2026."
    fenced = "```json\n" + json.dumps({
        "deadline_iso": "2026-11-03", "quote": "Please apply by November 3, 2026.",
    }) + "\n```"
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: _api_text_response(fenced))
    guess = extract_deadline_ai(text)
    assert guess is not None
    assert guess.value == "2026-11-03"


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_extract_deadline_ai_rejects_a_non_iso_date(monkeypatch):
    text = "Apply by 30/10/2026."
    reply = _api_text_response(json.dumps({"deadline_iso": "30/10/2026", "quote": "Apply by 30/10/2026."}))
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: reply)
    assert extract_deadline_ai(text) is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_post_json_retries_on_server_error_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError(req.full_url, 500, "server error", None, None)
        return FakeHTTPResponse(_api_text_response("hi"))

    monkeypatch.setattr(ai_extract.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ai_extract.time, "sleep", lambda *a: None)

    result = ai_extract._post_json({"x": 1}, timeout=5, retries=2)
    assert calls["n"] == 2
    assert result["content"][0]["text"] == "hi"


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_post_json_does_not_retry_a_client_error(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 401, "bad key", None, None)

    monkeypatch.setattr(ai_extract.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(AIExtractError):
        ai_extract._post_json({"x": 1}, timeout=5, retries=2)
    assert calls["n"] == 1


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_post_json_raises_after_exhausting_retries(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "down", None, None)

    monkeypatch.setattr(ai_extract.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ai_extract.time, "sleep", lambda *a: None)

    with pytest.raises(AIExtractError):
        ai_extract._post_json({"x": 1}, timeout=5, retries=1)


@override_settings(ANTHROPIC_API_KEY="")
def test_complete_text_returns_none_without_a_key(monkeypatch):
    called = []
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: called.append(1))
    assert complete_text("Write a brief.") is None
    assert called == []


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_complete_text_returns_none_for_blank_prompt():
    assert complete_text("") is None
    assert complete_text("   ") is None


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_complete_text_returns_the_model_text(monkeypatch):
    monkeypatch.setattr(ai_extract, "_post_json", lambda *a, **kw: _api_text_response("Here is your brief."))
    assert complete_text("Write a brief.") == "Here is your brief."


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_complete_text_returns_none_on_api_failure(monkeypatch):
    def raiser(*a, **kw):
        raise AIExtractError(RuntimeError("boom"))
    monkeypatch.setattr(ai_extract, "_post_json", raiser)
    assert complete_text("Write a brief.") is None
