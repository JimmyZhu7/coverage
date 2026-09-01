"""`_language_fit` and the language chip: the one eligibility input that is
a warning or a match and NEVER a wall (directory/views.py, 2026-09-01).

The three states the field has to honour, and the ordering rule that makes
wiring it safe:

* No languages listed: the card is exactly what it was before the field
  existed — a bare "Mandarin needed" chip, no verdict.
* A named language the student lacks: a `language_warn` verdict that
  blocks nothing ("Mandarin needed · not in your profile").
* A named language the student listed: a `language_ok` match ("Mandarin ·
  you speak it").

And neither ever outranks a year or visa verdict. `year_out` is what
`Candidate.blocked` and the fit filter read; `year_ok` is what the bulk-save
offer counts. A non-blocking language verdict returned ahead of them would
un-block wrong-year roles and un-count right-year ones for precisely the
students who filled the field in — 33 of the 214 language-stating rows on
the live board also state a grad window.
"""

from __future__ import annotations

import pytest

from directory.models import Firm, Opportunity
from directory.views import (
    _eligibility, _eligibility_profile, _fact_chips, _language_fit,
)

pytestmark = pytest.mark.django_db

_BARCLAYS_HK = ("Fluent in written Chinese and spoken Mandarin if applying "
                "to the role in Hong Kong SAR.")


def _firm(slug="barclays", name="Barclays", **kw):
    return Firm.objects.create(slug=slug, name=name, **kw)


def _role(firm, *, facts, region="hk", title="IB Summer Analyst", **extra):
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{title.replace(' ', '-').lower()}",
        title=title, bucket="internship", status="open", region=region,
        raw={"facts": facts}, **extra)


def _mandarin(firm, **extra):
    facts = {"language": {"value": "Mandarin", "langs": ["Mandarin"],
                          "phrase": _BARCLAYS_HK}}
    facts.update(extra.pop("facts", {}))
    return _role(firm, facts=facts, **extra)


def _profile(**over):
    p = {"class_year": None, "work_auth": {}, "languages": [], "study_level": ""}
    p.update(over)
    return p


def _labels(chips):
    return [c["label"] for c in chips]


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------

def test_no_languages_listed_is_todays_behaviour_exactly():
    """Both sides must speak. A student who never filled Languages in gets
    no verdict and the bare fact chip the feed always showed."""
    o = _mandarin(_firm())
    profile = _profile()
    assert _language_fit(o, profile) is None
    assert _eligibility(o, profile) is None
    chips = _fact_chips(o, verdict=None)
    assert _labels(chips) == ["Mandarin needed"]
    assert chips[0]["css"] == "fact-wall"
    assert chips[0]["why"] == _BARCLAYS_HK


def test_a_named_language_the_student_lacks_warns_and_never_blocks():
    o = _mandarin(_firm())
    profile = _profile(languages=["english"])
    v = _language_fit(o, profile)
    assert v["kind"] == "language_warn"
    assert v["blocking"] is False
    assert v["label"] == "Mandarin needed · not in your profile"
    assert _BARCLAYS_HK in v["why"] and "never hides a role" in v["why"]
    # Nothing else spoke, so this IS the verdict, and the fact chip that
    # produced it stands down rather than saying the same thing twice.
    assert _eligibility(o, profile) == v
    assert "Mandarin" not in " ".join(_labels(_fact_chips(o, verdict=v)))


def test_a_listed_language_reads_as_a_match():
    o = _mandarin(_firm())
    profile = _profile(languages=["english", "mandarin"])
    v = _language_fit(o, profile)
    assert v["kind"] == "language_ok"
    assert v["blocking"] is False
    assert v["label"] == "Mandarin · you speak it"
    assert _eligibility(o, profile) == v
    assert "Mandarin" not in " ".join(_labels(_fact_chips(o, verdict=v)))


# ---------------------------------------------------------------------------
# Matching details
# ---------------------------------------------------------------------------

def test_matching_is_case_blind_and_trims():
    o = _mandarin(_firm())
    assert _language_fit(o, _profile(languages=["  MANDARIN "]))["kind"] == "language_ok"


def test_two_named_languages_report_the_one_that_is_missing():
    o = _role(_firm(), facts={"language": {
        "value": "Mandarin · Cantonese", "langs": ["Mandarin", "Cantonese"],
        "phrase": "Fluency in Mandarin and Cantonese required."}})
    warn = _language_fit(o, _profile(languages=["mandarin"]))
    assert warn["kind"] == "language_warn"
    assert warn["label"] == "Cantonese needed · not in your profile"
    ok = _language_fit(o, _profile(languages=["mandarin", "cantonese"]))
    assert ok["label"] == "Mandarin · Cantonese · you speak both"


def test_a_fact_stored_before_the_extractor_carried_langs_still_matches():
    """Rows extracted before `langs` existed only have `value`."""
    o = _role(_firm(), facts={"language": {"value": "Mandarin", "phrase": _BARCLAYS_HK}})
    assert _language_fit(o, _profile(languages=["mandarin"]))["kind"] == "language_ok"
    assert _language_fit(o, _profile(languages=["french"]))["kind"] == "language_warn"


