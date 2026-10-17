import pycountry
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models


class UserSettings(models.Model):
    user = models.OneToOneField(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="user_settings",
    )
    language = models.CharField(
        max_length=7,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE,
    )
    currency = models.CharField(
        max_length=3, choices=settings.CURRENCIES, default="EUR"
    )
    # Used to ground Grok price estimates ("typical retail price in {country}").
    # Distinct from currency — a UK expat in HK might want HKD display but UK reference prices.
    country = models.CharField(
        max_length=2,
        blank=True,
        default="",
        choices={c.alpha_2: c.name for c in pycountry.countries},
    )
    # Skip (or a real pick) must survive a scanner restart — empty country
    # alone is also the "never asked" state.
    country_prompted = models.BooleanField(default=False)
    notifications = models.BooleanField(default=True)
