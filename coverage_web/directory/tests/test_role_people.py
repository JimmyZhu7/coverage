""""Your people here" — the firm-page network slice, joined onto a ROLE.

The product's landing headline is "the deadline and the person behind it, one
place", and the two surfaces where a student actually decides — the posting
drawer on the feed, and a tracked role's card on My Applications — knew the
deadline and knew nothing at all about the relationship. The join existed only
on /firms/<slug>/, a page reached by navigating AWAY from the role being
decided about.

What is pinned here:

  * the join itself, on both surfaces, in both states (people / nobody yet);
  * tenancy — another student's contacts at the same firm are never on your
    role, and `Contact.objects` unscoped raises by construction;
  * the empty state's pre-filled add-contact link (`?firm=<slug>`, the same
    parameter the firm page's own button uses, handled by crm.views.contact_new);
  * the QUERY SHAPE. My Applications lists every role a student tracks, so the
    obvious per-card contact read grows with the pipeline. The page does ONE
    grouped read for every firm on it, and `assertNumQueries` here is what
    stops an N+1 creeping back in — a perf pass had just finished killing
    several on Today when this was built.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from analytics.models import UserOpportunity
from coverage_web.tenancy import TenantScopeError
from crm.models import Contact, Touch
from directory.models import Firm, Opportunity
from directory.views import _people_at_firms, _role_people

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures — a student, a firm with an open role, and people at it.
# ---------------------------------------------------------------------------

def _user(email="people@example.com"):
    return User.objects.create_user(email=email, password="x")


def _firm(slug="goldman-sachs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _role(firm, title="Summer Analyst", url="https://x/1"):
    return Opportunity.objects.create(
        firm=firm, url=url, title=title, bucket="internship", status="open",
    )


def _contact(user, firm, name, *, warmth="cold", role="", days_ago=None):
    c = Contact.all_objects.create(
        user=user, firm=firm, name=name, warmth=warmth, role=role,
    )
    if days_ago is not None:
        Touch.all_objects.create(
            user=user, contact=c, kind="chat", source="manual",
            ts=timezone.now() - timedelta(days=days_ago),
        )
    return c


# ---------------------------------------------------------------------------
# The grouped read.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_query_covers_every_firm_asked_about(django_assert_num_queries):
    """The whole point of the helper: N firms, one read. A per-firm loop would
    be six queries here and one per card on a real pipeline.

    TWO, not one, since campaign-hidden contacts came off these rosters: the
    second is `crm.campaigns.excluded_contact_ids`, which is one
    `CampaignContact` read that returns nothing and short-circuits on every
    account that has never classified a bulk send. The invariant this test
    exists for is untouched — neither number grows with the firm count, which
    is what `test_drawer_costs_one_query_for_its_people` pins from the other
    direction."""
    user = _user()
    firms = [_firm(slug=f"f{i}", name=f"Firm {i}") for i in range(6)]
    for f in firms:
        _contact(user, f, f"Person at {f.name}", days_ago=3)

    with django_assert_num_queries(2):
        out = _people_at_firms(
            user, [f.id for f in firms], today=timezone.localdate(), cap=3
        )

    assert set(out) == {f.id for f in firms}
    assert all(len(v["people"]) == 1 for v in out.values())


@pytest.mark.django_db
def test_warmest_first_and_capped_with_the_remainder_counted():
    """Ordering is the firm page's (warmth tier, then name) — whoever leads the
    list is the person to open, which is why no separate "warmest here" callout
    is needed at this size. Everyone past the cap is counted, never dropped
    silently."""
    user = _user()
    firm = _firm()
    _contact(user, firm, "Zoe Cold", warmth="cold")
    _contact(user, firm, "Maya Advocate", warmth="advocate")
    _contact(user, firm, "Alan Chatted", warmth="chatted")
    _contact(user, firm, "Bea Replied", warmth="replied")

    out = _people_at_firms(user, [firm.id], today=timezone.localdate(), cap=2)

    slice_ = out[firm.id]
    assert [p["name"] for p in slice_["people"]] == ["Maya Advocate", "Alan Chatted"]
    assert slice_["total"] == 4
    assert slice_["more"] == 2


@pytest.mark.django_db
def test_days_since_last_touch_and_never():
    user = _user()
    firm = _firm()
    _contact(user, firm, "Maya Advocate", warmth="advocate", days_ago=24)
    _contact(user, firm, "New Person", warmth="cold")

    out = _people_at_firms(user, [firm.id], today=timezone.localdate(), cap=3)
    by_name = {p["name"]: p for p in out[firm.id]["people"]}

    assert by_name["Maya Advocate"]["days_since"] == 24
    assert by_name["New Person"]["days_since"] is None


@pytest.mark.django_db
def test_archived_contacts_are_not_your_people_here():
    user = _user()
    firm = _firm()
    c = _contact(user, firm, "Gone Away", warmth="advocate")
    c.archived = True
    c.save()

    assert _people_at_firms(user, [firm.id], today=timezone.localdate(), cap=3) == {}


@pytest.mark.django_db
def test_another_students_contacts_are_never_on_your_role():
    """Tenancy. The whole slice goes through `.for_user`; the unscoped manager
    raises by construction (coverage_web/tenancy.py), which is what makes the
    leak impossible to write by accident rather than merely absent today."""
    mine, theirs = _user("mine@example.com"), _user("theirs@example.com")
    firm = _firm()
    _contact(theirs, firm, "Their Advocate", warmth="advocate")

    assert _people_at_firms(mine, [firm.id], today=timezone.localdate(), cap=3) == {}

    with pytest.raises(TenantScopeError):
        Contact.objects.filter(firm=firm)


@pytest.mark.django_db
def test_signed_out_reader_gets_nothing():
    from django.contrib.auth.models import AnonymousUser

    firm = _firm()
    assert _people_at_firms(
        AnonymousUser(), [firm.id], today=timezone.localdate(), cap=3
    ) == {}


def test_no_block_at_all_without_a_firm_to_join_on():
    """`None` means draw nothing; an empty `people` list means "this firm, and
    you know nobody here yet", which is a different and renderable answer."""
    assert _role_people(None, None) is None

    firm = Firm(id=7, slug="gs", name="Goldman Sachs")
    empty = _role_people(firm, None)
    assert empty["people"] == [] and empty["firm_slug"] == "gs"


