# Do the tal.net table boards paginate? One probe, bodies kept.

Follow-up to `docs/talnet-2026-09.md`, whose closing section recorded two
things it measured but could not answer. This document answers the first of
them. Probe run 2026-09-02 06:15 to 06:23 UTC from the scraper host
(`zhujimmy` macOS, Python 3.13, `coverage_connectors` HTTP layer).

## Why this document exists

The earlier probe found that three of the four reachable tal.net boards
returned **exactly 50** vacancies: Bank of America, Morgan Stanley and
Jefferies. Fifty is the card layout's page size. Evercore returned 9, which
is under any page size and so said nothing either way.

That mattered because `talnet.py` had two layouts with two different
pagination policies. The card branch followed the board's `next_links` nav.
The table branch did not paginate at all, justified in the module docstring
by "Morgan Stanley returns 1,076 rows in one response". If that claim had
gone stale, then every table-layout fetch was page one read as the whole
board — and `ingest` infers "closed" from "absent from a successful fetch",
so the product would be closing roles it simply never looked at.

The earlier probe could not settle it because it did not save the response
bodies. This one does.

## What was done

One request per board, four boards, bodies written to disk. Then one
follow-up request per board that turned out to carry a next-page link, to
confirm page two is real rather than a nav pointing at nothing. Six requests
in total, spaced two and a half seconds apart. Nothing was written to the
database and no posting page was opened.

- The user agent is `coverage_connectors.http.USER_AGENT`, the honest one
  the scraper actually sends. No browser string was used anywhere in this
  probe.
- TLS verified against certifi's roots, the fetch layer's default. Nothing
  was relaxed.
- Vacancy counts come from `talnet._parse_table` and `talnet._parse_cards`,
  the connector's own parsers.
- **Nomura was not fetched.** It serves an Oleeo Protect challenge and is a
  standing condition, not a question (see the earlier document, "What
  follows").

## Result

| Board (jobs) | Layout | Rows page 1 | Board's own count | `next_links` nav | Next href |
|---|---|---|---|---|---|
| Bank of America `bankcampuscareers` | table | 50 | **148 results match!** | yes | `?start=50` |
| Morgan Stanley `morganstanley` | table | 50 | **67 results match** | yes | `?start=50` |
| Evercore `evercore` | table | 9 | 9 results match! | **no paging div at all** | — |
| Jefferies `jefferies` | card | 50 | 51 results match! | yes | `?start=50` |

Follow-up fetch, one per table board that advertised a next page:

| Board | Page 2 url | Rows page 2 | Overlap with page 1 | Next href on page 2 |
|---|---|---|---|---|
| Bank of America | `…/adv/?start=50` | 50 | 0 | `?start=100` |
| Morgan Stanley | `…/adv/?start=50` | 17 | 0 | none (last page) |

Morgan Stanley closes exactly: 50 + 17 = 67, the number the board itself
printed. Bank of America pages three deep against its stated 148.

## The markup that settles it

Morgan Stanley is the board the docstring's claim was about, so its page is
the one that matters. Its results header, verbatim:

```html
<div class="results_meta">
  <div></div>
  <h2 role="alert">67 results match</h2>
</div>
```

and at the foot of the same page, immediately after `</tbody></table>`:

```html
<div class="paging">
  <span class="next_links">
    <a href="?start=50">Next page</a>
  </span>
</div>
```

Bank of America's is character-for-character the same nav under a different
count (`<h2 role="alert">148 results match!</h2>`). So is Jefferies'. **The
`next_links` nav is not a card-layout feature. Oleeo renders the same nav
under both layouts**, and `talnet._NEXT_PAGE_RE` — written for the card
branch — already matched it on both table boards unchanged.

Evercore, the board that fits on one page, carries no `<div class="paging">`
element at all. Morgan Stanley's page two carries the div with a
`prev_links` span and no `next_links`, which is how a walk knows it is
finished:

```html
<div class="paging">
  <span class="prev_links">
    <a href="?start=0">Previous page</a>
  </span>
</div>
```

One more thing the bodies show, which the fix depends on. tal.net mints its
per-session `xf-<hex>` segment **per response, not per board**: every
listing link on Morgan Stanley's page one carried `/xf-3f07b483b799` and
every one on page two carried `/xf-7505baa6e7de`. A walk that dedups on the
url the board served can therefore never recognise a posting it has already
read on an earlier page.

## What it settles

**The table boards paginate at 50, exactly as the card boards do. The
docstring's "1,076 rows in one response" is stale, and it was load-bearing.**

1. Two of the three table boards probed hold more than one page, and both
   say so in their own markup, twice over: a stated total above 50 and a
   next-page link.
2. Every table-layout fetch the product has run was page one reported as a
   complete board. That is not a short list, it is a wrong one: `ingest`
   reads a successful fetch as the full picture and auto-closes what is
   absent from it.
3. Evercore is the control. Nine rows, nine stated, no paging div. A board
   that fits on one page still fetches once after the fix.

## Blast radius, measured on the founder's live data

Read-only queries, 2026-09-02.

- **275 open `Opportunity` rows** come from table-layout tal.net boards
  (Bank of America 67, Morgan Stanley 89, Evercore 57, Nomura 48, Moelis 8,
  Perella Weinberg 6). All 275 were fetched one page deep.
- Of the **67 live roles sitting on page two** of Bank of America (50) and
  Morgan Stanley (17), **zero are in the database.** Not stale, not closed —
  never seen. Bank of America's own count implies a further 48 on page
  three, which was not fetched.
- The 51 Jefferies rows are the card board and were already walked
  correctly.

The rows the product does hold are page-one rows, which is why nothing has
visibly broken: a role that has never left page one is fetched every run and
stays open. The damage is silent and one-directional — a role that scrolls
off page one becomes invisible, and on the next run its absence reads as a
death.

## What follows

- **Both branches walk the nav.** `_fetch_card_pages` became `_fetch_pages`
  and takes the layout's parser as an argument; `fetch()` picks the parser
  from page one and hands it over. There is one walker, not two, so the
  bound, the loop guard and the `truncated` contract are shared rather than
  reimplemented.
- **A walk that cannot finish still reports `truncated=True`, not a clean
  short list.** That is the card branch's existing contract and `ingest`
  already reads it: a truncated pair is exempted from closed-detection, its
  rows upserted, and nothing closed off it. A page fetch that raises leaves
  `fetch()` through the existing `ok=False` path.
- **Dedup keys on `canonical_url`, not the served url**, for the `xf-`
  reason above.
- Bank of America and Morgan Stanley will now issue 3 and 2 requests per run
  instead of 1. That is the cost of reading the board.

## One thing this probe measured but did not act on

Every board prints its own total in a `results_meta` header — "148 results
match!", "67 results match", "9 results match!". That is a positive
completeness check strictly stronger than following the nav: it would catch
the case where the nav markup changes and the walk silently drops back to
one page.

It is **not** wired up here. Doing so would change the card branch's
behaviour too, and the one card fixture in the suite states 51 while holding
4 rows, so the check would have to be reconciled against fixtures this
probe did not gather. Following the nav is what the evidence demanded;
cross-checking the total is a separate change with its own evidence to
collect first (P1).
