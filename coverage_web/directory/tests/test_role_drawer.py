"""The description, read here instead of behind a four-second Workday shell.

The drawer's contract is narrow and worth pinning: it shows the text we
actually hold, it says so plainly when we hold none, it never renders page
furniture as if it were the job, and the card only offers a Read button when
there is something behind it.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from directory.facts import paragraphs
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


DESCRIPTION = (
    "Job Description The Operations Division is the front line of defence for "
    "the Firm. Responsibilities Support daily transactions and controls. "
    "Qualifications Minimum GPA of 3.5. Graduating in 2028."
)

# A real Bank of America page, shortened: the cookie banner and the navigation
# arrive before a single word about the job.
CHROME = (
    "APAC | Japan | 2026 Recruitment Event - Bank of America This site stores "
    "cookies on your device that contain information about you and your "
    "preferences. Strictly Necessary cookies are always on. Cookie Policy "
    "Accept all cookies I accept the cookie policy Disable non-essential "
    "cookies Read more about our cookie policy Skip to content Toggle "
    "navigation Login | Register Home Global Programs Campus Events "
)


@pytest.fixture
def role(db):
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    return Opportunity.objects.create(
        firm=firm, url="https://ms.test/1", title="2027 Summer Analyst",
        bucket="internship", status="open", region="us", location="New York",
        raw={"detail_text": DESCRIPTION, "detail_fetched": True,
             "facts": {"gpa": {"value": "3.5", "phrase": "Minimum GPA of 3.5."}}},
    )


def test_the_drawer_serves_the_text_we_already_hold(client, role):
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "front line of defence" in html
    assert "Morgan Stanley" in html
    assert role.url in html, "the way to actually apply"


def test_the_drawer_shows_each_fact_beside_its_evidence(client, role):
    """The chips on the card have room for a value; this has room for the
    sentence that produced it, which is the difference between a claim and a
    quotation."""
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "3.5" in html
    assert "Minimum GPA of 3.5." in html


def test_a_stated_start_year_reaches_the_drawer(client, db):
    """Round 7 regression: `extract_start_year` (facts.py) stores an
    accurate 'start' fact on 9.5% of open target-bucket rows, but no
    `_FACT_LABELS` entry ever turned it into a rendered chip -- it was
    computed, phrase-verified, and then silently discarded before it
    reached any student-facing surface. The card's 2-chip budget is
    already spoken for by sponsorship/pay/language, so this only checks
    the drawer, which has room for everything a posting states."""
    firm = Firm.objects.create(slug="hsbc-start", name="HSBC")
    role = Opportunity.objects.create(
        firm=firm, url="https://hsbc.test/1", title="2027 Summer Analyst",
        bucket="internship", status="open", region="us", location="New York",
        raw={"detail_text": "Start Date and Duration: Mon Jul 19, 2027; 2 years",
             "facts": {"start": {
                 "value": "2027",
                 "phrase": "Start Date and Duration: Mon Jul 19, 2027; 2 years",
             }}},
    )
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "Start date" in html
    assert "2027" in html
    assert "Start Date and Duration: Mon Jul 19, 2027; 2 years" in html


def test_a_posting_we_never_fetched_says_so(client, role):
    role.raw = {}
    role.save(update_fields=["raw"])
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "haven&#x27;t fetched" in html or "haven't fetched" in html
    assert role.url in html, "the link out is the answer when we hold nothing"


def _markup(client) -> str:
    """The feed with its <script> blocks removed.

    The page's own drawer script contains the string "data-role-read" (it is
    the selector it listens on), so a bare substring test passes whether or
    not a single card rendered the button. Same reason the style-block tests
    strip <style> before asserting a class is absent.
    """
    import re

    html = client.get(reverse("opportunities")).content.decode()
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S)


def test_the_read_button_only_appears_where_there_is_text(client, role):
    """A button that opens an empty drawer is the same lie one step earlier."""
    assert "data-role-read" in _markup(client)

    role.raw = {}
    role.save(update_fields=["raw"])
    assert "data-role-read" not in _markup(client)


def test_a_missing_role_is_a_404(client, db):
    assert client.get(reverse("role_description", args=[999999])).status_code == 404


# ---------------------------------------------------------------------------
# A closed posting's drawer must not claim it's still open. Reproduced live:
# Opportunity id=12797 (TD Securities, Cherry Hill) is status='closed',
# closed_at set, re-verified closed against the firm's own site — yet the
# drawer rendered "It still shows as open because we also can't confirm it
# closed" and an active "Open the application" link. The drawer is reachable
# for a closed row from My Applications ('Read the posting' on any tracked
# stage, regardless of the posting's own status), not just the feed (which
# only lists status='open' rows and so never surfaces this path itself).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_closed_postings_drawer_says_so_and_drops_the_apply_claim(client):
    from datetime import timedelta

    from django.utils import timezone as dj_timezone

    firm = Firm.objects.create(slug="td-sec", name="TD Securities")
    now = dj_timezone.now()
    role = Opportunity.objects.create(
        firm=firm, url="https://td.wd3.myworkdayjobs.com/job/cherry-hill",
        title="Banking Associate", bucket="internship", region="us",
        location="Cherry Hill, New Jersey", status="closed",
    )
    # The exact shape id=12797 was found in: last_checked ahead of
    # last_verified (the shape `_unconfirmed_note` otherwise fires a
    # caution for) AND closed_at set.
    Opportunity.objects.filter(pk=role.pk).update(
        last_verified=now - timedelta(hours=11),
        last_checked=now, closed_at=now,
    )
    html = client.get(reverse("role_description", args=[role.id])).content.decode()

    assert "This posting is closed" in html
    assert "no longer accepting applications" in html
    # The false claim must be gone, not just relabelled.
    assert "still shows as open" not in html
    assert "Not recently confirmed live" not in html
    # No active "apply here" call to action for a role that cannot be
    # applied to anymore.
    assert "Open the application on" not in html
    assert role.url in html, "still reachable for reference, just not as a CTA"


@pytest.mark.django_db
def test_the_closed_postings_confirmation_time_is_a_single_unit(client):
    """Cross-surface consistency audit, finding C: `timesince` defaults to
    `depth=2` and this line rendered "confirmed 5 days, 13 hours ago" —
    noise for a caution meant to be read at a glance, and inconsistent with
    `directory.views._posting_closed_note`'s identical My Applications
    sentence, which already called `timesince(..., depth=1)` directly. Both
    now go through the same one-unit convention — the drawer via
    `core.templatetags.textstyle.timesince1`, the Python site via its own
    `depth=1` argument."""
    from datetime import timedelta

    from django.utils import timezone as dj_timezone

    firm = Firm.objects.create(slug="td-sec-depth", name="TD Securities")
    now = dj_timezone.now()
    role = Opportunity.objects.create(
        firm=firm, url="https://td.wd3.myworkdayjobs.com/job/depth-test",
        title="Banking Associate", bucket="internship", region="us",
        location="Cherry Hill, New Jersey", status="closed",
    )
    Opportunity.objects.filter(pk=role.pk).update(
        last_verified=now - timedelta(days=5, hours=14),
        last_checked=now, closed_at=now - timedelta(days=5, hours=13),
    )
    html = client.get(reverse("role_description", args=[role.id])).content.decode()

    assert "confirmed 5\xa0days ago" in html
    assert "confirmed 5\xa0days, 13\xa0hours ago" not in html


@pytest.mark.django_db
def test_an_open_postings_drawer_is_unaffected_by_the_closed_branch(client, role):
    """Sibling check: the new `{% if o.status == 'closed' %}` branch must not
    change anything for the ~all-open common case."""
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "This posting is closed" not in html
    assert "Open the application on Morgan Stanley" in html


@pytest.mark.django_db
def test_the_bucket_chip_shows_the_human_label_not_the_raw_code(client):
    """Live report: the drawer's meta line read the literal snake_case
    bucket code ("entry_level", "other") instead of its label ("Entry-Level",
    "Other"). `role_description()` built its context with only `"o": opp`
    and never a `bucket_label` key, unlike the two sibling call sites in this
    module that both compute `BUCKET_LABELS.get(bucket, bucket)` -- and
    `Opportunity` has no `bucket_label` attribute, so the template's
    `o.bucket_label` lookup always failed silently and fell through to the
    raw code via the `default` filter."""
    firm = Firm.objects.create(slug="citi-el", name="Citi")
    role = Opportunity.objects.create(
        firm=firm, url="https://citi.test/1", title="Officer, Finance Analyst",
        bucket="entry_level", status="open",
    )
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "Entry-Level" in html
    assert "entry_level" not in html


@pytest.mark.django_db
def test_the_other_bucket_chip_reads_the_human_label(client):
    """BUCKET_LABELS[OTHER] is "Experienced", not "Other" -- see classify.py's
    comment on that mapping: "Other" is what this bucket is to the classifier,
    not what it is to a reader, since every row in it is an experienced hire."""
    firm = Firm.objects.create(slug="sig-other", name="SIG")
    role = Opportunity.objects.create(
        firm=firm, url="https://sig.test/1", title="Trading Systems Developer",
        bucket="other", status="open",
    )
    html = client.get(reverse("role_description", args=[role.id])).content.decode()
    assert "<span>Experienced</span>" in html


# --- Reading the text ------------------------------------------------------

def test_headings_become_paragraph_breaks():
    """The stored text is one unbroken line: Workday delivers HTML and the
    tags are stripped before storage, so the only structure left is the
    section headings these firms all write."""
    blocks = paragraphs(DESCRIPTION)
    assert len(blocks) == 3
    assert blocks[1].startswith("Responsibilities")
    assert blocks[2].startswith("Qualifications")


def test_barclays_own_headings_also_become_breaks():
    """Live report: a Barclays posting rendered as one unbroken wall of text
    in the drawer. Its template uses "Purpose of the role" and
    "Accountabilities" where the rest of _SECTIONS only recognized "About
    the role" and "Responsibilities" — so it never split at all."""
    text = (
        "Job Description Purpose of the role Bringing quantitative expertise "
        "to the team. Accountabilities Develop and implement quantitative "
        "models. Design and maintain trading platforms and risk systems."
    )
    blocks = paragraphs(text)
    assert len(blocks) == 3
    assert blocks[1].startswith("Purpose of the role")
    assert blocks[2].startswith("Accountabilities")


