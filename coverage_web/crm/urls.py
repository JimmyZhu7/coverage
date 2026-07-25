from django.urls import path

from . import views

app_name = "crm"

# Mounted at /app/ (see coverage_web/urls.py). Login required on every view.
urlpatterns = [
    # The authed hub: the weekly priority list (cadence.due_actions output).
    path("", views.week, name="week"),
    # Today cockpit quick actions (htmx): sent / reply / snooze / skip.
    path("today/<int:pk>/<str:verb>/", views.today_act, name="today_act"),
    # Post-chat debrief, keyed by the `chat` touch it belongs to (one
    # debrief per chat — see crm.models.ChatDebrief).
    path("debrief/<int:pk>/", views.debrief, name="debrief"),
    path("debrief/<int:pk>/dismiss/", views.debrief_dismiss, name="debrief_dismiss"),
    path("debrief/<int:pk>/promote/", views.debrief_promote, name="debrief_promote"),
    path("contacts/", views.contact_list, name="contact_list"),
    # Hand-add / edit a contact — the coffee-chat entry path.
    path("contacts/new/", views.contact_new, name="contact_new"),
    # Drag-and-drop tier changes from the Network board (POST firm+tier).
    path("firms/tier/", views.set_firm_tier, name="set_firm_tier"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    # htmx: log a touch, re-render the live panel with visible warmth movement.
    path("contacts/<int:pk>/touch/", views.log_touch, name="log_touch"),
]
