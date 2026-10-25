"""A second identical wine must 409 with the existing row's id, not 500."""

from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.wine.models import Wine


PAYLOAD = {
    "name": "Old Vine Chenin",
    "producer": "Bosman",
    "vintage": 2020,
    "wine_type": "WH",
    "country": "ZA",
    "abv": 13.5,
}


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def test_duplicate_create_returns_409_with_existing_id(client, db, django_user_model):
    household = Household.objects.create(name="Hillside")
    user = django_user_model.objects.create_user(username="dup", password="pw")
    HouseholdMembership.objects.filter(user=user).delete()
    HouseholdMembership.objects.create(user=user, household=household, role="owner")
    auth = _auth(client, user)

    first = auth.post(
        reverse("api-wine-create"),
        data=PAYLOAD,
        content_type="application/json",
    )
    assert first.status_code == 201, first.content
    wine_id = first.json()["id"]

    second = auth.post(
        reverse("api-wine-create"),
        data=PAYLOAD,
        content_type="application/json",
    )
    assert second.status_code == 409, second.content
    body = second.json()
    assert body["error"] == "duplicate_wine"
    assert body["id"] == wine_id
    assert Wine.objects.filter(household=household, name="Old Vine Chenin").count() == 1


def test_same_name_different_vintage_is_not_a_duplicate(client, db, django_user_model):
    household = Household.objects.create(name="Hillside")
    user = django_user_model.objects.create_user(username="ok", password="pw")
    HouseholdMembership.objects.filter(user=user).delete()
    HouseholdMembership.objects.create(user=user, household=household, role="owner")
    auth = _auth(client, user)

    first = auth.post(
        reverse("api-wine-create"),
        data=PAYLOAD,
        content_type="application/json",
    )
    assert first.status_code == 201, first.content

    other = dict(PAYLOAD, vintage=2021)
    second = auth.post(
        reverse("api-wine-create"),
        data=other,
        content_type="application/json",
    )
    assert second.status_code == 201, second.content
    assert second.json()["id"] != first.json()["id"]
