from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.tasks import drink_by_reminder


def _stock(wine, user, deleted=False):
    storage = user.storage_set.first()
    return StorageItem.objects.create(wine=wine, storage=storage, deleted=deleted)


def _chat(user, chat_id="4242"):
    ServiceConfig.objects.update_or_create(
        key="bot_token", defaults={"value": "test-token"},
    )
    return TelegramUser.objects.create(
        user=user, chat_id=chat_id, display_name=user.username, is_active=True,
    )


def _ok():
    response = MagicMock()
    response.status_code = 200
    return response


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_window_includes_due_soon_and_overdue(post, user, wine_factory):
    _chat(user)
    today = timezone.localdate()
    due = wine_factory(
        name="Bin 311", drink_by=today + timedelta(days=10), user=user,
    )
    later = wine_factory(
        name="Too Soon", drink_by=today + timedelta(days=15), user=user,
    )
    overdue = wine_factory(
        name="Old Barolo", drink_by=today - timedelta(days=1), user=user,
    )
    _stock(due, user)
    _stock(later, user)
    _stock(overdue, user)
    post.return_value = _ok()

    drink_by_reminder()

    body = post.call_args.kwargs["json"]["text"]
    assert post.call_args.kwargs["json"]["chat_id"] == "4242"
    assert "Bin 311" in body
    assert "Old Barolo" in body
    assert "overdue" in body
    assert "Too Soon" not in body
    due.refresh_from_db()
    overdue.refresh_from_db()
    later.refresh_from_db()
    assert due.drink_by_reminded_on == today
    assert overdue.drink_by_reminded_on == today
    assert later.drink_by_reminded_on is None


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_drunk_bottle_is_not_reminded(post, user, wine_factory):
    _chat(user)
    wine = wine_factory(drink_by=timezone.localdate(), user=user)
    _stock(wine, user, deleted=True)
    post.return_value = _ok()

    drink_by_reminder()

    post.assert_not_called()
    wine.refresh_from_db()
    assert wine.drink_by_reminded_on is None


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_recent_reminder_waits_a_week(post, user, wine_factory):
    _chat(user)
    today = timezone.localdate()
    recent = wine_factory(
        name="Reminded Yesterday",
        drink_by=today,
        drink_by_reminded_on=today - timedelta(days=1),
        user=user,
    )
    stale = wine_factory(
        name="Reminded Last Week",
        drink_by=today,
        drink_by_reminded_on=today - timedelta(days=8),
        user=user,
    )
    _stock(recent, user)
    _stock(stale, user)
    post.return_value = _ok()

    drink_by_reminder()

    body = post.call_args.kwargs["json"]["text"]
    assert "Reminded Last Week" in body
    assert "Reminded Yesterday" not in body
    recent.refresh_from_db()
    stale.refresh_from_db()
    assert recent.drink_by_reminded_on == today - timedelta(days=1)
    assert stale.drink_by_reminded_on == today


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_does_not_send_email(post, user, wine_factory):
    from django.core import mail

    _chat(user)
    wine = wine_factory(drink_by=timezone.localdate(), user=user)
    _stock(wine, user)
    post.return_value = _ok()

    drink_by_reminder()

    assert len(mail.outbox) == 0
    assert Wine.objects.get(pk=wine.pk).drink_by_reminded_on == timezone.localdate()
