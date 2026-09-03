"""Producer is its own column, not a prefix inside `comment`.

It used to be packed in as "Producer: X | tasting notes" and parsed back by
prefix match in four places. WineUpdateView rebuilt that string from producer +
description, so a PATCH carrying a producer and no description replaced the
tasting notes with "Producer: X" — which is exactly what the bot's "Update from
label" flow sends when Grok reads a producer off the label but writes no notes.
"""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.wine.models import Wine

NOTES = "Bright citrus and wet stone, with a long saline finish."


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def user(db, django_user_model, household):
    u = django_user_model.objects.create_user(username="prod", password="pw")
    HouseholdMembership.objects.filter(user=u).delete()
    HouseholdMembership.objects.create(user=u, household=household, role="owner")
    return u


@pytest.fixture
def auth(client, user):
    """Token auth, not session.

    ScannerAPIView declares TokenAuthentication only — force_login() gets you a
    403 here, which is why these endpoints are reached with a token.
    """
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


@pytest.fixture
def wine(db, user, household):
    return Wine.objects.create(
        name="Old Vine Chenin", wine_type="WH", country="ZA",
        user=user, household=household, producer="Ken Forrester", comment=NOTES,
    )


def test_updating_producer_alone_keeps_the_tasting_notes(auth, wine):
    """The regression this whole change exists for."""
    response = auth.patch(
        reverse("api-wine-update", kwargs={"pk": wine.pk}),
        data={"name": wine.name, "producer": "Ken Forrester Wines"},
        content_type="application/json",
    )
    assert response.status_code == 200

    wine.refresh_from_db()
    assert wine.producer == "Ken Forrester Wines"
    assert wine.comment == NOTES  # previously became "Producer: Ken Forrester Wines"


def test_updating_notes_alone_keeps_the_producer(auth, wine):
    auth.patch(
        reverse("api-wine-update", kwargs={"pk": wine.pk}),
        data={"name": wine.name, "description": "Riper than last year."},
        content_type="application/json",
    )
    wine.refresh_from_db()
    assert wine.producer == "Ken Forrester"
    assert wine.comment == "Riper than last year."


def test_create_stores_producer_and_notes_separately(auth, user):
    response = auth.post(
        reverse("api-wine-create"),
        data={
            "name": "Blanc de Noirs", "wine_type": "SP", "country": "ZA",
            "producer": "Graham Beck", "description": NOTES,
        },
        content_type="application/json",
    )
    assert response.status_code == 201

    wine = Wine.objects.get(pk=response.json()["id"])
    assert wine.producer == "Graham Beck"
    assert wine.comment == NOTES
    assert "Producer:" not in wine.comment


def test_producer_is_exposed_on_the_api(auth, wine):
    response = auth.get(reverse("api-wine-list-all"))
    entry = next(w for w in response.json()["wines"] if w["id"] == wine.pk)
    assert entry["producer"] == "Ken Forrester"
    assert entry["comment"] == NOTES


def test_in_stock_reports_producer_without_parsing(auth, wine, user, household):
    from wine_cellar.apps.storage.models import Storage, StorageItem

    storage = Storage.objects.create(
        name="Fridge", location="Kitchen", rows=0, columns=0,
        user=user, household=household,
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user)

    response = auth.get(reverse("api-wines-in-stock"))
    entry = next(w for w in response.json()["wines"] if w["id"] == wine.pk)
    assert entry["producer"] == "Ken Forrester"


def test_a_wine_with_no_producer_is_fine(auth, user, household):
    wine = Wine.objects.create(
        name="Mystery Red", wine_type="RE", country="FR",
        user=user, household=household,
    )
    response = auth.get(reverse("api-wine-list-all"))
    entry = next(w for w in response.json()["wines"] if w["id"] == wine.pk)
    assert entry["producer"] == ""
