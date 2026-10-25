"""Typed 'drank this' / 'drank one of my X' must remove stock, not search."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import (
    TelegramBotHandler,
    _cb,
    match_in_stock_wines,
    parse_typed_stock,
)


BAROLO = {
    "id": 11, "name": "Barolo Cannubi 2018", "producer": "Brezza",
    "stock_count": 2, "items": [{"storage": "Fridge", "item_id": 4}],
}
BAROLO_B = {
    "id": 12, "name": "Barolo Brunate 2016", "producer": "Vietti",
    "stock_count": 1, "items": [{"storage": "Cellar", "item_id": 5}],
}
CHENIN = {
    "id": 9, "name": "Old Vine Chenin Blanc", "producer": "Bosman",
    "stock_count": 1,
    "items": [{"storage": "Fridge", "item_id": 3}],
}


def _handler(api):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock):
        api.list_all_wines.return_value = []
    if isinstance(getattr(api.wines_in_stock, "return_value", None), MagicMock):
        api.wines_in_stock.return_value = []
    if isinstance(getattr(api, "web_url", None), MagicMock) or not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
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
    assert parse_typed_stock("Drank this") == ("this", "")
    assert parse_typed_stock("drank it") == ("this", "")
    assert parse_typed_stock("remove one") == ("this", "")
    assert parse_typed_stock("gedrink") == ("this", "")
    assert parse_typed_stock("verwyder een") == ("this", "")
    assert parse_typed_stock("I drank one of my barolos") == ("of", "barolos")
    assert parse_typed_stock("Remove one of my barolos") == ("of", "barolos")
    assert parse_typed_stock("Ek het een van my barolos gedrink") == ("of", "barolos")
    assert parse_typed_stock("Verwyder een van my barolos") == ("of", "barolos")
    assert parse_typed_stock("what should I drink") is None
    assert parse_typed_stock("add") is None


def test_match_in_stock_wines_stems_plurals():
    hits = match_in_stock_wines([BAROLO, BAROLO_B, CHENIN], "barolos")
    assert [w["id"] for w in hits] == [11, 12]
    assert match_in_stock_wines([CHENIN], "barolo") == []


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


@pytest.mark.asyncio
async def test_typed_drank_one_of_my_barolos_asks_which_then_confirms():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.return_value = [BAROLO, BAROLO_B]
    handler = _handler(api)
    update = _text_update("I drank one of my barolos")
    await handler._unknown_text(update, MagicMock())
    api.remove_stock.assert_not_called()
    text = update.message.reply_text.call_args.args[0]
    assert "Which one" in text
    labels = [
        b.text
        for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Cannubi" in t for t in labels)
    assert any("Brunate" in t for t in labels)
    assert all("a:dc," in b.callback_data or b.text == "❌ Cancel"
               for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
               for b in row)


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
