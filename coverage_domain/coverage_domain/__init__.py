"""coverage_domain — Coverage's pure-logic core.

Framework-free modules over plain data / DB-API 2.0 connections. No Django
import anywhere; every private-data path carries an explicit `user_id` scope.

Modules:
  - pipeline: the warmth / thread-state ratchet (takes a DB connection; the
    one module here that WRITES). See docs/build-plan.md §4.
  - cadence:  the weekly-priority decision tree (`due_actions`) and the
    backward task planner (`tasks_from_change`, `plan_task_write`). Pure
    functions over plain data — reads and computes, never writes.
  - scoring:  the fit-score engine — Contact Warmth Score (`score_contact`)
    and Firm Fit Score (`score_firm`). Pure, deterministic, zero LLM. See
    docs/build-plan.md §6.
"""

from __future__ import annotations

from . import cadence, pipeline, scoring

__all__ = ["cadence", "pipeline", "scoring"]
