"""Graduation windows the old extractor could not express, and the two
readers that act on them.

G1  An OPEN upper bound — "graduating in 2028 or later" — was stored as the
    closed window [2028, 2028] and blocked every later class. Measured live
    before the fix: 18 open campus rows carried an open-ended phrase and
    every one of them told a 2029 student the role was not for them.
G2  A body "Class of 2028" was invisible: 32 open rows say it in prose, 17
    of them with neither a title year nor a grad fact, so the verdict and
    the scorer were blind to the one sentence that names who the role is
    for.
G3  Two windows in one sentence (Jefferies' "December 2027 – June 2028 and
    December 2028 – June 2029") came back as the first pair alone.

Every phrase below is verbatim from a live posting; the Opportunity id is in
the docstring. `refresh_grad_facts` is the command that carries the new
extractor over rows already stored.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from directory.classify import extract_class_year
from directory.facts import GRAD_YEAR_MAX, extract_grad_years
from directory.management.commands.refresh_grad_facts import (
    classify_change, refreshed_grad_fact,
)
from directory.models import Firm, Opportunity
from directory import recommend as R
from directory.recommend import (
    Candidate, Profile, _class_fit, _stated_grad_window, stated_class_mismatch,
)
from directory.views import _eligibility, _fact_chips


def _open_years(lo: int) -> list[str]:
    return [str(y) for y in range(lo, GRAD_YEAR_MAX + 1)]


def _verdict(o, cy):
    return _eligibility(o, {"class_year": cy, "work_auth": {}})


def _row(firm, **kw):
    kw.setdefault("url", f"https://x/{firm.slug}/{kw.get('title', 'r')[:8]}")
    kw.setdefault("title", "2027 Summer Analyst")
    kw.setdefault("bucket", "internship")
    kw.setdefault("status", "open")
    return Opportunity.objects.create(firm=firm, **kw)


# ---------------------------------------------------------------------------
# G1 — the extractor reads an open upper bound
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("floor, text", [
    # Optiver 19209/18634/18629 — the OR-list whose open half is "after".
    (2027, "Who you are: Bachelor’s or Master’s university student, "
           "graduating in 2028 or after 2027 September . Ma"),
    # Optiver 18628
    (2028, "Who you are: Students graduating in 2028 or later . Foundations in"),
    # Baird 21901/24937/25372
    (2028, "Anticipated graduation date of May 2028 or later Strong written "
           "and verbal communication skills"),
    # Baird 28435
    (2027, "Anticipated graduation date of December 2027 or later Interest "
           "in financial services"),
    # Deutsche Bank 27101
    (2028, "Graduation expected from December 2028 onwards (allowing "
           "availability for a two-year internship)"),
    # Société Générale 26002
    (2028, "with an expected graduation date in June 2028 or later.Advanced "
           "English is"),
    # PJT Partners 27961/27962/27969/27971
    (2026, "Currently enrolled as a full-time university student or a recent "
           "graduate (graduation no earlier than June 2026) > Available to "
           "start January 2027"),
    # PJT Partners 27963
    (2026, "recent graduate (graduation date not earlier than June 2026) "
           "Available to start from January 2027"),
    # Morgan Stanley 1912
    (2028, "You are currently studying towards a Bachelors/Masters degree and "
           "due to graduate in Summer 2028 or later You are exploring a"),
    # Deutsche Bank 5017
    (2026, "Studying Technology related subjects; Bachelor’s degree "
           "completion: 2026 or later; Intermediary English"),
    # Optiver 2751
    (2027, "WHO YOU ARE: PhD students who are graduating in or after 2027. "
           "Studying a degree in Computer Science"),
    # Wells Fargo 19200/19201/22438/24274/27117/27118
    (2027, "Statistics or related quantitative field, with an expected "
           "graduation date after December 2027. Excellent programming skills"),
    # Bank of America 28384 and six sibling off-cycle rows
    (2026, "For candidates who have already graduated, the graduation date "
           "must be after January 2026"),
    # Citi 433
    (2026, "If you're graduating on or after December 2026, regardless of "
           "your academic discipline, come and find us"),
    # McKinsey 5622
    (2028, "communications or business administration with expected "
           "graduation date no earlier than 2028 Good knowledge of"),
    # Société Générale 7537
    (2028, "Bachelor Degree with graduation after December 2028Advanced "
           "English Level"),
])
def test_an_open_upper_bound_is_read_as_open(floor, text):
    """A floor and no ceiling. `years` runs from the floor to the
    extractor's own horizon, `open_high` says the horizon is ours, and the
    label reads "2028+" rather than the closed "2028" that blocked every
    later class."""
    got = extract_grad_years(text)
    assert got is not None
    assert got["open_high"] is True
    assert got["value"] == f"{floor}+"
    assert got["years"] == _open_years(floor)


@pytest.mark.parametrize("years, text", [
    (["2027", "2028"], "Graduating between August 2027 and December 2028."),
    # RBC 417/416 — two dates, no open bound: the closed sorted range stays.
    (["2027", "2028"], "candidates graduating in Spring 2028 or December 2027"),
    # Optiver 2865 — a closed range keeps its stored shape exactly.
    (["2027", "2029"], "Expected graduation between December 2027 and June 2029, "
                       "with sophomore standing or higher"),
    # A bare "after" is a narrative, not a bound; it needs its conjunction.
    (["2027"], "on track to graduate in 2027 after completing a four-year degree."),
    (["2028"], "On track to graduate in 2028."),
])
def test_a_closed_window_is_still_closed(years, text):
    got = extract_grad_years(text)
    assert got["years"] == years
    assert "open_high" not in got


def test_a_ceiling_is_not_a_floor():
    """PJT 27967: "Graduation date no later than June 2027" bounds the
    window from ABOVE. It is not an open-high window and must not become
    one; it is stored as it always was, the single year 2027. (An open
    LOWER bound is a separate, unmodelled shape: 38 live rows say "by" or
    "no later than", and this test pins that they are untouched here.)"""
    got = extract_grad_years("Graduation date no later than June 2027 CV must "
                             "include expected graduation month/year")
    assert got["years"] == ["2027"] and "open_high" not in got
    got = extract_grad_years("Graduating by August 2028 GPA of 3.5 or above")
    assert got["years"] == ["2028"] and "open_high" not in got


# ---------------------------------------------------------------------------
# G1 — the eligibility verdict honours the open bound
# ---------------------------------------------------------------------------

def _open_fact(lo=2028, **extra):
    return {"value": f"{lo}+", "years": _open_years(lo), "open_high": True,
            "phrase": f"Students graduating in {lo} or later", **extra}


@pytest.mark.django_db
def test_an_open_window_includes_every_later_class():
    """Optiver 18628, "Students graduating in 2028 or later": before the fix
    a 2029 student read "For 2028 grads" and was blocked."""
    o = _row(Firm.objects.create(slug="optiver", name="Optiver"),
             raw={"facts": {"grad": _open_fact(2028)}})
    for cy in (2028, 2029, 2031, GRAD_YEAR_MAX + 2):
        v = _verdict(o, cy)
        assert v["kind"] == "year_ok" and v["blocking"] is False, cy
        assert v["why"] == "Students graduating in 2028 or later"
    out = _verdict(o, 2027)
    assert out["kind"] == "year_out" and out["blocking"] is True
    assert out["label"] == "For 2028+ grads"


@pytest.mark.django_db
def test_the_flag_governs_not_the_enumeration():
    """`open_high` is the statement; the enumerated years are a courtesy to
    readers that see years alone. A fact carrying the flag and only its
    floor still includes a later student."""
    o = _row(Firm.objects.create(slug="db", name="Deutsche Bank"),
             raw={"facts": {"grad": {"value": "2026+", "years": ["2026"],
                                     "open_high": True,
                                     "phrase": "degree completion: 2026 or later"}}})
    assert _verdict(o, 2030)["kind"] == "year_ok"
    assert _verdict(o, 2025)["kind"] == "year_out"


@pytest.mark.django_db
def test_a_row_stored_before_the_flag_existed_still_reads_closed():
    """The extractor only changes `raw.facts` on re-extraction. A row still
    carrying the old shape — years and no flag — reads exactly as it did:
    closed, blocking, labelled from its own value."""
    o = _row(Firm.objects.create(slug="baird", name="Baird"),
             raw={"facts": {"grad": {"value": "2028", "years": ["2028"],
                                     "phrase": "graduation date of May 2028 or later"}}})
    v = _verdict(o, 2029)
    assert v["kind"] == "year_out" and v["label"] == "For 2028 grads"
    assert _verdict(o, 2028)["kind"] == "year_ok"


@pytest.mark.django_db
def test_silence_is_never_a_verdict():
    """No grad fact, or one with nothing readable in it, is not a window:
    no `year_out`, no `year_ok`, exactly as before."""
    f = Firm.objects.create(slug="x", name="X")
    assert _verdict(_row(f, title="a"), 2029) is None
    assert _verdict(_row(f, title="b", raw={"facts": {}}), 2029) is None
    assert _verdict(_row(f, title="c", raw={"facts": {"grad": {
        "value": "?", "years": ["n/a"], "phrase": "..."}}}), 2029) is None
    assert _verdict(_row(f, title="d", raw={"facts": {"grad": {
        "value": "2028", "years": [], "phrase": "..."}}}), 2029) is None


@pytest.mark.django_db
def test_years_stored_as_ints_are_read_too():
    o = _row(Firm.objects.create(slug="y", name="Y"),
             raw={"facts": {"grad": {"value": "2027–2028", "years": [2027, 2028],
                                     "phrase": "graduating 2027 or 2028"}}})
    assert _verdict(o, 2028)["kind"] == "year_ok"
    assert _verdict(o, 2029)["kind"] == "year_out"


@pytest.mark.django_db
def test_a_title_class_year_still_outranks_an_open_body_window():
    o = _row(Firm.objects.create(slug="z", name="Z"), class_year="2027",
             raw={"facts": {"grad": _open_fact(2028)}})
    v = _verdict(o, 2029)
    assert v["kind"] == "year_out" and v["label"] == "For 2027 grads"
    assert "Class of 2027" in v["why"]


@pytest.mark.django_db
def test_the_grad_chip_reads_the_open_label():
    o = _row(Firm.objects.create(slug="chip", name="Chip"),
             raw={"facts": {"grad": _open_fact(2028)}})
    labels = [c["label"] for c in _fact_chips(o, verdict=None)]
    assert "Grad 2028+" in labels


# ---------------------------------------------------------------------------
# G1 — the scorer's window is open too
# ---------------------------------------------------------------------------

def _cand(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("firm_id", 1)
    kw.setdefault("firm_name", "Optiver")
    kw.setdefault("firm_slug", "optiver")
    kw.setdefault("title", "2027 Summer Internship")
    kw.setdefault("url", "https://x/1")
    return Candidate(**kw)


def test_the_scorer_reads_an_open_window_as_open():
    """`Candidate` carries the years alone, so an open window reaches the
    scorer as its enumeration up to the extractor's horizon. A window that
    touches the horizon is "and everyone after": nobody later than the
    floor is a mismatch, including a student past the horizon itself.

    REWRITTEN 2026-09-01 (S3). Eligibility is unchanged — every student at or
    after the floor is still included, still not vetoed, still chipped with
    the floor — but the SCORE no longer treats all of them alike. See
    `test_an_open_window_years_below_the_student_is_a_near_miss` below."""
    c = _cand(grad_years=tuple(_open_years(2028)))
    for cy in (2028, 2029, 2032, GRAD_YEAR_MAX, GRAD_YEAR_MAX + 3):
        p = Profile(class_year=cy)
        lo, hi = _stated_grad_window(p, c)
        assert lo == 2028 and hi >= cy, cy
        assert stated_class_mismatch(p, c) is False, cy
        points, reasons = _class_fit(p, c)
        assert points > 0 and reasons[0].text.startswith("For 2028+ grads"), cy
        assert "—" not in reasons[0].text and "–" not in reasons[0].text, cy
    p = Profile(class_year=2027)
    assert _stated_grad_window(p, c) == (2028, GRAD_YEAR_MAX)
    assert stated_class_mismatch(p, c) is True
    assert _class_fit(p, c)[0] < 0


def test_an_open_window_years_below_the_student_is_a_near_miss():
    """S3, 2026-09-01. "Graduation date must be after January 2026" is a
    sentence about who is not excluded, not a sentence about who the
    programme is for — and containment could not tell the two apart, so it
    paid `W_CLASS_STATED` (30), the same as a posting that names the
    student's class outright. Three of the founder's top five rode it: two
    Bank of America London off-cycles with a floor three years back, and a
    year-round Baird internship.

    ONE YEAR is the line, the same line the rest of this axis draws. A floor
    at or within a year below the student is the firm describing this
    cohort with a tolerance and keeps the full bonus; a floor further back is
    the firm describing four cohorts at once and pays
    `W_CLASS_DERIVED_NEAR`, the product's standing "worth a look, not a fit"
    weight. The chip still prints the floor either way, because that floor is
    exactly the fact that tells the student whose programme this is."""
    c = _cand(grad_years=tuple(_open_years(2026)))
    far = Profile(class_year=2029)
    assert stated_class_mismatch(far, c) is False       # still not blocked
    points, reasons = _class_fit(far, c)
    assert points == R.W_CLASS_DERIVED_NEAR
    assert reasons[0].text == "For 2026+ grads"
    assert "(yours)" not in reasons[0].text
    assert "3 years before you" in reasons[0].detail

    # Floor within one year: still the firm naming this cohort.
    for cy, floor in ((2026, 2026), (2027, 2026), (2028, 2028), (2029, 2028)):
        p, near = Profile(class_year=cy), _cand(grad_years=tuple(_open_years(floor)))
        assert _class_fit(p, near)[0] == R.W_CLASS_STATED, (cy, floor)
        assert _class_fit(p, near)[1][0].text == f"For {floor}+ grads (yours)"

    # A CLOSED window is untouched however wide it is: a firm that named both
    # ends of it named the cohorts it meant.
    wide = _cand(grad_years=("2026", "2027", "2028", "2029"))
    assert _class_fit(Profile(class_year=2029), wide)[0] == R.W_CLASS_STATED
    assert _class_fit(Profile(class_year=2029), wide)[1][0].text == (
        "For 2026-2029 grads (yours)")


def test_a_closed_window_in_the_scorer_is_unchanged():
    c = _cand(grad_years=("2028", "2029"))
    assert _stated_grad_window(Profile(class_year=2030), c) == (2028, 2029)
    assert stated_class_mismatch(Profile(class_year=2030), c) is True
    assert stated_class_mismatch(Profile(class_year=2029), c) is False
    assert _stated_grad_window(Profile(class_year=None), c) is None
    assert _stated_grad_window(Profile(class_year=2029), _cand()) is None


@pytest.mark.django_db
def test_from_opportunity_carries_the_enumerated_years():
    o = _row(Firm.objects.create(slug="wf", name="Wells Fargo"),
             raw={"facts": {"grad": _open_fact(2027)}})
    c = Candidate.from_opportunity(o)
    assert c.grad_years == tuple(_open_years(2027))
    assert stated_class_mismatch(Profile(class_year=2029), c) is False


# ---------------------------------------------------------------------------
# G2 — the body's own "Class of 2028"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("years, text", [
    # Oliver Wyman 26964/26965
    (["2028"], "Internship length : 8 weeks Eligibility : Class of 2028 grads "
               "only (bachelor’s & master’s); Oliver Wyman does not offer visa "
               "sponsorship"),
    # Oliver Wyman 26476
    (["2028"], "Qualifications: Class of 2028 Graduate Pursuit of a degree in "
               "Business, Engineering, MIS/CS, Finance"),
    # SIG 8994/8996/9069/9070/9103/9104
    (["2027", "2028"], "Demonstrable interest in the financial markets and "
                       "current affairs. Class of 2027 or 2028 : Successful "
                       "interns are invited back as graduates to join our "
                       "Equity Research Analyst Graduate Programme"),
    # Apollo 634/26088
    (["2028"], "Qualifications & Experience Pursuing a BA or BS (Class of 2028) "
               "degree from undergraduate institution with a record of "
               "academic achievement"),
    # Apollo 4784
    (["2027"], "Pursuing an MBA or JD/MBA (Class of 2027) with excellent "
               "academic credentials"),
    # Evercore 26528
    (["2027"], "Specific Qualifications: Undergraduate – Senior (Class of 2027) "
               "Prior relevant internship experience"),
    # Oliver Wyman 524
    (["2028", "2029"], "Actuarial and Strategy Consulting Intern (ICG) Class of "
                       "2028 or 2029 Currently pursuing a degree in Actuarial "
                       "Science"),
    # BMO 27908/27913/27916 — a list, not a range, and every year of it.
    (["2028", "2029", "2030"], "Currently enrolled in an undergraduate degree "
                               "program; graduating in Spring 2028, 2029, 2030 "
                               "Proven track record of excellent academic "
                               "standing"),
])
def test_a_body_class_of_is_a_graduation_window(years, text):
    got = extract_grad_years(text)
    assert got["years"] == years
    assert "open_high" not in got


def test_a_programme_cohort_called_class_of_is_not_a_graduating_class():
    """Houlihan Lokey 23652/23653: "The position is for a Summer Analyst
    Class of 2027 (i.e. Graduate Class of 2028 Financial Analyst)". The
    first "Class of" is the intake cohort; only the second names the
    graduating class."""
    got = extract_grad_years(
        "The position is for a Summer Analyst Class of 2027 (i.e. Graduate "
        "Class of 2028 Financial Analyst).")
    assert got["years"] == ["2028"]


def test_a_historic_class_of_is_not_a_window():
    """PwC 26052: "Preferably candidates graduate in 2023 (class of 2019)"
    — both years are behind the plausible window, so there is no fact."""
    assert extract_grad_years(
        "Preferably candidates graduate in 2023 (class of 2019). Fresh "
        "graduates are welcome to apply.") is None
    assert extract_grad_years("a world-class 2027 programme for students") is None


def test_a_stated_class_of_beats_an_application_form_later_in_the_text():
    """Flow Traders 3503: the requirement is "Class of 2028, preferred"; the
    old extractor could not see it and instead read "2027" off the
    application form's own dropdown further down ("When is your expected
    graduation date? * December 2027 May 2028 ..."), blocking the 2028
    student the posting asks for."""
    got = extract_grad_years(
        "Mathematics, Statistics, Computer Science, Economics or related "
        "Class of 2028, preferred Demonstrable interest in trading and global "
        "financial markets. When is your expected graduation date? * December "
        "2027 May 2028 June 2028 December 2028 May 2029 June 2029 If you have "
        "attended a Flow Traders sponsored event, please list below")
    assert got["years"] == ["2028"] and got["value"] == "2028"


@pytest.mark.django_db
def test_a_title_class_of_is_not_double_handled():
    """Point72 25726: the title says "Class of 2029" and the body echoes it.
    `extract_class_year` owns the title; the body extractor reads its own
    prose; `_eligibility` reads the title FIRST, so there is one verdict
    and it is the title's."""
    title = "Point72 Academy Coffee Chats — Class of 2029 (US)"
    assert extract_class_year(title) == "2029"
    body = extract_grad_years(
        "Point72 Academy Coffee Chats—Class of 2029 (US) Curious what it's "
        "really like to work as an investor")
    assert body["years"] == ["2029"]
    o = _row(Firm.objects.create(slug="p72", name="Point72"), title=title,
             class_year="2029", raw={"facts": {"grad": body}})
    v = _verdict(o, 2028)
    assert v["kind"] == "year_out" and "Class of 2029" in v["why"]


