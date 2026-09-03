from datetime import timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.wine.emails import send_drink_by_reminder
from wine_cellar.apps.wine.models import Wine


@shared_task(name="drink_by_reminder")
def drink_by_reminder():
    User = get_user_model()
    users = (
        User.objects.exclude(email__isnull=True)
        .exclude(email__exact="")
        .exclude(user_settings__notifications=False)
    )
    date = timezone.now().date() + timedelta(days=14)
    for user in users:
        # Household-scoped, like everything else: a member should be reminded
        # about the cellar's wine, not only rows they happened to add.
        household = get_user_household(user)
        if household is None:
            continue
        wines = Wine.objects.filter(
            household=household, drink_by=date, storageitem__isnull=False
        ).distinct()
        if wines.count() > 0:
            send_drink_by_reminder(user, wines)


@shared_task(name="refresh_wine_references", bind=True, max_retries=0)
def refresh_wine_references(self, wine_id, user_id=None):
    """Look up references for one wine, off the request path.

    Grok's live search can run for minutes; doing it inside a request killed
    the gunicorn worker at its 30s default and, with one sync worker, blocked
    everything else meanwhile.
    """
    from wine_cellar.apps.wine.references import perform_reference_refresh

    return perform_reference_refresh(wine_id, user_id=user_id)
