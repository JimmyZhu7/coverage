"""
Root URL configuration for coverage_web.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from analytics import views as analytics_views
from directory import views as directory_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # django-allauth: login/logout/signup + Google social-auth callback.
    path("accounts/", include("allauth.urls")),
    # The opportunities feed (insight programmes / internships /
    # entry-level) — the star page: an urgency feed ranked by deadline then
    # freshness, served by the directory app's list view.
    path("opportunities/", directory_views.opportunities, name="opportunities"),
    # The user's saved / applied roles, and the track toggle behind each card.
    path("opportunities/mine/", directory_views.my_applications, name="my_applications"),
    path("opportunities/<int:pk>/track/", directory_views.track_opportunity, name="track_opportunity"),
    # One click saves every open role whose own text names the user's class
    # year — the eligibility lens reaching the pipeline.
    path("opportunities/track-eligible/", directory_views.track_eligible, name="track_eligible"),
    # The description we already fetched, read inline instead of behind a
    # four-second Workday shell. Public: the posting itself is public.
    path("opportunities/<int:pk>/read/", directory_views.role_description, name="role_description"),
    # Per-firm detail pages linked from the feed.
    path("firms/", include("directory.urls")),
    path("app/", include("crm.urls")),               # authed hub: today, network
    path("welcome/", include("accounts.urls")),      # onboarding, import, settings, delete/export
    # The legal pages live under /welcome/, but /privacy/ and /terms/ are what
    # people type and what link scanners probe. Permanent redirects, not
    # duplicate routes: one canonical URL per document.
    # Chrome falls back to /favicon.ico after pushState history changes;
    # without this route that fallback 404s and blanks the tab icon.
    path("favicon.ico", RedirectView.as_view(url="/static/img/favicon.svg", permanent=True)),
    path("privacy/", RedirectView.as_view(pattern_name="accounts:privacy", permanent=True)),
    path("terms/", RedirectView.as_view(pattern_name="accounts:terms", permanent=True)),
    path("capture/", include("capture.urls")),       # inbound-email webhook + capture settings
    # The founder dashboard: staff-only reader over the ProductEvent rows
    # every action has been writing since the cutover.
    path("instrument/", analytics_views.dashboard, name="instrument"),
    path("", include("core.urls")),
]

# Avatars, DEBUG only — see MEDIA_ROOT's comment in settings/base.py for why
# production has no equivalent yet.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
