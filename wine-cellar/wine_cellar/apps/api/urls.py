from django.urls import path

from . import list_views, views

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
    path("stock/bulk/", views.StockBulkView.as_view(), name="api-stock-bulk"),
    path(
        "stock/bulk/<uuid:batch_id>/undo/",
        views.StockBulkUndoView.as_view(),
        name="api-stock-bulk-undo",
    ),
    path(
        "stock/restore-latest/",
        views.StockRestoreLatestView.as_view(),
        name="api-stock-restore-latest",
    ),
    path(
        "stock/<int:pk>/restore/",
        views.StockRestoreView.as_view(),
        name="api-stock-restore",
    ),
    path("stock/set/", views.StockSetView.as_view(), name="api-stock-set"),
    path(
        "stock/history/",
        views.StockHistoryView.as_view(),
        name="api-stock-history",
    ),
    path(
        "stock/<int:pk>/move/",
        views.StockMoveView.as_view(),
        name="api-stock-move",
    ),
    path(
        "stock/<int:pk>/vote/",
        views.StockVoteView.as_view(),
        name="api-stock-vote",
    ),
    path(
        "storages/",
        views.StorageListView.as_view(),
        name="api-storage-list",
    ),
    path(
        "wines/<int:pk>/detail/",
        views.WineDetailView.as_view(),
        name="api-wine-detail",
    ),
    path(
        "wines/<int:pk>/stock/",
        views.WineStockView.as_view(),
        name="api-wine-stock",
    ),
    path(
        "wines/<int:pk>/images/",
        views.WineImageUploadView.as_view(),
        name="api-wine-images",
    ),
    path(
        "wines/<int:pk>/",
        views.WineUpdateView.as_view(),
        name="api-wine-update",
    ),
    path(
        "wines/<int:pk>/refresh-references/",
        views.WineRefreshReferencesView.as_view(),
        name="api-wine-refresh-references",
    ),
    path(
        "wines/<int:pk>/apply-references/",
        views.WineApplyReferencesView.as_view(),
        name="api-wine-apply-references",
    ),
    path(
        "images/<int:pk>/file/",
        views.WineImageFileView.as_view(),
        name="api-image-file",
    ),
    path(
        "references/lookup/",
        views.ReferenceLookupView.as_view(),
        name="api-reference-lookup",
    ),
    path("wines/search/", views.WineSearchView.as_view(), name="api-wine-search"),
    path("wines/all/", views.WineListAllView.as_view(), name="api-wine-list-all"),
    path("wines/", views.WineCreateView.as_view(), name="api-wine-create"),
    path("lists/", list_views.WineListCollectionView.as_view(), name="api-lists"),
    path(
        "lists/<int:pk>/",
        list_views.WineListDetailView.as_view(),
        name="api-list-detail",
    ),
    path(
        "lists/<int:pk>/items/",
        list_views.WineListItemCreateView.as_view(),
        name="api-list-items",
    ),
    path(
        "lists/<int:pk>/items/<int:item_id>/",
        list_views.WineListItemDeleteView.as_view(),
        name="api-list-item-delete",
    ),
    path(
        "lists/<int:pk>/items/<int:item_id>/to-cellar/",
        list_views.WineListItemToCellarView.as_view(),
        name="api-list-item-to-cellar",
    ),
]
