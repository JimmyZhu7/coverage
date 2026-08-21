from django.urls import path

from ops import views

app_name = "ops"

urlpatterns = [
    path("health/cron/", views.health_cron, name="health-cron"),
]
