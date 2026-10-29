"""Shared stock mutations.

StockAddView and list to-cellar must create StorageItem the same way. Calling
HTTP-to-self from to-cellar would nest auth and drop X-Household.
"""

from datetime import timedelta

from django.utils import timezone

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.achievements import award_for_add
from wine_cellar.apps.wine.taste import rebuild_snapshot

import logging

logger = logging.getLogger(__name__)


def refresh_taste(household):
    try:
        rebuild_snapshot(household)
    except Exception:
        logger.exception("taste snapshot rebuild failed")


def add_stock_item(
    *,
    household,
    user,
    wine,
    storage_name,
    price=None,
    row=None,
    drank=False,
    check_duplicate=False,
    confirm_duplicate=False,
):
    """Create a StorageItem (or return a recent_duplicate 409 payload).

    Returns ``(payload, status_code)``. 409 payload has ``error`` =
    ``recent_duplicate``. 201 payload matches StockAddView.
    """
    storage = Storage.objects.filter(name=storage_name, household=household).first()
    if not storage:
        storage = Storage.objects.filter(household=household).first()
        if not storage:
            storage = Storage.objects.create(
                name="Fridge",
                household=household,
                user=user,
                location="Fridge",
                rows=0,
                columns=0,
            )

    if check_duplicate and not confirm_duplicate:
        cutoff = timezone.now() - timedelta(minutes=60)
        recent = (
            StorageItem.objects.filter(
                wine=wine,
                wine__household=household,
                deleted=drank,
                created__gte=cutoff,
            )
            .order_by("-created")
            .first()
        )
        if recent is not None:
            return (
                {
                    "error": "recent_duplicate",
                    "wine_id": wine.id,
                    "item_id": recent.id,
                    "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                    "stock_count": wine.total_stock,
                },
                409,
            )

    item = StorageItem.objects.create(
        storage=storage,
        wine=wine,
        user=user,
        price=price,
        row=row,
        deleted=drank,
    )
    refresh_taste(household)
    payload = {
        "item_id": item.id,
        "wine_id": wine.id,
        "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
        "stock_count": wine.total_stock,
        "storage": storage.name,
        "storage_id": storage.id,
        "row": row,
        "drawers": storage.rows,
        "action": "drank" if drank else "added",
        "achievements": award_for_add(
            wine, user, storage=storage, drank=drank,
        ),
    }
    return payload, 201
