from django.contrib.auth.decorators import login_not_required
from django.db.models import Count, Q
from django.http import FileResponse
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.parsers import MultiPartParser

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import ImageType, Wine, WineImage

from .serializers import (
    StockAddSerializer,
    StockMoveSerializer,
    StockRemoveSerializer,
    StockSetSerializer,
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

        Priority:
        1. X-Household header (must be a household the user belongs to)
        2. User's default household (is_default=True)
        3. User's first household
        """
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        if header_id:
            try:
                membership = HouseholdMembership.objects.select_related("household").get(
                    user=request.user, household_id=int(header_id),
                )
                return membership.household
            except (HouseholdMembership.DoesNotExist, ValueError):
                pass

        # Default household
        membership = HouseholdMembership.objects.filter(
            user=request.user, is_default=True,
        ).select_related("household").first()
        if membership:
            return membership.household

        # Fallback: first household
        membership = HouseholdMembership.objects.filter(
            user=request.user,
        ).select_related("household").first()
        if membership:
            return membership.household

        # No household at all — shouldn't happen after migration
        raise Household.DoesNotExist("User has no household")


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

        item = StorageItem.objects.create(
            storage=storage, wine=wine, user=request.user, price=price, row=row
        )

        return Response(
            {
                "item_id": item.id,
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
                "storage": storage.name,
                "row": row,
                "action": "added",
            },
            status=status.HTTP_201_CREATED,
        )


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
        if storage_name:
            items = items.filter(storage__name=storage_name)
        item = items.order_by("-created").first()

        if not item:
            return Response(
                {"error": "No stock remaining for this wine"},
                status=status.HTTP_409_CONFLICT,
            )

        item.deleted = True
        item.save()

        return Response(
            {
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
                "action": "removed",
            }
        )


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
            # Need to remove items
            items = StorageItem.objects.filter(
                wine=wine, wine__household=household, deleted=False
            )
            if storage_name:
                items = items.filter(storage__name=storage_name)
            items = items.order_by("-created")[: abs(diff)]
            for item in items:
                item.deleted = True
                item.save()

        return Response(
            {
                "wine_id": wine.id,
                "wine_name": f"{wine.name} {wine.vintage or ''}".strip(),
                "stock_count": wine.total_stock,
                "action": "set",
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
                # Extract producer from comment ("Producer: X | description")
                producer = ""
                if wine.comment:
                    for part in wine.comment.split("|"):
                        part = part.strip()
                        if part.startswith("Producer:"):
                            producer = part[len("Producer:"):].strip()
                            break

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

        # Update comment from producer + description
        producer = data.get("producer", "")
        description = data.get("description", "")
        if producer or description:
            parts = []
            if producer:
                parts.append(f"Producer: {producer}")
            if description:
                parts.append(description)
            wine.comment = " | ".join(parts)[:500]

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
    """Refresh critic_score, vivino_rating, market_price by re-asking Grok.

    Text-only call (no photo). Currency comes from the requesting user's settings.
    Only writes fields when Grok returns reference_confidence >= "medium".
    """

    # Allow web (session) and API (token) callers — the refresh button on the
    # wine detail page calls this directly from the user's session.
    authentication_classes = [SessionAuthentication, TokenAuthentication]

    def post(self, request, pk):
        from django.utils import timezone

        from wine_cellar.apps.wine.grok_references import fetch_references, validate_reference_field

        household = self.get_household(request)
        try:
            wine = Wine.objects.get(id=pk, household=household)
        except Wine.DoesNotExist:
            return Response({"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND)

        currency = "USD"
        country = ""
        if hasattr(request.user, "user_settings"):
            currency = request.user.user_settings.currency or "USD"
            country = request.user.user_settings.country or ""

        # Producer hint extracted from comment ("Producer: X | ...") or first vineyard
        producer = ""
        if wine.comment and wine.comment.startswith("Producer: "):
            producer = wine.comment.split(" | ", 1)[0][len("Producer: "):]
        elif wine.vineyard.exists():
            producer = wine.vineyard.first().name

        data = fetch_references(
            name=wine.name,
            producer=producer,
            vintage=wine.vintage,
            target_currency=currency,
            target_country=country,
            country=wine.country or "",
            region=wine.region.name if wine.region else "",
            appellation=wine.appellation.name if wine.appellation else "",
            grape=", ".join(g.name for g in wine.grapes.all()),
            vineyard=", ".join(v.name for v in wine.vineyard.all()),
        )
        if data is None:
            return Response(
                {"error": "Grok request failed"}, status=status.HTTP_502_BAD_GATEWAY,
            )

        # Per-field preservation: each field keeps its prior value unless THIS
        # refresh produces a valid replacement for that specific field. A bad
        # search day for one field doesn't destroy good prior data.
        rank = {"high": 3, "medium": 2, "low": 1}
        persisted_confidences = []
        details = {"critic_score": None, "vivino_rating": None, "market_price": None}

        critic = data.get("critic_score")
        if validate_reference_field(critic, data.get("critic_score_confidence"), data.get("critic_score_reasoning")):
            if isinstance(critic, (int, float)) and 0 <= critic <= 100:
                wine.critic_score = int(critic)
                conf = data.get("critic_score_confidence", "").lower()
                persisted_confidences.append(conf)
                details["critic_score"] = {"value": int(critic), "confidence": conf}

        vivino = data.get("vivino_rating")
        if validate_reference_field(vivino, data.get("vivino_rating_confidence"), data.get("vivino_rating_reasoning")):
            if isinstance(vivino, (int, float)) and 0 <= vivino <= 5:
                wine.vivino_rating = round(float(vivino), 1)
                conf = data.get("vivino_rating_confidence", "").lower()
                persisted_confidences.append(conf)
                details["vivino_rating"] = {"value": round(float(vivino), 1), "confidence": conf}

        price = data.get("market_price")
        if validate_reference_field(price, data.get("market_price_confidence"), data.get("market_price_reasoning")):
            if isinstance(price, (int, float)) and price >= 0:
                wine.market_price = price
                currency_returned = data.get("market_price_currency") or currency
                if isinstance(currency_returned, str):
                    wine.market_price_currency = currency_returned.upper()[:3]
                conf = data.get("market_price_confidence", "").lower()
                persisted_confidences.append(conf)
                details["market_price"] = {
                    "value": float(price), "currency": wine.market_price_currency,
                    "confidence": conf,
                }

        if not persisted_confidences:
            # Nothing landed this run — wine fields are untouched (per-field
            # preservation), so just report that no update happened.
            kept_any = any(getattr(wine, f) is not None for f in (
                "critic_score", "vivino_rating", "market_price",
            ))
            return Response({
                "updated": False,
                "confidence": "low",
                "message": "Grok's live search did not find usable references for this wine. Existing values (if any) were kept.",
                "details": details,
                "kept_prior": kept_any,
            })

        # Aggregate confidence: highest of THIS run's new fields and any prior
        # confidence that still applies to kept fields. Avoids regressing the
        # aggregate when a high-confidence prior field is still in place.
        candidates = list(persisted_confidences)
        if wine.reference_confidence:
            candidates.append(wine.reference_confidence.lower())
        wine.reference_confidence = max(candidates, key=lambda c: rank.get(c, 0))
        wine.reference_updated = timezone.now()
        wine.save()

        return Response({
            "updated": True,
            "confidence": wine.reference_confidence,
            "critic_score": wine.critic_score,
            "vivino_rating": float(wine.vivino_rating) if wine.vivino_rating is not None else None,
            "category_rating": wine.category_rating,
            "market_price": float(wine.market_price) if wine.market_price is not None else None,
            "market_price_currency": wine.market_price_currency,
            "details": details,
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
