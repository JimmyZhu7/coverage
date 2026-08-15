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
                             extract_duration, extract_facts, extract_gpa,
                             extract_grad_years, extract_languages,
                             extract_pay, extract_rolling, extract_study_stage)


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


# --- GPA hedge gate ---------------------------------------------------------
# Round 4 regression: a "preferred"/"desired"/"ideally"-worded GPA rendered
# identically to a hard cutoff (same label, same "fact-plain" styling),
# indistinguishable from a genuine minimum like Brookfield's "Minimum 3.0
# Cumulative GPA". Fixtures below are trimmed verbatim from live scraped
# `detail_text` (BofA id=18204, Citi id=17758, Barclays id=14503, and the
# hard-cutoff contrast is Brookfield id=17745) — proving the gate against the
# actual scraped shapes, including the ones that read as false positives
# under a naive wide-window check (Morgan Stanley id=1857, Oliver Wyman
# id=524: an unrelated "preferred" sits hundreds of characters away in a
# different bullet once HTML list punctuation is stripped) and under a naive
# unbounded-by-sentence check (BofA id=1795: an unrelated "Preferred majors"
# 129 characters before an actually-hard GPA number; Barclays id=1247: a
# genuinely hard "GPA of 3.2 or above." immediately followed, past its own
# sentence-ending period, by an unrelated "Ideally, you would also have an
# interest in working..." only 18 characters later).

def test_a_gpa_preferred_after_the_number_is_hedged():
    got = extract_gpa("Desired Skills 3.2 minimum GPA preferred Pursing a major")
    assert got["value"] == "3.2"
    assert got["hedge"] is True


def test_a_gpa_preferred_before_the_number_is_hedged():
    got = extract_gpa("You have obtained a preferred GPA of 3.5 or above "
                      "You are a proficient user of MS Word")
    assert got["value"] == "3.5"
    assert got["hedge"] is True


def test_ideally_worded_gpa_is_hedged():
    got = extract_gpa("with an anticipated graduation date between December "
                      "2027 and June 2028. Ideally you'll have a GPA of 3.2 "
                      "or above. You'll have mathematical skills")
    assert got["value"] == "3.2"
    assert got["hedge"] is True


def test_a_hard_gpa_cutoff_is_not_hedged():
    got = extract_gpa("Minimum 3.0 Cumulative GPA Strong analytical skills required")
    assert got["value"] == "3.0"
    assert got["hedge"] is False


def test_a_distant_preferred_in_an_unrelated_bullet_does_not_hedge_a_hard_gpa():
    """Morgan Stanley id=1857 / Oliver Wyman id=524's shape: once HTML list
    punctuation is stripped, an unrelated "preferred" from a LATER bullet
    sits only a few hundred characters away with no sentence boundary
    between them and a naive wide window mistakes it for a hedge."""
    got = extract_gpa(
        "You have a minimum cumulative GPA of 3.0 Track record of academic "
        "excellence Excellent attention to detail and the ability to manage "
        "competing priorities Excellent written and verbal communication "
        "skills preferred across all our internship cohorts globally")
    assert got["value"] == "3.0"
    assert got["hedge"] is False


def test_an_unrelated_hedge_word_in_the_prior_sentence_does_not_hedge_a_hard_gpa():
    """Barclays id=1247's shape: the GPA sentence ends in a hard "or above."
    and only the NEXT, unrelated sentence happens to start with "Ideally" —
    a pure character-distance check (no sentence boundary) misreads this as
    a hedge purely because "Ideally" lands within a few characters."""
    got = extract_gpa(
        "with anticipated graduation date between December 2027 - June 2028 "
        "and a GPA of 3.2 or above. Ideally, you would also have an "
        "interest in working across global markets")
    assert got["value"] == "3.2"
    assert got["hedge"] is False


def test_an_unrelated_hedge_word_before_a_hard_gpa_does_not_hedge_it():
    """BofA id=1795's shape: an earlier, unrelated "Preferred majors"
    sentence sits well before a GPA number that is itself stated as a plain
    minimum with its own trailing hedge -- the gate must key off the word
    actually attached to the number ("preferred" directly after it), not an
    unrelated hedge word much further back."""
    got = extract_gpa(
        "Qualifications: Preferred majors include Accounting, Finance, "
        "Economics, or related fields Minimum GPA of 3.5 preferred "
        "Experience with data tools")
    assert got["value"] == "3.5"
    assert got["hedge"] is True  # the number's OWN trailing "preferred" governs


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


def test_a_colon_between_graduate_and_programme_still_excludes():
    """Regression test for the confirmed SIG defect (id=9171): the negative
    lookahead's leading gap was plain `\\s+`, which cannot cross a colon.
    SIG's own template also writes "Soon-to-be or recent graduate : Our
    programme starts in early September 2026." -- word-adjacency held
    ("graduate" then "programme"), but a colon sits between them, so the old
    `\\s+`-only gap never found the forbidden pattern and the exclusion
    silently never fired, reading the programme's own start year (2026) as
    the applicant's stated graduation year. Real stored text, copied off the
    live row."""
    assert extract_grad_years(
        "or smarter, more effective ways to operate. Soon-to-be or recent "
        "graduate : Our programme starts in early September 2026. We "
        "welcome applications from final-year students.") is None
    # The plain word-adjacent case (no punctuation at all) must keep working.
    assert extract_grad_years(
        "Soon-to-be or recent graduate: our programme starts in "
        "September 2026.") is None
    # A genuine graduation statement elsewhere in the same posting is still read.
    got = extract_grad_years(
        "Soon-to-be or recent graduate : Our programme starts in early "
        "September 2026. Applicants must be on track to graduate in 2027.")
    assert got and got["years"] == ["2027"]


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


