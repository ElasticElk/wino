"""A button older than ~48h must not crash the callback handler.

Telegram stops sending the message body for old callbacks and substitutes an
InaccessibleMessage. It is truthy, so every `if query.message` guard in
_handle_callback sails past it, and the first attribute touched — chat_id,
text_html, caption, reply_markup — raises AttributeError. That lands in
_on_error, which is where the generic "Something went wrong handling that"
reply comes from: the message that says a stale button is the *likely* cause
was itself produced by one.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, InaccessibleMessage

from integrations.telegram_bot import TelegramBotHandler


def _handler():
    api = MagicMock()
    api.household_id = 1
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


def _stale_query(data="a:wl,p:0"):
    query = AsyncMock()
    query.data = data
    query.message = InaccessibleMessage(
        chat=Chat(id=4242, type=Chat.PRIVATE), message_id=7,
    )
    return query


def test_inaccessible_message_raises_on_the_attributes_the_handler_reads():
    """Guards the premise: this is why the crash happened at all."""
    msg = InaccessibleMessage(chat=Chat(id=1, type=Chat.PRIVATE), message_id=5)
    assert bool(msg) is True, "truthy, so `if query.message` does not catch it"
    for attr in ("chat_id", "text", "text_html", "caption", "reply_markup"):
        with pytest.raises(AttributeError):
            getattr(msg, attr)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["a:wl,p:0", "a:swh,w:2", "a:look", "a:wlf,p:1", "cmd:fridge", "a:+,w:3"],
)
async def test_stale_button_explains_itself_instead_of_crashing(data):
    handler = _handler()
    update = MagicMock()
    update.callback_query = _stale_query(data)
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await handler._handle_callback(update, context)

    context.bot.send_message.assert_awaited_once()
    kwargs = context.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 4242
    assert "two days old" in kwargs["text"]


@pytest.mark.asyncio
async def test_stale_button_still_clears_the_spinner():
    """Without answer(), the button sits spinning in the client."""
    handler = _handler()
    update = MagicMock()
    update.callback_query = _stale_query()
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await handler._handle_callback(update, context)

    update.callback_query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_answer_failing_does_not_stop_the_explanation():
    """Telegram rejects answers to callbacks this old; the reply must still go."""
    handler = _handler()
    update = MagicMock()
    update.callback_query = _stale_query()
    update.callback_query.answer.side_effect = Exception("query is too old")
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await handler._handle_callback(update, context)

    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_message_is_dropped_quietly():
    """No chat to answer in — log and return, never raise."""
    handler = _handler()
    update = MagicMock()
    query = AsyncMock()
    query.data = "a:wl,p:0"
    query.message = None
    update.callback_query = query
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await handler._handle_callback(update, context)

    context.bot.send_message.assert_not_awaited()
