from django.contrib import messages
from django.forms import model_to_dict
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DeleteView, DetailView, FormView, ListView
from django.views.generic.list import MultipleObjectMixin

from wine_cellar.apps.scanner.households import HouseholdScopedMixin
from wine_cellar.apps.storage.forms import StockForm, StorageForm
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.achievements import award_for_add
from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.taste import rebuild_snapshot


class StorageListView(HouseholdScopedMixin, ListView):
    model = Storage
    template_name = "storage_list.html"
    context_object_name = "storages"
    paginate_by = 10

    def get_queryset(self):
        return self.scoped(super().get_queryset().order_by("created"))


class StorageDetailView(HouseholdScopedMixin, DetailView, MultipleObjectMixin):
    template_name = "storage_detail.html"
    model = Storage
    paginate_by = 10

    def get_context_data(self, **kwargs):
        object = self.get_object()
        object_list = object.get_wines
        context = super(StorageDetailView, self).get_context_data(
            object_list=object_list, **kwargs
        )
        return context

    def get_queryset(self):
        return self.scoped(super().get_queryset())


class StorageCreateView(HouseholdScopedMixin, FormView):
    template_name = "storage_create.html"
    form_class = StorageForm
    success_url = reverse_lazy("storage-list")

    def form_valid(self, form):
        self.process_form_data(self.request.user, form.cleaned_data, self.household)
        return super().form_valid(form)

    @staticmethod
    def process_form_data(user, cleaned_data, household=None):
        location = cleaned_data["location"]
        description = cleaned_data["description"]
        name = cleaned_data["name"]
        rows = cleaned_data["rows"] or 0
        columns = cleaned_data["columns"] or 0

        Storage.objects.create(
            location=location,
            description=description,
            name=name,
            rows=rows,
            columns=columns,
            user=user,
            household=household,
        )


class StorageUpdateView(HouseholdScopedMixin, FormView):
    template_name = "storage_edit.html"
    form_class = StorageForm
    success_url = reverse_lazy("storage-list")

    def get_initial(self):
        initial = super().get_initial()
        storage = get_object_or_404(Storage, pk=self.kwargs["pk"])
        initial.update(model_to_dict(storage))
        return initial

    def form_valid(self, form):
        storage = get_object_or_404(
            self.scoped(Storage.objects.all()), pk=self.kwargs["pk"]
        )
        self.process_form_data(storage, self.request.user, form.cleaned_data)
        self.success_url = reverse_lazy("storage-detail", kwargs={"pk": storage.pk})
        return super().form_valid(form)

    @staticmethod
    def process_form_data(storage, user, cleaned_data):
        location = cleaned_data["location"]
        description = cleaned_data["description"]
        name = cleaned_data["name"]
        rows = cleaned_data["rows"]
        columns = cleaned_data["columns"]

        storage.location = location
        storage.description = description
        storage.name = name
        storage.rows = rows
        storage.columns = columns
        storage.user = user
        storage.save()


class StorageDeleteView(HouseholdScopedMixin, DeleteView):
    model = Storage
    template_name = "storage_confirm_delete.html"
    success_url = reverse_lazy("storage-list")

    def form_valid(self, form):
        storages = self.scoped(Storage.objects.all()).count()
        if storages <= 1:
            form.add_error(
                None,
                _(
                    "You must have at least one storage."
                    + " Cannot delete the last storage."
                ),
            )
            return self.form_invalid(form)
        return super().form_valid(form)

    def get_queryset(self):
        return self.scoped(super().get_queryset())


