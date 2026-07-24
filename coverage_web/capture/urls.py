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
]
