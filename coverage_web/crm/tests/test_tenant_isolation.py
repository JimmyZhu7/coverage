"""Tenant-isolation tests (docs/build-plan.md §2's "Deliberate calls" and
§9's testing posture): user B must never retrieve user A's private-zone
rows, and unscoped access through the default manager must fail loudly
rather than silently return everything.

Parametrized across every private-zone model (docs/build-plan.md §2's
private zone) — the task brief calls out contact/touch/capture_event/
task/fit_score explicitly as mandatory; user_firms/user_opportunities/
product_events/imports are included too since they carry the exact same
`user_id` denormalization and the same risk.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import FitScore, Import, ProductEvent, UserOpportunity
from coverage_web.tenancy import TenantScopeError
from crm.models import CaptureEvent, Contact, Task, Touch, UserFirm
from directory.models import Firm, Opportunity

User = get_user_model()


def _firm() -> Firm:
    firm, _ = Firm.objects.get_or_create(slug="acme", defaults={"name": "Acme Corp"})
    return firm


def _opportunity() -> Opportunity:
    opportunity, _ = Opportunity.objects.get_or_create(
        firm=_firm(),
        url="https://acme.example/jobs/summer-analyst",
        defaults={"title": "Summer Analyst"},
    )
    return opportunity


def _make_contact(user) -> Contact:
    return Contact.all_objects.create(user=user, name=f"Contact for {user.email}")


def _make_touch(user) -> Touch:
    return Touch.all_objects.create(
        user=user,
        contact=_make_contact(user),
        ts=timezone.now(),
        kind="outreach",
        channel="email",
    )


def _make_capture_event(user) -> CaptureEvent:
    return CaptureEvent.all_objects.create(
        user=user, provider="bcc", provider_ref=f"ref-{user.email}"
    )


def _make_task(user) -> Task:
    return Task.all_objects.create(user=user, title="Follow up")


def _make_user_firm(user) -> UserFirm:
    return UserFirm.all_objects.create(user=user, firm=_firm())


def _make_user_opportunity(user) -> UserOpportunity:
    return UserOpportunity.all_objects.create(user=user, opportunity=_opportunity())


def _make_fit_score(user) -> FitScore:
    return FitScore.all_objects.create(user=user, subject_type="contact", subject_id=1)


def _make_product_event(user) -> ProductEvent:
    return ProductEvent.all_objects.create(user=user, event="signup")


def _make_import(user) -> Import:
    return Import.all_objects.create(user=user, kind="csv", filename="contacts.csv")


PRIVATE_MODELS = [
    pytest.param(Contact, _make_contact, id="Contact"),
    pytest.param(Touch, _make_touch, id="Touch"),
    pytest.param(CaptureEvent, _make_capture_event, id="CaptureEvent"),
    pytest.param(Task, _make_task, id="Task"),
    pytest.param(FitScore, _make_fit_score, id="FitScore"),
    pytest.param(UserFirm, _make_user_firm, id="UserFirm"),
    pytest.param(UserOpportunity, _make_user_opportunity, id="UserOpportunity"),
    pytest.param(ProductEvent, _make_product_event, id="ProductEvent"),
    pytest.param(Import, _make_import, id="Import"),
]


@pytest.fixture
def user_a(db):
    return User.objects.create_user(email="isolation-a@example.com", password="x")


@pytest.fixture
def user_b(db):
    return User.objects.create_user(email="isolation-b@example.com", password="x")


@pytest.mark.django_db
@pytest.mark.parametrize("model, make", PRIVATE_MODELS)
def test_default_manager_refuses_unscoped_access(model, make, user_a):
    """`Model.objects` (TenantManager) must never silently hand back an
    unscoped queryset — every queryset-producing call raises, forcing
    `.for_user(...)` (or the explicit `all_objects` escape hatch)."""
    make(user_a)

    with pytest.raises(TenantScopeError):
        model.objects.all()

    with pytest.raises(TenantScopeError):
        model.objects.count()

    with pytest.raises(TenantScopeError):
        model.objects.filter(pk=1)


@pytest.mark.django_db
@pytest.mark.parametrize("model, make", PRIVATE_MODELS)
def test_for_user_never_returns_another_tenants_row(model, make, user_a, user_b):
    row_a = make(user_a)
    row_b = make(user_b)

    ids_scoped_to_a = set(model.objects.for_user(user_a).values_list("pk", flat=True))
    ids_scoped_to_b = set(model.objects.for_user(user_b).values_list("pk", flat=True))

    assert row_a.pk in ids_scoped_to_a
    assert row_a.pk not in ids_scoped_to_b
    assert row_b.pk in ids_scoped_to_b
    assert row_b.pk not in ids_scoped_to_a


@pytest.mark.django_db
@pytest.mark.parametrize("model, make", PRIVATE_MODELS)
def test_for_user_accepts_a_raw_user_id(model, make, user_a):
    """`.for_user()` accepts either a user instance or a raw id — services
    code (see crm/services.py) often only has the id on hand."""
    row = make(user_a)

    assert row.pk in set(model.objects.for_user(user_a.pk).values_list("pk", flat=True))


@pytest.mark.django_db
@pytest.mark.parametrize("model, make", PRIVATE_MODELS)
def test_all_objects_is_the_unscoped_admin_escape_hatch(model, make, user_a, user_b):
    """`all_objects` is the deliberate, visible, cross-tenant path (admin/
    internal use) — it must see both tenants' rows at once."""
    row_a = make(user_a)
    row_b = make(user_b)

    all_ids = set(model.all_objects.values_list("pk", flat=True))
    assert row_a.pk in all_ids
    assert row_b.pk in all_ids
