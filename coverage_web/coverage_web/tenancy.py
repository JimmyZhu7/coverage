"""Tenant-isolation primitives for private-zone models.

docs/build-plan.md §2 draws a hard line between the shared zone (no
`user_id`, written only by the central worker) and the private zone (every
row carries a denormalized `user_id`, "so isolation checks and indexes are
direct"). §2's "Deliberate calls" section and §9's testing posture both
call for the *same* guarantee: querying a private-zone model without a
tenant scope must not be a possible mistake to make silently — "a Django
manager that refuses unscoped queries on private models."

Every private-zone model inherits `PrivateModel` below instead of using
`models.Model` directly. That buys:

- `Model.objects` is a `TenantManager`. Calling *any* queryset-producing
  method on it (`.all()`, `.filter()`, `.get()`, `.count()`, ...) reaches
  `get_queryset()` first — which unconditionally raises `TenantScopeError`.
  There is no unscoped path through `objects`; the only way to get a
  queryset out of it is `.for_user(user)`.
- `Model.objects.for_user(user)` — the one supported way for application
  code to query a private model. Accepts either a user instance or a raw
  user_id.
- `Model.all_objects` — a plain, unscoped `Manager`. The deliberate escape
  hatch for the two legitimate unscoped callers: Django's own internals
  (uniqueness validation, the cascade-delete collector — see
  `default_manager_name` / `base_manager_name` below, both pinned to this
  manager so those internals never trip the `TenantManager` guard) and
  founder/admin support tooling that genuinely needs to see across
  tenants. Reaching for `all_objects` by name is a visible, greppable
  admission of "this deliberately isn't scoped" — that visibility is the
  point, not a loophole to route around.

Concrete models declare their own `Meta` as
`class Meta(PrivateModel.Meta): db_table = "..."` (etc.) to keep the
`default_manager_name` / `base_manager_name` pins while setting their own
table name — Django's abstract-base Meta inheritance does not carry
forward automatically unless the child's Meta explicitly subclasses the
parent's.
"""

from __future__ import annotations

from django.db import models


class TenantScopeError(Exception):
    """Raised when a private-zone model is queried without a tenant scope.

    Not something application code should catch and handle — it means the
    call site has a bug. Every access to a private-zone model must go
    through `.for_user(user)`, or explicitly (and visibly) opt into
    `Model.all_objects` for a deliberate cross-tenant reason.
    """


class TenantQuerySet(models.QuerySet):
    def for_user(self, user) -> "TenantQuerySet":
        """Scope this queryset to one tenant. `user` may be a user
        instance/id — either works since we only ever need `.pk`."""
        user_id = getattr(user, "pk", user)
        return self.filter(user_id=user_id)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Default manager for private-zone models — see module docstring."""

    def get_queryset(self):
        raise TenantScopeError(
            f"{self.model.__name__}.objects can't be queried directly. Use "
            f"{self.model.__name__}.objects.for_user(user) to scope by "
            f"tenant, or {self.model.__name__}.all_objects for a "
            f"deliberate, explicit cross-tenant (admin/internal) query."
        )

    def for_user(self, user):
        # Deliberately does NOT go through self.get_queryset() (which
        # always raises) — this is the one method on this manager allowed
        # to build a real queryset, and only ever a pre-scoped one.
        user_id = getattr(user, "pk", user)
        return TenantQuerySet(self.model, using=self._db).filter(user_id=user_id)


class PrivateModel(models.Model):
    """Abstract base for every private-zone table (docs/build-plan.md §2).
    Concrete subclasses must have a `user` ForeignKey — nullable only where
    anonymous rows are a legitimate case (`product_events`'s pre-signup
    funnel events; `billing.ProWaitlist`'s logged-out "notify me" joins) —
    so `user_id` is a real, denormalized, indexed column on every row.
    """

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "all_objects"
        base_manager_name = "all_objects"
