"""The bench (Phase 1 bench design, 2026-08-27): parked contacts are not
gone, and a live opening at a chatted/advocate-parked contact's firm may
draw ONE of them back into view per day. See `crm.today._opening_bench`.

`transaction=True`: bench actions go through `crm.services.set_contact_state`,
which opens its own psycopg connection outside Django's test transaction and
therefore cannot see uncommitted rows — same posture as `test_today.py`.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import BenchDismissal, Contact, Touch, UserFirm
from crm.today import BENCH_PLAN_MAX, _cockpit_context, _opening_bench
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="bench@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


def _contact(*, user, **kw):
    return Contact.all_objects.create(user=user, **kw)


def _touch(user, contact, kind, *, days_ago=0, channel="email"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel=channel,
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _firm_with_opening(user, *, name, slug, tier=1, days_out=37):
    """A tiered firm with a CONFIRMED FirmDate inside `OPENING_HORIZON_DAYS`
    (45) but outside `pre_deadline_reping_days` (14 by default) — the same
    fixture shape `test_today.py`'s `_stuck_queue` uses for Katy Chen, so a
    warm parked contact here is a live opening WITHOUT also tripping the
    engine's own branch-3 re-ping (which would make them "busy" and
    correctly excluded from the bench)."""
    firm = Firm.objects.create(name=name, slug=slug)
    UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=days_out),
        confidence=1.0, precision="day",
    )
    return firm


# ---------------------------------------------------------------------------
# Who benches, and who correctly never does.
# ---------------------------------------------------------------------------
def test_katy_chen_chatted_parked_tier1_appears_on_the_bench():
    """The evidence case, reproduced: tier 1, chatted, parked, a role at her
    firm closing inside the horizon. She must appear."""
    user = _user(weekly_touch_goal=14)
    nomura = _firm_with_opening(user, name="Nomura", slug="nomura-bench", days_out=34)
    katy = _contact(
        user=user, name="Katy Chen", firm=nomura, region="us",
        warmth="chatted", thread_state="parked",
    )
    _touch(user, katy, "chat", days_ago=20)

    bench = _cockpit_context(user)["bench"]
    assert [b["contact"]["name"] for b in bench] == ["Katy Chen"]
    assert bench[0]["tier"] == 1


def test_advocate_parked_is_also_bench_eligible():
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Jane Street", slug="js-bench")
    c = _contact(
        user=user, name="Adele Advocate", firm=firm, region="us",
        warmth="advocate", thread_state="parked",
    )
    _touch(user, c, "chat", days_ago=20)

    names = [b["contact"]["name"] for b in _cockpit_context(user)["bench"]]
    assert names == ["Adele Advocate"]


def test_cold_parked_contact_never_benches():
    """Cold-parked stays dark. An inbound reply already un-parks through the
    pipeline ratchet; the bench must never do that job for a cold contact."""
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Citi", slug="citi-bench-cold")
    c = _contact(
        user=user, name="Cold Carl", firm=firm, region="us",
        warmth="cold", thread_state="parked",
    )
    _touch(user, c, "outreach", days_ago=40)

    assert _cockpit_context(user)["bench"] == []


def test_replied_parked_contact_never_benches():
    """Replied-parked already gets the engine's own branch-3 re-ping ahead
    of the parked skip; the bench must not duplicate or compete with that."""
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Barclays", slug="barc-bench-replied")
    c = _contact(
        user=user, name="Replied Rae", firm=firm, region="us",
        warmth="replied", thread_state="parked",
    )
    _touch(user, c, "reply_received", days_ago=20)

    assert _cockpit_context(user)["bench"] == []


def test_active_not_parked_contact_is_not_a_bench_candidate():
    """An ACTIVE chatted contact with an opening is `_opening_keep_warms`'s
    job, not the bench's — the two functions must never double up on the
    same person."""
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="UBS", slug="ubs-bench-active")
    c = _contact(
        user=user, name="Active Amy", firm=firm, region="us",
        warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=20)

    bench_names = [b["contact"]["name"] for b in _cockpit_context(user)["bench"]]
    assert "Active Amy" not in bench_names


def test_no_opening_means_no_bench_card():
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(name="No News Bank", slug="no-news-bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    c = _contact(
        user=user, name="Quiet Chatted", firm=firm, region="us",
        warmth="chatted", thread_state="parked",
    )
    _touch(user, c, "chat", days_ago=20)

    assert _cockpit_context(user)["bench"] == []


def test_bench_never_touches_thread_state_by_itself():
    """No expiry, no timer, nothing un-parks itself — only a tap does.
    Rendering the bench (repeatedly, across renders) must never itself move
    `thread_state` off 'parked'."""
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Wells Fargo", slug="wf-bench-notap")
    c = _contact(
        user=user, name="Static Stacy", firm=firm, region="us",
        warmth="chatted", thread_state="parked",
    )
    _touch(user, c, "chat", days_ago=20)

    for _ in range(3):
        ctx = _cockpit_context(user)
        assert any(b["contact"]["id"] == c.id for b in ctx["bench"])
        c.refresh_from_db()
        assert c.thread_state == "parked"


# ---------------------------------------------------------------------------
# At most one card a day, ranked, not merely the first one found.
# ---------------------------------------------------------------------------
def test_at_most_one_bench_card():
    user = _user(weekly_touch_goal=14)
    firm_a = _firm_with_opening(user, name="Tier One Bank", slug="tier1-bench", tier=1)
    firm_b = _firm_with_opening(user, name="Tier Three Bank", slug="tier3-bench", tier=3)
    a = _contact(user=user, name="Alice Chatted", firm=firm_a, region="us",
                 warmth="chatted", thread_state="parked")
    b = _contact(user=user, name="Bob Chatted", firm=firm_b, region="us",
                 warmth="chatted", thread_state="parked")
    _touch(user, a, "chat", days_ago=20)
    _touch(user, b, "chat", days_ago=20)

    bench = _cockpit_context(user)["bench"]
    assert len(bench) == BENCH_PLAN_MAX == 1
    # Ranked, not arbitrary: the tier-1 firm outscores the tier-3 one.
    assert bench[0]["contact"]["name"] == "Alice Chatted"


# ---------------------------------------------------------------------------
# Leave parked: dismiss for THIS opening only.
# ---------------------------------------------------------------------------
def test_leave_parked_dismisses_only_this_opening(client):
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Nomura", slug="nomura-leave", days_out=34)
    katy = _contact(user=user, name="Katy Chen", firm=firm, region="us",
                     warmth="chatted", thread_state="parked")
    _touch(user, katy, "chat", days_ago=20)
    client.force_login(user)

    assert _cockpit_context(user)["bench"] != []
    resp = client.post(reverse("crm:today_bench_act", args=[katy.id, "leave"]))
    assert resp.status_code == 200

    katy.refresh_from_db()
    assert katy.thread_state == "parked", "Leave parked must never un-park"
    assert BenchDismissal.objects.for_user(user).filter(contact=katy).exists()
    assert _cockpit_context(user)["bench"] == []

    # A FRESH opening at the same firm is a new question, not a repeat of
    # the dismissed one — it must be free to bench her again.
    FirmDate.objects.create(
        firm=firm, event_kind="info_session", region="us",
        date=timezone.localdate() + timedelta(days=10),
        confidence=1.0, precision="day",
    )
    names = [b["contact"]["name"] for b in _cockpit_context(user)["bench"]]
    assert names == ["Katy Chen"]


# ---------------------------------------------------------------------------
# Restore: the only tap that un-parks, and it reads the target state off
# warmth, never a single guess.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("warmth,expected_state", [
    ("chatted", "chat_done"),
    ("advocate", "advocate"),
])
def test_restore_sets_thread_state_from_warmth(client, warmth, expected_state):
    user = _user(weekly_touch_goal=14)
    firm = _firm_with_opening(user, name="Restore Bank", slug=f"restore-{warmth}")
    c = _contact(user=user, name="Restore Rita", firm=firm, region="us",
                 warmth=warmth, thread_state="parked")
    _touch(user, c, "chat", days_ago=20)
    client.force_login(user)

    resp = client.post(reverse("crm:today_bench_act", args=[c.id, "restore"]))
    assert resp.status_code == 200

    c.refresh_from_db()
    assert c.thread_state == expected_state
    assert Touch.all_objects.filter(
        user=user, contact=c, kind="manual_override"
    ).exists()


def test_stale_bench_click_is_a_harmless_noop(client):
    """A contact no longer on the bench (opening gone, already dismissed,
    another tenant's id) simply matches nothing — same posture as
    `today_act`/`today_park_all` on a stale id."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Not On Bench", warmth="cold",
                 thread_state="no_reply")
    client.force_login(user)
    resp = client.post(reverse("crm:today_bench_act", args=[c.id, "restore"]))
    assert resp.status_code == 200
    c.refresh_from_db()
    assert c.thread_state == "no_reply"


