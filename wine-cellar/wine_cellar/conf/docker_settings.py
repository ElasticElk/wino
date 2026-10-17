import os

import sentry_sdk

from wine_cellar.__init__ import __version__
from wine_cellar.conf.prod import *  # noqa: F403

DEBUG = os.getenv("DJANGO_DEBUG", "False") == "True"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS").split(" ")
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("SQL_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("SQL_DATABASE", BASE_DIR / "db.sqlite3"),  # noqa: F405
        "USER": os.environ.get("SQL_USER", "user"),
        "PASSWORD": os.environ.get("SQL_PASSWORD", "password"),
        "HOST": os.environ.get("SQL_HOST", "localhost"),
        "PORT": os.environ.get("SQL_PORT", "5432"),
    }
}

CSRF_TRUSTED_ORIGINS = os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS").split(" ")

MEDIA_ROOT = "mediafiles"
STATIC_ROOT = "staticfiles"
# Manifest hashing dies on Tom Select's missing .map. Compression without
# hashed names is enough for WhiteNoise on this stack.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

SITE_URL = os.environ.get("DJANGO_SITE_URL")

ENABLE_SIGNUPS = os.environ.get("DJANGO_ENABLE_SIGNUPS", "False") == "True"

EMAIL_HOST = os.environ.get("DJANGO_EMAIL_HOST")
EMAIL_PORT = os.environ.get("DJANGO_EMAIL_PORT")
EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_USER")
EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_PASSWORD")
# USE_TLS and USE_SSL are mutual exclusive
EMAIL_USE_TLS = os.environ.get("DJANGO_EMAIL_USE_TLS", "True") == "True"
EMAIL_USE_SSL = os.environ.get("DJANGO_EMAIL_USE_SSL", "False") == "True"
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL")

if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

CELERY_BROKER_URL = "redis://redis:6379"
CELERY_RESULT_BACKEND = "redis://redis:6379"
CELERY_RESULT_EXPIRES = 60 * 60

# Shared across gunicorn workers. LocMemCache would 404 a poll that
# landed on a different process than the POST that queued the lookup.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis:6379/1",
    }
}

if DEBUG:
    try:
        import debug_toolbar  # noqa: F401
    except ImportError:
        pass
    else:
        INSTALLED_APPS += ("debug_toolbar",)  # noqa: F405
        MIDDLEWARE += ("debug_toolbar.middleware.DebugToolbarMiddleware",)  # noqa: F405
        INTERNAL_IPS = ("127.0.0.1", "localhost")

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release="wine-cellar@" + __version__,
    )
