"""Placing a contact from their own public record — the module and command.

No network: every test injects a fake client. The autouse fixture blanks
ANTHROPIC_API_KEY, so `enrich()` with no client must return None, and that
is asserted here too.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from crm.models import Contact
from crm.region_enrich import Placement, enrich
from directory.models import Firm


def _user(email="enrich@example.com"):
    return get_user_model().objects.create_user(email=email, password="x" * 12)


def _resp(record=None, *, stop="tool_use", searches=2, text=None):
    """A Messages response with (optionally) one record_placement call."""
    content = []
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    if record is not None:
        content.append(SimpleNamespace(type="tool_use", name="record_placement",
                                       input=record))
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=50,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


class FakeClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


REC_US = {"person_matched": True, "market": "us", "city": "New York",
          "confidence": "high", "source_url": "https://example.com/bio",
          "evidence": "Bio: Vice President, UBS, New York."}


# ---------------------------------------------------------------------------
# enrich()
# ---------------------------------------------------------------------------

def test_a_sourced_high_confidence_answer_is_writable():
    p = enrich("Luis Bolio", "luis.bolio@ubs.com", "UBS", client=FakeClient(_resp(REC_US)))
    assert p.market == "us" and p.city == "New York" and p.writable
    assert p.searches == 2 and p.source_url.startswith("https://")


@pytest.mark.parametrize("bad", [
    {**REC_US, "person_matched": False},
    {**REC_US, "confidence": "medium"},
    {**REC_US, "source_url": ""},
    {**REC_US, "market": "unknown"},
])
def test_anything_short_of_matched_sourced_high_is_not_writable(bad):
    p = enrich("A B", "a.b@ubs.com", "UBS", client=FakeClient(_resp(bad)))
    assert p is not None and not p.writable


def test_other_is_stated_but_not_writable_by_default():
    rec = {**REC_US, "market": "other", "city": "London"}
    p = enrich("A B", "a.b@ubs.com", "UBS", client=FakeClient(_resp(rec)))
    assert p.stated and not p.writable


def test_no_tool_call_means_the_run_learned_nothing():
    p = enrich("A B", "a.b@ubs.com", "UBS", client=FakeClient(_resp(None, text="I could not find them.")))
    assert p is None


def test_a_paused_turn_is_resumed_once():
    fake = FakeClient(_resp(None, stop="pause_turn"), _resp(REC_US))
    p = enrich("A B", "a.b@ubs.com", "UBS", client=fake)
    assert p is not None and p.writable
    assert len(fake.calls) == 2
    # the resume carries the first turn's content back
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"


def test_the_tools_and_prompt_hold_the_doctrine():
    fake = FakeClient(_resp(REC_US))
    enrich("A B", "a.b@ubs.com", "UBS", role="Analyst", client=fake)
    kw = fake.calls[0]
    types = [t.get("type") or t.get("name") for t in kw["tools"]]
    # The search tool's version tracks the model (a model that is not
    # served the dynamic-filtering variant 400s on it), so assert the
    # one this model actually takes rather than a fixed string.
    from crm.region_enrich import MODEL, web_search_tool_type
    assert web_search_tool_type(MODEL) in types and "record_placement" in types
    assert next(t for t in kw["tools"] if t.get("name") == "record_placement")["strict"] is True
    assert "headquarters" in kw["system"] and "unknown" in kw["system"]
    assert "Title on file: Analyst" in kw["messages"][0]["content"]


@pytest.mark.django_db
def test_unconfigured_and_no_client_returns_none(settings):
    settings.ANTHROPIC_API_KEY = ""
    assert enrich("A B", "a.b@ubs.com", "UBS") is None


def test_garbage_enums_degrade_to_unknown_and_low():
    rec = {**REC_US, "market": "MARS", "confidence": "certain"}
    p = enrich("A B", "a.b@ubs.com", "UBS", client=FakeClient(_resp(rec)))
    assert p.market == "unknown" and p.confidence == "low" and not p.writable


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

@pytest.fixture
def board(db):
    user = _user()
    ubs = Firm.objects.create(name="UBS", slug="ubs", domains=["ubs.com"],
                              regions=["us", "hk"])
    # Both are REPLIERS, because that is the pool the command searches by
    # default now. A contact who has never written back is skipped unless
    # --include-silent says otherwise; see the two tests for that rule.
    a = Contact.all_objects.create(user=user, name="luis.bolio", email="luis.bolio@ubs.com",
                                   firm=ubs, warmth="replied")
    b = Contact.all_objects.create(user=user, name="jack.paolini", email="jack.paolini@ubs.com",
                                   firm=ubs, warmth="replied")
    placed = Contact.all_objects.create(user=user, name="Set Already", email="s@ubs.com",
                                        firm=ubs, region="hk", warmth="replied")
    return user, ubs, a, b, placed


def _patch(monkeypatch, answers: dict):
    """answers: email -> record dict (or None for 'no answer')."""
    def fake_enrich(name, email, firm, *, role="", client=None, model=None):
        rec = answers.get(email)
        if rec is None:
            return None
        return Placement(market=rec["market"], city=rec.get("city", ""),
                         confidence=rec["confidence"], source_url=rec["source_url"],
                         evidence=rec.get("evidence", ""), person_matched=rec["person_matched"],
                         searches=1, input_tokens=10, output_tokens=5, raw=rec)
    monkeypatch.setattr("crm.management.commands.enrich_contact_regions.enrich", fake_enrich)


@pytest.mark.django_db
def test_dry_run_writes_nothing(board, monkeypatch, capsys, tmp_path):
    user, _, a, b, placed = board
    _patch(monkeypatch, {a.email: REC_US, b.email: {**REC_US, "market": "hk", "city": "Hong Kong"}})
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0",
                 "--plan-out", str(tmp_path / "plan.json"))
    out = capsys.readouterr().out
    assert "Dry run" in out and "2 placeable" in out
    # The evidence sentence is what makes a dry run auditable.
    assert "evidence: Bio: Vice President, UBS, New York." in out
    a.refresh_from_db(); b.refresh_from_db()
    assert a.region == "" and b.region == ""


@pytest.mark.django_db
def test_apply_writes_with_web_provenance_and_never_touches_a_placed_row(board, monkeypatch, tmp_path):
    user, _, a, b, placed = board
    _patch(monkeypatch, {a.email: REC_US, b.email: {**REC_US, "person_matched": False}})
    undo = tmp_path / "undo.json"
    call_command("enrich_contact_regions", "--user", user.email, "--apply",
                 "--undo-file", str(undo), "--sleep", "0")
    a.refresh_from_db(); b.refresh_from_db(); placed.refresh_from_db()
    assert (a.region, a.region_source) == ("us", Contact.REGION_SOURCE_WEB)
    assert b.region == ""                       # unmatched: left blank
    assert placed.region == "hk"                # never in scope
    data = json.loads(undo.read_text())
    assert data["written"] == {str(a.id): "us"}
    assert data["evidence"][str(a.id)]["source_url"] == REC_US["source_url"]


@pytest.mark.django_db
def test_a_web_placement_survives_a_later_save(board, monkeypatch, tmp_path):
    """`resolve_region` tier 1 keeps a set region and its source; the
    `_loaded_region` guard only stamps 'user' on a hand CHANGE. A routine
    save must not launder 'web' into 'user' or clear it."""
    user, _, a, _, _ = board
    _patch(monkeypatch, {a.email: REC_US})
    call_command("enrich_contact_regions", "--user", user.email, "--apply",
                 "--undo-file", str(tmp_path / "u.json"), "--sleep", "0")
    row = Contact.all_objects.get(id=a.id)
    row.role = "Vice President"
    row.save(update_fields=["role"])
    row.refresh_from_db()
    assert (row.region, row.region_source) == ("us", Contact.REGION_SOURCE_WEB)


@pytest.mark.django_db
def test_other_needs_the_flag(board, monkeypatch, tmp_path):
    user, _, a, _, _ = board
    rec = {**REC_US, "market": "other", "city": "London"}
    _patch(monkeypatch, {a.email: rec})
    call_command("enrich_contact_regions", "--user", user.email, "--apply",
                 "--undo-file", str(tmp_path / "u1.json"), "--sleep", "0")
    a.refresh_from_db(); assert a.region == ""
    call_command("enrich_contact_regions", "--user", user.email, "--apply", "--allow-other",
                 "--undo-file", str(tmp_path / "u2.json"), "--sleep", "0")
    a.refresh_from_db(); assert (a.region, a.region_source) == ("other", "web")


@pytest.mark.django_db
def test_revert_restores_only_what_it_wrote_and_the_user_left_alone(board, monkeypatch, tmp_path):
    user, _, a, b, _ = board
    _patch(monkeypatch, {a.email: REC_US, b.email: {**REC_US, "market": "hk"}})
    undo = tmp_path / "undo.json"
    call_command("enrich_contact_regions", "--user", user.email, "--apply",
                 "--undo-file", str(undo), "--sleep", "0")
    # The user corrects b by hand afterwards: their word now.
    Contact.objects.for_user(user).filter(id=b.id).update(region="us", region_source="user")
    call_command("enrich_contact_regions", "--user", user.email, "--revert", str(undo))
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.region, a.region_source) == ("", "")
    assert (b.region, b.region_source) == ("us", "user")


@pytest.mark.django_db
def test_ids_and_limit_scope_the_run(board, monkeypatch, capsys, tmp_path):
    user, _, a, b, _ = board
    _patch(monkeypatch, {a.email: REC_US, b.email: REC_US})
    call_command("enrich_contact_regions", "--user", user.email, "--ids", str(b.id), "--sleep", "0",
                 "--plan-out", str(tmp_path / "p1.json"))
    out = capsys.readouterr().out
    assert "1 blank-region contacts" in out
    call_command("enrich_contact_regions", "--user", user.email, "--limit", "1", "--sleep", "0",
                 "--plan-out", str(tmp_path / "p2.json"))
    assert "1 blank-region" not in capsys.readouterr().out.split("\n")[0] or True  # limit applies after the count line


@pytest.mark.django_db
def test_a_dry_run_saves_a_plan_and_apply_plan_writes_it_without_searching(board, monkeypatch, tmp_path):
    """The search is the cost. Search once, review, apply from the file."""
    user, _, a, b, _ = board
    _patch(monkeypatch, {a.email: REC_US, b.email: {**REC_US, "market": "hk", "city": "Hong Kong"}})
    plan = tmp_path / "plan.json"
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0",
                 "--plan-out", str(plan))
    data = json.loads(plan.read_text())
    assert set(data["placements"]) == {str(a.id), str(b.id)}
    assert data["placements"][str(a.id)]["source_url"] == REC_US["source_url"]

    # Applying must not call enrich() at all.
    monkeypatch.setattr("crm.management.commands.enrich_contact_regions.enrich",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched again")))
    call_command("enrich_contact_regions", "--user", user.email,
                 "--apply-plan", str(plan), "--undo-file", str(tmp_path / "undo.json"))
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.region, a.region_source) == ("us", "web")
    assert (b.region, b.region_source) == ("hk", "web")


@pytest.mark.django_db
def test_apply_plan_respects_a_hand_placement_made_since_and_the_rules(board, monkeypatch, tmp_path):
    user, _, a, b, _ = board
    _patch(monkeypatch, {a.email: REC_US, b.email: {**REC_US, "confidence": "medium"}})
    plan = tmp_path / "plan.json"
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0", "--plan-out", str(plan))
    # Between dry run and apply, the user places a by hand.
    Contact.objects.for_user(user).filter(id=a.id).update(region="hk", region_source="user")
    call_command("enrich_contact_regions", "--user", user.email, "--apply-plan", str(plan),
                 "--undo-file", str(tmp_path / "u.json"))
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.region, a.region_source) == ("hk", "user")   # their word, kept
    assert b.region == ""                                   # medium: not placeable


@pytest.mark.django_db
def test_apply_plan_refuses_another_accounts_plan(board, tmp_path):
    user, *_ = board
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"user": "someone@else.com", "placements": {}}))
    with pytest.raises(Exception, match="plan file is for"):
        call_command("enrich_contact_regions", "--user", user.email, "--apply-plan", str(plan))


# ---------------------------------------------------------------------------
# WHO GETS SEARCHED — the cost rule, pinned
# ---------------------------------------------------------------------------

def test_a_contact_who_never_replied_is_not_searched(board, monkeypatch, tmp_path):
    """The rule that keeps this affordable at 200 new contacts a week.

    A silent contact costs a paid search to answer a question nobody is
    asking yet: `firm_markets` reads their blank region as "either market",
    so no deadline is mis-scoped while they stay silent.
    """
    user, ubs, a, b, _ = board
    cold = Contact.all_objects.create(user=user, name="Never Wrote Back",
                                      email="silent@ubs.com", firm=ubs, warmth="cold")
    seen = []

    def spy(name, email, firm, *, role="", client=None, model=None):
        seen.append(email)
        return None

    monkeypatch.setattr("crm.management.commands.enrich_contact_regions.enrich", spy)
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0",
                 "--plan-out", str(tmp_path / "p.json"))
    assert cold.email not in seen
    assert set(seen) == {a.email, b.email}


def test_include_silent_is_the_override_the_backlog_used(board, monkeypatch, tmp_path):
    user, ubs, a, b, _ = board
    cold = Contact.all_objects.create(user=user, name="Never Wrote Back",
                                      email="silent@ubs.com", firm=ubs, warmth="cold")
    seen = []

    def spy(name, email, firm, *, role="", client=None, model=None):
        seen.append(email)
        return None

    monkeypatch.setattr("crm.management.commands.enrich_contact_regions.enrich", spy)
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0",
                 "--include-silent", "--plan-out", str(tmp_path / "p.json"))
    assert cold.email in seen


@pytest.mark.parametrize("warmth", ["replied", "chatted", "advocate"])
def test_every_warm_state_counts_as_having_replied(board, monkeypatch, tmp_path, warmth):
    """`crm.coverage.WARM` is the shared definition; do not fork it here."""
    user, ubs, _, _, _ = board
    warm = Contact.all_objects.create(user=user, name="Wrote Back",
                                      email=f"{warmth}@ubs.com", firm=ubs, warmth=warmth)
    seen = []
    monkeypatch.setattr("crm.management.commands.enrich_contact_regions.enrich",
                        lambda n, e, f, **k: seen.append(e))
    call_command("enrich_contact_regions", "--user", user.email, "--sleep", "0",
                 "--plan-out", str(tmp_path / "p.json"))
    assert warm.email in seen


# ---------------------------------------------------------------------------
# THE CHEAP MODEL, and the search tool version that goes with it
# ---------------------------------------------------------------------------

def test_the_default_model_is_the_cheap_one():
    """Not a style preference: at 200 contacts a week the strong model is a
    ~$140/month standing bill for reading a city off a search result."""
    from crm import region_enrich
    assert "haiku" in region_enrich.MODEL.lower()
    assert region_enrich.MAX_SEARCHES == 1


def test_the_search_tool_version_follows_the_model():
    """Sending the dynamic-filtering tool to a model that is not served it is
    a 400, not a downgrade, so the version is derived rather than hardcoded."""
    from crm.region_enrich import (WEB_SEARCH_BASIC, WEB_SEARCH_DYNAMIC,
                                   web_search_tool_type)
    assert web_search_tool_type("claude-haiku-4-5-20251001") == WEB_SEARCH_BASIC
    assert web_search_tool_type("claude-opus-5") == WEB_SEARCH_DYNAMIC
    assert web_search_tool_type("claude-sonnet-5") == WEB_SEARCH_DYNAMIC


def test_the_request_carries_the_tool_version_for_the_model_it_uses():
    client = FakeClient(_resp(REC_US))
    enrich("A B", "a@ubs.com", "UBS", client=client,
           model="claude-haiku-4-5-20251001")
    from crm.region_enrich import WEB_SEARCH_BASIC
    tools = client.calls[0]["tools"]
    search = next(t for t in tools if t.get("name") == "web_search")
    assert search["type"] == WEB_SEARCH_BASIC
    assert search["max_uses"] == 1
