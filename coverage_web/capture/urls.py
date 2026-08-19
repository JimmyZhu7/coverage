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
]