@pytest.mark.django_db
def test_class_of_in_the_body_turns_a_likely_into_a_verdict(tmp_path):
    """Oliver Wyman 26964, "Eligibility : Class of 2028 grads only": before
    the fix the row carried only the convention-derived 2028 and rendered
    "Likely your year" to a 2028 student and NOTHING to a 2029 one. After
    `refresh_grad_facts --commit` the posting's own words decide both."""
    o = _row(Firm.objects.create(slug="ow", name="Oliver Wyman"),
             title="Oliver Wyman - Summer Analyst 2027 - Data and Analytics",
             cohort="2027", class_year_derived="2028",
             raw={"detail_text": "Location: Toronto Office only Internship "
                                 "length : 8 weeks Eligibility : Class of 2028 "
                                 "grads only (bachelor’s & master’s); Oliver "
                                 "Wyman does not offer visa sponsorship",
                  "facts": {}})
    assert _verdict(o, 2028)["kind"] == "year_likely"
    assert _verdict(o, 2029) is None

    call_command("refresh_grad_facts", commit=True, stdout=StringIO())
    o.refresh_from_db()
    assert o.raw["facts"]["grad"]["years"] == ["2028"]
    assert _verdict(o, 2028)["kind"] == "year_ok"
    out = _verdict(o, 2029)
    assert out["kind"] == "year_out" and out["label"] == "For 2028 grads"


