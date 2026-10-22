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
    stock_count = serializers.SerializerMethodField()
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
            "producer",
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
            "critic_score",
            "vivino_rating",
            "category_rating",
            "market_price",
            "market_price_currency",
            "reference_confidence",
            "reference_updated",
        ]
        read_only_fields = ["id"]

    def get_stock_count(self, obj):
        """Prefer the annotation from wine_queryset(); fall back to the property.

        Wine.total_stock issues its own COUNT per wine, which is the single
        biggest contributor to the N+1 on list endpoints.
        """
        annotated = getattr(obj, "stock_count_annotated", None)
        return obj.total_stock if annotated is None else annotated

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
    # Grok reference fields — each has its own confidence + reasoning so they
    # can be persisted independently (e.g. price known but no critic score).
    critic_score = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=100,
    )
    critic_score_confidence = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    critic_score_reasoning = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    vivino_rating = serializers.DecimalField(
        max_digits=3, decimal_places=1, required=False, allow_null=True,
        min_value=0, max_value=5,
    )
    vivino_rating_confidence = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    vivino_rating_reasoning = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    market_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
    )
    market_price_currency = serializers.CharField(
        max_length=3, required=False, allow_null=True, allow_blank=True,
    )
    market_price_confidence = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    market_price_reasoning = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # Aggregate confidence (set by the scanner merger) — kept for compatibility
    # but per-field _confidence values are what gate persistence in create().
    reference_confidence = serializers.CharField(required=False, allow_null=True, allow_blank=True)

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

        # producer and description are separate columns; the comment holds
        # only the tasting notes.
        comment = (description or "")[:500]

        # Size
        size_obj = None
        if size_litres:
            size_obj, _ = Size.objects.get_or_create(
                name=size_litres, user=user
            )

        # Per-field reference persistence — each of critic/vivino/market_price has
        # its own confidence + reasoning and is validated/persisted independently.
        from wine_cellar.apps.wine.grok_references import validate_reference_field

        ref_pulls = {}
        for f in (
            "critic_score", "critic_score_confidence", "critic_score_reasoning",
            "vivino_rating", "vivino_rating_confidence", "vivino_rating_reasoning",
            "market_price", "market_price_currency",
            "market_price_confidence", "market_price_reasoning",
            "reference_confidence",
        ):
            ref_pulls[f] = validated_data.pop(f, None)

        rank = {"high": 3, "medium": 2, "low": 1}
        persisted_confidences = []
        reference_kwargs = {}

        if validate_reference_field(
            ref_pulls["critic_score"],
            ref_pulls["critic_score_confidence"],
            ref_pulls["critic_score_reasoning"],
        ):
            reference_kwargs["critic_score"] = ref_pulls["critic_score"]
            persisted_confidences.append((ref_pulls["critic_score_confidence"] or "").lower())

        if validate_reference_field(
            ref_pulls["vivino_rating"],
            ref_pulls["vivino_rating_confidence"],
            ref_pulls["vivino_rating_reasoning"],
        ):
            reference_kwargs["vivino_rating"] = ref_pulls["vivino_rating"]
            persisted_confidences.append((ref_pulls["vivino_rating_confidence"] or "").lower())

        if validate_reference_field(
            ref_pulls["market_price"],
            ref_pulls["market_price_confidence"],
            ref_pulls["market_price_reasoning"],
        ):
            reference_kwargs["market_price"] = ref_pulls["market_price"]
            if ref_pulls["market_price_currency"]:
                reference_kwargs["market_price_currency"] = ref_pulls["market_price_currency"]
            persisted_confidences.append((ref_pulls["market_price_confidence"] or "").lower())

        if reference_kwargs:
            from django.utils import timezone
            reference_kwargs["reference_confidence"] = max(
                persisted_confidences, key=lambda c: rank.get(c, 0),
            )
            reference_kwargs["reference_updated"] = timezone.now()

        wine = Wine.objects.create(
            user=user,
            household=household,
            name=validated_data["name"],
            producer=producer,
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
            **reference_kwargs,
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
    drank = serializers.BooleanField(required=False, default=False)
    confirm_duplicate = serializers.BooleanField(required=False, default=False)
    # Opt-in. Only the label-photo Add/Drank card sends this; a case of six,
    # fridge +, undo, and the kitchen scanner must not 409 a real bottle.
    check_duplicate = serializers.BooleanField(required=False, default=False)


class StockVoteSerializer(serializers.Serializer):
    vote = serializers.ChoiceField(choices=["down", "meh", "up"])


class StockRemoveSerializer(serializers.Serializer):
    wine_id = serializers.IntegerField(required=False)
    item_id = serializers.IntegerField(required=False)
    storage_name = serializers.CharField(max_length=100, required=False, default="")

    def validate(self, data):
        if not data.get("wine_id") and not data.get("item_id"):
            raise serializers.ValidationError("wine_id or item_id required")
        return data


class StockBulkOpSerializer(serializers.Serializer):
    op = serializers.ChoiceField(choices=["remove", "move"])
    item_id = serializers.IntegerField()
    expected_storage = serializers.CharField(max_length=100, required=False, default="")
    expected_row = serializers.IntegerField(required=False, allow_null=True)
    storage_name = serializers.CharField(max_length=100, required=False)
    row = serializers.IntegerField(required=False, allow_null=True)


class StockBulkSerializer(serializers.Serializer):
    ops = StockBulkOpSerializer(many=True)

    def validate_ops(self, ops):
        if not ops:
            raise serializers.ValidationError("ops required")
        if len(ops) > 30:
            raise serializers.ValidationError("cap_exceeded")
        ids = [op["item_id"] for op in ops]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("duplicate_item")
        return ops


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
