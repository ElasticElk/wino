"""Taste-off setup: only the host gets the setup buttons, and picking a
bottle never dead-ends."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import t
from integrations.telegram_bot import TelegramBotHandler
from integrations.telegram_clubs import club_cb

CLUB = {"id": 1, "name": "Thursday reds", "cellar_visibility": "full"}
SETUP = {
    "id": 9, "title": "Friday flight", "status": "setup", "host_id": 5,
    "host_name": "Riley", "bottles": [],
}


def _handler(*, is_host):
    api = MagicMock()
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    api.list_clubs.return_value = {"clubs": [dict(CLUB)]}
    api.get_club.return_value = dict(CLUB)
    api.list_tasteoffs.return_value = {"tasteoffs": [dict(SETUP, is_host=is_host)]}
    api.get_tasteoff.return_value = dict(SETUP, is_host=is_host)
    api.club_members.return_value = {"members": [
        {"user_id": 5, "display_name": "Riley", "peek": "full"},
    ]}
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    handler.set_flags({"feature_clubs": "on", "feature_tasteoffs": "on"})
    return handler, api


def _tap(data):
    u = MagicMock()
    u.effective_chat.id = 4242
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Sam"
    query = AsyncMock()
    query.data = data
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_markup = None
    query.edit_message_text = AsyncMock()
    u.callback_query = query
    return u


async def _run(handler, data):
    tap = _tap(data)
    await handler._club_flow().on_callback(tap, MagicMock())
    call = tap.callback_query.edit_message_text.call_args
    markup = call.kwargs.get("reply_markup")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
    return call.args[0], datas


@pytest.mark.asyncio
async def test_non_host_sees_who_hosts_not_the_setup_buttons():
    handler, _api = _handler(is_host=False)
    text, datas = await _run(handler, club_cb("to", 1))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")
    assert datas == [club_cb("h", 1)]
    assert club_cb("op", 9) not in datas


@pytest.mark.asyncio
async def test_host_still_gets_the_setup_buttons():
    handler, _api = _handler(is_host=True)
    text, datas = await _run(handler, club_cb("to", 1))
    assert "Friday flight" in text
    for action in ("tb", "tl", "tf", "op", "tx"):
        assert club_cb(action, 9) in datas


@pytest.mark.asyncio
async def test_older_server_without_is_host_keeps_setup_buttons():
    handler, api = _handler(is_host=True)
    api.get_tasteoff.return_value = dict(SETUP)  # no is_host key
    _text, datas = await _run(handler, club_cb("to", 1))
    assert club_cb("op", 9) in datas


@pytest.mark.asyncio
async def test_stale_open_tap_by_non_host_says_who_hosts():
    # Before: 403 forbidden -> "Couldn't reach the club just now".
    handler, api = _handler(is_host=False)
    api.open_tasteoff.return_value = {"error": "forbidden", "status_code": 403}
    text, _datas = await _run(handler, club_cb("op", 9))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")
    assert text != t("en", "club.err")


@pytest.mark.asyncio
async def test_stale_add_from_cellar_by_non_host_says_who_hosts():
    handler, api = _handler(is_host=False)
    api.club_peek.return_value = {"visibility": "full", "wines": [
        {"id": 154, "name": "Chenin Blanc", "vintage": 2022},
    ]}
    api.add_tasteoff_bottle.return_value = {"error": "forbidden", "status_code": 403}
    text, _datas = await _run(handler, club_cb("tw", 9, 154))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")


@pytest.mark.asyncio
async def test_empty_cellar_offers_type_a_name_and_back():
    # Before: "This club only shares a summary" with no buttons.
    handler, api = _handler(is_host=True)
    api.club_peek.return_value = {"visibility": "full", "wines": []}
    text, datas = await _run(handler, club_cb("tm", 9, 5))
    assert text == t("en", "club.to.cellar_empty")
    assert datas == [club_cb("tb", 9), club_cb("to", 1)]


@pytest.mark.asyncio
async def test_summary_only_club_still_offers_a_way_on():
    handler, api = _handler(is_host=True)
    api.get_club.return_value = dict(CLUB, cellar_visibility="summary")
    text, datas = await _run(handler, club_cb("tf", 9))
    assert text == t("en", "club.to.summary_only")
    assert datas == [club_cb("tb", 9), club_cb("to", 1)]


def test_new_copy_has_afrikaans():
    for key in ("club.to.hosting", "club.to.cellar_empty"):
        assert t("af", key) != t("en", key)
