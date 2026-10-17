"""One-time Telegram household invites."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdInvite,
    HouseholdMembership,
    TelegramUser,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.user.models import UserSettings
from wine_cellar.apps.wine.models import Wine


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def member_user(db, django_user_model, household):
    user = django_user_model.objects.create_user(username="member", password="pw")
    HouseholdMembership.objects.create(user=user, household=household, role="member")
    TelegramUser.objects.create(user=user, chat_id="111", display_name="Alex")
    return user


@pytest.fixture
def outsider(db, django_user_model):
    user = django_user_model.objects.create_user(username="outsider", password="pw")
    other = Household.objects.create(name="Somewhere else")
    HouseholdMembership.objects.create(user=user, household=other, role="member")
    return user


@pytest.fixture
def staff_user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="staff", password="pw", is_staff=True,
    )


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


def test_member_can_mint_join(client, member_user, household):
    response = _mint(_auth(client, member_user), "join")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "join"
    assert len(body["code"]) == 8
    assert body["household"]["name"] == "Hillside"
    assert body["inviter_display_name"] == "Alex"
    invite = HouseholdInvite.objects.get(code=body["code"])
    assert invite.household_id == household.id
    assert invite.used_at is None
    assert invite.expires_at > timezone.now() + timedelta(days=6)


def test_outsider_cannot_mint_join(client, outsider, household):
    response = _mint(
        _auth(client, outsider), "join", household_id=household.id,
    )
    assert response.status_code == 403
    assert not HouseholdInvite.objects.filter(kind="join", household=household).exists()


def test_staff_mints_new_with_no_name(client, staff_user):
    response = _mint(_auth(client, staff_user), "new")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "new"
    assert body["household"] is None
    invite = HouseholdInvite.objects.get(code=body["code"])
    assert invite.household_id is None


def test_non_staff_cannot_mint_new(client, member_user):
    response = _mint(_auth(client, member_user), "new")
    assert response.status_code == 403
    assert not HouseholdInvite.objects.filter(kind="new").exists()


def test_staff_join_requires_household_id(client, staff_user, member_user):
    response = _mint(_auth(client, staff_user), "join")
    assert response.status_code == 400
    assert "household_id" in response.json()["error"]


def test_member_preview_is_forbidden(client, member_user):
    minted = _mint(_auth(client, member_user), "join")
    code = minted.json()["code"]
    preview = client.post(
        reverse("api-scanner-invites-preview"),
        data={"code": code},
        content_type="application/json",
    )
    assert preview.status_code == 403
    assert HouseholdInvite.objects.get(code=code).used_at is None


def test_preview_does_not_burn(client, member_user, staff_user):
    minted = _mint(_auth(client, member_user), "join")
    code = minted.json()["code"]
    preview = _auth(client, staff_user).post(
        reverse("api-scanner-invites-preview"),
        data={"code": code},
        content_type="application/json",
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["used"] is False
    assert body["kind"] == "join"
    assert body["household_name"] == "Hillside"
    assert HouseholdInvite.objects.get(code=code).used_at is None


def test_join_redeem_creates_user_member_and_token(client, member_user, staff_user):
    UserSettings.objects.update_or_create(
        user=member_user, defaults={"currency": "GBP", "country": "GB"},
    )
    code = _mint(_auth(client, member_user), "join").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["role"] == "member"
    assert body["household_name"] == "Hillside"
    assert body["token"]
    tu = TelegramUser.objects.get(chat_id="999")
    membership = HouseholdMembership.objects.get(
        user=tu.user, household__name="Hillside",
    )
    assert membership.role == "member"
    assert Token.objects.filter(user=tu.user, key=body["token"]).exists()
    assert HouseholdInvite.objects.get(code=code).used_at is not None
    assert UserSettings.objects.filter(user=tu.user).exists()
    settings = UserSettings.objects.get(user=tu.user)
    assert settings.currency == "GBP"
    assert settings.country == "GB"
    assert body["currency"] == "GBP"


def test_existing_telegram_user_gets_a_second_membership(
    client, member_user, staff_user, django_user_model, household,
):
    other = Household.objects.create(name="Yskas")
    existing = django_user_model.objects.create_user(username="sam", password="pw")
    TelegramUser.objects.create(user=existing, chat_id="999", display_name="Sam")
    HouseholdMembership.objects.create(user=existing, household=other, role="member")
    code = _mint(_auth(client, member_user), "join").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert HouseholdMembership.objects.filter(user=existing, household=household).exists()
    assert HouseholdMembership.objects.filter(user=existing, household=other).exists()


def test_invite_codes_are_masked():
    from wine_cellar.apps.scanner.views import mask_invite_code

    assert mask_invite_code("Ab3xY9k2") == "…k2"
    assert "Ab3x" not in mask_invite_code("Ab3xY9k2")


def test_lock_invite_sql_does_not_join():
    """The query that 500'd every redeem on Postgres: FOR UPDATE + LEFT JOIN."""
    from wine_cellar.apps.scanner.views import lock_invite_qs

    sql = str(lock_invite_qs("inSBXC11").query).upper()
    assert "JOIN" not in sql


