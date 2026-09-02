"""tal.net: the table layout pages at 50, like the card layout always did.

The defect. `_fetch_card_pages` followed the board's `next_links` nav for
the card layout only. The table branch took page one and returned it as the
whole board, justified in the module docstring by "Morgan Stanley returns
1,076 rows in one response". That was true once. Re-probed 2026-09-02 with
the response bodies kept (`docs/talnet-pagination-2026-09.md`):

    morganstanley.tal.net   50 rows, next_links nav, "67 results match"
    bankcampuscareers       50 rows, next_links nav, "148 results match!"
    evercore.tal.net         9 rows, no paging div,  "9 results match!"

So both table tenants with more than 50 vacancies were being read one page
deep. That is worse than a short list: `ingest` infers "closed" from
"absent from a successful fetch", so every role past position 50 was both
invisible and, had it ever been banked, a candidate for auto-close.
Measured the same night on the founder's data: 275 open rows come from
table-layout tal.net boards, and of the 67 live roles sitting on page two
of Bank of America and Morgan Stanley, zero were in the database.

The fix reuses the card branch's own walker rather than adding a second
one: `_fetch_pages` takes the layout's parser as an argument, and both
branches call it. These tests pin the walk, its termination, and the
cross-page dedup key.

The fixtures are two real morganstanley.tal.net pages trimmed to two
listing rows each; the header row, the results count and both paging navs
are the board's own markup.
"""

from __future__ import annotations

from pathlib import Path

from coverage_connectors import talnet as talnet_mod
from coverage_connectors.models import TalnetBoard
from coverage_connectors.talnet import fetch

FIXTURES = Path(__file__).parent / "fixtures"

MS_BOARD = TalnetBoard(
    firm="Morgan Stanley", kind="jobs",
    board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1"
              "/brand-2/candidate/jobboard/vacancy/1/adv/",
)
PAGE1 = FIXTURES / "talnet_table_paged_sample.html"
PAGE2 = FIXTURES / "talnet_table_paged_sample_page2.html"


def _serve(monkeypatch, pages: dict[str, str]) -> list[str]:
    """Serve `pages` by url, recording the order they were asked for."""
    calls: list[str] = []

    def fake(url, **kw):
        calls.append(url)
        try:
            return pages[url]
        except KeyError:
            raise AssertionError(f"unexpected fetch: {url}") from None

    monkeypatch.setattr(talnet_mod, "fetch_text", fake)
    return calls


def test_the_fixture_pages_are_the_shape_this_file_claims():
    """Guard the evidence itself: if a later edit flattens these fixtures,
    every test below would still pass while measuring nothing."""
    page1, page2 = PAGE1.read_text(), PAGE2.read_text()
    assert len(talnet_mod._parse_table(page1)) == 2
    assert len(talnet_mod._parse_table(page2)) == 2
    assert talnet_mod._parse_cards(page1) == [], "a table board is not a card board"
    # Page one advertises more; page two is the last page and says so with a
    # prev_links nav and no next one.
    assert talnet_mod._NEXT_PAGE_RE.search(page1).group(1) == "?start=50"
    assert talnet_mod._NEXT_PAGE_RE.search(page2) is None
    assert 'class="paging"' in page2, "the last page still renders a paging div"
    # The per-response xf token differs between the two pages. This is why
    # cross-page dedup cannot key on the url the board served.
    assert "/xf-3f07b483b799" in page1 and "/xf-7505baa6e7de" in page2


