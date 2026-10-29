"""Global ServiceConfig feature flags.

Values are on/true/1/yes (case-insensitive). Missing key is off.
"""

from wine_cellar.apps.scanner.models import ServiceConfig

_TRUE = {"on", "true", "1", "yes"}


def flag_on(key: str) -> bool:
    value = (
        ServiceConfig.objects.filter(key=key)
        .values_list("value", flat=True)
        .first()
    )
    return (value or "").strip().lower() in _TRUE
