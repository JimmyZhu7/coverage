"""Coverage gaps: tiered firms the student knows nobody at.

Until 2026-08-31 this module tested a "Your board" lane with two kinds of
card — a confirmed dated firm event (`_plays`) and this coverage-gap half
(`_coverage_cards`) — in one lane, ranked by a mixed-section tie-break. The
founder judged the lane not practically useful ("just take away the board i
dont think its practically useful") and it was removed whole, heading and
markup and dismiss buttons and all.

The dated half needed no replacement: the rail's Deadlines card
(`_next_deadlines`, exercised in `test_today.py`) already named the identical
facts on its own, and the tests that pinned `_plays` — the 3-card cap, the
fact-keyed dismissal, the sourcing case, the confirmed-only bar, the
`live_total`/parked-contact distinction — went with it, since none of that
machinery exists any more.

`_coverage_cards` survived, because it is the only reachable version of "add
someone at an empty tiered firm" for an account past `SEED_NETWORK_FLOOR`
live contacts (see `crm.today._coverage_cards`'s and `_starter_seeds`' own
notes) — deleting it with the rest of the board would have been a real
regression dressed as cleanup. What remains here is its own coverage: no new
judgment (it is `crm.coverage.rank_gaps`, the Network board's own ranking),
NO_CONTACTS only, tiered firms only, the anti-nag dismissal, and honest
counts.

`transaction=True`, matching `test_today.py` and `test_relevance.py`: some
paths this module exercises indirectly (`_cockpit_context`) go through
`crm.services`, which opens its own connection outside Django's test
transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, PlayDismissal, Touch, UserFirm
from crm.today import (
    COVERAGE_CARD_MAX, COVERAGE_CARD_MAX_BUSY, COVERAGE_DISMISSAL_DATE,
    COVERAGE_EVENT_KIND, _cockpit_context, _coverage_cards,
)
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="plays@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _firm(slug, name=None):
    return Firm.objects.create(slug=slug, name=name or slug.title())


def _confirmed(firm, *, today, days, event_kind="app_close", cycle="sa2028"):
    return FirmDate.objects.create(
        firm=firm, cycle=cycle, region="us", event_kind=event_kind,
        date=today + timedelta(days=days), confidence=1.0,
    )


def _contact(user, firm, *, name, warmth="cold", archived=False):
    return Contact.all_objects.create(
        user=user, firm=firm, name=name, warmth=warmth, archived=archived,
        school_affiliation=True,
    )


def _target(user, firm, *, tier):
    return UserFirm.all_objects.create(user=user, firm=firm, tier=tier)


def _login_and_get(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


# ---------------------------------------------------------------------------
# Rule 1: no new judgment — `rank_gaps` decides, this module just renders it.
# ---------------------------------------------------------------------------
def test_a_tier_1_firm_with_nobody_at_it_is_todays_work():
    user = _user()
    today = timezone.localdate()
    _target(user, _firm("centerview", "Centerview"), tier=1)

    cards = _cockpit_context(user)["coverage_gaps"]
    assert [(c["firm"].name, c["kind"]) for c in cards] == [
        ("Centerview", "coverage")]
    card = cards[0]
    assert card["label"] == "Tier 1 target"
    assert card["cta_label"] == "Add someone"
    assert f"firm={card['firm'].slug}" in card["cta_href"]
    # No clock it did not earn.
    assert card["date"] is None
    assert card["when"] == ""
    assert card["urgent"] is False
    # The unsourced claim retired 2026-08-31 ("Three is where a firm starts
    # to know you") does not come back in another form.
    assert card["people_line"] == "Nobody there yet."


def test_the_tier_the_student_set_orders_the_cards():
    """The user's own tiering is the only statement of priority the product
    has. `rank_gaps` multiplies by it, so tier 1 leads tier 2 leads tier 3
    with no tie-break of this module's own invention."""
    user = _user()
    _target(user, _firm("t3", "Cee Firm"), tier=3)
    _target(user, _firm("t1", "Aay Firm"), tier=1)
    _target(user, _firm("t2", "Bee Firm"), tier=2)

    cards = _coverage_cards(user, timezone.localdate(), limit=9)
    assert [c["firm"].name for c in cards] == [
        "Aay Firm", "Bee Firm", "Cee Firm"]


