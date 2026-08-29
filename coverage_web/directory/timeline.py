"""Shared vocabulary for firm_dates: event kinds, cycles, and tracks.

Once home to the heat-mapped cycle-calendar builders; the calendar page was
retired in favour of the Opportunities urgency feed, so what remained was a
single label map, imported by the firm-detail timeline table (directory.views)
and the Today week-ahead strip (crm.views).

It now also owns the CYCLE vocabulary, because `FirmDate.cycle` was the last
free-text key on the model. `event_kind` has always been closed (EVENT_KINDS
in import_firm_dates), `confidence` is bounded by a CHECK constraint, and
`region` is drawn from classify.REGION_LABELS — but `cycle` was whatever the
writer happened to type, and the two writers typed different things. The live
41 rows held four spellings of one idea:

    sa2028_ib   18    a cycle AND a desk
    SA 2028     11    a cycle, human-spelled
    sa2028_hk    7    a cycle AND a market (already in the `region` column)
    sa2028_pe    2    a cycle AND a desk
    insight      1    not a cycle at all
    (blank)      2    unstated

Three read-path patches in directory/views.py existed only to cope with that:
`cycle_label` reformats the spellings so a student never sees the raw slug,
`cycle_region` splits a market suffix back out so the firm page does not print
the market twice, and `_drop_contradicted_openings` suppresses an estimated
opening that a close in "the same printed scope" contradicts — a scope only
ambiguous because two spellings render alike. None of them could do the one
thing the founder actually asked for: group a programme ACROSS firms. There
was no query that answered "what is the SA 2028 cycle doing right now",
because there was no key that all 38 SA 2028 rows shared.

WHAT IS MODELLED, AND WHAT DELIBERATELY IS NOT
----------------------------------------------
`cycle` is now a SHAPE, not an enumeration: a season code and a four-digit
intake year, or blank for "not stated". A shape rather than a whitelist for
the same reason `recommend.cycle_choices` is a function and not a module-level
constant — "a module-level `_YEAR = date.today().year` goes stale in a
long-lived worker process". An enumerated vocabulary of cycles needs a
migration every autumn; a shape does not.

The desk half of the old suffix moves to its own column (`FirmDate.track`),
drawn from `classify.TRACKED_TRACKS` — the SAME six-way vocabulary a student
states a preference over. That is the whole point of splitting it: a track in
its own column, spelled the way the profile spells it, is joinable to
`User.tracks`. Fused into a cycle string it was joinable to nothing.

NOT modelled, on purpose. 41 rows across 27 firms is thin, and structure the
data cannot support is worse than none:

  - No `Program` entity. A programme would need a name, an owner, an intake
    size and its own event series to earn a table; what exists is at most four
    dated events per firm.
  - No stage pipeline (applied -> OA -> superday -> offer). `EVENT_KINDS` has
    carried `interview` and `offer` labels since the calendar days and the
    corpus contains ZERO of either row. Modelling a pipeline nothing writes to
    would be inventing a schema out of an empty table.
  - No insight cycle slug. The one row spelled `cycle: insight` was a
    mis-filing — insight programmes are already distinguished by their
    `event_kind` (`insight_open` / `insight_deadline`), which is where the
    importer's closed vocabulary already puts them. A second place to say the
    same thing is how `cycle` got into this state.
"""

from __future__ import annotations

import re

from directory.classify import TRACKED_TRACKS

EVENT_LABELS = {
    "app_open": "Applications Open",
    "app_close": "Applications Close",
    "app_deadline": "Application Deadline",
    "insight_open": "Insight Programme Opens",
    # `insight_close` is the label map's own spelling and predates the
    # importer; `insight_deadline` is what `import_firm_dates.EVENT_KINDS`
    # actually accepts and what the live corpus holds (Morgan Stanley, id 32).
    # The map had only the former, so `_firm_date_row`'s fallback fired and
    # that row rendered as "Insight deadline" — sentence-cased raw slug, sat
    # directly beneath "Applications Close" and "Insight Programme Opens" in
    # the same table. Both spellings are mapped rather than one renamed: the
    # importer matches on the stored string, so renaming the data to suit a
    # label would break re-imports for a display bug.
    "insight_deadline": "Insight Programme Deadline",
    "insight_close": "Insight Programme Closes",
    "interview": "Interviews",
    "offer": "Offers Out",
}

#: Season half of a cycle slug -> how it is printed. `sa` is the summer
#: analyst / summer internship intake, `ft` the full-time / graduate one.
#: These two are what the corpus and `recommend.CYCLE_LABELS` between them
#: actually distinguish; a third is not added until a row needs it.
CYCLE_SEASONS = {"sa": "SA", "ft": "FT"}

#: The stored shape. Lowercase season + four-digit intake YEAR (the year the
#: programme runs, not the year it is applied for): `sa2028`, `ft2027`.
CYCLE_RE = re.compile(r"^(sa|ft)(\d{4})$")

