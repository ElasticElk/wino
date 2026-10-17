"""Taste is inferred from the cellar and from looking, not from ratings."""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.models import UserSettings
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.models import TasteIntent
from wine_cellar.apps.wine.taste import (
    format_taste_card,
    rebuild_snapshot,
    record_intent,
    serialize_snapshot,
)
from wine_cellar.apps.wine.tests.factories import WineFactory


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    storage = StorageFactory(user=user, household=household)
    wine = WineFactory(
        user=user, household=household, country="ZA", producer="Kanonkop",
        wine_type="RE", vintage=2021, price=180,
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    return user, household, wine


def test_snapshot_counts_whats_in_stock(cellar):
    user, household, wine = cellar
    snap = rebuild_snapshot(household)
    assert snap.counts["in_stock_wines"] == 1
    types = dict(snap.counts["types"])
    assert types.get("RE") == 1
    countries = dict(snap.counts["countries"])
    assert countries.get("ZA") == 1
    assert "Styles:" in snap.summary
    assert "South Africa" in snap.summary


def test_drunk_bottle_still_shapes_taste(cellar):
    user, household, wine = cellar
    item = StorageItem.objects.get(wine=wine)
    item.deleted = True
    item.save()
    snap = rebuild_snapshot(household)
    assert snap.counts["in_stock_wines"] == 0
    assert snap.counts["bought_bottles"] == 1
    assert snap.counts["drunk_bottles"] == 1
    assert dict(snap.counts["countries"]).get("ZA") == 1
    assert "South Africa" in snap.summary


def test_market_price_fills_taste_when_nothing_was_paid(cellar):
    user, household, wine = cellar
    wine.price = None
    wine.market_price = 30
    wine.market_price_currency = "EUR"
    wine.save()
    item = StorageItem.objects.get(wine=wine)
    item.price = None
    item.save()
    snap = rebuild_snapshot(household)
    bands = dict(snap.counts["price_bands"])
    assert bands.get("€20-45") == 1
    assert "€20-45" in snap.summary


def test_paid_price_beats_market_estimate(cellar):
    user, household, wine = cellar
    wine.price = None
    wine.market_price = 400
    wine.market_price_currency = "EUR"
    wine.save()
    item = StorageItem.objects.get(wine=wine)
    item.price = 10
    item.save()
    snap = rebuild_snapshot(household)
    assert dict(snap.counts["price_bands"]) == {"under €20": 1}


def test_price_bands_convert_to_household_currency(cellar):
    """A R420 bottle in a GBP cellar is about £18 — not a 300+ bottle."""
    user, household, wine = cellar
    UserSettings.objects.update_or_create(user=user, defaults={"currency": "GBP"})
    wine.price = None
    wine.market_price = 420
    wine.market_price_currency = "ZAR"
    wine.save()
    StorageItem.objects.filter(wine=wine).update(price=None)
    snap = rebuild_snapshot(household)
    assert dict(snap.counts["price_bands"]) == {"£15-40": 1}


def test_look_intent_is_remembered(cellar):
    user, household, wine = cellar
    record_intent(household, user, "look", {
        "name": "Cloudy Bay Sauvignon", "producer": "Cloudy Bay", "country": "NZ",
    })
    assert TasteIntent.objects.filter(household=household, kind="look").count() == 1
    data = serialize_snapshot(household)
    assert data["counts"]["looks"] == 1
    assert any("Cloudy Bay" in x["name"] for x in data["recent_looks"])
    assert "Looked at, not added" in data["card"]


def test_taste_api_get_and_post(client, cellar):
    user, household, wine = cellar
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    listed = client.get(reverse("api-household-taste"))
    assert listed.status_code == 200
    assert listed.json()["counts"]["in_stock_wines"] == 1

    posted = client.post(
        reverse("api-household-taste"),
        data={"kind": "look", "wine": {"name": "Sancerre", "country": "FR"}},
        content_type="application/json",
    )
    assert posted.status_code == 201
    assert posted.json()["counts"]["looks"] == 1


def test_card_escapes_text_for_telegram_html():
    """Telegram rejected the whole card on the "<150" price band."""
    card = format_taste_card({
        "counts": {"in_stock_wines": 1},
        "summary": "Price bands: <150 (3), 150-300 (1)",
        "recent_looks": [{"name": "Smith & Sons <Reserve>"}],
    })
    assert "&lt;150" in card
    assert "Smith &amp; Sons &lt;Reserve&gt;" in card
    assert "<b>What this cellar drinks</b>" in card
