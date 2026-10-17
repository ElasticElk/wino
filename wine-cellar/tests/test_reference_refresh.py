"""Reference refresh runs off the request path.

A Grok live search budgets 120s per attempt and retries once. Run inline that
outlived gunicorn's 30s default timeout, and with the default single sync
worker it blocked every other request until it died. POST now queues and
returns 202; callers poll.
"""

from unittest.mock import patch

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.references import perform_reference_refresh

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


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def user(db, django_user_model, household):
    u = django_user_model.objects.create_user(username="ref", password="pw")
    HouseholdMembership.objects.filter(user=u).delete()  # drop the factory-free default
    HouseholdMembership.objects.create(user=u, household=household, role="owner")
    return u


@pytest.fixture
def wine(db, user, household):
    return Wine.objects.create(
        name="Old Vine Chenin", wine_type="WH", country="ZA",
        vintage=2020, user=user, household=household,
    )


def test_post_queues_and_returns_immediately(client, user, wine):
    """202 + a task id, not a four-minute request."""
    client.force_login(user)
    with patch("wine_cellar.apps.wine.tasks.refresh_wine_references.delay") as delay:
        delay.return_value.id = "task-123"
        response = client.post(
            reverse("api-wine-refresh-references", kwargs={"pk": wine.pk}),
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-123"
    assert response.json()["status"] == "queued"
    delay.assert_called_once_with(wine.id, user.id, False)


def test_post_404s_for_another_households_wine(client, db, django_user_model, wine):
    other = django_user_model.objects.create_user(username="outsider2", password="pw")
    HouseholdMembership.objects.create(
        user=other, household=Household.objects.create(name="Luttik"), role="owner",
    )
    client.force_login(other)
    with patch("wine_cellar.apps.wine.tasks.refresh_wine_references.delay") as delay:
        response = client.post(
            reverse("api-wine-refresh-references", kwargs={"pk": wine.pk}),
        )
    assert response.status_code == 404
    delay.assert_not_called()


def test_user_with_no_household_gets_403_not_500(client, db, django_user_model, wine):
    """An account created outside the normal flow used to raise, not respond."""
    stray = django_user_model.objects.create_user(username="stray", password="pw")
    HouseholdMembership.objects.filter(user=stray).delete()
    client.force_login(stray)
    response = client.post(
        reverse("api-wine-refresh-references", kwargs={"pk": wine.pk}),
    )
    assert response.status_code == 403


def test_get_without_task_id_reports_current_values(client, user, wine):
    wine.critic_score = 90
    wine.save()
    client.force_login(user)
    response = client.get(
        reverse("api-wine-refresh-references", kwargs={"pk": wine.pk}),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "IDLE"
    assert response.json()["current"]["critic_score"] == 90


def test_refresh_persists_validated_fields(wine):
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=GOOD):
        result = perform_reference_refresh(wine.id)

    assert result["updated"] is True
    wine.refresh_from_db()
    assert wine.critic_score == 92
    assert float(wine.vivino_rating) == 4.1
    assert float(wine.market_price) == 240.0
    assert wine.market_price_currency == "ZAR"
    # Aggregate confidence is the best of the persisted fields.
    assert wine.reference_confidence == "high"
    assert wine.reference_updated is not None


def test_low_confidence_is_not_persisted(wine):
    weak = dict(GOOD, critic_score_confidence="low", vivino_rating_confidence="low",
                market_price_confidence="low")
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=weak):
        result = perform_reference_refresh(wine.id)

    assert result["updated"] is False
    wine.refresh_from_db()
    assert wine.critic_score is None


def test_generic_reasoning_is_rejected(wine):
    hand_wavy = dict(GOOD, critic_score_reasoning="based on the grape variety")
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=hand_wavy):
        perform_reference_refresh(wine.id)

    wine.refresh_from_db()
    assert wine.critic_score is None      # rejected
    assert float(wine.vivino_rating) == 4.1  # the others still land independently


def test_a_bad_run_keeps_prior_values(wine):
    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=GOOD):
        perform_reference_refresh(wine.id)

    with patch("wine_cellar.apps.wine.references.fetch_references", return_value=None):
        result = perform_reference_refresh(wine.id, force=True)

    assert result["updated"] is False
    wine.refresh_from_db()
    assert wine.critic_score == 92  # not destroyed by the failed run


def test_missing_wine_is_handled(db):
    assert perform_reference_refresh(999999)["updated"] is False


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"


@pytest.mark.django_db
def test_apply_writes_a_medium_confidence_rating(client, user, wine):
    from datetime import timedelta

    from django.utils import timezone

    _auth(client, user)
    response = client.post(
        reverse("api-wine-apply-references", kwargs={"pk": wine.pk}),
        data={
            "updated": True,
            "confidence": "high",
            "details": {
                "vivino_rating": {"value": 4.2, "confidence": "medium"},
                "critic_score": {"value": 99, "confidence": "low"},
            },
        },
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    assert response.json()["vivino_rating"] == 4.2
    wine.refresh_from_db()
    assert float(wine.vivino_rating) == 4.2
    assert wine.critic_score is None
    assert wine.reference_updated is not None
    assert wine.reference_confidence == "high"
    assert timezone.now() - wine.reference_updated < timedelta(minutes=1)


@pytest.mark.django_db
def test_apply_404s_for_another_household(client, db, django_user_model, wine):
    other = django_user_model.objects.create_user(
        username="outsider-apply", password="pw",
    )
    HouseholdMembership.objects.create(
        user=other, household=Household.objects.create(name="Elsewhere"), role="owner",
    )
    _auth(client, other)
    response = client.post(
        reverse("api-wine-apply-references", kwargs={"pk": wine.pk}),
        data={"details": {"vivino_rating": {"value": 4.0, "confidence": "high"}}},
        content_type="application/json",
    )
    assert response.status_code == 404
    wine.refresh_from_db()
    assert wine.vivino_rating is None


def test_recent_references_skip_another_search(wine):
    from datetime import timedelta

    from django.utils import timezone

    wine.reference_updated = timezone.now() - timedelta(days=1)
    wine.vivino_rating = 4.0
    wine.save()
    with patch("wine_cellar.apps.wine.references.fetch_references") as fetch:
        result = perform_reference_refresh(wine.id)
    fetch.assert_not_called()
    assert result["skipped"] is True
    assert result["updated"] is False

    with patch(
        "wine_cellar.apps.wine.references.fetch_references", return_value=GOOD,
    ) as fetch:
        forced = perform_reference_refresh(wine.id, force=True)
    fetch.assert_called_once()
    assert forced["updated"] is True
