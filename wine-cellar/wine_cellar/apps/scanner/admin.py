from django.contrib import admin

from .models import (
    Household, HouseholdInvite, HouseholdMembership, Scanner, ServiceConfig, TelegramUser,
)


class HouseholdMembershipInline(admin.TabularInline):
    model = HouseholdMembership
    extra = 1


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("name", "gamification_enabled", "member_count", "created")
    list_filter = ("gamification_enabled",)
    inlines = [HouseholdMembershipInline]

    def member_count(self, obj):
        return obj.memberships.count()
    member_count.short_description = "Members"


@admin.register(HouseholdMembership)
class HouseholdMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "household", "role", "is_default", "created")
    list_filter = ("role", "is_default")


@admin.register(ServiceConfig)
class ServiceConfigAdmin(admin.ModelAdmin):
    list_display = ("key", "description")
    search_fields = ("key",)


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("display_name", "chat_id", "user", "is_active", "created")
    list_filter = ("is_active",)
    search_fields = ("display_name", "chat_id")


@admin.register(HouseholdInvite)
class HouseholdInviteAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "household", "created_by", "created", "expires_at", "used_at")
    list_filter = ("kind",)
    search_fields = ("code",)
    readonly_fields = ("code", "created")


@admin.register(Scanner)
class ScannerAdmin(admin.ModelAdmin):
    list_display = ("name", "household", "user", "default_storage", "is_active", "created")
    list_filter = ("is_active", "household")
    filter_horizontal = ("notify_users",)
    readonly_fields = ("api_key",)
