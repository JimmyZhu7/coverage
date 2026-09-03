"""Shared test fixtures for coverage_connectors.

Unit tests monkeypatch `coverage_connectors.http.fetch_json` / `post_json`
to return captured JSON fixtures (see `tests/fixtures/`) instead of hitting
the network, so normalization/parsing tests are deterministic and run
offline every time. Live smoke tests (marked `@pytest.mark.live`) hit real
boards and are skipped unless `RUN_LIVE=1` is set — see `test_live_smoke.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def greenhouse_jobs_fixture():
    return load_fixture("greenhouse_jobs.json")


@pytest.fixture
def greenhouse_job_detail_fixture():
    return load_fixture("greenhouse_job_detail.json")


@pytest.fixture
def lever_postings_fixture():
    return load_fixture("lever_postings.json")


@pytest.fixture
def workday_citi_page1_fixture():
    return load_fixture("workday_citi_page1.json")


@pytest.fixture
def workday_no_locations_text_fixture():
    """Six real Raymond James CxS rows (raymondjames.wd1/raymondjamescareers,
    captured 2026-09-02), kept because they carry NO `locationsText` key at
    all — the tenant's board columns put the address in `bulletFields`
    instead. Every other Workday fixture here sends `locationsText`, so this
    is the only one that exercises the absent-field path."""
    return load_fixture("workday_raymondjames_no_locationstext.json")
