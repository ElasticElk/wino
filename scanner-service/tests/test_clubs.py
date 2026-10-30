"""Club bot flows. API is mocked; copy, buttons, callbacks and EN/AF matter."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import catalog_keys, t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import (
    build_home,
    club_cb,
    clubs_enabled,
    parse_club_cb,
)
from integrations.telegram_commands import admin_commands, menu_commands, staff_command_payload


def _handler(api, *, clubs=True):
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
    handler.set_flags(flags)
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


def test_callback_budget_peek_large_ids():
    data = club_cb("p", 999_999_999, 888_888_888)
    assert len(data.encode("utf-8")) < CALLBACK_DATA_MAX
    data = club_cb("dy", 999_999_999)
    assert len(data.encode("utf-8")) < CALLBACK_DATA_MAX
    action, rest = parse_club_cb(data)
    assert action == "dy"


def test_menu_includes_club_only_when_flag_on():
    assert ("club", "Wine clubs") in menu_commands({"feature_clubs": True})
    assert "club" not in {c for c, _ in menu_commands({})}
    assert "club" not in {c for c, _ in menu_commands({"feature_lists": True})}


@pytest.mark.asyncio
async def test_club_pitch_when_none():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    handler = _handler(api)
    update = _update()
    await handler._cmd_club(update, MagicMock(args=[]))
    text = update.message.reply_text.call_args.args[0]
    assert "own cellars" in text
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.found.btn") in labels


@pytest.mark.asyncio
async def test_club_off_one_liner():
    api = MagicMock()
    handler = _handler(api, clubs=False)
    update = _update()
    await handler._cmd_club(update, MagicMock(args=[]))
    assert update.message.reply_text.call_args.args[0] == t("en", "club.off")


@pytest.mark.asyncio
async def test_create_flow_and_invite_card():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    api.create_club.return_value = {
        "id": 3, "name": "Thursday reds", "role": "owner",
        "cellar_visibility": "full", "badges_visible": True,
        "sharing_version": 1, "member_count": 1, "status_code": 201,
    }
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "role": "owner",
        "cellar_visibility": "full", "badges_visible": True,
        "feed_muted": False, "member_count": 1, "events": [],
    }
    api.mint_club_invite.return_value = {
        "code": "K9m2Pq4r",
        "club_name": "Thursday reds",
        "cellar_visibility": "full",
        "badges_visible": True,
        "max_uses": 20,
        "expires_at": "2026-11-28T00:00:00+00:00",
        "status_code": 201,
    }
    handler = _handler(api)
    update = _update()
    await handler._cmd_club(update, MagicMock(args=["new"]))
    assert t("en", "club.found.name") in update.message.reply_text.call_args.args[0]
    named = _update(text="Thursday reds")
    await handler._club_flow().on_text(named, MagicMock())
    labels = _labels(named.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.share.full") in "".join(labels) or any(
        t("en", "club.share.full") in x for x in labels
    )
    query = _query(club_cb("c"))
    update.callback_query = query
    update.effective_chat.id = 4242
    await handler._club_flow().on_callback(update, MagicMock())
    api.create_club.assert_called()
    assert api.mint_club_invite.called
    assert query.edit_message_text.called
    assert query.message.reply_text.called
    invite_text = query.message.reply_text.call_args.args[0]
    assert "Thursday reds" in invite_text
    assert "t.me/whineybot?start=K9m2Pq4r" in invite_text


@pytest.mark.asyncio
async def test_start_club_code_vs_household():
    api = MagicMock()
    api.preview_invite.return_value = {
        "kind": "club",
        "code": "K9m2Pq4r",
        "club": {"id": 3, "name": "Thursday reds"},
        "cellar_visibility": "full",
        "badges_visible": True,
        "inviter_display_name": "Riley",
        "expires_at": "2026-11-28T00:00:00+00:00",
        "expired": False,
        "used": False,
    }
    api.list_households.return_value = [{"id": 4, "name": "Hillside"}]
    handler = _handler(api)
    handler.api = api
    update = _update(text="/start K9m2Pq4r")
    context = MagicMock()
    context.args = ["K9m2Pq4r"]
    await handler._invite_flow().start(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "Thursday reds" in text
    assert "Hillside" in text
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert any("Join" in x for x in labels)


@pytest.mark.asyncio
async def test_stranger_compound_asks_for_cellar_name():
    api = MagicMock()
    api.preview_invite.return_value = {
        "kind": "club",
        "code": "K9m2Pq4r",
        "club": {"id": 3, "name": "Thursday reds"},
        "cellar_visibility": "full",
        "badges_visible": True,
        "inviter_display_name": "Riley",
        "expired": False,
        "used": False,
    }
    handler = _handler(api)
    handler.api = api
    handler.chat_ids = []
    update = _update(chat_id=999, text="/start K9m2Pq4r")
    context = MagicMock()
    context.args = ["K9m2Pq4r"]
    await handler._invite_flow().start(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "own cellar" in text.lower() or "call it" in text.lower()


@pytest.mark.asyncio
async def test_mute_toggles():
    api = MagicMock()
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "feed_muted": False,
        "role": "member", "cellar_visibility": "full", "badges_visible": True,
        "member_count": 2, "events": [],
    }
    api.patch_club_membership.return_value = {}
    handler = _handler(api)
    query = _query(club_cb("u", 3))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    api.patch_club_membership.assert_called()
    kwargs = api.patch_club_membership.call_args
    assert kwargs[0][0] == 3
    assert kwargs[1]["feed_muted"] is True


def test_slash_menus_stay_english():
    blob = " ".join(
        d for _, d in menu_commands({"feature_lists": True, "feature_clubs": True})
    )
    blob += " " + " ".join(
        d for _, d in admin_commands({"feature_global_clubs": True})
    )
    assert "Wine clubs" in blob
    assert "Things to drink or buy" in blob
    assert "Wynklubs" not in blob
    assert "Ontdek" not in blob
    assert "clubpost" not in {
        c["command"] for c in staff_command_payload({"feature_clubs": True})
    }
    assert "clubpost" in {
        c["command"]
        for c in staff_command_payload({"feature_global_clubs": True})
    }


@pytest.mark.asyncio
async def test_join_blocked_when_clubs_off():
    api = MagicMock()
    handler = _handler(api, clubs=False)
    query = _query(club_cb("j", "AbCdEfGh"))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert query.edit_message_text.call_args.args[0] == t("en", "club.off")


@pytest.mark.asyncio
async def test_help_and_status_omit_clubs_when_off():
    api = MagicMock()
    api.wines_in_stock.return_value = [
        {"name": "Merlot", "stock_count": 1, "wine_type": "RE", "producer": ""},
    ]
    api.list_storages.return_value = []
    handler = _handler(api, clubs=False)
    handler._router = None
    update = _update(text="/help")
    await handler._cmd_help(update, MagicMock())
    assert "/club" not in update.message.reply_text.call_args.args[0]
    status = _update(text="/status")
    await handler._cmd_status(status, MagicMock())
    body = status.message.reply_text.call_args.args[0]
    assert t("en", "club.status.ready") not in body
    assert t("en", "club.status.resting") not in body
    assert t("en", "list.status.ready") not in body
    assert t("en", "list.status.resting") not in body


@pytest.mark.asyncio
async def test_help_and_status_include_clubs():
    api = MagicMock()
    api.wines_in_stock.return_value = [
        {"name": "Merlot", "stock_count": 1, "wine_type": "RE", "producer": ""},
    ]
    api.list_storages.return_value = []
    handler = _handler(api)
    handler._router = None
    update = _update(text="/help")
    await handler._cmd_help(update, MagicMock())
    assert "/club" in update.message.reply_text.call_args.args[0]
    status = _update(text="/status")
    await handler._cmd_status(status, MagicMock())
    assert t("en", "club.status.ready") in status.message.reply_text.call_args.args[0]


def test_phase2_keys_en_and_af():
    required = {
        "club.off", "club.pitch", "club.found.btn", "club.hh.notice",
        "club.widen.stay", "club.end.ballot", "club.policy.full",
        "club.join.known", "club.muted",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    notice = t("en", "club.hh.notice")
    assert "{name}" not in notice
    assert "{club}" not in notice
    assert t("af", "club.found.btn") == "Begin 'n klub"


def test_home_buttons_under_budget():
    text, markup = build_home(
        {
            "id": 12, "name": "Thursday reds", "member_count": 4,
            "cellar_visibility": "full", "badges_visible": True,
            "role": "owner", "feed_muted": False, "events": [],
        },
        "en",
    )
    assert "Thursday reds" in text
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) < CALLBACK_DATA_MAX


def test_clubs_enabled_uses_getattr_for_bare_handler():
    handler = TelegramBotHandler.__new__(TelegramBotHandler)
    assert clubs_enabled(handler) is False
    handler.flags = {"feature_clubs": True}
    assert clubs_enabled(handler) is True


@pytest.mark.asyncio
async def test_compound_skip_names_cellar():
    api = MagicMock()
    api.preview_invite.return_value = {
        "kind": "club",
        "code": "K9m2Pq4r",
        "club": {"id": 3, "name": "Thursday reds"},
        "cellar_visibility": "full",
        "badges_visible": True,
        "inviter_display_name": "Riley",
        "expired": False,
        "used": False,
    }
    handler = _handler(api)
    handler.api = api
    handler.chat_ids = []
    update = _update(chat_id=999, text="/start K9m2Pq4r")
    context = MagicMock()
    context.args = ["K9m2Pq4r"]
    await handler._invite_flow().start(update, context)
    skip = _update(chat_id=999, text="Skip")
    await handler._club_flow().on_text(skip, MagicMock())
    assert skip.message.reply_text.call_count >= 1
    join_text = skip.message.reply_text.call_args_list[-1].args[0]
    assert "Thursday reds" in join_text
    labels = _labels(skip.message.reply_text.call_args_list[-1].kwargs.get("reply_markup"))
    assert any("Join" in x for x in labels)


@pytest.mark.asyncio
async def test_afrikaans_back_on_members():
    api = MagicMock()
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "role": "owner",
        "cellar_visibility": "full", "badges_visible": True,
        "member_count": 1, "events": [],
    }
    api.club_members.return_value = {
        "members": [{
            "user_id": 1, "display_name": "Riley", "role": "owner",
            "household_id": 4, "household_name": "Hillside",
            "peek": "full", "badges": True,
        }],
        "households": [{"household_id": 4, "name": "Hillside", "user_ids": [1]}],
    }
    handler = _handler(api)
    handler.set_language("4242", "af")
    query = _query(club_cb("m", 3))
    update = _update()
    update.effective_user.language_code = "af"
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    labels = _labels(query.edit_message_text.call_args.kwargs["reply_markup"])
    assert t("af", "club.back") in labels
    assert t("af", "club.peek.btn") in labels


@pytest.mark.asyncio
async def test_mute_keeps_unmute_and_back():
    api = MagicMock()
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "feed_muted": False,
        "role": "member", "cellar_visibility": "full", "badges_visible": True,
        "member_count": 2, "events": [],
    }
    api.patch_club_membership.return_value = {}
    handler = _handler(api)
    query = _query(club_cb("u", 3))
    update = _update()
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    labels = _labels(query.edit_message_text.call_args.kwargs.get("reply_markup"))
    assert t("en", "club.home.unmute") in labels
    assert t("en", "club.back") in labels


def test_join_callback_under_budget():
    data = club_cb("j", "K9m2Pq4r")
    assert len(data.encode("utf-8")) < CALLBACK_DATA_MAX
    data = club_cb("ph", "K9m2Pq4r", 99)
    assert len(data.encode("utf-8")) < CALLBACK_DATA_MAX


@pytest.mark.asyncio
async def test_afrikaans_pitch():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": []}
    handler = _handler(api)
    handler.set_language("4242", "af")
    update = _update()
    update.effective_user.language_code = "af"
    await handler._cmd_club(update, MagicMock(args=[]))
    assert "Wynklubs" in update.message.reply_text.call_args.args[0]