#: The desk vocabulary a `FirmDate.track` may hold, plus "" for a row that is
#: not desk-scoped (21 of the 41 live rows are not). Deliberately
#: `classify.TRACKED_TRACKS` and not a private copy — `accounts.forms`'s
#: preference checkboxes and the Opportunities track filter already read that
#: tuple, and a fourth copy of the same six slugs is exactly the drift
#: `TRACK_LABELS`'s own comment describes.
CYCLE_TRACKS = TRACKED_TRACKS

#: `recommend.parse_target_cycle` bucket -> cycle season. `insight` is absent
#: on purpose (see the module docstring): an insight programme is an event
#: kind here, not a cycle of its own.
_BUCKET_SEASONS = {"internship": "sa", "entry_level": "ft"}


def parse_cycle(raw: str) -> tuple[str, str] | None:
    """A stored or incoming cycle string -> `(cycle, track)`, or None.

    Accepts every spelling the corpus and the two writers produced:

        "sa2028"     -> ("sa2028", "")
        "sa2028_ib"  -> ("sa2028", "ib")      desk suffix -> track column
        "sa2028_hk"  -> ("sa2028", "")        market suffix -> already in `region`
        "SA 2028"    -> ("sa2028", "")        human spelling
        ""           -> ("", "")              a real state: not stated
        "insight"    -> None                  not a cycle; caller must not guess

    None means "this does not name a cycle" and is NOT the same answer as
    `("", "")`. The distinction is the same one `import_firm_dates._parse_date`
    draws between a deliberately blank date and an unreadable one: a blank is
    information, an unparseable string is a broken finding, and collapsing the
    two is how a bad value gets written as though it were a known-unknown.
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""

    # Human spelling: "SA 2028", "FT 2027". One space, season then year.
    head, sep, tail = text.partition(" ")
    if sep:
        season = head.strip().lower()
        year = tail.strip()
        if season in CYCLE_SEASONS and year.isdigit() and len(year) == 4:
            return f"{season}{year}", ""
        return None

    head, sep, tail = text.lower().partition("_")
    if not CYCLE_RE.match(head):
        return None
    if not sep:
        return head, ""
    # The suffix is polymorphic by history: a desk on 20 rows, a market on 7.
    # A market is dropped rather than kept, because `FirmDate.region` already
    # holds it — on all 7 live rows the suffix and the column agree, and a
    # second copy of a fact is a second thing that can go stale.
    if tail in CYCLE_TRACKS:
        return head, tail
    from directory.classify import REGION_LABELS
    if tail in REGION_LABELS:
        return head, ""
    return None


def is_valid_cycle(value: str) -> bool:
    """True for a value the `firm_dates_cycle_vocabulary` CHECK will accept."""
    text = str(value or "")
    return text == "" or bool(CYCLE_RE.match(text))


def is_valid_track(value: str) -> bool:
    """True for a value the `firm_dates_track_vocabulary` CHECK will accept."""
    text = str(value or "")
    return text == "" or text in CYCLE_TRACKS


def cycle_slug_for_target(bucket: str, year: int) -> str:
    """`recommend.parse_target_cycle`'s answer -> the `FirmDate.cycle` slug.

    `("internship", 2028)` -> `"sa2028"`. The bridge between what a student
    STATED on their profile and the key the timeline corpus is now filed
    under. Returns "" for a bucket with no cycle slug (insight), which reads
    downstream as "this preference cannot be matched here" — never as a match.
    """
    season = _BUCKET_SEASONS.get(bucket or "")
    if not season:
        return ""
    try:
        return f"{season}{int(year):04d}"
    except (TypeError, ValueError):
        return ""


def cycle_text(cycle: str, track: str = "") -> str:
    """`("sa2028", "ib")` -> `"SA 2028 · IB"`. The scope a timeline row prints.

    Kept separate from `directory.views.cycle_label`, which stays
    backward-tolerant of the pre-migration spellings; this one speaks only the
    closed vocabulary.
    """
    slug = str(cycle or "").strip().lower()
    m = CYCLE_RE.match(slug)
    label = f"{CYCLE_SEASONS[m.group(1)]} {m.group(2)}" if m else ""
    if not label:
        return ""
    desk = TRACK_SHORT.get(str(track or "").strip().lower(), "")
    return f"{label} · {desk}" if desk else label


#: Short desk names for the timeline's `.tl-scope`, which is uppercased and
#: narrow. `classify.TRACK_LABELS`'s full names ("Private Equity / Credit")
#: are for the facets and the Settings checkboxes, where there is room.
TRACK_SHORT = {
    "ib": "IB",
    "st": "S&T",
    "pe": "PE",
    "am": "AM",
    "consulting": "Consulting",
    "corp-strat": "Corp Strat",
}
