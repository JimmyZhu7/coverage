"""The description, read here instead of behind a four-second Workday shell.

The drawer's contract is narrow and worth pinning: it shows the text we
actually hold, it says so plainly when we hold none, it never renders page
furniture as if it were the job, and the card only offers a Read button when
there is something behind it.
"""

from __future__ import annotations

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


# --- Reading the text ------------------------------------------------------

def test_headings_become_paragraph_breaks():
    """The stored text is one unbroken line: Workday delivers HTML and the
    tags are stripped before storage, so the only structure left is the
    section headings these firms all write."""
    blocks = paragraphs(DESCRIPTION)
    assert len(blocks) == 3
    assert blocks[1].startswith("Responsibilities")
    assert blocks[2].startswith("Qualifications")


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
