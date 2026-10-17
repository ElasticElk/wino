from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, HouseholdMembership


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"


@pytest.fixture
def household(db):
    return Household.objects.create(name="Hillside")


@pytest.fixture
def user_a(db, django_user_model, household):
    user = django_user_model.objects.create_user(username="owner-a", password="pw")
    HouseholdMembership.objects.filter(user=user).delete()
    HouseholdMembership.objects.create(user=user, household=household, role="owner")
    return user


@pytest.fixture
def user_b(db, django_user_model):
    user = django_user_model.objects.create_user(username="owner-b", password="pw")
    HouseholdMembership.objects.filter(user=user).delete()
    HouseholdMembership.objects.create(
        user=user, household=Household.objects.create(name="Elsewhere"), role="owner",
    )
    return user


@pytest.mark.django_db
def test_another_user_cannot_read_a_lookup_task(client, user_a, user_b):
    result = MagicMock()
    result.state = "SUCCESS"
    result.successful.return_value = True
    result.failed.return_value = False
    result.result = {"updated": True, "vivino_rating": 4.1}

    _auth(client, user_a)
    with patch("wine_cellar.apps.wine.tasks.lookup_wine_references.delay") as delay:
        delay.return_value.id = "task-fixed"
        queued = client.post(
            reverse("api-reference-lookup"),
            data={"name": "Bin 311"},
            content_type="application/json",
        )
    assert queued.status_code == 202

    with patch("celery.result.AsyncResult") as async_result:
        async_result.return_value = result
        client.defaults["HTTP_AUTHORIZATION"] = (
            f"Token {Token.objects.get_or_create(user=user_b)[0].key}"
        )
        hidden = client.get(reverse("api-reference-lookup"), {"task_id": "task-fixed"})
        assert hidden.status_code == 404
        assert hidden.json()["error"] == "Task not found"
        async_result.assert_not_called()

        client.defaults["HTTP_AUTHORIZATION"] = (
            f"Token {Token.objects.get(user=user_a).key}"
        )
        visible = client.get(reverse("api-reference-lookup"), {"task_id": "task-fixed"})
    assert visible.status_code == 200
    assert visible.json()["state"] == "SUCCESS"
