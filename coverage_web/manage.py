#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    # `setdefault` means an already-set DJANGO_SETTINGS_MODULE (e.g. from CI or
    # a production environment) always wins; local dev gets this default for free.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coverage_web.settings.local")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment? Run `uv sync "
            "--all-packages` from the repo root first."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
