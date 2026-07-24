"""Tests for the hand-rolled YAML parsers (no PyYAML available). These guard
the highest-risk code in the seed path against the concrete file shapes it must
handle: flow-mapping firm rows, and block firm_dates with quoted / folded /
multi-line values.
"""

from __future__ import annotations

from directory.seed_parsers import parse_firms_yaml, parse_timeline_yaml

FIRMS = """\
# Target list. tier/tracks/regions/status.
firms:
  # ---- Tier 1 ----
  - {id: gs, name: Goldman Sachs, tier: 1, tracks: [ib, st], regions: [us, hk], status: active, domains: [goldmansachs.com], sponsors: true}
  - {id: cicc, name: CICC, tier: 2, tracks: [ib], regions: [hk], status: active, domains: [cicc.com], sponsors: unknown}
  - {id: socgen, name: Société Générale, tier: 3, tracks: [st], regions: [hk], status: active, domains: [], sponsors: true}
  - {id: usc, name: USC (on-campus), tier: 3, tracks: [], regions: [us], status: on_campus, domains: [usc.edu]}
"""

TIMELINE = """\
# US SA 2028 cycle.
cycle: SA 2028
region: us
phases:
- id: sophomore_positioning
  name: Sophomore positioning
  start: 2026-06-01
  focus: Build the networking base and apply as programs open
firm_dates:
- key: blackstone/sa2028_pe/app_open
  date: 2026-12
  precision: estimated
  confidence: reported
  source: 'seed:historical-pattern'
  found: '2026-07-03'
  note: Historically the earliest mover of any firm
- key: gs/sa2028_ib/insight_open
  date: '2026-09'
  precision: month
  confidence: confirmed_official
  source:
    https://www.goldmansachs.com/careers/students/programs/emerging-leaders
  found: '2026-07-03'
  note: "Goldman Sachs Emerging Leaders Series (Americas) for students graduating December
    2028 - June 2029: official page states 'Applications will open in the fall 2026'
    (no exact day posted)."
- key: ms/insight/insight_open
  date: '2026-07-16'
  precision: day
  confidence: reported
  source: https://morganstanley.tal.net/candidate/jobboard/vacancy/2/adv/
  found: '2026-07-03'
  note: ''
"""


def test_parse_firms_basic_and_types():
    rows = parse_firms_yaml(FIRMS)
    assert len(rows) == 4
    gs = rows[0]
    assert gs["id"] == "gs"
    assert gs["name"] == "Goldman Sachs"
    assert gs["tracks"] == ["ib", "st"]
    assert gs["regions"] == ["us", "hk"]
    assert gs["domains"] == ["goldmansachs.com"]
    assert gs["sponsors"] is True
    assert gs["status"] == "active"


def test_parse_firms_unknown_sponsors_unicode_and_empty_list():
    rows = parse_firms_yaml(FIRMS)
    cicc = next(r for r in rows if r["id"] == "cicc")
    assert cicc["sponsors"] == "unknown"
    socgen = next(r for r in rows if r["id"] == "socgen")
    assert socgen["name"] == "Société Générale"
    assert socgen["domains"] == []
    usc = next(r for r in rows if r["id"] == "usc")
    assert usc["tracks"] == []
    assert usc["status"] == "on_campus"
    assert "sponsors" not in usc  # on-campus pseudo-firm has no sponsors flag


def test_parse_timeline_region_cycle_and_ignores_phases():
    region, cycle_label, entries = parse_timeline_yaml(TIMELINE)
    assert region == "us"
    assert cycle_label == "SA 2028"
    # phases block must not leak into firm_dates
    assert len(entries) == 3
    assert all("/" in e["key"] for e in entries)


def test_parse_timeline_plain_and_quoted_scalars():
    _, _, entries = parse_timeline_yaml(TIMELINE)
    bx = entries[0]
    assert bx["key"] == "blackstone/sa2028_pe/app_open"
    assert bx["date"] == "2026-12"
    assert bx["precision"] == "estimated"
    assert bx["confidence"] == "reported"
    assert bx["source"] == "seed:historical-pattern"
    assert bx["found"] == "2026-07-03"
    assert bx["note"].startswith("Historically the earliest")


def test_parse_timeline_folded_source_and_multiline_note():
    _, _, entries = parse_timeline_yaml(TIMELINE)
    gs = entries[1]
    assert gs["date"] == "2026-09"
    assert gs["confidence"] == "confirmed_official"
    # source value was empty inline, folded onto the next indented line
    assert gs["source"] == "https://www.goldmansachs.com/careers/students/programs/emerging-leaders"
    # multi-line double-quoted note collapsed to a single spaced string
    assert gs["note"].startswith("Goldman Sachs Emerging Leaders Series")
    assert "graduating December 2028 - June 2029" in gs["note"]
    assert '"' not in gs["note"]


def test_parse_timeline_empty_note_and_single_line_url_source():
    _, _, entries = parse_timeline_yaml(TIMELINE)
    ms = entries[2]
    assert ms["key"] == "ms/insight/insight_open"
    assert ms["date"] == "2026-07-16"
    assert ms["note"] == ""
    assert ms["source"] == "https://morganstanley.tal.net/candidate/jobboard/vacancy/2/adv/"
