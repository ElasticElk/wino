"""Gun IN/OUT must name the fridge they touched so Telegram can correct it."""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.tests.factories import WineFactory, WineImageFactory


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    big = StorageFactory(user=user, household=household, name="Big Fridge", rows=8)
    kitchen = StorageFactory(user=user, household=household, name="Kitchen", rows=0)
    wine = WineFactory(user=user, household=household, name="Viña Pomal")
    token, _ = Token.objects.get_or_create(user=user)
    return user, household, wine, big, kitchen, token


def _auth(client, token, household):
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    client.defaults["HTTP_X_HOUSEHOLD"] = str(household.id)
    return client


def test_add_returns_the_fridge_it_used(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine.id, "storage_name": "Big Fridge"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["storage"] == "Big Fridge"
    assert body["storage_id"] == big.id
    assert body["drawers"] == 8
    assert body["item_id"]


def test_remove_prefers_the_named_fridge(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    StorageItem.objects.create(storage=kitchen, wine=wine, user=user)
    StorageItem.objects.create(storage=big, wine=wine, user=user)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-remove"),
        data={"wine_id": wine.id, "storage_name": "Big Fridge"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["storage"] == "Big Fridge"
    assert body["storage_id"] == big.id
    assert StorageItem.objects.filter(storage=big, wine=wine, deleted=False).count() == 0
    assert StorageItem.objects.filter(storage=kitchen, wine=wine, deleted=False).count() == 1


def test_remove_named_fridge_does_not_steal_from_another(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    StorageItem.objects.create(storage=kitchen, wine=wine, user=user)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-remove"),
        data={"wine_id": wine.id, "storage_name": "Big Fridge"},
        content_type="application/json",
    )
    assert response.status_code == 409
    assert "Big Fridge" in response.json()["error"]
    assert StorageItem.objects.filter(storage=kitchen, wine=wine, deleted=False).count() == 1


def test_drank_does_not_take_fridge_stock(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    StorageItem.objects.create(storage=big, wine=wine, user=user)
    _auth(client, token, household)

    response = client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine.id, "storage_name": "Big Fridge", "drank": True},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["action"] == "drank"
    assert body["stock_count"] == 1
    assert "achievements" in body
    assert StorageItem.objects.filter(wine=wine, deleted=False).count() == 1
    drunk = StorageItem.objects.get(id=body["item_id"])
    assert drunk.deleted is True
    assert drunk.wine_id == wine.id


def test_vote_works_on_a_deleted_bottle(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    item = StorageItem.objects.create(
        storage=big, wine=wine, user=user, deleted=True,
    )
    _auth(client, token, household)

    response = client.patch(
        reverse("api-stock-vote", args=[item.id]),
        data={"vote": "up"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["vote"] == "up"
    item.refresh_from_db()
    assert item.vote == "up"

    meh = client.patch(
        reverse("api-stock-vote", args=[item.id]),
        data={"vote": "meh"},
        content_type="application/json",
    )
    assert meh.status_code == 200
    item.refresh_from_db()
    assert item.vote == "meh"


def test_stock_history_lists_drank_newest_first(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    older = WineFactory(user=user, household=household, name="Older Chenin", vintage=2018)
    newer = WineFactory(user=user, household=household, name="Newer Pinot", vintage=2020)
    older.comment = "Green apple and wet stone."
    older.save(update_fields=["comment"])
    StorageItem.objects.create(storage=big, wine=older, user=user, deleted=True)
    StorageItem.objects.create(storage=kitchen, wine=newer, user=user, deleted=True)
    StorageItem.objects.create(storage=big, wine=wine, user=user, deleted=False)
    _auth(client, token, household)

    response = client.get(reverse("api-stock-history"))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 2
    assert body["offset"] == 0
    names = [item["wine_name"] for item in body["items"]]
    assert names[0].startswith("Newer Pinot")
    assert names[1].startswith("Older Chenin")
    assert "Green apple" in body["items"][1]["comment"]
    assert body["items"][1]["has_image"] is False
    assert all("wine_id" in item and "drank_at" in item for item in body["items"])


def test_stock_history_paginates(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    for i in range(6):
        w = WineFactory(user=user, household=household, name=f"Bottle {i}", vintage=2010 + i)
        StorageItem.objects.create(storage=big, wine=w, user=user, deleted=True)
    _auth(client, token, household)

    first = client.get(reverse("api-stock-history"), {"limit": 5, "offset": 0})
    assert first.status_code == 200
    body = first.json()
    assert body["count"] == 6
    assert len(body["items"]) == 5
    assert body["limit"] == 5

    second = client.get(reverse("api-stock-history"), {"limit": 5, "offset": 5})
    assert second.status_code == 200
    page = second.json()
    assert len(page["items"]) == 1
    assert page["offset"] == 5


def test_duplicate_drink_is_household_wide(client, cellar):
    from wine_cellar.apps.scanner.models import HouseholdMembership

    user, household, wine, big, kitchen, token = cellar
    other = UserFactory(household=household)
    StorageItem.objects.create(storage=big, wine=wine, user=other, deleted=True)
    _auth(client, token, household)

    blocked = client.post(
        reverse("api-stock-add"),
        data={
            "wine_id": wine.id, "storage_name": "Big Fridge",
            "drank": True, "check_duplicate": True,
        },
        content_type="application/json",
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "recent_duplicate"

    ok = client.post(
        reverse("api-stock-add"),
        data={
            "wine_id": wine.id, "storage_name": "Big Fridge",
            "drank": True, "confirm_duplicate": True,
        },
        content_type="application/json",
    )
    assert ok.status_code == 201
    assert HouseholdMembership.objects.filter(user=other, household=household).exists()


def test_duplicate_add_is_household_wide(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    other = UserFactory(household=household)
    StorageItem.objects.create(storage=big, wine=wine, user=other, deleted=False)
    _auth(client, token, household)

    blocked = client.post(
        reverse("api-stock-add"),
        data={
            "wine_id": wine.id, "storage_name": "Big Fridge",
            "check_duplicate": True,
        },
        content_type="application/json",
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "recent_duplicate"


def test_six_adds_of_the_same_wine_do_not_409(client, cellar):
    user, household, wine, big, kitchen, token = cellar
    _auth(client, token, household)
    for _ in range(6):
        response = client.post(
            reverse("api-stock-add"),
            data={"wine_id": wine.id, "storage_name": "Big Fridge"},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
    assert StorageItem.objects.filter(
        wine=wine, storage=big, deleted=False,
    ).count() == 6


def test_stock_history_has_image_and_hides_other_households(client, cellar):
    from wine_cellar.apps.scanner.models import Household

    user, household, wine, big, kitchen, token = cellar
    wine.comment = "Notes"
    wine.save(update_fields=["comment"])
    StorageItem.objects.create(storage=big, wine=wine, user=user, deleted=True)
    WineImageFactory(wine=wine, user=user)

    other = Household.objects.create(name="Other Cellar")
    outsider_wine = WineFactory(user=user, household=other, name="Secret Bottle")
    other_storage = StorageFactory(user=user, household=other, name="Away")
    StorageItem.objects.create(
        storage=other_storage, wine=outsider_wine, user=user, deleted=True,
    )
    _auth(client, token, household)

    response = client.get(reverse("api-stock-history"))
    assert response.status_code == 200
    body = response.json()
    names = [item["wine_name"] for item in body["items"]]
    assert any("Viña Pomal" in n for n in names)
    assert not any("Secret" in n for n in names)
    ours = next(item for item in body["items"] if item["wine_id"] == wine.id)
    assert ours["has_image"] is True