def test_an_untiered_firm_is_never_carded():
    """No tier is no claim to care. `rank_gaps` skips these outright and this
    lane is not allowed to invent a priority the student never stated."""
    user = _user()
    _target(user, _firm("unranked", "Unranked Bank"), tier=None)
    assert _coverage_cards(user, timezone.localdate(), limit=9) == []


def test_a_firm_with_contacts_is_the_queues_problem_not_the_boards():
    """RULE 2, and the one that keeps this from becoming the cold-contact
    flood. `rank_gaps` also ranks all_cold / no_advocate firms — 22 of the
    founder's 40 — and every one of those is a firm where somebody already
    exists for the cadence engine to schedule. Carding them here as well
    would be the page asking twice about the same person."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("has-people", "Has People")
    _target(user, firm, tier=1)
    _contact(user, firm, name="Only Cold One", warmth="cold")

    assert _coverage_cards(user, today, limit=9) == []


def test_a_confirmed_date_no_longer_blocks_the_coverage_card():
    """Until 2026-08-31 a firm with BOTH a confirmed date and nobody there
    got the dated card only — the coverage half skipped it via
    `skip_firm_ids`, because the two kinds shared one lane and one slot per
    firm. With the dated half (and the lane) gone, a firm's coverage gap is
    just as real whether or not it also has a date on the rail — there is no
    other card competing for the slot any more, and `_coverage_cards` no
    longer takes a `skip_firm_ids` argument at all."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("blackrock", "BlackRock")
    _target(user, firm, tier=2)
    _confirmed(firm, today=today, days=3)

    cards = _cockpit_context(user)["coverage_gaps"]
    assert [(c["firm"].name, c["kind"]) for c in cards] == [
        ("BlackRock", "coverage")]


def test_a_loud_page_gets_one_coverage_card_and_a_quiet_one_gets_two():
    """The standing backlog is equally true either way; what changes is how
    much of it belongs in front of somebody who already has a morning's work
    queued. Nothing here decides whether a firm is empty."""
    user = _user()
    for i, name in enumerate(["Aay", "Bee", "Cee", "Dee"]):
        _target(user, _firm(f"gap{i}", name), tier=1)

    quiet = _cockpit_context(user)
    assert not quiet["lanes"], "precondition: no cadence work"
    assert len(quiet["coverage_gaps"]) == COVERAGE_CARD_MAX == 2

    # Now give the cadence engine something to plan.
    other = _firm("otherfirm", "Other Firm")
    for i in range(6):
        c = _contact(user, other, name=f"Due {i:02d}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20),
        )
    busy = _cockpit_context(user)
    assert busy["lanes"], "precondition: this queue has planned work"
    assert len(busy["coverage_gaps"]) == COVERAGE_CARD_MAX_BUSY == 1


def test_dismissing_a_coverage_card_is_permanent_for_that_firm(client):
    """The anti-nag rule. A coverage hole has no date, so one Dismiss means
    "stop asking about this firm" and the escape hatch is putting somebody
    there."""
    user = _user()
    keep = _firm("keepme", "Keep Me")
    drop = _firm("dropme", "Drop Me")
    _target(user, drop, tier=1)
    _target(user, keep, tier=1)

    assert len(_cockpit_context(user)["coverage_gaps"]) == 2

    client.force_login(user)
    resp = client.post(reverse("crm:play_dismiss"), {
        "firm": drop.id,
        "event_kind": COVERAGE_EVENT_KIND,
        "date": COVERAGE_DISMISSAL_DATE.isoformat(),
    })
    assert resp.status_code == 200
    assert PlayDismissal.all_objects.filter(
        user=user, firm=drop, event_kind=COVERAGE_EVENT_KIND).count() == 1

    assert [c["firm"].name for c in _cockpit_context(user)["coverage_gaps"]] == [
        "Keep Me"]


def test_the_dismiss_endpoint_rejects_a_malformed_fact(client):
    user = _user()
    client.force_login(user)
    resp = client.post(reverse("crm:play_dismiss"), {"firm": "not-a-number"})
    assert resp.status_code == 400
    assert PlayDismissal.objects.for_user(user).count() == 0


