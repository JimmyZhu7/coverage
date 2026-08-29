"""Accessibility contracts that a page must not regress.

Measured, not assumed: an audit found the monogram tiles at 4.11:1 against a
4.5 requirement, no skip link on a page carrying ~3,000 focusable elements,
and a heading outline that jumped 1 to 3. Everything else it flagged turned
out to be the probe's fault — programmatic .focus() never triggers
:focus-visible, so "no focus ring" was a measurement artifact over a global
rule that covers every control.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _page(client, url="/opportunities/"):
    return client.get(url).content.decode()


def test_the_skip_link_is_the_first_thing_in_the_tab_order(client):
    body = _page(client)
    head = body[body.index("<body>"):body.index("<body>") + 600]
    assert 'class="skip-link"' in head
    assert head.index("skip-link") < (head.index("wordmark") if "wordmark" in head else 10**6)
    assert 'id="main" tabindex="-1"' in body, "the target must be able to take focus"


def test_the_heading_outline_never_skips_a_level(client):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(firm=firm, title="Summer Analyst", bucket="internship",
                               status="open", url="https://gs.com/sa")
    body = re.sub(r"<style.*?</style>", "", _page(client), flags=re.S)
    levels = [int(m) for m in re.findall(r"<h([1-4])\b", body)]
    assert levels, "the page has headings at all"
    jumps = [f"{a}->{b}" for a, b in zip(levels, levels[1:]) if b > a + 1]
    assert jumps == [], f"heading level skipped: {jumps}"


def test_every_firm_column_names_itself_as_a_section(client):
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(firm=firm, title="Analyst", bucket="internship",
                               status="open", url="https://ms.com/a")
    body = _page(client)
    assert '<h2 class="firmcol-h"' in body


def test_the_monogram_keeps_its_contrast_headroom(client):
    """The tile is hsl(hue 52% 90%) and the glyph hsl(hue 55% L). At L=31%
    the pair measured 4.11:1 on some hues, under the 4.5 that text needs;
    24% clears it across the wheel. A future tweak that raises L again should
    fail here rather than on a user's screen."""
    root = pathlib.Path(__file__).resolve().parents[2]
    css = (root / "templates" / "directory" / "_styles.html").read_text()
    m = re.search(r"\.firmcol-logo \{.*?color: hsl\(var\(--hue, 210\) 55% (\d+)%\)", css, re.S)
    assert m, "the monogram rule moved — re-measure before changing it"
    assert int(m.group(1)) <= 24, "lightness above 24% drops the glyph under 4.5:1"


# ---------------------------------------------------------------------------
# DARK MODE. One token block, not fifteen — and the pairings that flip.
# ---------------------------------------------------------------------------

