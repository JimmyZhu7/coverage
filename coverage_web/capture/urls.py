from django.urls import path

from capture import views

app_name = "capture"

urlpatterns = [
    # Gmail Live (docs/build-plan.md §5's "v2") — a separate incremental
    # OAuth consent, never the login flow.
    path("gmail/connect/", views.gmail_connect, name="gmail_connect"),
    path("gmail/callback/", views.gmail_callback, name="gmail_callback"),
    path("gmail/disconnect/", views.gmail_disconnect, name="gmail_disconnect"),
    path("gmail/rescan/", views.gmail_rescan, name="gmail_rescan"),

    # Google Calendar (capture/gcal_live.py) — a THIRD consent, separate
    # again. Its own routes rather than a mode on the Gmail ones, because
    # the two grants are stored separately and disconnecting one must not
    # be able to reach the other.
    path("calendar/connect/", views.gcal_connect, name="gcal_connect"),
    path("calendar/callback/", views.gcal_callback, name="gcal_callback"),
    path("calendar/disconnect/", views.gcal_disconnect, name="gcal_disconnect"),
]