def test_today_bench_act_rejects_unknown_verb(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Whoever", warmth="cold", thread_state="no_reply")
    client.force_login(user)
    resp = client.post(reverse("crm:today_bench_act", args=[c.id, "nonsense"]))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tenant isolation.
# ---------------------------------------------------------------------------
def test_bench_is_tenant_scoped():
    a = _user("a@example.com")
    b = _user("b@example.com")
    firm = _firm_with_opening(a, name="Shared-Name Bank", slug="shared-bench-a")
    mine = _contact(user=a, name="Mine", firm=firm, region="us",
                     warmth="chatted", thread_state="parked")
    _touch(a, mine, "chat", days_ago=20)

    theirs_firm = _firm_with_opening(b, name="Shared-Name Bank", slug="shared-bench-b")
    theirs = _contact(user=b, name="Theirs", firm=theirs_firm, region="us",
                       warmth="chatted", thread_state="parked")
    _touch(b, theirs, "chat", days_ago=20)

    names_a = [x["contact"]["name"] for x in _cockpit_context(a)["bench"]]
    names_b = [x["contact"]["name"] for x in _cockpit_context(b)["bench"]]
    assert names_a == ["Mine"]
    assert names_b == ["Theirs"]


def test_bench_dismissal_does_not_leak_across_tenants():
    a = _user("a2@example.com")
    b = _user("b2@example.com")
    BenchDismissal.all_objects.create(
        user=b,
        contact=_contact(user=b, name="Someone Else", warmth="chatted",
                          thread_state="parked"),
        opening_signature="firm_date|2026-09-30|",
    )
    firm = _firm_with_opening(a, name="Isolation Bank", slug="isolation-bench")
    katy = _contact(user=a, name="Katy A", firm=firm, region="us",
                     warmth="chatted", thread_state="parked")
    _touch(a, katy, "chat", days_ago=20)

    names = [x["contact"]["name"] for x in _cockpit_context(a)["bench"]]
    assert names == ["Katy A"]


# ---------------------------------------------------------------------------
# 2026-09-01 audit: the bench's order has to be a TOTAL order.
# ---------------------------------------------------------------------------
def test_the_bench_picks_the_same_person_however_the_rows_arrive():
    """`_score` is a product of three small lookup tables, so ties are the
    normal case — and `BENCH_PLAN_MAX` is 1, so a tie decides WHICH SINGLE
    PERSON the strip shows, not merely how a list is arranged.

    Before the tiebreak, `sort(key=-_score)` fell through `list.sort`'s
    stability to the order `_build_actions`' unordered `Contact` queryset
    happened to return — free to change after any UPDATE. Same defect
    `coverage_domain.cadence.due_actions` was given its C5 contact-id key for
    and `crm.coverage.rank_gaps` its firm_id key for; this is the third
    surface, and the one where it is most visible.

    Three contacts identical on every scoring term (same tier, same warmth,
    same opening kind at their own firm), fed to the ranker in every possible
    order. One answer, every time.
    """
    user = _user("bench-order@example.com", weekly_touch_goal=14)
    people = []
    for i in range(3):
        firm = _firm_with_opening(
            user, name=f"Tied Bank {i}", slug=f"tied-bench-{i}", tier=2, days_out=34
        )
        c = _contact(
            user=user, name=f"Tied Person {i}", firm=firm, region="us",
            warmth="chatted", thread_state="parked",
        )
        _touch(user, c, "chat", days_ago=20)
        people.append(c)

    today = timezone.localdate()
    scores = {
        _opening_bench(user, [c], [], today)[0]["_score"] for c in people
    }
    assert len(scores) == 1, "fixture is not actually tied; the test proves nothing"

    picked = set()
    for order in itertools.permutations(people):
        result = _opening_bench(user, list(order), [], today)
        assert len(result) == BENCH_PLAN_MAX
        picked.add(result[0]["contact"]["name"])
    assert len(picked) == 1, f"the bench showed {picked} for one unchanged dataset"