@pytest.mark.django_db
def test_locking_a_new_cellar_invite_does_not_outer_join(staff_user):
    """A new-cellar invite has household=NULL. Postgres rejects FOR UPDATE on
    the nullable side of select_related("household")'s LEFT JOIN."""
    from wine_cellar.apps.scanner.views import lock_invite_qs

    invite = HouseholdInvite.objects.create(
        kind=HouseholdInvite.KIND_NEW, created_by=staff_user,
    )
    with transaction.atomic():
        locked = lock_invite_qs(invite.code).first()
    assert locked is not None
    assert locked.household_id is None


def test_new_redeem_creates_owner_cellar_storage_and_token(client, staff_user):
    UserSettings.objects.update_or_create(
        user=staff_user, defaults={"currency": "GBP", "country": "GB"},
    )
    code = _mint(_auth(client, staff_user), "new").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={
            "code": code,
            "chat_id": "999",
            "display_name": "Sam",
            "household_name": "Riverside",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["role"] == "owner"
    assert body["first_cellar"] is True
    assert body["token"]
    household = Household.objects.get(name="Riverside")
    tu = TelegramUser.objects.get(chat_id="999")
    membership = HouseholdMembership.objects.get(user=tu.user, household=household)
    assert membership.role == "owner"
    assert Storage.objects.filter(household=household, name="Cellar").exists()
    assert not Storage.objects.filter(household=household, name="Unsorted").exists()
    assert Token.objects.filter(user=tu.user).exists()
    invite = HouseholdInvite.objects.get(code=code)
    assert invite.used_by_id == tu.user.id
    assert invite.household_id == household.id
    assert UserSettings.objects.filter(user=tu.user).exists()
    settings = UserSettings.objects.get(user=tu.user)
    assert settings.currency == "EUR"
    assert settings.country == ""
    assert body["currency"] == "EUR"


def test_new_redeem_missing_name_is_400(client, staff_user):
    code = _mint(_auth(client, staff_user), "new").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert HouseholdInvite.objects.get(code=code).used_at is None
    assert not Household.objects.filter(name="Riverside").exists()


def test_duplicate_name_is_409_and_invite_stays_unused(client, staff_user):
    Household.objects.create(name="Riverside")
    code = _mint(_auth(client, staff_user), "new").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={
            "code": code,
            "chat_id": "999",
            "display_name": "Sam",
            "household_name": "riverside",
        },
        content_type="application/json",
    )
    assert response.status_code == 409
    assert HouseholdInvite.objects.get(code=code).used_at is None
    assert Household.objects.filter(name__iexact="riverside").count() == 1
    body = response.json()
    assert body["error"] == "That name's taken \u2014 pick another"
    assert "riverside" not in body["error"].lower()
    suggested = body["suggested_name"]
    assert suggested.casefold() != "riverside"
    assert not Household.objects.filter(name__iexact=suggested).exists()


def test_suggest_uses_first_name_when_free(db):
    from wine_cellar.apps.scanner.views import suggest_household_name
    Household.objects.create(name="Riverside")
    assert suggest_household_name("Riverside", "Sam") == "Riverside (Sam)"


def test_suggest_skips_a_taken_variant(db):
    from wine_cellar.apps.scanner.views import suggest_household_name
    Household.objects.create(name="Riverside")
    Household.objects.create(name="Riverside (Sam)")
    suggested = suggest_household_name("Riverside", "Sam")
    assert suggested.casefold() == "riverside 2"
    assert not Household.objects.filter(name__iexact=suggested).exists()


def test_member_cannot_preview(client, member_user):
    code = _mint(_auth(client, member_user), "join").json()["code"]
    response = _auth(client, member_user).post(
        reverse("api-scanner-invites-preview"),
        data={"code": code},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert HouseholdInvite.objects.get(code=code).used_at is None


def test_member_cannot_redeem(client, member_user):
    """Redeem hands back the token for any chat_id — never to a member."""
    code = _mint(_auth(client, member_user), "join").json()["code"]
    response = _auth(client, member_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert not TelegramUser.objects.filter(chat_id="999").exists()
    assert HouseholdInvite.objects.get(code=code).used_at is None


def test_used_invite_is_410(client, member_user, staff_user):
    code = _mint(_auth(client, member_user), "join").json()["code"]
    auth = _auth(client, staff_user)
    first = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert first.status_code == 200
    second = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "888", "display_name": "Other"},
        content_type="application/json",
    )
    assert second.status_code == 410
    assert not TelegramUser.objects.filter(chat_id="888").exists()


def test_used_invite_same_chat_replays_the_token(client, member_user, staff_user):
    """A lost 201 must not strand the person: same chat gets the token again."""
    code = _mint(_auth(client, member_user), "join").json()["code"]
    auth = _auth(client, staff_user)
    first = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert first.status_code == 200
    token = first.json()["token"]
    second = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert second.status_code == 200, second.content
    body = second.json()
    assert body["token"] == token
    assert body["already_member"] is True
    assert TelegramUser.objects.filter(chat_id="999").count() == 1


