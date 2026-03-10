from django.urls import path

from . import views

urlpatterns = [
    path("config/", views.ScannerConfigView.as_view(), name="api-scanner-config"),
    path("household/", views.HouseholdView.as_view(), name="api-household"),
    path("household/add-member/", views.HouseholdAddMemberView.as_view(), name="api-household-add-member"),
    path("household/remove-member/", views.HouseholdRemoveMemberView.as_view(), name="api-household-remove-member"),
    path("households/", views.HouseholdsListView.as_view(), name="api-households-list"),
]
