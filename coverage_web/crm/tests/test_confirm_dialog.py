"""The styled confirm dialog that replaced the browser's `confirm()`.

Five bulk writes in this product asked through the browser's own confirm():
an OS box with OS buttons in the middle of a page that is otherwise entirely
ours, and unreachable by any stylesheet. All five go through base.html's
`#cov-confirm` now — the two `hx-confirm` attributes in `crm/_cockpit.html`
via a global `htmx:confirm` listener that leaves the attributes untouched,
and the three hand-written calls (two on the Network board, one on the Parked
page) via `window.covConfirm`.

A confirm is the last thing between a student and a write that moves dozens
of people at once, so the assertions here are weighted accordingly: the copy
of all five is pinned unchanged, the fallback to the native box is pinned so
the guard can never simply be absent, and every not-yes path is pinned to
cancel.

TWO THINGS ABOUT HOW THESE READ THE PAGE. Assertions on the dialog's markup
are anchored on the CLASS NAME, never on the whole `class` attribute: this
element wears `confirm panel`, and a primitive appended to a class list has
broken a row of tests here before. And assertions about the JavaScript strip
its comments first — the code lives in a rendered `{% script %}` block, so
its `//` comments ship in the page body and would otherwise satisfy or break
a guard on their own. `{% comment %}` blocks are not rendered and need no
such care.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "coverage.css"

BASE_HTML = TEMPLATES / "base.html"
COCKPIT = TEMPLATES / "crm" / "_cockpit.html"
CONTACT_LIST = TEMPLATES / "crm" / "contact_list.html"
CONTACT_PARKED = TEMPLATES / "crm" / "contact_parked.html"

# The five sentences. Not one of them may change: several name a count, and
# the wording of each was argued out where it lives.
COPY = {
    "accept": "Accept {{ proposals|length }} contacts? They're added as-is, "
              "without the role or region on each card.",
    "park_all": "Park all {{ park_total }}? A reply un-parks anyone, and you "
                "can undo this straight after.",
    "archive": "Archive \" + n + \" contact\" + (n === 1 ? \"\" : \"s\") +",
    "bulk_park": "Stop following up with \" + n + \" contact\"",
    "unpark": "Bring \" + n + \" contact\" + (n === 1 ? \"\" : \"s\") + "
              "\" back into the queue?\"",
}

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_DJANGO_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.S)
_DJANGO_HASH = re.compile(r"{#.*?#}", re.S)


def _code(text: str) -> str:
    """The code with every kind of comment around it removed.

    Both kinds matter and for opposite reasons. A `//` comment inside a
    rendered `{% script %}` block is page text: left in, the sentence
    explaining why `issueRequest(true)` is needed would satisfy a guard
    looking for that call. A `{% comment %}` block is NOT rendered, but it
    is still template source, and five templates in this repo discuss
    `<script>` tags and `confirm()` in prose — which is enough to fail a
    guard scanning the source for either.
    """
    text = _DJANGO_COMMENT.sub("", _DJANGO_HASH.sub("", text))
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _dialog_block(src: str) -> str:
    """base.html's confirm dialog markup.

    Sliced forward from its id: the palette's `</dialog>` comes EARLIER in
    the file, so a bare `src.index("</dialog>")` closes the wrong element.
    """
    start = src.index('id="cov-confirm"')
    return src[start:src.index("</dialog>", start)]


def _signed_in_page() -> str:
    user = get_user_model().objects.create_user(
        email="confirm-dialog@example.com", password="pw12345!")
    client = Client()
    client.force_login(user)
    return client.get("/app/contacts/").content.decode()


# ---------------------------------------------------------------------------
# The dialog itself
# ---------------------------------------------------------------------------

def test_the_dialog_is_a_native_dialog_wearing_the_panel_primitive():
    """A native <dialog>, so the focus trap and Escape are the browser's job.

    Anchored on the class NAME. The element carries `confirm panel`, and a
    test that pinned the whole attribute would break the next time it gains
    a modifier.
    """
    src = BASE_HTML.read_text()
    tag = re.search(r"<dialog[^>]*id=\"cov-confirm\"[^>]*>", src)
    assert tag, "base.html no longer renders the #cov-confirm dialog"
    classes = re.search(r'class="([^"]*)"', tag.group(0)).group(1).split()
    assert "confirm" in classes
    assert "panel" in classes, (
        "the dialog must wear the shared panel primitive rather than "
        "redeclare a panel shape of its own")


def test_the_two_buttons_are_the_shared_btn_family():
    block = _dialog_block(BASE_HTML.read_text())
    buttons = re.findall(r"<button[^>]*>", block)
    assert len(buttons) == 2, "cancel and confirm, no more"
    classes = [re.search(r'class="([^"]*)"', b).group(1).split() for b in buttons]
    assert all("btn" in c for c in classes)
    # Cancel is plain; the confirm side carries the primary weighting, and
    # swaps to `btn-danger` at the one call site that takes people off the
    # board (see the archive test below).
    assert any("btn-primary" in c for c in classes)


def test_cancel_comes_first_in_source_order_so_enter_cancels():
    """The first button in a `method="dialog"` form is the default button.

    On a guard in front of a bulk write, Enter must not be the yes.
    """
    block = _dialog_block(BASE_HTML.read_text())
    assert block.index('value="cancel"') < block.index('value="ok"')


def test_the_dialog_renders_on_every_page_not_only_when_signed_in():
    """A guard that is only sometimes in the DOM is sometimes not there."""
    anon = Client().get("/accounts/login/").content.decode()
    assert 'id="cov-confirm"' in anon
    assert 'id="cov-confirm"' in _signed_in_page()


def test_only_an_ok_return_value_resolves_the_promise_true():
    """Escape, a backdrop click and any other close all mean cancel.

    They mean it by construction rather than by a listener someone has to
    remember: none of them sets `returnValue`, and this is the only
    comparison in the file that can produce a yes.
    """
    code = _code(BASE_HTML.read_text())
    assert 'resolve(dlg.returnValue === "ok")' in code
    # And it is cleared before every open, or a dialog confirmed once and
    # then dismissed with Escape would still be carrying "ok".
    assert 'dlg.returnValue = "";' in code


def test_a_backdrop_click_closes_the_dialog():
    """Native dialogs do not do this themselves; the palette wires it too."""
    code = _code(BASE_HTML.read_text())
    assert re.search(
        r"dlg\.addEventListener\(\"click\".*?e\.target === dlg.*?dlg\.close\(\)",
        code, re.S)


def test_the_stylesheet_carries_the_dialog_rules():
    css = CSS.read_text()
    assert ".confirm {" in css
    assert ".confirm::backdrop" in css
    assert ".confirm-text" in css
    assert ".confirm-actions" in css
    # A native <dialog> carries the browser's default text colour rather
    # than reliably inheriting the page's, which renders near-black ink on
    # the dark theme's dark surface. assistant/chat.html hit this live.
    rule = css[css.index(".confirm {"):css.index(".confirm::backdrop")]
    assert "color: var(--ink)" in rule
    assert "@media (prefers-reduced-motion: reduce) { .confirm" in css


# ---------------------------------------------------------------------------
# The fallback. The guard is never simply gone.
# ---------------------------------------------------------------------------

def test_a_missing_dialog_falls_back_to_the_native_confirm():
    code = _code(BASE_HTML.read_text())
    guard = re.search(
        r"if \(!dlg \|\| !textEl \|\| !okBtn \|\| "
        r"typeof dlg\.showModal !== \"function\"\) \{\s*"
        r"return Promise\.resolve\(window\.confirm\(message\)\);", code)
    assert guard, (
        "covConfirm must hand back the browser's own confirm() when the "
        "dialog or showModal() is unavailable — ugly beats absent")


def test_a_throwing_showmodal_falls_back_too():
    """Already open, or detached from the document. Still not a silent yes."""
    code = _code(BASE_HTML.read_text())
    catch = re.search(r"catch \(err\) \{(.*?)\}", code, re.S)
    assert catch, "showModal() must be wrapped"
    assert "resolve(window.confirm(message))" in catch.group(1)


def test_nothing_resolves_true_without_either_a_dialog_or_a_native_confirm():
    """Every `resolve(` in covConfirm is one of the three known paths."""
    code = _code(BASE_HTML.read_text())
    body = code[code.index("window.covConfirm ="):code.index("Backdrop click")
                if "Backdrop click" in code else len(code)]
    resolves = re.findall(r"resolve\(([^;]*)\);", body)
    assert resolves, "covConfirm must resolve"
    for r in resolves:
        assert ("window.confirm(message)" in r
                or 'dlg.returnValue === "ok"' in r), r


# ---------------------------------------------------------------------------
# hx-confirm: every attribute keeps working, untouched
# ---------------------------------------------------------------------------

def test_the_cockpit_hx_confirm_attributes_are_untouched():
    """The whole point of the htmx:confirm seam: no caller moves."""
    src = COCKPIT.read_text()
    assert COPY["accept"] in src
    assert COPY["park_all"] in src
    assert src.count("hx-confirm=") == 2


def test_the_global_listener_only_fires_when_there_is_a_question():
    """`htmx:confirm` fires for EVERY htmx request, question null or not.

    Without the guard this would put a dialog in front of every click on
    the site.
    """
    code = _code(BASE_HTML.read_text())
    handler = code[code.index('addEventListener("htmx:confirm"'):]
    assert "if (!e.detail || !e.detail.question) return;" in handler
    assert "e.preventDefault();" in handler


def test_the_listener_skips_htmx_own_native_confirm():
    """`issueRequest(true)`, not `issueRequest()`.

    The argument is htmx 2.0.10's `skipConfirmation`, and it gates two
    things inside `issueAjaxRequest`: the event we are already inside, and a
    plain `confirm(question)` further down. Passing nothing sends `false`,
    which suppresses only the first — so the student would answer the styled
    dialog and then get the OS box anyway.
    """
    code = _code(BASE_HTML.read_text())
    handler = code[code.index('addEventListener("htmx:confirm"'):]
    assert "e.detail.issueRequest(true)" in handler
    assert "e.detail.issueRequest()" not in handler


# ---------------------------------------------------------------------------
# The hand-written call sites
# ---------------------------------------------------------------------------

def test_no_template_calls_the_bare_browser_confirm_any_more():
    """No mix of styled and native.

    The lookbehind is what makes this a real guard rather than a spelling
    check: it rejects any `confirm(` reached through a dot, so
    `window.confirm(message)` — base.html's deliberate fallback — does not
    count, and a bare `confirm(...)` anywhere does.
    """
    offenders = {}
    for path in TEMPLATES.rglob("*.html"):
        code = _code(path.read_text())
        for m in re.finditer(r"(?<![.\w])confirm\(", code):
            before = code[max(0, m.start() - 40):m.start()]
            offenders.setdefault(
                path.relative_to(TEMPLATES).as_posix(), []).append(before[-30:])
    assert offenders == {}, f"bare confirm() left in templates: {offenders}"


def test_the_network_boards_two_questions_go_through_the_dialog_unchanged():
    src = CONTACT_LIST.read_text()
    assert COPY["archive"] in src
    assert COPY["bulk_park"] in src
    code = _code(src)
    assert "window.covConfirm(question, opts)" in code
    assert re.search(r"(?<![.\w])confirm\(", code.replace("covConfirm(", "")) is None


def test_archive_is_the_one_verb_weighted_destructive():
    """It is the only one here that takes people off the board. Park is
    reversible and keeps the accent primary; a reversible action drawn in
    red is a scare rather than a description."""
    code = _code(CONTACT_LIST.read_text())
    archive = code[code.index('if (verb === "archive")'):code.index('if (verb === "park")')]
    assert '{ ok: "Archive", tone: "danger" }' in archive
    park = code[code.index('if (verb === "park")'):code.index("if (!question) return;")]
    assert "tone" not in park


def test_the_bulk_bar_resubmits_with_its_submitter_so_the_verb_survives():
    """The verb this handler branches on IS the pressed button's value.

    `form.submit()` drops the submitter, so the POST would arrive with no
    `verb` at all. `requestSubmit(submitter)` keeps it — and re-fires the
    submit event, which is what the re-entry flag exists for.
    """
    code = _code(CONTACT_LIST.read_text())
    assert "form.requestSubmit(submitter)" in code
    assert "if (confirmed) { confirmed = false; return; }" in code
    # And when requestSubmit is unavailable, the verb is carried by hand
    # rather than posted without.
    fallback = code[code.index("if (form.requestSubmit)"):]
    assert 'hidden.name = "verb"' in fallback


def test_the_parked_pages_question_goes_through_the_dialog_unchanged():
    src = CONTACT_PARKED.read_text()
    assert COPY["unpark"] in src
    assert "cold — with nothing new, the cadence will propose parking" in src
    assert "window.covConfirm(msg)" in _code(src)


# ---------------------------------------------------------------------------
# The CSP bug found while doing the above
# ---------------------------------------------------------------------------

def test_no_template_carries_an_inline_script_without_a_nonce():
    """`script-src 'self' <nonce>` blocks a bare inline <script> outright.

    `crm/contact_parked.html` carried the last one in templates/, so its
    unpark confirm never ran in a browser at all: every form on that page
    submitted with no confirmation, silently, from the day the CSP landed.
    django-csp's `{% script %}` is what stamps the nonce.
    """
    offenders = [
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*.html")
        if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", _code(p.read_text()))
    ]
    assert offenders == [], (
        f"inline <script> with no nonce, blocked by CSP: {offenders}")


def test_the_parked_page_actually_ships_its_script_with_a_nonce():
    user = get_user_model().objects.create_user(
        email="parked-nonce@example.com", password="pw12345!")
    client = Client()
    client.force_login(user)
    html = client.get("/app/contacts/parked/").content.decode()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", html)
    assert scripts, "the parked page renders no inline script at all"
    assert all("nonce=" in s for s in scripts), scripts
    assert "covConfirm" in html
