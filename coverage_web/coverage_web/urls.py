"""
Root URL configuration for coverage_web.
"""
from django.contrib import admin
from django.urls import include, path

from directory import views as directory_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # django-allauth: login/logout/signup + Google social-auth callback.
    path("accounts/", include("allauth.urls")),
    # The opportunities feed (insight programmes / internships /
    # entry-level) — the star page: an urgency feed ranked by deadline then
    # freshness, served by the directory app's list view.
    path("opportunities/", directory_views.opportunities, name="opportunities"),
    # Per-firm detail pages linked from the feed.
    path("firms/", include("directory.urls")),
    path("app/", include("crm.urls")),               # authed hub: today, network
    path("welcome/", include("accounts.urls")),      # onboarding, import, settings, delete/export
    path("capture/", include("capture.urls")),       # inbound-email webhook + capture settings
    path("", include("core.urls")),
]
