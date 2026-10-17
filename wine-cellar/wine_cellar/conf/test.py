import os

from .settings import *  # noqa: F403

MEDIA_ROOT = BASE_DIR / "test_media/"  # noqa: F405

CELERY_TASK_ALWAYS_EAGER = True

# CI sets PGHOSTADDR. Local pytest keeps SQLite so we don't need CREATEDB
# on the compose role. The GitHub workflow's postgres service is otherwise
# unused — tests were silently running on SQLite and ignoring row locks.
if os.environ.get("PGHOSTADDR"):
    DATABASES = {  # noqa: F405
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("SQL_DATABASE", "django"),
            "USER": os.environ.get("SQL_USER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD")
            or os.environ.get("SQL_PASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOSTADDR", "127.0.0.1"),
            "PORT": os.environ.get("SQL_PORT", "5432"),
            "TEST": {
                "NAME": os.environ.get("SQL_TEST_DATABASE", "django_test"),
            },
        }
    }
