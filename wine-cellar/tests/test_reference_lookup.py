"""References for a wine that isn't in the cellar.

The refresh endpoint needs a Wine row to write to. A label photographed in a
shop has none — and that is exactly when the critic score, the Vivino rating
and the going price decide whether the bottle comes home. Same queue-and-poll
shape, same validation, nothing persisted.
"""

from unittest.mock import patch

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.wine.references import blended_category, perform_reference_lookup

GOOD = {
    "critic_score": 92,
    "critic_score_confidence": "high",
    "critic_score_reasoning": "Tim Atkin SA Report 2022 gave the 2020 vintage 92pts",
    "vivino_rating": 4.1,
    "vivino_rating_confidence": "medium",
    "vivino_rating_reasoning": "Vivino averages 4.1 across ~250 ratings on the wine page",
    "market_price": 240.0,
    "market_price_currency": "ZAR",
    "market_price_confidence": "high",
    "market_price_reasoning": "Listed R230-R255 across Checkers, Woolworths and Norman Goodfellows",
}

URL = "api-reference-lookup"


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def user(db, django_user_model, household):
    u = django_user_model.objects.create_user(username="look", password="pw")
    HouseholdMembership.objects.filter(user=u).delete()
    HouseholdMembership.objects.create(user=u, household=household, role="owner")
    return u


def test_post_queues_and_returns_immediately(client, user):
    """202 + a task id — a live search must never be held open by a request."""
    client.force_login(user)
    with patch("wine_cellar.apps.wine.tasks.lookup_wine_references.delay") as delay:
        delay.return_value.id = "task-abc"
        response = client.post(
            reverse(URL),
            data={"name": "Old Vine Chenin", "producer": "Bosman", "vintage": 2020},
            content_type="application/json",
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-abc"
    args, kwargs = delay.call_args
    assert args[0] == "Old Vine Chenin"
    assert kwargs["producer"] == "Bosman"
    assert kwargs["vintage"] == 2020
    assert kwargs["user_id"] == user.id


def test_name_is_required(client, user):
    client.force_login(user)
    response = client.post(reverse(URL), data={}, content_type="application/json")
    assert response.status_code == 400


def test_get_without_task_id_is_a_400(client, user):
    """Unlike the wine refresh, there are no stored values to fall back on."""
    client.force_login(user)
    assert client.get(reverse(URL)).status_code == 400


def test_token_auth_works_for_the_scanner_service(client, user):
    token = Token.objects.create(user=user)
    with patch("wine_cellar.apps.wine.tasks.lookup_wine_references.delay") as delay:
        delay.return_value.id = "task-token"
        response = client.post(
            reverse(URL),
            data={"name": "Kanonkop Paul Sauer"},
            content_type="application/json",
            headers={"authorization": f"Token {token.key}"},
        )
    assert response.status_code == 202


def test_lookup_returns_validated_values_and_persists_nothing(db, user):
    from wine_cellar.apps.wine.models import Wine

    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=GOOD):
        result = perform_reference_lookup(
            "Old Vine Chenin", producer="Bosman", vintage=2020, user_id=user.id,
        )

    assert result["updated"] is True
    assert result["critic_score"] == 92
    assert result["vivino_rating"] == 4.1
    assert result["market_price"] == 240.0
    assert result["market_price_currency"] == "ZAR"
    assert result["confidence"] == "high"  # best of the per-field confidences
    assert Wine.objects.count() == 0


def test_low_confidence_and_generic_reasoning_are_dropped():
    """The shop case is held to the same standard as the cellar refresh."""
    junk = dict(GOOD, critic_score_confidence="low",
                vivino_rating_reasoning="typical for this style")
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=junk):
        result = perform_reference_lookup("Old Vine Chenin")

    assert result["critic_score"] is None
    assert result["vivino_rating"] is None
    assert result["market_price"] == 240.0


def test_nothing_found_is_reported_not_faked():
    empty = {f: None for f in ("critic_score", "vivino_rating", "market_price")}
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=empty):
        result = perform_reference_lookup("Unknown Bottle")
    assert result["updated"] is False
    assert result["confidence"] == "low"


def test_grok_failure_is_reported():
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=None):
        assert perform_reference_lookup("X") == {
            "updated": False, "error": "Grok request failed",
        }


def test_empty_name_never_reaches_grok():
    with patch("wine_cellar.apps.wine.references.fetch_references") as fetch:
        assert perform_reference_lookup("")["updated"] is False
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "critic, vivino, expected",
    [(92, 4.1, 89), (92, None, 92), (None, 4.1, 82), (None, None, None)],
)
def test_blended_category_matches_the_model(critic, vivino, expected):
    assert blended_category(critic, vivino) == expected


@pytest.mark.parametrize("returned, expected", [
    ("£", "GBP"), ("€", "EUR"), ("R", "ZAR"), ("euro", "EUR"),
    ("gbp", "GBP"), ("CHF", "CHF"), ("", "USD"), (None, "USD"), ("£/bottle", "USD"),
])
def test_market_price_currency_is_normalised(returned, expected):
    from wine_cellar.apps.wine.references import validated_fields

    values, _, _ = validated_fields({
        "market_price": 12,
        "market_price_confidence": "high",
        "market_price_reasoning": "Listed at 12 across three UK merchants",
        "market_price_currency": returned,
    }, "USD")
    assert values["market_price_currency"] == expected
