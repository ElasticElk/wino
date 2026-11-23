"""P5 scanner side: tasting-note offer after the vote, prompt, submit, feed
detail Edit note, flags off, and the summary swipe-reply carry-over."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.card_store import CardStore
from integrations.copy import catalog_keys, t
from integrations.telegram_bot import (
    CALLBACK_DATA_MAX,
    TelegramBotHandler,
    _cb,
    build_drank_view,
)
from integrations.telegram_clubs import THREAD_PROMPT_TTL_SECONDS, club_cb

ON = {"feature_clubs": "on", "feature_tasting_notes": "on"}


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


def _update(text="hello", chat_id=4242, reply_to=None):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    u.message.reply_to_message = reply_to
    return u


def _query(data):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_text = AsyncMock(return_value=MagicMock(message_id=901))
    query.message.delete = AsyncMock()
    query.message.reply_markup = None
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


def _api(offer=True):
    api = MagicMock()
    api.club_note_offer.return_value = {
        "offer": offer, "wine": "2019 Merlot", "status_code": 200,
    }
    api.club_note.return_value = {
        "saved": True, "count": 1, "clubs": ["Thursday <reds>"], "status_code": 200,
    }
    return api


async def _vote(handler, item_id=99):
    body, markup = build_drank_view("Chenin", 0, item_id)
    query = AsyncMock()
    query.data = _cb("vt", item_id=item_id, original="up")
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_markup = markup
    query.message.text_html = body
    query.message.text = body
    query.message.set_reaction = AsyncMock()
    query.message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_chat.id = 4242
    update.effective_user.language_code = "en"
    update.callback_query = query
    await handler._handle_callback(update, MagicMock())
    return query


# -- offer ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_offer_follows_the_vote_with_add_and_skip():
    api = _api()
    handler = _handler(api)
    query = await _vote(handler)
    api.vote_item.assert_called_once_with(99, "up")
    api.club_note_offer.assert_called_once_with(99)
    call = query.message.reply_text.call_args
    assert call.args[0] == "Add a tasting note for the 2019 Merlot?"
    assert call.kwargs["reply_to_message_id"] == 7
    assert _labels(call.kwargs["reply_markup"]) == ["Add a note", "Skip"]
    datas = [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert datas == ["cl:nt:a:99", "cl:nt:s:99"]
    assert all(len(d.encode()) <= CALLBACK_DATA_MAX for d in datas)
    # Changing the vote does not ask again.
    again = await _vote(handler)
    assert again.message.reply_text.call_count == 0


@pytest.mark.asyncio
async def test_no_offer_when_server_says_no_or_flag_off():
    api = _api(offer=False)
    handler = _handler(api)
    query = await _vote(handler)
    assert query.message.reply_text.call_count == 0
    api = _api()
    off = _handler(api, feature_clubs="on")  # notes flag off
    query = await _vote(off)
    api.club_note_offer.assert_not_called()
    assert query.message.reply_text.call_count == 0
    # Notes on but clubs off: nothing either.
    api = _api()
    query = await _vote(_handler(api, feature_tasting_notes="on"))
    api.club_note_offer.assert_not_called()


@pytest.mark.asyncio
async def test_offer_failure_never_breaks_the_vote():
    api = _api()
    api.club_note_offer.side_effect = RuntimeError("down")
    query = await _vote(_handler(api))
    api.vote_item.assert_called_once()
    assert query.message.reply_text.call_count == 0


@pytest.mark.asyncio
async def test_skip_is_one_tap_and_removes_the_offer():
    handler = _handler(_api())
    query = _query("cl:nt:s:99")
    assert await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    query.message.delete.assert_awaited()
    assert handler._club_comment_store.get("4242") is None


# -- prompt and submit ----------------------------------------------------
@pytest.mark.asyncio
async def test_add_opens_a_stored_prompt_and_next_message_is_the_note():
    api = _api()
    handler = _handler(api)
    query = _query("cl:nt:a:99")
    await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    call = query.message.reply_text.call_args
    assert call.args[0] == "What did you think? A line or two."
    entry = handler._club_comment_store.get("4242")
    assert entry["kind"] == "note" and entry["item_id"] == 99 and entry["prompt_id"] == 901
    # A restarted handler on the same store still takes the text.
    handler2 = _handler(api)
    handler2._club_comment_store = handler._club_comment_store
    update = _update("Plums & pepper")
    assert await handler2._club_flow().on_text(update, MagicMock()) is True
    api.club_note.assert_called_once()
    assert api.club_note.call_args.args[0] == "Plums & pepper"
    assert api.club_note.call_args.kwargs == {"item_id": 99}
    assert update.message.reply_text.call_args.args[0] == (
        "Saved. Thursday &lt;reds&gt; will see it."
    )
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_over_long_note_keeps_the_prompt_open():
    api = _api()
    handler = _handler(api)
    await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    update = _update("x" * 281)
    await handler._club_flow().on_text(update, MagicMock())
    assert update.message.reply_text.call_args.args[0] == t("en", "club.comment.too_long")
    api.club_note.assert_not_called()
    assert handler._club_comment_store.get("4242")["kind"] == "note"
    ok = _update("x" * 280)
    await handler._club_flow().on_text(ok, MagicMock())
    api.club_note.assert_called_once()


@pytest.mark.asyncio
async def test_swipe_reply_to_the_prompt_is_the_note():
    api = _api()
    handler = _handler(api)
    await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    reply = MagicMock(message_id=901)
    reply.from_user.is_bot = True
    update = _update("Plums", reply_to=reply)
    assert await handler._club_flow().on_reply(update, MagicMock()) is True
    assert api.club_note.call_args.args[0] == "Plums"
    api.club_thread_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_prompt_expires_after_15_minutes_and_commands_abandon_it():
    handler = _handler(_api())
    flow = handler._club_flow()
    await flow.on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    entry = handler._club_comment_store.get("4242")
    entry["at"] -= THREAD_PROMPT_TTL_SECONDS + 5
    handler._club_comment_store["4242"] = entry
    assert flow._prompt_get("4242") is None
    await flow.on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    assert flow.thread_prompt("4242")["kind"] == "note"
    flow.drop_comment_prompt("4242")  # what a command does
    assert flow.thread_prompt("4242") is None


@pytest.mark.asyncio
async def test_save_errors_are_answered_not_raised():
    for result, key in (
        ({"error": "no_event", "status_code": 409}, "club.note.gone"),
        ({"error": "not_enabled", "status_code": 404}, "club.note.off"),
        ({"error": "boom", "status_code": 500}, "club.err"),
    ):
        api = _api()
        api.club_note.return_value = result
        handler = _handler(api)
        await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
        update = _update("Plums")
        await handler._club_flow().on_text(update, MagicMock())
        assert update.message.reply_text.call_args.args[0] == t("en", key)


# -- feed detail ------------------------------------------------------------
@pytest.mark.asyncio
async def test_feed_shows_the_note_and_edit_button_on_own_event_only():
    api = _api()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {
        "count": 2, "events": [
            {"id": 11, "kind": "opened", "actor_name": "Riley", "wine_name": "Syrah",
             "vintage": 2019, "vote": "up", "note": "Plums <b>", "mine": True},
            {"id": 12, "kind": "opened", "actor_name": "Sam", "wine_name": "Chenin",
             "vintage": 2020, "vote": "", "note": "Honey", "mine": False},
        ],
    }
    handler = _handler(api)
    query = _query("cl:fd:3:0")
    update = _cb_update(query)
    await handler._club_flow()._show_feed(update, 3, 0, query=query)
    call = query.edit_message_text.call_args
    text = call.args[0] if call.args else call.kwargs["text"]
    assert "“Plums &lt;b&gt;”" in text and "“Honey”" in text
    markup = call.kwargs["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "cl:nt:e:11" in datas and "cl:nt:e:12" not in datas
    assert "Edit note" in _labels(markup)


@pytest.mark.asyncio
async def test_feed_has_no_note_ui_with_the_flag_off():
    api = _api()
    api.club_feed.return_value = {
        "count": 1, "events": [
            {"id": 11, "kind": "opened", "actor_name": "Riley", "wine_name": "Syrah",
             "vintage": 2019, "vote": "up", "mine": True},
        ],
    }
    handler = _handler(api, feature_clubs="on")
    query = _query("cl:fd:3:0")
    await handler._club_flow()._show_feed(_cb_update(query), 3, 0, query=query)
    markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    assert not any(d.startswith("cl:nt") for d in (
        b.callback_data for row in markup.inline_keyboard for b in row
    ))


@pytest.mark.asyncio
async def test_edit_note_prompt_shows_current_note_and_remove():
    api = _api()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {
        "events": [{"id": 11, "kind": "opened", "note": "Plums", "mine": True}],
    }
    handler = _handler(api)
    query = _query("cl:nt:e:11")
    await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    call = query.message.reply_text.call_args
    assert "Plums" in call.args[0]
    assert "Remove note" in _labels(call.kwargs["reply_markup"])
    entry = handler._club_comment_store.get("4242")
    assert entry["kind"] == "note" and entry["event_id"] == 11
    update = _update("Plums and smoke")
    await handler._club_flow().on_text(update, MagicMock())
    assert api.club_note.call_args.args[0] == "Plums and smoke"
    assert api.club_note.call_args.kwargs == {"event_id": 11}
    # Remove note
    rq = _query("cl:nt:r:11")
    await handler._club_flow().on_callback(_cb_update(rq), MagicMock())
    assert api.club_note.call_args.args[0] == ""
    assert rq.message.reply_text.call_args.args[0] == t("en", "club.note.removed")


@pytest.mark.asyncio
async def test_thread_head_leads_with_the_note():
    api = _api()
    event = {"id": 11, "kind": "opened", "actor_name": "Riley", "wine_name": "Syrah",
             "vintage": 2019, "vote": "", "note": "Plums", "mine": True}
    api.club_feed.return_value = {"events": [event]}
    api.club_comments.return_value = {"comments": []}
    handler = _handler(api, **ON, feature_club_comments="on", feature_club_threads="on")
    query = _query("cl:th:3:11:0")
    await handler._club_flow()._show_thread(_cb_update(query), 3, 11, 0, query)
    call = query.edit_message_text.call_args
    text = call.args[0] if call.args else call.kwargs["text"]
    lines = text.split("\n")
    assert lines[1] == "“Plums”"
    assert "Edit note" in _labels(call.kwargs["reply_markup"])


# -- flags off: stale button ----------------------------------------------
@pytest.mark.asyncio
async def test_stale_button_gets_a_toast_when_the_flag_is_off():
    api = _api()
    handler = _handler(api, feature_clubs="on")
    query = _query("cl:nt:a:99")
    assert await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    assert query.answer.call_args.args[0] == t("en", "club.note.off")
    query.message.reply_text.assert_not_called()
    assert handler._club_comment_store.get("4242") is None


@pytest.mark.asyncio
async def test_note_prompt_is_dropped_when_the_flag_goes_off():
    api = _api()
    handler = _handler(api)
    await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    handler.set_flags({"feature_clubs": "on"})
    update = _update("Plums")
    # With notes off the stored prompt is not a live "next message" slot.
    assert handler._club_flow().thread_prompt("4242") is None
    await handler._club_flow().on_text(update, MagicMock())
    api.club_note.assert_not_called()


# -- copy -----------------------------------------------------------------
def test_copy_is_in_both_locales_and_buttons_are_english():
    keys = [k for k in catalog_keys("en") if k.startswith("club.note.")]
    assert len(keys) >= 10
    for key in keys:
        assert t("af", key) and not t("af", key).startswith("club.note.")
    for key in ("club.note.offer", "club.note.prompt", "club.note.saved"):
        assert t("af", key) != t("en", key)
    for key in ("btn_add", "btn_skip", "btn_edit", "btn_remove"):
        assert t("af", f"club.note.{key}") == t("en", f"club.note.{key}")
    assert club_cb("nt", "e", 123456789) == "cl:nt:e:123456789"


# -- carry-over (b): summary swipe-reply with threads off -------------------
@pytest.mark.asyncio
async def test_summary_swipe_reply_is_answered_with_threads_off():
    api = _api()
    api.club_thread_resolve.return_value = {"summary": True, "status_code": 200}
    handler = _handler(api, feature_clubs="on", feature_club_notify="on")
    reply = MagicMock(message_id=55)
    reply.from_user.is_bot = True
    update = _update("thanks", reply_to=reply)
    assert await handler._club_flow().on_reply(update, MagicMock()) is True
    assert update.message.reply_text.call_args.args[0] == t(
        "en", "club.notify.summary_reply",
    )


@pytest.mark.asyncio
async def test_reply_to_an_event_still_needs_threads():
    api = _api()
    api.club_thread_resolve.return_value = {"error": "not_enabled", "status_code": 404}
    handler = _handler(api, feature_clubs="on")
    reply = MagicMock(message_id=55)
    reply.from_user.is_bot = True
    update = _update("hi", reply_to=reply)
    assert await handler._club_flow().on_reply(update, MagicMock()) is False
    # Network trouble with threads off is not our business either.
    api.club_thread_resolve.return_value = {"error": "HTTP 500", "status_code": 500}
    assert await handler._club_flow().on_reply(update, MagicMock()) is False
    update.message.reply_text.assert_not_called()


# -- review fixes ------------------------------------------------------------
@pytest.mark.asyncio
async def test_blank_note_says_write_something_and_saved_names_no_club_when_none():
    api = _api()
    handler = _handler(api)
    await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    update = _update("   ")
    await handler._club_flow().on_text(update, MagicMock())
    assert update.message.reply_text.call_args.args[0] == "Write something first."
    api.club_note.assert_not_called()
    api.club_note.return_value = {"saved": True, "count": 0, "clubs": []}
    await handler._club_flow().on_text(_update("fine"), MagicMock())
    api.club_note.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limited_save_is_friendly():
    api = _api()
    api.club_note.return_value = {"error": "rate_limited", "retry_minutes": 10}
    handler = _handler(api)
    await handler._club_flow().on_callback(_cb_update(_query("cl:nt:a:99")), MagicMock())
    update = _update("Plums")
    await handler._club_flow().on_text(update, MagicMock())
    assert "10 min" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_vote_row_carries_add_note_and_keeps_the_card_buttons():
    from integrations.telegram_bot import vote_keyboard_row

    on = _handler(_api())
    assert on is not None
    row = vote_keyboard_row(99)
    assert [b.callback_data for b in row][-1] == "cl:nt:v:99"
    api = _api()
    handler = _handler(api)
    query = _query("cl:nt:v:99")
    await handler._club_flow().on_callback(_cb_update(query), MagicMock())
    query.edit_message_reply_markup.assert_not_called()  # card keeps its buttons
    assert handler._club_comment_store.get("4242")["item_id"] == 99
    api.club_note_offer.return_value = {"offer": False}
    q2 = _query("cl:nt:v:98")
    await handler._club_flow().on_callback(_cb_update(q2), MagicMock())
    assert q2.message.reply_text.call_args.args[0] == t("en", "club.note.no_club")
    _handler(_api(), feature_clubs="on")  # notes off: no button
    assert len(vote_keyboard_row(99)) == 3