def test_a_dismissed_coverage_card_does_not_eat_a_slot():
    """Filtered BEFORE the cap. Dismissing the worst gap must promote the
    next one, not leave a hole where it was."""
    user = _user()
    firms = [_firm(f"cap{i}", f"Firm {i}") for i in range(4)]
    for f in firms:
        _target(user, f, tier=1)
    assert len(_cockpit_context(user)["coverage_gaps"]) == 2

    PlayDismissal.all_objects.create(
        user=user, firm=firms[0], event_kind=COVERAGE_EVENT_KIND,
        date=COVERAGE_DISMISSAL_DATE,
    )
    after = _cockpit_context(user)
    assert len(after["coverage_gaps"]) == 2, (
        "a dismissed gap must not occupy one of the two slots")
    assert firms[0].name not in [c["firm"].name for c in after["coverage_gaps"]]


def test_a_covered_board_gets_no_coverage_cards_and_says_so():
    """THE WHOLE TEST OF THIS WORK. A genuinely empty day is still allowed to
    be empty: with somebody at every tiered firm the lane renders nothing and
    the quiet header comes back. The page never manufactures a card."""
    user = _user()
    firm = _firm("covered", "Covered Bank")
    _target(user, firm, tier=1)
    # Five, so the setup seeds are gated off too (SEED_NETWORK_FLOOR): this
    # student is running an account, not still building one.
    for i in range(5):
        c = _contact(user, firm, name=f"Somebody {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=1),
        )

    ctx = _cockpit_context(user)
    assert ctx["seeds"] == []
    assert ctx["coverage_gaps"] == []
    assert not ctx["lanes"]
    assert ctx["quiet"] is True
    assert ctx["quiet_line"].startswith("Quiet on the cadence.")


def test_a_page_with_coverage_cards_never_also_says_youre_all_caught_up(client):
    """A `would_be_quiet` regression the founder's own page once hit: three
    board cards rendered above a full-width "You're all caught up" panel,
    because the quiet header's copy of the rule knew about the board and the
    empty states below it did not. Pinned here against the plain heading
    that replaced "Your board"."""
    user = _user()
    _target(user, _firm("contradiction", "Contradiction Bank"), tier=1)

    body = _login_and_get(client, user)
    assert "Firms with no contacts yet" in body
    assert "You're all caught up." not in body
    assert "Done for today." not in body


def test_coverage_cards_are_scoped_to_their_tenant():
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    _target(theirs, _firm("theirbank", "Their Bank"), tier=1)
    assert _cockpit_context(mine)["coverage_gaps"] == []


# ---------------------------------------------------------------------------
# The confirmed-date bar the Deadlines rail card holds (crm.today._next_
# deadlines), pinned here rather than moved to test_today.py because these
# fixtures were already built for it and the rail is this module's only
# remaining connection to dated facts.
# ---------------------------------------------------------------------------
def test_an_estimated_date_never_reaches_the_rail_however_confident():
    """`_next_deadlines` spelled its bar as `confidence=1.0` alone, while
    `directory.views._firm_date_row` — the page that renders these same rows
    with their provenance attached — has always required BOTH halves:
    `confidence >= 0.8 AND precision in ("day", "month", "")`.

    The two halves say different things. `confidence` is how sure we are the
    firm holds this date; `precision` is how exactly the stored day locates
    it. `precision="estimated"` means a month-level guess, printed on the firm
    timeline as "~ Nov 2026" — and printed by the rail as a hard "5d"
    countdown. `import_firm_dates` reads the two from independent keys of one
    YAML entry, so a single seed line saying `confidence: confirmed_official`
    / `precision: estimated` produces exactly this row.
    """
    from crm.today import _next_deadlines

    user = _user()
    firm = _firm("gs", "Goldman Sachs")
    _target(user, firm, tier=1)
    _contact(user, firm, name="Ada", warmth="replied")
    today = timezone.localdate()
    guess = _confirmed(firm, today=today, days=5)
    FirmDate.objects.filter(pk=guess.pk).update(precision="estimated")

    assert _next_deadlines(user, today) == []


def test_a_month_precision_date_still_reaches_the_rail():
    """The over-reach guard. "month" is confirmed — the firm timeline calls it
    confirmed too — and dropping it would silently delete a real date from
    the rail to fix a different one."""
    from crm.today import _next_deadlines

    user = _user()
    firm = _firm("ms", "Morgan Stanley")
    _target(user, firm, tier=1)
    _contact(user, firm, name="Grace", warmth="replied")
    today = timezone.localdate()
    real = _confirmed(firm, today=today, days=5, event_kind="insight_deadline")
    FirmDate.objects.filter(pk=real.pk).update(precision="month")

    assert len(_next_deadlines(user, today)) == 1
