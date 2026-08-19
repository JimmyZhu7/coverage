"""The onboarding wizard's live preview panel.

The panel's whole claim is that every number on it is real. So the tests
that matter most here are not "does it render" — they are "does the number
it renders equal the number the database gives when you ask by hand", and
"when the answer is nothing, does it say nothing rather than showing the
previous answer's rows".

Fixtures build a small, fully-known directory so every expected count in
this file is derivable by reading the fixture rather than by trusting the
code under test.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import Contact, UserFirm
from directory.models import Firm, Opportunity

User = get_user_model()

PREVIEW = "accounts:onboarding_preview"
WIZARD = "accounts:onboarding"


@pytest.fixture
def user(db):
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="x")


@pytest.fixture
def world(db):
    """A directory small enough to count by hand.

    us/ib   : 3 open campus roles at Goldman (1 says it sponsors, 1 says it
              does not, 1 is silent)
    hk/ib   : 1 open campus role at JPMorgan
    us/cons : 1 open campus role at Bain
    plus one CLOSED us/ib role and one EXPERIENCED us/ib role, both of which
    every count in this module must exclude.
    """
    gs = Firm.objects.create(slug="gs", name="Goldman Sachs",
                             regions=["us"], tracks=["ib"])
    jpm = Firm.objects.create(slug="jpm", name="JPMorgan",
                              regions=["us", "hk"], tracks=["ib"])
    bain = Firm.objects.create(slug="bain", name="Bain",
                               regions=["us"], tracks=["consulting"])

    def opp(firm, title, **kw):
        kw.setdefault("bucket", "internship")
        kw.setdefault("status", "open")
        kw.setdefault("region", "us")
        kw.setdefault("sponsorship", "unknown")
        return Opportunity.objects.create(
            firm=firm, title=title, url=f"https://x.test/{firm.slug}/{title}", **kw)

    opp(gs, "SA Sponsors", sponsorship="yes")
    opp(gs, "SA No Visa", sponsorship="no")
    opp(gs, "SA Silent")
    opp(jpm, "HK Summer", region="hk")
    opp(bain, "Consulting SA")
    # Must never be counted: closed, and out-of-scope bucket.
    opp(gs, "Closed One", status="closed")
    opp(gs, "Experienced One", bucket="other")
    return {"gs": gs, "jpm": jpm, "bain": bain}


def _get(client, step, **params):
    qs = "&".join(
        f"{k}={v}" for k, vals in params.items()
        for v in (vals if isinstance(vals, list) else [vals])
    )
    url = f"{reverse(PREVIEW)}?step={step}"
    return client.get(f"{url}&{qs}" if qs else url)


def _num(body: str) -> int:
    """The panel's one headline number."""
    m = re.search(r'ob-pv-num">(\d+)<', body)
    assert m, "panel rendered no headline number"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# The honesty contract
# ---------------------------------------------------------------------------
def test_profile_count_equals_a_hand_written_query(client, user, world):
    """THE test. The panel's count and a direct ORM count of the same
    question must be the same integer — not close, the same."""
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    client.force_login(user)

    hand = Opportunity.objects.filter(
        status="open", bucket="internship", region="us",
        firm__tracks__overlap=["ib"],
    ).count()
    assert hand == 3  # derivable from the fixture by reading it

    body = _get(client, "profile").content.decode()
    assert _num(body) == hand


def test_profile_count_tracks_the_live_form_not_the_saved_profile(client, user, world):
    """Ticking a chip changes the number before anything is saved."""
    client.force_login(user)
    assert user.regions in (None, [])

    all_open = _num(_get(client, "profile").content.decode())
    assert all_open == 5  # every open campus row in the fixture

    narrowed = _num(_get(client, "profile", live="1", regions="hk").content.decode())
    assert narrowed == 1  # only JPMorgan's HK role

    # And the saved profile is untouched — this endpoint writes nothing.
    user.refresh_from_db()
    assert user.regions in (None, [])


def test_a_selection_that_matches_nothing_says_so(client, user, world):
    """No stale rows, no invented rows, no last-good-answer. The fixture has
    no Japanese roles at all, so this must be the empty state."""
    client.force_login(user)
    body = _get(client, "profile", live="1", regions="jp").content.decode()
    assert _num(body) == 0
    assert "No live roles match yet" in body
    # None of the firms that WOULD have matched a wider selection leak in.
    assert "Goldman" not in body
    assert "JPMorgan" not in body


