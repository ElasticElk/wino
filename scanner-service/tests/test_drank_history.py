"""/recent — last bottles tagged drank or removed, with notes, label, pagination."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import (
    DRANK_PAGE_SIZE,
    TelegramBotHandler,
    _cb,
    build_drank_history_view,
    build_drank_notes_view,
    format_drank_at,
)


def _handler(api):
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


def _item(i, *, has_image=False, comment="Crisp and dry.", vote=""):
    return {
        "item_id": i,
        "wine_id": 10 + i,
        "wine_name": f"Wine {i} 2020",
        "producer": "Bosman",
        "comment": comment,
        "wine_type": "WH",
        "has_image": has_image,
        "storage": "Fridge",
        "vote": vote,
        "drank_at": "2026-04-10T12:00:00Z",
    }


def test_format_drank_at_relative():
    now = datetime.now(timezone.utc)
    assert format_drank_at((now - timedelta(minutes=10)).isoformat()) == "just now"
    assert format_drank_at((now - timedelta(days=1)).isoformat()) == "yesterday"
    assert "days ago" in format_drank_at((now - timedelta(days=3)).isoformat())
    assert format_drank_at("") == ""


def test_history_page_lists_names_notes_and_label_buttons():
    payload = {
        "count": 12,
        "offset": 0,
        "limit": 5,
        "items": [
            _item(1, has_image=True, comment="Green apple."),
            _item(2, has_image=False, comment=""),
            _item(3, has_image=True, vote="up"),
            _item(4),
            _item(5),
        ],
    }
    text, markup = build_drank_history_view(payload)
    assert "Recent bottles" in text
    assert "1–5 of 12" in text or "1\u20135 of 12" in text
    assert "Wine 1 2020" in text
    assert "Green apple." in text
    assert "Wine 2 2020" in text

    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Notes" in t and t.startswith("\U0001F4DD 1") for t in labels)
    assert any("Label" in t for t in labels)
    assert any("Older 5" in t for t in labels)
    assert not any("Newer 5" in t for t in labels)

    # Wine 2 has no image — its row is notes only.
    rows = markup.inline_keyboard
    wine2_row = rows[1]
    assert len(wine2_row) == 1
    assert "Notes" in wine2_row[0].text


def test_history_page_older_and_newer():
    payload = {
        "count": 12,
        "offset": 5,
        "limit": 5,
        "items": [_item(i) for i in range(6, 11)],
    }
    text, markup = build_drank_history_view(payload)
    assert "6\u201310 of 12" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Newer 5" in t for t in labels)
    assert any("Older 5" in t for t in labels)
    nav = markup.inline_keyboard[-1]
    assert _parse_page(nav[0].callback_data) == 0
    assert _parse_page(nav[1].callback_data) == 2


def _parse_page(data: str) -> int:
    from integrations.telegram_bot import _parse_cb
    return _parse_cb(data)["page"]


def test_empty_history():
    text, markup = build_drank_history_view(
        {"count": 0, "offset": 0, "limit": 5, "items": []},
    )
    assert "Nothing here yet" in text
    assert markup is None


def test_notes_view_back_keeps_the_page():
    wine = {"name": "Chenin Blanc", "producer": "Bosman", "comment": "Wet stone."}
    text, markup = build_drank_notes_view(wine, page=2)
    assert "Chenin Blanc" in text
    assert "Wet stone." in text
    assert "Bosman" in text
    back = markup.inline_keyboard[0][0]
    from integrations.telegram_bot import _parse_cb
    parsed = _parse_cb(back.callback_data)
    assert parsed["action"] == "dh"
    assert parsed["page"] == 2


@pytest.mark.asyncio
async def test_cmd_recent_renders_the_first_page():
    api = MagicMock()
    api.household_id = 1
    api.stock_history.return_value = {
        "count": 1, "offset": 0, "limit": 5,
        "items": [_item(1, has_image=True, comment="Green apple.")],
    }
    handler = _handler(api)
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.reply_text = AsyncMock()

    await handler._cmd_recent(update, MagicMock())

    api.stock_history.assert_called_once_with(offset=0, limit=DRANK_PAGE_SIZE)
    body = update.message.reply_text.call_args.args[0]
    assert "Wine 1 2020" in body
    assert "Green apple." in body
    assert update.message.reply_text.call_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_older_page_callback_fetches_the_next_five():
    api = MagicMock()
    api.household_id = 1
    api.stock_history.return_value = {
        "count": 12, "offset": 5, "limit": 5,
        "items": [_item(i) for i in range(6, 11)],
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("dh", page=1)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query

    await handler._handle_callback(update, MagicMock())

    api.stock_history.assert_called_with(offset=5, limit=5)
    body = query.edit_message_text.call_args.args[0]
    assert "Wine 6 2020" in body


@pytest.mark.asyncio
async def test_notes_callback_shows_comment_and_back():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = {
        "id": 11, "name": "Chenin Blanc", "producer": "Bosman",
        "comment": "Green apple and wet stone.",
    }
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("dn", wine_id=11, page=1)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query

    await handler._handle_callback(update, MagicMock())

    api.get_wine_detail.assert_called_once_with(11)
    body = query.edit_message_text.call_args.args[0]
    assert "Green apple and wet stone." in body
    markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    from integrations.telegram_bot import _parse_cb
    parsed = _parse_cb(markup.inline_keyboard[0][0].callback_data)
    assert parsed == {"action": "dh", "page": 1}


@pytest.mark.asyncio
async def test_label_callback_sends_the_photo():
    api = MagicMock()
    api.household_id = 1
    api.get_wine_detail.return_value = {"id": 11, "name": "Chenin Blanc"}
    api.get_wine_images.return_value = [{"id": 99}]
    api.get_image_file.return_value = b"jpeg-bytes"
    handler = _handler(api)
    query = AsyncMock()
    query.data = _cb("di", wine_id=11, page=0)
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_photo = AsyncMock()
    update = MagicMock()
    update.callback_query = query

    await handler._handle_callback(update, MagicMock())

    api.get_image_file.assert_called_once_with(99)
    query.message.reply_photo.assert_awaited_once()
    assert query.message.reply_photo.call_args.kwargs["photo"] == b"jpeg-bytes"
    caption = query.message.reply_photo.call_args.kwargs["caption"]
    assert "Chenin Blanc" in caption
