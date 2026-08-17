from django.urls import path

from . import views

app_name = "assistant"

# Mounted at /assistant/ (see coverage_web/urls.py). Login required on every
# view: the whole page reads one student's private CRM.
urlpatterns = [
    path("", views.chat, name="chat"),
    # POST only — every send costs money (see views.py).
    path("send/", views.send, name="send"),
    path("new/", views.new_conversation, name="new"),
]
