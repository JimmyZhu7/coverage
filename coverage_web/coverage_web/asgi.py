"""
ASGI config for the coverage_web project.

Exposes the ASGI callable as a module-level variable named ``application``.
Not currently used to serve real-time features (see docs/build-plan.md —
native async/websocket realtime is explicitly deferred), but Django expects
this module to exist and it costs nothing to wire up correctly now.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coverage_web.settings.local")

application = get_asgi_application()
