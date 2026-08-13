"""What a description says, and — mostly — what it does not.

Nearly every test here is a REFUSAL. The extractors are cheap to make greedy
and expensive to trust, and each false positive below was found on live text
during the first pass over 854 real descriptions: a language listed as "a
plus" rendered as a hard wall, an accessibility boilerplate line rendered as a
video interview, a point salary rendered as a range. Those are the cases the
patterns exist to refuse, so they are the cases the tests are mostly about.
"""

from __future__ import annotations

import pytest

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


def test_an_or_list_with_the_later_year_first_still_sorts_low_to_high():
    """Regression test for the confirmed Optiver/RBC defect: 'graduating in
    2028 or after 2027 September' is an OR-eligibility statement (graduate
    in 2028, or already graduated by Sept 2027), not an ascending range —
    but the connector regex treats 'or' the same as '-'/'to', so the years
    were captured in the sentence's own order and the label read the
    backwards '2028–2027'. Confirmed live on 6 rows (Optiver ids
    19209/18634/18629/18627, RBC ids 417/416)."""
    got = extract_grad_years("candidates graduating in 2028 or after 2027 September")
    assert got["value"] == "2027–2028"
    got = extract_grad_years("candidates graduating in Spring 2028 or December 2027")
    assert got["value"] == "2027–2028"


def test_graduate_programme_start_is_not_the_applicants_own_graduation():
    """Regression test for the confirmed SIG defect (12 of 25 open rows,
    48%): 'invited to join our full-time graduate programme in either
    September 2027, January 2028 or August 2028' states when the
    POST-INTERNSHIP FULL-TIME OFFER starts, not when the applicant
    graduates — but the bare `graduat\\w*` gate matched the word "graduate"
    inside "graduate programme" identically to a genuine graduation
    statement. Confirmed live on SIG id=9111, whose own true graduation cue
    ("completed 3 years at university by June 2027") implies a 2028
    graduate, while the old extractor stored value='2027' straight off the
    programme-start sentence."""
    assert extract_grad_years(
        "Successful interns will be invited to join our full-time graduate "
        "programme in either September 2027, January 2028 or August 2028, "
        "depending on availability.") is None
    # A genuine "graduate scheme" start-date phrasing is refused the same way.
    assert extract_grad_years(
        "you will be invited to join our graduate scheme in either "
        "January or August 2028") is None
    # But a real graduation statement elsewhere in the same posting is still read.
    got = extract_grad_years(
        "Successful interns will be invited to join our full-time graduate "
        "programme in either September 2027, January 2028 or August 2028. "
        "Applicants must be on track to graduate in 2028.")
    assert got and got["years"] == ["2028"]


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


def test_a_language_explicitly_not_required_is_not_a_wall():
    """Regression test for the confirmed Morgan Stanley/Blackstone defect:
    the old guard only looked FORWARD from where the match ended, but in
    all three live examples the negation sits INSIDE the match itself,
    between the language name and the requirement keyword that made the
    pattern fire — never after it. Confirmed live: id=1911 'Proficiency in
    German is not required', id=1878 'Knowledge of German is beneficial but
    not essential', id=348 '...advantageous but not required'. All three
    previously stored facts.language with the negation quoted as its own
    'phrase', rendering a blocking 'German needed' chip."""
    assert extract_languages("Proficiency in German is not required.") is None
    assert extract_languages(
        "Knowledge of German is beneficial but not essential.") is None
    assert extract_languages(
        "Proficiency in a European language (Italian or German) is "
        "advantageous but not required.") is None
    # A genuine requirement is unaffected by an unrelated negation elsewhere
    # in the same sentence.
    assert extract_languages(
        "Fluency in Mandarin is required; German is not offered here."
    )["value"] == "Mandarin"


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
    # than silence — so Cambodia stays blank and bare "San Jose" stays blank.
    # "Costa Rica" itself became a recognised untracked market in the
    # 2026-08-08 census (McKinsey's hub states it as the country), so the
    # stated pair now files under "other": the COUNTRY resolves the city.
    assert normalize_region("Phnom Penh, Cambodia") == ""
    assert normalize_region("San Jose") == ""
    assert normalize_region("San Jose, Costa Rica") == "other"


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
    # A place nothing recognises is still silence, not a guess. The example
    # used to be Ulaanbaatar, which stopped being unrecognised the day two
    # live rows stated it — an untracked-markets key list grows as the board
    # does, so this assertion needs a placeholder no employer here posts in.
    assert region_from_prose("Location: Nuuk") == ""


