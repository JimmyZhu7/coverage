"""
Proves the pytest + pytest-django harness works end to end: real Django
settings load, the URLconf resolves, the view executes, and a real HTTP
response comes back through the Django test client.

Deliberately does not use the `django_db` marker or a DB-backed fixture:
`healthz` never touches the database (see core/views.py), so this test has no
Postgres dependency and can run in any environment where the app's Python
dependencies are installed — including one with no Postgres server at all.
DB-dependent tests (models, tenant isolation per docs/build-plan.md §9) land
alongside coverage_domain's schema in M1+.
"""


def test_healthz_returns_200_ok_json(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_healthz_reverses_by_name(client):
    from django.urls import reverse

    response = client.get(reverse("core:healthz"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
