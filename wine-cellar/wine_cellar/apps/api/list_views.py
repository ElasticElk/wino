"""To-drink list API. Keyed by request.user, not X-Household.

X-Household is read only on to-cellar (which household's fridge).
"""

from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from wine_cellar.apps.api.serializers import WineCreateSerializer
from wine_cellar.apps.api.stock import add_stock_item
from wine_cellar.apps.api.views import ScannerAPIView, _existing_wine_for_create
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.wine.models import (
    Size,
    Wine,
    WineList,
    WineListItem,
    get_or_create_personal_list,
)

logger = logging.getLogger(__name__)

PAYLOAD_MAX_BYTES = 16 * 1024
PAYLOAD_ALLOW = {
    "name",
    "producer",
    "vintage",
    "grape_variety",
    "grape",
    "region",
    "appellation",
    "country",
    "abv",
    "wine_type",
    "category",
    "barcode",
    "description",
    "vineyard",
    "size",
    "critic_score",
    "vivino_rating",
    "market_price",
    "market_price_currency",
    "critic_score_confidence",
    "critic_score_reasoning",
    "vivino_rating_confidence",
    "vivino_rating_reasoning",
    "market_price_confidence",
    "market_price_reasoning",
    "reference_confidence",
    "rating",
    "food_pairings",
    "attributes",
    "drink_by",
    "price",
}
IMAGE_KEYS = {
    "image",
    "photo",
    "photo_data",
    "photo_front",
    "photo_bytes",
    "thumbnail",
    "image_data",
    "label_photo",
}
SOURCE_VALUES = {choice[0] for choice in WineListItem.SOURCE_CHOICES}


class ListsDisabled(Exception):
    pass


class ListsEnabledMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_lists"):
            raise ListsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, ListsDisabled):
            return Response(
                {"error": "not_enabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return super().handle_exception(exc)


def _owned_personal(user, list_id):
    return WineList.objects.filter(
        id=list_id, owner=user, kind=WineList.KIND_PERSONAL,
    ).first()


def _coerce_vintage(value):
    if value in (None, ""):
        return None
    try:
        vintage = int(float(value))
    except (TypeError, ValueError):
        return None
    if vintage <= 0:
        return None
    return vintage


def sanitize_payload(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if key.lower() in IMAGE_KEYS or isinstance(value, (bytes, bytearray)):
            continue
        if key not in PAYLOAD_ALLOW:
            continue
        cleaned[key] = value
    if "grape_variety" not in cleaned and cleaned.get("grape"):
        cleaned["grape_variety"] = cleaned["grape"]
    blob = json.dumps(cleaned, default=str)
    if len(blob.encode("utf-8")) <= PAYLOAD_MAX_BYTES:
        return cleaned
    cleaned.pop("description", None)
    cleaned.pop("critic_score_reasoning", None)
    cleaned.pop("vivino_rating_reasoning", None)
    cleaned.pop("market_price_reasoning", None)
    blob = json.dumps(cleaned, default=str)
    if len(blob.encode("utf-8")) <= PAYLOAD_MAX_BYTES:
        return cleaned
    keep = (
        "name", "producer", "vintage", "wine_type", "country",
        "grape_variety", "barcode", "region", "appellation",
    )
    return {k: cleaned[k] for k in keep if k in cleaned}


def serialize_item(item: WineListItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "producer": item.producer,
        "vintage": item.vintage,
        "wine_type": item.wine_type,
        "country": item.country,
        "grape": item.grape,
        "barcode": item.barcode,
        "wine_id": item.wine_id,
        "source": item.source,
        "created": item.created.isoformat() if item.created else None,
        "moved_at": item.moved_at.isoformat() if item.moved_at else None,
    }


def _wine_for_user(user, wine_id):
    if not wine_id:
        return None
    try:
        wine_id = int(wine_id)
    except (TypeError, ValueError):
        return None
    return (
        Wine.objects.filter(id=wine_id, household__memberships__user=user)
        .distinct()
        .first()
    )


class WineListCollectionView(ListsEnabledMixin, ScannerAPIView):
    def get(self, request):
        get_or_create_personal_list(request.user)
        lists = (
            WineList.objects.filter(
                owner=request.user, kind=WineList.KIND_PERSONAL,
            )
            .annotate(
                item_count=Count(
                    "items", filter=Q(items__moved_at__isnull=True),
                )
            )
            .order_by("id")
        )
        return Response(
            {
                "lists": [
                    {
                        "id": lst.id,
                        "name": lst.name,
                        "kind": lst.kind,
                        "item_count": lst.item_count,
                    }
                    for lst in lists
                ]
            }
        )


class WineListDetailView(ListsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        lst = _owned_personal(request.user, pk)
        if lst is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 5), 50))
        except (TypeError, ValueError):
            limit = 5
        try:
            offset = max(0, int(request.query_params.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        include_moved = request.query_params.get("include_moved") in ("1", "true")
        items = WineListItem.objects.filter(list=lst)
        if not include_moved:
            items = items.filter(moved_at__isnull=True)
        count = items.count()
        page = list(items.order_by("-created")[offset : offset + limit])
        return Response(
            {
                "id": lst.id,
                "name": lst.name,
                "count": count,
                "offset": offset,
                "limit": limit,
                "items": [serialize_item(item) for item in page],
            }
        )


class WineListItemCreateView(ListsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        lst = _owned_personal(request.user, pk)
        if lst is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        data = request.data if isinstance(request.data, dict) else {}
        name = str(data.get("name") or "").strip()
        wine = _wine_for_user(request.user, data.get("wine_id"))
        if wine is not None and not name:
            name = wine.name
        if not name:
            return Response(
                {"error": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = sanitize_payload(data.get("payload") if "payload" in data else data)
        producer = str(data.get("producer") or "").strip()[:100]
        grape = str(data.get("grape") or payload.get("grape_variety") or payload.get("grape") or "").strip()[:100]
        barcode = str(data.get("barcode") or payload.get("barcode") or "").strip()[:100]
        wine_type = str(data.get("wine_type") or payload.get("wine_type") or "")[:2]
        country = str(data.get("country") or payload.get("country") or "")[:3]
        vintage = _coerce_vintage(data.get("vintage", payload.get("vintage")))
        source = str(data.get("source") or "label")
        if source not in SOURCE_VALUES:
            source = "label"
        if wine is not None:
            existing = WineListItem.objects.filter(
                list=lst, wine=wine, moved_at__isnull=True,
            ).first()
            if existing is not None:
                existing.payload = payload or existing.payload
                if producer:
                    existing.producer = producer
                if grape:
                    existing.grape = grape
                if barcode:
                    existing.barcode = barcode
                existing.save()
                body = serialize_item(existing)
                body["already"] = True
                return Response(body, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                item = WineListItem.objects.create(
                    list=lst,
                    name=name[:100],
                    producer=producer,
                    vintage=vintage,
                    wine_type=wine_type,
                    country=country,
                    grape=grape,
                    barcode=barcode,
                    payload=payload,
                    wine=wine,
                    source=source,
                    added_by=request.user,
                )
        except IntegrityError:
            existing = WineListItem.objects.filter(
                list=lst, wine=wine, moved_at__isnull=True,
            ).first()
            if existing is None:
                raise
            existing.payload = payload or existing.payload
            existing.save()
            body = serialize_item(existing)
            body["already"] = True
            return Response(body, status=status.HTTP_200_OK)
        return Response(serialize_item(item), status=status.HTTP_201_CREATED)


class WineListItemDeleteView(ListsEnabledMixin, ScannerAPIView):
    def delete(self, request, pk, item_id):
        lst = _owned_personal(request.user, pk)
        if lst is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        item = WineListItem.objects.filter(id=item_id, list=lst).first()
        if item is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        if item.moved_at is not None:
            return Response(
                {"error": "already_moved", "id": item.id},
                status=status.HTTP_409_CONFLICT,
            )
        item.delete()
        return Response({"status": "removed", "id": item_id})


def _create_or_find_wine(request, household, item):
    """Resolve a Wine in the current household from the list row."""
    if item.wine_id:
        wine = Wine.objects.filter(id=item.wine_id, household=household).first()
        if wine is not None:
            return wine
    payload = dict(item.payload or {})
    payload.setdefault("name", item.name)
    if item.producer:
        payload.setdefault("producer", item.producer)
    if item.vintage:
        payload.setdefault("vintage", item.vintage)
    if item.wine_type:
        payload.setdefault("wine_type", item.wine_type)
    if item.country:
        payload.setdefault("country", item.country)
    if item.barcode:
        payload.setdefault("barcode", item.barcode)
    if item.grape:
        payload.setdefault("grape_variety", item.grape)
    payload = sanitize_payload(payload)
    if not str(payload.get("name") or "").strip():
        return None
    serializer = WineCreateSerializer(
        data=payload,
        context={"request": request, "household": household},
    )
    serializer.is_valid(raise_exception=True)
    # UniqueConstraint treats NULL abv/size as distinct, so look up the
    # household unique key first rather than relying on IntegrityError.
    size_litres = serializer.validated_data.get("size")
    size_obj = None
    if size_litres is not None:
        size_obj = Size.objects.filter(name=size_litres, user=request.user).first()
    existing = Wine.objects.filter(
        household=household,
        name=serializer.validated_data.get("name"),
        wine_type=serializer.validated_data.get("wine_type", "RE"),
        abv=serializer.validated_data.get("abv"),
        size=size_obj,
        vintage=serializer.validated_data.get("vintage"),
        country=serializer.validated_data.get("country", "CH"),
    ).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return serializer.save()
    except IntegrityError:
        existing = _existing_wine_for_create(
            household, serializer.validated_data, request.user,
        )
        if existing is None:
            raise
        return existing


class WineListItemToCellarView(ListsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, item_id):
        lst = _owned_personal(request.user, pk)
        if lst is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        item = WineListItem.objects.filter(id=item_id, list=lst).first()
        if item is None:
            return Response(
                {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
            )
        if item.moved_at is not None:
            return Response(
                {"error": "already_moved", "id": item.id},
                status=status.HTTP_409_CONFLICT,
            )
        household = self.get_household(request)
        data = request.data if isinstance(request.data, dict) else {}
        ack_duplicate = bool(data.get("ack_duplicate"))
        confirm_duplicate = bool(data.get("confirm_duplicate"))
        wine = _create_or_find_wine(request, household, item)
        if wine is None:
            return Response(
                {"error": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ack_duplicate:
            item.wine = wine
            item.moved_at = timezone.now()
            item.save(update_fields=["wine", "moved_at", "touched_at"])
            return Response(
                {
                    "item_id": data.get("item_id"),
                    "wine_id": wine.id,
                    "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                    "stock_count": wine.total_stock,
                    "storage": "",
                    "action": "acked",
                    "achievements": [],
                    "list_item_id": item.id,
                }
            )
        storage_name = data.get("storage_name") or "Fridge"
        row = data.get("row")
        price = data.get("price")
        payload, code = add_stock_item(
            household=household,
            user=request.user,
            wine=wine,
            storage_name=storage_name,
            price=price,
            row=row,
            drank=False,
            check_duplicate=True,
            confirm_duplicate=confirm_duplicate,
        )
        if code == 409:
            payload["list_item_id"] = item.id
            return Response(payload, status=status.HTTP_409_CONFLICT)
        item.wine = wine
        item.moved_at = timezone.now()
        item.save(update_fields=["wine", "moved_at", "touched_at"])
        payload["list_item_id"] = item.id
        return Response(payload, status=status.HTTP_201_CREATED)
