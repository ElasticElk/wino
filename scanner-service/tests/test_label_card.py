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
from telegram.error import BadRequest

from integrations.telegram_bot import (
    ADD_ANOTHER_PHOTO,
    ADD_PHOTO_INFO,
    DRANK_THIS_LABEL,
    JUST_LOOKING_NOTE,
    PENDING_TIMEOUT_SECONDS,
    REFS_PENDING_NOTE,
    RESEND_PHOTO,
    SIDE_PHOTO_GONE,
    UNREADABLE_LABEL,
    VIEW_WINE,
    BrowseSession,
    TelegramBotHandler,
    _can_update_wine,
    _cb,
    _clean_wine_data,
    _merge_stored_references,
    _needs_references,
    _safe_edit,
    apply_vote_choice,
    apply_vote_thanks,
    build_drank_view,
    find_existing_wines,
    merge_front_back,
    proposed_wine_update,
    wines_to_link,
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
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock):
        api.list_all_wines.return_value = []
    if isinstance(getattr(api, "web_url", None), MagicMock) or not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
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
async def test_unnamed_label_does_not_lookup_and_asks_for_a_clearer_photo():
    """A None/empty Grok name used to POST /api/references/lookup/ and 400."""
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    handler = _handler(api)
    for name in (None, "", "  "):
        status_msg = _status_msg()
        await handler._handle_triage_label(status_msg, {"type": "label", "name": name})
        await _drain()
        api.lookup_references.assert_not_called()
        assert status_msg.edit_text.call_args.args[0] == UNREADABLE_LABEL
        assert status_msg.edit_text.call_args.kwargs.get("reply_markup") is None
        assert not any(str(k).startswith("_label_") for k in handler.pending_store)

    status_msg = _status_msg()
    await handler._show_barcode_label_confirm(
        status_msg, {"type": "label", "name": None}, "8411543235217", api,
    )
    await _drain()
    api.lookup_references.assert_not_called()
    assert status_msg.edit_text.call_args.args[0] == UNREADABLE_LABEL


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
async def test_safe_edit_swallows_not_modified_and_answers():
    query = AsyncMock()
    query.edit_message_text = AsyncMock(
        side_effect=BadRequest(
            "Message is not modified: specified new message content and "
            "reply markup are exactly the same as a current content and "
            "reply markup of the message"
        ),
    )
    await _safe_edit(query, "same", parse_mode="HTML")
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_safe_edit_reraises_other_badrequest():
    query = AsyncMock()
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Message to edit not found"))
    with pytest.raises(BadRequest, match="not found"):
        await _safe_edit(query, "x")
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_double_tap_add_ignores_unmodified_store_card():
    """Second Add redraws the same fridge picker; Telegram rejects that."""
    api = MagicMock()
    api.household_id = 1
    api.list_storages.return_value = [
        {"id": 2, "name": "Wine Fridge", "bottles": 3, "drawers": 8},
        {"id": 3, "name": "Kitchen", "bottles": 0, "drawers": 0},
    ]
    api.get_wine_detail.return_value = {
        "name": "Cepa Lebrel 2019", "stock_count": 0,
    }
    handler = _handler(api)
    handler._label_cards["4242:7"] = {
        "committed": True, "wine_id": 12, "wine_name": "Cepa Lebrel 2019",
    }
    query = AsyncMock()
    query.data = _cb("grok", barcode="_label_7")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message is not modified"),
    )
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.create_wine.assert_not_called()
    query.answer.assert_awaited()
    query.edit_message_text.assert_awaited()


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
    assert _needs_references({"name": "Chenin", "critic_score": None, "vivino_rating": None})
    assert _needs_references({"name": "Chenin", "critic_score": 92, "vivino_rating": None})
    assert not _needs_references({"name": "Chenin", "critic_score": 92, "vivino_rating": 4.1})
    assert not _needs_references({"name": None, "critic_score": None, "vivino_rating": None})
    assert not _needs_references({"name": "", "critic_score": None})
    assert not _needs_references({"name": "  ", "critic_score": None})


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


def test_match_barcode_less_wine_requires_two_words_and_producer():
    from integrations.telegram_bot import match_barcode_less_wine, match_barcode_less_wines

    glen = {"id": 4, "name": "Syrah", "producer": "Glenz", "barcode": ""}
    assert match_barcode_less_wine(
        {"name": "Private Cellar Syrah", "producer": "Cederberg"}, [glen],
    ) is None

    contino = {
        "id": 67, "name": "Contino Reserva",
        "producer": "Viñedos del Contino", "barcode": "",
    }
    match = match_barcode_less_wine(
        {"name": "Contino Rioja", "producer": "Viñedos Del Contino"},
        [contino],
    )
    assert match["id"] == 67

    others = [
        contino,
        {"id": 12, "name": "Viña Pomal", "producer": "Bodegas Bilbaínas", "barcode": ""},
        {"id": 8, "name": "Contino Gran Reserva", "producer": "Viñedos del Contino", "barcode": ""},
        {"id": 9, "name": "Contino Viña del Olivo", "producer": "Viñedos del Contino", "barcode": ""},
        {"id": 10, "name": "Contino Blanco", "producer": "Viñedos del Contino", "barcode": ""},
    ]
    hits = match_barcode_less_wines(
        {"name": "Contino Rioja", "producer": "Viñedos Del Contino"}, others,
    )
    assert [w["id"] for w in hits] == [67, 8, 9]


def test_match_requires_a_name_word_not_just_producer():
    from integrations.telegram_bot import match_barcode_less_wine, match_barcode_less_wines
    same_estate = [
        {"id": 8, "name": "Gran Reserva", "producer": "Viñedos del Contino", "barcode": ""},
        {"id": 67, "name": "Contino Reserva", "producer": "Viñedos del Contino", "barcode": ""},
        {"id": 11, "name": "White Blend", "producer": "Viñedos del Contino", "barcode": ""},
    ]
    hits = match_barcode_less_wines(
        {"name": "Contino Rioja", "producer": "Viñedos Del Contino"}, same_estate,
    )
    assert [w["id"] for w in hits] == [67]
    assert match_barcode_less_wine(
        {"name": "Imperial Reserva", "producer": "CVNE"},
        [{"id": 1, "name": "Viña Real Crianza", "producer": "CVNE", "barcode": ""}],
    ) is None


def test_match_barcode_less_wine_ignores_wines_that_already_have_a_barcode():
    from integrations.telegram_bot import match_barcode_less_wine
    wines = [
        {"id": 97, "name": "Contino Rioja", "producer": "Viñedos del Contino",
         "barcode": "8410591003359"},
        {"id": 67, "name": "Contino Reserva 2021", "producer": "Viñedos del Contino",
         "barcode": ""},
    ]
    match = match_barcode_less_wine(
        {"name": "Contino Rioja", "producer": "Viñedos Del Contino"}, wines,
    )
    assert match["id"] == 67


