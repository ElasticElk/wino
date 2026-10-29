"""Cellar peek for a club. Forbidden keys must never appear in the JSON."""

from __future__ import annotations

from collections import Counter

from django.db.models import Count, Q

from wine_cellar.apps.club.models import VISIBILITY_RANK
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.models import Wine, WineType

FORBIDDEN_PEEK_KEYS = frozenset(
    {
        "price",
        "item_price",
        "market_price",
        "market_price_currency",
        "storage",
        "storage_id",
        "row",
        "drawer",
        "drawers",
        "comment",
        "drink_by",
        "barcode",
        "rating",
        "critic_score",
        "vivino_rating",
        "category_rating",
        "wine_id",
        "item_id",
    }
)

PEEK_WINE_LIMIT = 30


def effective_visibility(club, membership) -> str:
    if membership.accepted_sharing_version == club.sharing_version:
        return club.cellar_visibility or "none"
    previous = club.previous_cellar_visibility or "none"
    if previous not in VISIBILITY_RANK:
        return "none"
    return previous


def tighter_visibility(*levels: str) -> str:
    picked = "full"
    for level in levels:
        vis = level if level in VISIBILITY_RANK else "none"
        if VISIBILITY_RANK[vis] < VISIBILITY_RANK[picked]:
            picked = vis
    return picked


def effective_badges(club, membership) -> bool:
    if membership.accepted_sharing_version == club.sharing_version:
        return bool(club.badges_visible)
    if club.previous_badges_visible is not None:
        return bool(club.previous_badges_visible)
    return bool(club.badges_visible)


def collect_keys(payload) -> set[str]:
    keys = set()
    if isinstance(payload, dict):
        keys.update(payload.keys())
        for value in payload.values():
            keys.update(collect_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            keys.update(collect_keys(item))
    return keys


def forbidden_present(payload) -> set[str]:
    return collect_keys(payload) & FORBIDDEN_PEEK_KEYS


def _in_stock(household):
    return StorageItem.objects.filter(
        wine__household=household, deleted=False,
    )


def peek_summary(household) -> dict:
    items = _in_stock(household).select_related("wine")
    type_counts = Counter()
    grape_counts = Counter()
    wine_ids = set()
    total = 0
    for item in items:
        total += 1
        wine = item.wine
        wine_ids.add(wine.id)
        type_counts[wine.wine_type or "RE"] += 1
    # Count bottles per grape: a blend contributes to each grape once per bottle.
    bottles_by_wine = dict(
        _in_stock(household)
        .values("wine_id")
        .annotate(n=Count("id"))
        .values_list("wine_id", "n")
    )
    for wine_id, grape in (
        Wine.objects.filter(id__in=wine_ids)
        .values_list("id", "grapes__name")
    ):
        if grape:
            grape_counts[grape] += bottles_by_wine.get(wine_id, 0)
    return {
        "household_id": household.id,
        "household_name": household.name,
        "visibility": "summary",
        "counts_by_type": dict(type_counts),
        "counts_by_grape": dict(grape_counts),
        "total_bottles": total,
    }


def peek_full(household) -> dict:
    body = peek_summary(household)
    body["visibility"] = "full"
    rows = (
        Wine.objects.filter(household=household)
        .annotate(
            stock_count=Count(
                "storageitem", filter=Q(storageitem__deleted=False),
            ),
        )
        .filter(stock_count__gt=0)
        .select_related("region")
        .prefetch_related("grapes")
        .order_by("name", "vintage", "id")
    )
    wines = []
    truncated = False
    for wine in rows:
        if len(wines) >= PEEK_WINE_LIMIT:
            truncated = True
            break
        wines.append(
            {
                "id": wine.id,
                "name": wine.name,
                "producer": wine.producer or "",
                "vintage": wine.vintage,
                "wine_type": wine.wine_type,
                "country": wine.country or "",
                "region": wine.region.name if wine.region_id else "",
                "grapes": [g.name for g in wine.grapes.all()],
                "stock_count": wine.stock_count,
            }
        )
    body["wines"] = wines
    body["truncated"] = truncated
    return body


def type_label(code: str) -> str:
    try:
        return WineType(code).label
    except ValueError:
        return code or ""
