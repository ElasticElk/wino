"""Staff view of how chat recommendations land.

The useful signal is whether the suggested bottle was opened soon after and
how it was voted, not just which button was tapped.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.storage.tests.factories import StorageFactory
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.chat_stats import chat_stats
from wine_cellar.apps.wine.models import TasteIntent
from wine_cellar.apps.wine.tests.factories import WineFactory


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    storage = StorageFactory(user=user, household=household)
    picked = WineFactory(user=user, household=household, name="Picked", country="ZA")
    other = WineFactory(user=user, household=household, name="Other", country="ZA")
    return user, household, storage, picked, other


def _ask(household, user, payload, created=None):
    intent = TasteIntent.objects.create(
        household=household, user=user, kind=TasteIntent.ASK, payload=payload,
    )
    if created is not None:
        TasteIntent.objects.filter(pk=intent.pk).update(created=created)
        intent.refresh_from_db()
    return intent


def _opened(storage, wine, user, when, vote=None):
    item = StorageItem.objects.create(
        storage=storage, wine=wine, user=user, deleted=True, vote=vote,
    )
    StorageItem.objects.filter(pk=item.pk).update(removed_at=when)
    return item


def test_pick_opened_within_two_days_counts_with_its_vote(cellar):
    user, household, storage, picked, _other = cellar
    intent = _ask(household, user, {
        "sentence": "deep red", "shown": picked.id, "outcome": "accepted",
        "model": "grok-4.20-0309-non-reasoning", "latency_ms": 1400,
    })
    _opened(storage, picked, user, intent.created + timedelta(hours=3), vote="up")

    stats = chat_stats(30)

    assert stats["asks"] == 1
    assert stats["shown"] == 1
    assert stats["outcomes"]["accepted"] == 1
    assert stats["opened_pick"] == 1
    assert stats["opened_pick_pct"] == 100
    assert stats["pick_votes"]["up"] == 1
    assert stats["models"] == {"grok-4.20-0309-non-reasoning": 1}
    assert stats["latency_ms"]["p50"] == 1400
    assert stats["worst"] == []


def test_pick_opened_after_the_window_does_not_count(cellar):
    user, household, storage, picked, _other = cellar
    intent = _ask(household, user, {"shown": picked.id})
    _opened(storage, picked, user, intent.created + timedelta(hours=72))

    stats = chat_stats(30)

    assert stats["opened_pick"] == 0
    assert stats["opened_other"] == 0


def test_bottle_opened_before_the_ask_does_not_count(cellar):
    user, household, storage, picked, _other = cellar
    intent = _ask(household, user, {"shown": picked.id})
    _opened(storage, picked, user, intent.created - timedelta(hours=1))

    assert chat_stats(30)["opened_pick"] == 0


def test_opening_a_different_bottle_is_flagged(cellar):
    user, household, storage, picked, other = cellar
    intent = _ask(household, user, {"sentence": "a white for fish", "shown": picked.id})
    _opened(storage, other, user, intent.created + timedelta(hours=1))

    stats = chat_stats(30)

    assert stats["opened_pick"] == 0
    assert stats["opened_other"] == 1
    assert stats["worst"][0]["why"] == ["opened a different bottle"]
    assert stats["worst"][0]["sentence"] == "a white for fish"


def test_thumbs_down_on_the_pick_ranks_worst(cellar):
    user, household, storage, picked, _other = cellar
    bad = _ask(household, user, {"sentence": "bad", "shown": picked.id})
    _opened(storage, picked, user, bad.created + timedelta(hours=1), vote="down")
    # Days earlier, so the bottle opened after "bad" is outside its window.
    _ask(household, user, {
        "sentence": "fussy", "shown": picked.id, "rejected": [1, 2], "outcome": "rejected",
    }, created=timezone.now() - timedelta(days=5))

    worst = chat_stats(30)["worst"]

    assert [row["sentence"] for row in worst] == ["bad", "fussy"]
    assert "2× something else" in worst[1]["why"]


def test_old_asks_and_other_kinds_are_left_out(cellar):
    user, household, _storage, picked, _other = cellar
    _ask(household, user, {"shown": picked.id}, created=timezone.now() - timedelta(days=40))
    TasteIntent.objects.create(household=household, user=user, kind=TasteIntent.LOOK, payload={})

    assert chat_stats(30)["asks"] == 0


def test_question_only_ask_counts_but_is_not_shown(cellar):
    user, household, _storage, _picked, _other = cellar
    _ask(household, user, {"question": "Red or white?", "shown": None})

    stats = chat_stats(30)

    assert stats["asks"] == 1
    assert stats["questions"] == 1
    assert stats["shown"] == 0
    assert stats["opened_pick_pct"] == 0


def test_endpoint_is_staff_only(client, cellar):
    user, household, _storage, picked, _other = cellar
    _ask(household, user, {"shown": picked.id})
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"

    assert client.get(reverse("api-scanner-chat-stats")).status_code == 403

    user.is_staff = True
    user.save()
    response = client.get(reverse("api-scanner-chat-stats"), {"days": 7})
    assert response.status_code == 200
    assert response.json()["asks"] == 1
    assert response.json()["days"] == 7


def test_endpoint_rejects_bad_days(client, cellar):
    user, *_ = cellar
    user.is_staff = True
    user.save()
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"

    assert client.get(reverse("api-scanner-chat-stats"), {"days": "lots"}).status_code == 400


def test_model_and_latency_can_be_patched_onto_an_ask(client, cellar):
    user, household, _storage, picked, _other = cellar
    intent = _ask(household, user, {"shown": picked.id})
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"

    response = client.patch(
        reverse("api-household-taste-intent", args=[intent.id]),
        data={"model": "quick", "latency_ms": 900, "fallback": True},
        content_type="application/json",
    )

    assert response.status_code == 200
    intent.refresh_from_db()
    assert intent.payload["model"] == "quick"
    assert intent.payload["latency_ms"] == 900
    assert intent.payload["fallback"] is True