def test_wines_to_link_uses_find_existing_and_skips_barcoded():
    wines = [
        {"id": 97, "name": "Contino Rioja", "producer": "Viñedos del Contino",
         "barcode": "8410591003359"},
        {"id": 67, "name": "Contino Reserva 2021", "producer": "Viñedos del Contino",
         "barcode": ""},
        {"id": 52, "name": "Calamin Grand Cru Epesses",
         "producer": "Pierre Fonjallaz", "barcode": ""},
    ]
    hits = wines_to_link(
        {"name": "Contino Rioja", "producer": "Viñedos Del Contino"}, wines,
    )
    assert [w["id"] for w in hits] == [67]


def test_invalid_12_digit_barcode_is_not_padded_as_upc_a():
    from integrations.barcodes import normalise_barcode
    assert normalise_barcode("506558704244") == "506558704244"
    assert normalise_barcode("123456789012") == "0123456789012"


@pytest.mark.asyncio
async def test_label_on_unknown_barcode_asks_to_link_barcode_less_wine():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [
        {"id": 67, "name": "Contino Reserva 2021",
         "producer": "Viñedos del Contino", "barcode": "", "stock_count": 2},
    ]
    handler = _handler(api)
    status_msg = _status_msg()
    grok = {
        "type": "label", "name": "Contino Rioja",
        "producer": "Viñedos Del Contino",
        "vintage": 2019, "wine_type": "RE", "country": "ES",
    }
    await handler._show_barcode_label_confirm(status_msg, grok, "8410591003359", api)
    text = status_msg.edit_text.call_args.args[0]
    assert "Is this" in text
    assert "Contino Reserva 2021" in text
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(t.startswith("Yes, link") for t in labels)
    assert "No, new wine" in labels


@pytest.mark.asyncio
async def test_barcode_confirm_does_not_offer_a_wine_that_already_has_a_barcode():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [
        {"id": 97, "name": "Contino Rioja", "producer": "Viñedos del Contino",
         "barcode": "8410591003359", "stock_count": 1},
        {"id": 67, "name": "Contino Reserva 2021", "producer": "Viñedos del Contino",
         "barcode": "", "stock_count": 2},
        {"id": 52, "name": "Calamin Grand Cru Epesses",
         "producer": "Pierre Fonjallaz", "barcode": "", "stock_count": 1},
    ]
    handler = _handler(api)
    status_msg = _status_msg()
    grok = {
        "type": "label", "name": "Contino Rioja",
        "producer": "Viñedos Del Contino",
        "vintage": 2019, "wine_type": "RE", "country": "ES",
    }
    await handler._show_barcode_label_confirm(status_msg, grok, "4000000000000", api)
    await _drain()
    text = status_msg.edit_text.call_args.args[0]
    assert "Contino Reserva 2021" in text
    assert "Calamin" not in text
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Yes, link" in t and "Contino Reserva" in t for t in labels)
    assert not any("8410591003359" in t for t in labels)


@pytest.mark.asyncio
async def test_unknown_barcode_photo_suggests_existing_via_find_existing_wines():
    from unittest.mock import patch

    api = MagicMock()
    api.household_id = 1
    api.lookup_barcode.return_value = []
    api.list_all_wines.return_value = [
        {"id": 97, "name": "Contino Rioja", "producer": "Viñedos del Contino",
         "barcode": "8410591003359"},
        {"id": 67, "name": "Contino Reserva 2021", "producer": "Viñedos del Contino",
         "barcode": ""},
        {"id": 52, "name": "Calamin Grand Cru Epesses",
         "producer": "Pierre Fonjallaz", "barcode": ""},
    ]
    handler = _handler(api)
    status_msg = _status_msg()
    with patch("integrations.open_food_facts.OpenFoodFactsAPI") as off:
        off.return_value.lookup.return_value = {
            "name": "Contino Rioja", "producer": "Viñedos Del Contino",
        }
        await handler._handle_barcode_result(status_msg, "4000000000000")
    text = status_msg.edit_text.call_args.args[0]
    assert "Contino Reserva 2021" in text
    assert "Calamin" not in text
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(t.startswith("Yes, link") for t in labels)


