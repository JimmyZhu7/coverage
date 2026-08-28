"""Deprecated entry point — the seeding logic now lives in the real
management command `accounts.management.commands.seed_demo`, so it is
discoverable as `manage.py seed_demo` instead of only reachable by piping
this file into a shell. Kept here, thin, so `manage.py shell <
scripts/demo_seed.py` (docs/see-it-locally.md's old instruction, anything
else that still calls it that way) keeps working.

Prefer: python coverage_web/manage.py seed_demo
"""
from django.core.management import call_command

call_command("seed_demo")