# ---------------------------------------------------------------------------
# G3 — two windows in one sentence are one union
# ---------------------------------------------------------------------------

def test_two_windows_in_one_sentence_are_their_union():
    """Jefferies 2027 Marketing Summer Analyst (found in research; the row is
    not on the board today): "Expected graduation between December 2027 –
    June 2028 and December 2028 – June 2029". Four dates, three classes."""
    got = extract_grad_years(
        "Expected graduation between December 2027 – June 2028 and December "
        "2028 – June 2029")
    assert got["years"] == ["2027", "2028", "2029"]
    assert got["value"] == "2027–2029"
    assert "open_high" not in got


@pytest.mark.parametrize("years, value, text", [
    # Nomura 24130
    (["2028", "2029"], "2028–2029", "Graduating May/June 2028 or May/June 2029 "
                                    "Applicants for this position"),
    # Belvedere Trading 8927/8928/8932
    (["2027", "2028"], "2027–2028", "Graduation date of December 2027/Spring 2028 "
                                    "Belvedere Trading is a leading proprietary"),
    # T. Rowe Price 6939 — "May/June" the old pattern could not cross.
    (["2027", "2029"], "2027–2029", "expected graduation date of December 2027 – "
                                    "May/June 2029 Major: Computer Science"),
    # Five Rings 3431
    (["2026", "2027"], "2026–2027", "About You Graduating in winter of 2026 or "
                                    "spring/summer of 2027 Quantitatively-focused"),
    # Wells Fargo 1406 — one year named twice is one year.
    (["2028"], "2028", "with an expected graduation between March 2028 – "
                       "August 2028 Summer Internship timeline"),
])
def test_every_year_a_statement_names_is_kept_once(years, value, text):
    got = extract_grad_years(text)
    assert got["years"] == years and got["value"] == value