@pytest.mark.asyncio
async def test_unknown_scan_then_matching_label_links_and_does_not_create():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [
        {"id": 67, "name": "Contino Reserva 2021",
         "producer": "Viñedos del Contino", "barcode": ""},
    ]
    api.get_wine_detail.return_value = {
        "id": 67, "name": "Contino Reserva 2021", "barcode": "",
    }
    api.lookup_barcode.return_value = []
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.update_wine.return_value = {"name": "Contino Reserva 2021"}
    api.add_stock.return_value = {
        "stock_count": 3, "wine_name": "Contino Reserva 2021", "item_id": 143,
    }
    api.refresh_references.return_value = {"updated": False}
    handler = _handler(api)
    handler._create_pending(api, "8410591003359", off_data={"name": "Contino Rioja"})
    handler.pending_store["1:8410591003359"]["grok_merged"] = {
        "name": "Contino Rioja", "producer": "Viñedos Del Contino", "wine_type": "RE",
    }
    handler.pending_store["1:8410591003359"]["photo_data"] = b"label-jpg"
    query = AsyncMock()
    query.data = _cb("lnk", wine_id=67, barcode="8410591003359")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())
    await handler._handle_callback(update, MagicMock())
    await _drain()

    api.create_wine.assert_not_called()
    api.update_wine.assert_called_once_with(67, {"barcode": "8410591003359"})
    api.upload_wine_image.assert_called_once_with(67, b"label-jpg")
    api.add_stock.assert_called_once_with(
        67, storage_name="Cellar", price=None, check_duplicate=True,
    )
    texts = [c.args[0] for c in query.edit_message_text.call_args_list if c.args]
    assert any("already linked" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_scan_move_without_wine_id_does_not_fetch_a_blank_card():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("sl", original="4242:7")
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    await handler._handle_callback(update, MagicMock())

    api.get_wine_detail.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Wine" != text
    assert "out of date" in text.lower() or "stock" in text.lower()


def _tap(handler, data):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    return update, query


LNK_BARCODE = "8410591003359"


def _lnk_api():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.get_wine_detail.return_value = {
        "id": 67, "name": "Contino Reserva", "barcode": "",
    }
    api.lookup_barcode.return_value = []
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.update_wine.return_value = {"name": "Contino Reserva"}
    api.add_stock.return_value = {
        "stock_count": 3, "wine_name": "Contino Reserva", "item_id": 143,
    }
    api.refresh_references.return_value = {"updated": False}
    return api


def _lnk_handler(api, *, pending=True, grok=True):
    handler = _handler(api)
    if pending:
        handler._create_pending(api, LNK_BARCODE, off_data={"name": "Contino Rioja"})
        if grok:
            handler.pending_store[f"1:{LNK_BARCODE}"]["grok_merged"] = {
                "name": "Contino Rioja", "producer": "Viñedos Del Contino",
                "wine_type": "RE",
            }
            handler.pending_store[f"1:{LNK_BARCODE}"]["photo_data"] = b"label-jpg"
    return handler


async def _lnk_taps(handler, n, wine_id=67, barcode=LNK_BARCODE):
    last = None
    for _ in range(n):
        update, last = _tap(handler, _cb("lnk", wine_id=wine_id, barcode=barcode))
        await handler._handle_callback(update, MagicMock())
    await _drain()
    return last


@pytest.mark.asyncio
async def test_lnk_two_and_three_taps_link_once():
    for n in (2, 3):
        api = _lnk_api()
        handler = _lnk_handler(api)
        query = await _lnk_taps(handler, n)
        assert api.update_wine.call_count == 1
        assert api.add_stock.call_count == 1
        api.update_wine.assert_called_once_with(67, {"barcode": LNK_BARCODE})
        api.add_stock.assert_called_once_with(
            67, storage_name="Cellar", price=None, check_duplicate=True,
        )
        texts = [c.args[0] for c in query.edit_message_text.call_args_list if c.args]
        assert texts[-1] == "Already linked."


@pytest.mark.asyncio
async def test_lnk_refuses_expired_or_gone_card():
    from integrations.telegram_bot import PENDING_TIMEOUT_SECONDS, BARCODE_FINISHED
    api = _lnk_api()
    handler = _lnk_handler(api, pending=False)
    query = await _lnk_taps(handler, 1)
    api.update_wine.assert_not_called()
    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == BARCODE_FINISHED

    api = _lnk_api()
    handler = _lnk_handler(api)
    handler.pending_store[f"1:{LNK_BARCODE}"]["time"] = (
        time.time() - PENDING_TIMEOUT_SECONDS - 1
    )
    query = await _lnk_taps(handler, 1)
    api.update_wine.assert_not_called()
    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == BARCODE_FINISHED


@pytest.mark.asyncio
async def test_lnk_refuses_target_that_already_has_a_different_barcode():
    api = _lnk_api()
    api.get_wine_detail.return_value = {
        "id": 67, "name": "Contino Reserva", "barcode": "0123456789012",
    }
    handler = _lnk_handler(api)
    query = await _lnk_taps(handler, 1)
    api.update_wine.assert_not_called()
    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == (
        "Contino Reserva already has a different barcode"
    )


@pytest.mark.asyncio
async def test_lnk_refuses_barcode_already_on_another_wine():
    api = _lnk_api()
    api.lookup_barcode.return_value = [
        {"id": 12, "name": "Viña Pomal"},
    ]
    handler = _lnk_handler(api)
    query = await _lnk_taps(handler, 1)
    api.update_wine.assert_not_called()
    api.add_stock.assert_not_called()
    assert "already on Viña Pomal" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_lnk_created_for_a_different_wine_is_not_already_linked():
    api = _lnk_api()
    handler = _lnk_handler(api)
    handler.pending_store[f"1:{LNK_BARCODE}"]["wine_id"] = 67
    query = await _lnk_taps(handler, 1, wine_id=8)
    api.update_wine.assert_not_called()
    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == (
        "This barcode was already added as Contino Reserva"
    )


@pytest.mark.asyncio
async def test_grok_no_new_wine_creates_immediately_after_is_this_prompt():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [
        {"id": 67, "name": "Contino Reserva",
         "producer": "Viñedos del Contino", "barcode": ""},
    ]
    api.create_wine.return_value = {"id": 98, "name": "Contino Rioja"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 9}
    handler = _handler(api)
    handler._create_pending(api, "8410591003359")
    handler.pending_store["1:8410591003359"]["grok_merged"] = {
        "name": "Contino Rioja", "producer": "Viñedos Del Contino",
        "wine_type": "RE", "country": "ES",
    }
    update, query = _tap(handler, _cb("grok", barcode="8410591003359"))
    await handler._handle_callback(update, MagicMock())
    api.create_wine.assert_not_called()
    assert "Is this" in query.edit_message_text.call_args.args[0]

    update, query = _tap(
        handler, _cb("grok", barcode="8410591003359", original="new"),
    )
    await handler._handle_callback(update, MagicMock())
    api.create_wine.assert_called_once()
    assert not any("busy" in str(c).lower() for c in query.answer.call_args_list)
    assert not any(
        "busy" in str(c.args[0]).lower()
        for c in query.edit_message_text.call_args_list if c.args
    )


@pytest.mark.asyncio
async def test_mode3_pyzbar_barcode_is_kept_despite_other_pending():
    api = MagicMock()
    api.household_id = 1
    api.lookup_barcode.return_value = []
    api.list_all_wines.return_value = []
    handler = _handler(api)
    handler._create_pending(api, "1111111111111")
    handler._download_photo = AsyncMock(return_value=b"jpeg")
    handler.set_local_barcode_reader(lambda data: "8410591003359")
    handler.set_triage_callback(
        lambda *a, **k: {**LABEL, "type": "label", "name": "Contino Rioja"},
    )
    status_msg = _status_msg()
    update = MagicMock()
    update.message.reply_to_message = None
    update.message.chat_id = 4242
    update.message.reply_text = AsyncMock(return_value=status_msg)
    update.effective_chat.id = 4242
    context = MagicMock()

    from unittest.mock import patch
    with patch("integrations.open_food_facts.OpenFoodFactsAPI") as off:
        off.return_value.lookup.return_value = None
        await handler._handle_photo_body(update, context)

    pending = handler.pending_store.get("1:8410591003359") or {}
    assert pending.get("grok_merged", {}).get("name") == "Contino Rioja"
    other = handler.pending_store.get("1:1111111111111") or {}
    assert "grok_merged" not in other
    texts = " ".join(
        str(c.args[0]) for c in status_msg.edit_text.call_args_list if c.args
    )
    replies = " ".join(
        str(c.args[0]) for c in update.message.reply_text.call_args_list if c.args
    )
    assert "8410591003359" in texts + replies
    assert "Multiple barcodes" not in replies


@pytest.mark.asyncio
async def test_pi_scan_then_same_barcode_photo_shows_label_confirm():
    api = MagicMock()
    api.household_id = 1
    api.lookup_barcode.return_value = []
    api.list_all_wines.return_value = []
    handler = _handler(api)
    handler._create_pending(api, "8410591003359", off_data={"name": "Contino Rioja"})
    handler._download_photo = AsyncMock(return_value=b"jpeg")
    handler.set_local_barcode_reader(lambda data: "8410591003359")
    handler.set_photo_callback(
        lambda *a, **k: {**LABEL, "type": "label", "name": "Contino Rioja"},
    )

    def no_triage(*a, **k):
        raise AssertionError("pending barcode should use the label flow, not triage")

    handler.set_triage_callback(no_triage)
    status_msg = _status_msg()
    update = MagicMock()
    update.message.reply_to_message = None
    update.message.chat_id = 4242
    update.message.reply_text = AsyncMock(return_value=status_msg)
    update.effective_chat.id = 4242

    await handler._handle_photo_body(update, MagicMock())

    pending = handler.pending_store.get("1:8410591003359") or {}
    assert pending.get("grok_merged", {}).get("name") == "Contino Rioja"
    texts = " ".join(
        str(c.args[0]) for c in status_msg.edit_text.call_args_list if c.args
    )
    replies = " ".join(
        str(c.args[0]) for c in update.message.reply_text.call_args_list if c.args
    )
    blob = texts + replies
    assert "8410591003359" in blob
    assert "already pending" not in blob.lower()
    labels = [
        b.text
        for c in status_msg.edit_text.call_args_list
        for row in (c.kwargs.get("reply_markup").inline_keyboard if c.kwargs.get("reply_markup") else [])
        for b in row
    ]
    assert any("Confirm" in t or "add to cellar" in t.lower() for t in labels)


@pytest.mark.asyncio
async def test_photo_of_already_created_barcode_shows_known_wine_not_grok():
    api = MagicMock()
    api.household_id = 1
    api.lookup_barcode.return_value = [
        {"id": 67, "name": "Contino Reserva", "stock_count": 1},
    ]
    handler = _handler(api)
    handler._create_pending(api, "8410591003359")
    handler.pending_store["1:8410591003359"]["wine_id"] = 67
    handler._download_photo = AsyncMock(return_value=b"jpeg")
    handler.set_local_barcode_reader(lambda data: "8410591003359")

    def no_grok(*a, **k):
        raise AssertionError("already-created barcode must not call Grok")

    handler.set_photo_callback(no_grok)
    handler.set_triage_callback(no_grok)
    status_msg = _status_msg()
    update = MagicMock()
    update.message.reply_to_message = None
    update.message.chat_id = 4242
    update.message.reply_text = AsyncMock(return_value=status_msg)
    update.effective_chat.id = 4242

    await handler._handle_photo_body(update, MagicMock())

    api.lookup_barcode.assert_called()
    texts = " ".join(
        str(c.args[0]) for c in status_msg.edit_text.call_args_list if c.args
    )
    assert "Contino Reserva" in texts
    assert "In stock" in texts
    labels = [
        b.text
        for c in status_msg.edit_text.call_args_list
        for row in (c.kwargs.get("reply_markup").inline_keyboard if c.kwargs.get("reply_markup") else [])
        for b in row
    ]
    assert any("Add" in t for t in labels)
    assert not any("Confirm" in t for t in labels)


@pytest.mark.asyncio
async def test_lnk_concurrent_taps_link_once_with_try_claim():
    class ClaimDict(dict):
        def __init__(self):
            super().__init__()
            self._held = set()
            self._lock = threading.Lock()

        def try_claim(self, key):
            with self._lock:
                if key in self._held:
                    return False
                self._held.add(key)
                return True

        def release_claim(self, key):
            with self._lock:
                self._held.discard(key)

    api = _lnk_api()
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api,
        pending_store=ClaimDict(),
    )
    handler._create_pending(api, LNK_BARCODE, off_data={"name": "Contino Rioja"})
    handler.pending_store[f"1:{LNK_BARCODE}"]["grok_merged"] = {
        "name": "Contino Rioja", "producer": "Viñedos Del Contino",
        "wine_type": "RE",
    }
    updates = [_tap(handler, _cb("lnk", wine_id=67, barcode=LNK_BARCODE)) for _ in range(3)]
    await asyncio.gather(*(
        handler._handle_callback(update, MagicMock()) for update, _ in updates
    ))
    await _drain()
    assert api.update_wine.call_count == 1
    assert api.add_stock.call_count == 1


