"""Staff can create a household from Telegram via POST /api/scanner/households/."""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership, TelegramUser
from wine_cellar.apps.storage.models import Storage


@pytest.fixture
def staff_user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="staff", password="pw", is_staff=True,
    )


@pytest.fixture
def owner_user(db, django_user_model):
    user = django_user_model.objects.create_user(username="owner", password="pw")
    HouseholdMembership.objects.create(
        user=user,
        household=Household.objects.create(name="Already here"),
        role="owner",
    )
    return user


@pytest.fixture
def staff_auth(client, staff_user):
    token, _ = Token.objects.get_or_create(user=staff_user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


@pytest.fixture
def owner_auth(client, owner_user):
    token, _ = Token.objects.get_or_create(user=owner_user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _url():
    return reverse("api-households-list")


def test_staff_creates_household_and_owner(staff_auth, django_user_model):
    response = staff_auth.post(
        _url(),
        data={
            "name": "Riverside",
            "chat_id": "123456789",
            "display_name": "Alex",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["household"]["name"] == "Riverside"
    assert body["owner"]["created"] is True
    assert body["owner"]["chat_id"] == "123456789"

    household = Household.objects.get(name="Riverside")
    assert household.gamification_enabled is True
    tu = TelegramUser.objects.get(chat_id="123456789")
    membership = HouseholdMembership.objects.get(user=tu.user, household=household)
    assert membership.role == "owner"
    assert membership.is_default is True
    assert Token.objects.filter(user=tu.user).exists()
    assert Storage.objects.filter(household=household, name="Unsorted").exists()


def test_non_staff_cannot_create_a_household(owner_auth):
    response = owner_auth.post(
        _url(),
        data={
            "name": "Riverside",
            "chat_id": "123456789",
            "display_name": "Alex",
        },
        content_type="application/json",
    )
    assert response.status_code == 403
    assert not Household.objects.filter(name="Riverside").exists()


def test_duplicate_name_is_rejected(staff_auth):
    Household.objects.create(name="Riverside")
    response = staff_auth.post(
        _url(),
        data={
            "name": "riverside",
            "chat_id": "123456789",
            "display_name": "Alex",
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "That name's taken \u2014 pick another"
    assert body["suggested_name"] == "riverside (Alex)"


def test_existing_telegram_user_becomes_owner_of_the_new_cellar(
    staff_auth, django_user_model,
):
    existing = django_user_model.objects.create_user(username="alex", password="pw")
    TelegramUser.objects.create(
        user=existing, chat_id="123456789", display_name="Alex",
    )
    response = staff_auth.post(
        _url(),
        data={
            "name": "Riverside",
            "chat_id": "123456789",
            "display_name": "Alex",
        },
        content_type="application/json",
    )
    assert response.status_code == 201
    assert response.json()["owner"]["created"] is False
    assert Token.objects.filter(user=existing).exists()
    household = Household.objects.get(name="Riverside")
    membership = HouseholdMembership.objects.get(user=existing, household=household)
    assert membership.role == "owner"


@pytest.mark.parametrize(
    "payload",
    [
        {"chat_id": "1", "display_name": "P"},
        {"name": "Riverside", "display_name": "P"},
        {"name": "Riverside", "chat_id": "1"},
    ],
)
def test_missing_fields_are_rejected(staff_auth, payload):
    response = staff_auth.post(
        _url(), data=payload, content_type="application/json",
    )
    assert response.status_code == 400


def _add_member_url():
    return reverse("api-household-add-member")


def test_staff_add_member_requires_household_id(staff_auth, owner_user):
    """Staff X-Household is their own cellar — it must not be a silent default."""
    staff_auth.defaults["HTTP_X_HOUSEHOLD"] = str(
        owner_user.household_memberships.get().household_id
    )
    response = staff_auth.post(
        _add_member_url(),
        data={"chat_id": "999", "display_name": "Alex"},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "household_id" in response.json()["error"]


def test_staff_add_member_to_any_household(staff_auth, owner_user):
    household = owner_user.household_memberships.get().household
    response = staff_auth.post(
        _add_member_url(),
        data={
            "chat_id": "999",
            "display_name": "Alex",
            "household_id": household.id,
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    tu = TelegramUser.objects.get(chat_id="999")
    assert HouseholdMembership.objects.filter(
        user=tu.user, household=household, role="member",
    ).exists()


def test_owner_add_member_stays_in_their_household(owner_auth, owner_user):
    household = owner_user.household_memberships.get().household
    other = Household.objects.create(name="Somewhere else")
    response = owner_auth.post(
        _add_member_url(),
        data={"chat_id": "999", "display_name": "Alex"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    tu = TelegramUser.objects.get(chat_id="999")
    assert HouseholdMembership.objects.filter(
        user=tu.user, household=household,
    ).exists()
    assert not HouseholdMembership.objects.filter(
        user=tu.user, household=other,
    ).exists()


def test_staff_households_include_counts(staff_auth, owner_user):
    household = owner_user.household_memberships.get().household
    response = staff_auth.get(_url())
    assert response.status_code == 200
    row = next(h for h in response.json()["households"] if h["id"] == household.id)
    assert row["member_count"] == 1
    assert row["wine_count"] == 0
    assert row["bottle_count"] == 0


def test_staff_can_read_another_households_members(staff_auth, owner_user):
    household = owner_user.household_memberships.get().household
    TelegramUser.objects.create(
        user=owner_user, chat_id="111", display_name="Owner",
    )
    response = staff_auth.get(
        reverse("api-household"), {"household_id": household.id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == household.name
    assert body["role"] == "staff"
    assert body["members"][0]["telegram_chat_id"] == "111"


def test_staff_whois_by_chat_id(staff_auth, owner_user):
    TelegramUser.objects.create(
        user=owner_user, chat_id="111", display_name="Owner",
    )
    response = staff_auth.get(reverse("api-scanner-whois"), {"q": "111"})
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["display_name"] == "Owner"
    assert results[0]["households"][0]["name"] == "Already here"


def test_owner_cannot_whois_or_stats(owner_auth):
    assert owner_auth.get(reverse("api-scanner-whois"), {"q": "111"}).status_code == 403
    assert owner_auth.get(reverse("api-scanner-stats")).status_code == 403


def test_staff_stats(staff_auth, owner_user):
    response = staff_auth.get(reverse("api-scanner-stats"))
    assert response.status_code == 200
    body = response.json()
    assert body["households"] >= 1
    assert "telegram_users" in body
    assert "wines" in body
    assert "bottles" in body
    assert "scanners" in body
