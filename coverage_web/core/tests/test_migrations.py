"""Guards against model changes that were never captured in a migration
file. docs/build-plan.md §9: "CI runs migrations against real Postgres" —
pytest-django's own test-DB setup already proves every checked-in
migration *applies* cleanly (see the task's manual verification too:
`manage.py migrate` against a freshly dropped/recreated database). This
test proves the complementary thing: that there ISN'T a model change
sitting un-migrated in the first place.
"""

import io

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_missing_migrations():
    out = io.StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