@pytest.mark.asyncio
async def test_fridge_minus_last_bottle_offers_undo_and_put_back():
    api = MagicMock()
    api.household_id = 1
    api.remove_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 0, "item_id": 10,
        "storage": "Big Fridge",
    }
    api.restore_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 1, "item_id": 10,
        "storage": "Big Fridge", "action": "restored",
    }
    leftover = [{
        "id": 1, "name": "Barbaresco 2021", "stock_count": 1,
        "wine_type": "RE", "producer": "Cascina Ghercina",
    }]
    api.wines_in_stock.return_value = []
    handler = _handler(api)
    handler._browse_sessions["4242:7"] = BrowseSession(
        leftover, mode="fridge", storage_name="All storages",
    )
    update, query = _tap(handler, _cb("-", wine_id=1, page=0))
    await handler._handle_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "last bottle" in text.lower()
    assert "Barbaresco 2021" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Undo" in t for t in labels)
    api.remove_stock.assert_called_once_with(1)

    api.wines_in_stock.return_value = leftover
    update, query = _tap(handler, _cb("ul", wine_id=1, item_id=10))
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(10)
    api.add_stock.assert_not_called()
    restored = query.edit_message_text.call_args.args[0]
    assert "Barbaresco 2021" in restored
    assert "last bottle" not in restored.lower()


@pytest.mark.asyncio
async def test_ul_double_tap_restores_once():
    api = MagicMock()
    api.household_id = 1
    api.restore_stock.side_effect = [
        {
            "item_id": 10, "wine_name": "Barbaresco 2021", "stock_count": 1,
            "storage": "Big Fridge", "action": "restored",
        },
        {"error": "already_restored", "item_id": 10, "wine_name": "Barbaresco 2021",
         "stock_count": 1},
    ]
    handler = _handler(api)
    update, first = _tap(handler, _cb("ul", wine_id=1, item_id=10))
    await handler._handle_callback(update, MagicMock())
    update, second = _tap(handler, _cb("ul", wine_id=1, item_id=10))
    await handler._handle_callback(update, MagicMock())
    assert api.restore_stock.call_count == 2
    api.add_stock.assert_not_called()
    assert "Put back" in first.edit_message_text.call_args.args[0]
    assert second.edit_message_text.call_args.args[0] == "Already put back"


@pytest.mark.asyncio
async def test_ul_after_ten_minutes_is_refused():
    api = MagicMock()
    api.household_id = 1
    api.restore_stock.return_value = {"error": "too_late", "item_id": 10}
    handler = _handler(api)
    update, query = _tap(handler, _cb("ul", wine_id=1, item_id=10))
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(10)
    api.add_stock.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == (
        "Too late to undo, use + to add it back"
    )


