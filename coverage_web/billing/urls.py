from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("checkout/<str:pack_key>/", views.checkout, name="checkout"),
    path("webhook/", views.webhook, name="webhook"),
    path("waitlist/join/", views.waitlist_join, name="waitlist_join"),
]
