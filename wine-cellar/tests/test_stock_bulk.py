"""Catch-up apply: item_id remove, transactional bulk, batch undo."""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.tests.factories import WineFactory


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    gat = StorageFactory(user=user, household=household, name="Gat", rows=8)
    small = StorageFactory(user=user, household=household, name="Small", rows=4)
    rack = StorageFactory(user=user, household=household, name="Rack", rows=0)
    pinot = WineFactory(user=user, household=household, name="Kanonkop Pinotage", vintage=2019)
    syrah = WineFactory(user=user, household=household, name="Mullineux Syrah", vintage=2020)
    token, _ = Token.objects.get_or_create(user=user)
    return user, household, gat, small, rack, pinot, syrah, token


def _auth(client, token, household):
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    client.defaults["HTTP_X_HOUSEHOLD"] = str(household.id)
    return client


def test_remove_by_item_id_does_not_take_the_other_drawer(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    d3 = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=3)
    d5 = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=5)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-remove"),
        data={"wine_id": syrah.id, "item_id": d3.id},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["item_id"] == d3.id
    d3.refresh_from_db()
    d5.refresh_from_db()
    assert d3.deleted is True
    assert d5.deleted is False
    assert d5.row == 5


def test_remove_item_id_from_another_household_is_404(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    other = UserFactory()
    other_h = get_user_household(other)
    other_fridge = StorageFactory(user=other, household=other_h, name="Gat", rows=8)
    other_wine = WineFactory(user=other, household=other_h, name="Stolen")
    item = StorageItem.objects.create(
        storage=other_fridge, wine=other_wine, user=other, row=1,
    )
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-remove"),
        data={"item_id": item.id},
        content_type="application/json",
    )
    assert response.status_code == 404
    item.refresh_from_db()
    assert item.deleted is False


def test_bulk_drunk_one_of_x2_leaves_the_other(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    first = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=3)
    second = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=3)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "remove",
                "item_id": first.id,
                "expected_storage": "Gat",
                "expected_row": 3,
            }]
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.deleted is True
    assert second.deleted is False


def test_bulk_stale_item_rolls_back(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    stay = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    moved = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=3)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [
                {
                    "op": "remove",
                    "item_id": stay.id,
                    "expected_storage": "Gat",
                    "expected_row": 3,
                },
                {
                    "op": "remove",
                    "item_id": moved.id,
                    "expected_storage": "Gat",
                    "expected_row": 1,
                },
            ]
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "stale_item"
    stay.refresh_from_db()
    moved.refresh_from_db()
    assert stay.deleted is False
    assert moved.deleted is False


def test_bulk_duplicate_item_is_409(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [
                {"op": "remove", "item_id": item.id, "expected_storage": "Gat", "expected_row": 3},
                {
                    "op": "move",
                    "item_id": item.id,
                    "expected_storage": "Gat",
                    "expected_row": 3,
                    "storage_name": "Small",
                    "row": 1,
                },
            ]
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "duplicate_item"
    item.refresh_from_db()
    assert item.deleted is False
    assert item.row == 3


def test_bulk_unknown_storage_does_not_create_one(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "move",
                "item_id": item.id,
                "expected_storage": "Gat",
                "expected_row": 3,
                "storage_name": "Attic",
                "row": 1,
            }]
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "storage_not_found"
    from wine_cellar.apps.storage.models import Storage
    assert not Storage.objects.filter(household=household, name="Attic").exists()


def test_bulk_row_not_allowed_on_rack(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "move",
                "item_id": item.id,
                "expected_storage": "Gat",
                "expected_row": 3,
                "storage_name": "Rack",
                "row": 1,
            }]
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "row_not_allowed"


def test_bulk_undo_restores_remove_and_patches_move(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    drunk = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    shifted = StorageItem.objects.create(storage=gat, wine=syrah, user=user, row=3)
    _auth(client, token, household)

    applied = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [
                {
                    "op": "remove",
                    "item_id": drunk.id,
                    "expected_storage": "Gat",
                    "expected_row": 3,
                },
                {
                    "op": "move",
                    "item_id": shifted.id,
                    "expected_storage": "Gat",
                    "expected_row": 3,
                    "storage_name": "Small",
                    "row": 2,
                },
            ]
        },
        content_type="application/json",
    )
    assert applied.status_code == 200, applied.content
    body = applied.json()
    drunk.refresh_from_db()
    shifted.refresh_from_db()
    assert drunk.deleted is True
    assert shifted.storage_id == small.id
    assert shifted.row == 2

    undone = client.post(
        reverse("api-stock-bulk-undo", args=[body["batch_id"]]),
        content_type="application/json",
    )
    assert undone.status_code == 200, undone.content
    drunk.refresh_from_db()
    shifted.refresh_from_db()
    assert drunk.deleted is False
    assert shifted.storage_id == gat.id
    assert shifted.row == 3


