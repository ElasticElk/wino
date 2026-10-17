"""Staff admin surface on the existing bot — scoped by chat, not a second bot."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_admin import (
    cmd_adduser,
    cmd_members,
    cmd_newhousehold,
    cmd_removeuser,
    cmd_stats,
    cmd_welcome,
    cmd_whois,
    parse_admin_removeuser_args,
    parse_newhousehold_args,
    register_admin_handlers,
)
from integrations.telegram_commands import (
    ADMIN_COMMANDS,
    PUBLIC_COMMANDS,
    public_command_payload,
    staff_command_payload,
)


def test_staff_menu_is_public_plus_admin_not_a_replacement():
    public = {c for c, _ in PUBLIC_COMMANDS}
    staff = [c for c, _ in PUBLIC_COMMANDS + ADMIN_COMMANDS]
    assert staff[: len(PUBLIC_COMMANDS)] == [c for c, _ in PUBLIC_COMMANDS]
    assert "recent" in public
    assert "newhousehold" in {c for c, _ in ADMIN_COMMANDS}
    assert "newhousehold" not in public
    assert "invite" in public
    assert "newcellar" in {c for c, _ in ADMIN_COMMANDS}
    assert "newcellar" not in public
    assert {c["command"] for c in public_command_payload()} == public
    assert "admin" in {c["command"] for c in staff_command_payload()}


@pytest.mark.parametrize(
    "args, expected",
    [
        (["Riverside", "123456789", "Alex"], ("Riverside", "123456789", "Alex")),
        (
            ["The", "Broke", "Sommeliers", "123", "Ada", "Lovelace"],
            ("The Broke Sommeliers", "123", "Ada Lovelace"),
        ),
        (["Cellar", "2", "123456789", "Alex"], ("Cellar 2", "123456789", "Alex")),
        (["Riverside"], None),
        (["123456789", "Alex"], None),
        (["Riverside", "123456789"], None),
        ([], None),
    ],
)
def test_parse_newhousehold_args(args, expected):
    assert parse_newhousehold_args(args) == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (["Riverside", "123456789"], ("Riverside", "123456789")),
        (["Cellar", "2", "123456789"], ("Cellar 2", "123456789")),
        (["123456789"], None),
        (["Riverside"], None),
        (["Riverside", "123456789", "extra"], None),
        ([], None),
    ],
)
def test_parse_admin_removeuser_args(args, expected):
    assert parse_admin_removeuser_args(args) == expected


def test_register_admin_handlers_is_a_noop_without_staff():
    app = MagicMock()
    handler = MagicMock()
    handler.admin_chat_ids = []
    register_admin_handlers(app, handler)
    app.add_handler.assert_not_called()


@pytest.mark.asyncio
async def test_newhousehold_provisions_and_welcomes():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Riverside", "999", "Alex"]

    api = MagicMock()
    api.create_household.return_value = {
        "household": {"id": 7, "name": "Riverside"},
        "owner": {"username": "alex", "created": True},
    }
    handler = MagicMock()
    handler._get_api.return_value = api
    handler._api_factory = MagicMock(return_value="owner-api")
    handler.add_chat_id = MagicMock()
    handler._welcome_new_member = AsyncMock(return_value=True)

    await cmd_newhousehold(update, context, handler)

    api.create_household.assert_called_once_with("Riverside", "999", "Alex")
    handler._api_factory.assert_called_once_with("999", 7)
    handler.add_chat_id.assert_called_once_with("999", api_client="owner-api")
    body = update.message.reply_text.call_args.args[0]
    assert "Riverside" in body
    assert "welcomed" in body.lower() or "photo" in body.lower() or "Owner" in body


@pytest.mark.asyncio
async def test_newhousehold_surfaces_api_errors():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Riverside", "999", "Alex"]
    api = MagicMock()
    api.create_household.return_value = {"error": "A household called Riverside already exists"}
    handler = MagicMock()
    handler._get_api.return_value = api
    handler._welcome_new_member = AsyncMock()

    await cmd_newhousehold(update, context, handler)

    handler._welcome_new_member.assert_not_awaited()
    assert "already exists" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_staff_adduser_requires_household():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["999", "Alex"]
    handler = MagicMock()
    handler._get_api.return_value = MagicMock()

    await cmd_adduser(update, context, handler)

    handler._get_api.return_value.add_household_member.assert_not_called()
    body = update.message.reply_text.call_args.args[0]
    assert "household" in body.lower()
    assert "/adduser" in body


@pytest.mark.asyncio
async def test_staff_adduser_targets_the_named_household():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Riverside", "999", "Alex"]

    api = MagicMock()
    api.list_households.return_value = [{"id": 7, "name": "Riverside"}]
    api.add_household_member.return_value = {
        "status": "created",
        "username": "alex",
        "household": "Riverside",
    }
    handler = MagicMock()
    handler._get_api.return_value = api
    handler._api_factory = MagicMock(return_value="owner-api")
    handler.add_chat_id = MagicMock()
    handler._welcome_new_member = AsyncMock(return_value=True)

    await cmd_adduser(update, context, handler)

    api.add_household_member.assert_called_once_with("999", "Alex", household_id=7)
    handler.add_chat_id.assert_called_once_with("999", api_client="owner-api")


@pytest.mark.asyncio
async def test_staff_removeuser_requires_household():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["999"]
    api = MagicMock()
    handler = MagicMock()
    handler._get_api.return_value = api

    await cmd_removeuser(update, context, handler)

    api.remove_household_member.assert_not_called()
    assert "household" in update.message.reply_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_staff_removeuser_targets_the_named_household():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Riverside", "999"]
    api = MagicMock()
    api.list_households.return_value = [{"id": 7, "name": "Riverside"}]
    api.remove_household_member.return_value = {
        "display_name": "Alex",
        "household": "Riverside",
    }
    handler = MagicMock()
    handler._get_api.return_value = api

    await cmd_removeuser(update, context, handler)

    api.remove_household_member.assert_called_once_with("999", household_id=7)
    assert "Alex" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_members_loads_a_named_household():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Riverside"]
    api = MagicMock()
    api.list_households.return_value = [{"id": 7, "name": "Riverside"}]
    api.get_household.return_value = {
        "name": "Riverside",
        "members": [
            {"role": "owner", "telegram_name": "Alex", "telegram_chat_id": "999", "username": "alex"},
        ],
    }
    handler = MagicMock()
    handler._get_api.return_value = api

    await cmd_members(update, context, handler)

    api.get_household.assert_called_once_with(household_id=7)
    body = update.message.reply_text.call_args.args[0]
    assert "Alex" in body
    assert "999" in body


@pytest.mark.asyncio
async def test_whois_renders_households():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Alex"]
    api = MagicMock()
    api.whois.return_value = {
        "results": [{
            "chat_id": "999",
            "display_name": "Alex",
            "username": "alex",
            "is_staff": False,
            "is_active": True,
            "households": [{"id": 7, "name": "Riverside", "role": "owner", "is_default": True}],
        }],
    }
    handler = MagicMock()
    handler._get_api.return_value = api

    await cmd_whois(update, context, handler)

    api.whois.assert_called_once_with("Alex")
    body = update.message.reply_text.call_args.args[0]
    assert "Alex" in body
    assert "Riverside" in body


@pytest.mark.asyncio
async def test_welcome_resends_to_known_user():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["999"]
    api = MagicMock()
    api.whois.return_value = {
        "results": [{
            "display_name": "Alex",
            "username": "alex",
            "households": [{"name": "Riverside", "is_default": True}],
        }],
    }
    handler = MagicMock()
    handler._get_api.return_value = api
    handler._welcome_new_member = AsyncMock(return_value=True)

    await cmd_welcome(update, context, handler)

    handler._welcome_new_member.assert_awaited_once_with(context, "999", "Alex", "Riverside")
    assert "Alex" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_stats_lists_counts_and_scanners():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    api = MagicMock()
    api.stats.return_value = {
        "households": 9,
        "telegram_users": 12,
        "wines": 80,
        "bottles": 100,
        "scanners": [{"name": "Kitchen Pi", "household": "Hillside", "is_active": True}],
    }
    handler = MagicMock()
    handler._get_api.return_value = api

    await cmd_stats(update, MagicMock(), handler)

    body = update.message.reply_text.call_args.args[0]
    assert "9" in body
    assert "Kitchen Pi" in body
