from datetime import timedelta

from django.contrib.auth.decorators import login_not_required
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.parsers import MultiPartParser

from wine_cellar.apps.api.task_access import remember_task, task_visible
from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.scanner.models import Household, TelegramUser
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.achievements import (
    award_for_add,
    backfill,
    month_tally,
    serialize_achievement,
)
from wine_cellar.apps.wine.models import CellarAchievement, ImageType, Wine, WineImage
from wine_cellar.apps.wine.taste import rebuild_snapshot

import logging

logger = logging.getLogger(__name__)


def _refresh_taste(household):
    try:
        rebuild_snapshot(household)
    except Exception:
        logger.exception("taste snapshot rebuild failed")

from .serializers import (
    StockAddSerializer,
    StockMoveSerializer,
    StockRemoveSerializer,
    StockSetSerializer,
    StockVoteSerializer,
    WineCreateSerializer,
    WineSerializer,
)


def wine_queryset(household):
    """Wines for a household, shaped so WineSerializer costs a constant number
    of queries instead of one per wine per relation.

    WineSerializer touches total_stock, grapes, region, appellation, size,
    food_pairings, attributes and vineyard. Unprefetched that measured 383
    queries for 49 wines; the bot pays it on every detail card and every label
    photo, so it dominates perceived latency.
    """
    return (
        Wine.objects.filter(household=household)
        .select_related("region", "appellation", "size")
        .prefetch_related("grapes", "food_pairings", "attributes", "vineyard")
        .annotate(
            stock_count_annotated=Count(
                "storageitem", filter=Q(storageitem__deleted=False), distinct=True,
            ),
        )
    )


