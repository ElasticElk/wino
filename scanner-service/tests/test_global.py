"""Phase 4 Telegram: Discover, join consent, global home, staff post."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import catalog_keys, t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import club_cb, parse_club_cb
from integrations.telegram_commands import ADMIN_COMMANDS, menu_commands


def _handler(api, *, clubs=True, global_clubs=True, staff=False):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock) or not hasattr(
        api, "list_all_wines",
    ):
        api.list_all_wines = MagicMock(return_value=[])
    if not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    flags = {}
    if clubs:
        flags["feature_clubs"] = "on"
    if global_clubs:
        flags["feature_global_clubs"] = "on"
    handler.set_flags(flags)
    if staff:
        handler.admin_chat_ids = ["4242"]
    return handler


def _update(chat_id=4242, text="/club"):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.effective_user.full_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    return u


def _query(data, *, text="card"):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.text = text
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _labels(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def test_callback_budget_discover_join():
    data = club_cb("dj", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    action, rest = parse_club_cb(data)
    assert action == "dj"
    data = club_cb("jj", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    data = club_cb("dc")
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    data = club_cb("gp", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX


def test_clubpost_is_staff_menu_only():
    assert ("clubpost", "Post to a global club") in ADMIN_COMMANDS
    assert "clubpost" not in {c for c, _ in menu_commands({"feature_clubs": True})}
    assert "clubpost" not in {c for c, _ in menu_commands({
        "feature_clubs": True, "feature_global_clubs": True,
    })}


def test_phase4_keys_en_and_af():
    required = {
        "club.discover.title",
        "club.discover.btn",
        "club.global.consent",
        "club.global.private_cellar",
        "club.global.join",
        "club.global.not_now",
        "club.global.frozen",
        "club.global.kicked",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert "Your cellar stays private" in t("en", "club.global.consent", name="New World Cab")
    assert "Jou kelder bly privaat" in t("af", "club.global.consent", name="New World Cab")
    assert t("en", "club.global.private_cellar") == "Your cellar stays private."
    assert t("af", "club.global.private_cellar") == "Jou kelder bly privaat."
    assert t("af", "club.discover.btn") == "Ontdek"


@pytest.mark.asyncio
async def test_discover_on_empty_pitch_when_flag_on():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    handler = _handler(api)
    update = _update()
    await handler._cmd_club(update, MagicMock(args=[]))
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.found.btn") in labels
    assert t("en", "club.discover.btn") in labels


@pytest.mark.asyncio
async def test_discover_hidden_when_flag_off():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    handler = _handler(api, global_clubs=False)
    update = _update()
    await handler._cmd_club(update, MagicMock(args=[]))
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.discover.btn") not in labels


@pytest.mark.asyncio
async def test_club_discover_command_lists_join():
    api = MagicMock()
    api.discover_clubs.return_value = {
        "clubs": [{
            "id": 10,
            "name": "New World Cab",
            "description": "Cabs that drink well now.",
            "member_count": 6,
            "joined": False,
            "frozen": False,
        }],
    }
    handler = _handler(api)
    update = _update(text="/club discover")
    await handler._cmd_club(update, MagicMock(args=["discover"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Clubs for everyone" in text
    assert "New World Cab" in text
    assert "Cabs that drink well now" in text
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert any("Join" in x and "New World Cab" in x for x in labels)


@pytest.mark.asyncio
async def test_join_consent_says_cellar_stays_private():
    api = MagicMock()
    api.discover_clubs.return_value = {
        "clubs": [{
            "id": 10, "name": "New World Cab", "description": "",
            "joined": False, "frozen": False,
        }],
    }
    handler = _handler(api)
    query = _query(club_cb("dj", 10))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Your cellar stays private" in text
    assert "drink a bottle" in text
    assert "Hillside" not in text
    labels = _labels(query.edit_message_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.global.join") in labels
    assert t("en", "club.global.not_now") in labels


@pytest.mark.asyncio
async def test_join_consent_afrikaans():
    api = MagicMock()
    api.discover_clubs.return_value = {
        "clubs": [{
            "id": 10, "name": "New World Cab", "description": "",
            "joined": False, "frozen": False,
        }],
    }
    handler = _handler(api)
    handler.set_language("4242", "af")
    query = _query(club_cb("dj", 10))
    update = _update()
    update.effective_user.language_code = "af"
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Jou kelder bly privaat" in text
    labels = _labels(query.edit_message_text.call_args.kwargs["reply_markup"])
    assert t("af", "club.global.join") in labels
    assert t("af", "club.global.not_now") in labels


@pytest.mark.asyncio
async def test_confirm_join_opens_home_without_invite():
    api = MagicMock()
    api.join_club.return_value = {
        "id": 10, "name": "New World Cab", "kind": "global",
        "role": "member", "cellar_visibility": "none", "badges_visible": False,
        "feed_muted": False, "member_count": 7, "events": [],
        "description": "Cabs that drink well now.", "status_code": 201,
    }
    api.get_club.return_value = {
        "id": 10, "name": "New World Cab", "kind": "global",
        "role": "member", "cellar_visibility": "none", "badges_visible": False,
        "feed_muted": False, "member_count": 7, "events": [],
        "description": "Cabs that drink well now.",
    }
    handler = _handler(api)
    query = _query(club_cb("jj", 10))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    api.join_club.assert_called_with(10)
    text = query.edit_message_text.call_args.args[0]
    assert "New World Cab" in text
    assert "Cabs that drink well now" in text
    labels = _labels(query.edit_message_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.home.invite") not in labels
    assert t("en", "club.home.settings") not in labels
    assert t("en", "club.home.leave") in labels
    assert t("en", "club.home.mute") in labels
    assert t("en", "club.discover.btn") in labels


@pytest.mark.asyncio
async def test_global_members_have_no_peek_buttons():
    api = MagicMock()
    api.get_club.return_value = {
        "id": 10, "name": "New World Cab", "kind": "global",
        "role": "member", "cellar_visibility": "none", "badges_visible": False,
        "member_count": 2, "events": [],
    }
    api.club_members.return_value = {
        "members": [{
            "user_id": 1, "display_name": "Riley", "role": "owner",
            "household_id": 4, "household_name": "Hillside",
            "peek": "none", "badges": False,
        }],
        "households": [{"household_id": 4, "name": "Hillside", "user_ids": [1]}],
    }
    handler = _handler(api)
    query = _query(club_cb("m", 10))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    labels = _labels(query.edit_message_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.peek.btn") not in labels
    assert t("en", "club.badges.btn") not in labels


@pytest.mark.asyncio
async def test_frozen_club_shows_paused():
    api = MagicMock()
    api.discover_clubs.return_value = {
        "clubs": [{
            "id": 10, "name": "New World Cab", "description": "",
            "joined": False, "frozen": True,
        }],
    }
    handler = _handler(api)
    update = _update()
    await handler._club_flow()._show_discover(update)
    text = update.message.reply_text.call_args.args[0]
    assert t("en", "club.global.frozen") in text
    markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
    assert markup is None or t("en", "club.global.join") not in _labels(markup)


@pytest.mark.asyncio
async def test_staff_clubpost_prompts_and_posts():
    api = MagicMock()
    api.discover_clubs.return_value = {
        "clubs": [{
            "id": 10, "name": "New World Cab", "description": "",
            "joined": True, "frozen": False,
        }],
    }
    api.post_club_note.return_value = {"event_id": 91, "status_code": 201}
    handler = _handler(api, staff=True)
    update = _update(text="/clubpost New World Cab")
    await handler._club_flow().cmd_clubpost(update, MagicMock(args=["New", "World", "Cab"]))
    assert "What should" in update.message.reply_text.call_args.args[0]
    typed = _update(text="This week: Cabs that actually drink well now.")
    await handler._club_flow().on_text(typed, MagicMock())
    api.post_club_note.assert_called_with(
        10, "This week: Cabs that actually drink well now.",
    )
    assert "Posted" in typed.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_clubpost_guest_is_staff_only():
    api = MagicMock()
    handler = _handler(api, staff=False)
    update = _update(text="/clubpost")
    await handler._club_flow().cmd_clubpost(update, MagicMock(args=[]))
    assert update.message.reply_text.call_args.args[0] == t("en", "club.global.staff_only")
    api.discover_clubs.assert_not_called()


@pytest.mark.asyncio
async def test_help_lists_discover_when_flag_on():
    api = MagicMock()
    handler = _handler(api)
    update = _update(text="/help")
    await handler._cmd_help(update, MagicMock())
    text = update.message.reply_text.call_args.args[0]
    assert "/club discover" in text


@pytest.mark.asyncio
async def test_afrikaans_discover_button():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    handler = _handler(api)
    handler.set_language("4242", "af")
    update = _update()
    update.effective_user.language_code = "af"
    await handler._cmd_club(update, MagicMock(args=[]))
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("af", "club.discover.btn") in labels
