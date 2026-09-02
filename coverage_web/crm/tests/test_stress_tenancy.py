"""Adversarial tenancy suite — the highest-severity bug class in this app.

`test_tenant_isolation.py` covers 8 hand-listed models against 3 queryset
methods. That leaves the interesting questions unasked: it cannot notice a
NEW `PrivateModel` subclass that forgets `class Meta(PrivateModel.Meta)`, it
does not walk the other ~15 queryset entry points, and it says nothing about
the two paths that bypass `TenantManager` entirely by Django's own design.

This file discovers its subject matter instead of listing it — every concrete
`PrivateModel` subclass in the installed app registry — so a model added
tomorrow is covered the moment it exists, which is the only version of this
test that stays true.

THE TWO STRUCTURAL HOLES pinned at the bottom are not bugs today; they are
places a bug could hide with nothing to stop it, and both come from
`PrivateModel.Meta` pinning `default_manager_name = "all_objects"`. That pin
is REQUIRED (Django's uniqueness validation and cascade-delete collector both
go through the default/base manager and would trip the guard otherwise), so
the holes cannot be closed by removing it. They are pinned here as
characterization tests so that if someone ever writes the traversal that
would leak, the assertion below is what tells them.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

from coverage_web.tenancy import PrivateModel, TenantManager, TenantScopeError
from crm.models import Contact, Task, Touch, UserFirm
from directory.models import Firm

User = get_user_model()


def _private_models():
    """Every concrete `PrivateModel` subclass Django knows about.

    Discovered, not listed. A hand-maintained list is exactly the thing that
    goes stale silently — the model added next week is the one nobody
    remembers to add here, and it is also the one whose isolation nobody has
    checked.
    """
    return sorted(
        (m for m in apps.get_models()
         if issubclass(m, PrivateModel) and not m._meta.abstract),
        key=lambda m: f"{m._meta.app_label}.{m.__name__}",
    )


ALL_PRIVATE = [
    pytest.param(m, id=f"{m._meta.app_label}.{m.__name__}") for m in _private_models()
]


def test_the_discovery_actually_found_the_private_zone():
    """A guard on the guard: if `_private_models()` ever returns nothing (an
    import reshuffle, an app removed from INSTALLED_APPS), every
    parametrized test below silently becomes zero tests and this file reports
    green while checking nothing."""
    found = _private_models()
    assert len(found) >= 20, f"only found {len(found)} private models: {found}"
    names = {m.__name__ for m in found}
    for expected in ("Contact", "Touch", "Task", "UserFirm", "Campaign",
                     "CampaignContact", "ChatDebrief", "UserOpportunity"):
        assert expected in names, f"{expected} is a private model and was not discovered"


# ===========================================================================
# INVARIANT 1 — every private model actually CARRIES the tenancy contract.
# A subclass that declares a bare `class Meta:` instead of
# `class Meta(PrivateModel.Meta)` silently loses both manager pins, and
# nothing else in the codebase would notice.
# ===========================================================================
@pytest.mark.parametrize("model", ALL_PRIVATE)
def test_every_private_model_keeps_both_manager_pins(model):
    assert model._meta.default_manager_name == "all_objects", (
        f"{model.__name__}.Meta does not subclass PrivateModel.Meta — the "
        "default-manager pin was lost, so Django's uniqueness validation and "
        "cascade-delete collector will hit TenantManager and raise"
    )
    assert model._meta.base_manager_name == "all_objects"
    assert isinstance(model.objects, TenantManager)
    assert type(model.all_objects) is models.Manager


@pytest.mark.parametrize("model", ALL_PRIVATE)
def test_every_private_model_has_a_real_indexed_user_column(model):
    """`PrivateModel`'s docstring promises `user_id` is "a real, denormalized,
    indexed column on every row" — that is what makes an isolation check a
    direct one. A model that stores its tenant indirectly (through a FK to a
    contact, say) cannot be scoped by `for_user` at all."""
    field = model._meta.get_field("user")
    assert isinstance(field, models.ForeignKey), model.__name__
    assert field.db_index or field.unique or field.primary_key, (
        f"{model.__name__}.user carries no index — every tenant scope on this "
        "table is a sequential scan"
    )


# ===========================================================================
# INVARIANT 2 — `.objects` has NO unscoped path. Not one entry point.
#
# `test_tenant_isolation.py` checks `.all()`, `.count()`, `.filter()`. The
# manager is built with `from_queryset`, so every OTHER queryset method is
# generated the same way and must raise the same way — but "must" is not
# "does" until something asserts it, and a hand-written override added to
# TenantManager later would not go through `get_queryset()`.
# ===========================================================================
_ENTRY_POINTS = [
    ("all", lambda m: m.objects.all()),
    ("filter", lambda m: m.objects.filter(pk=1)),
    ("exclude", lambda m: m.objects.exclude(pk=1)),
    ("get", lambda m: m.objects.get(pk=1)),
    ("first", lambda m: m.objects.first()),
    ("last", lambda m: m.objects.last()),
    ("count", lambda m: m.objects.count()),
    ("exists", lambda m: m.objects.exists()),
    ("none", lambda m: m.objects.none()),
    ("values", lambda m: m.objects.values("pk")),
    ("values_list", lambda m: m.objects.values_list("pk", flat=True)),
    ("aggregate", lambda m: m.objects.aggregate(n=models.Count("pk"))),
    ("annotate", lambda m: m.objects.annotate(n=models.Count("pk"))),
    ("order_by", lambda m: m.objects.order_by("pk")),
    ("iterator", lambda m: list(m.objects.iterator())),
    ("in_bulk", lambda m: m.objects.in_bulk([1])),
    ("dates", lambda m: m.objects.dates("id", "day")),
    ("create", lambda m: m.objects.create()),
    ("get_or_create", lambda m: m.objects.get_or_create(pk=1)),
    ("update_or_create", lambda m: m.objects.update_or_create(pk=1)),
    ("bulk_create", lambda m: m.objects.bulk_create([])),
    ("select_related", lambda m: m.objects.select_related()),
    ("prefetch_related", lambda m: m.objects.prefetch_related()),
    ("distinct", lambda m: m.objects.distinct()),
    ("using", lambda m: m.objects.using("default")),
    ("raw", lambda m: m.objects.raw("SELECT 1")),
    ("iteration", lambda m: list(m.objects.all())),
]


@pytest.mark.django_db
@pytest.mark.parametrize("name,call", [pytest.param(n, c, id=n) for n, c in _ENTRY_POINTS])
@pytest.mark.parametrize("model", ALL_PRIVATE)
def test_no_queryset_entry_point_escapes_the_tenant_guard(model, name, call):
    with pytest.raises(TenantScopeError):
        call(model)


@pytest.mark.django_db
@pytest.mark.parametrize("model", ALL_PRIVATE)
def test_for_user_is_the_only_way_through_and_it_scopes(model):
    """`for_user` deliberately does not call `get_queryset()`. Assert it both
    works AND emits a `user_id` predicate — a future refactor that routed it
    through an unfiltered queryset would still "work" and leak everything."""
    qs = model.objects.for_user(1)
    assert "user_id" in str(qs.query), (
        f"{model.__name__}.objects.for_user() produced a query with no "
        f"user_id predicate: {qs.query}"
    )


# ===========================================================================
# INVARIANT 3 — `for_user` on a signed-out request must not silently widen.
#
# `AnonymousUser.pk` is None, so `getattr(user, "pk", user)` resolves to None
# and the scope degrades to `WHERE user_id IS NULL`. For the models with a
# non-nullable `user` that is harmlessly empty. For the TWO with a nullable
# one — `analytics.ProductEvent` (the pre-signup funnel) and
# `billing.ProWaitlist` (logged-out "notify me" joins, WITH EMAIL ADDRESSES) —
# it returns every anonymous row in the table.
#
# No caller does this today: every non-`login_required` view guards first.
# This test states the shape so that the day one stops guarding, something
# fails here rather than on the page.
# ===========================================================================
def test_which_private_models_have_a_nullable_user_column():
    nullable = {m.__name__ for m in _private_models()
                if m._meta.get_field("user").null}
    assert nullable == {"ProductEvent", "ProWaitlist"}, (
        "a new private model gained a nullable `user`. `for_user(AnonymousUser)` "
        "degrades to `WHERE user_id IS NULL` on exactly these tables, so any "
        "new member needs its callers checked for a signed-out path."
    )


@pytest.mark.django_db
def test_for_user_with_an_anonymous_user_returns_the_anonymous_rows_not_a_tenants():
    """Characterization, not approval. The important half of the assertion is
    the second one: whatever `for_user(None)` returns, it must never contain a
    row belonging to a real user."""
    from django.contrib.auth.models import AnonymousUser

    from analytics.models import ProductEvent

    owner = User.objects.create_user(email="tenancy-owner@example.com", password="x")
    mine = ProductEvent.all_objects.create(user=owner, event="signup")
    anon = ProductEvent.all_objects.create(user=None, event="landing")

    got = set(ProductEvent.objects.for_user(AnonymousUser()).values_list("pk", flat=True))
    assert mine.pk not in got, "a signed-out scope reached a real tenant's row"
    assert anon.pk in got


# ===========================================================================
# INVARIANT 4 — no `for_user` queryset ever contains another tenant's row,
# for any model, through any of the read shapes an application uses.
# ===========================================================================
@pytest.fixture
def owner(db):
    return User.objects.create_user(email="stress-owner@example.com", password="x")


@pytest.fixture
def intruder(db):
    return User.objects.create_user(email="stress-intruder@example.com", password="x")


@pytest.fixture
def firm(db):
    f, _ = Firm.objects.get_or_create(slug="stressco", defaults={"name": "StressCo"})
    return f


def _populate(user, firm):
    """A small, realistic private graph for one tenant."""
    contact = Contact.all_objects.create(user=user, name=f"C:{user.email}", firm=firm)
    Touch.all_objects.create(user=user, contact=contact, ts=timezone.now(),
                             kind="outreach", channel="email")
    Task.all_objects.create(user=user, title=f"T:{user.email}", firm=firm)
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    return contact


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Contact, Touch, Task, UserFirm])
def test_no_read_shape_crosses_the_tenant_boundary(model, owner, intruder, firm):
    _populate(owner, firm)
    _populate(intruder, firm)

    mine = model.objects.for_user(owner)
    theirs = set(model.objects.for_user(intruder).values_list("pk", flat=True))

    assert set(mine.values_list("pk", flat=True)).isdisjoint(theirs)
    assert mine.count() == 1
    assert mine.exists()
    assert mine.first().user_id == owner.pk
    # A filter, an exclude, an order and a slice must all stay inside the scope.
    for qs in (mine.filter(pk__gte=0), mine.exclude(pk=-1),
               mine.order_by("-pk"), mine.all()[:10]):
        assert all(row.user_id == owner.pk for row in qs)
    # An aggregate over the scoped queryset counts only the tenant's rows.
    assert mine.aggregate(n=models.Count("pk"))["n"] == 1


@pytest.mark.django_db
def test_a_scoped_delete_cannot_reach_another_tenants_rows(owner, intruder, firm):
    _populate(owner, firm)
    _populate(intruder, firm)
    Contact.objects.for_user(owner).delete()
    assert Contact.objects.for_user(owner).count() == 0
    assert Contact.objects.for_user(intruder).count() == 1
    # The cascade took the owner's touches and left the intruder's.
    assert Touch.objects.for_user(intruder).count() == 1


@pytest.mark.django_db
def test_a_scoped_update_cannot_reach_another_tenants_rows(owner, intruder, firm):
    _populate(owner, firm)
    _populate(intruder, firm)
    Contact.objects.for_user(owner).update(name="renamed")
    assert Contact.objects.for_user(intruder).first().name != "renamed"


# ===========================================================================
# STRUCTURAL HOLE 1 — reverse related managers bypass the guard.
#
# `default_manager_name = "all_objects"` is what Django's
# `create_reverse_many_to_one_manager` builds from, so `firm.contacts.all()`
# returns EVERY tenant's contacts at that firm and raises nothing.
#
# `Firm` is shared-zone and `Contact`/`Task`/`CalendarEvent` all point at it
# with a `related_name`, so the traversal is one attribute access away
# anywhere a `Firm` is in hand. Nothing in the codebase writes it today.
# Pinned so that the assertion below is what a future author trips over.
# ===========================================================================
@pytest.mark.django_db
def test_reverse_traversal_from_a_shared_firm_is_unscoped_by_construction(
    owner, intruder, firm
):
    _populate(owner, firm)
    _populate(intruder, firm)

    leaked = firm.contacts.all()
    assert leaked.count() == 2, (
        "if this is now 1, reverse-manager scoping changed — delete this test "
        "and celebrate"
    )
    assert {c.user_id for c in leaked} == {owner.pk, intruder.pk}, (
        "REVERSE TRAVERSAL FROM A SHARED MODEL IS CROSS-TENANT. This is the "
        "documented consequence of PrivateModel.Meta's default_manager_name "
        "pin, which cannot be removed (Django's uniqueness validation and "
        "cascade collector need it). The rule application code must follow: "
        "never reach a private model through a shared model's related_name — "
        "go Contact.objects.for_user(user).filter(firm=firm) instead."
    )


@pytest.mark.django_db
def test_no_template_or_module_reaches_a_private_model_through_a_shared_one():
    """The rule above, enforced against the tree rather than trusted.

    Greps for `<shared object>.<private related_name>` in Python and in
    templates. A hit is not automatically a leak — but every hit needs a human
    to confirm the object on the left is a private one — so this fails on a
    NEW one rather than on the ones already reviewed.
    """
    import re
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # related_names on private models that hang off the SHARED `Firm` table.
    patterns = [r"firm\.contacts\b", r"firm\.tasks\b", r"firm\.calendar_events\b",
                r"\.firm\.contacts\b", r"opportunity\.user_opportunities\b"]
    hits = []
    for pattern in patterns:
        proc = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.html", "-E", pattern, str(root)],
            capture_output=True, text=True,
        )
        for line in proc.stdout.splitlines():
            if "/tests/" in line or line.startswith(str(Path(__file__))):
                continue
            hits.append(line)
    assert not hits, (
        "a shared-model reverse traversal into the private zone appeared:\n"
        + "\n".join(hits)
        + "\n\nThese managers are UNSCOPED (see "
        "test_reverse_traversal_from_a_shared_firm_is_unscoped_by_construction). "
        "Rewrite as `<PrivateModel>.objects.for_user(user).filter(firm=...)`."
    )


# ===========================================================================
# STRUCTURAL HOLE 2 — ModelForm FK fields default to `all_objects`.
#
# Django builds a `ModelChoiceField` queryset from `_default_manager`, so any
# `ModelForm` exposing a FK to a `PrivateModel` gets a cross-tenant dropdown
# AND accepts another tenant's pk on POST, with no exception raised.
#
# Closed today only by author discipline in `crm/forms.py`. This asserts the
# discipline rather than the framework.
# ===========================================================================
@pytest.mark.django_db
def test_every_modelform_fk_to_a_private_model_is_explicitly_rescoped(owner, intruder, firm):
    """Walk every ModelForm in the project. For each field whose queryset
    model is a `PrivateModel`, instantiate the form and assert the queryset
    carries a `user_id` predicate — i.e. the author overrode it."""
    import importlib
    import inspect

    from django.forms import ModelForm
    from django.forms.models import ModelChoiceField

    _populate(owner, firm)
    _populate(intruder, firm)

    offenders = []
    for module_name in ("crm.forms", "accounts.forms", "directory.forms",
                        "capture.forms", "billing.forms", "assistant.forms"):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, ModelForm) or obj is ModelForm:
                continue
            try:
                form = obj(user=owner)
            except TypeError:
                try:
                    form = obj()
                except Exception:
                    continue
            except Exception:
                continue
            for name, field in form.fields.items():
                if not isinstance(field, ModelChoiceField):
                    continue
                model = field.queryset.model if field.queryset is not None else None
                if model is None or not issubclass(model, PrivateModel):
                    continue
                if "user_id" not in str(field.queryset.query):
                    offenders.append(f"{obj.__module__}.{obj.__name__}.{name} "
                                     f"-> unscoped {model.__name__}")
    assert not offenders, (
        "a ModelForm exposes an unscoped queryset over a private model. "
        "Django builds ModelChoiceField querysets from `_default_manager`, "
        "which PrivateModel.Meta pins to `all_objects` — so this dropdown "
        "lists every tenant's rows and the field validates another tenant's "
        "pk on POST. Override it in __init__ with "
        "`Model.objects.for_user(user)`:\n  " + "\n  ".join(offenders)
    )


# ===========================================================================
# INVARIANT 5 — the `all_objects` escape hatch stays greppable and rare.
#
# The manager's whole justification is that reaching for it by name is "a
# visible, greppable admission". That only holds while the count is small
# enough for a human to review. This is a ratchet, not a limit: it exists so
# that a batch of new unscoped calls has to be noticed and argued for.
# ===========================================================================
def test_the_unscoped_escape_hatch_has_not_quietly_proliferated():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", r"all_objects\.", str(root)],
        capture_output=True, text=True,
    )
    lines = [
        line for line in proc.stdout.splitlines()
        if "/tests/" not in line and "/migrations/" not in line
    ]
    # Measured at 77 lines on the audit of 2026-08-27, raised to 84 the same
    # night after several concurrent merges (the bench/park dismissal writes
    # in crm/today.py, the firm-tier setter in crm/views.py, and others) each
    # added one properly-scoped call, then to 87 on 2026-08-28 for 3 more of
    # the same shape. A handful of the count are prose mentions inside
    # docstrings rather than calls; the grep is deliberately blunt so it
    # cannot be evaded by formatting.
    #
    # Raised to 96 on 2026-08-29: merging four overnight cleanup sweeps back
    # into main (each done in its own worktree, all branched before the
    # night's other concurrent merges) surfaced 9 net-new call sites diffed
    # by CONTENT against the 2026-08-28 baseline, not by line number, since
    # unrelated edits elsewhere in the same files had shifted most of the 84
    # to new line numbers without changing them. Every one of the 9 reads:
    # the analytics pilot-funnel dashboard's per-user drilldown queries
    # (`user_id__in=ids` / `user_id=subject.id`, analytics/views.py, a staff
    # view scoped to explicit ids gathered earlier in the same function), the
    # calendar-invite dedup split in capture/gmail.py (`user=user` on both
    # halves of what used to be one call), and this same night's gmail_poll
    # import ledger (`user=connection.user`, capture/management/commands/
    # gmail_poll.py) -- none is a new unscoped read or write.
    #
    # Raised to 97 on 2026-09-01: the new `audit_chat_claims` management
    # command (capture/management/commands/audit_chat_claims.py) reads
    # CalendarEvent and Touch rows through `all_objects` because both queries
    # run against a `contact` already pulled from `Contact.objects.for_user
    # (user)` a few lines up, so `all_objects` here is not an unscoped
    # cross-tenant read. Both calls carry an explicit `user=user` predicate
    # anyway (added alongside this ratchet bump), matching every other call
    # site's style rather than leaning on the contact FK alone.
    #
    # Raised to 98 on 2026-09-01 (second raise that day): the Reschedule
    # handler in crm/today.py now moves the live chat CalendarEvent instead
    # of logging a dateless touch, and when no live event exists it CREATES
    # one through `all_objects` -- the same create-only exception every other
    # writer in that module makes, because the tenant manager raises on an
    # unscoped queryset and a create is not a query. The row carries
    # `user=user` explicitly, and the contact it hangs off was already pulled
    # through `Contact.objects.for_user(user)`. Not a cross-tenant read or
    # write.
    #
    # A RATCHET, not a limit. The headroom is small on purpose: this is meant
    # to fire on the next batch of unscoped calls so somebody looks at them,
    # which is the whole justification for `all_objects` being greppable.
    # Raising the number is a legitimate response — AFTER reading the diff.
    assert len(lines) <= 98, (
        f"{len(lines)} unscoped `all_objects` lines, up from the 98 reviewed "
        "on 2026-09-01. Each new call site needs an explicit `user=` predicate "
        "or a written cross-tenant justification — read the diff, then raise "
        "this number deliberately."
    )