# ---------------------------------------------------------------------------
# The drawer.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_drawer_names_your_people_above_the_postings_own_words(client):
    user = _user()
    firm = _firm()
    opp = _role(firm)
    _contact(user, firm, "Maya Chen", warmth="advocate", role="Analyst", days_ago=24)
    client.force_login(user)

    body = client.get(reverse("role_description", args=[opp.id])).content.decode()

    assert "You know 1 person at Goldman Sachs" in body
    assert "Maya Chen" in body
    assert "In your corner" in body       # warmth as a word, not colour alone
    assert "24d" in body                  # last touch
    assert reverse("crm:contact_detail", args=[_only_contact(user).id]) in body
    # Placement: above everything the posting itself says, not under 3,800
    # characters of description. See the drawer template's own note.
    assert body.index("You know 1 person") < body.index("drawer-apply")


@pytest.mark.django_db
def test_drawer_empty_state_names_the_firm_and_prefills_the_add_form(client):
    user = _user()
    opp = _role(_firm())
    client.force_login(user)

    body = client.get(reverse("role_description", args=[opp.id])).content.decode()

    assert "Nobody at Goldman Sachs yet" in body
    assert f"{reverse('crm:contact_new')}?firm=goldman-sachs" in body


@pytest.mark.django_db
def test_drawer_shows_a_signed_out_reader_no_network_block(client):
    """`role_description` is deliberately not login_required — the posting text
    is public. A student's contacts are not."""
    opp = _role(_firm())

    body = client.get(reverse("role_description", args=[opp.id])).content.decode()

    assert "You know" not in body
    assert "Nobody at" not in body


@pytest.mark.django_db
def test_drawer_costs_one_query_for_its_people(django_assert_num_queries):
    from django.test import Client

    user = _user()
    firm = _firm()
    opp = _role(firm)
    for i in range(4):
        _contact(user, firm, f"Person {i}", warmth="replied", days_ago=i + 1)
    c = Client()
    c.force_login(user)
    url = reverse("role_description", args=[opp.id])
    c.get(url)  # warm any per-process caches so the count is the steady state

    before = _query_count(c, url)
    Contact.all_objects.filter(user=user).delete()
    for i in range(12):
        _contact(user, firm, f"Extra {i}", warmth="replied", days_ago=i + 1)
    after = _query_count(c, url)

    assert before == after, "people cost must not grow with the contact count"


