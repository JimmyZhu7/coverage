"""The opener-drafting pipeline's two commands, and the contract they hold:
fill-only, substance-gated, drafts never mail."""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from crm.models import Contact

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="j@x.com", password="x")


def _contact(user, name, **kw):
    return Contact.all_objects.create(user=user, name=name, **kw)


def _worklist(capsys, email, **kw):
    call_command("opener_worklist", email=email, **kw)
    return capsys.readouterr().out


def test_worklist_needs_substance_not_just_a_name(user, capsys):
    """"Ben, Citi" and nothing else can only be padded with invented rapport,
    and an invented opener is worse than the empty Compose it replaces."""
    _contact(user, "Rich Contact", firm_text="Citi", role="IB Associate")
    _contact(user, "Thin Contact", firm_text="Citi")
    out = _worklist(capsys, "j@x.com")
    assert "Rich Contact" in out.split("NEEDS_SUBSTANCE")[0]
    assert "NEEDS_SUBSTANCE: Thin Contact" in out


def test_worklist_skips_contacts_that_already_have_a_draft(user, capsys):
    _contact(user, "Has Draft", firm_text="Citi", role="VP", opener="hi")
    out = _worklist(capsys, "j@x.com")
    assert "Has Draft" not in out


def test_apply_is_fill_only_and_never_clobbers(user, tmp_path):
    fresh = _contact(user, "Fresh", firm_text="Citi", role="VP")
    edited = _contact(user, "Edited", firm_text="GS", role="MD",
                      opener="my own words")
    f = tmp_path / "f.json"
    f.write_text(json.dumps([
        {"id": fresh.id, "opener": "Drafted note."},
        {"id": edited.id, "opener": "Overwrite attempt."},
    ]))
    call_command("capture_openers", email="j@x.com", findings=str(f))
    fresh.refresh_from_db(); edited.refresh_from_db()
    assert fresh.opener == "Drafted note."
    assert edited.opener == "my own words", "the field belongs to the user"


def test_apply_refuses_an_overlong_draft_rather_than_truncating(user, tmp_path):
    c = _contact(user, "Fresh", firm_text="Citi", role="VP")
    f = tmp_path / "f.json"
    f.write_text(json.dumps([{"id": c.id, "opener": "x" * 700}]))
    call_command("capture_openers", email="j@x.com", findings=str(f))
    c.refresh_from_db()
    assert c.opener == "", "a truncated draft ends mid-sentence in Compose"


def test_apply_is_tenant_scoped(user, django_user_model, tmp_path):
    other = django_user_model.objects.create_user(email="o@x.com", password="x")
    theirs = _contact(other, "Not Yours", firm_text="Citi", role="VP")
    f = tmp_path / "f.json"
    f.write_text(json.dumps([{"id": theirs.id, "opener": "note"}]))
    call_command("capture_openers", email="j@x.com", findings=str(f))
    theirs.refresh_from_db()
    assert theirs.opener == ""


def test_dry_run_writes_nothing(user, tmp_path):
    c = _contact(user, "Fresh", firm_text="Citi", role="VP")
    f = tmp_path / "f.json"
    f.write_text(json.dumps([{"id": c.id, "opener": "Drafted."}]))
    call_command("capture_openers", email="j@x.com", findings=str(f), dry_run=True)
    c.refresh_from_db()
    assert c.opener == ""
