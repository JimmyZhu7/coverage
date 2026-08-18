from django.urls import path

from capture import views

app_name = "capture"

urlpatterns = [
    # Inbound-email webhook (POST, CSRF-exempt, shared-secret authenticated).
    # Full path under the project mount is /capture/inbound/.
    path("inbound/", views.inbound, name="inbound"),
    # "Is my capture working?" strip.
    path("health/", views.health, name="health"),
    # needs_review one-click confirmation queue.
    path("review/", views.review, name="review"),
    path("review/<int:event_id>/confirm/", views.confirm, name="confirm"),
    path("review/<int:event_id>/ignore/", views.ignore, name="ignore"),
    # Gmail Live (docs/build-plan.md §5's "v2") — a separate incremental
    # OAuth consent, never the login flow.
    path("gmail/connect/", views.gmail_connect, name="gmail_connect"),
    path("gmail/callback/", views.gmail_callback, name="gmail_callback"),
    path("gmail/disconnect/", views.gmail_disconnect, name="gmail_disconnect"),
]
