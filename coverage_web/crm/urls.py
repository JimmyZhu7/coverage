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
    # htmx: today's brief, generated off the page's own response so an LLM
    # call can never sit in front of the morning load. POST because it can
    # spend money — same rule as the contact AI endpoints below.
    path("today/brief/", views.daily_brief, name="daily_brief"),
    path("today/<int:pk>/<str:verb>/", views.today_act, name="today_act"),
    # Post-chat debrief, keyed by the `chat` touch it belongs to (one
    # debrief per chat — see crm.models.ChatDebrief).
    # Contact proposals found in the mailbox (capture.discovery) — one-tap
    # accept/dismiss from the Today page. Literal "bulk" segment first so the
    # int converter can't shadow it.
    path("proposals/bulk/<str:verb>/", views.proposals_bulk, name="proposals_bulk"),
    # The in-the-moment Undo strip (ids in the POST body, see
    # crm.views._dismiss_undo_offer) and the durable Settings restore. Both
    # literal segments, declared ahead of the int converter for the same
    # reason "bulk" is.
    path("proposals/undo/", views.proposals_undo, name="proposals_undo"),
    path("proposals/<int:pk>/restore/", views.proposal_restore, name="proposal_restore"),
    path("proposals/<int:pk>/<str:verb>/", views.proposal_act, name="proposal_act"),
    # Autopilot (capture.autopilot): the one-tap apply over a reviewed run,
    # the activity ledger with every decision's quote, and the per-decision
    # undo that doubles as the permanent override.
    path("autopilot/", views.autopilot_log, name="autopilot_log"),
    path("autopilot/<int:pk>/apply/", views.autopilot_apply,
         name="autopilot_apply"),
    path("autopilot/decision/<int:pk>/undo/", views.autopilot_undo,
         name="autopilot_undo"),
    # Application-status mail (capture.appmail) — the other half of the same
    # door: a proposed pipeline move, committed only on the tap. No bulk
    # verb; each card is a different role at a different stage, and "accept
    # all" over a student's whole funnel is not a decision worth batching.
    path("applications/<int:pk>/<str:verb>/", views.app_event_act, name="app_event_act"),
    # Mail facts (capture.mailfacts) — what a message stated about a person,
    # acted on with a grounded quote. Undo reverses the automatic action;
    # dismiss acknowledges the card.
    path("mailfacts/<int:pk>/<str:verb>/", views.mail_fact_act, name="mail_fact_act"),
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
    # The same trapdoor problem for the other way a contact leaves the board:
    # answering "that bulk send was not my recruiting" in Settings now takes
    # its originating contacts off the Network board, and this is where the
    # board's caveat line points so the answer can be audited and undone one
    # person at a time. Literal-before-converter, same as the line above.
    path("contacts/not-recruiting/", views.contact_campaign_hidden,
         name="contact_campaign_hidden"),
    path("contacts/<int:pk>/keep/", views.contact_campaign_keep,
         name="contact_campaign_keep"),
    # And the third way off the board: the person themselves is not related
    # to the user's recruiting (crm/recruitment.py — the founder's "any
    # unrelated should not show up" rule). Same ledger-plus-one-click-back
    # shape as the two lists above.
    path("contacts/unrelated/", views.contact_unrelated,
         name="contact_unrelated"),
    path("contacts/<int:pk>/recruitment-keep/", views.contact_unrelated_keep,
         name="contact_unrelated_keep"),
    # Multi-select bulk verbs from the Network board (snooze / park /
    # archive). Same literal-before-converter ordering as the line above.
    # Duplicate cards: merge / "different people" taps and the merge undo,
    # all reached from Settings (see accounts settings.html #duplicates).
    path("contacts/merge/undo/<int:pk>/", views.contact_merge_undo,
         name="contact_merge_undo"),
    path("contacts/merge/<str:verb>/", views.contact_merge_act,
         name="contact_merge_act"),
    path("contacts/bulk/", views.contacts_bulk, name="contacts_bulk"),
    path("contacts/<int:pk>/archive/", views.contact_archive, name="contact_archive"),
    path("contacts/<int:pk>/unarchive/", views.contact_unarchive, name="contact_unarchive"),
    # Drag-and-drop tier changes from the Network board, and Settings'
    # Target Firms add/retier controls (POST firm+tier).
    path("firms/tier/", views.set_firm_tier, name="set_firm_tier"),
    # Settings' Target Firms remove control (POST firm).
    path("firms/remove/", views.remove_target_firm, name="remove_target_firm"),
    # Settings' Campaigns card: answer the one question about a detected bulk
    # send (POST campaign + kind). See crm/campaigns.py.
    path("campaigns/classify/", views.classify_campaign, name="classify_campaign"),
    # Fire-and-forget instrumentation for the Coverage Gaps card's "Who to
    # find" panel: opened, and left for a LinkedIn search. Writes one
    # product event and nothing else.
    path("firms/sourcing-event/", views.sourcing_event, name="sourcing_event"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    # htmx: log a touch, re-render the live panel with visible warmth movement.
    path("contacts/<int:pk>/touch/", views.log_touch, name="log_touch"),
    # htmx: save the Compose draft (contact.opener) in place.
    path("contacts/<int:pk>/opener/", views.contact_opener, name="contact_opener"),
    path("contacts/<int:pk>/ai-brief/", views.contact_ai_brief, name="contact_ai_brief"),
    # htmx: (re)write the AI relationship summary. POST for the same reason
    # ai-brief is — a paid call must never fire from a prefetch or a crawl.
    path("contacts/<int:pk>/ai-summary/", views.contact_ai_summary, name="contact_ai_summary"),
]
