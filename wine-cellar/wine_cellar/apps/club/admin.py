from django.contrib import admin

from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubDismantleBallot,
    ClubDismantleVote,
    ClubEvent,
    ClubEventDelivery,
    ClubInvite,
    ClubMembership,
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)


class ClubMembershipInline(admin.TabularInline):
    model = ClubMembership
    extra = 0


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = (
        "name", "kind", "cellar_visibility", "badges_visible",
        "sharing_version", "frozen", "created",
    )
    list_filter = ("kind", "cellar_visibility", "badges_visible", "frozen")
    search_fields = ("name",)
    inlines = [ClubMembershipInline]


@admin.register(ClubMembership)
class ClubMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user", "club", "role", "household", "feed_muted",
        "accepted_sharing_version", "is_last_used", "created",
    )
    list_filter = ("role", "feed_muted", "is_last_used")
    search_fields = ("user__username", "club__name")


@admin.register(ClubInvite)
class ClubInviteAdmin(admin.ModelAdmin):
    list_display = (
        "code", "club", "created_by", "created", "expires_at",
        "use_count", "max_uses", "revoked_at",
    )
    search_fields = ("code", "club__name")
    readonly_fields = ("code", "created")


@admin.register(ClubEvent)
class ClubEventAdmin(admin.ModelAdmin):
    list_display = (
        "id", "club", "kind", "actor", "wine_name", "vintage",
        "vote", "created", "retracted_at",
    )
    list_filter = ("kind",)
    search_fields = ("wine_name", "club__name")


@admin.register(ClubEventDelivery)
class ClubEventDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event", "chat_id", "message_id", "sent_at", "error")


class ClubDismantleBallotInline(admin.TabularInline):
    model = ClubDismantleBallot
    extra = 0


@admin.register(ClubDismantleVote)
class ClubDismantleVoteAdmin(admin.ModelAdmin):
    list_display = ("id", "club", "proposed_by", "status", "created", "expires_at")
    list_filter = ("status",)
    inlines = [ClubDismantleBallotInline]


@admin.register(ClubDismantleBallot)
class ClubDismantleBallotAdmin(admin.ModelAdmin):
    list_display = ("vote", "user", "choice", "decided_at")
    list_filter = ("choice",)


@admin.register(ClubComment)
class ClubCommentAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "user", "text", "created")
    search_fields = ("text",)


class TasteOffBottleInline(admin.TabularInline):
    model = TasteOffBottle
    extra = 0


class TasteOffScoreInline(admin.TabularInline):
    model = TasteOffScore
    extra = 0


@admin.register(TasteOff)
class TasteOffAdmin(admin.ModelAdmin):
    list_display = ("id", "club", "title", "status", "host", "created", "opened_at", "closed_at")
    list_filter = ("status",)
    inlines = [TasteOffBottleInline, TasteOffScoreInline]


@admin.register(TasteOffBottle)
class TasteOffBottleAdmin(admin.ModelAdmin):
    list_display = ("id", "tasteoff", "label", "sort_order", "wine_id")


@admin.register(TasteOffScore)
class TasteOffScoreAdmin(admin.ModelAdmin):
    list_display = ("id", "tasteoff", "bottle", "user", "score", "modified")
    list_filter = ("score",)
