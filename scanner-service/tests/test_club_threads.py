"""P1 threads: swipe-reply, 15-minute stored prompt, ping buttons, thread view."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.card_store import CardStore
from integrations.copy import catalog_keys, t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import (
    THREAD_PROMPT_TTL_SECONDS,
    club_cb,
)

ON = {
    "feature_clubs": "on",
    "feature_club_comments": "on",
    "feature_club_threads": "on",
}


def _handler(api, **flags):
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    handler.set_flags(flags if flags else ON)
    return handler


def _update(text="hello", chat_id=4242, reply_to=None):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    if reply_to is None:
        u.message.reply_to_message = None
    else:
        u.message.reply_to_message = MagicMock(message_id=reply_to)
        u.message.reply_to_message.from_user.is_bot = True
    return u


def _query(data):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


def _api():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "name": "Thursday reds", "is_last_used": True}],
    }
    api.club_feed.return_value = {
        "events": [{
            "id": 88, "kind": "opened", "actor_name": "Sam",
            "wine_name": "Merlot", "vintage": 2019,
        }],
        "count": 1,
    }
    api.add_club_comment.return_value = {"id": 1, "status_code": 201}
    return api


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


@pytest.mark.asyncio
async def test_swipe_reply_is_a_comment_without_say_something():
    api = _api()
    api.club_thread_resolve.return_value = {
        "event_id": 88, "club_id": 3, "retracted": False,
    }
    handler = _handler(api)
    update = _update("we had that in Stellenbosch", reply_to=555)
    assert await handler._club_flow().on_reply(update, MagicMock()) is True
    api.club_thread_resolve.assert_called_once_with("4242", 555)
    api.add_club_comment.assert_called_once_with(3, 88, "we had that in Stellenbosch")
    assert update.message.reply_text.call_args.args[0] == t("en", "club.comment.saved")


@pytest.mark.asyncio
async def test_swipe_reply_too_long_uses_existing_copy():
    api = _api()
    api.club_thread_resolve.return_value = {"event_id": 88, "club_id": 3}
    handler = _handler(api)
    update = _update("x" * 281, reply_to=555)
    assert await handler._club_flow().on_reply(update, MagicMock()) is True
    api.add_club_comment.assert_not_called()
    assert update.message.reply_text.call_args.args[0] == t("en", "club.comment.too_long")


@pytest.mark.asyncio
async def test_swipe_reply_to_unrelated_message_falls_through():
    api = _api()
    api.club_thread_resolve.return_value = {"error": "HTTP 404", "status_code": 404}
    handler = _handler(api)
    update = _update("hi", reply_to=1)
    assert await handler._club_flow().on_reply(update, MagicMock()) is False
    api.add_club_comment.assert_not_called()


@pytest.mark.asyncio
async def test_swipe_reply_flag_off_is_ignored():
    api = _api()
    handler = _handler(api, feature_clubs="on", feature_club_comments="on")
    update = _update("hi", reply_to=1)
    assert await handler._club_flow().on_reply(update, MagicMock()) is False
    api.club_thread_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_failure_never_raises():
    api = _api()
    api.club_thread_resolve.side_effect = RuntimeError("down")
    handler = _handler(api)
    assert await handler._club_flow().on_reply(_update("hi", reply_to=1), MagicMock()) is False


@pytest.mark.asyncio
async def test_say_something_prompt_is_stored_and_survives_restart():
    api = _api()
    store = CardStore(ttl=THREAD_PROMPT_TTL_SECONDS, prefix="whiney:clubcm:")
    handler = _handler(api)
    handler._club_comment_store = store
    update = _update()
    query = _query(club_cb("cm", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert t("en", "club.thread.prompt") in query.message.reply_text.call_args.args[0]
    assert "15 minutes" in t("en", "club.thread.prompt")
    assert store.get("4242")["event_id"] == 88
    # A brand-new handler (restart) sharing the store still takes the comment.
    handler2 = _handler(api)
    handler2._club_comment_store = store
    ok = _update("lovely")
    assert await handler2._club_flow().on_text(ok, MagicMock()) is True
    api.add_club_comment.assert_called_once_with(3, 88, "lovely")
    assert store.get("4242") is None


@pytest.mark.asyncio
async def test_prompt_expires_after_15_minutes_not_2():
    api = _api()
    store = CardStore(ttl=THREAD_PROMPT_TTL_SECONDS, prefix="whiney:clubcm:")
    handler = _handler(api)
    handler._club_comment_store = store
    flow = handler._club_flow()
    flow._prompt_put("4242", event_id=88, club_id=3, prompt_id=None)
    entry = store.get("4242")
    entry["at"] = time.time() - 5 * 60
    assert flow._prompt_get("4242") is not None  # 5 min later: still live
    entry["at"] = time.time() - THREAD_PROMPT_TTL_SECONDS - 5
    assert flow._prompt_get("4242") is None
    assert store.get("4242") is None
    assert await flow.on_text(_update("late"), MagicMock()) is False
    api.add_club_comment.assert_not_called()


@pytest.mark.asyncio
async def test_command_drops_stored_prompt():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    flow._prompt_put("4242", event_id=88, club_id=3, prompt_id=None)
    await handler._abandon_pending_states("4242", None)
    assert flow._prompt_get("4242") is None


@pytest.mark.asyncio
async def test_ping_reply_button_starts_same_prompt():
    api = _api()
    handler = _handler(api)
    update = _update()
    query = _query(club_cb("rp", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert handler._club_flow()._prompt_get("4242")["event_id"] == 88


@pytest.mark.asyncio
async def test_mute_button_toggles_label():
    api = _api()
    api.club_thread_mute.return_value = {"muted": True, "status_code": 200}
    handler = _handler(api)
    update = _update()
    query = _query(club_cb("mt", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    api.club_thread_mute.assert_called_once_with(88)
    markup = query.edit_message_reply_markup.call_args.kwargs["reply_markup"]
    assert _labels(markup) == ["Reply", "Unmute thread"]
    assert query.message.reply_text.call_args.args[0] == t("en", "club.thread.muted")
    api.club_thread_mute.return_value = {"muted": False, "status_code": 200}
    await handler._club_flow().on_callback(update, MagicMock())
    markup = query.edit_message_reply_markup.call_args.kwargs["reply_markup"]
    assert _labels(markup) == ["Reply", "Mute thread"]


def test_callbacks_fit_in_64_bytes():
    for data in (
        club_cb("rp", 999_999_999), club_cb("mt", 999_999_999),
        club_cb("th", 999_999_999, 999_999_999, 99),
    ):
        assert len(data.encode()) < CALLBACK_DATA_MAX


@pytest.mark.asyncio
async def test_feed_row_shows_thread_button_with_count():
    api = _api()
    api.club_feed.return_value = {
        "events": [{
            "id": 88, "kind": "opened", "actor_name": "Sam", "wine_name": "Merlot",
            "vintage": 2019, "comment_count": 4,
        }],
        "count": 1,
    }
    handler = _handler(api)
    update = _update()
    query = _query(club_cb("fd", 3, 0))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    assert "Thread (4)" in _labels(markup)


@pytest.mark.asyncio
async def test_thread_view_shows_every_comment_and_escapes():
    api = _api()
    comments = [
        {"display_name": "Lee", "text": f"note {i} <b>"} for i in range(12)
    ]
    api.club_comments.return_value = {"comments": comments, "count": 12}
    handler = _handler(api)
    update = _update()
    query = _query(club_cb("th", 3, 88, 0))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    for i in range(12):
        assert f"note {i} &lt;b&gt;" in text
    assert "<b>" not in text.replace("</b>", "").replace("&lt;b&gt;", "")


def test_thread_pages_split_under_telegram_limit():
    from integrations.telegram_clubs import ClubFlow

    comments = [{"display_name": "Pat", "text": "y" * 280} for _ in range(40)]
    pages = ClubFlow._thread_pages("head", comments)
    assert len(pages) > 1
    assert all(len(p) < 4096 for p in pages)
    assert sum(p.count("Pat:") for p in pages) == 40


def test_thread_copy_en_and_af():
    keys = {k for k in catalog_keys("en") if k.startswith("club.thread.")}
    assert keys and keys == {k for k in catalog_keys("af") if k.startswith("club.thread.")}
    assert t("af", "club.thread.muted") != t("en", "club.thread.muted")
    for key in ("club.thread.btn_reply", "club.thread.btn_mute", "club.thread.btn_unmute"):
        assert t("af", key) == t("en", key)  # buttons stay English
