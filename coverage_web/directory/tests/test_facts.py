"""What a description says, and — mostly — what it does not.

Nearly every test here is a REFUSAL. The extractors are cheap to make greedy
and expensive to trust, and each false positive below was found on live text
during the first pass over 854 real descriptions: a language listed as "a
plus" rendered as a hard wall, an accessibility boilerplate line rendered as a
video interview, a point salary rendered as a range. Those are the cases the
patterns exist to refuse, so they are the cases the tests are mostly about.
"""

from __future__ import annotations

from directory.facts import (extract_assessment, extract_cover_letter,
                             extract_facts, extract_gpa, extract_grad_years,
                             extract_languages, extract_pay, extract_rolling)


# --- GPA -------------------------------------------------------------------

def test_a_stated_gpa_cutoff_is_read():
    got = extract_gpa("Minimum GPA of 3.5 required.")
    assert got["value"] == "3.5"
    assert "3.5" in got["phrase"]


def test_a_whole_number_cutoff_keeps_its_decimal():
    """"3" reads as a rounded-off 3-point-something; the posting said 3.0."""
    assert extract_gpa("GPA above 3.0 out of 4.")["value"] == "3.0"


def test_a_gpa_scale_is_not_a_gpa_requirement():
    """Live on Citi: "a minimum 3.0 GPA out of 4.0" rendered "GPA 4.0". The
    forward pattern reached the denominator first, so the card stated a
    different requirement from the one the posting made — not a stricter
    reading of it, a wrong one."""
    got = extract_gpa("a minimum 3.0 GPA out of 4.0 at undergraduate level")
    assert got["value"] == "3.0"


def test_a_number_before_the_word_is_the_cutoff():
    assert extract_gpa("cumulative 3.2 GPA or above")["value"] == "3.2"


def test_a_bare_scale_states_no_requirement():
    """Naming the scale is not naming a bar to clear."""
    assert extract_gpa("Grades are reported on a 4.0 scale.") is None


def test_the_evidence_never_opens_mid_number():
    """A full stop with digits either side is a decimal point. Treating it as
    a sentence end opened the evidence for a 3.0 cutoff at "0 GPA out of
    4.0", which reads as a parsing failure even where the value is right."""
    phrase = extract_gpa("We require a minimum 3.0 GPA out of 4.0.")["phrase"]
    assert phrase.startswith("We require")


def test_a_year_beside_the_word_gpa_is_not_a_gpa():
    assert extract_gpa("Class of 2028. GPA discussed at interview.") is None


def test_no_gpa_mentioned_is_not_a_gpa_of_zero():
    assert extract_gpa("Strong academic record expected.") is None


# --- Graduation window -----------------------------------------------------

def test_a_graduation_range_keeps_both_ends():
    got = extract_grad_years("Graduating between August 2027 and December 2028.")
    assert got["value"] == "2027–2028"
    assert got["years"] == ["2027", "2028"]


def test_a_single_graduation_year_is_read_alone():
    assert extract_grad_years("On track to graduate in 2028.")["value"] == "2028"


def test_a_historic_year_is_not_an_eligibility_window():
    """"our graduate programme began in 2015" is a fact about the firm."""
    assert extract_grad_years("Our graduate programme began in 2015.") is None


# --- Language --------------------------------------------------------------

def test_a_required_language_is_a_wall():
    got = extract_languages("Fluency in Mandarin is required for this role.")
    assert got["value"] == "Mandarin"


def test_a_language_that_is_only_a_plus_is_not_a_wall():
    """Live: PwC. "Fluency in English required; French or German is a plus"
    matched every requirement pattern and was not a requirement."""
    assert extract_languages(
        "Languages: Fluency in English required; French or German is a plus") is None


def test_a_language_that_is_a_strong_preference_is_not_a_wall():
    """Live: Bank of America."""
    assert extract_languages(
        "Fluency in English is essential and a fluency in German is a strong "
        "preference.") is None


def test_english_alone_is_never_a_wall():
    """Everyone on this board already reads English; saying so is noise."""
    assert extract_languages("Fluency in English is required.") is None


def test_two_required_languages_both_travel():
    got = extract_languages(
        "Fluency in Mandarin is required. Fluency in Cantonese is required.")
    assert got["langs"] == ["Mandarin", "Cantonese"]


# --- Cover letter ----------------------------------------------------------

def test_a_required_cover_letter_is_read():
    assert extract_cover_letter(
        "Please submit your CV and cover letter.")["value"] == "Cover letter"


def test_an_optional_cover_letter_is_not_a_requirement():
    assert extract_cover_letter("A cover letter is optional.") is None


def test_a_cover_letter_explicitly_not_required_is_not_a_requirement():
    assert extract_cover_letter("A cover letter is not required.") is None


# --- Assessments -----------------------------------------------------------

def test_a_named_assessment_is_read():
    assert extract_assessment(
        "Complete a pre-recorded video submission via HireVue.")["value"] == "HireVue"


def test_accessibility_boilerplate_is_not_an_assessment():
    """Live: every Bank of America page carries this line. The generic phrase
    "video interview" used to tag it, so a page that never mentioned an
    assessment claimed one."""
    assert extract_assessment(
        "If you need a workplace adjustment to search for a job opening, need "
        "help completing your application or video interview, let us know.") is None


# --- Pay -------------------------------------------------------------------

def test_an_annual_range_reads_in_thousands():
    got = extract_pay("Pay Range $85,000-$100,000")
    assert got["value"] == "$85k–$100k"
    assert got["unit"] == "year"


