"""The "Found in your inbox" card, rebuilt 2026-09-02.

The founder screenshotted one proposal (Kbiji at Centerview) and said:
"refine this widget, taking up too much space, reorganize within, ensure
visual harmoniousness". Measured with headless Playwright at 1280x800 and
375x812, demo account, six proposals stacked, light and dark:

    1280  188.8px a card  ->  128.8px   (-32%; lane 1210 -> 850)
     375  348.5-383.3px   ->  253.8-293.9px   (-24%; lane 2293 -> 1770)

A Django test client has no layout engine, so what is pinned here is the
markup and the CSS text that produce those numbers; the pixels are quoted
rather than re-run. Every assertion below names the defect it protects.

The facts and controls are unchanged and that is the point: firm, name,
email, the not-in-network verdict, the thread evidence, the role input, the
region select, Add, Dismiss. Nothing entered the CRM before Add and nothing
does now (capture/discovery.py); Dismiss is still remembered forever.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from capture.models import ContactProposal
from crm.models import Firm

pytestmark = pytest.mark.django_db

TODAY = "/app/"


def _user(email):
    return get_user_model().objects.create_user(email=email, password="x" * 14)


def _proposal(user, **kw):
    fields = dict(
        name="Kbiji Sanghvi",
        email="kbiji.sanghvi@centerview.com",
        role_hint="",
        recruiting_hint=False,
        evidence="Seen in a thread you sent.",
        thread_subject="Coffee chat about your summer analyst experience",
        threaded_reply=False,
        evidence_kind="outreach",
        occurred_at=timezone.now(),
        status="pending",
    )
    fields.update(kw)
    fields.setdefault("thread_id", f"t-{fields['email']}")
    return ContactProposal.all_objects.create(user=user, **fields)


def _page(user):
    client = Client()
    client.force_login(user)
    return client.get(TODAY).content.decode()


def _card(html: str) -> str:
    """The first proposal card's markup, from its <article> to the next one."""
    lane = html.split('class="lane lane-proposals"', 1)
    assert len(lane) == 2, "the proposals lane did not render"
    body = lane[1].split("</section>", 1)[0]
    parts = body.split('class="act-card', 2)
    assert len(parts) >= 2, "the proposals lane rendered no card"
    return parts[1]


def _styles(html: str) -> str:
    """EVERY <style> block, not the first. Pages here carry more than one and
    a helper that reads `[0]` has already misdirected two guards."""
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "the page no longer renders a <style> block"
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_the_proposal_card_is_two_halves_who_and_ask():
    """The card used to hand FOUR children to `.act-card`'s three-column
    ledger grid, so the fourth wrapped: identity, evidence and the two blanks
    took row one and the buttons fell to row two under the NAME. Four blocks,
    no shared line, 188.8px a card.

    Two wrappers now: `.act-who` is the person and the reason we are asking,
    `.act-ask` is the two blanks plus the tap that sends them. Substring
    checks on the class NAME, never on a whole class attribute — an exact
    match breaks the day anything is added to the element.
    """
    user = _user("prop-shape@example.com")
    _proposal(user)

    card = _card(_page(user))
    for zone in ("act-who", "act-ident", "act-context", "act-ask", "act-fill", "act-quick"):
        assert zone in card, zone

    # Reading order is markup order: who before ask, and inside each half the
    # identity before its evidence, the blanks before the button that sends them.
    assert card.index("act-who") < card.index("act-ask")
    assert card.index("act-ident") < card.index("act-context") < card.index("act-ask")
    assert card.index("act-fill") < card.index("act-quick")


def test_the_lane_lays_its_card_out_in_two_columns_that_collapse_on_a_phone():
    """The two halves sit side by side down to 640px of QUEUE (the container,
    not the window: at 1024 the rail is still 320px wide and a viewport rule
    reads the wrong number).

    The phone rule has to restate the single column rather than lean on
    `.act-card`'s own collapse: `.lane-proposals .act-card` is 0,2,0 and
    outranks it at every width. Without the restatement the 375px card kept
    the two-column form and painted the Role label over the contact's name,
    which is exactly what the first build of this pass did.
    """
    user = _user("prop-grid@example.com")
    _proposal(user)
    css = _styles(_page(user))

    assert ".lane-proposals .act-card" in css
    two_col = re.search(
        r"\.lane-proposals \.act-card \{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)"
        r"\s+minmax\([^)]*\)",
        css,
    )
    assert two_col, "the proposal card no longer declares its own two columns"

    collapse = re.findall(
        r"\.lane-proposals \.act-card \{ grid-template-columns: minmax\(0, 1fr\); \}", css
    )
    assert len(collapse) == 2, (
        "the single-column collapse needs BOTH the container query and the "
        "no-container-queries media fallback, same as the ledger rule"
    )
    assert "@container queue (max-width: 640px)" in css


