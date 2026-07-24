from django.urls import path

from . import views

app_name = "crm"

# Mounted at /app/ (see coverage_web/urls.py). Login required on every view.
urlpatterns = [
    # The authed hub: the weekly priority list (cadence.due_actions output).
    path("", views.week, name="week"),
    # Today cockpit quick actions (htmx): sent / reply / snooze / skip.
    path("today/<int:pk>/<str:verb>/", views.today_act, name="today_act"),
    path("contacts/", views.contact_list, name="contact_list"),
    # Drag-and-drop tier changes from the Network board (POST firm+tier).
    path("firms/tier/", views.set_firm_tier, name="set_firm_tier"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    # htmx: log a touch, re-render the live panel with visible warmth movement.
    path("contacts/<int:pk>/touch/", views.log_touch, name="log_touch"),
]