@method_decorator(login_not_required, name="dispatch")
class ScannerAPIView(APIView):
    """Base view that bypasses Django's LoginRequiredMiddleware.

    Resolves the active household from X-Household header or user's default.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_household(self, request) -> Household:
        """Resolve the household for this request.

        Priority: X-Household header (only if the user belongs to it), then
        their default household, then their first. Shares one implementation
        with the web layer so the two cannot drift apart again.
        """
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        requested = None
        if header_id:
            try:
                requested = int(header_id)
            except (TypeError, ValueError):
                requested = None

        household = get_user_household(request.user, household_id=requested)
        if household is None:
            raise Household.DoesNotExist("User has no household")
        return household

    def handle_exception(self, exc):
        """Turn "no household" into a clean 403 instead of a 500.

        Every provisioned user has a household, but an account created outside
        the normal flow does not — and an uncaught DoesNotExist here surfaced
        as a server error rather than something a client could act on.
        """
        if isinstance(exc, Household.DoesNotExist):
            return Response(
                {"error": "You do not belong to a household yet."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return
        cutoff = timezone.now() - timedelta(minutes=5)
        TelegramUser.objects.filter(user=user).filter(
            Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=cutoff)
        ).update(last_seen_at=timezone.now())


class WineByBarcodeView(ScannerAPIView):
    def get(self, request, ean):
        household = self.get_household(request)
        wines = wine_queryset(household).filter(barcode=ean)
        serializer = WineSerializer(wines, many=True)
        return Response({"count": wines.count(), "wines": serializer.data})


class StockAddView(ScannerAPIView):
    def post(self, request):
        serializer = StockAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        household = self.get_household(request)
        wine_id = serializer.validated_data["wine_id"]
        storage_name = serializer.validated_data["storage_name"]
        price = serializer.validated_data.get("price")
        row = serializer.validated_data.get("row")

        try:
            wine = Wine.objects.get(id=wine_id, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        storage = Storage.objects.filter(name=storage_name, household=household).first()
        if not storage:
            # Fall back to household's first storage, or create "Fridge"
            storage = Storage.objects.filter(household=household).first()
            if not storage:
                storage = Storage.objects.create(
                    name="Fridge", household=household,
                    user=request.user, location="Fridge", rows=0, columns=0,
                )

        drank = serializer.validated_data.get("drank", False)
        confirm_duplicate = serializer.validated_data.get("confirm_duplicate", False)
        check_duplicate = serializer.validated_data.get("check_duplicate", False)
        if check_duplicate and not confirm_duplicate:
            cutoff = timezone.now() - timedelta(minutes=60)
            recent = (
                StorageItem.objects.filter(
                    wine=wine, wine__household=household, deleted=drank,
                    created__gte=cutoff,
                )
                .order_by("-created")
                .first()
            )
            if recent is not None:
                return Response(
                    {
                        "error": "recent_duplicate",
                        "wine_id": wine.id,
                        "item_id": recent.id,
                        "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                        "stock_count": wine.total_stock,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        item = StorageItem.objects.create(
            storage=storage, wine=wine, user=request.user, price=price, row=row,
            deleted=drank,
        )
        _refresh_taste(household)

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
                wine, request.user, storage=storage, drank=drank,
            ),
        }
        return Response(payload, status=status.HTTP_201_CREATED)


class StockRemoveView(ScannerAPIView):
    def post(self, request):
        serializer = StockRemoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        household = self.get_household(request)
        wine_id = serializer.validated_data["wine_id"]
        storage_name = serializer.validated_data.get("storage_name", "")

        try:
            wine = Wine.objects.get(id=wine_id, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        items = StorageItem.objects.filter(
            wine=wine, wine__household=household, deleted=False
        )
        item = None
        if storage_name:
            item = items.filter(storage__name=storage_name).order_by("-created").first()
            if item is None:
                return Response(
                    {"error": f"No stock of this wine in {storage_name}"},
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            item = items.order_by("-created").first()

        if not item:
            return Response(
                {"error": "No stock remaining for this wine"},
                status=status.HTTP_409_CONFLICT,
            )

        taken_from = item.storage
        taken_row = item.row
        item_id = item.id
        item.deleted = True
        item.save()
        _refresh_taste(household)

        return Response(
            {
                "item_id": item_id,
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
                "storage": taken_from.name,
                "storage_id": taken_from.id,
                "row": taken_row,
                "drawers": taken_from.rows,
                "action": "removed",
            }
        )


class StockHistoryView(ScannerAPIView):
    """Bottles tagged drank or removed, newest first.

    `modified` is when the bottle left the cellar (soft-delete) or when a
    drank-without-stock row was created. Page with ?limit=5&offset=0.
    """

    def get(self, request):
        household = self.get_household(request)
        try:
            limit = int(request.query_params.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        try:
            offset = int(request.query_params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 20))
        offset = max(0, offset)

        images = WineImage.objects.filter(wine_id=OuterRef("wine_id"))
        qs = (
            StorageItem.objects.filter(wine__household=household, deleted=True)
            .select_related("wine", "storage")
            .annotate(has_image=Exists(images))
            .order_by("-modified", "-id")
        )
        total = qs.count()
        items = []
        for item in qs[offset:offset + limit]:
            wine = item.wine
            items.append({
                "item_id": item.id,
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "producer": wine.producer or "",
                "comment": wine.comment or "",
                "wine_type": wine.wine_type,
                "has_image": bool(item.has_image),
                "storage": item.storage.name if item.storage_id else "",
                "vote": item.vote or "",
                "drank_at": item.modified,
            })
        return Response({
            "count": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        })


class StockSetView(ScannerAPIView):
    """Set stock to an exact quantity. Adds or removes items to match."""

    def post(self, request):
        serializer = StockSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        household = self.get_household(request)
        wine_id = serializer.validated_data["wine_id"]
        target = serializer.validated_data["quantity"]
        storage_name = serializer.validated_data.get("storage_name", "")

        try:
            wine = Wine.objects.get(id=wine_id, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        current = wine.total_stock
        diff = target - current

        if diff > 0:
            # Need to add items
            storage, _ = Storage.objects.get_or_create(
                name=storage_name or "Wine Fridge",
                household=household,
                defaults={"user": request.user, "location": storage_name or "Wine Fridge", "rows": 0, "columns": 0},
            )
            for _ in range(diff):
                StorageItem.objects.create(
                    storage=storage, wine=wine, user=request.user
                )
        elif diff < 0:
            items = StorageItem.objects.filter(
                wine=wine, wine__household=household, deleted=False
            )
            if storage_name:
                items = items.filter(storage__name=storage_name)
            items = items.order_by("-created")[: abs(diff)]
            for item in items:
                item.deleted = True
                item.save()

        if diff:
            _refresh_taste(household)

        return Response(
            {
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
                "action": "set",
                "achievements": award_for_add(wine, request.user, storage=storage) if diff > 0 else [],
            }
        )


class WineStockView(ScannerAPIView):
    def get(self, request, pk):
        household = self.get_household(request)
        try:
            wine = Wine.objects.get(id=pk, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            {
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
            }
        )


class WineCreateView(ScannerAPIView):
    def post(self, request):
        household = self.get_household(request)
        serializer = WineCreateSerializer(
            data=request.data, context={"request": request, "household": household}
        )
        serializer.is_valid(raise_exception=True)
        wine = serializer.save()

        return Response(
            {
                "id": wine.id,
                "name": wine.name,
                "vintage": wine.vintage,
                "barcode": wine.barcode,
                "url": request.build_absolute_uri(f"/wine/{wine.id}"),
            },
            status=status.HTTP_201_CREATED,
        )


class WineInStockView(ScannerAPIView):
    """List all wines that currently have stock > 0.

    Optional query param: ?storage=Name to filter by storage/fridge.
    """

    def get(self, request):
        household = self.get_household(request)
        storage_filter = request.query_params.get("storage", "")

        items = StorageItem.objects.filter(
            wine__household=household, deleted=False
        ).select_related("wine", "wine__region", "storage").prefetch_related("wine__grapes")

        if storage_filter:
            items = items.filter(storage__name=storage_filter)

        # Group by wine
        wine_map = {}
        for item in items:
            wine = item.wine
            if wine.id not in wine_map:
                producer = wine.producer
                grapes = wine.grapes.all()
                grape = grapes[0].name if grapes else ""

                wine_map[wine.id] = {
                    "id": wine.id,
                    "name": f"{wine.name} {wine.vintage or ''}".strip(),
                    "wine_type": wine.wine_type,
                    "barcode": wine.barcode or "",
                    "producer": producer,
                    "country": wine.country or "",
                    "grape": grape,
                    "stock_count": 0,
                    "market_price": (
                        None if wine.market_price is None else float(wine.market_price)
                    ),
                    "market_price_currency": wine.market_price_currency,
                    "items": [],
                }
            wine_map[wine.id]["stock_count"] += 1
            wine_map[wine.id]["items"].append({
                "item_id": item.id,
                "storage": item.storage.name,
                "drawer": item.row,
            })

        wines_in_stock = sorted(wine_map.values(), key=lambda w: w["name"])
        return Response({"count": len(wines_in_stock), "wines": wines_in_stock})


class WineDetailView(ScannerAPIView):
    """One wine with its stock locations, in a constant number of queries.

    The bot previously built a detail card by calling list_all_wines() *and*
    wines_in_stock() and merging them — two full-collection fetches to render
    one card. Five other callbacks (set quantity, move, drawer view, ...) did
    the same to act on a single bottle.
    """

    def get(self, request, pk):
        household = self.get_household(request)
        wine = wine_queryset(household).filter(pk=pk).first()
        if wine is None:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        data = WineSerializer(wine).data
        data["items"] = [
            {
                "item_id": item.id,
                "storage": item.storage.name,
                "drawer": item.row,
            }
            for item in StorageItem.objects.filter(
                wine=wine, deleted=False,
            ).select_related("storage").order_by("storage__name", "row")
        ]
        return Response(data)


class WineUpdateView(ScannerAPIView):
    """Update fields on an existing wine."""

    def patch(self, request, pk):
        household = self.get_household(request)
        try:
            wine = Wine.objects.get(id=pk, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = WineCreateSerializer(
            data=request.data, context={"request": request, "household": household}, partial=True
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Update simple fields
        for field in ("name", "wine_type", "category", "vintage", "country", "abv", "price", "barcode"):
            if field in data:
                setattr(wine, field, data[field])

        # Each field stands alone now. Previously both were packed into
        # `comment`, so a payload with a producer and no description wiped the
        # tasting notes — which is exactly what "Update from label" sends when
        # Grok reads a producer off the label but writes no notes.
        if "producer" in data:
            wine.producer = data["producer"]
        if "description" in data:
            wine.comment = (data["description"] or "")[:500]

        rating = data.get("rating")
        if rating is not None:
            wine.rating = rating

        # drink_by
        if "drink_by" in data:
            wine.drink_by = data["drink_by"]

        # Size
        size_litres = data.get("size")
        if size_litres:
            from wine_cellar.apps.wine.models import Size
            size_obj, _ = Size.objects.get_or_create(name=size_litres, user=request.user)
            wine.size = size_obj

        wine.save()

        # Update grape if provided
        grape_name = data.get("grape_variety", "")
        if grape_name:
            from wine_cellar.apps.wine.models import Grape
            grape, _ = Grape.objects.get_or_create(name=grape_name, user=request.user)
            wine.grapes.clear()
            wine.grapes.add(grape)

        # Update region if provided
        region_name = data.get("region", "")
        if region_name:
            from wine_cellar.apps.wine.models import Region
            region, _ = Region.objects.get_or_create(name=region_name, user=request.user)
            wine.region = region
            wine.save()

        # Update appellation if provided
        appellation_name = data.get("appellation", "")
        if appellation_name:
            from wine_cellar.apps.wine.models import Appellation
            appellation, _ = Appellation.objects.get_or_create(
                name=appellation_name, user=request.user
            )
            wine.appellation = appellation
            wine.save()

        # Update vineyard if provided
        vineyard_name = data.get("vineyard", "")
        if vineyard_name:
            from wine_cellar.apps.wine.models import Vineyard
            vy, _ = Vineyard.objects.get_or_create(
                name=vineyard_name, user=request.user,
                defaults={"region": region_name, "country": data.get("country", "")},
            )
            wine.vineyard.clear()
            wine.vineyard.add(vy)

        # Update food pairings if provided
        food_pairing_names = data.get("food_pairings", [])
        if food_pairing_names:
            from wine_cellar.apps.wine.models import FoodPairing
            wine.food_pairings.clear()
            for fp_name in food_pairing_names:
                fp, _ = FoodPairing.objects.get_or_create(name=fp_name, user=request.user)
                wine.food_pairings.add(fp)

        # Update attributes if provided
        attribute_names = data.get("attributes", [])
        if attribute_names:
            from wine_cellar.apps.wine.models import Attribute
            wine.attributes.clear()
            for attr_name in attribute_names:
                attr, _ = Attribute.objects.get_or_create(name=attr_name, user=request.user)
                wine.attributes.add(attr)

        return Response(
            {
                "id": wine.id,
                "name": f"{wine.name} {wine.vintage or ''}".strip(),
                "wine_type": wine.wine_type,
                "stock_count": wine.total_stock,
                "barcode": wine.barcode or "",
            }
        )


class WineRefreshReferencesView(ScannerAPIView):
    """Queue a reference refresh (POST) and report on it (GET).

    The Grok lookup runs a live web search and can take minutes; it used to run
    inline, where gunicorn's 30s default timeout killed the worker before it
    finished and — with the default single sync worker — blocked every other
    request, bot traffic included, until it did. POST now enqueues a Celery
    task and returns 202 immediately; callers poll GET with the task_id.
    """

    # Allow web (session) and API (token) callers — the refresh button on the
    # wine detail page calls this directly from the user's session.
    authentication_classes = [SessionAuthentication, TokenAuthentication]

    def _get_wine(self, request, pk):
        household = self.get_household(request)
        return Wine.objects.filter(id=pk, household=household).first()

    def post(self, request, pk):
        from wine_cellar.apps.wine.tasks import refresh_wine_references

        wine = self._get_wine(request, pk)
        if wine is None:
            return Response({"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data if isinstance(request.data, dict) else {}
        force = bool(payload.get("force"))
        async_result = refresh_wine_references.delay(wine.id, request.user.id, force)
        remember_task(async_result.id, request.user.id, self.get_household(request).id)
        return Response(
            {
                "status": "queued",
                "task_id": async_result.id,
                "wine_id": wine.id,
                "poll": request.build_absolute_uri(
                    reverse("api-wine-refresh-references", kwargs={"pk": wine.id}),
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request, pk):
        """Report on a queued refresh, or just the wine's current references.

        Pass ?task_id= to learn whether that run finished; without it this is
        simply the wine's stored reference values.
        """
        from celery.result import AsyncResult

        wine = self._get_wine(request, pk)
        if wine is None:
            return Response({"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND)

        current = {
            "critic_score": wine.critic_score,
            "vivino_rating": float(wine.vivino_rating) if wine.vivino_rating is not None else None,
            "category_rating": wine.category_rating,
            "market_price": float(wine.market_price) if wine.market_price is not None else None,
            "market_price_currency": wine.market_price_currency,
            "confidence": wine.reference_confidence,
            "reference_updated": wine.reference_updated,
        }

        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response({"state": "IDLE", "current": current})

        household_id = self.get_household(request).id
        if not task_visible(task_id, request.user.id, household_id):
            return Response(
                {"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND,
            )

        result = AsyncResult(task_id)
        payload = {"state": result.state, "current": current}
        if result.successful():
            payload["result"] = result.result
        elif result.failed():
            # Don't leak a traceback to the client; the worker logged it.
            payload["result"] = {"updated": False, "error": "Lookup failed"}
        return Response(payload)


class ReferenceLookupView(ScannerAPIView):
    """Queue a reference lookup for a wine that isn't in the cellar (POST),
    and report on it (GET).

    The cellar refresh at /api/wines/<pk>/refresh-references/ needs a Wine row
    to write to. A label photographed in a shop has none — and that is exactly
    when the critic score, the Vivino rating and the going price decide whether
    the bottle comes home. Same queue-and-poll shape, same live search, nothing
    persisted.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]

    def post(self, request):
        from wine_cellar.apps.wine.tasks import lookup_wine_references

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response(
                {"error": "Name is required"}, status=status.HTTP_400_BAD_REQUEST,
            )

        vintage = request.data.get("vintage")
        try:
            vintage = int(vintage) if vintage not in (None, "") else None
        except (TypeError, ValueError):
            vintage = None

        hints = {
            field: (request.data.get(field) or "")
            for field in ("country", "region", "appellation", "grape", "vineyard")
        }

        household = self.get_household(request)
        async_result = lookup_wine_references.delay(
            name,
            producer=(request.data.get("producer") or ""),
            vintage=vintage,
            user_id=request.user.id,
            **hints,
        )
        remember_task(async_result.id, request.user.id, household.id)
        return Response(
            {
                "status": "queued",
                "task_id": async_result.id,
                "poll": request.build_absolute_uri(
                    reverse("api-reference-lookup"),
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request):
        """Report on a queued lookup. ?task_id= is required — there is no
        stored wine whose current values could stand in for it."""
        from celery.result import AsyncResult

        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response(
                {"error": "task_id is required"}, status=status.HTTP_400_BAD_REQUEST,
            )

        household_id = self.get_household(request).id
        if not task_visible(task_id, request.user.id, household_id):
            return Response(
                {"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND,
            )

        result = AsyncResult(task_id)
        payload = {"state": result.state}
        if result.successful():
            payload["result"] = result.result
        elif result.failed():
            # Don't leak a traceback to the client; the worker logged it.
            payload["result"] = {"updated": False, "error": "Lookup failed"}
        return Response(payload)


def _qualified_reference(raw, kind):
    """A detail block is usable when confidence is medium or high and in range."""
    if not isinstance(raw, dict):
        return None
    confidence = (raw.get("confidence") or "").lower()
    if confidence not in ("medium", "high"):
        return None
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if kind == "critic":
        if isinstance(value, float) and not value.is_integer():
            return None
        number = int(value)
        if not 0 <= number <= 100:
            return None
        return number, confidence
    if kind == "vivino":
        if not 0 <= float(value) <= 5:
            return None
        return round(float(value), 1), confidence
    if kind == "price":
        if value < 0:
            return None
        return value, confidence
    return None


class WineApplyReferencesView(ScannerAPIView):
    """Write a lookup the bot already paid for. Does not call Grok."""

    def post(self, request, pk):
        household = self.get_household(request)
        wine = Wine.objects.filter(id=pk, household=household).first()
        if wine is None:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND,
            )

        body = request.data if isinstance(request.data, dict) else {}
        details = body.get("details")
        if not isinstance(details, dict):
            details = {}

        written = []
        critic = _qualified_reference(details.get("critic_score"), "critic")
        if critic is not None:
            wine.critic_score, conf = critic
            written.append(conf)
        vivino = _qualified_reference(details.get("vivino_rating"), "vivino")
        if vivino is not None:
            wine.vivino_rating, conf = vivino
            written.append(conf)
        price = _qualified_reference(details.get("market_price"), "price")
        if price is not None:
            wine.market_price, conf = price
            written.append(conf)
            block = details.get("market_price")
            currency = block.get("currency") if isinstance(block, dict) else None
            if currency:
                wine.market_price_currency = str(currency)[:3]

        if not written:
            return Response({"updated": False})

        body_confidence = (request.data.get("confidence") or "").lower()
        if body_confidence in ("medium", "high"):
            wine.reference_confidence = body_confidence
        else:
            wine.reference_confidence = max(
                written, key=lambda c: {"high": 3, "medium": 2}.get(c, 0),
            )
        wine.reference_updated = timezone.now()
        wine.save()

        vivino = wine.vivino_rating
        price = wine.market_price
        return Response({
            "updated": True,
            "confidence": wine.reference_confidence,
            "critic_score": wine.critic_score,
            "vivino_rating": None if vivino is None else float(vivino),
            "category_rating": wine.category_rating,
            "market_price": None if price is None else float(price),
            "market_price_currency": wine.market_price_currency,
        })


class WineSearchView(ScannerAPIView):
    """Search all wines by name (case-insensitive substring match)."""

    def get(self, request):
        household = self.get_household(request)
        q = request.query_params.get("q", "").strip()
        if not q:
            return Response({"count": 0, "wines": []})

        wines = wine_queryset(household).filter(name__icontains=q).order_by("name")[:20]

        serializer = WineSerializer(wines, many=True)
        return Response({"count": len(serializer.data), "wines": serializer.data})


class WineListAllView(ScannerAPIView):
    """List all wines for the household."""

    def get(self, request):
        household = self.get_household(request)
        wines = wine_queryset(household).order_by("name")
        serializer = WineSerializer(wines, many=True)
        return Response({"count": len(serializer.data), "wines": serializer.data})


class StorageListView(ScannerAPIView):
    """List all storages (fridges) for the household."""

    def get(self, request):
        household = self.get_household(request)
        storages = Storage.objects.filter(household=household).order_by("name")
        return Response({
            "storages": [
                {
                    "id": s.id,
                    "name": s.name,
                    "location": s.location,
                    "drawers": s.rows,
                    "bottles": s.used_slots,
                    "capacity": s.total_slots,
                }
                for s in storages
            ]
        })


class StockMoveView(ScannerAPIView):
    """Move a StorageItem to a different drawer and/or storage."""

    def patch(self, request, pk):
        household = self.get_household(request)
        try:
            item = StorageItem.objects.get(
                id=pk, wine__household=household, deleted=False
            )
        except StorageItem.DoesNotExist:
            return Response(
                {"error": "Item not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = StockMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        storage_name = serializer.validated_data.get("storage_name")
        if storage_name:
            storage, _ = Storage.objects.get_or_create(
                name=storage_name,
                household=household,
                defaults={"user": request.user, "location": storage_name, "rows": 0, "columns": 0},
            )
            item.storage = storage

        if "row" in serializer.validated_data:
            item.row = serializer.validated_data["row"]

        item.save()

        return Response({
            "item_id": item.id,
            "wine_id": item.wine.id,
            "wine_name": f"{item.wine.name} {item.wine.vintage or ''}".strip(),
            "storage": item.storage.name,
            "drawer": item.row,
        })


class StockVoteView(ScannerAPIView):
    """Optional 👎 / — / 👍 on a bottle that already left. Deleted items are OK."""

    def patch(self, request, pk):
        household = self.get_household(request)
        try:
            item = StorageItem.objects.get(id=pk, wine__household=household)
        except StorageItem.DoesNotExist:
            return Response(
                {"error": "Item not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = StockVoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item.vote = serializer.validated_data["vote"]
        item.save(update_fields=["vote"])
        return Response({
            "item_id": item.id,
            "vote": item.vote,
            "wine_id": item.wine_id,
        })


class WineImageUploadView(ScannerAPIView):
    """Upload a label photo for a wine."""

    parser_classes = [MultiPartParser]

    def post(self, request, pk):
        household = self.get_household(request)
        try:
            wine = Wine.objects.get(id=pk, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        image_file = request.FILES.get("image")
        if not image_file:
            return Response(
                {"error": "No image file provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        image_type = request.data.get("image_type", "FR")
        if image_type not in [c[0] for c in ImageType.choices]:
            image_type = "FR"

        # Replace existing image of same type
        WineImage.objects.filter(
            wine=wine, image_type=image_type
        ).delete()

        wine_image = WineImage.objects.create(
            wine=wine,
            user=request.user,
            image=image_file,
            image_type=image_type,
        )

        return Response(
            {
                "id": wine_image.id,
                "wine_id": wine.id,
                "image_url": request.build_absolute_uri(wine_image.image.url),
                "image_type": image_type,
            },
            status=status.HTTP_201_CREATED,
        )

    def get(self, request, pk):
        """Get image URLs for a wine."""
        household = self.get_household(request)
        try:
            wine = Wine.objects.get(id=pk, household=household)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        images = WineImage.objects.filter(wine=wine)
        return Response({
            "wine_id": wine.id,
            "images": [
                {
                    "id": img.id,
                    "image_url": request.build_absolute_uri(img.image.url),
                    "thumbnail_url": request.build_absolute_uri(img.thumbnail.url) if img.thumbnail else None,
                    "image_type": img.image_type,
                }
                for img in images
            ],
        })


class WineImageFileView(ScannerAPIView):
    """Serve the actual image file through the authenticated API."""

    def get(self, request, pk):
        household = self.get_household(request)
        try:
            image = WineImage.objects.get(id=pk, wine__household=household)
        except WineImage.DoesNotExist:
            return Response(
                {"error": "Image not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not image.image:
            return Response(
                {"error": "No file"}, status=status.HTTP_404_NOT_FOUND
            )

        return FileResponse(image.image.open("rb"), content_type="image/jpeg")
