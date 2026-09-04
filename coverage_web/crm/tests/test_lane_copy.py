"""The copy-names control on a queue lane (2026-09-02, the founder's ask).

A lane is a list of people he is about to write to somewhere else, and a
page that already holds the names should not make anyone retype them.

WHAT IT COPIES was wrong until 2026-09-03: the control read the lane's
rendered cards, so a lane the daily cap had trimmed to "2 of 29 today"
handed over two people. It copies the lane's whole membership now, and most
of what is below pins that.
"""

import re
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.templatetags.textstyle import smart_person_name, smart_title
from crm.models import Contact, Touch, UserFirm
from directory.models import Firm

pytestmark = pytest.mark.django_db


def _queue_user(email="lanecopy@example.com", people=3):
    """A user whose queue actually builds a lane: a targeted firm, and cold
    contacts touched long enough ago to be due a follow-up.

    `people` is the knob the cap tests turn. A day's plan holds at most
    TODAY_PLAN_MAX of them, so nine guarantee a lane that renders fewer
    names than it counts.
    """
    user = get_user_model().objects.create_user(
        email=email, password="pw12345!", weekly_touch_goal=14)
    firm = Firm.objects.create(slug="nomura-copy", name="Nomura")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(people):
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}", firm=firm)
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20))
    return user


def _lane_headings(body: str) -> list[str]:
    return re.findall(r'<h2 class="lane-title">(.*?)</h2>', body, re.S)