def test_the_union_never_crosses_into_the_next_sentence():
    got = extract_grad_years(
        "Applicants should graduate in 2028. Our programme has run since "
        "2019 and 2020 saw its largest intake.")
    assert got["years"] == ["2028"]


@pytest.mark.django_db
def test_a_student_inside_the_second_window_is_eligible():
    fact = extract_grad_years(
        "Expected graduation between December 2027 – June 2028 and December "
        "2028 – June 2029")
    o = _row(Firm.objects.create(slug="jef", name="Jefferies"),
             raw={"facts": {"grad": fact}})
    assert _verdict(o, 2028)["kind"] == "year_ok"
    assert _verdict(o, 2029)["kind"] == "year_ok"
    assert _verdict(o, 2030)["kind"] == "year_out"


# ---------------------------------------------------------------------------
# refresh_grad_facts — carrying the extractor over stored rows
# ---------------------------------------------------------------------------

OPTIVER_TEXT = ("Regular social events. Who you are: Students graduating in "
                "2028 or later . Foundations in Machine Learning")
OLD_CLOSED = {"value": "2028", "years": ["2028"],
              "phrase": "Who you are: Students graduating in 2028 or later"}


def _run(**opts):
    out = StringIO()
    call_command("refresh_grad_facts", stdout=out, **opts)
    return out.getvalue()


