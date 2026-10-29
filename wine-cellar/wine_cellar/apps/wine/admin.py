from django.contrib import admin

from wine_cellar.apps.wine.models import (
    Attribute,
    CellarAchievement,
    FoodPairing,
    Grape,
    HouseholdTasteSnapshot,
    Size,
    Source,
    TasteIntent,
    Vineyard,
    Wine,
    WineList,
    WineListItem,
)


@admin.register(Wine)
class WineAdmin(admin.ModelAdmin):
    list_display = ["name", "barcode", "user"]
    fields = [
        "name",
        "barcode",
        "user",
        "location",
        "region",
        "appellation",
        "vineyard",
        "source",
        "grapes",
        "attributes",
    ]


@admin.register(CellarAchievement)
class CellarAchievementAdmin(admin.ModelAdmin):
    list_display = ["title", "household", "slug", "unlocked_at"]
    list_filter = ["household", "slug"]
    search_fields = ["title", "subject"]


@admin.register(HouseholdTasteSnapshot)
class HouseholdTasteSnapshotAdmin(admin.ModelAdmin):
    list_display = ["household", "updated_at"]


@admin.register(TasteIntent)
class TasteIntentAdmin(admin.ModelAdmin):
    list_display = ["kind", "household", "created"]
    list_filter = ["kind", "household"]


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ["name", "user"]
    fields = ["name", "user"]


@admin.register(Grape)
class GrapeAdmin(admin.ModelAdmin):
    list_display = ["name", "user"]
    fields = ["name", "user"]


@admin.register(Vineyard)
class VineyardAdmin(admin.ModelAdmin):
    list_display = ["name", "country", "user"]
    fields = ["name", "website", "country", "region", "user"]


@admin.register(FoodPairing)
class FoodPairingAdmin(admin.ModelAdmin):
    list_display = ["name", "user"]
    fields = ["name", "user"]


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ["name", "user"]
    fields = ["name", "user"]


@admin.register(Attribute)
class AttributeAdmin(admin.ModelAdmin):
    list_display = ["name", "user"]
    fields = ["name", "user"]


@admin.register(WineList)
class WineListAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "kind", "item_count"]
    list_filter = ["kind"]
    search_fields = ["name", "owner__username"]

    def item_count(self, obj):
        return obj.items.filter(moved_at__isnull=True).count()


@admin.register(WineListItem)
class WineListItemAdmin(admin.ModelAdmin):
    list_display = ["name", "list", "vintage", "moved_at", "added_by"]
    list_filter = ["source"]
    search_fields = ["name", "producer"]