def test_the_footer_names_only_the_filters_that_actually_ran(client, user, world):
    """A true count under a false caption is still a lie. The footer is built
    from the applied filters, so it can never name one that did not run."""
    client.force_login(user)

    none_yet = _get(client, "profile", live="1").content.decode()
    assert "Every live campus role" in none_yet
    assert "Narrowed from" not in none_yet

    region_only = _get(client, "profile", live="1", regions="us").content.decode()
    assert "Narrowed from 5 live roles by region." in region_only

    both = _get(client, "profile", live="1", regions="us",
                tracks="ib").content.decode()
    assert "Narrowed from 5 live roles by region and track." in both

    all_three = _get(client, "profile", live="1", regions="us", tracks="ib",
                     class_year="2028").content.decode()
    assert "by region, track and class year." in all_three


def test_sample_rows_are_real_postings(client, user, world):
    client.force_login(user)
    body = _get(client, "profile", live="1", regions="hk").content.decode()
    # The one HK row in the fixture, by name, from the database.
    assert "JPMorgan" in body
    assert "HK Summer" in body


def test_closed_and_non_campus_rows_are_never_counted(client, user, world):
    """The panel promises the Opportunities feed's population. A closed
    posting and an experienced-hire posting are in neither."""
    client.force_login(user)
    assert _num(_get(client, "profile", live="1").content.decode()) == 5
    body = _get(client, "profile", live="1").content.decode()
    assert "Closed One" not in body
    assert "Experienced One" not in body


# ---------------------------------------------------------------------------
# Step 2 — sponsorship
# ---------------------------------------------------------------------------
def test_work_step_counts_the_sponsorship_answers_honestly(client, user, world):
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    client.force_login(user)

    body = _get(client, "work_auth", live="1", work_auth_us="sponsorship").content.decode()
    # 3 us/ib roles: one yes, one no, one silent. Neither firm in the fixture
    # carries a `sponsors` policy, so the new "firm policy known" bar is 0.
    assert _num(body) == 2  # "2 of 3 answer the visa question"
    assert "of 3 answer the visa question" in body
    nums = [int(n) for n in re.findall(r'ob-pv-bar-n">(\d+)<', body)]
    assert nums == [1, 1, 0, 1]  # posting-yes / posting-no / firm-known / not-stated
    # The one blocking verdict: a us role saying "no" while the student says
    # they need sponsorship in the us.
    assert "<strong>1</strong> of these are ruled out" in body


def test_work_step_counts_a_firm_policy_answer_in_its_own_bar(db):
    """docs/founder-decisions-2026-08-20.md, Decision 3: a firm's per-region
    policy is real information, distinct from a silent posting AND from a
    posting's own stated answer — it gets a fourth bar, not folded into
    either."""
    from accounts.onboarding_preview import work_preview

    firm = Firm.objects.create(slug="db2", name="Deutsche Bank Two",
                               regions=["us"], tracks=["ib"],
                               sponsors={"us": False})
    Opportunity.objects.create(
        firm=firm, title="Silent Posting, Firm Says No", bucket="internship",
        status="open", region="us", sponsorship="unknown",
        url="https://x.test/db2/1")

    answers = {"regions": ["us"], "tracks": [], "class_year": None,
               "work_auth": {}, "firm_ids": [], "live": True}
    preview = work_preview(None, answers)
    assert preview["yes"] == 0
    assert preview["no"] == 0
    assert preview["firm_known"] == 1
    assert preview["silent"] == 0
    assert preview["answered"] == 1  # firm policy counts as answered


def test_work_step_reads_the_saved_profile_not_an_empty_one(client, user, world):
    """REGRESSION. `live=1` used to be read as a global flag, so on this step
    — whose form carries no region or track inputs — the profile filters came
    back empty and the panel counted the whole directory while its footer
    said "the roles matching your profile". Correct arithmetic, false
    sentence. `live` is per-step now."""
    user.regions = ["hk"]
    user.save()
    client.force_login(user)

    body = _get(client, "work_auth", live="1",
                work_auth_hk="sponsorship").content.decode()
    # Only the single HK role, not all five.
    assert "of 1 answer the visa question" in body


def test_work_step_reports_nothing_blocked_rather_than_a_zero(client, user, world):
    """"0 ruled out" reads as a measurement; the sentence is the honest form
    and it is only drawn when the student has actually claimed a region."""
    user.regions = ["hk"]
    user.save()
    client.force_login(user)
    body = _get(client, "work_auth", live="1",
                work_auth_hk="sponsorship").content.decode()
    assert "Nothing is ruled out on visa." in body

    # Nothing claimed at all: no verdict line either way.
    silent = _get(client, "work_auth", live="1").content.decode()
    assert "ruled out" not in silent