CEPA = {
    "id": 94, "name": "Cepa Lebrel / De Nuestra Bodega 2019 Rioja",
    "producer": "De Nuestra Bodega", "vintage": 2019, "country": "ES",
    "region_name": "Rioja", "wine_type": "RE", "barcode": "",
}


def test_reserva_without_producer_offers_cepa_lebrel_not_other_vintages():
    from integrations.telegram_bot import match_barcode_less_wines

    wines = [
        CEPA,
        {**CEPA, "id": 95, "name": "Cepa Lebrel 2018", "vintage": 2018},
        {
            "id": 96, "name": "Ribera Reserva", "producer": "Other",
            "vintage": 2019, "country": "ES", "region_name": "Ribera del Duero",
            "wine_type": "RE", "barcode": "",
        },
    ]
    hits = match_barcode_less_wines(
        {"name": "Reserva", "vintage": 2019, "country": "ES",
         "region": "Rioja", "wine_type": "RE"},
        wines,
    )
    assert [w["id"] for w in hits] == [94]


@pytest.mark.asyncio
async def test_classification_name_confirm_card_says_couldnt_read_the_name():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [CEPA]
    handler = _handler(api)
    status_msg = _status_msg()
    await handler._show_barcode_label_confirm(
        status_msg,
        {"name": "Reserva", "vintage": 2019, "country": "ES",
         "region": "Rioja", "wine_type": "RE"},
        "4056489114406",
        api,
    )
    await _drain()
    text = status_msg.edit_text.call_args.args[0]
    assert "We couldn't read the name." in text
    assert "Who makes this?" in text
    labels = [
        b.text
        for row in status_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert not any("Cepa Lebrel" in t for t in labels)
    assert not any(t.startswith("Yes, link") for t in labels)


@pytest.mark.asyncio
async def test_grok_without_producer_asks_who_makes_this_before_create():
    from integrations.telegram_bot import WHO_MAKES_THIS

    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler._create_pending(api, "4056489114406")
    handler.pending_store["1:4056489114406"]["grok_merged"] = {
        "name": "Reserva", "vintage": 2019, "country": "ES", "wine_type": "RE",
    }
    update, query = _tap(handler, _cb("grok", barcode="4056489114406"))
    await handler._handle_callback(update, MagicMock())
    api.create_wine.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == WHO_MAKES_THIS
    markup = query.edit_message_text.call_args.kwargs.get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Add without producer" in t for t in labels)
    assert any("Cancel" in t for t in labels)
    assert "4056489114406" in handler._await_producer["4242"]["barcode"]


@pytest.mark.asyncio
async def test_typed_producer_then_confirm_creates_the_wine():
    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 100, "name": "Reserva"}
    api.list_storages.return_value = [{"name": "Cellar", "id": 1, "bottles": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 3}
    handler = _handler(api)
    handler._create_pending(api, "_label_1")
    handler.pending_store["_label_1"]["grok_merged"] = {
        "name": "Reserva", "vintage": 2019, "country": "ES", "wine_type": "RE",
    }
    handler._await_producer["4242"] = {"barcode": "_label_1", "time": time.time()}
    msg = AsyncMock()
    msg.text = "De Nuestra Bodega"
    msg.chat_id = 4242
    msg.message_id = 8
    update = MagicMock()
    update.message = msg
    update.effective_chat.id = 4242
    assert await handler._maybe_take_producer(
        update, "De Nuestra Bodega", MagicMock(),
    ) is True
    api.create_wine.assert_called_once()
    assert api.create_wine.call_args.args[0]["producer"] == "De Nuestra Bodega"
    assert api.create_wine.call_args.args[0]["name"] == "Reserva"


@pytest.mark.asyncio
async def test_front_label_add_tips_to_send_the_barcode_side():
    from integrations.telegram_bot import BARCODE_TIP

    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 94, "name": "Cepa Lebrel"}
    api.list_storages.return_value = [
        {"name": "Kitchen", "id": 2, "bottles": 1},
        {"name": "Cellar", "id": 1, "bottles": 0},
    ]
    handler = _handler(api)
    handler._create_pending(api, "_label_9")
    handler.pending_store["_label_9"]["grok_merged"] = {
        "name": "Cepa Lebrel", "producer": "De Nuestra Bodega", "vintage": 2019,
    }
    update, query = _tap(handler, _cb("grok", barcode="_label_9"))
    await handler._handle_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Send the barcode side too so scans find it next time." in text
    assert BARCODE_TIP.strip() in text or "barcode side" in text


@pytest.mark.asyncio
async def test_plus_after_remove_restores_instead_of_adding_unsorted():
    api = MagicMock()
    api.household_id = 1
    api.restore_stock.return_value = {
        "wine_name": "Pièce de Résistance", "stock_count": 1,
        "item_id": 59, "storage": "Kitchen", "action": "restored",
    }
    leftover = [{
        "id": 59, "name": "Pièce de Résistance", "stock_count": 1,
        "wine_type": "RE",
    }]
    api.wines_in_stock.return_value = leftover
    handler = _handler(api)
    handler._browse_sessions["4242:7"] = BrowseSession(
        leftover, mode="fridge", storage_name="Kitchen",
    )
    handler._browse_sessions["4242:7"].last_removed = {
        "wine_id": 59, "name": "Pièce de Résistance", "item_id": 136,
        "storage": "Kitchen",
    }
    update, query = _tap(handler, _cb("+", wine_id=59, page=0))
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(136)
    api.add_stock.assert_not_called()
    toasts = [c.args[0] for c in query.answer.await_args_list if c.args]
    assert any("Put back" in str(t) for t in toasts)


@pytest.mark.asyncio
async def test_plus_without_recent_remove_adds_to_filtered_storage():
    api = MagicMock()
    api.household_id = 1
    api.restore_latest.return_value = {"error": "nothing_to_restore"}
    api.add_stock.return_value = {
        "wine_name": "Pièce de Résistance", "stock_count": 2, "item_id": 200,
        "action": "added",
    }
    leftover = [{
        "id": 59, "name": "Pièce de Résistance", "stock_count": 2,
        "wine_type": "RE",
    }]
    api.wines_in_stock.return_value = leftover
    handler = _handler(api)
    handler._browse_sessions["4242:7"] = BrowseSession(
        leftover, mode="fridge", storage_name="Kitchen",
    )
    update, query = _tap(handler, _cb("+", wine_id=59, page=0))
    await handler._handle_callback(update, MagicMock())
    api.add_stock.assert_called_once_with(59, storage_name="Kitchen")


