from rest_framework import serializers

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import (
    Appellation,
    Attribute,
    FoodPairing,
    Grape,
    Region,
    Size,
    Vineyard,
    Wine,
)


class WineSerializer(serializers.ModelSerializer):
    stock_count = serializers.IntegerField(source="total_stock", read_only=True)
    grape_variety = serializers.CharField(source="get_grapes", read_only=True)
    region_name = serializers.CharField(source="region.name", read_only=True, default="")
    appellation_name = serializers.CharField(
        source="appellation.name", read_only=True, default=""
    )
    food_pairings = serializers.SerializerMethodField()
    attributes = serializers.SerializerMethodField()
    vineyards = serializers.SerializerMethodField()
    size_ml = serializers.SerializerMethodField()

    class Meta:
        model = Wine
        fields = [
            "id",
            "name",
            "vintage",
            "wine_type",
            "category",
            "country",
            "abv",
            "price",
            "barcode",
            "comment",
            "rating",
            "grape_variety",
            "region_name",
            "appellation_name",
            "food_pairings",
            "attributes",
            "vineyards",
            "size_ml",
            "drink_by",
            "stock_count",
        ]
        read_only_fields = ["id"]

    def get_food_pairings(self, obj):
        return [fp.name for fp in obj.food_pairings.all()]

    def get_attributes(self, obj):
        return [a.name for a in obj.attributes.all()]

    def get_vineyards(self, obj):
        return [v.name for v in obj.vineyard.all()]

    def get_size_ml(self, obj):
        return obj.size.name if obj.size else None


class WineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    vintage = serializers.IntegerField(required=False, allow_null=True)
    producer = serializers.CharField(max_length=100, required=False, default="")
    grape_variety = serializers.CharField(max_length=100, required=False, default="")
    region = serializers.CharField(max_length=100, required=False, default="")
    appellation = serializers.CharField(max_length=100, required=False, default="")
    country = serializers.CharField(max_length=3, required=False, default="CH")
    abv = serializers.FloatField(required=False, allow_null=True)
    wine_type = serializers.CharField(max_length=2, required=False, default="RE")
    category = serializers.CharField(max_length=2, required=False, allow_null=True, default=None)
    barcode = serializers.CharField(max_length=100, required=False, default="")
    price = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True
    )
    description = serializers.CharField(max_length=500, required=False, default="")
    rating = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=10
    )
    vineyard = serializers.CharField(max_length=100, required=False, default="", allow_blank=True)
    size = serializers.FloatField(required=False, allow_null=True)
    drink_by = serializers.DateField(required=False, allow_null=True)
    food_pairings = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False, default=list,
    )
    attributes = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False, default=list,
    )

    def create(self, validated_data):
        user = self.context["request"].user
        household = self.context.get("household")
        grape_name = validated_data.pop("grape_variety", "")
        region_name = validated_data.pop("region", "")
        appellation_name = validated_data.pop("appellation", "")
        producer = validated_data.pop("producer", "")
        description = validated_data.pop("description", "")
        rating = validated_data.pop("rating", None)
        vineyard_name = validated_data.pop("vineyard", "")
        size_litres = validated_data.pop("size", None)
        drink_by = validated_data.pop("drink_by", None)
        food_pairing_names = validated_data.pop("food_pairings", [])
        attribute_names = validated_data.pop("attributes", [])
        category = validated_data.pop("category", None)

        # Build comment: "Producer: X | Tasting notes here"
        parts = []
        if producer:
            parts.append(f"Producer: {producer}")
        if description:
            parts.append(description)
        comment = " | ".join(parts)[:500]

        # Size
        size_obj = None
        if size_litres:
            size_obj, _ = Size.objects.get_or_create(
                name=size_litres, user=user
            )

        wine = Wine.objects.create(
            user=user,
            household=household,
            name=validated_data["name"],
            barcode=validated_data.get("barcode", ""),
            wine_type=validated_data.get("wine_type", "RE"),
            category=category,
            vintage=validated_data.get("vintage"),
            country=validated_data.get("country", "CH"),
            abv=validated_data.get("abv"),
            price=validated_data.get("price"),
            rating=rating,
            comment=comment,
            size=size_obj,
            drink_by=drink_by,
        )

        if grape_name:
            grape, _ = Grape.objects.get_or_create(name=grape_name, user=user)
            wine.grapes.add(grape)

        if region_name:
            region, _ = Region.objects.get_or_create(name=region_name, user=user)
            wine.region = region
            wine.save()

        if appellation_name:
            appellation, _ = Appellation.objects.get_or_create(
                name=appellation_name, user=user
            )
            wine.appellation = appellation
            wine.save()

        if vineyard_name:
            vy, _ = Vineyard.objects.get_or_create(
                name=vineyard_name, user=user,
                defaults={"region": region_name, "country": validated_data.get("country", "")},
            )
            wine.vineyard.add(vy)

        for fp_name in food_pairing_names:
            fp, _ = FoodPairing.objects.get_or_create(name=fp_name, user=user)
            wine.food_pairings.add(fp)

        for attr_name in attribute_names:
            attr, _ = Attribute.objects.get_or_create(name=attr_name, user=user)
            wine.attributes.add(attr)

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
