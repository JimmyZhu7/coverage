"""Day-one seeds: what Today says when the queue has nothing of its own.

THE MEASURED BUG. After onboarding with zero or one contact, Today said
"You're all caught up." With ONE contact just written to, the cadence
follow-up window puts the next action six business days out, so the page
stayed silent for a week — congratulating a day-old account on an empty queue
it had no way to fill, at exactly the moment a new student most needs a push.
The queue is the product's habit loop, and it went dark on day one.

The rules pinned here:

  1. Seeds appear only when the queue is genuinely silent — no planned lane,
     no chat prep, no debrief, and nothing pacing out behind the cap — AND
     the network is still being built. An empty queue means two opposite
     things: "nothing to do because you have nothing yet" earns seeds,
     "nothing to do because you handled it" earns "You're all caught up."
  2. A working queue never sees one, and never pays a query for one.
  3. Every seed is DERIVED from state on each render and stored nowhere, so
     it disappears the moment its job is done. Nothing to dismiss.
  4. Nothing is offered that this deploy cannot actually do.

THE FIRM SEEDS ARE NOT HERE ANY MORE. "Add N people at X" moved to
`crm.today._coverage_cards` on 2026-08-27, because this lane could never
show it to the account with the most of that work to do: seeds only run
below SEED_NETWORK_FLOOR live contacts, and the founder has 182 of them
over 25 empty target firms. Those tests moved with it — and then were
deleted along with it on 2026-08-31, when the founder saw the lane's actual
one-card render on his own account and judged it useless (see
`crm.today._starter_seeds`'s own module note for the full history). There is
no replacement feature for those tests to assert against; the prompt is
gone. "Pick your target firms" followed the same exit a few hours earlier
that day for an unrelated reason: the getting-started rail checklist
(`crm.today._getting_started_checklist`) now owns that message persistently
instead of only on a silent-queue render, so the two stopped telling the
student the same thing in two registers at once. What is left in this file
is the rest of SETUP — mailbox connected, list imported, track set — which
is the honest remaining scope for a lane headed "Start here".

Its own module rather than an append to `test_today.py`: this is a distinct
feature with a distinct premise, and the file it would otherwise land in is
1,700 lines and actively edited elsewhere.

`transaction=True` matches test_today.py — several helpers here go through
`crm.services`, which opens its own connection outside the test transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, Touch, UserFirm
from crm.today import (
    SEED_MAX, SEED_NETWORK_FLOOR, _cockpit_context, _starter_seeds,
)
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="seed@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", weekly_touch_goal=14, **kw
    )


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _login_and_get(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


def _seed_keys(user) -> list[str]:
    return [s["key"] for s in _cockpit_context(user)["seeds"]]


# ---------------------------------------------------------------------------
# 1. When they appear.
# ---------------------------------------------------------------------------
def test_one_contact_just_touched_is_never_called_caught_up(client):
    """The measured bug, reproduced. Cadence is correctly silent for six
    business days; the PAGE must not read that silence as success."""
    user = _user()
    gs = Firm.objects.create(name="Goldman Sachs", slug="goldman-sachs")
    UserFirm.all_objects.create(user=user, firm=gs, tier=1)
    c = Contact.all_objects.create(user=user, name="Ada Lovelace", firm=gs)
    _touch(user, c, "outreach", days_ago=0)

    ctx = _cockpit_context(user)
    assert not ctx["lanes"], "precondition: the cadence has nothing due yet"

    body = _login_and_get(client, user)
    assert "You're all caught up." not in body
    assert "Nothing due today." in body
    assert "Start here" in body


def test_a_queue_of_nothing_but_parks_still_gets_seeds(client):
    """A "Gone quiet" strip is a list of EXITS, not forward work, so a queue
    holding only parks is silent in every sense that matters to a student."""
    user = _user()
    gs = Firm.objects.create(name="Goldman Sachs", slug="goldman-sachs")
    UserFirm.all_objects.create(user=user, firm=gs, tier=1)
    c = Contact.all_objects.create(user=user, name="Quiet One", firm=gs)
    _touch(user, c, "outreach", days_ago=40)
    _touch(user, c, "follow_up", days_ago=30)

    body = _login_and_get(client, user)
    assert "Gone quiet" in body, "precondition: this contact is park-eligible"
    assert "Start here" in body


# ---------------------------------------------------------------------------
# 2. When they must not.
# ---------------------------------------------------------------------------
def test_a_working_queue_gets_no_seeds_at_all(client):
    """Seeds are what the page says INSTEAD of nothing — never alongside real
    work, which they would only push down the page."""
    user = _user()
    for i in range(6):
        # `school_affiliation`: the queue's relevance gate (crm.relevance) only
        # speaks about people at a tiered firm, people who share the student's
        # school, or people waiting on a reply. This test is about seeds, not
        # the gate, and the school tie is the cheapest way to have a queue.
        c = Contact.all_objects.create(
            user=user, name=f"Due {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    assert ctx["lanes"], "precondition: this queue has planned work in it"
    assert ctx["seeds"] == []
    # The lane's full class attribute, not its heading text or a bare
    # class name: this page inlines its stylesheet, so either of those
    # would match the CSS itself and pass for the wrong reason.
    assert 'class="lane lane-seed"' not in _login_and_get(client, user)


def test_a_handled_queue_keeps_the_line_the_student_earned(client):
    """The same false comfort pointed the other way, and the harder half of
    the rule.

    Thirty contacts with every one snoozed by hand is an empty queue too — but
    it is empty because the student read it and made a decision about every
    row. "You're all caught up" is the true sentence there, and answering it
    with "Add 3 people at Goldman Sachs" would overwrite an earned message
    with a beginner's one. Silence alone is not the trigger; silence over a
    network that isn't built yet is.
    """
    user = _user()
    for i in range(30):
        # See the note on `school_affiliation` above: without it the relevance
        # gate leaves this student no queue at all to pace out.
        c = Contact.all_objects.create(
            user=user, name=f"Cold {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=20)
    Contact.all_objects.filter(user=user).update(
        snoozed_until=timezone.now() + timedelta(days=1)
    )

    ctx = _cockpit_context(user)
    assert not ctx["lanes"] and not ctx["held"], (
        "precondition: this queue is as silent as a day-one account's")
    assert ctx["seeds"] == []

    body = _login_and_get(client, user)
    assert "You're all caught up." in body
    assert "Start here" not in body


def test_the_same_silence_over_an_unbuilt_network_does_get_seeds(client):
    """The other side of the same line, so the rule is a distinction and not
    just a size cutoff nobody can see. Identical silence, identical snoozes —
    only the size of the network differs."""
    user = _user()
    gs = Firm.objects.create(name="Goldman Sachs", slug="goldman-sachs")
    UserFirm.all_objects.create(user=user, firm=gs, tier=1)
    for i in range(SEED_NETWORK_FLOOR - 1):
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)
    Contact.all_objects.filter(user=user).update(
        snoozed_until=timezone.now() + timedelta(days=1)
    )

    ctx = _cockpit_context(user)
    assert not ctx["lanes"] and not ctx["held"]
    assert ctx["seeds"], (
        "four people, none of them at the firm they tiered first, is a start "
        "and not a network to be caught up on"
    )
    body = _login_and_get(client, user)
    assert "You're all caught up." not in body


def test_seeds_stay_silent_while_the_cap_is_still_pacing_work_out():
    """Held is not gone. A page rationing real follow-ups is not a page with
    nothing to say, so it must not start suggesting beginner moves."""
    user = _user()
    for i in range(30):
        # See the note on `school_affiliation` above: without it the relevance
        # gate leaves this student no queue at all to pace out.
        c = Contact.all_objects.create(
            user=user, name=f"Cold {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    planned = [a["contact"]["id"] for lane in ctx["lanes"] for a in lane["items"]]
    # Snooze exactly what made today's plan, leaving the remainder held.
    Contact.all_objects.filter(pk__in=planned).update(
        snoozed_until=timezone.now() + timedelta(days=1)
    )
    after = _cockpit_context(user)
    assert after["held"], "precondition: work is still pacing out behind the cap"
    assert after["seeds"] == []


# ---------------------------------------------------------------------------
# 3. Derived, never stored: each one ends when its job does.
# ---------------------------------------------------------------------------
def test_the_import_seed_goes_away_once_something_was_imported():
    user = _user()
    assert "import" in _seed_keys(user)
    Contact.all_objects.create(user=user, name="From The CSV", source="import")
    assert "import" not in _seed_keys(user)


def test_the_track_seed_goes_away_once_a_track_is_set():
    user = _user()
    assert "track" in _seed_keys(user)
    user.tracks = ["ib"]
    user.save(update_fields=["tracks"])
    assert "track" not in _seed_keys(user)


def test_the_firms_seed_no_longer_exists_the_rail_checklist_owns_it():
    """"Pick your target firms" used to be a `_starter_seeds` entry, gated to
    an account that finished onboarding and still had no firms (the
    unfinished-onboarding banner in week.html already covers the
    not-finished case). Retired 2026-08-31: the getting-started rail
    checklist (`crm.today._getting_started_checklist`) now says this
    persistently, on every render, rather than only when the queue happens
    to be silent — so it must never come back as a seed key, finished
    onboarding or not."""
    user = _user()
    assert "firms" not in _seed_keys(user)

    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    assert "firms" not in _seed_keys(user)


# ---------------------------------------------------------------------------
# 4. Honest reasons, honest offers.
# ---------------------------------------------------------------------------
def test_the_gmail_seed_is_dark_when_gmail_live_is_not_configured(settings):
    """A seed offering a connection this install cannot make is the exact
    class of over-claim the rest of this page exists to avoid."""
    settings.GMAIL_LIVE_CLIENT_ID = ""
    settings.GMAIL_LIVE_CLIENT_SECRET = ""
    settings.GMAIL_LIVE_PUBSUB_TOPIC = ""
    settings.GMAIL_LIVE_TOKEN_KEY = ""
    user = _user(tracks=["ib"])
    assert "gmail" not in _seed_keys(user)


def test_the_gmail_seed_appears_where_gmail_live_is_live(settings):
    settings.GMAIL_LIVE_CLIENT_ID = "id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
    settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
    settings.GMAIL_LIVE_TOKEN_KEY = "key"
    user = _user(tracks=["ib"])
    assert "gmail" in _seed_keys(user)


def test_the_gmail_seed_survives_the_cap_on_a_brand_new_account(settings):
    """THE ORDERING BUG, and why this seed moved above the CSV one.

    A brand-new account offers firms, import and track — three seeds, which
    is exactly `SEED_MAX`. The Gmail seed used to be appended last and was
    therefore truncated away on the one render it exists for: verified on a
    live fresh account (2026-08-27), Today said "Start here 3" and Connect
    Gmail was not among the three.

    It leads now because of what connecting does on an EMPTY account: the
    first-connect pass sweeps six months of the student's own sent mail and
    proposes everyone they wrote to at a directory firm
    (capture/gmail_live.py). "Import your CSV first" was only ever the right
    order while the historical pass searched per-contact and could find
    nobody new.
    """
    settings.GMAIL_LIVE_CLIENT_ID = "id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
    settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
    settings.GMAIL_LIVE_TOKEN_KEY = "key"
    # No tracks, no firms, no contacts — the emptiest possible account, and
    # the one that would fill every slot before Gmail got a look in.
    user = _user()
    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])

    keys = _seed_keys(user)
    assert len(keys) == SEED_MAX
    assert "gmail" in keys
    assert keys.index("gmail") < keys.index("import")


def test_a_connected_mailbox_gives_the_slot_back_to_the_csv_seed(settings):
    """The seed is an offer, not a permanent fixture: once the mailbox is
    connected the slot returns to whatever is still undone."""
    from capture.models import GmailConnection

    settings.GMAIL_LIVE_CLIENT_ID = "id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
    settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
    settings.GMAIL_LIVE_TOKEN_KEY = "key"
    user = _user(tracks=["ib"])
    assert "gmail" in _seed_keys(user)

    GmailConnection.all_objects.create(
        user=user, gmail_address=user.email, refresh_token_encrypted="x",
    )
    keys = _seed_keys(user)
    assert "gmail" not in keys
    assert "import" in keys


def test_seeds_are_capped_so_the_page_stays_a_nudge_not_a_curriculum(settings):
    settings.GMAIL_LIVE_CLIENT_ID = "id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "secret"
    settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
    settings.GMAIL_LIVE_TOKEN_KEY = "key"
    user = _user()
    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    for i in range(9):
        f = Firm.objects.create(name=f"Firm {i}", slug=f"firm-{i}")
        UserFirm.all_objects.create(user=user, firm=f, tier=1)

    seeds = _cockpit_context(user)["seeds"]
    assert len(seeds) == SEED_MAX == 3
    # And never a firm: naming firms is the getting-started checklist's job
    # now, so nine tiered firms cannot crowd the setup steps out of their own
    # lane.
    assert not any(s["key"].startswith("firm-") for s in seeds)


# ---------------------------------------------------------------------------
# 5. Cost. A perf pass fixed the N+1s on this exact page the same day this
# feature was written; it must not put one back.
# ---------------------------------------------------------------------------
def _silent_today_query_count(client, user, n_firms: int) -> int:
    from django.db import connection, reset_queries
    from django.test import override_settings

    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    for i in range(n_firms):
        f = Firm.objects.create(name=f"F{user.pk}-{i}", slug=f"f{user.pk}-{i}")
        UserFirm.all_objects.create(user=user, firm=f, tier=1)
        FirmDate.objects.create(
            firm=f, cycle="sa2028", region="us", event_kind="app_close",
            date=timezone.localdate() + timedelta(days=20), confidence=1.0,
        )
    client.force_login(user)
    client.get(reverse("crm:week"))          # warm
    with override_settings(DEBUG=True):
        reset_queries()
        client.get(reverse("crm:week"))
        return len(connection.queries)


def test_a_silent_today_does_not_grow_its_query_count_with_target_firms(client):
    """Every firm-shaped lookup on a silent Today — the seeds' own, the
    getting-started checklist's, the Deadlines rail's — is a bulk read. A
    count that climbs with the firm list means somebody put a loop back.

    TWENTY AND FORTY, not some small count against a large one: both sizes
    exercise every query on the page (every firm here carries a confirmed
    date for the Deadlines rail to read), so a difference between them can
    only be per-firm. This fixture used to also exercise `_coverage_cards`'
    own ranking query, which read every tiered firm before capping to the
    two-or-one it rendered; that lane is gone (2026-08-31), but the fixture
    is unchanged because the Deadlines rail alone already needs both sizes to
    catch a reintroduced per-firm loop.
    """
    small = _silent_today_query_count(
        client, _user("seed-q-small@example.com"), 20)
    big = _silent_today_query_count(
        client, _user("seed-q-big@example.com"), 40)
    assert big <= small, (
        f"a silent Today ran {small} queries against 20 target firms but "
        f"{big} against 40 — something is looping firms into the database."
    )


def test_a_working_queue_pays_nothing_for_the_seed_feature(client):
    """The silence gate is also the cost control: every query
    `_starter_seeds` runs sits inside it, so the common case is free."""
    import crm.today

    user = _user("seed-q-busy@example.com")
    gs = Firm.objects.create(name="Goldman Sachs", slug="goldman-sachs")
    UserFirm.all_objects.create(user=user, firm=gs, tier=1)
    for i in range(6):
        c = Contact.all_objects.create(user=user, name=f"Due {i:02d}", firm=gs)
        _touch(user, c, "outreach", days_ago=20)

    calls = []
    real = crm.today._starter_seeds
    crm.today._starter_seeds = lambda *a, **kw: calls.append(a) or real(*a, **kw)
    try:
        assert 'class="lane lane-seed"' not in _login_and_get(client, user)
    finally:
        crm.today._starter_seeds = real
    assert calls == [], "the seed builder must not run when the queue has work"
