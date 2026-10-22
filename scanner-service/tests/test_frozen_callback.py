"""Callback taps must not setattr on python-telegram-bot's frozen CallbackQuery.

Since scanner 2026-10-18.1, _dispatch_callback did
`query.edit_message_text = _QuietEdit(...)`. CallbackQuery is frozen, so every
tap raised AttributeError before the [tap] log. MagicMock hid this.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, Chat, Message, User

from integrations.telegram_bot import TelegramBotHandler, _cb


class FrozenCallbackQuery:
    """Stands in for telegram.CallbackQuery: methods work, assignment does not."""

    def __init__(self, data: str):
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "id", "1")
        object.__setattr__(self, "chat_instance", "x")
        msg = MagicMock()
        msg.chat_id = 4242
        msg.message_id = 7
        object.__setattr__(self, "message", msg)
        object.__setattr__(self, "edit_message_text", AsyncMock())
        object.__setattr__(self, "answer", AsyncMock())

    def __setattr__(self, name, value):
        raise AttributeError(
            f"Attribute '{name}' of class 'CallbackQuery' can't be set!"
        )


def _handler():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


def test_real_callback_query_cannot_set_edit_message_text():
    """Guards the premise: this is why every tap crashed."""
    user = User(id=1, is_bot=False, first_name="Alex")
    chat = Chat(id=4242, type=Chat.PRIVATE)
    msg = Message(message_id=7, date=datetime.now(timezone.utc), chat=chat)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="x", data="a:x", message=msg,
    )
    with pytest.raises(AttributeError, match="can't be set"):
        query.edit_message_text = None


@pytest.mark.asyncio
async def test_frozen_callback_query_cancel_logs_tap_and_edits(caplog):
    handler = _handler()
    query = FrozenCallbackQuery(_cb("x"))
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242

    with caplog.at_level(logging.INFO, logger="integrations.telegram_bot"):
        await handler._handle_callback(update, MagicMock())

    assert "[tap]" in caplog.text
    assert "action=x" in caplog.text
    query.edit_message_text.assert_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert text == "Cancelled."
    query.answer.assert_awaited()
