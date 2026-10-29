"""To-drink lists are user-scoped and never stock."""

from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership, ServiceConfig
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import CellarAchievement, Wine, WineList, WineListItem


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable_lists():
    ServiceConfig.objects.update_or_create(
        key="feature_lists", defaults={"value": "on"},
    )


def _pair(django_user_model, household_name="Hillside"):
    household = Household.objects.create(name=household_name)
    riley = django_user_model.objects.create_user(username="riley", password="pw")
    sam = django_user_model.objects.create_user(username="sam", password="pw")
    HouseholdMembership.objects.create(user=riley, household=household, role="owner")
    HouseholdMembership.objects.create(user=sam, household=household, role="member")
    return household, riley, sam


def test_flag_off_returns_not_enabled(client, db, django_user_model):
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    response = auth.get(reverse("api-lists"))
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_default_list_created_on_get_for_this_user(client, db, django_user_model):
    _enable_lists()
    household, riley, sam = _pair(django_user_model)
    riley_client = _auth(client, riley)
    response = riley_client.get(reverse("api-lists"))
    assert response.status_code == 200, response.content
    lists = response.json()["lists"]
    assert len(lists) == 1
    assert lists[0]["name"] == "To drink"
    assert lists[0]["kind"] == "personal"
    assert lists[0]["item_count"] == 0
    assert WineList.objects.filter(owner=riley).count() == 1
    assert WineList.objects.filter(owner=sam).count() == 0

    sam_client = _auth(client, sam)
    other = sam_client.get(reverse("api-lists"))
    assert other.status_code == 200
    assert other.json()["lists"][0]["id"] != lists[0]["id"]
    assert WineList.objects.filter(owner=sam, kind="personal").count() == 1


def test_add_item_is_not_in_stock(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    added = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "producer": "Coastal Cellars",
            "vintage": 2022,
            "wine_type": "WH",
            "country": "ZA",
            "grape": "Chenin Blanc",
            "source": "label",
            "payload": {"name": "Chenin Blanc", "region": "Stellenbosch"},
        },
        content_type="application/json",
    )
    assert added.status_code == 201, added.content
    stock = auth.get(reverse("api-wines-in-stock"))
    assert stock.status_code == 200
    assert stock.json()["wines"] == []
    assert StorageItem.objects.count() == 0


def test_unique_open_list_wine_is_idempotent(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    wine = Wine.objects.create(
        name="Chenin Blanc", wine_type="WH", country="ZA",
        vintage=2022, user=riley, household=household,
    )
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    first = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Chenin Blanc", "wine_id": wine.id, "source": "label"},
        content_type="application/json",
    )
    assert first.status_code == 201, first.content
    second = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "wine_id": wine.id,
            "payload": {"region": "Stellenbosch"},
        },
        content_type="application/json",
    )
    assert second.status_code == 200, second.content
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["already"] is True
    assert WineListItem.objects.filter(list_id=list_id).count() == 1


def test_duplicate_free_text_without_wine_id_is_allowed(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    first = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Mystery red", "source": "typed"},
        content_type="application/json",
    )
    second = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Mystery red", "source": "typed"},
        content_type="application/json",
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_to_cellar_creates_stock_and_second_call_conflicts(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    Storage.objects.create(
        name="Cellar", household=household, user=riley, location="Cellar",
        rows=0, columns=0,
    )
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "vintage": 2022,
            "wine_type": "WH",
            "country": "ZA",
            "payload": {
                "name": "Chenin Blanc",
                "vintage": 2022,
                "wine_type": "WH",
                "country": "ZA",
            },
        },
        content_type="application/json",
    ).json()
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Cellar"},
        content_type="application/json",
    )
    assert moved.status_code == 201, moved.content
    body = moved.json()
    assert body["action"] == "added"
    assert body["list_item_id"] == item["id"]
    assert StorageItem.objects.filter(deleted=False).count() == 1
    row = WineListItem.objects.get(id=item["id"])
    assert row.moved_at is not None
    assert row.wine_id == body["wine_id"]

    again = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Cellar"},
        content_type="application/json",
    )
    assert again.status_code == 409
    assert again.json()["error"] == "already_moved"


