from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("pricing/", views.pricing, name="pricing"),
    path("search/", views.search, name="search"),
    path("healthz", views.healthz, name="healthz"),
]
