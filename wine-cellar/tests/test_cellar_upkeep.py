from datetime import timedelta
from pathlib import Path
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.models import Household, ServiceConfig, TelegramUser
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.tasks import empty_cellar_nudge


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"


def _ok():
    response = MagicMock()
    response.status_code = 200
    return response


@pytest.mark.django_db
def test_last_seen_updates_once_inside_five_minutes(client, user):
    chat = TelegramUser.objects.create(
        user=user, chat_id="9001", display_name="Sam",
        last_seen_at=timezone.now() - timedelta(minutes=6),
    )
    _auth(client, user)
    client.get(reverse("api-wines-in-stock"))
    chat.refresh_from_db()
    stamped = chat.last_seen_at
    assert stamped > timezone.now() - timedelta(minutes=1)

    client.get(reverse("api-wines-in-stock"))
    chat.refresh_from_db()
    assert chat.last_seen_at == stamped


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_empty_cellar_nudge_is_one_shot(post, user):
    ServiceConfig.objects.update_or_create(
        key="bot_token", defaults={"value": "test-token"},
    )
    TelegramUser.objects.create(
        user=user, chat_id="9001", display_name=user.username, is_active=True,
    )
    household = user.household_memberships.first().household
    young = Household.objects.create(name="Brand new")
    Household.objects.filter(pk=household.pk).update(
        created=timezone.now() - timedelta(days=4),
    )
    post.return_value = _ok()

    empty_cellar_nudge()

    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["chat_id"] == "9001"
    assert "front label" in post.call_args.kwargs["json"]["text"]
    household.refresh_from_db()
    assert household.empty_nudge_sent_at is not None
    young.refresh_from_db()
    assert young.empty_nudge_sent_at is None

    post.reset_mock()
    empty_cellar_nudge()
    post.assert_not_called()


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_nudge_skips_a_cellar_that_has_ever_had_a_wine(post, user, wine_factory):
    ServiceConfig.objects.update_or_create(
        key="bot_token", defaults={"value": "test-token"},
    )
    TelegramUser.objects.create(user=user, chat_id="9002", is_active=True)
    household = user.household_memberships.first().household
    Household.objects.filter(pk=household.pk).update(
        created=timezone.now() - timedelta(days=4),
    )
    wine = wine_factory(user=user)
    StorageItem.objects.create(
        wine=wine, storage=user.storage_set.first(), deleted=True,
    )
    post.return_value = _ok()

    empty_cellar_nudge()

    post.assert_not_called()
    household.refresh_from_db()
    assert household.empty_nudge_sent_at is None


@pytest.mark.django_db
@patch("wine_cellar.apps.wine.tasks.requests.post")
def test_nudge_skips_staff_and_messages_members(post, user, user_factory):
    ServiceConfig.objects.update_or_create(
        key="bot_token", defaults={"value": "test-token"},
    )
    household = user.household_memberships.first().household
    Household.objects.filter(pk=household.pk).update(
        created=timezone.now() - timedelta(days=4),
    )
    user.is_staff = True
    user.save()
    TelegramUser.objects.create(user=user, chat_id="1", is_active=True)
    member = user_factory(household=household)
    TelegramUser.objects.create(user=member, chat_id="2", is_active=True)
    post.return_value = _ok()

    empty_cellar_nudge()

    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["chat_id"] == "2"


@pytest.mark.django_db
def test_backfill_dry_run_queues_nothing(wine):
    out = StringIO()
    with patch("wine_cellar.apps.wine.tasks.refresh_wine_references.delay") as delay:
        call_command("backfill_references", "--dry-run", stdout=out)
    delay.assert_not_called()
    assert str(wine.id) in out.getvalue()


def test_web_start_always_migrates():
    """Autoreload does not migrate. Only web applies schema; workers wait."""
    root = Path(__file__).resolve().parents[1]
    for name in ("entrypoint.sh", "entrypoint.prod.sh"):
        text = (root / name).read_text()
        assert "set -e" in text.splitlines()[:3], name
        role_at = text.index("CONTAINER_ROLE")
        apply_at = text.index("python manage.py migrate --noinput")
        check_at = text.index("python manage.py migrate --check")
        assert apply_at > role_at, name
        assert check_at > apply_at, name
    compose = (root / "docker-compose.yml").read_text()
    assert "python manage.py migrate --noinput" in compose
    assert compose.split("celery:")[1].count("migrate --noinput") == 0


def test_worker_migrate_wait_times_out():
    """A stuck schema must fail loud — not hang celery/beat forever."""
    root = Path(__file__).resolve().parents[1]
    for name in ("entrypoint.sh", "entrypoint.prod.sh"):
        text = (root / name).read_text()
        wait_block = text.split("else")[-1]
        assert "MIGRATE_WAIT_SECS:-300" in wait_block, name
        assert "Last migrate --check" in wait_block, name
        assert "exit 1" in wait_block, name
        assert ">/dev/null" not in wait_block, name