@pytest.mark.asyncio
async def test_out_button_undo_restores():
    api = MagicMock()
    api.household_id = 1
    api.remove_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 0, "item_id": 10,
    }
    api.restore_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 1, "item_id": 10,
        "storage": "Cellar", "action": "restored",
    }
    handler = _handler(api)
    update, query = _tap(handler, _cb("out", wine_id=1))
    await handler._handle_callback(update, MagicMock())
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Undo" in t for t in labels)
    update, query = _tap(handler, _cb("ul", wine_id=1, item_id=10))
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(10)
    assert "Put back" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_scan_out_card_undo_restores():
    from integrations.telegram_bot import build_stock_update_view

    api = MagicMock()
    api.household_id = 1
    api.restore_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 1, "item_id": 79,
        "storage": "Big Fridge", "action": "restored",
    }
    text, markup = build_stock_update_view(
        "out", "Barbaresco 2021", 0, wine_id=1, item_id=79, storage="Big Fridge",
    )
    assert "Undo" in "".join(b.text for row in markup.inline_keyboard for b in row)
    undo = markup.inline_keyboard[0][0].callback_data
    assert "a:ul," in undo
    handler = _handler(api)
    update, query = _tap(handler, undo)
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(79)


@pytest.mark.asyncio
async def test_drank_undo_restores_and_does_not_revoke_badges():
    """Undoing Drank puts the bottle back. Badges stay (award is get_or_create)."""
    api = MagicMock()
    api.household_id = 1
    api.restore_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 1, "item_id": 12,
        "storage": "Cellar", "action": "restored",
    }
    text, markup = build_drank_view("Barbaresco 2021", 0, 12, wine_id=1)
    undo = markup.inline_keyboard[0][0].callback_data
    assert "a:ul," in undo
    handler = _handler(api)
    update, query = _tap(handler, undo)
    await handler._handle_callback(update, MagicMock())
    api.restore_stock.assert_called_once_with(12)
    assert not hasattr(api, "revoke_achievement") or not api.revoke_achievement.called


@pytest.mark.asyncio
async def test_ul_concurrent_taps_restore_once_with_try_claim():
    class ClaimDict(dict):
        def __init__(self):
            super().__init__()
            self._held = set()
            self._lock = threading.Lock()

        def try_claim(self, key):
            with self._lock:
                if key in self._held:
                    return False
                self._held.add(key)
                return True

        def release_claim(self, key):
            with self._lock:
                self._held.discard(key)

    api = MagicMock()
    api.household_id = 1
    api.restore_stock.return_value = {
        "item_id": 10, "wine_name": "Barbaresco 2021", "stock_count": 1,
        "storage": "Big Fridge", "action": "restored",
    }
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api,
        pending_store=ClaimDict(),
        card_store=ClaimDict(),
    )
    updates = [_tap(handler, _cb("ul", wine_id=1, item_id=10)) for _ in range(3)]
    await asyncio.gather(*(
        handler._handle_callback(update, MagicMock()) for update, _ in updates
    ))
    assert api.restore_stock.call_count == 1
    api.add_stock.assert_not_called()


@pytest.mark.asyncio
async def test_card_redraw_not_modified_is_a_noop_and_answers():
    api = MagicMock()
    api.household_id = 1
    api.wines_in_stock.return_value = [{
        "id": 1, "name": "Barbaresco 2021", "stock_count": 1, "wine_type": "RE",
    }]
    handler = _handler(api)
    handler._browse_sessions["4242:7"] = BrowseSession(
        api.wines_in_stock.return_value, mode="fridge", storage_name="All storages",
    )
    update, query = _tap(handler, _cb("+", wine_id=1, page=0))
    api.restore_latest.return_value = {"error": "nothing_to_restore"}
    api.add_stock.return_value = {
        "wine_name": "Barbaresco 2021", "stock_count": 2, "action": "added",
    }
    query.edit_message_text.side_effect = BadRequest("Message is not modified")
    await handler._handle_callback(update, MagicMock())
    query.answer.assert_awaited()


HH6_CELLAR = [
    {
        "id": 52, "name": "Calamin Grand Cru Epesses",
        "producer": "Pierre Fonjallaz", "vintage": 2023, "stock_count": 1,
    },
    {
        "id": 105, "name": "St-Saphorin Grand Cru",
        "producer": "Domaine Bovy", "vintage": 2024, "stock_count": 1,
    },
    {
        "id": 107, "name": "Dezaley",
        "producer": "Louis Bovard", "vintage": 2022, "stock_count": 1,
    },
    {
        "id": 109, "name": "Dezaley Grand Cru Chasselas",
        "producer": "Union Vinicole Cully", "vintage": 2025, "stock_count": 1,
    },
    {
        "id": 110, "name": "Dezaley",
        "producer": "Maison Blanche", "vintage": 2024, "stock_count": 1,
    },
]
DEZALEY_QUERY = {
    "name": "Dezaley Grand Cru Chasselas",
    "producer": "Union Vinicole Cully",
    "vintage": 2025,
    "wine_type": "WH",
    "country": "CH",
}


def test_merge_front_back_front_wins_name_back_wins_barcode():
    merged = merge_front_back(
        {
            "name": "L'Originelle", "producer": "Domaine X", "vintage": 2022,
            "wine_type": "WH", "abv": None,
        },
        {
            "name": "White Wine", "producer": "", "vintage": None,
            "barcode": "8026856001468", "abv": 12.5, "wine_type": "WH",
        },
    )
    assert merged["name"] == "L'Originelle"
    assert merged["producer"] == "Domaine X"
    assert merged["vintage"] == 2022
    assert merged["barcode"] == "8026856001468"
    assert merged["abv"] == 12.5


def test_proposed_wine_update_fills_blanks_and_flags_overwrites():
    fills, overwrites = proposed_wine_update(
        {"producer": "Old Prod", "vintage": 2018, "barcode": "111", "abv": None},
        {"producer": "New Prod", "vintage": 2020, "barcode": "222", "abv": 13.5},
    )
    assert fills["abv"] == 13.5
    fields = {field for field, _old, _new in overwrites}
    assert fields == {"producer", "vintage", "barcode"}
    assert "abv" not in fields


def test_dezaley_query_does_not_match_appellation_neighbours():
    hits = find_existing_wines(DEZALEY_QUERY, HH6_CELLAR)
    ids = [w["id"] for w in hits]
    assert 52 not in ids
    assert 105 not in ids
    assert 107 not in ids
    assert 110 not in ids
    assert ids == [109]
    assert _can_update_wine(DEZALEY_QUERY, HH6_CELLAR[3])


def test_wine_choice_label_includes_producer_and_vintage():
    from integrations.telegram_bot import _wine_choice_label
    label = _wine_choice_label(HH6_CELLAR[3])
    assert "Union Vinicole Cully" in label
    assert "2025" in label


