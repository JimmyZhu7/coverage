"""render.yaml — the four facts about the deploy blueprint that code
elsewhere depends on being true.

WHY TESTED AT ALL. Every other file in this repo has a test that fails when
someone undoes its fix. `render.yaml` had none, and three of the defects
this suite now covers were defects IN it: two crons in the wrong order, two
crons on the same tick, and a cache that was never provisioned. Each is a
one-line edit away from coming back, and none of them would fail anything.

PARSED BY HAND, not with PyYAML: this project has no YAML dependency (see
`grep -rn "import yaml"` — zero hits, the firm seeds are read by the
connectors package's own reader) and adding one so a test can read a config
file is a poor trade. The parse below is deliberately dumb — it finds
`name:`/`schedule:`/`key:` lines and nothing else — and it asserts on the
handful of scalars this file cares about. A structural change to the
blueprint that broke this parse would fail loudly, which is the correct
outcome for a file whose shape these tests are about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# coverage_web/ops/tests/ -> repo root.
RENDER_YAML = Path(__file__).resolve().parents[3] / "render.yaml"


@pytest.fixture(scope="module")
def blueprint() -> str:
    assert RENDER_YAML.exists(), f"render.yaml not found at {RENDER_YAML}"
    return RENDER_YAML.read_text()


def _blocks(text: str) -> dict[str, str]:
    """`name:` -> everything until the next `- type:` line. Good enough to
    ask "does this service mention that key", which is all these tests do."""
    blocks: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if re.match(r"\s*-\s+type:\s", line):
            current = None
        match = re.match(r"\s*name:\s*(\S+)\s*$", line)
        if match and current is None:
            current = match.group(1)
            blocks[current] = ""
            continue
        if current:
            blocks[current] += line + "\n"
    return blocks


def _schedule(text: str, service: str) -> str:
    block = _blocks(text)[service]
    match = re.search(r'schedule:\s*"([^"]+)"', block)
    assert match, f"{service} has no schedule"
    return match.group(1)


# ---------------------------------------------------------------------------
# T2 — the daily 05:00 block's ordering
# ---------------------------------------------------------------------------
def test_trial_expiry_runs_before_the_watch_renewal(blueprint):
    """These ran the other way round — renew 05:00, expire 05:30 — so a
    trial that ended overnight got one last 7-day watch renewal half an hour
    before the plan flip meant to stop it. `gmail_watch_renew` renews every
    `plan="pro"` connection, so the job that DECIDES who is Pro has to land
    before the job that ACTS on it."""
    expire = _schedule(blueprint, "coverage-pro-trial-expire")
    renew = _schedule(blueprint, "coverage-gmail-watch-renew")

    expire_minute, expire_hour = expire.split()[0], expire.split()[1]
    renew_minute, renew_hour = renew.split()[0], renew.split()[1]

    assert expire_hour == renew_hour == "5"
    assert int(expire_minute) < int(renew_minute), (
        f"trial-expire ({expire}) must run before watch-renew ({renew})"
    )


# ---------------------------------------------------------------------------
# T4 — the two credit-spending crons must not share a tick
# ---------------------------------------------------------------------------
def test_the_two_credit_clamping_crons_are_offset(blueprint):
    """Both pre-clamp their work against the same student's balance
    (billing.credits.affordable_*). The real fix is at the debit
    (`_spend_clamped` re-runs the clamp under a row lock); this offset is
    belt and braces, and it is only belt and braces if it is actually there."""
    backfill = _schedule(blueprint, "coverage-gmail-backfill")
    autopilot = _schedule(blueprint, "coverage-autopilot")

    assert backfill != autopilot, (
        "gmail-backfill and autopilot are back on the same tick"
    )
    # Both must still be every five minutes — a student is watching each of
    # them, and the offset is not licence to slow either down.
    assert backfill.startswith("*/5")
    assert autopilot.split()[0].endswith("/5")


# ---------------------------------------------------------------------------
# The shared cache
# ---------------------------------------------------------------------------
def test_a_key_value_store_is_declared(blueprint):
    """Was a `sync: false` REDIS_URL with a comment telling you to create the
    store by hand — which meant every deploy so far ran on LocMemCache, so
    allauth's "5 failed logins per 5 minutes" was really 15 (one allowance
    per gunicorn worker) and reset on every deploy."""
    assert "type: keyvalue" in blueprint
    assert "name: coverage-kv" in blueprint


@pytest.mark.parametrize("service", ["coverage-web", "coverage-gmail-live"])
def test_the_long_lived_services_read_redis_url_from_that_store(blueprint, service):
    """Named exactly REDIS_URL: settings/base.py reads it, and django-axes
    is being wired to the same setting."""
    block = _blocks(blueprint)[service]

    assert "key: REDIS_URL" in block
    assert "coverage-kv" in block, f"{service}'s REDIS_URL is not wired to the store"


# ---------------------------------------------------------------------------
# T7 — the keys that were read by code and absent from this file
# ---------------------------------------------------------------------------
def test_site_url_is_declared_on_the_web_service(blueprint):
    """Absent entirely, so every weekly-digest link pointed at
    http://localhost:8000 — base.py's dev default."""
    assert "key: SITE_URL" in _blocks(blueprint)["coverage-web"]


def test_site_url_and_email_are_declared_on_the_trial_expiry_cron(blueprint):
    """That cron now sends the trial-ended email, so it needs a relay and a
    host for the link in it."""
    block = _blocks(blueprint)["coverage-pro-trial-expire"]

    assert "key: EMAIL_URL" in block
    assert "key: SITE_URL" in block


@pytest.mark.parametrize("key", ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"])
def test_stripe_keys_are_declared_even_though_they_are_blank(blueprint, key):
    """Declared while blank on purpose: a key typed into the dashboard but
    absent from this file is a key the next Blueprint apply can drop."""
    assert f"key: {key}" in _blocks(blueprint)["coverage-web"]
