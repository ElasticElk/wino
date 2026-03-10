from django.apps import AppConfig


class ScannerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "wine_cellar.apps.scanner"
    verbose_name = "Scanner Service"