def test_a_title_specific_heading_does_not_orphan_the_title_word():
    """A real live posting: "...trading infrastructure. Assistant Vice
    President Expectations To advise..." Adding "Vice President
    Expectations" AND "Assistant Vice President Expectations" as separate
    _SECTIONS entries once split this into two pieces sandwiching a lone
    "Assistant" paragraph — the shorter phrase's own lookbehind is satisfied
    right after "Assistant" ends in a letter, independent of the longer
    phrase matching earlier. Neither is in the list now; assert no orphan."""
    text = (
        "Design and maintain trading infrastructure. Assistant Vice "
        "President Expectations To advise and influence decision making."
    )
    blocks = paragraphs(text)
    assert not any(b.strip() == "Assistant" for b in blocks)


def test_a_heading_word_mid_sentence_is_not_a_break():
    """"the requirements of the programme" is prose, "Requirements" is a
    heading. Case is the only thing that tells them apart."""
    blocks = paragraphs("We reviewed the requirements of the programme carefully.")
    assert len(blocks) == 1


def test_a_cookie_banner_is_not_a_job_description():
    """Live: 153 of 854 stored descriptions open with consent text and a nav
    bar. The drawer rendered the firm's cookie policy under the job's title."""
    blocks = paragraphs(CHROME + DESCRIPTION)
    assert blocks, "the description survives"
    assert "cookie" not in " ".join(blocks).lower()
    assert "front line of defence" in " ".join(blocks)