# ---------------------------------------------------------------------------
# Step 3 — firms
# ---------------------------------------------------------------------------
def test_firms_step_counts_live_roles_per_picked_firm(client, user, world):
    client.force_login(user)
    body = _get(client, "firms", live="1",
                firms=[str(world["gs"].id), str(world["bain"].id)]).content.decode()
    assert "Goldman Sachs" in body and "Bain" in body
    # gs has 3 open campus roles, bain 1 — highest first.
    assert [int(n) for n in re.findall(r'ob-pv-count[^"]*">(\d+)<', body)] == [3, 1]
    assert _num(body) == 4


def test_firms_step_empty_until_something_is_picked(client, user, world):
    client.force_login(user)
    body = _get(client, "firms", live="1").content.decode()
    assert "No firms picked yet" in body
    assert "Goldman" not in body


def test_a_firm_with_no_live_roles_still_shows_its_real_zero(client, user, world):
    quiet = Firm.objects.create(slug="quiet", name="Quiet Capital", regions=["us"])
    client.force_login(user)
    body = _get(client, "firms", live="1", firms=str(quiet.id)).content.decode()
    assert "Quiet Capital" in body
    assert re.search(r'ob-pv-count is-zero">0<', body)


# ---------------------------------------------------------------------------
# Step 4 — the board
# ---------------------------------------------------------------------------
def test_import_step_shows_the_real_board_state(client, user, world):
    client.force_login(user)
    body = _get(client, "import").content.decode()
    assert _num(body) == 0
    assert "Nothing on it yet" in body

    UserFirm.all_objects.create(user=user, firm=world["gs"], tier=2, status="target")
    Contact.all_objects.create(user=user, name="Jane Banker")
    body = _get(client, "import").content.decode()
    assert _num(body) == 1  # one contact
    assert "1 target firm" in body


# ---------------------------------------------------------------------------
# Tenancy, auth, and the read-only contract
# ---------------------------------------------------------------------------
def test_preview_requires_login(client):
    resp = client.get(f"{reverse(PREVIEW)}?step=profile")
    assert resp.status_code == 302


def test_one_students_board_never_appears_on_anothers(client, user, other_user, world):
    UserFirm.all_objects.create(user=other_user, firm=world["gs"], tier=1,
                                status="target")
    Contact.all_objects.create(user=other_user, name="Someone Else")
    client.force_login(user)
    body = _get(client, "import").content.decode()
    assert _num(body) == 0
    assert "Nothing on it yet" in body

    firms_body = _get(client, "firms").content.decode()
    assert "Goldman" not in firms_body


def test_the_preview_writes_nothing(client, user, world):
    """It is a GET and it must stay one. A student clicking through the
    wizard and abandoning it must leave the same rows behind as one who
    never opened it."""
    client.force_login(user)
    before = (user.regions, user.tracks, user.class_year, user.work_authorization)
    for step in ("profile", "work_auth", "firms", "import"):
        _get(client, step, live="1", regions="us", tracks="ib", class_year="2029")
    assert UserFirm.objects.for_user(user).count() == 0
    user.refresh_from_db()
    assert (user.regions, user.tracks, user.class_year, user.work_authorization) == before

    # And POST is not an entrance.
    assert client.post(reverse(PREVIEW), {"step": "profile"}).status_code == 405


def test_an_unknown_step_falls_back_instead_of_erroring(client, user, world):
    """Same posture as the step machine's own `if step not in
    ONBOARDING_STEPS` — a stale bookmark gets the first panel, not a 500."""
    client.force_login(user)
    resp = _get(client, "assets", live="1")
    assert resp.status_code == 200
    assert "match" in resp.content.decode()


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("step,budget", [
    # Two of every budget below are the session + user lookups every
    # authenticated request pays; the rest is this panel's own work. The
    # ceiling exists so a future edit cannot quietly reintroduce the feed
    # pipeline (or an N+1 over the picked firms) on a control that re-runs
    # every time a chip is toggled.
    ("profile", 5),
    # work_auth's budget is 4, not 3: `firm_policy_map()` (see
    # directory/sponsorship.py) adds one small, bounded scan of firms
    # carrying policy data (58 on live data) so the panel can answer with
    # the SAME firm-fallback rule the feed filter and `_eligibility` use —
    # a second query, not an N+1, so still one flat cost regardless of how
    # many roles are in scope.
    ("work_auth", 4),
    ("firms", 3),
    ("import", 4),
])
def test_the_panel_stays_cheap(client, user, world, django_assert_max_num_queries,
                               step, budget):
    UserFirm.all_objects.create(user=user, firm=world["gs"], tier=2, status="target")
    UserFirm.all_objects.create(user=user, firm=world["jpm"], tier=2, status="target")
    UserFirm.all_objects.create(user=user, firm=world["bain"], tier=2, status="target")
    client.force_login(user)
    with django_assert_max_num_queries(budget):
        _get(client, step, live="1", regions="us", tracks="ib")