def test_a_table_board_follows_its_next_page_nav(monkeypatch):
    """The whole point. Before this, a table board returned page one only
    and reported it as the complete board."""
    nxt = MS_BOARD.board_url + "?start=50"
    calls = _serve(monkeypatch, {MS_BOARD.board_url: PAGE1.read_text(),
                                 nxt: PAGE2.read_text()})

    result = fetch(MS_BOARD)

    assert calls == [MS_BOARD.board_url, nxt]
    assert result.ok and result.error is None
    assert [o.title for o in result.opportunities] == [
        "Honouring Black History Month: Empowering Early Careers in Banking "
        "and Fostering Community",
        "2027 Risk Management Off Cycle Internship (Frankfurt)",
        # page two, reached through the board's own next_links nav
        "2027 Internal Audit - Business Audit Summer Analyst Master’s "
        "Program (Baltimore)",
        "2027 Finance Summer Analyst Program (Baltimore)",
    ]
    assert result.raw_count == 4
    # The table path's own contract is unchanged by the walk: the board's
    # City column still becomes the row's location.
    assert result.opportunities[-1].location == "Baltimore"
    # Every page's per-session token is stripped, not just page one's.
    assert all("/xf-" not in o.url for o in result.opportunities)
    # The nav ran out, so the list is whole and absence from it IS evidence
    # of absence. This is the flag ingest reads before auto-closing.
    assert result.truncated is False


def test_a_table_board_with_no_next_page_nav_fetches_once(monkeypatch):
    """Evercore's shape: 9 rows, 9 stated, no paging div at all. A board
    that fits on one page must not gain a second request."""
    page1 = PAGE1.read_text().replace("next_links", "prev_links")
    calls = _serve(monkeypatch, {MS_BOARD.board_url: page1})

    result = fetch(MS_BOARD)

    assert calls == [MS_BOARD.board_url]
    assert result.ok and len(result.opportunities) == 2
    assert result.truncated is False


def test_a_table_nav_that_loops_says_truncated_rather_than_looping(monkeypatch):
    """Same guarantee the card branch has always had, now that the table
    branch shares the walker. A nav pointing back at a page already read
    must stop AND report the list as partial — reporting it whole is what
    lets ingest close a live posting off a list that never held it."""
    page1 = PAGE1.read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page1)

    result = fetch(MS_BOARD)

    assert result.ok
    assert len(result.opportunities) == 2
    assert result.truncated is True


def test_the_same_posting_on_two_pages_is_counted_once(monkeypatch):
    """tal.net mints a fresh xf-<hex> per RESPONSE, so a posting that
    appears on two pages of one walk arrives under two different urls.
    Dedup keys on the canonical url for exactly this reason; keying on the
    served url would double-count it and inflate raw_count."""
    page1 = PAGE1.read_text()
    # Page two, verbatim, except that it carries page one's rows under its
    # own session token — the collision the canonical key exists to catch.
    page2 = PAGE2.read_text().replace("next_links", "prev_links")
    body = page1.replace("/xf-3f07b483b799", "/xf-7505baa6e7de")
    page2 = page2[:page2.index("<tbody>")] + "<tbody>" + "".join(
        body[body.index("<tbody>") + len("<tbody>"):body.index("</tbody>")]
    ) + page2[page2.index("</tbody>"):]

    nxt = MS_BOARD.board_url + "?start=50"
    _serve(monkeypatch, {MS_BOARD.board_url: page1, nxt: page2})

    result = fetch(MS_BOARD)

    assert result.ok and result.truncated is False
    assert result.raw_count == 2, "the two pages listed the same two postings"
    assert len({o.url for o in result.opportunities}) == 2


def test_a_failed_second_page_is_a_board_error_not_a_short_list(monkeypatch):
    """A walk that cannot finish must not hand ingest the rows it did get
    as though they were the board. `fetch()` catches it into ok=False, the
    same "unreadable, not empty" answer the bot-challenge and zero-rows
    guards give."""
    def fake(url, **kw):
        if url == MS_BOARD.board_url:
            return PAGE1.read_text()
        raise OSError("connection reset while fetching page 2")

    monkeypatch.setattr(talnet_mod, "fetch_text", fake)

    result = fetch(MS_BOARD)

    assert result.ok is False
    assert result.opportunities == []
    assert "connection reset" in result.error