# --- Programme length --------------------------------------------------
# Regression test for the confirmed McKinsey id=1946 / Morgan Stanley id=1852
# defect: the mandatory digit group in `_WEEKS` sat immediately before
# "week(s)", so on a stated range the match started at the number ADJACENT
# to "weeks" and never reached back far enough to capture the low bound.
# "8-12 weeks internship" rendered a chip claiming a fixed 12-week
# programme for something the posting itself states as 8-12 weeks.

def test_a_plain_stated_length_is_read():
    got = extract_duration("This is a 10 week internship programme.")
    assert got["value"] == "10 weeks"
    assert got["weeks"] == 10
    assert got["low_weeks"] is None


def test_a_stated_week_range_keeps_its_low_bound():
    """McKinsey id=1946's exact live shape."""
    got = extract_duration(
        "During your 8-12 weeks internship, you will serve as a junior "
        "consultant.")
    assert got["value"] == "8-12 weeks"
    assert got["weeks"] == 12
    assert got["low_weeks"] == 8
    assert "8-12 weeks" in got["phrase"]


def test_a_second_stated_week_range_keeps_its_low_bound():
    """Morgan Stanley id=1852's exact live shape."""
    got = extract_duration("This is 8-10 weeks internship program for students.")
    assert got["value"] == "8-10 weeks"
    assert got["weeks"] == 10
    assert got["low_weeks"] == 8


def test_a_week_range_out_of_band_drops_only_the_low_bound():
    """A nonsense low bound (outside the 2-52 week band) must not sink the
    whole fact -- the high bound is still a genuine programme length."""
    got = extract_duration("This is 1-10 weeks internship program for students.")
    assert got["value"] == "10 weeks"
    assert got["low_weeks"] is None


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


# --- Pay: multi-location merge + currency -----------------------------------
# Round 4 regression: extract_pay `return`ed on the FIRST regex match, so a
# multi-location posting's higher-paying city was silently discarded, and any
# stated non-USD currency code was dropped entirely, rendering a CAD figure
# identically to a genuinely-USD one. Fixtures are trimmed verbatim from live
# scraped `detail_text` (Wells Fargo id=5079: 3 locations; id=1262: 6
# locations; id=5080: the "2 Locations" placeholder whose own text actually
# lists 3 cities; TD Securities id=19632 for the currency code).

def test_multi_location_pay_range_takes_the_full_low_to_high_span():
    """Wells Fargo id=5079's exact shape: Minneapolis pays more than
    Charlotte/Irving, but the OLD code returned on the first match
    (Charlotte, $33.66) and never looked at the rest of the block."""
    got = extract_pay(
        "Pay Range Charlotte, NC : $33.66 – $33.66/Hour Irving, TX : "
        "$33.66 - $33.66/Hour Minneapolis, MN : $37.02 - $37.02/Hour "
        "Wells Fargo only considers candidates")
    assert got["value"] == "$33.66–$37.02/hr"
    assert got["low"] == 33.66
    assert got["high"] == 37.02


def test_six_city_pay_block_still_finds_the_outlier_city():
    """Wells Fargo id=1262: the higher Minneapolis rate sits 4th of 6 cities
    in the block, well past the first match's own sentence."""
    got = extract_pay(
        "Pay Range: Charlotte, NC: $33.66 - $33.66/Hour Des Moines, IA: "
        "$33.66 - $33.66/Hour Dallas Metro, TX: $33.66 - $33.66/Hour "
        "Minneapolis, MN: $37.02-$37.02/Hour Phoenix Metro, AZ: $33.66 - "
        "$33.66/Hour San Antonio, TX: $33.66 - $33.66/Hour In this role")
    assert got["value"] == "$33.66–$37.02/hr"


def test_lower_first_listed_city_is_not_the_final_answer():
    """Wells Fargo id=5080: the first-listed city ($43.27) is actually the
    HIGH end here — Charlotte ($36.06) and Minneapolis ($39.91), listed
    after it, are both lower. The merged range must span all three either
    way, not just keep whichever was printed first."""
    got = extract_pay(
        "Pay Range: New York, NY & San Francisco, CA: $43.27-$43.27 hourly "
        "Charlotte, NC: $36.06 – $36.06 hourly Minneapolis, MN: $39.91 – "
        "$39.91 hourly Wells Fargo only considers candidates")
    assert got["value"] == "$36.06–$43.27/hr"
    assert got["low"] == 36.06
    assert got["high"] == 43.27


