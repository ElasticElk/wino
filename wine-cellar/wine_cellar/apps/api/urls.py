from django.urls import path

from . import views

urlpatterns = [
    path(
        "wines/by-barcode/<str:ean>/",
        views.WineByBarcodeView.as_view(),
        name="api-wine-by-barcode",
    ),
    path(
        "wines/in-stock/",
        views.WineInStockView.as_view(),
        name="api-wines-in-stock",
    ),
    path("stock/add/", views.StockAddView.as_view(), name="api-stock-add"),
    path("stock/remove/", views.StockRemoveView.as_view(), name="api-stock-remove"),
    path("stock/set/", views.StockSetView.as_view(), name="api-stock-set"),
    path(
        "stock/<int:pk>/move/",
        views.StockMoveView.as_view(),
        name="api-stock-move",
    ),
    path(
        "storages/",
        views.StorageListView.as_view(),
        name="api-storage-list",
    ),
    path(
        "wines/<int:pk>/stock/",
        views.WineStockView.as_view(),
        name="api-wine-stock",
    ),
    path(
        "wines/<int:pk>/",
        views.WineUpdateView.as_view(),
        name="api-wine-update",
    ),
    path("wines/", views.WineCreateView.as_view(), name="api-wine-create"),
]
