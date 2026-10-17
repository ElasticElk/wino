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
