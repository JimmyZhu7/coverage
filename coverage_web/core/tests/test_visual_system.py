"""One type scale, one page header, one nav, one meaning per italic.

Each test here pins a rule that had drifted into many local answers. They are
grouped in one module because they are one argument: a design system is not a
document, it is the set of things a change cannot quietly undo.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "coverage_web"
CSS = WEB / "static" / "css" / "coverage.css"
TEMPLATES = WEB / "templates"

DISPLAY_TOKENS = {"--fs-display-1", "--fs-display-2", "--fs-display-3", "--fs-figure"}

# HTML email cannot read a custom property: Outlook, Gmail's web client and
# every gateway that rewrites <style> need the literal in the `style`
# attribute. Both hits are the Coverage wordmark, not a display heading.
EMAIL_LITERAL_PX = {
    "templates/accounts/emails/trial_ended.html",
    "templates/crm/emails/weekly_digest.html",
}


def _stylesheets() -> dict[str, str]:
    """The shared file plus every template that carries a <style> block."""
    out = {"static/css/coverage.css": CSS.read_text(encoding="utf-8")}
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "<style" in text:
            out[str(path.relative_to(WEB))] = text
    return out


# ---------------------------------------------------------------------------
# WS-UI-05 — one display type scale
# ---------------------------------------------------------------------------

def test_the_display_scale_has_exactly_four_tokens():
    css = CSS.read_text(encoding="utf-8")
    declared = set(re.findall(r"(--fs-display-\d|--fs-figure)\s*:", css))
    assert declared == DISPLAY_TOKENS
    # The three tokens the scale replaced are gone, not left as aliases: two
    # names for 22px is the same defect one layer down.
    for retired in ("--fs-xl:", "--fs-xxl:", "--fs-hero:"):
        assert retired not in css


def test_no_display_size_is_written_as_a_literal():
    """The audit's own grep, as a test.

    `font-size: 20px` through `46px` may appear only where a token is being
    DEFINED. Everywhere else the size has a name, so a heading added next
    month picks one of four rather than inventing a fifteenth.
    """
    offenders = []
    for name, text in _stylesheets().items():
        if name in EMAIL_LITERAL_PX:
            continue
        for line in text.splitlines():
            if re.search(r"font-size: *(2[0-9]|3[0-9]|4[0-6])px", line):
                if re.search(r"--fs-[\w-]+: *\d", line):
                    continue  # a token definition
                offenders.append(f"{name}: {line.strip()}")
    assert not offenders, offenders


def test_every_title_and_large_figure_reads_a_display_token():
    """The named elements from the audit, plus every heading rule.

    Checked by parsing the declaration rather than the browser, because the
    browser only tells you about the page you happened to load and there are
    thirteen.
    """
    named = (".dash-num", ".pace-done", ".set-row-value", ".ob-pv-num",
             ".pagehead-title", ".auth-title", ".plan-name", ".kin-hero-title")
    rule = re.compile(r"(?P<sel>[^{}]+)\{(?P<body>[^{}]*)\}")
    size = re.compile(r"font(?:-size)?: *([^;]+)")
    bad = []
    for name, text in _stylesheets().items():
        if name in EMAIL_LITERAL_PX:
            continue
        for m in rule.finditer(re.sub(r"/\*.*?\*/", "", text, flags=re.S)):
            sel = m.group("sel").strip()
            if sel.startswith("@") or "<" in sel:
                continue
            is_heading = re.search(r"(^|[\s,>])h[1-3]\b", sel)
            is_named = any(n in sel for n in named)
            if not (is_heading or is_named):
                continue
            decl = size.search(m.group("body"))
            if not decl:
                continue
            value = decl.group(1)
            px = [int(p) for p in re.findall(r"(\d+)px", value)]
            if not any(p > 17 for p in px):
                # Either it already reads a token, or it is at or below the
                # body scale, which is not display type.
                if not px and "var(--fs-" not in value:
                    continue
            if px and any(p > 17 for p in px):
                bad.append(f"{name}: {sel} -> {value.strip()}")
    assert not bad, bad


# ---------------------------------------------------------------------------
# WS-UI-06 — one page-header system
# ---------------------------------------------------------------------------

def test_the_july_page_header_is_gone():
    """`.page-head` and `.pagehead` were two answers to "where does a page
    start", and the account pages wore one while the Settings page they are
    reached from wore the other."""
    hits = []
    for path in list(TEMPLATES.rglob("*.html")) + [CSS]:
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r"page-head\b", line):
                hits.append(f"{path.relative_to(WEB)}:{i}")
    assert not hits, hits


def test_the_compact_modifier_is_a_modifier_not_a_component():
    css = CSS.read_text(encoding="utf-8")
    assert ".pagehead--compact" in css
    # It may only restate what the base component already sets. A rule that
    # introduced a new property here would be the second component again.
    body = "\n".join(
        line.split("{", 1)[1] for line in css.splitlines()
        if line.startswith(".pagehead--compact") and "{" in line
    )
    props = set(re.findall(r"([a-z-]+):", body))
    assert props <= {"padding-bottom", "margin-bottom", "font-size", "content"}


@pytest.mark.django_db
def test_the_converted_pages_render_the_shared_header():
    user = get_user_model().objects.create_user(
        email="hdr@example.com", password="x" * 14
    )
    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    c = Client()
    c.force_login(user)
    for url in ("/welcome/export/", "/welcome/import/", "/welcome/delete/"):
        body = c.get(url).content.decode()
        assert "pagehead pagehead--compact" in body, url
        assert "pagehead-title" in body, url


# ---------------------------------------------------------------------------
# WS-UI-07 — the token gaps
# ---------------------------------------------------------------------------

def test_no_rule_falls_back_to_a_token_that_does_not_exist():
    """`var(--surface-2, #eee)` reads as a graceful degradation and is a
    token gap: the fallback fires 100% of the time and no grep for the
    missing token finds anything."""
    # A property is "set somewhere" if its name appears anywhere in the
    # source OUTSIDE a `var()` reference: the :root block, an inline
    # `style="--i:3"`, a JS `setProperty`. Scoped properties with a fallback
    # are legitimate defaults; the defect is a name that exists NOWHERE but
    # inside the reference that despairs of it.
    sheets = _stylesheets()
    corpus = "\n".join(sheets.values())
    for path in list(TEMPLATES.rglob("*.html")) + list(WEB.rglob("*.py")):
        if "tests" in path.parts:
            continue
        corpus += "\n" + path.read_text(encoding="utf-8", errors="ignore")
    elsewhere = re.sub(r"var\(\s*--[\w-]+", "", corpus)

    bad = []
    for name, text in sheets.items():
        for m in re.finditer(r"var\(\s*(--[\w-]+)\s*,", text):
            token = m.group(1)
            if not re.search(re.escape(token) + r"(?![\w-])", elsewhere):
                bad.append(f"{name}: {m.group(0)} is set nowhere")
    assert not bad, bad


def test_every_layout_measure_has_a_consumer():
    css = CSS.read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    others = "\n".join(
        t for n, t in _stylesheets().items() if n != "static/css/coverage.css"
    )
    for token in ("--page-w", "--page-w-narrow", "--page-w-wide", "--page-w-full"):
        assert re.search(re.escape(token) + r":", body), token
        used = re.findall(r"var\(" + re.escape(token) + r"\)", body + others)
        assert used, f"{token} is defined and nothing reads it"


# ---------------------------------------------------------------------------
# WS-UI-08 — the phone nav
# ---------------------------------------------------------------------------

def test_below_480_the_nav_wraps_instead_of_scrolling():
    """A 606px row of pills inside a 174px slot is a filing cabinet, not a
    navigation. Two rows of three, 44px each, and no hamburger."""
    css = CSS.read_text(encoding="utf-8")
    block = re.search(
        r"@media \(max-width: 479px\) \{(.*?)\n\}\n", css, re.S
    )
    assert block, "the phone-nav breakpoint is gone"
    rules = block.group(1)
    assert "flex-wrap: wrap" in rules
    assert "overflow-x: visible" in rules
    assert "mask-image: none" in rules
    assert "min-height: 44px" in rules
    # `height`, ever, is the bug this codebase has shipped twice: a
    # letterspaced OPPORTUNITIES wraps on the narrowest phones.
    assert not re.search(r"[^-]height: *44px", rules)


def test_the_nav_still_has_exactly_six_destinations_and_no_menu_control():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    nav = base[base.index('<nav class="site-nav"'):]
    nav = nav[: nav.index("</nav>")]
    assert nav.count("<a href=") == 6
    assert "hamburger" not in base.lower()


# ---------------------------------------------------------------------------
# WS-UI-11 — the silent "no date" dash
# ---------------------------------------------------------------------------

def test_the_undated_cell_carries_no_em_dash_and_speaks():
    card = TEMPLATES / "directory" / "_rolecard.html"
    text = card.read_text(encoding="utf-8")
    assert "—" not in text and "&mdash;" not in text
    assert '<span class="rr-due-n rr-due-none"><span class="vh">No date posted</span></span>' in text
    # The mark itself is drawn, so it is never announced.
    styles = (TEMPLATES / "directory" / "_styles.html").read_text(encoding="utf-8")
    assert ".rr-due-none::before" in styles


@pytest.mark.django_db
def test_an_undated_row_renders_the_spoken_cell(client):
    from directory.models import Firm, Opportunity

    firm = Firm.objects.create(slug="evercore", name="Evercore")
    Opportunity.objects.create(
        firm=firm, url="https://x/undated", title="Summer Analyst",
        bucket="internship", status="open", deadline=None, region="",
    )
    body = client.get(reverse("opportunities")).content.decode()
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    assert "rr-due-none" in body
    assert '<span class="vh">No date posted</span>' in body
    assert "&mdash;" not in body


# ---------------------------------------------------------------------------
# WS-UI-12 — motion on the two state changes that still flip
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_every_firm_card_carries_a_unique_view_transition_name():
    from crm.models import UserFirm
    from directory.models import Firm

    user = get_user_model().objects.create_user(
        email="board@example.com", password="x" * 14
    )
    user.onboarded_at = timezone.now()
    user.save(update_fields=["onboarded_at"])
    for i, slug in enumerate(("evercore", "jefferies", "moelis")):
        firm = Firm.objects.create(slug=slug, name=slug.title())
        UserFirm.all_objects.create(user=user, firm=firm, tier=1 + i % 3)

    c = Client()
    c.force_login(user)
    body = c.get("/app/contacts/").content.decode()
    names = re.findall(r"view-transition-name: (firm-card-\d+)", body)
    assert len(names) >= 3
    assert len(names) == len(set(names))


def test_a_retier_is_animated_and_reduced_motion_is_not():
    page = (TEMPLATES / "crm" / "contact_list.html").read_text(encoding="utf-8")
    assert "document.startViewTransition" in page
    # The preference is honoured by NOT STARTING one, not by starting one at
    # zero duration: a flattened transition still leaves live animations on
    # the document for a reader who asked for none.
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in page
    assert "if (document.startViewTransition && !still)" in page


def test_only_the_new_history_row_animates_on_a_logged_touch():
    live = (TEMPLATES / "crm" / "_contact_live.html").read_text(encoding="utf-8")
    assert '{% if moved and not forloop.first %} is-settled{% endif %}' in live
    styles = (TEMPLATES / "crm" / "_styles.html").read_text(encoding="utf-8")
    assert ".cd-log-row.is-settled { animation: none; }" in styles


# ---------------------------------------------------------------------------
# WS-UI-13 — the one-off italic
# ---------------------------------------------------------------------------

def test_the_engines_two_explanations_agree_about_italic():
    """`.reasoning` was the site's only italic paragraph. It is not italic
    now, and neither is Today's `.act-reason`, which is the same sentence
    from the same engine. Spreading it instead would have put two different
    meanings of italic on one act card, since `.opp-firm.is-missing` already
    uses it to mean "this fact is absent"."""
    css = CSS.read_text(encoding="utf-8")
    reasoning = re.search(r"^\.reasoning \{([^}]*)\}", css, re.M)
    assert reasoning and "italic" not in reasoning.group(1)
    crm = (TEMPLATES / "crm" / "_styles.html").read_text(encoding="utf-8")
    act = re.search(r"\.act-reason \{([^}]*)\}", crm, re.S)
    assert act and "italic" not in act.group(1)


# ---------------------------------------------------------------------------
# The datum the whole system rests on
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_no_app_page_lost_its_shared_header():
    """Regression guard for the header conversion: the six nav pages must
    still render `.pagehead`, which is what puts their first line of content
    on one shared y datum."""
    user = get_user_model().objects.create_user(
        email="datum@example.com", password="x" * 14
    )
    user.onboarded_at = timezone.now() - timedelta(days=1)
    user.save(update_fields=["onboarded_at"])
    c = Client()
    c.force_login(user)
    for url in ("/app/", "/opportunities/", "/app/contacts/", "/app/calendar/",
                "/welcome/settings/"):
        body = c.get(url).content.decode()
        assert 'class="pagehead' in body, url
        assert "pagehead--compact" not in body, url