def test_chrome_stripping_never_eats_the_whole_posting():
    """If the markers match something that was not chrome, keeping the
    original text is the safe failure."""
    text = "Accept all cookies is the name of our team's internal joke."
    assert paragraphs(text) == [text]


def test_a_long_description_is_cut_with_a_mark_not_silently():
    body = "Sentence about the role. " * 400
    joined = " ".join(paragraphs(body))
    assert len(joined) < 4200
    assert joined.endswith("…"), "a silent truncation reads as the whole posting"


# ---------------------------------------------------------------------------
# THE DRAWER IS NOT THE FEED'S ALONE. The firm page and the palette both used
# to send a student out to a client-rendered board for text this database
# already holds — the poorer product, reachable without ever knowing a better
# one existed.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_firm_page_offers_the_posting_it_holds(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://gs.com/sa",
        raw={"detail_text": "A ten-week programme in New York." * 12})
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "data-role-read" in body
    assert 'id="role-drawer"' in body


@pytest.mark.django_db
def test_the_firm_page_offers_nothing_for_a_posting_it_never_read(client):
    firm = Firm.objects.create(slug="ubs", name="UBS")
    Opportunity.objects.create(
        firm=firm, title="Unread Analyst", bucket="internship", status="open",
        url="https://ubs.com/unread")
    # <script> stripped: the drawer's own opener listens on this selector, so
    # a bare substring test passes whether or not a card rendered a button.
    # Same reason `_markup` above strips it for the feed.
    body = re.sub(r"<script.*?</script>", "",
                  client.get(f"/firms/{firm.slug}/").content.decode(), flags=re.S)
    assert "Unread Analyst" in body
    assert "data-role-read" not in body