def test_to_cellar_unique_wine_finds_existing(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    wine = Wine.objects.create(
        name="Chenin Blanc", wine_type="WH", country="ZA",
        vintage=2022, user=riley, household=household,
    )
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "vintage": 2022,
            "wine_type": "WH",
            "country": "ZA",
            "payload": {
                "name": "Chenin Blanc",
                "vintage": 2022,
                "wine_type": "WH",
                "country": "ZA",
            },
        },
        content_type="application/json",
    ).json()
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    )
    assert moved.status_code == 201, moved.content
    assert moved.json()["wine_id"] == wine.id
    assert Wine.objects.filter(household=household, name="Chenin Blanc").count() == 1


def test_to_cellar_awards_on_add(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    household.gamification_enabled = True
    household.save()
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "First bottle",
            "wine_type": "RE",
            "country": "FR",
            "payload": {"name": "First bottle", "wine_type": "RE", "country": "FR"},
        },
        content_type="application/json",
    ).json()
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    )
    assert moved.status_code == 201, moved.content
    assert moved.json()["achievements"]
    assert CellarAchievement.objects.filter(household=household).exists()


def test_partner_cannot_see_or_mutate_list(client, db, django_user_model):
    _enable_lists()
    household, riley, sam = _pair(django_user_model)
    riley_client = _auth(client, riley)
    list_id = riley_client.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = riley_client.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Shop find", "source": "label"},
        content_type="application/json",
    ).json()

    sam_client = _auth(client, sam)
    assert sam_client.get(
        reverse("api-list-detail", kwargs={"pk": list_id}),
    ).status_code == 404
    assert sam_client.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Nope"},
        content_type="application/json",
    ).status_code == 404
    assert sam_client.delete(
        reverse(
            "api-list-item-delete",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
    ).status_code == 404
    assert sam_client.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    ).status_code == 404


def test_payload_strips_image_keys_and_caps_size(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    added = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "payload": {
                "name": "Chenin Blanc",
                "photo_data": "not-bytes-but-forbidden",
                "image": "xxxx",
                "secret": "nope",
                "description": "x" * 20000,
            },
        },
        content_type="application/json",
    )
    assert added.status_code == 201, added.content
    row = WineListItem.objects.get(id=added.json()["id"])
    assert "photo_data" not in row.payload
    assert "image" not in row.payload
    assert "secret" not in row.payload
    blob = str(row.payload)
    assert len(blob.encode()) <= 16 * 1024


def test_blank_name_is_400(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    response = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "  "},
        content_type="application/json",
    )
    assert response.status_code == 400


def test_remove_already_moved_is_409(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Chenin Blanc",
            "payload": {"name": "Chenin Blanc", "wine_type": "WH", "country": "ZA"},
        },
        content_type="application/json",
    ).json()
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    )
    assert moved.status_code == 201, moved.content
    deleted = auth.delete(
        reverse(
            "api-list-item-delete",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
    )
    assert deleted.status_code == 409
    assert deleted.json()["error"] == "already_moved"


def test_wine_id_from_other_household_is_ignored_on_move(client, db, django_user_model):
    _enable_lists()
    household, riley, _sam = _pair(django_user_model)
    other = Household.objects.create(name="Riverside")
    outsider = django_user_model.objects.create_user(username="pat", password="pw")
    HouseholdMembership.objects.create(user=outsider, household=other, role="owner")
    foreign = Wine.objects.create(
        name="Foreign", wine_type="RE", country="FR",
        user=outsider, household=other,
    )
    auth = _auth(client, riley)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={
            "name": "Foreign",
            "wine_id": foreign.id,
            "payload": {"name": "Foreign", "wine_type": "RE", "country": "FR"},
        },
        content_type="application/json",
    ).json()
    assert item["wine_id"] is None
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    )
    assert moved.status_code == 201, moved.content
    created = Wine.objects.get(id=moved.json()["wine_id"])
    assert created.household_id == household.id
    assert created.id != foreign.id


def test_no_household_is_403_on_to_cellar(client, db, django_user_model):
    _enable_lists()
    loner = django_user_model.objects.create_user(username="lee", password="pw")
    auth = _auth(client, loner)
    list_id = auth.get(reverse("api-lists")).json()["lists"][0]["id"]
    item = auth.post(
        reverse("api-list-items", kwargs={"pk": list_id}),
        data={"name": "Solo", "payload": {"name": "Solo"}},
        content_type="application/json",
    ).json()
    moved = auth.post(
        reverse(
            "api-list-item-to-cellar",
            kwargs={"pk": list_id, "item_id": item["id"]},
        ),
        data={"storage_name": "Fridge"},
        content_type="application/json",
    )
    assert moved.status_code == 403
    assert "household" in moved.json()["error"].lower()
