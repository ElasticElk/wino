"""P2 scanner side: reaction taps, Want this, toasts, flag-off behaviour."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from integrations.copy import catalog_keys, t
from integrations.telegram_bot import TelegramBotHandler
from integrations.telegram_clubs import PALETTE, club_cb, reaction_buttons

ON = {"feature_clubs": "on", "feature_club_reactions": "on", "feature_lists": "on"}


def _handler(api, **flags):
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    handler.set_flags(flags)
    return handler


def _update(query):
    u = MagicMock()
    u.effective_chat.id = 4242
    u.effective_user.language_code = "en"
    u.callback_query = query
    return u


def _query(data, markup=None):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_text = AsyncMock()
    query.message.reply_markup = markup
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


def _card_markup(event_id=88):
    return InlineKeyboardMarkup([
        reaction_buttons(event_id),
        [
            InlineKeyboardButton("Say something", callback_data=club_cb("cm", event_id)),
            InlineKeyboardButton("Want this", callback_data=club_cb("wt", event_id)),
        ],
    ])


def test_palette_helper_compact_row_and_callbacks():
    row = reaction_buttons(88, [3, 0, 0, 1, 0])
    assert [b.text for b in row] == [f"{PALETTE[0]} 3", PALETTE[1], PALETTE[2], f"{PALETTE[3]} 1", PALETTE[4]]
    assert [b.callback_data for b in row][0] == "cl:rx:88:0"
    assert all(len(b.callback_data.encode()) <= 64 for b in row)
    assert PALETTE == ("\U0001F377", "\U0001F60D", "\U0001F44D", "\U0001F602", "\U0001F914")


@pytest.mark.asyncio
async def test_tap_toasts_and_refreshes_only_the_emoji_row():
    api = MagicMock()
    api.club_event_react.return_value = {"on": True, "counts": [2, 0, 1, 0, 0], "status_code": 200}
    handler = _handler(api, **ON)
    query = _query(club_cb("rx", 88, 0), _card_markup())
    await handler._club_flow().on_callback(_update(query), MagicMock())
    api.club_event_react.assert_called_once_with(88, 0, chat_id=4242, message_id=7)
    assert query.answer.call_args.args[0] == t("en", "club.rx.on", emoji=PALETTE[0])
    query.message.reply_text.assert_not_called()
    markup = query.edit_message_reply_markup.call_args.kwargs["reply_markup"]
    rows = markup.inline_keyboard
    assert rows[0][0].text == f"{PALETTE[0]} 2" and rows[0][2].text == f"{PALETTE[2]} 1"
    assert [b.text for b in rows[1]] == ["Say something", "Want this"]


@pytest.mark.asyncio
async def test_untap_toast_and_retracted_and_error():
    api = MagicMock()
    handler = _handler(api, **ON)
    api.club_event_react.return_value = {"on": False, "counts": [0] * 5, "status_code": 200}
    q = _query(club_cb("rx", 88, 1), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.rx.undone", emoji=PALETTE[1])
    api.club_event_react.return_value = {"error": "retracted", "status_code": 409}
    q = _query(club_cb("rx", 88, 1), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.rx.retracted")
    q.edit_message_reply_markup.assert_not_called()
    api.club_event_react.return_value = {"error": "x", "status_code": 500}
    q = _query(club_cb("rx", 88, 1), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.err")


@pytest.mark.asyncio
async def test_markup_edit_not_modified_is_harmless():
    api = MagicMock()
    api.club_event_react.return_value = {"on": True, "counts": [1, 0, 0, 0, 0]}
    handler = _handler(api, **ON)
    q = _query(club_cb("rx", 88, 0), _card_markup())
    q.edit_message_reply_markup.side_effect = Exception("Message is not modified")
    assert await handler._club_flow().on_callback(_update(q), MagicMock()) is True


@pytest.mark.asyncio
async def test_bad_index_and_flag_off_toast_without_api_call():
    api = MagicMock()
    handler = _handler(api, **ON)
    q = _query("cl:rx:88:9")
    await handler._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_react.assert_not_called()
    off = _handler(api, feature_clubs="on")
    q = _query(club_cb("rx", 88, 0))
    await off._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_react.assert_not_called()
    assert q.answer.call_args.args[0] == t("en", "club.rx.off")


@pytest.mark.asyncio
async def test_want_this_toast_only_no_new_message():
    api = MagicMock()
    api.club_event_want.return_value = {"saved": True, "status_code": 201}
    handler = _handler(api, **ON)
    q = _query(club_cb("wt", 88), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_want.assert_called_once_with(88)
    assert q.answer.call_args.args[0] == t("en", "club.td.added")
    q.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_want_this_lists_off_and_retracted():
    api = MagicMock()
    handler = _handler(api, feature_clubs="on", feature_club_reactions="on")
    q = _query(club_cb("wt", 88))
    await handler._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_want.assert_not_called()
    assert q.answer.call_args.args[0] == t("en", "club.td.off")
    on = _handler(api, **ON)
    api.club_event_want.return_value = {"error": "retracted", "status_code": 409}
    q = _query(club_cb("wt", 88))
    await on._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.rx.retracted")
    api.club_event_want.return_value = {"error": "not_enabled", "status_code": 404}
    q = _query(club_cb("wt", 88))
    await on._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.td.off")


@pytest.mark.asyncio
async def test_old_to_drink_button_unchanged_with_flag_off():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {"events": [{"id": 88}], "count": 1}
    api.event_to_list.return_value = {"id": 9, "status_code": 201}
    handler = _handler(api, feature_clubs="on", feature_lists="on")
    q = _query(club_cb("td", 88))
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.message.reply_text.call_args.args[0] == t("en", "club.td.added")
    # a stale Want this button with the flag off falls back to the same path
    q = _query(club_cb("wt", 88))
    await handler._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_want.assert_not_called()
    assert q.message.reply_text.call_args.args[0] == t("en", "club.td.added")


def test_copy_en_af_parity():
    en = {k for k in catalog_keys("en") if k.startswith("club.rx.")}
    assert en and en == {k for k in catalog_keys("af") if k.startswith("club.rx.")}


@pytest.mark.asyncio
async def test_held_text_gets_a_fresh_window():
    import time

    handler = _handler(MagicMock(), feature_clubs="on",
                       feature_club_comments="on", feature_club_threads="on")
    handler._club_comment_store = {}
    flow = handler._club_flow()
    flow._prompt_put("4242", event_id=88, club_id=3, prompt_id=5)
    handler._club_comment_store["4242"]["at"] = time.time() - 14 * 60
    prompt = flow._prompt_get("4242")
    flow._prompt_hold("4242", prompt, "hello")
    assert flow._prompt_get("4242")["held"] == "hello"
    assert handler._club_comment_store["4242"]["at"] > time.time() - 5


@pytest.mark.asyncio
async def test_close_prompt_uses_bot_from_button_message():
    handler = _handler(MagicMock(), **ON)
    flow = handler._club_flow()
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.message = None
    update.callback_query.message.get_bot.return_value = bot
    await flow._close_comment_prompt(update, "4242", 55)
    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=4242, message_id=55, reply_markup=None,
    )


@pytest.mark.asyncio
async def test_want_this_repeat_says_already_on_list():
    api = MagicMock()
    api.club_event_want.return_value = {"saved": True, "created": False, "status_code": 200}
    handler = _handler(api, **ON)
    q = _query(club_cb("wt", 88), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == "Already on your list"
    assert t("af", "club.rx.already") != t("en", "club.rx.already")
    api.club_event_want.return_value = {"saved": True, "created": True, "status_code": 201}
    q = _query(club_cb("wt", 88), _card_markup())
    await handler._club_flow().on_callback(_update(q), MagicMock())
    assert q.answer.call_args.args[0] == t("en", "club.td.added")


@pytest.mark.asyncio
async def test_stale_reaction_tap_flag_off_neutral_copy_and_palette_removed():
    api = MagicMock()
    off = _handler(api, feature_clubs="on")
    q = _query(club_cb("rx", 88, 0), _card_markup())
    await off._club_flow().on_callback(_update(q), MagicMock())
    api.club_event_react.assert_not_called()
    assert q.answer.call_args.args[0] == t("en", "club.rx.off")
    assert "yet" not in t("en", "club.rx.off")
    rows = q.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard
    assert [[b.text for b in r] for r in rows] == [["Say something", "Want this"]]
    # a message that is only the palette loses its keyboard entirely
    q = _query(club_cb("rx", 88, 0), InlineKeyboardMarkup([reaction_buttons(88)]))
    await off._club_flow().on_callback(_update(q), MagicMock())
    assert q.edit_message_reply_markup.call_args.kwargs["reply_markup"] is None
