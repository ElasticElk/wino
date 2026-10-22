"""Invite → first bottle, against Postgres.

The Telegram photo/card path is covered in scanner-service. This is the API
half: redeem, settings, create, drank, the 60-minute duplicate guard, and
the first-bottle achievement — the bits that were silent for Jaco.
"""

from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdInvite,
    HouseholdMembership,
    TelegramUser,
)
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.user.models import UserSettings
from wine_cellar.apps.wine.models import CellarAchievement, Wine


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _mint(client, kind, **extra):
    return client.post(
        reverse("api-scanner-invites"),
        data={"kind": kind, **extra},
        content_type="application/json",
    )


def _redeem(client, staff_user, code, chat_id, display_name, **extra):
    return _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": chat_id, "display_name": display_name, **extra},
        content_type="application/json",
    )


def _as_user(client, token, household_id=None):
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token}"
    if household_id is not None:
        client.defaults["HTTP_X_HOUSEHOLD"] = str(household_id)
    else:
        client.defaults.pop("HTTP_X_HOUSEHOLD", None)
    return client


def test_join_then_first_drank_awards_and_guards_a_double_tap(
    client, db, django_user_model,
):
    household = Household.objects.create(name="Curl Curl")
    member = django_user_model.objects.create_user(username="alex", password="pw")
    HouseholdMembership.objects.create(user=member, household=household, role="member")
    TelegramUser.objects.create(user=member, chat_id="111", display_name="Alex")
    UserSettings.objects.update_or_create(
        user=member, defaults={"currency": "GBP", "country": "GB"},
    )
    staff = django_user_model.objects.create_user(
        username="staff", password="pw", is_staff=True,
    )

    code = _mint(_auth(client, member), "join").json()["code"]
    redeemed = _redeem(client, staff, code, "999", "Jaco")
    assert redeemed.status_code == 200, redeemed.content
    body = redeemed.json()
    assert body["role"] == "member"
    token = body["token"]
    household_id = body["household_id"]
    tu = TelegramUser.objects.get(chat_id="999")
    settings = UserSettings.objects.get(user=tu.user)
    assert settings.currency == "GBP"
    assert settings.country == "GB"
    assert body["currency"] == "GBP"

    user_client = _as_user(client, token, household_id)
    created = user_client.post(
        reverse("api-wine-create"),
        data={
            "name": "Hungerford Hill Chardonnay",
            "producer": "Hungerford Hill",
            "vintage": 2021,
            "wine_type": "WH",
            "country": "AU",
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    wine_id = created.json()["id"]

    drank = user_client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine_id, "storage_name": "Fridge", "drank": True},
        content_type="application/json",
    )
    assert drank.status_code == 201, drank.content
    first = drank.json()
    assert first["action"] == "drank"
    assert any("bottle" in a["title"].lower() for a in first["achievements"])
    assert StorageItem.objects.filter(wine_id=wine_id).count() == 1
    assert CellarAchievement.objects.filter(
        household_id=household_id, slug="bottles", subject="1",
    ).exists()

    again = user_client.post(
        reverse("api-stock-add"),
        data={
            "wine_id": wine_id, "storage_name": "Fridge", "drank": True,
            "check_duplicate": True,
        },
        content_type="application/json",
    )
    assert again.status_code == 409
    assert again.json()["error"] == "recent_duplicate"
    assert again.json()["item_id"] == first["item_id"]
    assert StorageItem.objects.filter(wine_id=wine_id).count() == 1

    confirmed = user_client.post(
        reverse("api-stock-add"),
        data={
            "wine_id": wine_id,
            "storage_name": "Fridge",
            "drank": True,
            "confirm_duplicate": True,
        },
        content_type="application/json",
    )
    assert confirmed.status_code == 201, confirmed.content
    assert StorageItem.objects.filter(wine_id=wine_id).count() == 2


def test_new_cellar_redeem_has_settings_and_can_add_then_drink(
    client, db, django_user_model,
):
    staff = django_user_model.objects.create_user(
        username="staff", password="pw", is_staff=True,
    )
    code = _mint(_auth(client, staff), "new").json()["code"]
    redeemed = _redeem(
        client, staff, code, "888", "Alex", household_name="Alex Cellar",
    )
    assert redeemed.status_code == 201, redeemed.content
    body = redeemed.json()
    assert body["first_cellar"] is True
    tu = TelegramUser.objects.get(chat_id="888")
    assert UserSettings.objects.filter(user=tu.user).exists()
    assert body["currency"]

    user_client = _as_user(client, body["token"], body["household_id"])
    created = user_client.post(
        reverse("api-wine-create"),
        data={
            "name": "Old Vine Chenin", "producer": "Bosman",
            "vintage": 2020, "wine_type": "WH", "country": "ZA",
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    wine_id = created.json()["id"]

    added = user_client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine_id, "storage_name": "Cellar"},
        content_type="application/json",
    )
    assert added.status_code == 201, added.content
    assert added.json()["action"] == "added"
    assert any("bottle" in a["title"].lower() for a in added.json()["achievements"])
    assert Wine.objects.get(id=wine_id).total_stock == 1

    drank = user_client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine_id, "storage_name": "Cellar", "drank": True},
        content_type="application/json",
    )
    assert drank.status_code == 201, drank.content
    assert drank.json()["action"] == "drank"
    assert Wine.objects.get(id=wine_id).total_stock == 1
    assert StorageItem.objects.filter(wine_id=wine_id).count() == 2
    assert HouseholdInvite.objects.get(code=code).used_at is not None
