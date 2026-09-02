"""A plan column may only list what the plan does. Everything else is a plan.

The 2026-09-01 billing audit read every branch on `user.plan` in the repo.
Three things are gated, and they are all the Pro card is now allowed to sell:

  real-time Gmail   capture/gmail_live.py registers the watch only for Pro,
                    and capture/management/commands/gmail_poll.py filters on
                    `user__plan="pro"`.
  scan cadence      capture/gmail_live.py::free_rescan_unlocks_at returns
                    None for Pro and `last_scan + GMAIL_FREE_RESCAN_INTERVAL_DAYS`
                    for Free; capture/views.py::gmail_rescan refuses in between.
  the advisor       assistant/plans.py picks the model per plan and
                    billing/credits.py grants 60 vs 180 credits a month.

Four other bullets had no implementation at all: hourly Tier 1 refresh,
LinkedIn contact import, the multi-cycle season archive, and calendar sync.
They now sit in a separately headed "Planned, not yet built" list on the same
card, which is the honest place for a roadmap. These tests pin the split, so
a future edit cannot quietly promote a plan back into the feature list.

Two things the page was silent about are also pinned here, both of them
DECISIONS THAT SHIPPED and were never stated where a student looks: the
14-day Pro trial that starts when you connect Gmail (accounts/trials.py,
called from the connect path), and the Free scan interval, which is the only
Free ceiling the product actually enforces and which the table used to draw
as a match between the two plans.
"""

from __future__ import annotations

import re

import pytest
from django.test import override_settings

# The four claims with no code behind them, in the exact words the page uses.
UNBUILT = (
    "Calendar sync",
    "Hourly refresh on your Tier 1 firms",
    "LinkedIn contact import",
    "Multi-cycle archive of past seasons",
)


def _pro_card(body: str) -> str:
    return re.search(r'<article class="price-card preview.*?</article>', body, re.S).group(0)


def _features(card: str) -> str:
    return re.search(r'<ul class="plan-features">(.*?)</ul>', card, re.S).group(1)


def _planned(card: str) -> str:
    return re.search(r'<ul class="plan-planned">(.*?)</ul>', card, re.S).group(1)


@pytest.mark.django_db
def test_pro_feature_list_holds_nothing_unbuilt(client):
    card = _pro_card(client.get("/pricing/").content.decode())
    features = _features(card)

    for claim in UNBUILT:
        assert claim not in features, (
            f"{claim!r} has no implementation in the repo and must not sit in "
            "the Pro plan's feature list. It belongs under 'Planned, not yet built'."
        )


@pytest.mark.django_db
def test_the_unbuilt_four_are_still_stated_under_a_planned_heading(client):
    """Moved, not deleted. Dropping them would hide the roadmap; leaving them
    in the feature list sold them as shipped. A separate, muted, dash-marked
    list under its own heading says both true things at once."""
    body = client.get("/pricing/").content.decode()
    card = _pro_card(body)

    assert "Planned, not yet built" in card
    planned = _planned(card)
    for claim in UNBUILT:
        assert claim in planned, f"{claim!r} should still be visible, as a plan"


@pytest.mark.django_db
def test_pro_feature_list_sells_the_three_gates_that_exist(client):
    features = _features(_pro_card(client.get("/pricing/").content.decode()))

    assert "Gmail Live: real-time sync that logs itself." in features
    assert "Scan your inbox any time" in features
    assert "Talk to Coverage on a stronger model" in features


@pytest.mark.django_db
@override_settings(GMAIL_FREE_RESCAN_INTERVAL_DAYS=3)
def test_the_scan_row_reads_the_real_cadence_from_the_setting(client):
    """Two ticks side by side used to say Free and Pro scan alike. They do
    not, and this is the one Free limit the product enforces.

    The interval is overridden to a value that appears nowhere in the
    template, so passing proves the page READS the setting rather than that
    a literal 7 happens to agree with it.
    """
    body = client.get("/pricing/").content.decode()
    table = re.search(r'<table class="cmp">.*?</table>', body, re.S).group(0)

    row = re.search(r"<tr>\s*<td>Gmail scan on demand</td>(.*?)</tr>", table, re.S)
    assert row, "the on-demand scan row should exist"
    assert '<span class="cmp-val">Every 3 days</span>' in row.group(1)
    assert '<span class="cmp-val">Any time</span>' in row.group(1)
    assert "cmp-yes" not in row.group(1), (
        "a tick in both columns says the plans are the same here; they are not"
    )
    assert "once every 3 days" in _features(_pro_card(body)), (
        "the Pro bullet quotes the same interval as the table"
    )


@pytest.mark.django_db
@override_settings(PRO_TRIAL_DAYS=21)
def test_the_pro_trial_is_stated_on_the_pro_card(client):
    """PRO_TRIAL_DAYS / PRO_TRIAL_TRIGGER were decided and accounts/trials.py
    implements them off the Gmail connect path, and until now the trial
    appeared on no page a prospective student ever sees.

    Overridden to 21 for the same reason as the scan interval: a typed 14
    would pass a test written against the default and go stale silently.
    """
    body = client.get("/pricing/").content.decode()
    card = _pro_card(body)

    assert "Connect Gmail and Pro is free for 21 days." in card
    assert "One trial per account." in card
