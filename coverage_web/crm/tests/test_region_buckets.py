"""The Network board's three region buckets and the backfill behind them.

The founder's 2026-08-25 report: "i need accurate sorting between united
states and hongkong and other countries this is not working right now." His
live board showed why — 61 of 131 contacts had no region at all, because
`default_region_from_firm` rightly refuses to guess for a firm carrying
['us', 'hk'], and there was no bucket for anyone outside both markets.

What these tests pin down:

- "other" is a real stored region (a person KNOWN to be outside both
  markets), never a synonym for blank/unknown, and the two never mix.
- Inference lives in the reviewed backfill only, fires on positive evidence
  only, and degrades honestly when the evidence (a touch subject) is missing.
- Explicit region still wins everywhere, and the counts a region tab shows
  agree with the cards it renders.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, Touch
from crm.region_inference import infer_region
from directory.models import Firm

User = get_user_model()


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="x")


def _cards(resp):
    return [c for s in resp.context["sections"] for c in s["cards"]]


def _names(client, scope):
    resp = client.get(reverse("crm:contact_list"), {"scope": scope})
    assert resp.status_code == 200
    return {card["c"].name for card in _cards(resp)}


# ---------------------------------------------------------------------------
# The three buckets on the board.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_other_tab_shows_explicit_other_and_only_them(client):
    """A person known to be in London sits in Other countries — and in no
    us/hk tab, however many markets their firm recruits in."""
    user = _user()
    dual = Firm.objects.create(slug="dual", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Lena London", firm=dual,
                              region="other", warmth="replied")
    Contact.all_objects.create(user=user, name="Hana HK", firm=dual,
                              region="hk", warmth="replied")

    client.force_login(user)
    assert _names(client, "other") == {"Lena London"}
    assert _names(client, "hk") == {"Hana HK"}
    assert _names(client, "us") == set()


@pytest.mark.django_db
def test_unknown_is_not_relabelled_other(client):
    """The distinction the whole fix hangs on: a contact with no region at a
    us/hk firm is UNKNOWN — shown in both market tabs on a flagged guess —
    not "somewhere else". The Other tab must not absorb them."""
    user = _user()
    dual = Firm.objects.create(slug="dual", name="Dual Bank", regions=["us", "hk"])
    Contact.all_objects.create(user=user, name="Mystery Person", firm=dual,
                              warmth="replied")

    client.force_login(user)
    assert _names(client, "other") == set()
    assert _names(client, "hk") == {"Mystery Person"}
    assert _names(client, "us") == {"Mystery Person"}


@pytest.mark.django_db
def test_unknown_at_an_outside_market_firm_is_a_flagged_guess_in_other(client):
    """The Other tab keeps the same honesty rule the market tabs have: an
    unknown at a firm whose footprint is entirely outside us/hk shows there
    on a guess, is counted unconfirmed, and gets no region pill."""
    user = _user()
    sg_firm = Firm.objects.create(slug="sg-shop", name="SG Shop", regions=["sg"])
    Contact.all_objects.create(user=user, name="Maybe Singapore", firm=sg_firm,
                              warmth="replied")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"), {"scope": "other"})
    cards = _cards(resp)
    assert {c["c"].name for c in cards} == {"Maybe Singapore"}
    assert resp.context["unconfirmed_total"] == 1
    assert cards[0]["region"] == ""
    assert "no region set" in resp.content.decode()


@pytest.mark.django_db
def test_apac_footprint_is_not_evidence_of_other(client):
    """Jane Street carries ['us', 'apac'], and APAC contains Hong Kong — so
    an unknown there must not be guessed into the Other tab."""
    user = _user()
    js = Firm.objects.create(slug="js", name="Jane Street", regions=["us", "apac"])
    Contact.all_objects.create(user=user, name="Ambiguous Anna", firm=js,
                              warmth="replied")

    client.force_login(user)
    assert _names(client, "other") == set()
    assert _names(client, "us") == {"Ambiguous Anna"}


@pytest.mark.django_db
def test_other_tab_is_offered_even_with_narrowed_interests(client):
    """user.regions narrows the market tabs, but Other is not a market a
    student declares an interest in — it must survive the narrowing."""
    user = _user()
    user.regions = ["hk", "us"]
    user.save(update_fields=["regions"])

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    codes = [r["code"] for r in resp.context["region_scopes"]]
    assert codes == ["hk", "us", "other"]
    labels = {r["code"]: r["label"] for r in resp.context["region_scopes"]}
    assert labels["other"] == "Other countries"


@pytest.mark.django_db
def test_tab_counts_agree_with_rendered_cards(client):
    """The bug class this repo has fixed twice: a header count that disagrees
    with the cards below it. For each region tab, contact_total and
    unconfirmed_total must both describe exactly the rendered set."""
    user = _user()
    dual = Firm.objects.create(slug="dual", name="Dual Bank", regions=["us", "hk"])
    sg_firm = Firm.objects.create(slug="sg-shop", name="SG Shop", regions=["sg"])
    Contact.all_objects.create(user=user, name="Hana HK", firm=dual,
                              region="hk", warmth="replied")
    Contact.all_objects.create(user=user, name="Uma US", firm=dual,
                              region="us", warmth="replied")
    Contact.all_objects.create(user=user, name="Lena London", firm=dual,
                              region="other", warmth="replied")
    Contact.all_objects.create(user=user, name="Mystery Person", firm=dual,
                              warmth="replied")
    Contact.all_objects.create(user=user, name="Maybe Singapore", firm=sg_firm,
                              warmth="replied")

    client.force_login(user)
    for scope in ("hk", "us", "other"):
        resp = client.get(reverse("crm:contact_list"), {"scope": scope})
        cards = _cards(resp)
        assert resp.context["contact_total"] == len(cards), scope
        assert resp.context["unconfirmed_total"] == sum(
            1 for c in cards if not c["c"].region
        ), scope


@pytest.mark.django_db
def test_explicit_region_survives_save_and_the_edit_form_offers_other():
    """Explicit-wins in save(): a hand-set region is never overwritten by the
    firm default. And the fix-one-contact-by-hand path exists: the edit form
    offers all three values plus honest blank."""
    user = _user()
    us_only = Firm.objects.create(slug="us-only", name="US Only", regions=["us"])
    c = Contact.all_objects.create(user=user, name="Lena London", firm=us_only,
                                  region="other")
    c.save()
    c.refresh_from_db()
    assert c.region == "other"

    from crm.forms import ContactForm
    values = [v for v, _ in ContactForm().fields["region"].choices]
    assert values == ["", "us", "hk", "other"]


# ---------------------------------------------------------------------------
# Inference (backfill-only), signal by signal.
# ---------------------------------------------------------------------------
def test_subject_market_tag_beats_everything():
    region, reason = infer_region(
        touch_subjects=["HK Jul 29–31 | Nomura | IBD - USC Student Coffee Chat Request"],
        email="x@jefferies.com", source="Gmail USC discovery",
    )
    assert region == "hk"
    assert "subject" in reason


def test_subject_tag_is_anchored_not_substring():
    # "HSBC ..." and a mid-subject "USC" are not market tags.
    assert infer_region(touch_subjects=["HSBC recruiting update"]) is None
    assert infer_region(touch_subjects=["Invited: USC x West Monroe AMA"]) is None


def test_missing_subject_degrades_to_weaker_signals_honestly():
    # Older touches have subject="" — the signal says nothing, the row falls
    # through to the next tier, and with no tier left it stays unknown.
    assert infer_region(
        touch_subjects=["", ""], email="x@jefferies.com", role="", source="capture",
    ) is None


def test_email_domain_signals():
    assert infer_region(email="a@clsa.com.hk")[0] == "hk"
    assert infer_region(email="a@marshall.usc.edu")[0] == "us"
    assert infer_region(email="a@barclays.co.uk")[0] == "other"
    # .cn is Greater China, not "somewhere else" — his own data has an HK
    # conversation on a .cn domain.
    assert infer_region(email="a@blackstone.com.cn") is None


def test_role_and_source_usc_and_hk_signals():
    assert infer_region(role="USC alum, finance professional")[0] == "us"
    assert infer_region(firm_text="usc")[0] == "us"
    assert infer_region(source="Apollo HK campaign - tech M&A batch")[0] == "hk"
    assert infer_region(source="Gmail USC discovery")[0] == "us"
    # Positive evidence only — no confident default for a mute row.
    assert infer_region(source="manual") is None
    assert infer_region() is None


def test_firm_footprint_signals():
    assert infer_region(firm_regions=["us"])[0] == "us"
    assert infer_region(firm_regions=["sg", "eu"])[0] == "other"
    assert infer_region(firm_regions=["us", "hk"]) is None
    # APAC contains Hong Kong: not evidence of anything.
    assert infer_region(firm_regions=["us", "apac"]) is None
    assert infer_region(firm_regions=["apac"]) is None


# ---------------------------------------------------------------------------
# The backfill command: dry-run first, blanks only, reversible.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_backfill_dry_run_writes_nothing(capsys, tmp_path):
    user = _user()
    c = Contact.all_objects.create(user=user, name="Sammy Yau", email="s@jefferies.com")
    Touch.all_objects.create(user=user, contact=c, ts=timezone.now(), kind="email",
                            subject="HK Jul 29–31 | HSBC | Jefferies - Coffee Chat")

    call_command("backfill_contact_regions", "--user", user.email)
    c.refresh_from_db()
    assert c.region == ""
    out = capsys.readouterr().out
    assert "Dry run" in out and "-> hk" in out


@pytest.mark.django_db
def test_backfill_apply_fills_blanks_never_set_regions_and_reverts(tmp_path):
    user = _user()
    undo = tmp_path / "undo.json"
    hand_set = Contact.all_objects.create(
        user=user, name="Hand Set", region="us", source="Apollo HK campaign",
    )
    blank = Contact.all_objects.create(
        user=user, name="Blank Row", source="Apollo HK campaign",
    )
    mute = Contact.all_objects.create(user=user, name="No Signal", source="capture")

    call_command("backfill_contact_regions", "--user", user.email,
                 "--apply", "--undo-file", str(undo))
    for c in (hand_set, blank, mute):
        c.refresh_from_db()
    # Human word untouched, evidence written, silence left honest.
    assert hand_set.region == "us"
    assert blank.region == "hk"
    assert mute.region == ""

    call_command("backfill_contact_regions", "--user", user.email,
                 "--revert", str(undo))
    blank.refresh_from_db()
    assert blank.region == ""


@pytest.mark.django_db
def test_backfill_revert_keeps_later_human_edits(tmp_path):
    user = _user()
    undo = tmp_path / "undo.json"
    c = Contact.all_objects.create(user=user, name="Corrected Later",
                                  source="Apollo HK campaign")
    call_command("backfill_contact_regions", "--user", user.email,
                 "--apply", "--undo-file", str(undo))
    c.refresh_from_db()
    assert c.region == "hk"

    # The founder corrects the row by hand after the backfill...
    c.region = "us"
    c.save(update_fields=["region"])

    # ...and the revert must not undo his correction.
    call_command("backfill_contact_regions", "--user", user.email,
                 "--revert", str(undo))
    c.refresh_from_db()
    assert c.region == "us"


@pytest.mark.django_db
def test_backfill_is_tenant_scoped(tmp_path):
    """--user means that user's rows and nobody else's."""
    user = _user()
    other = _user("other@example.com")
    theirs = Contact.all_objects.create(user=other, name="Their Row",
                                       source="Apollo HK campaign")
    call_command("backfill_contact_regions", "--user", user.email,
                 "--apply", "--undo-file", str(tmp_path / "u.json"))
    theirs.refresh_from_db()
    assert theirs.region == ""
