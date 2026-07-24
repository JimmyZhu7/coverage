from django.urls import path

from directory import views

app_name = "directory"

# Mounted under /firms/ by the root urlconf. The public listing lives at the
# top-level /opportunities/ (see coverage_web/urls.py) as an urgency feed;
# this app now only serves per-firm detail pages linked from that feed.
urlpatterns = [
    # A single firm's openings + cycle timeline.
    path("<slug:slug>/", views.firm_detail, name="firm_detail"),
]