@pytest.mark.asyncio
async def test_front_and_back_photos_create_one_wine_with_both_images():
    front = {
        "type": "label",
        "name": "L'Originelle",
        "producer": "Domaine X",
        "vintage": 2022,
        "wine_type": "WH",
        "country": "FR",
    }
    back = {
        "type": "label",
        "name": "White Wine",
        "barcode": "8026856001468",
        "abv": 12.5,
        "wine_type": "WH",
        "country": "FR",
    }
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.create_wine.return_value = {"id": 200, "name": "L'Originelle"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 1}
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    handler.set_label_analyse_callback(
        lambda photo, *a, **k: dict(back) if photo == b"BACK" else dict(front),
    )
    status = _status_msg()
    update = MagicMock()
    update.effective_chat.id = 4242

    await handler._handle_triage_label(status, dict(front), photo_data=b"FRONT", update=update)
    await _drain()
    labels = [
        b.text
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert ADD_ANOTHER_PHOTO in labels

    await handler._attach_side_photo(status, "_label_1", b"BACK", update)
    pending = handler.pending_store["_label_1"]
    merged = pending["grok_merged"]
    assert merged["producer"] == "Domaine X"
    assert merged["vintage"] == 2022
    assert merged["barcode"] == "8026856001468"
    assert pending["photo_front"] == b"FRONT"
    assert pending["photo_back"] == b"BACK"

    tap, query = _tap(handler, _cb("grok", barcode="_label_1"))
    await handler._handle_callback(tap, MagicMock())
    api.create_wine.assert_called_once()
    created = api.create_wine.call_args.args[0]
    assert created["producer"] == "Domaine X"
    assert created["vintage"] == 2022
    assert created["barcode"] == "8026856001468"
    types = [c.kwargs.get("image_type") for c in api.upload_wine_image.call_args_list]
    assert types == ["LF", "LB"]
    assert api.upload_wine_image.call_count == 2


@pytest.mark.asyncio
async def test_attach_side_photo_refuses_missing_expired_or_confirmed():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    handler._analyse_photo_bytes = AsyncMock()
    status = _status_msg()
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.reply_text = AsyncMock()

    await handler._attach_side_photo(status, "_label_1", b"BACK", update)
    assert status.edit_text.call_args.args[0] == SIDE_PHOTO_GONE
    handler._analyse_photo_bytes.assert_not_awaited()

    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Old Vine Chenin Blanc"},
        "time": time.time() - PENDING_TIMEOUT_SECONDS - 1,
    }
    status.edit_text.reset_mock()
    await handler._attach_side_photo(status, "_label_1", b"BACK", update)
    assert status.edit_text.call_args.args[0] == SIDE_PHOTO_GONE
    handler._analyse_photo_bytes.assert_not_awaited()

    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Old Vine Chenin Blanc"},
        "time": time.time(),
        "wine_id": 9,
    }
    status.edit_text.reset_mock()
    await handler._attach_side_photo(status, "_label_1", b"BACK", update)
    assert status.edit_text.call_args.args[0] == SIDE_PHOTO_GONE
    handler._analyse_photo_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsolicited_second_photo_offers_back_of_pending_wine():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), photo_data=b"FRONT")
    await _drain()

    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.reply_text = AsyncMock()
    offered = await handler._maybe_handle_extra_label_photo(update, b"BACK")
    assert offered is True
    text = update.message.reply_text.call_args.args[0]
    assert "Is this the back of" in text
    assert "Old Vine Chenin Blanc" in text
    labels = [
        b.text
        for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("back" in t.lower() for t in labels)
    assert any("different wine" in t.lower() for t in labels)


@pytest.mark.asyncio
async def test_yes_back_attaches_stored_photo_to_same_card():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    handler.set_label_analyse_callback(lambda photo, *a, **k: {
        "type": "label", "name": "White Wine", "barcode": "8026856001468", "abv": 12.5,
    })
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), photo_data=b"FRONT")
    await _drain()
    offer = MagicMock()
    offer.effective_chat.id = 4242
    offer.message.reply_text = AsyncMock()
    await handler._maybe_handle_extra_label_photo(offer, b"BACK")
    tap, _query = _tap(handler, _cb("yb", barcode="_label_1"))
    await handler._handle_callback(tap, MagicMock())
    pending = handler.pending_store["_label_1"]
    assert pending["grok_merged"]["barcode"] == "8026856001468"
    assert pending["grok_merged"]["producer"] == "Bosman"
    assert pending["photo_front"] == b"FRONT"
    assert pending["photo_back"] == b"BACK"


