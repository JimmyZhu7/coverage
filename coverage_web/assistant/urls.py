from django.urls import path

from . import views

app_name = "assistant"

# Mounted at /assistant/ (see coverage_web/urls.py). Login required on every
# view: the whole page reads one student's private CRM.
urlpatterns = [
    path("", views.chat, name="chat"),
    # Same view as above, opened on a specific past conversation rather than
    # the newest one — the history panel's links.
    path("<int:conversation_id>/", views.chat, name="chat_conversation"),
    # POST only — every send costs money (see views.py).
    path("send/", views.send, name="send"),
    path("stream/", views.stream, name="stream"),
    path("history/", views.history_fragment, name="history"),
    path("new/", views.new_conversation, name="new"),
    path("rename/", views.rename_conversation, name="rename"),
    path("delete/", views.delete_conversation, name="delete"),
    path("move/", views.move_conversation, name="move"),
    path("folders/new/", views.create_folder, name="folder_new"),
    path("folders/rename/", views.rename_folder, name="folder_rename"),
    path("folders/delete/", views.delete_folder, name="folder_delete"),
    path("memories/forget/", views.forget_memory, name="forget_memory"),
    # The draft card's one-click "Log touch" — a real CRM write, no model
    # round trip, POST-only like every other write on this page.
    path("log-draft-touch/", views.log_draft_touch, name="log_draft_touch"),
]