@pytest.mark.django_db
def test_search_sends_a_read_role_to_our_own_page(client, django_user_model):
    user = django_user_model.objects.create_user(email="s@x.com", password="x")
    client.force_login(user)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    held = Opportunity.objects.create(
        firm=firm, title="Held Analyst", bucket="internship", status="open",
        url="https://gs.com/held", raw={"detail_text": "Ten weeks. " * 40})
    Opportunity.objects.create(
        firm=firm, title="Held Elsewhere Analyst", bucket="internship",
        status="open", url="https://gs.com/elsewhere")

    roles = client.get("/search/?q=Held").json()["roles"]
    ours = next(r for r in roles if r["title"] == "Held Analyst")
    theirs = next(r for r in roles if r["title"] == "Held Elsewhere Analyst")

    assert ours["external"] is False
    assert f"read={held.id}" in ours["url"]
    # A role we never read still links out: for that one the firm's page
    # genuinely is the only copy.
    assert theirs["external"] is True
    assert theirs["url"] == "https://gs.com/elsewhere"


@pytest.mark.django_db
def test_a_card_names_itself_so_a_deep_link_can_find_it(client):
    firm = Firm.objects.create(slug="citi", name="Citi")
    o = Opportunity.objects.create(
        firm=firm, title="Deep Link Analyst", bucket="internship", status="open",
        url="https://citi.com/dl", raw={"detail_text": "Ten weeks. " * 40})
    body = client.get("/opportunities/").content.decode()
    assert f'data-role-id="{o.id}"' in body


@pytest.mark.django_db
def test_my_applications_can_read_the_postings_it_tracks(client, django_user_model):
    """The one role-listing surface that could not open the posting behind
    the decision — chips shipped to it, the drawer did not."""
    from analytics.models import UserOpportunity

    user = django_user_model.objects.create_user(email="ma@x.com", password="x")
    client.force_login(user)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    held = Opportunity.objects.create(
        firm=firm, title="Held Analyst", bucket="internship", status="open",
        url="https://gs.com/held", raw={"detail_text": "Ten weeks. " * 40})
    unread = Opportunity.objects.create(
        firm=firm, title="Unread Analyst", bucket="internship", status="open",
        url="https://gs.com/unread")
    UserOpportunity.all_objects.create(user=user, opportunity=held)
    UserOpportunity.all_objects.create(user=user, opportunity=unread)

    body = client.get("/opportunities/mine/").content.decode()
    assert 'id="role-drawer"' in body
    assert f'data-role-id="{held.id}"' in body
    assert f'data-role-id="{unread.id}"' not in body, \
        "never offer to open what we do not hold"


# ---------------------------------------------------------------------------
# DRAWER PARITY WITH THE CARD (WS-OPP-06).
#
# The drawer is where the student DECIDES (this file's own opening note), and
# it was missing three things the card already had: the sponsorship answer,
# the personal eligibility verdict, and why the role was picked. A student
# clicked through from a card reading "Won't sponsor you here" into a panel
# that did not mention visas at all.
# ---------------------------------------------------------------------------

def _drawer(client, opp):
    return client.get(reverse("role_description", args=[opp.id])).content.decode()


def test_the_drawer_quotes_the_sponsorship_sentence_verbatim(client):
    """The sentence, with its label, never a derived badge. The stated claims
    are four incommensurable kinds and Barclays appends a right-to-work
    disclosure to every posting including ones that also say it will sponsor
    (`research-eligibility-language.md §6`, Grade A)."""
    firm = Firm.objects.create(slug="ubs-spon", name="UBS")
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://ubs.test/spon", sponsorship="no",
        raw={"detail_text": "Text.",
             "facts": {"sponsorship": {
                 "value": "no",
                 "phrase": "We are unable to sponsor visas for this role."}}},
    )
    body = _drawer(client, o)
    assert "Visa sponsorship" in body
    assert "We are unable to sponsor visas for this role." in body


def test_the_drawer_says_so_when_the_posting_states_no_sponsorship_answer(client):
    """Silence is the answer on most rows, and a drawer that simply omits the
    line leaves the reader to supply their own guess about the fact most able
    to end the decision. P1: say what is not known."""
    firm = Firm.objects.create(slug="quiet-spon", name="Quiet")
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://quiet.test/1", raw={"detail_text": "Text."})
    body = _drawer(client, o)
    assert "Visa sponsorship" in body
    assert "Not stated in this posting" in body