# ---------------------------------------------------------------------------
# Progressive enhancement
# ---------------------------------------------------------------------------
def test_the_wizard_renders_the_panel_server_side_on_every_step(client, user, world):
    """With JS off the panel is still there, still correct, and the form
    beneath it still posts. htmx only ever replaces the same partial."""
    user.regions = ["us"]
    user.save()
    client.force_login(user)
    for step in ("profile", "work_auth", "firms", "import"):
        body = client.get(f"{reverse(WIZARD)}?step={step}").content.decode()
        assert 'id="ob-preview"' in body
        assert "ob-pv-num" in body, f"{step} rendered an empty panel"


def test_the_panel_never_fires_on_a_keystroke(client, user, world):
    """The school field runs its OWN htmx request on `keyup` for the
    university datalist. The preview must not ride along with it: it triggers
    on `change`, debounced, or a student typing a university name would run a
    feed query per letter."""
    client.force_login(user)
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()
    trigger = re.search(r'hx-trigger="(change from:#ob-form[^"]*)"', body)
    assert trigger, "the preview lost its change trigger"
    assert "keyup" not in trigger.group(1)
    assert "delay:250ms" in trigger.group(1)
    # The avatar file input must never be serialized into a preview GET.
    assert 'hx-params="regions,tracks,class_year"' in body


def test_the_step_machine_is_untouched(client, user, world):
    """This pass was a redesign plus a read-only endpoint. Continue and Skip
    still advance, and every field on every step is still optional."""
    client.force_login(user)
    resp = client.post(f"{reverse(WIZARD)}?step=profile", {"step": "profile"})
    assert resp.status_code == 302 and "step=work_auth" in resp["Location"]
    resp = client.post(f"{reverse(WIZARD)}?step=work_auth", {"step": "work_auth"})
    assert resp.status_code == 302 and "step=firms" in resp["Location"]
    resp = client.post(f"{reverse(WIZARD)}?step=firms", {"step": "firms"})
    assert resp.status_code == 302 and "step=import" in resp["Location"]
    resp = client.post(f"{reverse(WIZARD)}?step=import", {"step": "import"})
    assert resp.status_code == 302 and resp["Location"] == "/app/"


# ---------------------------------------------------------------------------
# The wizard's own stylesheet
# ---------------------------------------------------------------------------
def _wizard_style(client) -> str:
    """The contents of the `<style>` block _welcome_head.html injects."""
    body = client.get(f"{reverse(WIZARD)}?step=profile").content.decode()
    blocks = re.findall(r"<style>(.*?)</style>", body, re.S)
    assert blocks, "the wizard stopped shipping its stylesheet"
    return max(blocks, key=len)


def test_the_wizard_stylesheet_has_no_unterminated_comment(client, user):
    """A stray `*/` silently kills every rule after it.

    This is not hypothetical. The sticky offset below was edited, the edit
    left a second `*/` with no `/*` opening it, and `.ob-side` stopped
    matching entirely — `position` computed back to `static` and the preview
    panel scrolled away with the form. Nothing failed: not the template, not
    `manage.py check`, not any test, because a browser skipping a malformed
    rule is legal CSS and a Django template does not read what it inlines.

    A comment scan is the cheapest thing that would have caught it, and it
    catches the general case rather than that one selector.
    """
    client.force_login(user)
    css = _wizard_style(client)
    depth = 0
    for token in re.finditer(r"/\*|\*/", css):
        depth += 1 if token.group() == "/*" else -1
        assert depth >= 0, (
            f"a `*/` at offset {token.start()} closes a comment that was "
            "never opened — every rule after it is dead"
        )
    assert depth == 0, "an unterminated `/*` swallows the rest of the stylesheet"


def test_the_preview_panel_sticks_below_the_masthead(client, user):
    """`.site-header` is itself sticky at `top: 0` (coverage.css §3), so the
    panel has to offset by the masthead's real height or it pins underneath
    it — which it did, at a flat 56px against a 113px masthead, hiding the
    panel's eyebrow and the first digit of its count at every scroll
    position.

    The height is measured at runtime and republished as `--ob-mast`; the
    `var()` fallback is what a JS-off browser gets, so it has to be a real
    number rather than 0.
    """
    client.force_login(user)
    css = _wizard_style(client)
    offset = re.search(r"\.ob-side\s*\{[^}]*top:\s*calc\(([^;]+)\);", css)
    assert offset, ".ob-side lost its sticky offset"
    expr = offset.group(1)
    assert "--ob-mast" in expr, "the offset stopped tracking the masthead"
    fallback = re.search(r"--ob-mast,\s*(\d+)px", expr)
    assert fallback and int(fallback.group(1)) >= 100, (
        "the JS-off fallback has to clear the masthead on its own"
    )