def test_both_themes_are_defined_and_system_preference_leads():
    css = (pathlib.Path(__file__).resolve().parents[2]
           / "static" / "css" / "coverage.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css, "the OS decides by default"
    assert ':root:not([data-theme="light"])' in css, "an explicit light choice wins"
    assert ':root[data-theme="dark"]' in css, "an explicit dark choice wins"


def test_text_on_an_accent_fill_flips_with_the_palette():
    """White is right over the light theme's deep navy and measures 2.53:1
    over dark mode's light blue. Every such pairing goes through
    --on-accent, so a future `color: #fff` on an accent fill is a
    regression this catches."""
    css = (pathlib.Path(__file__).resolve().parents[2]
           / "static" / "css" / "coverage.css").read_text()
    for rule in (".btn-primary {", ".site-nav a.active {"):
        i = css.index(rule)
        block = css[i:css.index("}", i)]
        assert "var(--on-accent)" in block, f"{rule} must not hardcode its text colour"
    assert css.count("--on-accent:") >= 2, "the token is re-stated per theme"


def test_the_theme_is_applied_before_the_stylesheet_loads(client):
    """A deferred script runs after first paint — which is exactly the frame
    where the wrong palette would flash."""
    body = client.get("/opportunities/").content.decode()
    head = body[: body.index("</head>")]
    assert "coverage-theme" in head
    assert head.index("coverage-theme") < head.index("coverage.css")


# ---------------------------------------------------------------------------
# The token contract. Every colour in the product routes through the variables
# in coverage.css, which means one bad token edit can fail contrast on every
# page at once — and a palette change (the ledger-paper identity shift did
# exactly this to every base token) is precisely when it happens. So the
# tokens themselves are measured, in both themes, from the shipped file.


def _css_tokens():
    """Both themes' token maps, parsed from coverage.css itself."""
    css = (pathlib.Path(__file__).resolve().parents[2]
           / "static" / "css" / "coverage.css").read_text()
    # Light: the first :root block. Dark: the explicit [data-theme="dark"]
    # block (identical to the media-query one by construction; asserted below).
    blocks = re.findall(r'(:root(?:\[data-theme="dark"\])?)\s*{(.*?)\n}', css, re.S)
    themes = {}
    for sel, body in blocks:
        name = "dark" if "dark" in sel else "light"
        if name in themes:
            continue  # first :root wins; media-query dark parsed separately below
        themes[name] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", body))
    # The media-query dark block nests inside @media, so its :root closes on
    # an INDENTED brace — match the selector it actually uses.
    media = re.search(r':root:not\(\[data-theme="light"\]\)\s*{(.*?)\n  }', css, re.S)
    themes["media_dark"] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", media.group(1)))
    return themes


def _ratio(fg, bg):
    def lum(h):
        r, g, b = (int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = f(r), f(g), f(b)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    la, lb = lum(fg), lum(bg)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# The pairs that carry text. ink-3 is held to 4.5 because the token's own
# comment promises it for small labels; everything else is body-or-larger
# but held to 4.5 anyway — nothing in this product needs the 3:1 discount.
_TEXT_PAIRS = [
    ("ink", "paper"), ("ink", "surface"),
    ("ink-2", "paper"), ("ink-2", "surface"),
    ("ink-3", "paper"), ("ink-3", "surface"),
    ("accent", "paper"), ("accent", "surface"),
    ("on-accent", "accent"), ("on-accent-2", "accent"),
    ("band-cold-t", "band-cold-s"),
    ("conf-unrated-t", "conf-unrated-s"),
    ("fresh-t", "fresh-s"),
    ("spon-unknown-t", "spon-unknown-s"),
]


def test_every_text_bearing_token_pair_clears_wcag_in_both_themes():
    themes = _css_tokens()
    failures = []
    for theme in ("light", "dark"):
        t = themes[theme]
        for fg, bg in _TEXT_PAIRS:
            r = _ratio(t[fg], t[bg])
            if r < 4.5:
                failures.append(f"{theme}: --{fg} on --{bg} = {r:.2f}")
    assert failures == [], failures


def test_the_two_dark_blocks_never_drift_apart():
    """Dark mode is stated twice — the system-preference media query and the
    explicit [data-theme] override. They are the same palette by intent; an
    edit that touches one and not the other ships a theme that changes when
    the user flips the toggle. Compare every token both blocks state."""
    themes = _css_tokens()
    explicit, media = themes["dark"], themes["media_dark"]
    drift = {k: (media[k], explicit[k]) for k in media
             if k in explicit and media[k] != explicit[k]}
    assert drift == {}, drift


def test_every_text_entry_input_type_is_styled():
    """A widget type absent from the forms selector renders as raw browser
    chrome — grey square border, Arial — between two designed fields. That
    shipped for input[type=url] (Contact.linkedin) and the calendar's date
    and time pickers. The selector must name every type the product renders,
    so this reads the form templates for the types actually in use and
    asserts the stylesheet covers each one."""
    root = pathlib.Path(__file__).resolve().parents[2]
    css = (root / "static" / "css" / "coverage.css").read_text()
    styled = set(re.findall(r'input\[type="([a-z-]+)"\]', css))

    used = set()
    for tpl in (root / "templates").rglob("*.html"):
        used |= set(re.findall(r'<input[^>]*type="([a-z-]+)"', tpl.read_text()))
    # Widget types set explicitly on a form widget.
    for form in root.rglob("forms.py"):
        used |= set(re.findall(r'"type":\s*"([a-z-]+)"', form.read_text()))
    # Types Django derives from the MODEL field with no mention anywhere in
    # templates or forms. This is the branch that matters: the url input that
    # shipped unstyled came from `linkedin = models.URLField(...)`, so a scan
    # of markup alone reports "url is never used" and the guard passes over
    # the very bug it exists to catch. Verified by deleting the url selector
    # and confirming this test goes red.
    _FIELD_TO_TYPE = {"URLField": "url", "EmailField": "email",
                      "DateField": "date", "TimeField": "time",
                      "DateTimeField": "datetime-local"}
    for mod in root.rglob("models.py"):
        src = mod.read_text()
        for field, itype in _FIELD_TO_TYPE.items():
            # DateTimeField on a model is usually auto_now/auto_now_add and
            # never rendered; only count fields a user could edit.
            if re.search(rf"models\.{field}\((?![^)]*auto_now)", src):
                used.add(itype)

    ignore = {"hidden", "submit", "button", "image", "reset", "checkbox", "radio"}
    missing = sorted((used - ignore) - styled)
    assert missing == [], f"input types rendered but never styled: {missing}"


_SECTION_LABELS = ("strip-title", "rail-title", "apps-lenses-eyebrow",
                   "settings-nav-title", "cad-rail-title", "inst-card h2")


def test_the_section_label_has_exactly_one_definition():
    """Five classes name the same quiet uppercase micro-label. They were
    written independently in four files and drifted — letter-spacing across
    0.06/0.07/0.08em, colour between ink-2 and ink-3 — so the same label
    looked subtly different on every page and there was nowhere to fix it
    once. coverage.css §6b owns the type now; a page may still add what is
    genuinely local (a flex row, an indent, a mono face) but must not
    restate the shared properties."""
    root = pathlib.Path(__file__).resolve().parents[2]
    shared = {"font-size", "letter-spacing", "text-transform", "font-weight", "color"}

    offenders = []
    for tpl in (root / "templates").rglob("*.html"):
        text = tpl.read_text()
        for cls in _SECTION_LABELS:
            # Each declaration block for the class in this template.
            for m in re.finditer(rf"\.{re.escape(cls)}\s*{{([^}}]*)}}", text):
                props = {d.split(":")[0].strip()
                         for d in m.group(1).split(";") if ":" in d}
                clash = props & shared
                if clash:
                    offenders.append(f"{tpl.name}: .{cls} restates {sorted(clash)}")
    assert offenders == [], offenders


def test_only_section_headers_wear_the_rule():
    """The ruled underline is the identity's signature and is deliberately
    NOT universal: it marks labels that head a section. Settings' nav group
    label names a menu and the cadence caption names a diagram, so both take
    the type and skip the rule. A signature worn everywhere stops being one."""
    css = (pathlib.Path(__file__).resolve().parents[2]
           / "static" / "css" / "coverage.css").read_text()
    ruled = re.search(r"\n([^\n]*)\s*{\s*\n?\s*position: relative; padding-bottom: 7px;", css)
    assert ruled, "the ruled-label selector list is gone"
    selector = ruled.group(1)
    for cls in ("strip-title", "rail-title", "apps-lenses-eyebrow"):
        assert cls in selector, f".{cls} should head a section and carry the rule"
    for cls in ("settings-nav-title", "cad-rail-title"):
        assert cls not in selector, f".{cls} labels a menu/diagram and must skip the rule"


def test_nothing_sets_type_below_the_floor():
    """--fs-nano (10px) is the smallest size the product may use. Seven rules
    had gone under it with hardcoded pixels — 9px month labels and event
    times, and an 8px year tag at opacity .55 that measured 2.18:1 and was
    both the least readable text in the product and the only thing telling
    one year's August from the next on a 24-month rail.

    Scoped to sizes UNDER the floor, not to hardcoded px generally: the
    product legitimately sets one-off large display sizes (a 44px landing
    hero, a 28px instrument figure) that no shared token should own. The
    defect is going below the smallest token, not declining to use one."""
    root = pathlib.Path(__file__).resolve().parents[2]
    files = list((root / "templates").rglob("*.html")) + [root / "static" / "css" / "coverage.css"]
    floor = 10.0
    offenders = []
    for f in files:
        for m in re.finditer(r"font-size:\s*(\d+(?:\.\d+)?)px", f.read_text()):
            if float(m.group(1)) < floor:
                offenders.append(f"{f.name}: {m.group(0)}")
    assert offenders == [], (
        f"type below the {floor:.0f}px floor; use --fs-nano or larger: {offenders}")


def _is_icon_only_dimmed_control(selector: str, body: str, file_text: str) -> bool:
    """True if a `color:` hit is a currentColor tint on an icon child, not
    real dimmed text.

    A single class selector (e.g. `.tf-chip-x`) that also has a sibling
    `{selector} svg { ... }` sizing rule in the same file is, by
    construction, hosting an SVG child — that's the only reason a stylesheet
    would size an `svg` nested under that exact class. `color:` on such a
    rule tints the icon via `currentColor`, it doesn't paint text. Requiring
    the flagged rule itself to carry no `font-size`/`font-family` keeps this
    narrow: a rule that sizes type is asserting there IS text, icon sibling
    or not, and must not be waved through.
    """
    if not re.fullmatch(r"\.[\w-]+", selector.strip()):
        return False  # compound/descendant selectors aren't the icon-button shape
    if re.search(r"font-size|font-family", body):
        return False
    cls = re.escape(selector.strip())
    return re.search(rf"{cls}\s+svg\s*\{{[^}}]*width", file_text) is not None


def test_no_rule_dims_text_with_opacity():
    """Opacity on a text element silently multiplies its contrast against a
    ratio that was measured without it. .mrail-yr did exactly this: --ink-3
    is a legible 5:1, and `opacity: .55` turned it into 2.18:1 while the
    token audit above still reported the pair as passing. De-emphasis is a
    COLOUR decision, so it has to happen in a token the audit can see.

    A faded ICON or decorative bar is legitimate; faded TEXT is a hidden
    contrast cut. The `color:` half of the heuristic can't tell those apart
    on its own — currentColor tints an icon exactly the same way it paints
    text — so `_is_icon_only_dimmed_control` narrows the icon case: a sibling
    rule sizing an `svg` under the same class, with no font-size/font-family
    on the flagged rule itself. .se-chip-x and .tf-chip-x are both remove
    buttons that hold only an aria-hidden SVG glyph (verified against their
    templates — no visible text node) and fade 1 -> 0.4 until hovered or
    focused, same as the icon-only tf-tier control above them."""
    root = pathlib.Path(__file__).resolve().parents[2]
    files = list((root / "templates").rglob("*.html")) + [root / "static" / "css" / "coverage.css"]
    offenders = []
    for f in files:
        text = f.read_text()
        for m in re.finditer(r"^\s*([.#][\w.\- >:()\[\]=\"]+)\s*\{([^}]*)\}", text, re.M):
            selector, body = m.group(1), m.group(2)
            om = re.search(r"(?<!-)opacity:\s*(0?\.\d+)", body)
            # Only flag rules that also set a text property — a faded ICON or
            # decorative bar is legitimate; faded TEXT is a hidden contrast cut.
            if om and re.search(r"font-size|font-family|color:", body):
                if _is_icon_only_dimmed_control(selector, body, text):
                    continue
                offenders.append(f"{f.name}: {selector.strip()} opacity {om.group(1)}")
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# MOTION. An animation that never ends is the one the preference exists for.
#
# `.lane-dot` on the Today board pulsed forever with no reduced-motion guard,
# in a stylesheet where four neighbouring components (.src-panel, .cc-avatar,
# .adv-socket, .act-moved) each carry one. It was a miss, not a decision — and
# a sweep found three more: both `kin-sheen` hero washes and `.live-dot`.
#
# coverage.css §17 does end with a blanket `animation-duration: 0.01ms` over
# `*`, so nothing was visibly moving. But that rule keeps the animation
# *running* — an infinite loop retimed to 100k iterations a second rather than
# stopped — so it is a backstop, not the guard. The per-component
# `animation: none` is what actually satisfies WCAG 2.2.2, and this asserts
# every looping animation has one.


def _reduced_motion_selectors(text: str) -> set[str]:
    """Selectors that `animation: none` inside a reduced-motion query."""
    guarded = set()
    for m in re.finditer(r"@media \(prefers-reduced-motion: reduce\)\s*\{", text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text[m.end():i]):
            if re.search(r"animation:\s*none", body):
                guarded |= _split(sel)
    return guarded


def _split(selector: str) -> set[str]:
    return {" ".join(s.split()) for s in selector.split(",") if s.strip()}


def _is_guarded(selector: str, guarded: set[str]) -> bool:
    # A guard may name the component while the animation is set on a
    # state-scoped variant of it — `.fuse-fill` covers `.urg-today .fuse-fill`.
    return any(selector == g or selector.endswith(" " + g) for g in guarded)


def test_every_looping_animation_can_be_switched_off():
    root = pathlib.Path(__file__).resolve().parents[2]
    files = list((root / "templates").rglob("*.html")) + [root / "static" / "css" / "coverage.css"]

    unguarded = []
    for f in files:
        # Comments carry commas and braces of their own, so a prose sentence
        # sitting above a rule reads as part of its selector list.
        text = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)
        guarded = _reduced_motion_selectors(text)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if not re.search(r"\banimation(?:-name)?:[^;}]*\binfinite\b", body):
                continue
            for one in _split(sel):
                if not _is_guarded(one, guarded):
                    unguarded.append(f"{f.name}: {one}")

    assert unguarded == [], (
        "looping animation with no @media (prefers-reduced-motion: reduce) "
        f"guard in the same file: {unguarded}")