def test_a_posting_stating_no_language_gets_no_language_verdict():
    o = _role(_firm(), facts={"gpa": {"value": "3.0", "phrase": "minimum GPA of 3.0"}})
    profile = _profile(languages=["mandarin"])
    assert _language_fit(o, profile) is None
    assert _eligibility(o, profile) is None


# ---------------------------------------------------------------------------
# Never outranks a year or visa verdict
# ---------------------------------------------------------------------------

def test_a_language_warning_never_masks_a_blocking_year_verdict():
    """`year_out` must survive: it is what `Candidate.blocked` reads. The
    language reading rides along, and the CHIP carries it, personalised."""
    o = _mandarin(_firm(), facts={"grad": {
        "value": "2027", "years": ["2027"], "phrase": "graduating in 2027"}})
    v = _eligibility(o, _profile(class_year=2028, languages=["english"]))
    assert v["kind"] == "year_out"
    assert v["blocking"] is True
    assert v["language"]["kind"] == "language_warn"
    chips = _fact_chips(o, verdict=v)
    lang = next(c for c in chips if "Mandarin" in c["label"])
    assert lang["label"] == "Mandarin needed · not in your profile"
    assert lang["css"] == "fact-wall"
    assert "never hides a role" in lang["why"]
    assert not any(c["label"].startswith("Grad") for c in chips), \
        "the year_out verdict still suppresses the window that produced it"


def test_a_language_match_rides_on_a_year_ok_verdict():
    """`year_ok` must survive: it is what the bulk-save offer counts."""
    o = _mandarin(_firm(), facts={"grad": {
        "value": "2028", "years": ["2028"], "phrase": "graduating in 2028"}})
    v = _eligibility(o, _profile(class_year=2028, languages=["mandarin"]))
    assert v["kind"] == "year_ok"
    assert v["language"]["kind"] == "language_ok"
    chips = _fact_chips(o, verdict=v)
    lang = next(c for c in chips if "Mandarin" in c["label"])
    assert lang["label"] == "Mandarin · you speak it"
    assert lang["css"] == "fact-ok"


def test_a_language_warning_never_outranks_a_visa_wall():
    o = _mandarin(_firm(), sponsorship="no")
    v = _eligibility(o, _profile(work_auth={"hk": "sponsorship"}, languages=["english"]))
    assert v["kind"] == "visa_out"
    assert v["blocking"] is True


def test_a_year_verdict_without_languages_carries_no_language_key():
    """Readers that compare verdicts must see exactly what they saw before
    the field existed when the student has not filled it in."""
    o = _mandarin(_firm(), facts={"grad": {
        "value": "2028", "years": ["2028"], "phrase": "graduating in 2028"}})
    v = _eligibility(o, _profile(class_year=2028))
    assert v["kind"] == "year_ok" and "language" not in v


# ---------------------------------------------------------------------------
# The profile carries the new fields
# ---------------------------------------------------------------------------

def test_the_profile_carries_languages_and_study_level(django_user_model):
    u = django_user_model.objects.create_user(
        email="lang@x.com", password="x", languages=[" Mandarin ", "english"],
        study_level="undergrad")
    p = _eligibility_profile(u)
    assert p["languages"] == ["mandarin", "english"]
    assert p["study_level"] == "undergrad"
    assert p["class_year"] is None and p["work_auth"] == {}


def test_languages_alone_are_enough_for_the_profile_to_exist(django_user_model):
    u = django_user_model.objects.create_user(
        email="only-lang@x.com", password="x", languages=["mandarin"])
    assert _eligibility_profile(u)["languages"] == ["mandarin"]


def test_a_user_who_stated_nothing_still_has_no_profile(django_user_model):
    u = django_user_model.objects.create_user(email="none@x.com", password="x")
    assert _eligibility_profile(u) is None


# ---------------------------------------------------------------------------
# On the page
# ---------------------------------------------------------------------------

def test_the_feed_card_says_you_speak_it(client, django_user_model):
    u = django_user_model.objects.create_user(
        email="hk@x.com", password="x", languages=["english", "mandarin"])
    _mandarin(_firm(), title="Mandarin Desk Summer Analyst")
    client.force_login(u)
    body = client.get("/opportunities/").content.decode()
    card = body[body.index("Mandarin Desk Summer Analyst"):]
    card = card[:card.index("</article>")] if "</article>" in card else card[:2000]
    assert "Mandarin · you speak it" in card
    assert "Mandarin needed" not in card


def test_the_feed_card_says_not_in_your_profile(client, django_user_model):
    u = django_user_model.objects.create_user(
        email="us@x.com", password="x", languages=["english"])
    _mandarin(_firm(), title="Mandarin Desk Summer Analyst")
    client.force_login(u)
    body = client.get("/opportunities/").content.decode()
    card = body[body.index("Mandarin Desk Summer Analyst"):]
    card = card[:card.index("</article>")] if "</article>" in card else card[:2000]
    assert "Mandarin needed · not in your profile" in card
    assert card.count("Mandarin needed") == 1, "the verdict says it once"
