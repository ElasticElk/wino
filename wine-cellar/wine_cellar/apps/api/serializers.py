from rest_framework import serializers

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Grape, Region, Wine


class WineSerializer(serializers.ModelSerializer):
    stock_count = serializers.IntegerField(source="total_stock", read_only=True)
    grape_variety = serializers.CharField(source="get_grapes", read_only=True)

    class Meta:
        model = Wine
        fields = [
            "id",
            "name",
            "vintage",
            "wine_type",
            "country",
            "abv",
            "price",
            "barcode",
            "comment",
            "grape_variety",
            "stock_count",
        ]
        read_only_fields = ["id"]


class WineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    vintage = serializers.IntegerField(required=False, allow_null=True)
    producer = serializers.CharField(max_length=100, required=False, default="")
    grape_variety = serializers.CharField(max_length=100, required=False, default="")
    region = serializers.CharField(max_length=100, required=False, default="")
    country = serializers.CharField(max_length=3, required=False, default="CH")
    abv = serializers.FloatField(required=False, allow_null=True)
    wine_type = serializers.CharField(max_length=2, required=False, default="RE")
    barcode = serializers.CharField(max_length=100, required=False, default="")
    price = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True
    )
    description = serializers.CharField(max_length=200, required=False, default="")
    rating = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=10
    )

    def create(self, validated_data):
        user = self.context["request"].user
        grape_name = validated_data.pop("grape_variety", "")
        region_name = validated_data.pop("region", "")
        producer = validated_data.pop("producer", "")
        description = validated_data.pop("description", "")
        rating = validated_data.pop("rating", None)

        # Build comment: "Producer: X | Tasting notes here"
        parts = []
        if producer:
            parts.append(f"Producer: {producer}")
        if description:
            parts.append(description)
        comment = " | ".join(parts)[:250]  # respect max_length

        wine = Wine.objects.create(
            user=user,
            name=validated_data["name"],
            barcode=validated_data.get("barcode", ""),
            wine_type=validated_data.get("wine_type", "RE"),
            vintage=validated_data.get("vintage"),
            country=validated_data.get("country", "CH"),
            abv=validated_data.get("abv"),
            price=validated_data.get("price"),
            rating=rating,
            comment=comment,
        )

        if grape_name:
            grape, _ = Grape.objects.get_or_create(name=grape_name, user=user)
            wine.grapes.add(grape)

        if region_name:
            region, _ = Region.objects.get_or_create(name=region_name, user=user)
            wine.region = region
            wine.save()

        return wine


class StockAddSerializer(serializers.Serializer):
    wine_id = serializers.IntegerField()
    storage_name = serializers.CharField(max_length=100, default="Wine Fridge")
    row = serializers.IntegerField(required=False, allow_null=True)
    price = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True
    )


class StockRemoveSerializer(serializers.Serializer):
    wine_id = serializers.IntegerField()
    storage_name = serializers.CharField(max_length=100, required=False, default="")


class StockSetSerializer(serializers.Serializer):
    wine_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=0)
    storage_name = serializers.CharField(max_length=100, required=False, default="")


class StockMoveSerializer(serializers.Serializer):
    storage_name = serializers.CharField(max_length=100, required=False)
    row = serializers.IntegerField(required=False, allow_null=True)


class StorageItemSerializer(serializers.ModelSerializer):
    wine_name = serializers.CharField(source="wine.name", read_only=True)
    wine_vintage = serializers.IntegerField(source="wine.vintage", read_only=True)

    class Meta:
        model = StorageItem
        fields = ["id", "wine_name", "wine_vintage", "storage", "price", "created"]
