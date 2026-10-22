"""The in-stock payload the chat reads.

Existing keys stay as they were. The new ones are what a recommendation
needs and a shelf question cannot answer without: every grape, the note,
drink-by, and thumbs already given on bottles that have been drunk.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.tests.factories import (
    AttributeFactory,
    FoodPairingFactory,
    GrapeFactory,
    RegionFactory,
    WineFactory,
)


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"


def _cellar():
    user = UserFactory()
    household = get_user_household(user)
    storage = StorageFactory(user=user, household=household, name="Big Fridge")
    return user, household, storage


def _stock(client, user):
    _auth(client, user)
    response = client.get(reverse("api-wines-in-stock"))
    assert response.status_code == 200
    return response.json()["wines"]


@pytest.mark.django_db
def test_blend_lists_every_grape(client):
    user, household, storage = _cellar()
    syrah = GrapeFactory(name="Syrah", user=user)
    grenache = GrapeFactory(name="Grenache", user=user)
    wine = WineFactory(
        user=user, household=household, name="Blend", vintage=2019,
        wine_type="RE", country="FR", grapes=[syrah, grenache],
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user, row=2)

    entry = _stock(client, user)[0]
    assert entry["grapes"] == ["Syrah", "Grenache"]
    assert entry["grape"] == "Syrah"


@pytest.mark.django_db
def test_note_price_and_drink_by_are_present(client):
    user, household, storage = _cellar()
    region = RegionFactory(name="Stellenbosch", user=user)
    wine = WineFactory(
        user=user, household=household, name="Fennel", vintage=2018,
        wine_type="RE", country="ZA", region=region, grapes=[],
        comment="fennel and leather", price=Decimal("18.50"),
        drink_by=date(2027, 6, 1), rating=8, critic_score=92,
        vivino_rating=Decimal("4.10"),
    )
    wine.grapes.clear()
    wine.attributes.add(AttributeFactory(name="Leathery", user=user))
    wine.food_pairings.add(FoodPairingFactory(name="Lamb", user=user))
    StorageItem.objects.create(storage=storage, wine=wine, user=user)

    entry = _stock(client, user)[0]
    assert entry["comment"] == "fennel and leather"
    assert entry["price"] == 18.5
    assert entry["drink_by"] == "2027-06-01"
    assert entry["rating"] == 8
    assert entry["critic_score"] == 92
    assert entry["vivino_rating"] == 4.1
    assert entry["region"] == "Stellenbosch"
    assert entry["vintage"] == 2018
    assert entry["attributes"] == ["Leathery"]
    assert entry["food_pairings"] == ["Lamb"]
    assert entry["grapes"] == []
    assert entry["grape"] == ""


@pytest.mark.django_db
def test_votes_count_drunk_bottles_only(client):
    user, household, storage = _cellar()
    wine = WineFactory(
        user=user, household=household, name="Voted", wine_type="RE", country="ZA",
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user, vote="up")
    StorageItem.objects.create(
        storage=storage, wine=wine, user=user, deleted=True, vote="up",
    )
    StorageItem.objects.create(
        storage=storage, wine=wine, user=user, deleted=True, vote="down",
    )
    StorageItem.objects.create(
        storage=storage, wine=wine, user=user, deleted=True, vote=None,
    )

    entry = _stock(client, user)[0]
    assert entry["votes"] == {"up": 1, "meh": 0, "down": 1}
    assert entry["stock_count"] == 1


@pytest.mark.django_db
def test_item_carries_its_own_price(client):
    user, household, storage = _cellar()
    wine = WineFactory(
        user=user, household=household, name="Priced", wine_type="WH", country="ZA",
        price=Decimal("10.00"),
    )
    StorageItem.objects.create(
        storage=storage, wine=wine, user=user, row=3, price=Decimal("22.00"),
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user, row=4)

    entry = _stock(client, user)[0]
    prices = [item["price"] for item in entry["items"]]
    assert sorted(prices, key=lambda p: (p is None, p)) == [22.0, None]
    drawers = sorted(item["drawer"] for item in entry["items"])
    assert drawers == [3, 4]


@pytest.mark.django_db
def test_existing_keys_unchanged(client):
    user, household, storage = _cellar()
    grape = GrapeFactory(name="Chenin Blanc", user=user)
    wine = WineFactory(
        user=user, household=household, name="Ken Forrester", vintage=2020,
        wine_type="WH", country="ZA", barcode="600123", producer="Ken Forrester",
        grapes=[grape],
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user, row=1)
    StorageItem.objects.create(storage=storage, wine=wine, user=user, row=1)

    entry = _stock(client, user)[0]
    assert entry["name"] == "Ken Forrester 2020"
    assert entry["grape"] == "Chenin Blanc"
    assert entry["stock_count"] == 2
    assert entry["country"] == "ZA"
    assert entry["producer"] == "Ken Forrester"
    assert entry["barcode"] == "600123"
    assert entry["wine_type"] == "WH"
    assert {item["storage"] for item in entry["items"]} == {"Big Fridge"}
    assert {item["drawer"] for item in entry["items"]} == {1}


@pytest.mark.django_db
def test_missing_optionals_are_empty_not_absent(client):
    user, household, storage = _cellar()
    wine = Wine.objects.create(
        name="Bare", wine_type="RE", country="FR", user=user, household=household,
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user)

    entry = _stock(client, user)[0]
    assert entry["comment"] == ""
    assert entry["price"] is None
    assert entry["drink_by"] is None
    assert entry["rating"] is None
    assert entry["critic_score"] is None
    assert entry["vivino_rating"] is None
    assert entry["region"] == ""
    assert entry["grapes"] == []
    assert entry["attributes"] == []
    assert entry["food_pairings"] == []
    assert entry["votes"] == {"up": 0, "meh": 0, "down": 0}
    assert entry["items"][0]["price"] is None
    assert entry["items"][0]["drawer"] is None


@pytest.mark.django_db
def test_query_count_is_flat(client, django_assert_max_num_queries):
    user, household, storage = _cellar()
    other = StorageFactory(user=user, household=household, name="Small Fridge")
    for i in range(3):
        wine = WineFactory(
            user=user, household=household, name=f"Wine {i}", vintage=2020 + i,
            wine_type="RE", country="ZA",
        )
        wine.attributes.add(AttributeFactory(name=f"Attr {i}", user=user))
        wine.food_pairings.add(FoodPairingFactory(name=f"Food {i}", user=user))
        StorageItem.objects.create(storage=storage, wine=wine, user=user, row=i)
        StorageItem.objects.create(storage=other, wine=wine, user=user, row=i)
        StorageItem.objects.create(
            storage=storage, wine=wine, user=user, deleted=True, vote="up",
        )
    _auth(client, user)

    # Auth and household resolution sit in front of the stock queries.
    # 12 leaves room for those without letting a per-wine query sneak back.
    with django_assert_max_num_queries(12):
        response = client.get(reverse("api-wines-in-stock"))
    assert response.status_code == 200
    assert response.json()["count"] == 3
