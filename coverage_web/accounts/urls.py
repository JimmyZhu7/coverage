from django.urls import path

from . import views

app_name = "accounts"

# Mounted at /welcome/ (see coverage_web/urls.py). Everything is login-
# required except the two legal pages and the digest unsubscribe link, which
# is opened from an inbox by a reader who may have no session at all (see
# accounts/unsubscribe.py).
urlpatterns = [
    path("", views.onboarding, name="onboarding"),
    # The wizard's live preview panel. Read-only; the wizard renders it
    # server-side on every step and htmx re-fetches it as answers change.
    path("preview/", views.onboarding_preview_view, name="onboarding_preview"),
    path("import/", views.import_contacts, name="import"),
    # The import summary's "Link to..." fix-up for a firm string that didn't
    # match the directory — re-points a batch of contacts' firm FK.
    path("import/link-firm/", views.import_link_firm, name="import_link_firm"),
    path("import/template/", views.import_template, name="import_template"),
    path("settings/", views.settings_view, name="settings"),
    # "Got it" on the trial-ended banner in the Credits card. POST-only, on
    # its own route rather than a settings query string — see the view.
    path(
        "settings/trial-notice/dismiss/",
        views.dismiss_trial_notice,
        name="dismiss_trial_notice",
    ),
    # University autocomplete (datalist options) for the School field.
    path("universities/", views.university_search, name="university_search"),
    path("export/", views.export, name="export"),
    # Consequential actions, each on its own confirm page — never a one-click
    # on the settings page itself. See views.py's shared-pattern note.
    path("security/signout-all/", views.signout_other_sessions, name="signout_all"),
    path("delete/", views.delete_account, name="delete"),
    path("timezone/", views.timezone_detect, name="timezone_detect"),
    # Web Push deadline alerts (accounts/push.py); the Settings Notifications
    # toggle POSTs to these directly from JS.
    path("push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("push/unsubscribe/", views.push_unsubscribe, name="push_unsubscribe"),
    # The link in the weekly digest's footer. GET confirms, POST writes the
    # same `weekly_digest_opt_out` flag the Settings toggle writes.
    path("unsubscribe/<str:token>/", views.digest_unsubscribe,
         name="digest_unsubscribe"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
]
