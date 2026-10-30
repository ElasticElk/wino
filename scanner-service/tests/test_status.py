from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import TelegramBotHandler


def _handler(api):
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


async def _status(api):
    handler = _handler(api)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    await handler._cmd_status(update, MagicMock())
    return update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_status_includes_an_estimated_cellar_value():
    api = MagicMock()
    api.wines_in_stock.return_value = [
        {
            "name": "Bin 311", "stock_count": 2, "wine_type": "WH",
            "market_price": 60, "market_price_currency": "GBP",
        },
        {
            "name": "Barolo", "stock_count": 1, "wine_type": "RE",
            "market_price": 400, "market_price_currency": "ZAR",
        },
    ]
    api.list_storages.return_value = []
    text = await _status(api)
    assert (
        "Estimated cellar value: £120 · R400 (market prices, not what you paid)"
        in text
    )


@pytest.mark.asyncio
async def test_status_empty_cellar_invites_a_photo():
    api = MagicMock()
    api.wines_in_stock.return_value = []
    api.list_storages.return_value = [
        {"name": "Cellar", "drawers": 0, "capacity": 0, "bottles": 0},
    ]
    text = await _status(api)
    assert "0 wines" not in text
    assert "photo" in text.lower()
    assert "shelves" in text.lower()


@pytest.mark.asyncio
async def test_status_empty_cellar_follows_stored_language():
    api = MagicMock()
    api.wines_in_stock.return_value = []
    api.list_storages.return_value = [
        {"name": "Cellar", "drawers": 0, "capacity": 0, "bottles": 0},
    ]
    handler = _handler(api)
    handler.set_language("4242", "af")
    update = MagicMock()
    update.effective_chat.id = 4242
    update.effective_user.language_code = "en"
    update.message.text = "/status"
    update.message.chat_id = 4242
    update.message.reply_text = AsyncMock()
    await handler._cmd_status(update, MagicMock())
    text = update.message.reply_text.call_args.args[0]
    assert "leeg" in text.lower()
    assert "foto" in text.lower()
    assert "scan" not in text.lower()


@pytest.mark.asyncio
async def test_status_does_not_mention_shelves_for_a_zero_drawer_kitchen():
    api = MagicMock()
    api.wines_in_stock.return_value = []
    api.list_storages.return_value = [
        {"name": "Kitchen", "drawers": 0, "capacity": 0, "bottles": 0},
    ]
    text = await _status(api)
    assert "shelves" not in text.lower()
    assert "photo" in text.lower()


@pytest.mark.asyncio
async def test_status_omits_value_when_no_market_price():
    api = MagicMock()
    api.wines_in_stock.return_value = [
        {"name": "Bin 311", "stock_count": 2, "wine_type": "WH", "market_price": None},
    ]
    api.list_storages.return_value = []
    text = await _status(api)
    assert "Estimated cellar value" not in text