class _LaneNames(HTMLParser):
    """The browser's half of the round trip.

    The template writes the separator as `&#10;` and lets Django escape the
    names themselves, so what the handler actually receives is whatever an
    HTML parser hands back from `getAttribute` — not the source text. Reading
    the attribute with a regex and `html.unescape` would be this test
    marking its own homework: it would decode the entities the same way
    whether or not a real parser could. `HTMLParser` unescapes attribute
    values exactly as a browser does, so a name carrying a quote, an
    ampersand or an angle bracket is checked against the decoder that will
    really run.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.payloads: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag != "section" or "lane" not in (a.get("class") or "").split():
            return
        raw = a.get("data-lane-names")
        self.payloads.append(
            [] if raw is None else [n for n in raw.split("\n") if n.strip()]
        )


def _copy_payloads(body: str) -> list[list[str]]:
    """What each lane's Copy names button would put on the clipboard, read
    the way the handler reads it: the lane's own `data-lane-names`, decoded
    by a parser and split on the newline that separates one name from the
    next."""
    parser = _LaneNames()
    parser.feed(body)
    return parser.payloads


def test_a_lane_offers_a_copy_control_that_names_the_lane(client):
    """Presence and identity only. WHICH number the accessible name speaks
    is the next test's job: it used to be the lane's rendered count, and the
    loose digit match here would have gone on passing after that became
    wrong."""
    user = _queue_user()
    client.force_login(user)
    res = client.get("/app/")
    assert res.context["lanes"], "fixture built no lane to copy from"

    headings = _lane_headings(res.content.decode())
    assert headings, "no lane heading rendered"
    assert any("data-lane-copy" in h for h in headings), (
        "a lane heading carries no copy control"
    )
    # The accessible name says which lane and how many people, because
    # "Copy names" alone is identical on every lane of the page.
    labels = [
        m.group(1) for h in headings
        if (m := re.search(r'aria-label="Copy the \d+ names? in ([^"]+)"', h))
    ]
    assert labels and all(lane["label"] in labels for lane in res.context["lanes"])


def test_a_capped_lane_copies_the_names_it_is_holding_back(client):
    """THE BUG (2026-09-03, the founder's own report). A lane headed "3 of 9
    today" renders three cards and pages the other six out under "Up next".
    The button read `.act-name` inside the lane, so it copied the three on
    screen and dropped the six: a third of a list the student was about to
    write to, gone silently, under a control that says "Copy names" beside a
    heading that says nine.
    """
    user = _queue_user("lanecopy_capped@example.com", people=9)
    client.force_login(user)
    res = client.get("/app/")

    lanes = res.context["lanes"]
    assert any(lane["capped"] for lane in lanes), (
        "fixture built no capped lane, so it cannot pin the bug"
    )

    payloads = _copy_payloads(res.content.decode())
    assert len(payloads) == len(lanes), "a lane rendered without a copy payload"

    for lane, names in zip(lanes, payloads):
        assert len(names) == lane["total"], (
            f"{lane['key']}: heading says {lane['total']}, "
            f"copy carries {len(names)}"
        )
        # Every person the lane counts, held ones included, in the lane's own
        # order: what is on screen first, then the remainder as it will come.
        assert names == [
            smart_title(smart_person_name(a["contact"]["name"]))
            for a in lane["all_items"]
        ]
        # The rendered cards are the SHORTER list. If these ever matched on a
        # capped lane, the fixture stopped exercising the cap.
        if lane["capped"]:
            assert len(lane["items"]) < len(names)


def test_the_copy_control_names_the_whole_lane_not_the_rendered_slice(client):
    """The accessible name is the only place the count is spoken, and it has
    to be the count the button actually copies."""
    user = _queue_user("lanecopy_aria@example.com", people=9)
    client.force_login(user)
    res = client.get("/app/")

    totals = sorted(lane["total"] for lane in res.context["lanes"])
    spoken = sorted(
        int(m.group(1))
        for h in _lane_headings(res.content.decode())
        if (m := re.search(r'aria-label="Copy the (\d+) names? in ', h))
    )
    assert spoken == totals


def test_the_copied_names_read_like_names_not_like_stored_rows(client):
    """A contact captured off a Gmail thread is often stored as nothing
    better than the address's local part. The card renders it through
    `smart_person_name`, and the clipboard has to agree with the card: a
    list of "jude.yoon" pasted into an email is not a list of names."""
    user = _queue_user("lanecopy_local@example.com", people=0)
    firm = UserFirm.all_objects.get(user=user).firm
    c = Contact.all_objects.create(user=user, name="jude.yoon", firm=firm)
    Touch.all_objects.create(
        user=user, contact=c, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=20))

    client.force_login(user)
    res = client.get("/app/")
    assert res.context["lanes"], "fixture built no lane"

    copied = [n for names in _copy_payloads(res.content.decode()) for n in names]
    assert "Jude Yoon" in copied
    assert "jude.yoon" not in copied


def test_a_name_carrying_markup_survives_the_attribute_round_trip(client):
    """The separator is written as `&#10;` and the names go through Django's
    attribute escaping, so a name holding a quote or an ampersand is the case
    that decides whether an attribute was the right carrier at all. An
    unescaped quote would close the attribute early and truncate the lane; an
    unescaped ampersand would eat the separator that follows it and weld two
    people into one line.

    Display names really do carry these characters: a nickname in quotes is
    ordinary in a mail header, and capture stores what it observed.
    """
    user = _queue_user("lanecopy_markup@example.com", people=0)
    firm = UserFirm.all_objects.get(user=user).firm
    messy = ['Robert "Bob" O\'Brien', "Baird & Co <Campus>"]
    for name in messy:
        c = Contact.all_objects.create(user=user, name=name, firm=firm)
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20))

    client.force_login(user)
    res = client.get("/app/")
    lanes = res.context["lanes"]
    assert lanes, "fixture built no lane"

    copied = [n for names in _copy_payloads(res.content.decode()) for n in names]
    # Both people, still two lines: the quote did not close the attribute and
    # the ampersand did not swallow the newline after it.
    assert len(copied) == sum(lane["total"] for lane in lanes) == 2
    assert copied == [
        smart_title(smart_person_name(a["contact"]["name"]))
        for lane in lanes
        for a in lane["all_items"]
    ]
    # And the characters came back as themselves, not as entities.
    assert any('"' in n and "'" in n for n in copied)
    assert any("&" in n and "<" in n for n in copied)


def test_the_handler_copies_the_lane_attribute_not_the_visible_cards(client):
    """One source, so the two reads cannot disagree about who is in a lane.
    The attribute is rendered inside the cockpit, which every quick action
    swaps whole, so it is rebuilt with the cards it describes and cannot go
    stale behind a Snooze."""
    user = _queue_user("lanecopy2@example.com")
    client.force_login(user)
    body = client.get("/app/").content.decode()

    assert 'lane.getAttribute("data-lane-names")' in body
    assert 'lane.querySelectorAll(".act-name")' not in body, (
        "the copy still reads the rendered cards"
    )


def test_the_handler_is_delegated_so_it_survives_a_queue_swap(client):
    """Bound handlers die on the first Snooze: the cockpit's innerHTML is
    replaced, taking the button with it. The calendar hit exactly this on
    2026-09-02 with its Today control."""
    user = _queue_user("lanecopy3@example.com")
    client.force_login(user)
    body = client.get("/app/").content.decode()

    assert 'cockpit.addEventListener("click"' in body
    assert 'closest("[data-lane-copy]")' in body


# ---------------------------------------------------------------------------
# ONE lane classifier, shared by the plan and the held remainder
# ---------------------------------------------------------------------------

def test_the_lane_classifier_is_three_way_for_held_rows_too():
    """The plan loop and the held loop must answer the same question the same
    way.

    They used to be two separate ternaries and the held one could only say
    "cold" or "momentum" — correct only because `plan_split` builds `held`
    out of `rest`, which excludes critical rows. That made the held loop
    right by luck rather than by construction. While it carried an integer
    the cost of the luck running out was a heading off by one; now that it
    carries the rows Copy names hands over, the cost is a person appearing in
    another lane's clipboard. So both call `_lane_of`.
    """
    from crm.today import _lane_of

    # `_today_class` reads the row's `action`, so these are real actions
    # rather than a hand-set class: "reping" is CLASS_CRITICAL,
    # "first_outreach" is CLASS_COLD, "thank_you" is CLASS_ENGAGED.
    assert _lane_of({"action": "reping", "priority": 5}) == "critical"
    assert _lane_of({"action": "first_outreach", "priority": 5}) == "cold"
    assert _lane_of({"action": "thank_you", "priority": 5}) == "momentum"

    # priority 0 is the other road into critical, whatever the action says.
    assert _lane_of({"action": "first_outreach", "priority": 0}) == "critical"


def test_both_cockpit_loops_call_the_shared_classifier():
    """A regression guard with teeth only as long as it names the mechanism:
    if either loop goes back to spelling the ternary out inline, the two can
    drift apart again silently."""
    import inspect

    from crm.today import _cockpit_context

    src = inspect.getsource(_cockpit_context)
    assert src.count("_lane_of(a)") == 2, (
        "both planned_lanes and held_by_lane must classify through _lane_of"
    )
    assert 'planned_lanes["critical" if' not in src