def test_add_and_dismiss_share_one_row_under_the_blanks_they_send():
    """Add used to have a row of its own with Dismiss stacked below it, both
    in the card's far LEFT column while the inputs they commit sat in the far
    right one. Two rows of card height for one decision, and Add read as
    belonging to the name above it rather than to the form.

    They are one row now, inside `.act-ask`, directly under the two blanks
    `hx-include` sends. `.act-quick` stays a column everywhere else, so the
    override is scoped to the lane.
    """
    user = _user("prop-row@example.com")
    _proposal(user)
    html = _page(user)
    css = _styles(html)

    assert re.search(
        r"\.lane-proposals \.act-quick \{[^}]*flex-direction: row", css
    ), "the proposal's two buttons are stacked again"
    assert ".act-quick { display: flex; flex-direction: column;" in css, (
        "the ledger's own act-quick must stay a column; only the lane overrides it"
    )

    card = _card(html)
    assert card.index("act-fill") < card.index("Add to network") < card.index("Dismiss")
    # The tap still carries the blanks. This is the contract Add-all skips.
    assert 'hx-include="#prop-fill-' in card


# ---------------------------------------------------------------------------
# The two pills
# ---------------------------------------------------------------------------
def test_the_card_states_the_verdict_once_and_drops_the_warmth_chip():
    """Two pills said adjacent things: a "new" warmth chip beside the name and
    "Not in your network" opening a separate zone below it. They are the same
    fact — a proposal is by definition somebody the CRM has never held, and
    nothing in this lane can be a proposal and not be new.

    The verdict is the one that survived, because it is the one the student
    acts on: it is what Add changes, and it is what the scan actually
    computed. The warmth chip went, on the stylesheet's own rule about
    `.act-draft` — a badge that renders on every card in the lane, every time,
    distinguishes nothing.
    """
    user = _user("prop-pill@example.com")
    _proposal(user)
    card = _card(_page(user))

    assert card.count("Not in your network") == 1
    assert "warmth-cold" not in card, "the always-true warmth chip is back on the card"
    # And it rides the firm line it is about rather than opening a third zone.
    assert card.index("act-eyebrow") < card.index("Not in your network") < card.index("act-name")


def test_a_referral_still_keeps_its_own_chip_beside_the_verdict():
    """Dropping the warmth chip is not a licence to drop the chips that DO
    vary. Referral and Recruiting contact distinguish one proposal from
    another and they moved up to the eyebrow with the verdict, not away.
    """
    user = _user("prop-chips@example.com")
    _proposal(user, evidence_kind="referral", recruiting_hint=True,
              email="yuki.tanaka@nomura.com", name="Yuki Tanaka")
    card = _card(_page(user))

    assert "Referral" in card and "Recruiting contact" in card
    eyebrow = card.split('class="act-eyebrow"', 1)[1].split("</div>", 1)[0]
    assert "Not in your network" in eyebrow
    assert "Referral" in eyebrow and "Recruiting contact" in eyebrow


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def test_the_address_survives_a_known_role_instead_of_losing_a_coin_toss():
    """`{% if role_hint %}…{% elif firm %}{{ email }}{% endif %}` meant the one
    fact that tells two people with the same name apart DISAPPEARED on exactly
    the cards that knew the most about them — while the role rendered twice,
    because it is also pre-filled into the Role input.

    The address now sits beside the name always; the role hint lives only
    where it can be edited.
    """
    user = _user("prop-mail@example.com")
    _proposal(user, role_hint="Vice President, Campus Recruiting")
    card = _card(_page(user))

    assert "kbiji.sanghvi@centerview.com" in card
    assert card.count("Vice President, Campus Recruiting") == 1, (
        "the role hint is printed as prose as well as into the input it belongs in"
    )
    assert 'name="role"' in card and 'value="Vice President, Campus Recruiting"' in card


