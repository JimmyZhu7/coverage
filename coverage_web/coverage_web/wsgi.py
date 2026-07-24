"""
WSGI config for the coverage_web project.

Exposes the WSGI callable as a module-level variable named ``application``.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coverage_web.settings.local")

application = get_wsgi_application()
