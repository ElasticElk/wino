from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Wine

from .serializers import (
    StockAddSerializer,
    StockMoveSerializer,
    StockRemoveSerializer,
    StockSetSerializer,
    WineCreateSerializer,
    WineSerializer,
)


@method_decorator(login_not_required, name="dispatch")
class ScannerAPIView(APIView):
    """Base view that bypasses Django's LoginRequiredMiddleware."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]


class WineByBarcodeView(ScannerAPIView):
    def get(self, request, ean):
        wines = Wine.objects.filter(barcode=ean, user=request.user)
        serializer = WineSerializer(wines, many=True)
        return Response({"count": wines.count(), "wines": serializer.data})


class StockAddView(ScannerAPIView):
    def post(self, request):
        serializer = StockAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        wine_id = serializer.validated_data["wine_id"]
        storage_name = serializer.validated_data["storage_name"]
        price = serializer.validated_data.get("price")
        row = serializer.validated_data.get("row")

        try:
            wine = Wine.objects.get(id=wine_id, user=request.user)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        storage, _ = Storage.objects.get_or_create(
            name=storage_name,
            user=request.user,
            defaults={"location": storage_name, "rows": 0, "columns": 0},
        )

        StorageItem.objects.create(
            storage=storage, wine=wine, user=request.user, price=price, row=row
        )

        return Response(
            {
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

        wine_id = serializer.validated_data["wine_id"]
        storage_name = serializer.validated_data.get("storage_name", "")

        try:
            wine = Wine.objects.get(id=wine_id, user=request.user)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        items = StorageItem.objects.filter(
            wine=wine, user=request.user, deleted=False
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

        wine_id = serializer.validated_data["wine_id"]
        target = serializer.validated_data["quantity"]
        storage_name = serializer.validated_data.get("storage_name", "")

        try:
            wine = Wine.objects.get(id=wine_id, user=request.user)
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
                user=request.user,
                defaults={"location": storage_name or "Wine Fridge", "rows": 0, "columns": 0},
            )
            for _ in range(diff):
                StorageItem.objects.create(
                    storage=storage, wine=wine, user=request.user
                )
        elif diff < 0:
            # Need to remove items
            items = StorageItem.objects.filter(
                wine=wine, user=request.user, deleted=False
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
        try:
            wine = Wine.objects.get(id=pk, user=request.user)
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
        serializer = WineCreateSerializer(
            data=request.data, context={"request": request}
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
        storage_filter = request.query_params.get("storage", "")

        items = StorageItem.objects.filter(
            user=request.user, deleted=False
        ).select_related("wine", "storage")

        if storage_filter:
            items = items.filter(storage__name=storage_filter)

        # Group by wine
        wine_map = {}
        for item in items:
            wine = item.wine
            if wine.id not in wine_map:
                wine_map[wine.id] = {
                    "id": wine.id,
                    "name": f"{wine.name} {wine.vintage or ''}".strip(),
                    "wine_type": wine.wine_type,
                    "barcode": wine.barcode or "",
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
        try:
            wine = Wine.objects.get(id=pk, user=request.user)
        except Wine.DoesNotExist:
            return Response(
                {"error": "Wine not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = WineCreateSerializer(
            data=request.data, context={"request": request}, partial=True
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Update simple fields
        for field in ("name", "wine_type", "vintage", "country", "abv", "price", "barcode"):
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
            wine.comment = " | ".join(parts)[:250]

        rating = data.get("rating")
        if rating is not None:
            wine.rating = rating

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

            region, _ = Region.objects.get_or_create(
                name=region_name, user=request.user
            )
            wine.region = region
            wine.save()

        return Response(
            {
                "id": wine.id,
                "name": f"{wine.name} {wine.vintage or ''}".strip(),
                "wine_type": wine.wine_type,
                "stock_count": wine.total_stock,
                "barcode": wine.barcode or "",
            }
        )


class StorageListView(ScannerAPIView):
    """List all storages (fridges) for the user."""

    def get(self, request):
        storages = Storage.objects.filter(user=request.user).order_by("name")
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
        try:
            item = StorageItem.objects.get(
                id=pk, user=request.user, deleted=False
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
                user=request.user,
                defaults={"location": storage_name, "rows": 0, "columns": 0},
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
