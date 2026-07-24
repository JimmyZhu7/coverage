from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self) -> None:
        # Connect the `signup` funnel event (allauth user_signed_up).
        from . import signals  # noqa: F401
