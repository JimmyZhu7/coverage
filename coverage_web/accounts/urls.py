from django.urls import path

from . import views

app_name = "accounts"

# Mounted at /welcome/ (see coverage_web/urls.py). Everything is login-
# required except the two legal pages.
urlpatterns = [
    path("", views.onboarding, name="onboarding"),
    path("import/", views.import_contacts, name="import"),
    path("import/template/", views.import_template, name="import_template"),
    path("settings/", views.settings_view, name="settings"),
    # University autocomplete (datalist options) for the School field.
    path("universities/", views.university_search, name="university_search"),
    path("export/", views.export, name="export"),
    path("delete/", views.delete_account, name="delete"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
]