class StorageItemAddView(HouseholdScopedMixin, FormView):
    template_name = "stock_add.html"
    form_class = StockForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if "user" not in kwargs:
            kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        household_storages = self.scoped(Storage.objects.all())
        free_cells_by_storage = {}
        for storage in household_storages:
            if storage.rows == 0:
                free_cells_by_storage[storage.pk] = {}
                continue
            used_cells = set(
                storage.items.filter(deleted=False).values_list("row", "column")
            )
            all_rows = range(1, storage.rows + 1)
            all_columns = range(1, storage.columns + 1)
            free_cells_by_storage[storage.pk] = {}
            for row in all_rows:
                free = []
                for column in all_columns:
                    if (row, column) not in used_cells:
                        free.append(column)
                free_cells_by_storage[storage.pk][row] = free
        context["free_cells_by_storage"] = free_cells_by_storage
        return context

    def form_valid(self, form):
        wine = get_object_or_404(self.scoped(Wine.objects.all()), pk=self.kwargs["pk"])
        self.process_form_data(wine, self.request.user, form.cleaned_data)
        try:
            rebuild_snapshot(getattr(wine, "household", None))
        except Exception:
            pass
        for badge in award_for_add(
            wine, self.request.user, storage=form.cleaned_data.get("storage"),
        ):
            messages.success(
                self.request,
                f"{badge.get('emoji', '🎉')} {badge['title']} — {badge.get('detail', '')}".strip(" —"),
            )
        self.success_url = reverse_lazy("wine-detail", kwargs={"pk": wine.pk})
        return super().form_valid(form)

    @staticmethod
    def process_form_data(wine, user, cleaned_data):
        storage = cleaned_data["storage"]
        row = cleaned_data["row"]
        column = cleaned_data["column"]
        price = cleaned_data.get("price")

        StorageItem.objects.create(
            storage=storage,
            wine=wine,
            row=row,
            column=column,
            user=user,
            price=price,
        )


class StorageItemUpdateView(HouseholdScopedMixin, FormView):
    template_name = "stock_add.html"
    form_class = StockForm

    def get_initial(self):
        initial = super().get_initial()
        self.storage_item = get_object_or_404(
            self.scoped(StorageItem.objects.all(), field="wine__household"),
            pk=self.kwargs["pk"],
        )
        initial.update(model_to_dict(self.storage_item))
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if "user" not in kwargs:
            kwargs["user"] = self.request.user
        if "storage_item" not in kwargs:
            kwargs["storage_item"] = self.storage_item
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        household_storages = self.scoped(Storage.objects.all())
        free_cells_by_storage = {}

        for storage in household_storages:
            if storage.rows == 0:
                free_cells_by_storage[storage.pk] = {}
                continue

            used_cells = set(
                storage.items.filter(deleted=False)
                .exclude(pk=self.kwargs["pk"])
                .values_list("row", "column")
            )

            free_cells_by_storage[storage.pk] = {}
            for row in range(1, storage.rows + 1):
                free_cells_by_storage[storage.pk][row] = [
                    col
                    for col in range(1, storage.columns + 1)
                    if (row, col) not in used_cells
                ]

        context["free_cells_by_storage"] = free_cells_by_storage
        return context

    def form_valid(self, form):
        self.process_form_data(
            self.storage_item,
            self.request.user,
            form.cleaned_data,
        )
        self.success_url = reverse_lazy(
            "wine-detail",
            kwargs={"pk": self.storage_item.wine.pk},
        )
        return super().form_valid(form)

    @staticmethod
    def process_form_data(storage_item, user, cleaned_data):
        storage_item.storage = cleaned_data["storage"]
        storage_item.row = cleaned_data["row"]
        storage_item.column = cleaned_data["column"]
        storage_item.price = cleaned_data.get("price")
        storage_item.user = user
        storage_item.save()


class StorageItemDeleteView(HouseholdScopedMixin, DeleteView):
    model = StorageItem
    template_name = "storage_item_confirm_delete.html"

    def get_success_url(self):
        next = self.request.GET.get("next")
        if next == "storage":
            return reverse_lazy("storage-detail", kwargs={"pk": self.object.storage.pk})
        return reverse_lazy("wine-detail", kwargs={"pk": self.object.wine.pk})

    def get_queryset(self):
        qs = super().get_queryset()
        return self.scoped(qs.filter(deleted=False), field="wine__household")

    def form_valid(self, form):
        self.object = self.get_object()
        self.object.deleted = True
        self.object.save(update_fields=["deleted"])
        try:
            rebuild_snapshot(getattr(self.object.wine, "household", None))
        except Exception:
            pass
        return redirect(self.get_success_url())


class StorageItemHistoryView(HouseholdScopedMixin, ListView):
    model = StorageItem
    template_name = "storage_item_history.html"
    context_object_name = "storage_items"
    paginate_by = 10

    def get_queryset(self):
        qs = super().get_queryset().order_by("-created")
        return self.scoped(qs.filter(deleted=True), field="wine__household")
