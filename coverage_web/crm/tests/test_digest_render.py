"""The digest as it lands in an inbox: what it says, and what it lets you do.

crm/tests/test_digest.py pins the ASSEMBLY (the rules the module reuses
rather than re-derives). This file pins the RENDER, which is where the
2026-09-01 audit found five defects, every one of them visible in the
founder's own live digest:

  1. two byte-identical "Goldman Sachs · Investment Banking Quantitative
     Strats" rows under New for you, reading as a duplicate bug. They are New
     York and Dallas. `_new_for_you` already runs the feed's own
     `directory.dupes.fold_duplicates`, which refuses to fold two cities on
     purpose ("the firm, the role and the place all say the same thing"), and
     the plain-text half has printed the location all along. The HTML half
     was dropping it.
  2. "For 2027-2028 grads — you", a chip written for a hover tooltip, joined
     into an inbox line where it dangles, with an em dash the house style
     does not use (P7).
  3. no unsubscribe link at all. The footer explained why the mail arrived
     and offered nothing to do about it; the only off switch was a toggle on
     a page you have to be signed in to reach.
  4. "Sept. 2", Django's AP-style `N`, where the whole rest of the product
     writes "Sep 2".
  5. two 999px pills and an 8px button, in a product whose controls are all
     10px, and no dark palette at all.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils import timezone

from crm.digest import assemble_digest, why_line
from crm.models import Contact, Touch, UserFirm
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
TODAY = timezone.localdate()


# ---------------------------------------------------------------------------
# why_line: the scorer's tooltip wording, in the digest's voice.
# ---------------------------------------------------------------------------

def test_why_line_turns_the_dangling_tail_into_a_parenthetical():
    assert why_line("Tier 1 · matches IB · For 2027-2028 grads — you") == (
        "Tier 1 · matches IB · For 2027-2028 grads (yours)"
    )


def test_why_line_survives_the_scorers_rewording():
    """The pass that owns directory/recommend.py is changing the label to
    "For 2027-2028 grads (yours)". Both spellings have to come out right, or
    the two passes break each other on merge."""
    already_fixed = "Tier 1 · For 2027-2028 grads (yours)"
    assert why_line(already_fixed) == already_fixed


def test_why_line_leaves_no_em_dash_behind_whatever_the_source_writes():
    out = why_line("Tier 1 — a firm you tiered — and an IB role")
    assert "—" not in out
    assert out == "Tier 1, a firm you tiered, and an IB role"


def test_why_line_normalises_an_en_dashed_year_range():
    assert why_line("For 2027–2028 grads") == "For 2027-2028 grads"


def test_why_line_is_punctuation_only_and_never_drops_a_claim():
    """A digest that edited the scorer's reasons would be a second definition
    of "why this role" (P5). Every word survives; only the joins change."""
    source = "Tier 1 · matches IB · US · For 2027-2028 grads — you"
    out = why_line(source)
    for claim in ("Tier 1", "matches IB", "US", "2027-2028 grads"):
        assert claim in out


def test_why_line_handles_nothing_at_all():
    assert why_line("") == ""
    assert why_line(None) == ""


# ---------------------------------------------------------------------------
# The rendered email.
# ---------------------------------------------------------------------------

@pytest.fixture
def digest_ctx():
    """A digest with one closing row and one action, so `assemble_digest`
    returns something, plus two picks that differ only by city."""
    user = User.objects.create_user(email="reader@example.com", password="pw12345!")
    firm = Firm.objects.create(name="Goldman Sachs", slug="gs")
    # all_objects, with the reason stated: fixture setup is a deliberate
    # write for one known user, which is the case tenancy.py's manager
    # reserves the escape hatch for.
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    closing = Opportunity.objects.create(
        firm=firm, url="https://x/gs/close", title="Summer Analyst",
        bucket="internship", status="open", deadline=TODAY + timedelta(days=3),
        location="London",
    )
    from analytics.models import UserOpportunity
    UserOpportunity.all_objects.create(user=user, opportunity=closing, applied_status="saved")

    contact = Contact.all_objects.create(
        user=user, name="Sarah Lin", firm=firm, email="sarah@gs.com", warmth="replied",
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=30),
    )

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    return user, digest


def _render(user, digest):
    return render_to_string(
        "crm/emails/weekly_digest.html",
        {"user": user, "digest": digest, "site_url": "https://coverage.test"},
    )


def test_the_html_half_prints_a_picks_location(digest_ctx):
    """Two picks that differ only by city must not render byte-identical.

    Built by hand rather than by scoring, because what is under test is the
    template, not the recommender: any two rows sharing a firm and a title
    are the failure this guards.
    """
    user, digest = digest_ctx
    digest["picks"] = [
        {"firm_name": "Goldman Sachs", "title": "IB Quant Strats", "url": "https://x/1",
         "location": "New York", "why": "Tier 1", "deadline_marker": {}, "reported": None},
        {"firm_name": "Goldman Sachs", "title": "IB Quant Strats", "url": "https://x/2",
         "location": "Dallas", "why": "Tier 1", "deadline_marker": {}, "reported": None},
    ]
    html = _render(user, digest)

    assert "New York" in html and "Dallas" in html
    rows = re.findall(r"IB Quant Strats.*?</td>", html, re.S)
    assert len(rows) == 2
    assert rows[0] != rows[1], "two picks at one firm must not render identically"


def test_the_date_is_the_apps_own_month_abbreviation(digest_ctx):
    user, digest = digest_ctx
    digest["today"] = date(2026, 9, 2)

    html = _render(user, digest)
    assert "Your week of Sep 2" in html
    assert "Sept." not in html, "`N` is AP style; the rest of the product uses `M`"


def test_every_control_takes_the_apps_control_radius(digest_ctx):
    user, digest = digest_ctx
    html = _render(user, digest)

    assert "border-radius:999px" not in html, (
        "the ping pills were the only capsules the email had, in a product "
        "whose controls are all squared"
    )
    assert "border-radius:8px" not in html, "8px was a third radius"
    assert html.count("border-radius:10px") >= 1


def test_the_email_carries_a_dark_palette_for_apple_mail(digest_ctx):
    user, digest = digest_ctx
    html = _render(user, digest)

    assert "@media (prefers-color-scheme: dark)" in html
    # The site's own four, copied as literals because mail clients resolve no
    # custom properties.
    for token in ("#141712", "#1c201a", "#eaece5", "#7aa7d4"):
        assert token in html, f"{token} is one of the four tokens the site flips"
    # The hooks are additive: every element still carries its full inline
    # style, so a client that strips the <style> loses nothing.
    assert 'class="d-page" style="background-color:#f2f4ee' in html


def test_the_footer_offers_a_way_out(digest_ctx):
    from django.urls import reverse

    user, digest = digest_ctx
    html = _render(user, digest)

    url = reverse("accounts:digest_unsubscribe", args=[digest["unsubscribe_token"]])
    assert f"https://coverage.test{url}" in html
    assert "Stop these emails" in html


def test_the_plain_text_half_offers_the_same_way_out(digest_ctx):
    user, digest = digest_ctx
    text = render_to_string(
        "crm/emails/weekly_digest.txt",
        {"user": user, "digest": digest, "site_url": "https://coverage.test"},
    )

    assert "Stop these emails: https://coverage.test/welcome/unsubscribe/" in text
    assert "Sept." not in text


# ---------------------------------------------------------------------------
# D-11: both halves of the email say which mode the section is in, and print
# the age every pick's place in it rests on.
# ---------------------------------------------------------------------------
def _text(user, digest):
    return render_to_string(
        "crm/emails/weekly_digest.txt",
        {"user": user, "digest": digest, "site_url": "https://coverage.test"},
    )


@pytest.fixture
def picks_ctx(digest_ctx):
    """The shared fixture plus two scoreable rows at the tiered firm, so the
    New for you section actually renders."""
    user, _ = digest_ctx
    firm = Firm.objects.get(slug="gs")
    for n in (1, 2):
        Opportunity.objects.create(
            firm=firm, url=f"https://x/gs/pick-{n}", title=f"Summer Analyst {n}",
            bucket="internship", status="open", cohort="2028", location="New York",
        )
    digest = assemble_digest(user, today=TODAY)
    assert digest["picks"], "fixture should have produced picks"
    return user, digest


def test_both_halves_print_which_mode_new_for_you_is_in(picks_ctx):
    user, digest = picks_ctx
    line = digest["picks_mode_line"]

    assert line in _render(user, digest)
    assert line in _text(user, digest)


def test_both_halves_print_how_old_every_pick_is(picks_ctx):
    """The rows are what make the sentence above them checkable, so the age
    is on every one of them, in the feed's own wording."""
    user, digest = picks_ctx
    html = _render(user, digest)
    text = _text(user, digest)

    for half in (html, text):
        assert half.count("First seen") == len(digest["picks"])
        assert "First seen today" in half
