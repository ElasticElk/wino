from django.db.models.signals import post_save
from django.dispatch import receiver

from wine_cellar.apps.scanner.models import HouseholdMembership
from wine_cellar.apps.storage.models import Storage


@receiver(post_save, sender=HouseholdMembership)
def create_default_storage(sender, instance, created, **kwargs):
    """Give a household somewhere to put bottles, once.

    This used to hang off User creation, which fires *before* the user has any
    household — so every new user produced a storage with household=NULL that
    was invisible to the API, to the bot, and (once the web layer was scoped)
    to the web UI too. Seven of those accumulated in production.

    Storage belongs to a household, so create it when a household gains its
    first member, and only if it has no storage yet. Adding a second person to
    an existing household no longer creates a duplicate.
    """
    if not created:
        return

    household = instance.household
    if household is None:
        return
    if Storage.objects.filter(household=household).exists():
        return

    Storage.objects.create(
        name="Unsorted",
        user=instance.user,
        household=household,
        description="Bottles whose location hasn't been set yet",
        location="Cellar",
        rows=0,
        columns=0,
    )
