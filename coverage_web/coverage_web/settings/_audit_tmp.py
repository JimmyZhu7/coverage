from .local import *  # noqa: F401,F403

DATABASES["default"]["TEST"] = {"NAME": "coverage_audit_tmp_test_content"}  # noqa: F405