def test_a_point_salary_is_not_rendered_as_a_range():
    """Live: Evercore posts "$120,000 - $120,000". "$120k–$120k" reads as
    broken output rather than as a precise number."""
    assert extract_pay("Salary Range: $120,000 - $120,000")["value"] == "$120k"


def test_an_hourly_rate_keeps_its_cents_and_its_unit():
    """Live: Wells Fargo. Dropping the cents makes an exact figure look
    approximate, and dropping the unit makes $40 an hour look like $40 a year."""
    got = extract_pay("Pay Range: Charlotte, NC: $40.87 - $40.87 hourly")
    assert got["value"] == "$40.87/hr"
    assert got["unit"] == "hour"


def test_a_deal_size_is_not_a_salary():
    assert extract_pay("We advise on transactions of $50 - $500 million.") is None


# --- Rolling ---------------------------------------------------------------

def test_stated_rolling_review_is_read():
    assert extract_rolling(
        "Applications are reviewed on a rolling basis.")["value"] == "Rolling"


def test_silence_about_closing_is_not_rolling_review():
    """The claim this whole split exists to stop: ~600 open roles say nothing
    about how they close, and the feed called every one of them rolling."""
    assert extract_rolling("Join our 2028 Summer Analyst Programme.") is None


# --- The whole pass --------------------------------------------------------

def test_facts_carry_the_words_that_produced_them():
    """The honesty contract: a chip that cannot show its own evidence does not
    ship, so every fact holds the phrase it came from."""
    facts = extract_facts(
        "Minimum GPA of 3.5. Graduating in 2028. Applications are reviewed on "
        "a rolling basis.")
    assert set(facts) == {"gpa", "grad", "rolling"}
    assert all(f["phrase"] for f in facts.values())


def test_an_empty_description_states_nothing():
    assert extract_facts("") == {}
    assert extract_facts(None) == {}


def test_html_entities_do_not_blind_the_extractors():
    """Pages are cached as fetched, so `&#160;` is literal in the text. Six
    characters spent on one space silently shortens every pattern's window."""
    assert extract_facts(
        "Minimum&#160;GPA&#160;of&#160;3.5&#160;required.")["gpa"]["value"] == "3.5"


def test_evidence_contains_the_fact_it_proves():
    """A phrase trimmed from the left of a 600-character sentence often did
    not contain the match — evidence that reads as a mis-extraction even when
    the value is right."""
    text = ("The team covers a wide range of sectors and geographies " * 6 +
            "and candidates need a minimum GPA of 3.5 to apply.")
    assert "3.5" in extract_facts(text)["gpa"]["phrase"]


# ---------------------------------------------------------------------------
# Region mapping — a bug found while feeding this function prose.
# ---------------------------------------------------------------------------

def test_a_us_state_suffix_still_maps_to_the_us():
    from directory.classify import normalize_region

    for place in ("Denver, CO", "Boston, MA", "New York, NY",
                  "Charlotte, NC / Dallas, TX"):
        assert normalize_region(place) == "us", place


def test_a_country_that_starts_with_a_state_code_is_not_the_us():
    """Live bug: ", ca" sits inside "Toronto, Canada" and ", co" inside
    "Bogota, Colombia", so five Canadian roles were filed under the United
    States. A two-letter state code needs a boundary after it."""
    from directory.classify import normalize_region

    # The guard is "not the US", which is what the ", ca"/", co" bug broke.
    # Where the country is one Coverage recognises as an untracked market it
    # now files under "other" instead of blank; either way it is not the US.
    for place in ("Toronto, Canada", "Vancouver, Canada", "Bogota, Colombia",
                  "San Jose, Costa Rica", "Phnom Penh, Cambodia"):
        assert normalize_region(place) != "us", place
    assert normalize_region("Toronto, Canada") == "other"
    # Cambodia and bare "San Jose" are deliberately absent from the untracked
    # key list — San Jose is California or Costa Rica and guessing is worse
    # than silence — so these stay blank.
    assert normalize_region("Phnom Penh, Cambodia") == ""
    assert normalize_region("San Jose, Costa Rica") == ""


def test_ordinary_prose_has_no_region():
    """The same bug with a comma in a sentence: "timely, complete and
    accurate" resolved to the United States."""
    from directory.classify import normalize_region

    assert normalize_region("ensuring timely, complete and accurate reporting") == ""


# --- Region from prose -----------------------------------------------------

def test_a_stated_location_fills_the_region():
    from directory.classify import region_from_prose

    assert region_from_prose("Program Locations: New York, NY and Charlotte") == "us"
    assert region_from_prose("This role is based in Hong Kong.") == "hk"


def test_boilerplate_name_drops_are_not_a_location():
    """A bank's About section lists half its offices; only text after a
    location anchor counts."""
    from directory.classify import region_from_prose

    assert region_from_prose(
        "Our London, Hong Kong and New York teams work as one firm.") == ""


def test_a_location_outside_the_tracked_markets_files_under_other():
    """Prose has to agree with the location field: if "Bangalore" in the
    location column resolves to "other", the same word read out of the
    description cannot resolve to blank. One fact, one answer, whichever
    field carried it."""
    from directory.classify import region_from_prose

    assert region_from_prose("Location: Bangalore, India") == "other"
    # A place nothing recognises is still silence, not a guess.
    assert region_from_prose("Location: Ulaanbaatar") == ""


def test_two_markets_in_anchored_windows_mean_no_answer():
    from directory.classify import region_from_prose

    assert region_from_prose(
        "Location: New York. This role is based in Hong Kong.") == ""
