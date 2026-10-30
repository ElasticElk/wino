"""Typed 'drank this' / 'drank one of my X' must remove stock, not search."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import (
    TelegramBotHandler,
    TypedStock,
    _cb,
    match_in_stock_wines,
    match_storage_name,
    parse_typed_stock,
    similar_in_stock_wines,
)


BAROLO = {
    "id": 11, "name": "Barolo Cannubi 2018", "producer": "Brezza",
    "country": "IT", "region": "Piedmont",
    "stock_count": 2, "items": [{"storage": "Fridge", "item_id": 4}],
}
BAROLO_B = {
    "id": 12, "name": "Barolo Brunate 2016", "producer": "Vietti",
    "country": "IT", "region": "Piedmont",
    "stock_count": 1, "items": [{"storage": "Cellar", "item_id": 5}],
}
BAROLO_C = {
    "id": 5, "name": "Barolo Terre del Barolo 2021", "producer": "Terre del Barolo",
    "country": "IT", "region": "Piedmont",
    "stock_count": 2, "items": [{"storage": "Big Fridge", "item_id": 6}],
}
CHIANTI = {
    "id": 20, "name": "Chianti Classico Riserva 2020", "producer": "Antinori",
    "country": "IT", "region": "Tuscany",
    "stock_count": 1, "items": [{"storage": "Fridge", "item_id": 8}],
}
SASSICAIA = {
    "id": 21, "name": "Sassicaia 2019", "producer": "Tenuta San Guido",
    "country": "IT", "region": "Tuscany",
    "stock_count": 1, "items": [{"storage": "Cellar", "item_id": 9}],
}
CHENIN = {
    "id": 9, "name": "Old Vine Chenin Blanc", "producer": "Bosman",
    "stock_count": 1,
    "items": [{"storage": "Fridge", "item_id": 3}],
}
SIGLATI = {
    "id": 30, "name": "I Siglati 2019", "producer": "Casa Sant'Orsola",
    "country": "IT", "region": "Piedmont",
    "appellation": "DOCG Barolo",
    "stock_count": 2,
    "items": [{"storage": "Cellar", "item_id": 10, "drawer": 2}],
}

ITALIANS = [BAROLO, BAROLO_B, BAROLO_C, CHIANTI, SASSICAIA]
HOUSEHOLD = [BAROLO_C, SIGLATI, CHENIN]


def _stock_for(wines):
    def _call(storage_name=""):
        if not storage_name:
            return list(wines)
        return [
            wine for wine in wines
            if any(
                (item.get("storage") or "") == storage_name
                for item in wine.get("items") or []
            )
        ]
    return _call


def _handler(api):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock):
        api.list_all_wines.return_value = []
    if isinstance(getattr(api.wines_in_stock, "return_value", None), MagicMock):
        api.wines_in_stock.return_value = []
    if isinstance(getattr(api, "web_url", None), MagicMock) or not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
    if isinstance(getattr(api.list_storages, "return_value", None), MagicMock):
        api.list_storages.return_value = [
            {"id": 1, "name": "Big Fridge"},
            {"id": 2, "name": "Fridge"},
            {"id": 3, "name": "Cellar"},
        ]
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


def _text_update(text):
    msg = AsyncMock()
    msg.text = text
    msg.chat_id = 4242
    msg.message_id = 11
    update = MagicMock()
    update.message = msg
    update.effective_chat.id = 4242
    return update


def test_parse_typed_stock_this_and_of():
    assert parse_typed_stock("Drank this") == TypedStock("this")
    assert parse_typed_stock("drank it") == TypedStock("this")
    assert parse_typed_stock("remove one") == TypedStock("this")
    assert parse_typed_stock("gedrink") == TypedStock("this")
    assert parse_typed_stock("verwyder een") == TypedStock("this")
    assert parse_typed_stock("I drank one of my barolos") == TypedStock("of", "barolos")
    assert parse_typed_stock("Remove one of my barolos") == TypedStock("of", "barolos")
    assert parse_typed_stock("Ek het een van my barolos gedrink") == TypedStock("of", "barolos")
    assert parse_typed_stock("Verwyder een van my barolos") == TypedStock("of", "barolos")
    assert parse_typed_stock(
        "Remove one of my barolos from my big fridge",
    ) == TypedStock("of", "barolos", "big fridge")
    assert parse_typed_stock(
        "ek het een van my barolos uit die yskas gedrink",
    ) == TypedStock("of", "barolos", "yskas")
    assert parse_typed_stock("I drank a bottle of barolo") == TypedStock("of", "barolo")
    assert parse_typed_stock("I drank some barolo") == TypedStock("of", "barolo")
    assert parse_typed_stock("I drank my last barolo") == TypedStock("of", "barolo")
    assert parse_typed_stock("what should I drink") is None
    assert parse_typed_stock("add") is None
    assert parse_typed_stock("please take a barolo out of the big fridge") is None


def test_match_storage_name_unique_or_ignored():
    names = ["Big Fridge", "Fridge", "Cellar"]
    assert match_storage_name("big fridge", names) == "Big Fridge"
    assert match_storage_name("yskas", names) == ""
    assert match_storage_name("garage", names) == ""


def test_match_in_stock_wines_stems_plurals():
    hits = match_in_stock_wines([BAROLO, BAROLO_B, CHENIN], "barolos")
    assert [w["id"] for w in hits] == [11, 12]
    assert match_in_stock_wines([CHENIN], "barolo") == []


def test_match_in_stock_wines_appellation_only():
    hits = match_in_stock_wines([SIGLATI, CHENIN], "barolos")
    assert [w["id"] for w in hits] == [30]
    assert match_in_stock_wines([CHENIN], "barolo") == []


def test_match_in_stock_wines_wine_type():
    red = {
        "id": 40, "name": "House Blend 2020", "producer": "X", "wine_type": "RE",
        "stock_count": 1, "items": [{"storage": "Cellar"}],
    }
    white = {
        "id": 41, "name": "House Blend 2021", "producer": "X", "wine_type": "WH",
        "stock_count": 1, "items": [{"storage": "Cellar"}],
    }
    assert [w["id"] for w in match_in_stock_wines([red, white], "reds")] == [40]


@pytest.mark.asyncio
async def test_typed_drank_this_after_a_match_card_removes_stock():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [CHENIN]
    api.get_wine_detail.return_value = CHENIN
    api.remove_stock.return_value = {
        "wine_name": "Old Vine Chenin Blanc", "stock_count": 0, "item_id": 3,
    }
    handler = _handler(api)
    handler.pending_store["_label_1"] = {
        "grok_merged": {
            "name": "Old Vine Chenin Blanc", "producer": "Bosman", "vintage": 2020,
        },
        "time": time.time(),
        "household_id": 1,
        "barcode": "_label_1",
        "photo_data": b"jpeg",
    }
    update = _text_update("Drank this")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_called_once()
    assert api.remove_stock.call_args.args[0] == 9
    api.search_wines.assert_not_called()
    text = " ".join(
        str(c.args[0]) for c in update.message.reply_text.call_args_list if c.args
    )
    assert "Drank" in text
    assert "Old Vine Chenin" in text


def _spy_fallthrough(handler):
    """Country browse, search listings and detail cards must not run."""
    handler._do_search = AsyncMock(wraps=handler._do_search)
    handler._cmd_stock = AsyncMock(wraps=handler._cmd_stock)
    handler._build_detail_card = MagicMock(wraps=handler._build_detail_card)
    handler._router = MagicMock()
    handler._router.handle_text = AsyncMock(return_value=True)
    return handler


def _pick_labels(update):
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    return [b.text for row in markup.inline_keyboard for b in row]


def _pick_buttons(update):
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    return [b for row in markup.inline_keyboard for b in row]


async def _assert_barolo_picks_then_confirm(handler, api, phrase):
    api.wines_in_stock.return_value = list(ITALIANS)
    api.search_wines.return_value = list(ITALIANS)
    api.get_wine_detail.return_value = BAROLO
    handler = _spy_fallthrough(handler)
    update = _text_update(phrase)
    await handler._unknown_text(update, MagicMock())

    api.remove_stock.assert_not_called()
    api.search_wines.assert_not_called()
    handler._do_search.assert_not_called()
    handler._cmd_stock.assert_not_called()
    handler._build_detail_card.assert_not_called()
    handler._router.handle_text.assert_not_called()

    text = update.message.reply_text.call_args.args[0]
    assert "Which one" in text
    labels = _pick_labels(update)
    assert any("Cannubi" in t for t in labels)
    assert any("Brunate" in t for t in labels)
    assert any("Terre del Barolo" in t for t in labels)
    assert any("Fridge" in t or "Cellar" in t for t in labels)
    assert not any("Chianti" in t for t in labels)
    assert not any("Sassicaia" in t for t in labels)
    buttons = _pick_buttons(update)
    assert all("a:dc," in b.callback_data or b.text == "❌ Cancel" for b in buttons)
    picked = next(b for b in buttons if "w:11" in b.callback_data)

    query = AsyncMock()
    query.data = picked.callback_data
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    tap = MagicMock()
    tap.callback_query = query
    tap.effective_chat.id = 4242
    await handler._handle_callback(tap, MagicMock())
    api.remove_stock.assert_not_called()
    confirm = query.edit_message_text.call_args.args[0]
    assert "Remove 1" in confirm
    assert "Cannubi" in confirm
    confirm_labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("drank it" in t.lower() for t in confirm_labels)


@pytest.mark.asyncio
async def test_i_drank_one_of_my_barolos_picks_then_confirms():
    api = MagicMock()
    api.household_id = 1
    await _assert_barolo_picks_then_confirm(
        _handler(api), api, "I drank one of my barolos",
    )


@pytest.mark.asyncio
async def test_remove_one_of_my_barolos_picks_then_confirms():
    api = MagicMock()
    api.household_id = 1
    await _assert_barolo_picks_then_confirm(
        _handler(api), api, "Remove one of my barolos",
    )


@pytest.mark.asyncio
async def test_do_search_does_not_list_drank_one_of_my_barolos():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.return_value = list(ITALIANS)
    api.search_wines.return_value = list(ITALIANS)
    handler = _handler(api)
    update = _text_update("I drank one of my barolos")
    await handler._do_search(update, "I drank one of my barolos")
    api.search_wines.assert_not_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Which one" in text
    labels = _pick_labels(update)
    assert any("Cannubi" in t for t in labels)
    assert not any("Chianti" in t for t in labels)


@pytest.mark.asyncio
async def test_typed_remove_one_of_my_unique_wine_confirms_first():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.return_value = [CHENIN]
    handler = _handler(api)
    update = _text_update("Remove one of my chenins")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Remove 1" in text
    assert "Chenin" in text
    labels = [
        b.text
        for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("drank it" in t.lower() for t in labels)


@pytest.mark.asyncio
async def test_confirm_yes_removes_from_the_only_storage():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = CHENIN
    api.remove_stock.return_value = {
        "wine_name": "Old Vine Chenin Blanc", "stock_count": 0, "item_id": 3,
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("dy", wine_id=9)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    await handler._handle_callback(update, MagicMock())
    api.remove_stock.assert_called_once()
    assert api.remove_stock.call_args.args[0] == 9
    assert api.remove_stock.call_args.kwargs.get("storage_name") == "Fridge"
    text = query.edit_message_text.call_args.args[0]
    assert "Drank" in text


def test_create_wine_returns_existing_id_on_409():
    from integrations.wine_cellar_api import WineCellarAPI

    api = WineCellarAPI("http://cellar.test", "tok")
    resp = MagicMock()
    resp.status_code = 409
    resp.content = b'{"id": 68, "name": "Old Vine Chenin", "error": "duplicate_wine"}'
    resp.json.return_value = {
        "id": 68, "name": "Old Vine Chenin", "error": "duplicate_wine",
    }
    api.session.post = MagicMock(return_value=resp)
    result = api.create_wine({"name": "Old Vine Chenin"})
    assert result["id"] == 68
    assert result["error"] == "duplicate_wine"
    resp.raise_for_status.assert_not_called()


def _no_match_text_and_labels(update):
    text = update.message.reply_text.call_args.args[0]
    labels = _pick_labels(update)
    return text, labels


@pytest.mark.asyncio
async def test_remove_barolos_from_big_fridge_confirms_the_one_there():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for(HOUSEHOLD)
    handler = _spy_fallthrough(_handler(api))
    update = _text_update("Remove one of my barolos from my big fridge")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    handler._router.handle_text.assert_not_called()
    api.wines_in_stock.assert_called_with(storage_name="Big Fridge")
    text = update.message.reply_text.call_args.args[0]
    assert "Remove 1" in text
    assert "Terre del Barolo" in text
    assert "I Siglati" not in text
    labels = [
        b.text
        for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("drank it" in t.lower() for t in labels)


@pytest.mark.asyncio
async def test_afrikaans_drank_barolos_picks_name_and_appellation():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for(HOUSEHOLD)
    api.get_wine_detail.return_value = BAROLO_C
    handler = _spy_fallthrough(_handler(api))
    update = _text_update("ek het een van my barolos uit die yskas gedrink")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    handler._router.handle_text.assert_not_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Which one" in text
    labels = _pick_labels(update)
    assert any("Terre del Barolo" in t for t in labels)
    assert any("I Siglati" in t for t in labels)
    assert any("Big Fridge" in t or "Cellar" in t for t in labels)
    assert not any("Chenin" in t for t in labels)


@pytest.mark.asyncio
async def test_typed_drink_multi_match_always_picks_then_confirms():
    api = MagicMock()
    api.household_id = 1
    await _assert_barolo_picks_then_confirm(
        _handler(api), api, "I drank one of my barolos",
    )


@pytest.mark.asyncio
async def test_typed_drink_no_match_offers_closest_browse_and_photo():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for(HOUSEHOLD)
    handler = _handler(api)
    update = _text_update("Remove one of my malbecs from my big fridge")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    text, labels = _no_match_text_and_labels(update)
    assert "Nothing matching" in text
    assert "malbecs" in text
    assert "label photo" in text.lower()
    assert any("Browse Big Fridge" in t for t in labels)
    assert any(t == "❌ Cancel" for t in labels)
    buttons = _pick_buttons(update)
    assert any(
        "a:fs," in b.callback_data and "o:Big Fridge" in b.callback_data
        for b in buttons
    )


@pytest.mark.asyncio
async def test_typed_drink_unknown_location_searches_all_storages():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for(HOUSEHOLD)
    handler = _handler(api)
    update = _text_update("Remove one of my barolos from my garage")
    await handler._unknown_text(update, MagicMock())
    api.wines_in_stock.assert_called_with()
    text = update.message.reply_text.call_args.args[0]
    assert "Which one" in text
    labels = _pick_labels(update)
    assert any("Terre del Barolo" in t for t in labels)
    assert any("I Siglati" in t for t in labels)


@pytest.mark.asyncio
async def test_typed_drink_appellation_only_confirms_single_match():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for([SIGLATI, CHENIN])
    handler = _handler(api)
    update = _text_update("Remove one of my barolos")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Remove 1" in text
    assert "I Siglati" in text
    labels = [
        b.text
        for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("drank it" in t.lower() for t in labels)


@pytest.mark.asyncio
async def test_llm_fallback_drink_when_regex_misses(caplog):
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for(HOUSEHOLD)
    handler = _handler(api)
    chat = MagicMock()
    chat.parse.return_value = {
        "intent": "drink",
        "wine_ref": "barolo",
        "fact": {"storage_text": "big fridge"},
    }
    handler.set_chat_models(chat)
    phrase = "please take a barolo out of the big fridge"
    assert parse_typed_stock(phrase) is None
    update = _text_update(phrase)
    with caplog.at_level(logging.INFO, logger="integrations.telegram_bot"):
        await handler._unknown_text(update, MagicMock())
    chat.parse.assert_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Remove 1" in text
    assert "Terre del Barolo" in text
    logged = [r.getMessage() for r in caplog.records if "[text]" in r.getMessage()]
    assert logged
    assert any("handler=chat" in msg for msg in logged)
    assert all("4242" not in msg for msg in logged)


@pytest.mark.asyncio
async def test_typed_stock_logs_handler_without_chat_id(caplog):
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.side_effect = _stock_for([CHENIN])
    handler = _handler(api)
    update = _text_update("Remove one of my chenins")
    with caplog.at_level(logging.INFO, logger="integrations.telegram_bot"):
        await handler._unknown_text(update, MagicMock())
    logged = [r.getMessage() for r in caplog.records if "[text]" in r.getMessage()]
    assert any("handler=typed_stock" in msg for msg in logged)
    assert any("Remove one of my chenins" in msg for msg in logged)
    assert all("4242" not in msg for msg in logged)


def test_similar_in_stock_wines_token_overlap():
    hits = similar_in_stock_wines(HOUSEHOLD, "malbecs")
    assert hits == []
    close = similar_in_stock_wines(HOUSEHOLD + [CHIANTI], "classico")
    assert [w["id"] for w in close] == [20]


@pytest.mark.asyncio
async def test_drank_this_dispatch_does_not_mutate_frozen_update():
    from datetime import datetime, timezone
    from telegram import Chat, Message, Update, User

    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    handler = _handler(api)
    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Old Vine Chenin Blanc", "producer": "Bosman"},
        "time": time.time(),
        "household_id": 1,
        "barcode": "_label_1",
        "photo_data": b"jpeg",
    }
    handler._dispatch_callback = AsyncMock()
    user = User(id=1, first_name="Sam", is_bot=False)
    chat = Chat(id=4242, type="private")
    message = Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="Drank this",
    )
    update = Update(update_id=11, message=message)
    with pytest.raises(AttributeError, match="can't be set"):
        update.callback_query = object()
    await handler._maybe_typed_stock(update, MagicMock(), "Drank this")
    handler._dispatch_callback.assert_awaited_once()
    fake = handler._dispatch_callback.await_args.args[0]
    assert fake.callback_query is not None
    assert getattr(update, "callback_query", None) is None
    assert fake.callback_query.data.startswith("a:dk")
