"""Cellar challenges start on; owners can still turn them off."""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.achievements import award_for_add, backfill
from wine_cellar.apps.wine.models import CellarAchievement, Grape
from wine_cellar.apps.wine.tests.factories import WineFactory


@pytest.fixture
def playing(db):
    user = UserFactory()
    household = get_user_household(user)
    household.gamification_enabled = True
    household.save(update_fields=["gamification_enabled"])
    storage = StorageFactory(user=user, household=household)
    return user, household, storage


def _add_bottle(user, household, storage, **wine_kw):
    wine = WineFactory(user=user, household=household, **wine_kw)
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    return wine, award_for_add(wine, user)


def test_flag_off_awards_nothing(db):
    user = UserFactory()
    household = get_user_household(user)
    household.gamification_enabled = False
    household.save(update_fields=["gamification_enabled"])
    storage = StorageFactory(user=user, household=household)
    wine = WineFactory(user=user, household=household, country="HR")
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    assert award_for_add(wine, user) == []
    assert CellarAchievement.objects.count() == 0


def test_first_croatian_wine(playing):
    user, household, storage = playing
    wine, unlocked = _add_bottle(
        user, household, storage, country="HR", producer="Korlat", wine_type="RE",
    )
    titles = [u["title"] for u in unlocked]
    assert any("Croatia" in t for t in titles)
    assert any("bottle" in t.lower() for t in titles)
    # Same wine, second bottle: country first already earned.
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    again = award_for_add(wine, user)
    assert not any("Croatia" in u["title"] and "First" in u["title"] for u in again)


def test_fifth_sauvignon(playing):
    user, household, storage = playing
    grape = Grape.objects.create(name="Sauvignon Blanc", user=user)
    for i in range(5):
        wine = WineFactory(
            user=user, household=household, country="NZ",
            producer=f"Maker {i}", grapes=[grape],
        )
        StorageItem.objects.create(storage=storage, wine=wine, user=user)
        unlocked = award_for_add(wine, user)
    titles = [u["title"] for u in unlocked]
    assert any("5× Sauvignon Blanc" in t or "5" in t and "Sauvignon" in t for t in titles)


def test_backfill_on_existing_cellar(playing):
    user, household, storage = playing
    household.gamification_enabled = False
    household.save(update_fields=["gamification_enabled"])
    wine = WineFactory(user=user, household=household, country="FR", producer="Leflaive")
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    assert award_for_add(wine, user) == []

    household.gamification_enabled = True
    household.save(update_fields=["gamification_enabled"])
    filled = backfill(household, user)
    assert filled >= 1
    assert CellarAchievement.objects.filter(household=household, slug="country").exists()


def test_stock_add_drank_awards_first_bottle(client, playing):
    user, household, storage = playing
    wine = WineFactory(
        user=user, household=household, country="AU", producer="Hungerford Hill",
        wine_type="WH",
    )
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    response = client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine.id, "storage_name": storage.name, "drank": True},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["action"] == "drank"
    assert any("bottle" in a["title"].lower() for a in body["achievements"])
    assert not any("First bottle in" in a["title"] for a in body["achievements"])
    item = StorageItem.objects.get(id=body["item_id"])
    assert item.deleted is True


def test_skipped_first_bottle_is_awarded_on_the_next_add(playing):
    user, household, storage = playing
    wine = WineFactory(user=user, household=household, country="AU")
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    unlocked = award_for_add(wine, user)
    titles = [u["title"] for u in unlocked]
    assert any("First bottle" in t for t in titles)


def test_stock_add_api_includes_achievements(client, playing):
    user, household, storage = playing
    wine = WineFactory(user=user, household=household, country="IT", producer="Gaja")
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    response = client.post(
        reverse("api-stock-add"),
        data={"wine_id": wine.id, "storage_name": storage.name},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert "achievements" in body
    assert any("Italy" in a["title"] for a in body["achievements"])


def test_patch_toggles_flag(client, playing):
    user, household, storage = playing
    household.gamification_enabled = False
    household.save(update_fields=["gamification_enabled"])
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    response = client.patch(
        reverse("api-household"),
        data={"gamification_enabled": True},
        content_type="application/json",
    )
    assert response.status_code == 200
    household.refresh_from_db()
    assert household.gamification_enabled is True
    listed = client.get(reverse("api-household-achievements"))
    assert listed.status_code == 200
    assert listed.json()["enabled"] is True
    assert "progress" in listed.json()


def test_new_household_starts_with_challenges_on(client, db):
    user = UserFactory()
    household = get_user_household(user)
    assert household.gamification_enabled is True
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    listed = client.get(reverse("api-household-achievements"))
    assert listed.json()["enabled"] is True


def test_achievements_hidden_when_off(client, db):
    user = UserFactory()
    household = get_user_household(user)
    household.gamification_enabled = False
    household.save(update_fields=["gamification_enabled"])
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    listed = client.get(reverse("api-household-achievements"))
    assert listed.json() == {
        "enabled": False,
        "achievements": [],
        "this_month": {"standings": []},
        "progress": {"tracks": [], "missing": [], "close": []},
    }


def test_progress_next_milestones_and_missing_types(playing):
    from wine_cellar.apps.wine.achievements import progress

    user, household, storage = playing
    wine, _ = _add_bottle(
        user, household, storage, country="HR", producer="Korlat", wine_type="RE",
    )
    data = progress(household)
    titles = {row["title"] for row in data["tracks"]}
    assert "Bottles logged" in titles
    bottles = next(row for row in data["tracks"] if row["title"] == "Bottles logged")
    assert bottles["have"] == 1
    assert bottles["next"] == 5
    missing_titles = {row["title"] for row in data["missing"]}
    assert "First white" in missing_titles
    assert "First red" not in missing_titles
    close_titles = {row["title"] for row in data["close"]}
    assert any("Croatia" in t for t in close_titles)


def test_five_wines_from_the_2010s(playing):
    user, household, storage = playing
    unlocked = []
    for i in range(5):
        _, unlocked = _add_bottle(
            user, household, storage,
            country="FR", producer=f"Maison {i}", wine_type="WH",
            vintage=2012 + i,
        )
    titles = [u["title"] for u in unlocked]
    assert any("5 wines from the 2010s" in t for t in titles)


def test_first_bottle_in_storage_and_full_fridge(playing):
    user, household, _ = playing
    fridge = StorageFactory(
        user=user, household=household, name="Big Fridge", rows=1, columns=1,
    )
    wine, unlocked = _add_bottle(
        user, household, fridge, country="ZA", producer="Kanonkop", wine_type="RE",
    )
    titles = [u["title"] for u in unlocked]
    assert any("First bottle in Big Fridge" in t for t in titles)
    assert any("Big Fridge is full" in t for t in titles)


def test_month_tally_is_a_ranked_race(playing):
    from wine_cellar.apps.wine.achievements import month_tally

    user, household, storage = playing
    _add_bottle(user, household, storage, country="IT", producer="Gaja", wine_type="RE")
    race = month_tally(household)
    assert race["label"]
    assert race["standings"]
    assert race["standings"][0]["leading"] is True
    assert race["standings"][0]["bottles"] == 1
    assert race["standings"][0]["rank"] == 1