def test_a_proposal_with_no_firm_still_names_the_address_once():
    """The firm slot falls back to the address when there is no firm, and has
    since before this pass. Now that the address also has a line of its own,
    the fallback must not print it twice.
    """
    user = _user("prop-nofirm@example.com")
    _proposal(user, firm=None, email="sam@some-boutique-advisory.co.uk", name="Sam Okonkwo")
    card = _card(_page(user))

    assert card.count("sam@some-boutique-advisory.co.uk") == 1


def test_the_evidence_reads_as_evidence_under_the_name():
    """"You reached out: <subject>" used to be a co-equal middle column. It is
    the REASON this person is on the page, so it belongs under the person, in
    `.act-who`, subordinate to the name — not beside it.
    """
    user = _user("prop-evid@example.com")
    _proposal(user)
    card = _card(_page(user))

    who = card.split('class="act-who"', 1)[1].split('class="act-ask"', 1)[0]
    assert "You reached out:" in who
    assert "Coffee chat about your summer analyst experience" in who


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------
def test_the_role_input_is_finally_the_size_its_own_rule_asked_for():
    """`.act-fill-in` has declared `padding: 4px 8px` and `--fs-s` since the
    blanks were added and not one character of it applied: coverage.css §7
    styles every text input as `input[type="text"]`, 0,1,1 against this rule's
    0,1,0. Measured at 1280 before this pass: a 41px control 203px wide with
    185px of usable inner width, against 226px of placeholder text — the card
    asked for a role in a box too small to show the question. After: 288px
    wide, 270px inner, against 196px of text at the smaller size, and 30.2px
    tall.

    Typed selectors are the whole fix, so the test is on the type: a rule
    written `.act-fill-in { … }` again loses to the global one silently.
    """
    user = _user("prop-input@example.com")
    _proposal(user)
    css = _styles(_page(user))

    assert "input.act-fill-in" in css and "select.act-fill-in" in css
    assert re.search(r"input\.act-fill-in, select\.act-fill-in \{[^}]*padding: 5px 10px", css)
    # And the right column is wide enough to hold the placeholder it prints.
    assert "minmax(19rem, 21rem)" in css


def test_the_region_control_is_told_the_same_thing_on_the_class_that_renders():
    """base.html's `enhance()` wraps every non-multiple select in `.csel`,
    hides the native control as `.csel-native` and draws a `.csel-btn` in its
    place. `select.act-fill-in` therefore governs a 1px clipped element while
    the visible box kept coverage.css's full `9px 14px` — 41px next to a
    30.2px Role input, two blanks in one form at visibly different sizes.
    """
    user = _user("prop-select@example.com")
    _proposal(user)
    css = _styles(_page(user))

    assert re.search(r"\.act-fill \.csel-btn \{[^}]*padding: 5px 10px", css)
    assert ".act-fill .csel-btn" in css


def test_the_blank_labels_drop_the_stacked_forms_margin():
    """coverage.css §7 gives every `label` `var(--s4) 0 var(--s1)`, which is
    right for a stacked form and wrong for a label sitting BESIDE its control.
    Measured 2026-09-02: it made the Role row 39.2px tall around a 30.2px
    input, twice a card.
    """
    user = _user("prop-lab@example.com")
    _proposal(user)
    css = _styles(_page(user))

    assert re.search(r"\.act-fill-lab \{[^}]*margin: 0", css)


def test_both_blanks_keep_a_finger_sized_target():
    """The compact padding takes these two controls to 30px, which is fine for
    a cursor and under the floor for a finger. At 41px they were three pixels
    short of it and nothing said so, because §16b's `pointer: coarse` block
    covers `.btn` and not a single input. Shrinking them without the floor
    would have turned a near miss into a shipped one.
    """
    user = _user("prop-touch@example.com")
    _proposal(user)
    css = _styles(_page(user))

    coarse = re.findall(r"@media \(pointer: coarse\) \{(.*?)\n  \}", css, re.S)
    assert coarse, "the proposal blanks declare no coarse-pointer floor"
    floor = "\n".join(coarse)
    assert "act-fill-in" in floor and "csel-btn" in floor
    assert "min-height: 44px" in floor