@pytest.mark.django_db
def test_report_only_prints_the_opened_row_and_writes_nothing():
    o = _row(Firm.objects.create(slug="optiver", name="Optiver"),
             raw={"detail_text": OPTIVER_TEXT,
                  "facts": {"grad": OLD_CLOSED, "gpa": {"value": "3.5"}},
                  "facts_at": "2026-08-01T00:00:00"})
    out = _run()
    assert "[dry-run]" in out and "OPENED (1 rows)" in out
    assert f"#{o.id} Optiver" in out and "'2028' [2028] -> '2028+' [2028+]" in out
    assert "Nothing was written" in out
    o.refresh_from_db()
    assert o.raw["facts"]["grad"] == OLD_CLOSED


@pytest.mark.django_db
def test_commit_refreshes_grad_and_leaves_every_other_fact_alone():
    o = _row(Firm.objects.create(slug="optiver", name="Optiver"),
             raw={"detail_text": OPTIVER_TEXT,
                  "facts": {"grad": OLD_CLOSED, "gpa": {"value": "3.5"}},
                  "facts_at": "2026-08-01T00:00:00", "deadline": "2026-11-01"})
    out = _run(commit=True)
    assert "1 refreshed (0 new, 1 opened" in out
    o.refresh_from_db()
    grad = o.raw["facts"]["grad"]
    assert grad["open_high"] is True and grad["value"] == "2028+"
    assert grad["years"] == _open_years(2028)
    assert o.raw["facts"]["gpa"] == {"value": "3.5"}
    assert o.raw["facts_at"] == "2026-08-01T00:00:00"
    assert o.raw["deadline"] == "2026-11-01"
    # Idempotent: a second run finds nothing.
    assert "Nothing stale" in _run()