def test_two_markets_in_anchored_windows_mean_no_answer():
    from directory.classify import region_from_prose

    assert region_from_prose(
        "Location: New York. This role is based in Hong Kong.") == ""


# ---------------------------------------------------------------------------
# The programme start year — the body-stated twin of the title cohort. Every
# positive case below is a real live phrase; so is every negative, and the
# negatives are the point: the same descriptions carry publication dates,
# conversion-offer years, eligibility study windows and next-season
# boilerplate, each of which this extractor once read or nearly read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,year", [
    # Label forms.
    ("Programme Start Date and Duration: Tue Jun 01, 2027; 10 weeks", "2027"),
    ("Type de contrat Stage Date prévue de prise de fonction 01/06/2026", "2026"),
    ("Inizio Internship: agosto/settembre 2026 Durata: 6 mesi", "2026"),
    # Prose forms.
    ("We are looking for an intern (m/f/d) starting in July 2026 for a "
     "period of ideally 6 months", "2026"),
    ("accepting applications from candidates who are available starting "
     "from January 2027", "2027"),
    ("The Summer Internship program will commence in June 2027 and run for "
     "nine weeks in our Hong Kong office", "2027"),
    ("commencing in February 2027. Our team helps public sector", "2027"),
    # Month-to-month ranges need no verb: nothing but a programme period
    # takes this shape.
    ("seeking a Corporate Banking Marketing Officer from January 2027 - "
     "August 2027 who will be primarily", "2027"),
    ("join a consulting team for a six-to-eight-week period during June "
     "and July 2027, where you will contribute", "2027"),
])
def test_start_year_reads_stated_intakes(text, year):
    from directory.facts import extract_start_year
    got = extract_start_year(text)
    assert got and got["value"] == year
    assert year in got["phrase"]


@pytest.mark.parametrize("text", [
    # SocGen: the start is "Immediately"; the year is the PUBLICATION date.
    "Reference 26000FL3 Start date Immediately Publication date 2026/06/26 "
    "Responsibilities",
    # BofA: the year a DIFFERENT job would begin — the conversion offer, one
    # summer after this internship.
    "interns who perform well during the internship may be offered full "
    "time employment to commence in July 2028",
    # Morgan Stanley Japan: an eligibility clause dating the applicant's
    # studies, not the programme.
    "or studying abroad outside of Japan during the academic year from "
    "June 2026 to September 2027",
    # IMC: the NEXT season's start, in rejection boilerplate.
    "You may reapply when the next recruitment season begins in September "
    "2027. We encourage you to",
    # Page furniture: copyright, requisition number, graduation ceremony.
    "© 2026 Squarepoint. All Rights Reserved.",
    "Rejoignez nos équipes ! Référence 2026-110866 Date de mise à jour",
    "must be available prior to commencement in May 2019",
])
def test_start_year_refuses_years_that_are_not_starts(text):
    from directory.facts import extract_start_year
    assert extract_start_year(text) is None


def test_grad_reads_or_ranges_and_recovers_past_a_stale_first_match():
    from directory.facts import extract_grad_years
    # SIG's range connector is "or", with words between it and the year.
    got = extract_grad_years(
        "open to students who are planning to graduate in the winter of "
        "2028 or the spring of 2029")
    assert got and got["years"] == ["2028", "2029"]
    # A history year earlier in the text must not end the search: the real
    # statement is still ahead.
    got = extract_grad_years(
        "our graduate scheme has run since 2019. Applicants should "
        "graduate in 2027")
    assert got and got["years"] == ["2027"]
    # Deutsche Bank states the same fact without the word "graduate".
    got = extract_grad_years(
        "Studying Technology related subjects; Bachelor's degree "
        "completion: 2026 or later")
    assert got and got["years"] == ["2026"]


def test_event_date_is_a_start():
    """An insight event's stated date is when the thing takes place — the
    same fact Citi labels "Event Start Date" and BofA plain "Event Date"."""
    from directory.facts import extract_start_year
    got = extract_start_year(
        "Event Date September 14, 2026 5:00 PM BST View Cookie Notice")
    assert got and got["value"] == "2026"
    got = extract_start_year(
        "Event Start Date: 28/08/2026 Event Start Time: 11:00 AM SGT")
    assert got and got["value"] == "2026"