@pytest.mark.asyncio
async def test_added_card_has_view_wine_add_photo_and_web_url():
    api = MagicMock()
    api.household_id = 1
    api.create_wine.return_value = {"id": 200, "name": "L'Originelle"}
    api.list_storages.return_value = [{"name": "Cellar", "drawers": 0}]
    api.add_stock.return_value = {"stock_count": 1, "item_id": 1}
    handler = _handler(api)
    handler.pending_store["_label_1"] = {
        "grok_merged": {
            "name": "L'Originelle", "producer": "Domaine X", "vintage": 2022,
        },
        "time": time.time(),
    }
    tap, query = _tap(handler, _cb("grok", barcode="_label_1"))
    await handler._handle_callback(tap, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "https://cellar.test/wine/200" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert ADD_PHOTO_INFO in labels
    assert VIEW_WINE in labels
    view = next(
        b for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row if b.text == VIEW_WINE
    )
    assert view.callback_data.startswith("a:det,")
    assert "w:200" in view.callback_data
    add = next(
        b for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row if b.text == ADD_PHOTO_INFO
    )
    assert add.callback_data.startswith("a:ai,")


def test_detail_card_has_add_photo_info_and_web_url():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    wine = {
        "id": 109, "name": "Dezaley Grand Cru Chasselas",
        "producer": "Union Vinicole Cully", "vintage": 2025,
        "wine_type": "WH", "stock_count": 1, "items": [],
    }
    text, kb = handler._build_detail_card(wine, api=api)
    assert "https://cellar.test/wine/109" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert ADD_PHOTO_INFO in labels
    add = next(b for row in kb.inline_keyboard for b in row if b.text == ADD_PHOTO_INFO)
    assert add.callback_data.startswith("a:ai,")


@pytest.mark.asyncio
async def test_edit_photo_asks_before_overwriting_filled_fields():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = {
        "id": 113, "name": "Existing", "producer": "Old Prod",
        "vintage": 2018, "barcode": "111",
    }
    handler = _handler(api)
    handler._photo_callback = lambda *a, **k: {
        "name": "Existing", "producer": "New Prod", "vintage": 2020,
        "barcode": "222", "wine_type": "RE", "country": "FR", "abv": 13.5,
    }
    update = MagicMock()
    update.effective_chat.id = 4242
    status = _status_msg()
    update.message.reply_text = AsyncMock(return_value=status)
    await handler._handle_edit_photo(update, MagicMock(), 113, b"PHOTO")
    text = status.edit_text.call_args.args[0]
    assert "Would change" in text
    labels = [
        b.text
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("apply those changes" in t.lower() for t in labels)
    assert any("fill blanks" in t.lower() for t in labels)
    assert handler.pending_store["_edit_113"]["photo_data"] == b"PHOTO"


@pytest.mark.asyncio
async def test_wrong_wine_hint_keeps_original_photo():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.search_wines.return_value = []
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    handler._identify_callback = lambda *a, **k: {
        "name": "Correct Wine", "producer": "Estate", "vintage": 2020,
        "wine_type": "RE", "country": "ZA",
    }
    captured = {}

    async def capture(status_msg, data, photo_data=b"", update=None, barcode="", reuse_key=""):
        captured["photo"] = photo_data
        captured["name"] = data.get("name")

    handler._handle_triage_label = capture
    handler._start_misread(
        4242, barcode="_label_1", wrong_name="Wrong Wine", photo_data=b"FRONTJPEG",
    )
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.reply_text = AsyncMock(return_value=_status_msg())
    assert await handler._maybe_identify_misread_text(update, "Correct Wine Estate 2020")
    assert captured["photo"] == b"FRONTJPEG"
    assert captured["name"] == "Correct Wine"


@pytest.mark.asyncio
async def test_shop_label_does_not_offer_update_for_appellation_neighbours():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = HH6_CELLAR
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(DEZALEY_QUERY), photo_data=b"FRONT")
    await _drain()
    buttons = [
        b
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    labels = [b.text for b in buttons]
    assert not any("Calamin" in t for t in labels)
    assert not any("St-Saphorin" in t or "Saphorin" in t for t in labels)
    assert any(t.startswith("✏️ Update") and "Union Vinicole Cully" in t for t in labels)
    assert any("2025" in t for t in labels)
    update_btn = next(b for b in buttons if b.text.startswith("✏️ Update"))
    assert "w:109" in update_btn.callback_data
    assert not any("w:52" in b.callback_data for b in buttons)
    assert not any("w:105" in b.callback_data for b in buttons)
    assert not any("w:107" in b.callback_data for b in buttons)
    assert not any("w:110" in b.callback_data for b in buttons)


@pytest.mark.asyncio
async def test_add_photo_info_starts_edit_flow():
    api = MagicMock()
    api.household_id = 1
    api.update_wine.return_value = {"name": "L'Originelle"}
    handler = _handler(api)
    tap, query = _tap(handler, _cb("ai", wine_id=200))
    await handler._handle_callback(tap, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "Edit:" in text
    assert "L'Originelle" in text
    assert "_edit_200" in handler.pending_store


EXISTING_CHENIN = {
    "id": 68, "name": "Old Vine Chenin Blanc", "producer": "Bosman",
    "vintage": 2020, "stock_count": 2, "barcode": "6001246543210",
    "items": [{"storage": "Fridge", "item_id": 4}],
}


@pytest.mark.asyncio
async def test_existing_wine_card_offers_drank_this():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [EXISTING_CHENIN]
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), photo_data=b"FRONT")
    await _drain()
    labels = [
        b.text
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(t.startswith("✏️ Update") for t in labels)
    assert DRANK_THIS_LABEL in labels
    drank = next(
        b
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
        if b.text == DRANK_THIS_LABEL
    )
    assert "a:dx," in drank.callback_data
    assert "w:68" in drank.callback_data


@pytest.mark.asyncio
async def test_match_card_offers_drank_this():
    neighbour = {
        "id": 70, "name": "Old Vine Chenin", "producer": "Bosman Family",
        "vintage": 2020, "stock_count": 1, "barcode": "",
        "items": [{"storage": "Cellar", "item_id": 8}],
    }
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [EXISTING_CHENIN, neighbour]
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), photo_data=b"FRONT")
    await _drain()
    text = status.edit_text.call_args.args[0]
    assert "Is this one of these" in text or "already in your cellar" in text
    labels = [
        b.text
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert DRANK_THIS_LABEL in labels


@pytest.mark.asyncio
async def test_label_add_offers_existing_wine_instead_of_creating():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = [EXISTING_CHENIN]
    handler = _handler(api)
    handler.pending_store["_label_3"] = {
        "grok_merged": dict(LABEL),
        "time": time.time(),
        "household_id": 1,
        "barcode": "_label_3",
    }
    update, query = _grok_query("_label_3")
    await handler._handle_callback(update, MagicMock())
    api.create_wine.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "Old Vine Chenin" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(t.startswith("Yes, that's") for t in labels)


@pytest.mark.asyncio
async def test_label_add_uses_existing_wine_when_create_returns_409():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.create_wine.return_value = {
        "error": "duplicate_wine", "id": 68, "name": "Old Vine Chenin Blanc",
    }
    api.list_storages.return_value = [
        {"id": 2, "name": "Fridge", "bottles": 1, "drawers": 0},
        {"id": 3, "name": "Kitchen", "bottles": 0, "drawers": 0},
    ]
    handler = _handler(api)
    handler.pending_store["_label_4"] = {
        "grok_merged": dict(LABEL),
        "time": time.time(),
        "household_id": 1,
        "barcode": "_label_4",
    }
    update, query = _grok_query("_label_4")
    await handler._handle_callback(update, MagicMock())
    api.create_wine.assert_called_once()
    text = query.edit_message_text.call_args.args[0]
    assert "Where are you storing" in text or "Old Vine Chenin" in text
    assert "Error creating wine" not in text


@pytest.mark.asyncio
async def test_drank_this_on_existing_wine_removes_from_its_only_storage():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = EXISTING_CHENIN
    api.remove_stock.return_value = {
        "wine_name": "Old Vine Chenin Blanc", "stock_count": 1, "item_id": 4,
    }
    handler = _handler(api)
    tap, query = _tap(handler, _cb("dx", wine_id=68))
    await handler._handle_callback(tap, MagicMock())
    api.remove_stock.assert_called_once()
    assert api.remove_stock.call_args.args[0] == 68
    assert api.remove_stock.call_args.kwargs.get("storage_name") == "Fridge"
    text = query.edit_message_text.call_args.args[0]
    assert "Drank" in text


@pytest.mark.asyncio
async def test_drank_this_asks_which_storage_when_wine_is_in_two():
    split = {
        **EXISTING_CHENIN,
        "items": [
            {"storage": "Fridge", "item_id": 4},
            {"storage": "Cellar", "item_id": 9},
        ],
    }
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = split
    api.list_storages.return_value = [
        {"id": 2, "name": "Fridge"},
        {"id": 3, "name": "Cellar"},
    ]
    handler = _handler(api)
    tap, query = _tap(handler, _cb("dx", wine_id=68))
    await handler._handle_callback(tap, MagicMock())
    api.remove_stock.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert "fridge" in text.lower() or "Fridge" in text
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "Fridge" in labels
    assert "Cellar" in labels