def test_the_drawer_states_the_eligibility_verdict_or_its_absence(client, django_user_model):
    """Both halves. A student whose year the posting names gets the verdict;
    a signed-out reader gets the honest "not stated" line rather than a blank
    — a verdict needs both sides to have spoken."""
    firm = Firm.objects.create(slug="elig", name="Elig")
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://elig.test/1",
        raw={"detail_text": "Text.",
             "facts": {"grad": {"value": "2028", "years": ["2028"],
                                "phrase": "graduating in 2028"}}},
    )
    anon = _drawer(client, o)
    assert "Eligibility not stated in this posting" in anon

    user = django_user_model.objects.create_user(
        email="elig@example.com", password="x" * 14)
    user.class_year = 2028
    user.save()
    client.force_login(user)
    signed_in = _drawer(client, o)
    assert "Your year (2028)" in signed_in
    assert "graduating in 2028" in signed_in


def test_no_derived_sponsorship_badge_is_rendered_anywhere():
    """The shape the research forbids, checked as absence in the templates
    that could carry it. A boolean or a badge collapses four incommensurable
    kinds of claim into one, which is exactly what
    `research-eligibility-language.md §6` (Grade A) rules out."""
    from pathlib import Path

    templates = Path(__file__).resolve().parents[2] / "templates" / "directory"
    for path in sorted(templates.rglob("*.html")):
        text = path.read_text()
        for banned in ("sponsorship_badge", "sponsors_ok"):
            assert banned not in text, f"{path.name} renders a derived badge: {banned}"


def test_the_drawer_prints_why_coverage_rates_this_one(client, django_user_model):
    """The card's Picked column prints `pick_why`; the drawer that card opens
    printed nothing, so the student who clicked BECAUSE the product said
    "this one" arrived at the panel where they decide with the reasoning left
    behind on the card."""
    from crm.models import UserFirm

    user = django_user_model.objects.create_user(
        email="why-drawer@example.com", password="x" * 14)
    user.tracks = ["ib"]
    user.regions = ["us"]
    user.save()
    client.force_login(user)

    firm = Firm.objects.create(slug="pjt-why", name="PJT", tracks=["ib"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    o = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst", bucket="internship",
        status="open", url="https://pjt.test/1", region="us",
        raw={"detail_text": "Text."})

    body = _drawer(client, o)
    assert "drawer-why" in body
    assert "Tier 1" in body


def test_a_role_under_the_bar_shows_no_reasoning(client, django_user_model):
    """Whether a role IS in the top six needs the whole board ranked, which
    is not a thing to do inside a single-role fetch. The bar used instead is
    `MIN_SCORE` — the same one the ranker applies before ordering anything —
    so a row below it shows nothing rather than a weak justification for
    something the product is not recommending."""
    user = django_user_model.objects.create_user(
        email="thin-drawer@example.com", password="x" * 14)
    user.tracks = ["ib"]
    user.regions = ["us"]
    user.save()
    client.force_login(user)

    # Untargeted, untiered, wrong region: nothing to score on.
    firm = Firm.objects.create(slug="nobody", name="Nobody", tracks=["consulting"])
    o = Opportunity.objects.create(
        firm=firm, title="Operations Programme", bucket="internship",
        status="open", url="https://nobody.test/1", region="eu",
        raw={"detail_text": "Text."})

    body = _drawer(client, o)
    assert "drawer-why" not in body


def test_the_card_now_carries_three_fact_chips(client):
    """The cap moved from 2 to 3 because the constraint it was measured under
    is gone: `.rr-meta` wraps, so a third chip costs a line break rather than
    a cut chip. 128 open rows were hiding a third fact behind the old cap and
    every one of them also stated sponsorship or a year of study."""
    from directory.views import _FACT_CHIPS_MAX, _fact_chips

    firm = Firm.objects.create(slug="threechip", name="Three Chip")
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://threechip.test/1", sponsorship="no",
        raw={"facts": {
            "sponsorship": {"value": "no", "phrase": "No sponsorship."},
            "study": {"value": "Penultimate year", "phrase": "Penultimate year."},
            "gpa": {"value": "3.5", "phrase": "Minimum GPA 3.5."},
            "duration": {"value": "10 weeks", "phrase": "Ten weeks."},
        }},
    )
    assert _FACT_CHIPS_MAX == 3
    assert len(_fact_chips(o)) == 3
