from django.urls import path

from . import calendar_views, views

app_name = "crm"

# Mounted at /app/ (see coverage_web/urls.py). Login required on every view.
urlpatterns = [
    # The authed hub: the weekly priority list (cadence.due_actions output).
    path("", views.week, name="week"),
    # Today cockpit quick actions (htmx): sent / reply / park / snooze / skip.
    # The literal segment is declared first so the <int:pk> converter can't
    # shadow it.
    path("today/park-all/", views.today_park_all, name="today_park_all"),
    path("today/<int:pk>/<str:verb>/", views.today_act, name="today_act"),
    # Post-chat debrief, keyed by the `chat` touch it belongs to (one
    # debrief per chat — see crm.models.ChatDebrief).
    path("debrief/<int:pk>/", views.debrief, name="debrief"),
    path("debrief/<int:pk>/dismiss/", views.debrief_dismiss, name="debrief_dismiss"),
    path("debrief/<int:pk>/promote/", views.debrief_promote, name="debrief_promote"),
    # The calendar: scheduled chats, hand-added events, confirmed deadlines.
    path("calendar/", calendar_views.calendar, name="calendar"),
    # Token-authenticated ICS feed — no session; calendar apps fetch from
    # their own servers. The token is the auth, so this is NOT login_required.
    path("calendar/feed/<str:token>.ics", calendar_views.calendar_ics, name="calendar_ics"),
    path("calendar/add/", calendar_views.calendar_add, name="calendar_add"),
    path("calendar/<int:pk>/delete/", calendar_views.calendar_delete, name="calendar_delete"),
    path("contacts/", views.contact_list, name="contact_list"),
    # Hand-add / edit a contact — the coffee-chat entry path.
    path("contacts/new/", views.contact_new, name="contact_new"),
    # The archived list + its two POST controls. `archived` had no UI at all
    # before these, which made it a one-way trapdoor — see crm/views.py.
    # Declared ahead of "contacts/<int:pk>/" so the literal segment can't be
    # shadowed by the int converter (it can't be, but the ordering keeps the
    # relationship obvious).
    path("contacts/archived/", views.contact_archived, name="contact_archived"),
    # Multi-select bulk verbs from the Network board (snooze / park /
    # archive). Same literal-before-converter ordering as the line above.
    path("contacts/bulk/", views.contacts_bulk, name="contacts_bulk"),
    path("contacts/<int:pk>/archive/", views.contact_archive, name="contact_archive"),
    path("contacts/<int:pk>/unarchive/", views.contact_unarchive, name="contact_unarchive"),
    # Drag-and-drop tier changes from the Network board, and Settings'
    # Target Firms add/retier controls (POST firm+tier).
    path("firms/tier/", views.set_firm_tier, name="set_firm_tier"),
    # Settings' Target Firms remove control (POST firm).
    path("firms/remove/", views.remove_target_firm, name="remove_target_firm"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    # htmx: log a touch, re-render the live panel with visible warmth movement.
    path("contacts/<int:pk>/touch/", views.log_touch, name="log_touch"),
    # htmx: save the Compose draft (contact.opener) in place.
    path("contacts/<int:pk>/opener/", views.contact_opener, name="contact_opener"),
    path("contacts/<int:pk>/ai-brief/", views.contact_ai_brief, name="contact_ai_brief"),
]
