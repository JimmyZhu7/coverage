# coverage_domain

Coverage's pure-logic core. State machine, cadence, apply layer, and scoring
live here as plain functions over DB-API 2.0 connections/cursors — no Django
import anywhere in this package, and no query without an explicit `user_id`
tenancy scope. See `docs/build-plan.md` §1 and §4.

## Modules

- `pipeline.py` — the warmth/thread-state ratchet, ported from
  `campaign/src/campaign/pipeline.py` (`TOUCH_TRANSITIONS`, `WARMTH_RANK`,
  `apply_touch()`'s atomic `UPDATE ... CASE`, the terminal-`advocate` guard),
  translated to Postgres parameter style (`%s` / `%(name)s`) with a `user_id`
  parameter threaded through every query. Adds `set_state()` — the manual
  override path (ported from the CLI's `contact set`) — which now also
  inserts an audit touch, so the touches log has no gap. See the module's
  own docstring and `tests/test_pipeline.py` for the full behavior contract.

Cadence, the gmail-enrich-style apply layer, and the fit-score engine are
separate port/build items (`docs/build-plan.md` §4's port table) and are not
in this package yet.

## Testing

```
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e ".[dev]" --group dev
.venv/bin/pytest
```

The main suite (`tests/test_pipeline.py`) runs against an in-memory SQLite
engine wrapped in a paramstyle-translating shim by default — no external
services required — and additionally against real Postgres when reachable
(`COVERAGE_DOMAIN_TEST_DATABASE_URL`, skipped cleanly otherwise). See
`tests/conftest.py` and `tests/test_pipeline_postgres.py` for exactly what
each backend does and does not prove about the atomic update's concurrency
safety.
