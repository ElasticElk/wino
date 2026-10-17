"""A single label photo: ratings arrive, and "Just looking" keeps the card.

Triage answers from the vision model's own memory, and under the reference
rubric that nearly always means no critic score and no Vivino rating — those
come from a live web search that takes far too long to hold the user on a
spinner. So the card goes out first and the numbers are edited into it.

"Just looking" used to route to the generic cancel, which replaced the whole
card with the word "Cancelled." — throwing away exactly the reading the user
had asked for.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.card_store import CardStore
from integrations.telegram_bot import (
    JUST_LOOKING_NOTE,
    REFS_PENDING_NOTE,
    RESEND_PHOTO,
    TelegramBotHandler,
    _cb,
    _clean_wine_data,
    _merge_stored_references,
    _needs_references,
    apply_vote_choice,
    apply_vote_thanks,
    build_drank_view,
)

LABEL = {
    "type": "label",
    "name": "Old Vine Chenin Blanc",
    "producer": "Bosman",
    "vintage": 2020,
    "wine_type": "WH",
    "country": "ZA",
}


def _status_msg():
    msg = AsyncMock()
    msg.chat_id = 4242
    msg.message_id = 7
    return msg


def _handler(api):
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


async def _drain():
    """Let the fire-and-forget enrichment task finish."""
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_gun_scan_label_card_also_runs_the_live_lookup():
    """Fridge-scan + label photo used to skip ratings. Same card as a shop shot."""
    api = MagicMock()
    api.household_id = 1
    api.lookup_references.return_value = {
        "updated": True, "critic_score": 91, "vivino_rating": 3.8,
        "market_price": 14, "market_price_currency": "GBP",
    }
    handler = _handler(api)
    status_msg = _status_msg()
    await handler._show_barcode_label_confirm(status_msg, dict(LABEL), "8411543235217", api)
    first = status_msg.edit_text.call_args.args[0]
    assert REFS_PENDING_NOTE.strip() in first
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Drank this" in t for t in labels)
    await _drain()
    api.lookup_references.assert_called_once()
    final = status_msg.edit_text.call_args.args[0]
    assert "91" in final and "3.8" in final
    assert "8411543235217" in final


@pytest.mark.asyncio
async def test_label_card_gains_ratings_from_the_live_lookup():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_references.return_value = {
        "updated": True,
        "confidence": "high",
        "critic_score": 92,
        "vivino_rating": 4.1,
        "category_rating": 89,
        "market_price": 240.0,
        "market_price_currency": "ZAR",
    }
    handler = _handler(api)
    status_msg = _status_msg()

    await handler._handle_triage_label(status_msg, dict(LABEL))
    first = status_msg.edit_text.call_args.args[0]
    assert REFS_PENDING_NOTE.strip() in first, "user should be told numbers are coming"
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Drank this" in t for t in labels)
    assert any("Add to cellar" in t for t in labels)

    await _drain()

    api.lookup_references.assert_called_once()
    final = status_msg.edit_text.call_args.args[0]
    assert "92" in final and "4.1" in final
    assert REFS_PENDING_NOTE.strip() not in final
    assert "Old Vine Chenin Blanc" in final


@pytest.mark.asyncio
async def test_lookup_finding_nothing_still_clears_the_pending_note():
    """A card must not sit there promising numbers that never come."""
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_references.return_value = {"updated": False, "confidence": "low"}
    handler = _handler(api)
    status_msg = _status_msg()

    await handler._handle_triage_label(status_msg, dict(LABEL))
    await _drain()

    final = status_msg.edit_text.call_args.args[0]
    assert REFS_PENDING_NOTE.strip() not in final
    assert "Old Vine Chenin Blanc" in final


@pytest.mark.asyncio
async def test_just_looking_keeps_the_wine_details():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)

    query = AsyncMock()
    query.data = "a:look"
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.text_html = (
        "\U0001F50D <b>Label analysis complete!</b>\n\n"
        "<b>Old Vine Chenin Blanc</b>\nVintage: 2020\n"
    )
    update = MagicMock()
    update.callback_query = query

    await handler._handle_callback(update, MagicMock())

    text = query.edit_message_text.call_args.args[0]
    assert "Old Vine Chenin Blanc" in text
    assert "Cancelled" not in text
    assert JUST_LOOKING_NOTE.strip() in text
    # No buttons left to press, but the reading stays on screen.
    assert query.edit_message_text.call_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_just_looking_mid_lookup_keeps_its_note_when_ratings_land():
    """Tapping "Just looking" while the search runs must not resurrect buttons."""
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    started = asyncio.Event()

    def slow_lookup(*a, **kw):
        started.set()
        return {"updated": True, "confidence": "high", "critic_score": 92,
                "vivino_rating": 4.1, "category_rating": 89}

    api.lookup_references.side_effect = slow_lookup
    handler = _handler(api)
    status_msg = _status_msg()

    await handler._handle_triage_label(status_msg, dict(LABEL))
    handler._label_cards["4242:7"]["looking"] = True
    await _drain()

    final = status_msg.edit_text.call_args
    assert JUST_LOOKING_NOTE.strip() in final.args[0]
    assert "92" in final.args[0]
    assert final.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_drank_mid_lookup_does_not_restore_add_buttons():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    started = asyncio.Event()

    def slow_lookup(*a, **kw):
        started.set()
        return {"updated": True, "confidence": "high", "critic_score": 92,
                "vivino_rating": 4.1, "category_rating": 89}

    api.lookup_references.side_effect = slow_lookup
    handler = _handler(api)
    status_msg = _status_msg()

    await handler._handle_triage_label(status_msg, dict(LABEL))
    first_count = status_msg.edit_text.call_count
    handler._label_cards["4242:7"]["drunk"] = True
    await _drain()

    assert status_msg.edit_text.call_count == first_count


@pytest.mark.asyncio
async def test_add_to_cellar_mid_lookup_does_not_restore_add_buttons():
    """Ratings landing used to overwrite the fridge picker with Add to cellar."""
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_references.return_value = {
        "updated": True, "critic_score": 92, "vivino_rating": 4.1,
    }
    handler = _handler(api)
    status_msg = _status_msg()

    await handler._handle_triage_label(status_msg, dict(LABEL))
    first_count = status_msg.edit_text.call_count
    handler._label_cards["4242:7"]["committed"] = True
    handler._label_cards["4242:7"]["wine_id"] = 68
    await _drain()

    assert status_msg.edit_text.call_count == first_count
    api.apply_references.assert_called_once()
    assert api.apply_references.call_args.args[0] == 68
    api.refresh_references.assert_not_called()


def _grok_query(barcode):
    query = AsyncMock()
    query.data = _cb("grok", barcode=barcode)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    return update, query


@pytest.mark.asyncio
async def test_confirm_applies_the_lookup_instead_of_searching_again():
    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 9, "name": "Old Vine Chenin Blanc"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 3}
    lookup = {
        "updated": True,
        "confidence": "high",
        "vivino_rating": 4.1,
        "details": {"vivino_rating": {"value": 4.1, "confidence": "medium"}},
    }
    handler = _handler(api)
    handler.pending_store["_label_7"] = {
        "grok_merged": {**LABEL, "reference_lookup": lookup},
        "time": time.time(),
    }
    update, query = _grok_query("_label_7")

    await handler._handle_callback(update, MagicMock())
    await _drain()

    api.apply_references.assert_called_once_with(9, lookup)
    api.refresh_references.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Where are you storing this" in text or "added" in text.lower() or "Cellar" in text


@pytest.mark.asyncio
async def test_confirm_without_a_lookup_still_searches():
    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 9, "name": "Old Vine Chenin Blanc"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1}
    api.refresh_references.return_value = {"updated": False}
    handler = _handler(api)
    handler.pending_store["_label_7"] = {
        "grok_merged": dict(LABEL),
        "time": time.time(),
    }
    update, query = _grok_query("_label_7")

    await handler._handle_callback(update, MagicMock())
    await _drain()

    api.apply_references.assert_not_called()
    api.refresh_references.assert_called_once()


@pytest.mark.asyncio
async def test_confirm_while_lookup_in_flight_does_not_search_again():
    """The usual shop path: Add is tapped before the first search lands."""
    gate = threading.Event()
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.create_wine.return_value = {"id": 88, "name": "Old Vine Chenin Blanc"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 3}
    lookup = {
        "updated": True,
        "confidence": "high",
        "vivino_rating": 4.1,
        "details": {"vivino_rating": {"value": 4.1, "confidence": "medium"}},
    }

    def slow_lookup(*a, **kw):
        gate.wait(timeout=2)
        return lookup

    api.lookup_references.side_effect = slow_lookup
    handler = _handler(api)
    status_msg = _status_msg()
    await handler._handle_triage_label(status_msg, dict(LABEL))
    assert handler._label_cards["4242:7"].get("pending_lookup") is True

    update, query = _grok_query("_label_1")
    await handler._handle_callback(update, MagicMock())
    api.refresh_references.assert_not_called()
    api.apply_references.assert_not_called()

    gate.set()
    await _drain()
    api.apply_references.assert_called_once_with(88, lookup)
    api.refresh_references.assert_not_called()


@pytest.mark.asyncio
async def test_second_add_to_cellar_tap_resumes_the_fridge_picker():
    api = MagicMock()
    api.household_id = 1
    api.list_storages.return_value = [
        {"id": 2, "name": "Big Fridge", "bottles": 3, "drawers": 8},
        {"id": 3, "name": "Kitchen", "bottles": 0, "drawers": 0},
    ]
    api.get_wine_detail.return_value = {
        "name": "Étoile de Pauillac", "stock_count": 0,
    }
    handler = _handler(api)
    handler._label_cards["4242:7"] = {
        "committed": True, "wine_id": 68, "wine_name": "Étoile de Pauillac",
    }
    query = AsyncMock()
    query.data = _cb("grok", barcode="_label_7")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.create_wine.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Where are you storing this" in text
    assert "Étoile de Pauillac" in text
    assert "already handled" not in text.lower()
    assert "expired" not in text.lower()


@pytest.mark.asyncio
async def test_double_tap_while_creating_does_not_create_twice():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler.pending_store["_label_7"] = {
        "grok_merged": {"name": "Étoile de Pauillac", "wine_type": "RE"},
        "time": time.time(),
        "in_progress": True,
    }
    query = AsyncMock()
    query.data = _cb("grok", barcode="_label_7")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.create_wine.assert_not_called()
    query.edit_message_text.assert_not_called()
    texts = [c.args[0] for c in query.answer.call_args_list if c.args]
    assert any("Still working" in str(t) for t in texts)


@pytest.mark.asyncio
async def test_second_drank_tap_does_not_say_barcode_expired():
    """Telegram often delivers Drank this twice. The first tap creates the wine
    and pops pending; the second must not overwrite the confirmation."""
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = {
        "name": "Hungerford Hill Chardonnay", "stock_count": 0,
    }
    handler = _handler(api)
    handler._label_cards["4242:7"] = {
        "drunk": True, "wine_id": 89, "wine_name": "Hungerford Hill Chardonnay",
    }
    query = AsyncMock()
    query.data = _cb("dk", barcode="_label_1")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.create_wine.assert_not_called()
    api.add_stock.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "already handled" not in text.lower()
    assert "expired" not in text.lower()
    assert "Hungerford Hill Chardonnay" in text


@pytest.mark.asyncio
async def test_drank_this_logs_without_taking_stock():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {
        "item_id": 99, "wine_name": "Old Vine Chenin Blanc 2020", "stock_count": 2,
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("dk", wine_id=5)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_called_once_with(
        5, drank=True, price=None, check_duplicate=True,
    )
    api.create_wine.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Drank:" in text
    assert "Old Vine Chenin Blanc 2020" in text
    assert "How was it? (optional)" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "\U0001F44E" in labels
    assert "\u2014" in labels
    assert "\U0001F44D" in labels
    assert "\U0001F610" not in "".join(labels)


@pytest.mark.asyncio
async def test_second_drank_within_an_hour_asks_before_logging_another():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {
        "error": "recent_duplicate",
        "item_id": 129,
        "wine_name": "Hungerford Hill Chardonnay 2021",
        "stock_count": 0,
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("dk", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_called_once_with(
        89, drank=True, price=None, check_duplicate=True,
    )
    text = query.edit_message_text.call_args.args[0]
    assert "Log another bottle?" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "Log another bottle?" in labels
    assert "That's the one" in labels


@pytest.mark.asyncio
async def test_log_another_bottle_confirms_the_duplicate():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {
        "item_id": 130, "wine_name": "Hungerford Hill Chardonnay 2021", "stock_count": 0,
    }
    handler = _handler(api)
    handler._label_cards["4242:7"] = {"guard": True, "wine_id": 89}
    query = AsyncMock()
    query.data = _cb("la", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_called_once_with(
        89, drank=True, price=None, confirm_duplicate=True,
    )
    text = query.edit_message_text.call_args.args[0]
    assert "Drank:" in text
    assert "How was it?" in text


@pytest.mark.asyncio
async def test_repeat_drank_on_the_same_card_reshows_instead_of_the_guard():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler._label_cards["4242:7"] = {
        "drunk": True, "item_id": 129, "wine_name": "Hungerford Hill Chardonnay 2021",
        "stock_count": 0, "wine_id": 89,
    }
    query = AsyncMock()
    query.data = _cb("dk", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Log another bottle?" not in text
    assert "Hungerford Hill Chardonnay 2021" in text
    assert "How was it?" in text


@pytest.mark.asyncio
async def test_log_another_double_tap_does_not_create_twice():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler._label_cards["4242:7"] = {
        "guard": True, "logged_another": True, "drunk": True,
        "item_id": 130, "wine_name": "Hungerford Hill Chardonnay 2021",
        "stock_count": 0, "wine_id": 89,
    }
    query = AsyncMock()
    query.data = _cb("la", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_not_called()


@pytest.mark.asyncio
async def test_log_another_expires_with_the_card():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("la", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_not_called()
    assert "finished" in query.edit_message_text.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_redis_drop_on_tap_asks_to_resend_the_photo():
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")

        def set(self, *a, **k):
            raise ConnectionError("down")

        def exists(self, *a, **k):
            raise ConnectionError("down")

        def delete(self, *a, **k):
            raise ConnectionError("down")

        def scan_iter(self, *a, **k):
            raise ConnectionError("down")

    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler._label_cards = CardStore(redis_client=Boom())
    query = AsyncMock()
    query.data = _cb("dk", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == RESEND_PHOTO


@pytest.mark.asyncio
async def test_add_another_bottle_confirms_the_duplicate():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {
        "item_id": 131, "wine_name": "Hungerford Hill Chardonnay 2021", "stock_count": 2,
    }
    handler = _handler(api)
    handler._label_cards["4242:7"] = {"guard": True, "wine_id": 89}
    query = AsyncMock()
    query.data = _cb("aa", wine_id=89)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.add_stock.assert_called_once_with(
        89, drank=False, price=None, confirm_duplicate=True,
    )
    text = query.edit_message_text.call_args.args[0]
    assert "Added:" in text


@pytest.mark.asyncio
async def test_move_put_back_failure_rolls_the_bottle_back():
    api = MagicMock()
    api.household_id = 1
    api.list_storages.return_value = [
        {"id": 2, "name": "Kitchen"},
        {"id": 3, "name": "Big Fridge"},
    ]
    api.remove_stock.return_value = {
        "wine_name": "Chenin", "stock_count": 0, "storage": "Big Fridge",
        "storage_id": 3, "item_id": 80,
    }
    api.add_stock.side_effect = [
        {"error": "HTTP 500"},
        {"item_id": 81, "wine_name": "Chenin", "stock_count": 1},
    ]
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("ro", wine_id=5, page=3, original="2")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    assert api.remove_stock.call_count == 1
    assert api.add_stock.call_count == 2
    text = query.edit_message_text.call_args.args[0]
    assert "still in" in text.lower() or "Kitchen" in text


@pytest.mark.asyncio
async def test_off_add_failure_offers_retry_without_saying_added():
    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 44, "name": "Bosman Chenin"}
    api.add_stock.return_value = {"error": "HTTP 500"}
    handler = _handler(api)
    handler.pending_store["_x"] = {
        "off_data": {"name": "Bosman Chenin", "producer": "Bosman", "country": "ZA"},
        "time": time.time(),
    }
    query = AsyncMock()
    query.data = _cb("off", barcode="_x")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    text = query.edit_message_text.call_args.args[0]
    assert "Added" not in text
    assert "couldn't add" in text.lower() or "Saved" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Try adding" in t for t in labels)


@pytest.mark.asyncio
async def test_paid_price_is_stored_on_the_card():
    handler = _handler(MagicMock())
    msg = MagicMock()
    msg.chat_id = 4242
    msg.message_id = 7
    handler._remember_paid(msg, 89, 14.5)
    assert handler._label_cards["4242:7"]["paid"] == 14.5
    handler._pending_paid.clear()
    assert handler._paid_for(msg, 89) == 14.5


@pytest.mark.asyncio
async def test_fridge_plus_does_not_say_added_when_stock_fails():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {
        "error": "recent_duplicate", "wine_name": "Chenin", "stock_count": 2,
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("+", wine_id=5, page=0)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    query.answer.assert_awaited()
    toast = query.answer.call_args.args[0]
    assert "Added" not in toast
    assert "Already logged" in toast or "duplicate" in toast.lower()


@pytest.mark.asyncio
async def test_undo_remove_does_not_say_undone_when_add_fails():
    api = MagicMock()
    api.household_id = 1
    api.add_stock.return_value = {"error": "HTTP 500"}
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("ur", wine_id=5)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    text = query.edit_message_text.call_args.args[0]
    assert "Undone" not in text
    assert "Could not undo" in text


@pytest.mark.asyncio
async def test_vote_highlights_the_dash_and_keeps_the_buttons():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    body, markup = build_drank_view("Chenin", 0, 99)
    query = AsyncMock()
    query.data = _cb("vt", item_id=99, original="meh")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_markup = markup
    query.message.text_html = body
    query.message.text = body
    query.message.set_reaction = AsyncMock()
    update = MagicMock()
    update.callback_query = query

    await handler._handle_callback(update, MagicMock())

    api.vote_item.assert_called_once_with(99, "meh")
    text = query.edit_message_text.call_args.args[0]
    assert "How was it?" not in text
    assert "Got it." in text
    new_markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    labels = [b.text for row in new_markup.inline_keyboard for b in row]
    assert "[\u2014]" in labels
    assert "\U0001F44E" in labels
    assert "\U0001F44D" in labels
    assert "\U0001F610" not in "".join(labels)
    query.message.set_reaction.assert_awaited()


def test_vote_thanks_replaces_the_prompt():
    body, _ = build_drank_view("Chenin", 0, 99)
    up = apply_vote_thanks(body, "up")
    assert "How was it?" not in up
    assert "\U0001F44D Got it." in up
    again = apply_vote_thanks(up, "down")
    assert "\U0001F44D Got it." not in again
    assert "\U0001F44E Got it." in again


def test_apply_vote_choice_keeps_other_buttons():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Undo", callback_data=_cb("ur", wine_id=5))],
        [
            InlineKeyboardButton("\U0001F44E", callback_data=_cb("vt", item_id=9, original="down")),
            InlineKeyboardButton("\u2014", callback_data=_cb("vt", item_id=9, original="meh")),
            InlineKeyboardButton("\U0001F44D", callback_data=_cb("vt", item_id=9, original="up")),
        ],
    ])
    chosen = apply_vote_choice(markup, "up")
    labels = [b.text for row in chosen.inline_keyboard for b in row]
    assert "Undo" in labels
    assert "[\U0001F44D]" in labels
    assert "\u2014" in labels
    assert "\U0001F610" not in "".join(labels)


def test_needs_references_only_when_a_rating_is_missing():
    assert _needs_references({"critic_score": None, "vivino_rating": None})
    assert _needs_references({"critic_score": 92, "vivino_rating": None})
    assert not _needs_references({"critic_score": 92, "vivino_rating": 4.1})


def test_stored_references_win_over_the_vision_guess():
    """A wine we already own carries numbers we looked up properly once."""
    data = {"name": "Chenin", "critic_score": None}
    _merge_stored_references(data, {"critic_score": 90, "vivino_rating": 4.0,
                                    "market_price": None})
    assert data["critic_score"] == 90
    assert data["vivino_rating"] == 4.0
    assert "market_price" not in data


def test_clean_wine_data_drops_the_lookup_blob():
    clean = _clean_wine_data({
        "name": "Old Vine Chenin",
        "reference_lookup": {"updated": True, "vivino_rating": 4.1},
    })
    assert "reference_lookup" not in clean
    assert clean["name"]


def test_clean_wine_data_drops_invalid_drink_by():
    """A prose drink-by from Grok used to 400 the create-wine API."""
    clean = _clean_wine_data({
        "name": "Old Vine Chenin",
        "type": "label",
        "drink_by": "drink from 2028",
        "abv": "13.5%",
        "vintage": "2020",
        "wine_type": "still white",
    })
    assert "drink_by" not in clean
    assert "type" not in clean
    assert clean["abv"] == 13.5
    assert clean["vintage"] == 2020
    assert clean["wine_type"] == "RE"


def test_clean_wine_data_keeps_iso_drink_by():
    clean = _clean_wine_data({"name": "X", "drink_by": "2028-06-01T00:00:00"})
    assert clean["drink_by"] == "2028-06-01"


def test_clean_wine_data_unshouts_label_typography():
    clean = _clean_wine_data({
        "name": "BIN 311 CHARDONNAY",
        "producer": "TERREDAVINO",
    })
    assert clean["name"] == "Bin 311 Chardonnay"
    assert clean["producer"] == "Terredavino"


def test_clean_wine_data_leaves_mixed_case_names():
    clean = _clean_wine_data({"name": "Étoile de Pauillac", "producer": "Penfolds"})
    assert clean["name"] == "Étoile de Pauillac"
    assert clean["producer"] == "Penfolds"


def test_label_prompt_forbids_all_caps():
    from integrations.grok_vision import build_label_prompt
    prompt = build_label_prompt("GBP", "GB")
    assert "ALL CAPS" in prompt
    assert "Bin 311 Chardonnay" in prompt


def test_cellar_match_for_hint_new_vintage_is_a_new_row():
    from integrations.telegram_bot import cellar_match_for_hint
    hits = [{"id": 1, "name": "Kanonkop Pinotage", "vintage": 2021}]
    action, wines = cellar_match_for_hint(hits, {"name": "Kanonkop Pinotage", "vintage": 2022})
    assert action == "create"
    assert wines == []
    action, wines = cellar_match_for_hint(hits, {"name": "Kanonkop Pinotage", "vintage": 2021})
    assert action == "stock"
    assert wines[0]["id"] == 1
    action, wines = cellar_match_for_hint(hits, {"name": "Kanonkop Pinotage"})
    assert action == "stock"


def test_identify_hint_prompt_carries_the_wrong_read():
    from integrations.grok_vision import build_identify_hint_prompt
    prompt = build_identify_hint_prompt(
        "Kanonkop Pinotage 2022",
        wrong_name="Kanonkop Pinotage 2021",
        context={"barcode": "600123", "vintage": 2021, "producer": "Kanonkop"},
        target_currency="GBP",
        target_country="GB",
    )
    assert "Kanonkop Pinotage 2021" in prompt
    assert "Kanonkop Pinotage 2022" in prompt
    assert "600123" in prompt
    assert "WRONG" in prompt
    assert "Wine-Searcher" in prompt


@pytest.mark.asyncio
async def test_wrong_wine_button_asks_for_a_typed_name():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = {"name": "Old Vine Chenin", "vintage": 2020}
    handler = _handler(api)
    query = AsyncMock()
    query.data = "a:ww,w:9,b:600123,o:in"
    query.message = MagicMock()
    query.message.chat_id = 4242
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    text = query.edit_message_text.call_args.args[0]
    assert "Type the producer" in text
    pending = handler._misread_corrections["4242"]
    assert pending["barcode"] == "600123"
    assert pending["wrong_name"] == "Old Vine Chenin"
    api.remove_stock.assert_called_once_with(9)


def test_clean_wine_data_saves_shelf_tag_as_paid_price():
    from integrations.telegram_bot import _paid_price
    clean = _clean_wine_data({
        "name": "Chenin",
        "list_price": 149,
        "market_price": 180,
        "retail_price": 175,
    })
    assert clean["price"] == 149
    assert "list_price" not in clean
    assert "retail_price" not in clean
    assert _paid_price({"list_price": 38}, scene="shelf") == 38
    assert _paid_price({"list_price": 120}, scene="menu") is None
    assert _paid_price({"market_price": 240}) is None


def test_taste_wine_compact():
    from integrations.telegram_bot import _taste_wine
    got = _taste_wine({
        "name": "Korlat", "producer": "Kozlović", "vintage": 2016,
        "wine_type": "RE", "country": "HR", "grape_variety": "Malvazija",
    })
    assert got["name"] == "Korlat"
    assert got["grape"] == "Malvazija"
    assert got["vintage"] == 2016


def test_format_achievements_empty():
    from integrations.telegram_bot import format_achievements
    assert format_achievements([]) == ""
    assert format_achievements(None) == ""


def test_format_achievements_renders_titles():
    from integrations.telegram_bot import format_achievements
    text = format_achievements([
        {"emoji": "🌍", "title": "First wine from Croatia", "detail": "Korlat opens Croatia."},
    ])
    assert "Cellar challenge" in text
    assert "First wine from Croatia" in text


def test_format_progress_lists_next_and_missing():
    from integrations.telegram_bot import format_progress
    lines = format_progress({
        "tracks": [{"emoji": "🍾", "title": "Bottles logged", "have": 1, "next": 5}],
        "close": [{"emoji": "🌍", "title": "Wines from Croatia", "have": 1, "next": 5}],
        "missing": [{"emoji": "🍷", "title": "First sparkling"}],
    })
    text = "\n".join(lines)
    assert "On the way" in text
    assert "1/5" in text
    assert "Not yet" in text
    assert "First sparkling" in text


def test_badge_gif_path_maps_family():
    from integrations.telegram_bot import badge_gif_path, _badge_family
    assert _badge_family("country:5") == "country"
    assert _badge_family("grape-explorer") == "grape"
    assert _badge_family("storage-full") == "bottles"
    assert _badge_family("month") == "bottles"
    path = badge_gif_path([{"slug": "country", "title": "First Croatia"}])
    assert path is not None
    assert path.name == "country.gif"
    assert path.is_file()


def test_format_competition_empty_month_still_shows_race():
    from integrations.telegram_bot import format_competition
    lines = format_competition({
        "label": "October",
        "days_left": 30,
        "standings": [],
    })
    text = "\n".join(lines)
    assert "October race" in text
    assert "Nobody has logged a bottle this month" in text


def test_format_competition_race():
    from integrations.telegram_bot import format_competition
    lines = format_competition({
        "label": "October",
        "days_left": 12,
        "standings": [
            {"name": "Sam", "bottles": 6, "rank": 1, "leading": True},
            {"name": "Alex", "bottles": 4, "rank": 2, "leading": False},
        ],
    })
    text = "\n".join(lines)
    assert "October race" in text
    assert "12 days left" in text
    assert "Sam" in text
    assert "Alex" in text
