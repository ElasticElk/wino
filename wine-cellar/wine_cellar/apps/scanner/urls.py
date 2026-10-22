from django.urls import path

from . import views

urlpatterns = [
    path("config/", views.ScannerConfigView.as_view(), name="api-scanner-config"),
    path("household/", views.HouseholdView.as_view(), name="api-household"),
    path("household/add-member/", views.HouseholdAddMemberView.as_view(), name="api-household-add-member"),
    path("household/remove-member/", views.HouseholdRemoveMemberView.as_view(), name="api-household-remove-member"),
    path("households/", views.HouseholdsListView.as_view(), name="api-households-list"),
    path("achievements/", views.HouseholdAchievementsView.as_view(), name="api-household-achievements"),
    path("taste/", views.HouseholdTasteView.as_view(), name="api-household-taste"),
    path(
        "taste/<int:pk>/",
        views.HouseholdTasteIntentView.as_view(),
        name="api-household-taste-intent",
    ),
    path("whois/", views.ScannerWhoisView.as_view(), name="api-scanner-whois"),
    path("stats/", views.ScannerStatsView.as_view(), name="api-scanner-stats"),
    path("chat-stats/", views.ScannerChatStatsView.as_view(), name="api-scanner-chat-stats"),
    path("invites/", views.HouseholdInviteCreateView.as_view(), name="api-scanner-invites"),
    path("invites/redeem/", views.HouseholdInviteRedeemView.as_view(), name="api-scanner-invites-redeem"),
    path("invites/cancel/", views.HouseholdInviteCancelView.as_view(), name="api-scanner-invites-cancel"),
    path("invites/preview/", views.HouseholdInvitePreviewView.as_view(), name="api-scanner-invites-preview"),
    path("settings/", views.ScannerSettingsView.as_view(), name="api-scanner-settings"),
]
