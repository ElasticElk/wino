"""P3 scanner side: Ask the club button, stored prompt, submit, Undo, feed."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from integrations.card_store import CardStore
from integrations.copy import catalog_keys, t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import (
    THREAD_PROMPT_TTL_SECONDS,
    _event_action_row,
    _wine_line,
    build_home,
    club_cb,
    reaction_buttons,
)

ON = {
    "feature_clubs": "on",
    "feature_club_comments": "on",
    "feature_club_threads": "on",
    "feature_club_relay": "on",
}


def _handler(api, **flags):
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    handler.set_flags(flags if flags else ON)
    handler._club_comment_store = CardStore(
        ttl=THREAD_PROMPT_TTL_SECONDS, prefix="whiney:clubcm:",
    )
    return handler


def _update(text="hello", chat_id=4242):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    u.message.reply_to_message = None
    return u


def _query(data):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_text = AsyncMock(return_value=MagicMock(message_id=901))
    query.message.reply_markup = None
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


def _cb_update(query):
    u = MagicMock()
    u.effective_chat.id = 4242
    u.effective_user.language_code = "en"
    u.callback_query = query
    return u


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _api():
    api = MagicMock()
    api.get_club.return_value = {"id": 3, "name": "Thursday reds", "kind": "private"}
    api.club_ask.return_value = {
        "event_id": 55, "club_name": "Thursday reds", "undo_seconds": 60,
        "status_code": 201,
    }
    return api


def _home(flags=None, kind="private", **extra):
    payload = {
        "id": 3, "name": "Thursday reds", "member_count": 4, "kind": kind,
        "cellar_visibility": "none", "role": "member", "events": [],
    }
    payload.update(extra)
    return build_home(payload, "en", flags=flags)[1]


# -- button -------------------------------------------------------------------
def test_button_only_with_flag_and_not_in_global_clubs():
    on = _home({"feature_club_relay": True})
    assert "Ask the club" in _labels(on)
    button = next(
        b for row in on.inline_keyboard for b in row if b.text == "Ask the club"
    )
    assert button.callback_data == "cl:ak:3"
    assert "Ask the club" not in _labels(_home({}))
    assert "Ask the club" not in _labels(_home({"feature_club_relay": True}, kind="global"))


def test_flag_dict_needs_clubs_threads_and_relay():
    api = _api()
    assert _handler(api)._club_flow()._flags()["feature_club_relay"] is True
    for missing in ON:
        flags = {k: v for k, v in ON.items() if k != missing}
        assert _handler(api, **flags)._club_flow()._flags()["feature_club_relay"] is False


def test_callbacks_fit_telegram_limit():
    for data in (club_cb("ak", 123456789), club_cb("au", 123456789)):
        assert len(data.encode()) <= CALLBACK_DATA_MAX


# -- prompt -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tap_opens_a_stored_prompt_with_cancel():
    api = _api()
    handler = _handler(api)
    query = _query("cl:ak:3")
    assert await handler._club_flow().on_callback(_cb_update(query), MagicMock()) is True
    call = query.message.reply_text.call_args
    assert call.args[0] == t("en", "club.ask.prompt")
    assert call.args[0].startswith("What would you like to ask the club?")
    assert _labels(call.kwargs["reply_markup"]) == ["Cancel"] or "Cancel" in _labels(
        call.kwargs["reply_markup"]
    ) or any("ancel" in x for x in _labels(call.kwargs["reply_markup"]))
    entry = handler._club_comment_store.get("4242")
    assert entry["kind"] == "ask" and entry["club_id"] == 3 and entry["prompt_id"] == 901
    # A fresh handler on the same store (restart) still takes the text.
    handler2 = _handler(api)
    handler2._club_comment_store = handler._club_comment_store
    update = _update("Anyone tried the Chenin?")
    assert await handler2._club_flow().on_text(update, MagicMock()) is True
    api.club_ask.assert_called_once_with(3, "Anyone tried the Chenin?")
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_prompt_expires_after_fifteen_minutes():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    store = handler._club_comment_store
    entry = store.get("4242")
    entry["at"] = time.time() - 16 * 60
    store["4242"] = entry
    assert await flow.on_text(_update("late"), MagicMock()) is False
    api.club_ask.assert_not_called()


@pytest.mark.asyncio
async def test_commands_and_labels_abandon_the_prompt():
    handler = _handler(_api())
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    flow.drop_comment_prompt("4242")  # what a command / menu label does
    assert handler._club_comment_store.get("4242") is None
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    await handler._nav().drop_pending("4242", bot)
    assert handler._club_comment_store.get("4242") is None
    assert bot.edit_message_reply_markup.call_args.kwargs["message_id"] == 901


@pytest.mark.asyncio
async def test_cancel_button_clears_the_ask_prompt():
    handler = _handler(_api())
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=7)
    nav = handler._nav()
    nav.show_screen = AsyncMock()
    await nav.cancel(_query("menu"), "4242")
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_blank_or_too_long_keeps_prompt_and_reuses_copy():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    for text in ("   ", "x" * 281):
        update = _update(text)
        assert await flow.on_text(update, MagicMock()) is True
        assert update.message.reply_text.call_args.args[0] == t("en", "club.comment.too_long")
    api.club_ask.assert_not_called()
    assert handler._club_comment_store.get("4242")["kind"] == "ask"
    update = _update("x" * 280)
    await flow.on_text(update, MagicMock())
    api.club_ask.assert_called_once()


# -- submit -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submit_confirms_with_undo_button():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    update = _update("What about <b>Port</b>?")
    await flow.on_text(update, MagicMock())
    call = update.message.reply_text.call_args
    assert call.args[0] == "Sent to Thursday reds."
    assert _labels(call.kwargs["reply_markup"]) == ["Undo"]
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "cl:au:55"


@pytest.mark.asyncio
async def test_club_name_is_escaped_in_confirmation():
    api = _api()
    api.club_ask.return_value = {
        "event_id": 55, "club_name": "<i>Reds</i>", "status_code": 201,
    }
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    update = _update("Hi")
    await flow.on_text(update, MagicMock())
    assert update.message.reply_text.call_args.args[0] == "Sent to &lt;i&gt;Reds&lt;/i&gt;."


@pytest.mark.asyncio
@pytest.mark.parametrize("result, key, kwargs", [
    ({"error": "duplicate", "status_code": 409}, "club.ask.dup", {}),
    ({"error": "rate_limited", "retry_minutes": 12, "status_code": 429}, "club.ask.limit", {"n": 12}),
    ({"error": "global", "status_code": 400}, "club.ask.global", {}),
    ({"error": "not_enabled", "status_code": 404}, "club.ask.off", {}),
    ({"error": "boom", "status_code": 500}, "club.err", {}),
])
async def test_submit_errors_have_friendly_copy(result, key, kwargs):
    api = _api()
    api.club_ask.return_value = result
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    update = _update("Question")
    await flow.on_text(update, MagicMock())
    assert update.message.reply_text.call_args.args[0] == t("en", key, **kwargs)
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_afrikaans_prompt_and_confirmation():
    api = _api()
    handler = _handler(api)
    handler.set_language("4242", "af")
    query = _query("cl:ak:3")
    await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    assert query.message.reply_text.call_args.args[0] == t("af", "club.ask.prompt")
    assert t("af", "club.ask.prompt") != t("en", "club.ask.prompt")


# -- undo ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_undo_outcomes():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    cases = [
        ({"undone": True, "status_code": 200}, "club.ask.undone"),
        ({"error": "already_out", "status_code": 409}, "club.ask.out"),
        ({"error": "not_found", "status_code": 404}, "club.ask.gone"),
        ({"error": "boom", "status_code": 500}, "club.err"),
    ]
    for result, key in cases:
        api.club_ask_undo.return_value = result
        query = _query("cl:au:55")
        await flow.on_callback(_cb_update(query), MagicMock())
        assert query.edit_message_text.call_args.args[0] == t("en", key)
        assert "reply_markup" not in query.edit_message_text.call_args.kwargs
    api.club_ask_undo.assert_called_with(55)


# -- global clubs -------------------------------------------------------------
@pytest.mark.asyncio
async def test_tap_on_global_club_is_refused():
    api = _api()
    api.get_club.return_value = {"id": 3, "name": "Staff", "kind": "global"}
    handler = _handler(api)
    query = _query("cl:ak:3")
    await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    assert query.message.reply_text.call_args.args[0] == t("en", "club.ask.global")
    assert handler._club_comment_store.get("4242") is None


# -- flags off ----------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["cl:ak:3", "cl:au:55"])
async def test_stale_taps_get_a_toast_when_flag_off(data):
    api = _api()
    handler = _handler(api, feature_clubs="on", feature_club_comments="on",
                       feature_club_threads="on")
    query = _query(data)
    assert await handler._club_flow().on_callback(_cb_update(query), MagicMock()) is True
    assert query.answer.call_args.args[0] == t("en", "club.ask.off")
    api.club_ask_undo.assert_not_called()
    api.get_club.assert_not_called()
    query.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_pending_prompt_when_flag_turned_off_says_so():
    api = _api()
    handler = _handler(api, feature_clubs="on", feature_club_comments="on",
                       feature_club_threads="on")
    flow = handler._club_flow()
    flow._prompt_put("4242", kind="ask", club_id=3, prompt_id=901)
    update = _update("late ask")
    assert await flow.on_text(update, MagicMock()) is True
    assert update.message.reply_text.call_args.args[0] == t("en", "club.ask.off")
    api.club_ask.assert_not_called()
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_comment_prompt_unchanged_by_the_relay():
    api = _api()
    api.add_club_comment.return_value = {"id": 1, "status_code": 201}
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", event_id=88, club_id=3, prompt_id=900, wine="2019 Merlot")
    update = _update("nice")
    assert await flow.on_text(update, MagicMock()) is True
    api.add_club_comment.assert_called_once_with(3, 88, "nice")
    api.club_ask.assert_not_called()


# -- feed ---------------------------------------------------------------------
ASK_EVENT = {
    "id": 55, "kind": "ask", "actor_name": "Riley",
    "payload": {"text": "Port & <b>cheese</b>?"}, "wine_name": "",
}


def test_ask_line_is_escaped_and_not_a_bottle():
    line = _wine_line(ASK_EVENT, "en")
    assert line == "\U0001F4AC Riley asks the club: Port &amp; &lt;b&gt;cheese&lt;/b&gt;?"
    assert "opened" not in line
    assert "vra die klub" in _wine_line(ASK_EVENT, "af")


def test_ask_event_has_reply_but_no_bottle_buttons():
    row = _event_action_row(ASK_EVENT, "en", {"feature_lists": True, "feature_club_comments": True})
    assert [b.callback_data for b in row] == ["cl:rp:55"]
    assert _event_action_row(ASK_EVENT, "en", {"feature_lists": True}) == []
    assert _event_action_row({**ASK_EVENT, "retracted": True}, "en", {"feature_club_comments": True}) == []


def test_home_does_not_offer_comment_shortcut_on_an_ask():
    markup = _home({"feature_club_comments": True}, events=[ASK_EVENT])
    assert not any(
        (b.callback_data or "").startswith("cl:cm:") for row in markup.inline_keyboard for b in row
    )


@pytest.mark.asyncio
async def test_feed_shows_ask_with_reply_and_thread():
    api = _api()
    api.club_feed.return_value = {
        "events": [{**ASK_EVENT, "comment_count": 2}], "count": 1,
    }
    handler = _handler(api)
    handler.flag_on("feature_lists")
    update = _update("")
    await handler._club_flow()._show_feed(update, club_id=3)
    call = update.message.reply_text.call_args
    assert "Riley asks the club" in call.args[0]
    data = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "cl:rp:55" in data and "cl:th:3:55:0" in data
    assert not any(d.startswith(("cl:td:", "cl:wt:")) for d in data)


def test_reaction_palette_reused_on_asks():
    row = reaction_buttons(55)
    assert row[0].callback_data == "cl:rx:55:0"


def test_copy_en_af_parity():
    en = {k for k in catalog_keys("en") if k.startswith("club.ask.")}
    assert en and en == {k for k in catalog_keys("af") if k.startswith("club.ask.")}
    assert t("en", "club.ask.btn") == "Ask the club" == t("af", "club.ask.btn")