def test_a_pay_range_far_later_in_a_long_posting_is_not_merged_in():
    """The merge is bounded to the same pay-range block: a second, unrelated
    dollar range much later in a long posting (a different section
    entirely) must not widen the reported figure."""
    filler = "x" * 800
    got = extract_pay(
        f"Pay Range: $85,000 - $95,000. {filler} Referral Bonus: "
        f"$150,000 - $200,000 for qualifying hires.")
    assert got["value"] == "$85k–$95k"


def test_a_stated_non_usd_currency_code_is_kept_on_the_chip():
    """TD Securities id=19632's exact shape: the code sits right after the
    range, with no unit phrase in between."""
    got = extract_pay(
        "Pay Details: $52,700 - $74,400 CAD TD is committed to providing "
        "fair and equitable compensation opportunities")
    assert got["value"] == "$52k–$74k CAD"
    assert got["currency"] == "CAD"


def test_an_unstated_currency_is_not_labeled_usd():
    got = extract_pay("Pay Range $85,000-$100,000")
    assert got["currency"] is None
    assert "USD" not in got["value"]


def test_an_explicit_usd_code_does_not_add_a_redundant_suffix():
    got = extract_pay("Salary: $120,000 - $140,000 USD annually")
    assert got["currency"] == "USD"
    assert got["value"] == "$120k–$140k"  # no "USD" suffix — it's the default


# --- Rolling ---------------------------------------------------------------

def test_stated_rolling_review_is_read():
    assert extract_rolling(
        "Applications are reviewed on a rolling basis.")["value"] == "Rolling"


def test_silence_about_closing_is_not_rolling_review():
    """The claim this whole split exists to stop: ~600 open roles say nothing
    about how they close, and the feed called every one of them rolling."""
    assert extract_rolling("Join our 2028 Summer Analyst Programme.") is None


# --- Year of study -----------------------------------------------------

def test_a_stated_eligibility_stage_is_read():
    got = extract_study_stage(
        "Requirements: Final-year student or recent graduate in Computer "
        "Science.")
    assert got["value"] == "Final year or recent graduate"


def test_an_or_eligibility_disjunction_keeps_both_named_groups():
    """Round 12 regression: extract_study_stage used to `return` on the
    FIRST _STUDY_STAGE regex that matched anywhere in the text, so a
    posting stating it welcomes BOTH groups ("current college student or
    recent graduate" -- SIG id=8935) collapsed to whichever group's pattern
    happened to run first in the list, silently dropping the other one. The
    dropped-side direction alternated with which two stages were named, so
    both directions are covered here: SIG's shape drops "current student"
    without this fix (falls through to "recent graduate" alone), and the
    PwC/IMC/Optiver/Carlyle shape below drops "recent graduate" (falls
    through to "final year" alone). Confirmed live on 16 open rows across 6
    firms."""
    got = extract_study_stage(
        "The ideal candidate for this role is a current college student "
        "or recent graduate who is eager to gain hands-on experience.")
    assert got["value"] == "Current student or recent graduate"

    got = extract_study_stage(
        "Requirements: final-year student or recent graduate in "
        "Engineering.")
    assert got["value"] == "Final year or recent graduate"


def test_a_bare_current_student_mention_is_read():
    """"Current student" only existed as half of the OR-disjunction shape
    above until this round -- a posting stating it, alone, produced no chip
    at all even though it is exactly the kind of stated eligibility fact
    this module exists to surface."""
    got = extract_study_stage(
        "We are looking for a current student to join our summer "
        "programme.")
    assert got["value"] == "Current student"


def test_recent_graduate_eligibility_statement_is_read():
    got = extract_study_stage(
        "This role is ideal for recent graduates or early-career "
        "professionals.")
    assert got["value"] == "Recent graduate"


def test_hearing_from_recent_graduates_is_not_an_eligibility_statement():
    """Round 5 regression: Bank of America id=18061's insight-event template
    names recent grads as PANELISTS ('gain firsthand insights from recent
    graduates about life at Bank of America'), not as who may attend. Before
    the context gate this produced a false 'Recent graduate' chip on 9 open
    BofA rows."""
    assert extract_study_stage(
        "Hear from senior leaders on the firm's South Korea's strategy, and "
        "gain firsthand insights from recent graduates about life at Bank "
        "of America.") is None


def test_hear_from_across_a_long_appositive_list_is_still_excluded():
    """The other confirmed BofA shape (ids 5634/5376/5379/5380/5381/5382):
    the governing 'hear from' sits far ahead of 'recent graduates' across a
    run of appositive nouns, not adjacent to it — the gate must look back
    far enough to still catch it."""
    assert extract_study_stage(
        "During the session, you'll hear from, and have the opportunity to "
        "ask questions to, our senior leaders, recent graduates, as well as "
        "our recruitment team.") is None


def test_a_genuine_requirement_after_an_unrelated_earlier_from_still_reads():
    """The gate must not become so wide it swallows a real eligibility
    statement merely because the word 'from' appears somewhere earlier in
    the description, in an unrelated clause."""
    got = extract_study_stage(
        "Applications are welcome from students in all majors. "
        "We are looking for a recent graduate to join our team.")
    assert got["value"] == "Recent graduate"


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