def test_used_new_invite_same_chat_replays_without_a_name(client, staff_user):
    code = _mint(_auth(client, staff_user), "new").json()["code"]
    auth = _auth(client, staff_user)
    first = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={
            "code": code,
            "chat_id": "999",
            "display_name": "Sam",
            "household_name": "Home",
        },
        content_type="application/json",
    )
    assert first.status_code == 201, first.content
    second = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert second.status_code == 200, second.content
    body = second.json()
    assert body["token"]
    assert body["household_name"] == "Home"
    assert body["already_member"] is True
    assert Household.objects.filter(name="Home").count() == 1


def test_used_new_invite_with_null_household_still_replays(client, staff_user):
    """New-cellar rows used to leave household NULL. Replay still finds the owner."""
    code = _mint(_auth(client, staff_user), "new").json()["code"]
    auth = _auth(client, staff_user)
    first = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={
            "code": code,
            "chat_id": "999",
            "display_name": "Sam",
            "household_name": "Home",
        },
        content_type="application/json",
    )
    assert first.status_code == 201, first.content
    HouseholdInvite.objects.filter(code=code).update(household=None)
    second = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert second.status_code == 200, second.content
    assert second.json()["household_name"] == "Home"


def test_expired_invite_is_410(client, member_user, staff_user):
    code = _mint(_auth(client, member_user), "join").json()["code"]
    invite = HouseholdInvite.objects.get(code=code)
    invite.expires_at = timezone.now() - timedelta(minutes=1)
    invite.save(update_fields=["expires_at"])
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 410
    assert HouseholdInvite.objects.get(code=code).used_at is None


def test_second_redeem_inside_a_transaction_fails(client, member_user, staff_user):
    """The row lock is what makes a double tap only provision one person."""
    code = _mint(_auth(client, member_user), "join").json()["code"]
    auth = _auth(client, staff_user)
    with transaction.atomic():
        invite = HouseholdInvite.objects.select_for_update().get(code=code)
        first = auth.post(
            reverse("api-scanner-invites-redeem"),
            data={"code": code, "chat_id": "999", "display_name": "Sam"},
            content_type="application/json",
        )
        assert first.status_code == 200
        invite.used_at = timezone.now()
        invite.save(update_fields=["used_at"])
    second = auth.post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "888", "display_name": "Other"},
        content_type="application/json",
    )
    assert second.status_code == 410


def test_cancel_drops_last_unused(client, member_user):
    auth = _auth(client, member_user)
    first = _mint(auth, "join").json()["code"]
    second = _mint(auth, "join").json()["code"]
    response = auth.post(reverse("api-scanner-invites-cancel"), data={}, content_type="application/json")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == second
    assert body["kind"] == "join"
    assert body["household_name"] == "Hillside"
    assert HouseholdInvite.objects.filter(code=first).exists()
    assert not HouseholdInvite.objects.filter(code=second).exists()


def _cancel(auth, **data):
    return auth.post(
        reverse("api-scanner-invites-cancel"), data=data, content_type="application/json",
    )


def test_cancel_skips_expired(client, member_user):
    auth = _auth(client, member_user)
    older = _mint(auth, "join").json()["code"]
    newer = _mint(auth, "join").json()["code"]
    HouseholdInvite.objects.filter(code=newer).update(
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    response = _cancel(auth)
    assert response.status_code == 200
    assert response.json()["code"] == older
    assert HouseholdInvite.objects.filter(code=newer).exists()


def test_cancel_scopes_to_household(client, member_user, household):
    other = Household.objects.create(name="Yskas")
    HouseholdMembership.objects.create(user=member_user, household=other, role="member")
    auth = _auth(client, member_user)
    mine = _mint(auth, "join", household_id=household.id).json()["code"]
    newer_elsewhere = _mint(auth, "join", household_id=other.id).json()["code"]
    response = _cancel(auth, household_id=household.id)
    assert response.status_code == 200
    assert response.json()["code"] == mine
    assert HouseholdInvite.objects.filter(code=newer_elsewhere).exists()


def test_cancel_falls_back_to_new_cellar_invite(client, staff_user, household):
    auth = _auth(client, staff_user)
    code = _mint(auth, "new").json()["code"]
    response = _cancel(auth, household_id=household.id)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == code
    assert body["kind"] == "new"
    assert body["household_name"] is None


def test_join_redeem_reports_bottle_count(client, member_user, staff_user, household):
    wine = Wine.objects.create(
        name="Test", user=member_user, household=household, wine_type="RE",
    )
    storage = Storage.objects.create(
        name="Cellar", user=member_user, household=household, location="Cellar",
    )
    StorageItem.objects.create(wine=wine, storage=storage, user=member_user)
    StorageItem.objects.create(wine=wine, storage=storage, user=member_user)
    code = _mint(_auth(client, member_user), "join").json()["code"]
    response = _auth(client, staff_user).post(
        reverse("api-scanner-invites-redeem"),
        data={"code": code, "chat_id": "999", "display_name": "Sam"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["bottle_count"] == 2