@pytest.mark.django_db
def test_every_change_class_is_reported_by_name():
    f = Firm.objects.create(slug="mix", name="Mix")
    new = _row(f, title="new", raw={
        "detail_text": "Eligibility : Class of 2028 grads only", "facts": {}})
    gone = _row(f, title="gone", raw={
        "detail_text": "Join our Graduate Programme in 2028.",
        "facts": {"grad": {"value": "2028", "years": ["2028"], "phrase": "x"}}})
    moved = _row(f, title="moved", raw={
        "detail_text": "Graduation date of December 2027/Spring 2028",
        "facts": {"grad": {"value": "2027", "years": ["2027"], "phrase": "x"}}})
    shape = _row(f, title="shape", raw={
        "detail_text": "candidates graduating in Spring 2028 or December 2027",
        "facts": {"grad": {"value": "2027–2028", "years": ["2028", "2027"],
                           "phrase": "x"}}})
    out = _run()
    assert f"NEW (1 rows)" in out and f"#{new.id}" in out
    assert "RETRACTED (1 rows)" in out and f"#{gone.id}" in out
    assert "BOUNDS (1 rows)" in out and f"#{moved.id}" in out
    assert "SHAPE (1 rows)" in out and f"#{shape.id}" not in out
    assert "4 would be refreshed (1 new, 0 opened, 1 bounds, 1 retracted, 1 shape-only)" in out

    _run(commit=True)
    gone.refresh_from_db()
    assert "grad" not in gone.raw["facts"]
    shape.refresh_from_db()
    assert shape.raw["facts"]["grad"]["years"] == ["2027", "2028"]


