"""Access control is by household, not by user.

Before this, the web layer filtered on `user` while the API and bot filtered on
`household` — so a member saw the whole cellar in Telegram and none of it on the
web, and wines created through the web form got no household at all and became
permanently invisible to the bot. These tests pin the intended behaviour:
everyone in a household sees all of its wine, and nobody sees another's.
"""

import pytest
from django.urls import reverse

from wine_cellar.apps.scanner.households import (
    get_user_household,
    household_member_ids,
    scope,
)
from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.storage.models import Storage
from wine_cellar.apps.wine.models import Wine


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def other_household(db):
    return Household.objects.create(name="Luttik")


@pytest.fixture
def owner(db, django_user_model, household):
    user = django_user_model.objects.create_user(username="owner", password="pw")
    HouseholdMembership.objects.create(user=user, household=household, role="owner")
    return user


@pytest.fixture
def member(db, django_user_model, household):
    """A second person in the SAME household who added nothing themselves."""
    user = django_user_model.objects.create_user(username="member", password="pw")
    HouseholdMembership.objects.create(user=user, household=household, role="member")
    return user


@pytest.fixture
def outsider(db, django_user_model, other_household):
    user = django_user_model.objects.create_user(username="outsider", password="pw")
    HouseholdMembership.objects.create(
        user=user, household=other_household, role="owner",
    )
    return user


@pytest.fixture
def owners_wine(db, owner, household):
    return Wine.objects.create(
        name="Old Vine Chenin", wine_type="WH", country="ZA",
        user=owner, household=household,
    )


def test_member_sees_a_wine_they_did_not_add(client, member, owners_wine):
    client.force_login(member)
    response = client.get(reverse("wine-detail", kwargs={"pk": owners_wine.pk}))
    assert response.status_code == 200


def test_outsider_cannot_see_another_households_wine(client, outsider, owners_wine):
    client.force_login(outsider)
    response = client.get(reverse("wine-detail", kwargs={"pk": owners_wine.pk}))
    assert response.status_code == 404


def test_wine_list_shows_the_whole_household(client, member, owner, household):
    for name in ("A", "B", "C"):
        Wine.objects.create(
            name=name, wine_type="RE", country="FR", user=owner, household=household,
        )
    client.force_login(member)
    response = client.get(reverse("wine-list"))
    assert response.context["paginator"].count == 3


def test_outsiders_list_is_empty(client, outsider, owners_wine):
    client.force_login(outsider)
    response = client.get(reverse("wine-list"))
    assert response.context["paginator"].count == 0


def test_storage_is_shared_across_the_household(client, member, owner, household):
    Storage.objects.create(
        name="Yskas", location="Kitchen", rows=0, columns=0,
        user=owner, household=household,
    )
    client.force_login(member)
    response = client.get(reverse("storage-list"))
    assert list(response.context["storages"]) != []


def test_a_user_with_no_household_sees_nothing(client, db, django_user_model, owners_wine):
    """`filter(household=None)` would match orphaned rows — none() must win."""
    loner = django_user_model.objects.create_user(username="loner", password="pw")
    assert get_user_household(loner) is None

    client.force_login(loner)
    assert client.get(reverse("wine-list")).context["paginator"].count == 0
    assert client.get(
        reverse("wine-detail", kwargs={"pk": owners_wine.pk}),
    ).status_code == 404


def test_scope_returns_nothing_rather_than_orphans(db, owner, household):
    """A NULL-household wine must not leak to a user with no household."""
    orphan = Wine.objects.create(
        name="Orphan", wine_type="RE", country="FR", user=owner, household=None,
    )
    assert orphan not in scope(Wine.objects.all(), None)
    assert scope(Wine.objects.all(), None).count() == 0


def test_household_member_ids_covers_housemates(owner, member, household):
    ids = household_member_ids(owner)
    assert set(ids) == {owner.pk, member.pk}


def test_household_member_ids_falls_back_to_self(db, django_user_model):
    loner = django_user_model.objects.create_user(username="loner2", password="pw")
    assert household_member_ids(loner) == [loner.pk]


def test_requested_household_is_ignored_unless_you_belong(owner, other_household, household):
    """Passing someone else's household id must not grant access to it."""
    assert get_user_household(owner, household_id=other_household.pk) == household
