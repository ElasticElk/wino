from django.urls import path

from wine_cellar.apps.club import views

urlpatterns = [
    path("", views.ClubListView.as_view(), name="club-list"),
    path("<int:pk>/", views.ClubDetailView.as_view(), name="club-detail"),
    path("<int:pk>/settings/", views.ClubSettingsView.as_view(), name="club-settings"),
]