def test_bulk_undo_of_a_move_does_not_undelete(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    applied = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "move",
                "item_id": item.id,
                "expected_storage": "Gat",
                "expected_row": 3,
                "storage_name": "Small",
                "row": 1,
            }]
        },
        content_type="application/json",
    )
    assert applied.status_code == 200
    item.refresh_from_db()
    item.deleted = True
    item.save(update_fields=["deleted", "removed_at"])

    undone = client.post(
        reverse("api-stock-bulk-undo", args=[applied.json()["batch_id"]]),
        content_type="application/json",
    )
    assert undone.status_code == 409
    assert undone.json()["error"] == "stale_item"
    item.refresh_from_db()
    assert item.deleted is True
    assert item.storage_id == small.id


def _move(client, item, storage_name, row, expected_row=3):
    return client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "move",
                "item_id": item.id,
                "expected_storage": "Gat",
                "expected_row": expected_row,
                "storage_name": storage_name,
                "row": row,
            }]
        },
        content_type="application/json",
    )


def test_bulk_move_to_a_drawer_fridge_with_no_drawer(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    response = _move(client, item, "Small", None)
    assert response.status_code == 200, response.content
    item.refresh_from_db()
    assert item.storage_id == small.id
    assert item.row is None


def test_bulk_move_to_a_drawer_that_does_not_exist(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    response = _move(client, item, "Small", 9)
    assert response.status_code == 409
    assert response.json()["error"] == "row_not_allowed"
    item.refresh_from_db()
    assert item.storage_id == gat.id


def test_bulk_undo_of_a_move_refuses_a_bottle_moved_again(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)

    applied = _move(client, item, "Small", 1)
    assert applied.status_code == 200
    item.refresh_from_db()
    item.row = 2
    item.save(update_fields=["row"])

    undone = client.post(
        reverse("api-stock-bulk-undo", args=[applied.json()["batch_id"]]),
        content_type="application/json",
    )
    assert undone.status_code == 409
    assert undone.json()["error"] == "stale_item"
    item.refresh_from_db()
    assert item.storage_id == small.id
    assert item.row == 2


def test_bulk_undo_of_a_drink_clears_the_vote(client, cellar):
    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)
    applied = client.post(
        reverse("api-stock-bulk"),
        data={"ops": [{
            "op": "remove", "item_id": item.id,
            "expected_storage": "Gat", "expected_row": 3,
        }]},
        content_type="application/json",
    )
    assert applied.status_code == 200
    item.refresh_from_db()
    item.vote = "up"
    item.save(update_fields=["vote"])

    undone = client.post(
        reverse("api-stock-bulk-undo", args=[applied.json()["batch_id"]]),
        content_type="application/json",
    )
    assert undone.status_code == 200, undone.content
    item.refresh_from_db()
    assert item.deleted is False
    assert item.vote is None


def test_restore_window_is_still_ten_minutes(client, cellar):
    from datetime import timedelta

    from django.utils import timezone

    user, household, gat, small, rack, pinot, syrah, token = cellar
    item = StorageItem.objects.create(storage=gat, wine=pinot, user=user, row=3)
    _auth(client, token, household)
    applied = client.post(
        reverse("api-stock-bulk"),
        data={
            "ops": [{
                "op": "remove",
                "item_id": item.id,
                "expected_storage": "Gat",
                "expected_row": 3,
            }]
        },
        content_type="application/json",
    )
    assert applied.status_code == 200
    item.refresh_from_db()
    item.removed_at = timezone.now() - timedelta(minutes=11)
    item.save(update_fields=["removed_at"])

    undone = client.post(
        reverse("api-stock-bulk-undo", args=[applied.json()["batch_id"]]),
        content_type="application/json",
    )
    assert undone.status_code == 409
    assert undone.json()["error"] == "too_late"
    item.refresh_from_db()
    assert item.deleted is True