@pytest.mark.django_db
def test_rows_extract_facts_never_read_are_left_alone():
    """No `facts` key means extract_facts has not run here; inventing a
    partial dict would make its own "have I read this row" check lie."""
    f = Firm.objects.create(slug="raw", name="Raw")
    o = _row(f, title="unread", raw={"detail_text": OPTIVER_TEXT})
    out = _run(commit=True)
    assert "0 row(s) examined" in out
    o.refresh_from_db()
    assert "facts" not in o.raw


@pytest.mark.django_db
def test_closed_rows_are_refreshed_too():
    """`_eligibility` still reads a closed row's grad fact on My
    Applications, so a closed row's stale window is as visible as an open
    one's."""
    o = _row(Firm.objects.create(slug="c", name="C"), status="closed",
             raw={"detail_text": OPTIVER_TEXT, "facts": {"grad": OLD_CLOSED}})
    _run(commit=True)
    o.refresh_from_db()
    assert o.raw["facts"]["grad"]["open_high"] is True


def test_classify_change_names_the_five_classes():
    closed = {"value": "2028", "years": ["2028"], "phrase": "p"}
    opened = {"value": "2028+", "years": _open_years(2028), "open_high": True,
              "phrase": "p"}
    assert classify_change(None, None) is None
    assert classify_change(closed, dict(closed)) is None
    assert classify_change(None, closed) == "NEW"
    assert classify_change(closed, None) == "RETRACTED"
    assert classify_change(closed, opened) == "OPENED"
    assert classify_change(closed, {**closed, "years": ["2028", "2029"],
                                    "value": "2028–2029"}) == "BOUNDS"
    assert classify_change(closed, {**closed, "phrase": "q"}) == "SHAPE"
    assert refreshed_grad_fact({"detail_text": "   "}) is None
    assert refreshed_grad_fact(None) is None


def test_an_open_window_chip_reads_plus_not_the_horizon():
    """"2028 or later" is stored as 2028..GRAD_YEAR_MAX so the years reach the
    scorer at all (`Candidate` has no `open_high`). The chip must not print that
    bookkeeping as the firm's words: "For 2028+ grads", never "2028–2035"."""
    from directory.facts import GRAD_YEAR_MAX
    from directory.recommend import _class_fit, Candidate, Profile
    profile = Profile(class_year=2029)
    c = Candidate(id=1, firm_id=1, firm_name="X", firm_slug="x", title="Summer Analyst",
                  url="https://x.test/1", bucket="internship", cohort="2027", region="us",
                  grad_years=tuple(str(y) for y in range(2028, GRAD_YEAR_MAX + 1)))
    _points, reasons = _class_fit(profile, c)
    texts = [r.text for r in reasons]
    assert any("2028+" in t for t in texts), texts
    assert not any(str(GRAD_YEAR_MAX) in t for t in texts), texts