# ---------------------------------------------------------------------------
# My Applications.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_saved_role_card_names_your_people_at_that_firm(client):
    """The compact form says "here", not the firm's name.

    REWRITTEN 2026-09-02, and the premise it retires is that this surface
    should read exactly like the drawer. It cannot: on a My Applications card
    the firm is already the card's own first line, in caps, so the full
    sentence printed "Goldman Sachs" a second time — and the overflow link
    below it a third, on one 300px cell. The founder read the card and named
    both repeats. What is pinned instead is that the join, the count and the
    person are all still said, and that the firm name is on the hover rather
    than deleted. The drawer's own wording is asserted unchanged above.
    """
    user = _user()
    firm = _firm()
    opp = _role(firm)
    UserOpportunity.all_objects.create(user=user, opportunity=opp)
    _contact(user, firm, "Maya Chen", warmth="advocate", role="Analyst", days_ago=24)
    client.force_login(user)

    body = client.get(reverse("my_applications")).content.decode()

    assert "You know 1 person here" in body
    assert "You know 1 person at Goldman Sachs" not in body, (
        "the card already names the firm on its own first line")
    assert 'title="At Goldman Sachs"' in body, "the name moved to the hover, not away"
    assert "Maya Chen" in body
    assert "24d" in body


@pytest.mark.django_db
def test_saved_role_card_empty_state_prefills_the_firm(client):
    """Same rewrite as the test above, on the other state. The prompt still
    names a next action and still pre-fills the firm on the add form; it just
    points at the firm with "here" instead of spelling it out under a heading
    that already has."""
    user = _user()
    opp = _role(_firm())
    UserOpportunity.all_objects.create(user=user, opportunity=opp)
    client.force_login(user)

    body = client.get(reverse("my_applications")).content.decode()

    assert "Nobody here yet" in body
    assert "Nobody at Goldman Sachs yet" not in body
    assert 'title="At Goldman Sachs"' in body
    assert f"{reverse('crm:contact_new')}?firm=goldman-sachs" in body


@pytest.mark.django_db
def test_a_done_row_gets_no_networking_prompt_but_keeps_real_names(client):
    """A finished application is not a networking opportunity, and a grid of
    terminal cards each nagging for a contact is the noise this block exists
    not to be. Real names stay: a role you finished at a firm where you know
    someone is still a relationship."""
    user = _user()
    bare, known = _firm("nomura", "Nomura"), _firm()
    done_bare = _role(bare, title="Closed Thing", url="https://x/2")
    done_known = _role(known, title="Other Thing", url="https://x/3")
    UserOpportunity.all_objects.create(
        user=user, opportunity=done_bare, applied_status="closed")
    UserOpportunity.all_objects.create(
        user=user, opportunity=done_known, applied_status="closed")
    _contact(user, known, "Maya Chen", warmth="advocate", days_ago=5)
    client.force_login(user)

    body = client.get(reverse("my_applications")).content.decode()

    # The card's prompt says "here" now (see the two tests above), so the
    # absence has to be asserted against the string the card would actually
    # print — "Nobody at Nomura yet" is a sentence this surface can no longer
    # produce, and a test asserting its absence would pass on a page that had
    # gone right back to nagging every finished row.
    assert "Nobody here yet" not in body
    assert "Nobody at Nomura yet" not in body
    assert "Maya Chen" in body


@pytest.mark.django_db
def test_my_applications_people_cost_does_not_grow_with_the_pipeline():
    """The N+1 guard. Twelve roles across twelve firms must cost the same
    number of queries as one role at one firm — one grouped read, not one per
    card."""
    from django.test import Client

    user = _user()
    c = Client()
    c.force_login(user)
    url = reverse("my_applications")

    firm = _firm()
    UserOpportunity.all_objects.create(user=user, opportunity=_role(firm))
    _contact(user, firm, "Maya Chen", warmth="advocate", days_ago=5)
    c.get(url)  # warm
    one = _query_count(c, url)

    for i in range(11):
        f = _firm(slug=f"firm-{i}", name=f"Firm {i}")
        UserOpportunity.all_objects.create(
            user=user, opportunity=_role(f, title=f"Role {i}", url=f"https://x/r{i}"))
        _contact(user, f, f"Person {i}", warmth="replied", days_ago=i + 1)
    many = _query_count(c, url)

    assert many == one, (
        f"one role cost {one} queries, twelve cost {many} — the people read "
        f"is growing per card"
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _only_contact(user):
    return Contact.objects.for_user(user).first()


def _query_count(client, url) -> int:
    """Queries one GET costs. `CaptureQueriesContext` rather than
    `assertNumQueries` because these tests compare two counts against each
    other rather than against a literal — the absolute number moves whenever
    an unrelated view helper changes, and pinning it would make this an
    unrelated-change tripwire instead of an N+1 guard."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        client.get(url)
    return len(ctx)
