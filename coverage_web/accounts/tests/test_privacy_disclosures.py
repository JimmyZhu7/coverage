"""Guard: the two disclosures the privacy page cannot lose.

Most of `templates/legal/privacy.html` is prose, and prose gets edited. Two
paragraphs in it are not prose in that sense — they are the things an outside
reviewer checks for, and losing either one in a copy pass is a silent
regression that only surfaces months later as a rejected verification.

1. Google's Limited Use representation. Coverage asks for `gmail.readonly`
   (settings/base.py's GMAIL_LIVE_SCOPES), a restricted scope, and Google's
   API Services User Data Policy requires the near-exact sentence asserted
   below to appear in the published privacy policy. Reworded, it does not
   count.

2. The Gmail-to-Anthropic flow. capture/gmail_residue.py sends a message's
   subject line and Gmail preview snippet to a third party during a "Scan
   Now" rescan. An onward transfer of Gmail-derived data that the policy
   does not name is exactly the finding that stalls a review.

These assert on the rendered page, not the file, so a template that stops
rendering fails them too.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils.html import strip_tags

pytestmark = pytest.mark.django_db

# Google's required wording. The subject may name the app; the rest is
# theirs. "and transfer to any other app of" is not optional filler -- it is
# part of the mandated sentence (Google's own published wording, verified
# 2026-08-30 against Google for Developers' Limited Use disclosure guidance
# and matched verbatim by every third-party Google-API disclosure page
# checked). A prior version of both this page and this constant dropped that
# clause, which is exactly the kind of silent trim a copy pass makes without
# realizing the sentence is not ours to shorten.
LIMITED_USE = (
    "use and transfer to any other app of information received from Google "
    "APIs will adhere to the Google API Services User Data Policy"
)


@pytest.fixture
def page(client):
    """The rendered page as running text.

    Tags are stripped and whitespace collapsed because the sentences below
    are read by a human reviewer, not a parser. Google's required wording has
    a link in the middle of it, so a raw-HTML match would fail on markup while
    the page reads exactly right.
    """
    resp = client.get(reverse("accounts:privacy"))
    assert resp.status_code == 200
    return " ".join(strip_tags(resp.content.decode()).split())


def test_the_policy_carries_googles_limited_use_wording(page):
    assert LIMITED_USE in page
    assert "including the Limited Use requirements" in page


def test_the_limited_use_statement_links_to_the_policy_it_names(client):
    resp = client.get(reverse("accounts:privacy"))
    assert "developers.google.com/terms/api-services-user-data-policy" in resp.content.decode()


def test_the_policy_says_gmail_subjects_and_snippets_go_to_anthropic(page):
    """Named provider, named data, named trigger. All three, or a reader
    cannot tell what they are agreeing to."""
    assert "Anthropic" in page
    assert "Scan Now" in page
    assert "subject line" in page and "snippet" in page


def test_the_ai_sharing_section_counts_scan_now_among_the_triggers(page):
    """The section used to list three AI triggers, then four (2026-08-30:
    Autopilot, capture/autopilot.py, makes it five -- it is a real,
    user-triggered Anthropic call the founder's own account has 53 decisions
    from, and it was missing from this list entirely). The list and the code
    have to stay in step."""
    section = page.split("Who we share data with", 1)[1]
    section = section.split("Cookies and sessions", 1)[0]
    for trigger in (
        "Talk to Coverage",
        "coffee-chat brief",
        "relationship summary",
        "Scan Now",
        "Autopilot",
    ):
        assert trigger in section, f"{trigger} is an AI trigger and must be listed"


def test_the_ai_sharing_section_discloses_autopilot_sends_the_email_address(page):
    """Autopilot's evidence_text() sends `PERSON: {name} <{email}>` to
    Anthropic (capture/autopilot.py) -- unlike the advisor, which is told
    only whether a contact has an email on file. The blanket "email
    addresses are deliberately excluded" line only covers the advisor's
    three triggers; Autopilot needs its own, separate, honest sentence."""
    section = page.split("Who we share data with", 1)[1]
    section = section.split("Cookies and sessions", 1)[0]
    assert "Autopilot" in section and "email address" in section


def test_the_page_is_still_marked_a_draft():
    """The disclosure gaps are filled; the lawyer review is not done. The
    banner comes off when counsel says so, not when a test stops caring."""
    from pathlib import Path

    from django.conf import settings

    source = Path(settings.BASE_DIR) / "templates" / "legal" / "privacy.html"
    assert "DRAFT. NOT REVIEWED BY A LAWYER." in source.read_text()
