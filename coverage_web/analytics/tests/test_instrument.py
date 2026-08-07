"""The founder dashboard: staff-only, and reads rows the app already writes."""

from __future__ import annotations

import pytest

from analytics.events import record_event

pytestmark = pytest.mark.django_db


def test_the_instrument_is_staff_only(client, django_user_model):
    assert client.get("/instrument/").status_code == 302, "anonymous is turned away"

    plain = django_user_model.objects.create_user(email="plain@x.com", password="x")
    client.force_login(plain)
    assert client.get("/instrument/").status_code == 302, "a signed-in user is not staff"

    staff = django_user_model.objects.create_user(email="staff@x.com", password="x")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)
    assert client.get("/instrument/").status_code == 200


def test_it_reports_what_the_product_recorded(client, django_user_model):
    staff = django_user_model.objects.create_user(email="staff2@x.com", password="x")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    for _ in range(3):
        record_event("touch_logged", user=staff, source="today")
    record_event("opportunity_tracked", user=staff, status="saved")

    client.force_login(staff)
    body = client.get("/instrument/").content.decode()
    assert "touch_logged" in body and "opportunity_tracked" in body
    assert "Product events" in body


def test_a_pipeline_stage_that_never_ran_says_so(client, django_user_model):
    """The honest answer to "is the automation still running" — a page that
    showed a blank where a stage should be would read as a rendering bug."""
    staff = django_user_model.objects.create_user(email="staff3@x.com", password="x")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)
    body = client.get("/instrument/").content.decode()
    assert "never run" in body
