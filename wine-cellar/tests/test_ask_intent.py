"""A recommendation is remembered as a TasteIntent of kind 'ask'.

It is not taste evidence: recording one must not rebuild the cellar profile.
Later taps (accepted, rejected, a place) merge into that same payload.
"""

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token
from unittest.mock import patch

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.scanner.models import Household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.models import TasteIntent
from wine_cellar.apps.wine.taste import rebuild_snapshot, record_intent
from wine_cellar.apps.wine.tests.factories import WineFactory


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    storage = StorageFactory(user=user, household=household)
    wine = WineFactory(
        user=user, household=household, country="ZA", wine_type="RE", vintage=2021,
    )
    StorageItem.objects.create(storage=storage, wine=wine, user=user)
    token, _ = Token.objects.get_or_create(user=user)
    return user, household, wine, token


def _client(client, token):
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def test_ask_is_recorded_and_returns_intent_id(client, cellar):
    user, household, wine, token = cellar
    _client(client, token)
    response = client.post(
        reverse("api-household-taste"),
        data={"kind": "ask", "payload": {"sentence": "something leathery"}},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["intent_id"]
    intent = TasteIntent.objects.get(pk=body["intent_id"])
    assert intent.kind == "ask"
    assert intent.household_id == household.id
    assert intent.payload["sentence"] == "something leathery"
    assert "counts" in body


def test_ask_without_an_object_payload_is_rejected(client, cellar):
    _user, _household, _wine, token = cellar
    _client(client, token)
    response = client.post(
        reverse("api-household-taste"),
        data={"kind": "ask", "payload": "leathery"},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert TasteIntent.objects.filter(kind="ask").count() == 0


def test_look_still_returns_snapshot_and_now_intent_id(client, cellar):
    _user, household, _wine, token = cellar
    _client(client, token)
    response = client.post(
        reverse("api-household-taste"),
        data={"kind": "look", "wine": {"name": "Sancerre", "country": "FR"}},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["intent_id"]
    assert body["counts"]["looks"] == 1
    assert TasteIntent.objects.get(pk=body["intent_id"]).household_id == household.id


def test_ask_does_not_rebuild_or_change_taste_counts(client, cellar):
    user, household, _wine, token = cellar
    before = rebuild_snapshot(household).counts
    _client(client, token)
    with patch("wine_cellar.apps.wine.taste.rebuild_snapshot") as rebuild:
        response = client.post(
            reverse("api-household-taste"),
            data={"kind": "ask", "payload": {"sentence": "what should I drink"}},
            content_type="application/json",
        )
    assert response.status_code == 201
    rebuild.assert_not_called()
    household.refresh_from_db()
    assert household.taste_snapshot.counts == before


def test_unknown_kind_names_ask(client, cellar):
    _user, _household, _wine, token = cellar
    _client(client, token)
    response = client.post(
        reverse("api-household-taste"),
        data={"kind": "quiz", "payload": {}},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "ask" in response.json()["error"]


def test_patch_merges_outcome_and_place(client, cellar):
    user, household, _wine, token = cellar
    intent = record_intent(household, user, "ask", {
        "sentence": "fennel", "offer": None,
    })
    _client(client, token)
    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={
            "outcome": "accepted",
            "shown": 7,
            "place": {"lat": 51.5, "lon": -0.1, "fix": "gps", "purpose": "weather"},
        },
        content_type="application/json",
    )
    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["sentence"] == "fennel"
    assert payload["offer"] is None
    assert payload["outcome"] == "accepted"
    assert payload["shown"] == 7
    assert payload["place"]["fix"] == "gps"
    intent.refresh_from_db()
    assert intent.payload["outcome"] == "accepted"


def test_patch_other_household_is_404(client, cellar):
    _user, _household, _wine, token = cellar
    other = Household.objects.create(name="Someone else")
    intent = TasteIntent.objects.create(
        household=other, kind="ask", payload={"sentence": "secret"},
    )
    _client(client, token)
    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={"outcome": "accepted"},
        content_type="application/json",
    )
    assert response.status_code == 404
    intent.refresh_from_db()
    assert "outcome" not in intent.payload


def test_patch_rejects_unknown_key(client, cellar):
    user, household, _wine, token = cellar
    intent = record_intent(household, user, "ask", {"sentence": "fennel"})
    _client(client, token)
    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={"sentence": "rewritten"},
        content_type="application/json",
    )
    assert response.status_code == 400
    intent.refresh_from_db()
    assert intent.payload == {"sentence": "fennel"}


def test_patch_rejects_bad_outcome(client, cellar):
    user, household, _wine, token = cellar
    intent = record_intent(household, user, "ask", {"sentence": "fennel"})
    _client(client, token)
    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={"outcome": "loved"},
        content_type="application/json",
    )
    assert response.status_code == 400
    intent.refresh_from_db()
    assert "outcome" not in intent.payload


def test_patch_on_a_look_intent_is_404(client, cellar):
    user, household, _wine, token = cellar
    intent = record_intent(household, user, "look", {"name": "Sancerre"})
    _client(client, token)
    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={"outcome": "accepted"},
        content_type="application/json",
    )
    assert response.status_code == 404
    intent.refresh_from_db()
    assert intent.payload == {"name": "Sancerre"}
