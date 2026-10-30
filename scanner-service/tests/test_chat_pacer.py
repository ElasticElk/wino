"""Reply pacing must wrap frozen Telegram objects, never monkeypatch them."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User

from integrations.chat_intent import (
    PacedMessage,
    PacedUpdate,
    ReplyPacer,
    pace_update,
)


def _ptb_message(text="hello"):
    user = User(id=1, first_name="Sam", is_bot=False)
    chat = Chat(id=4242, type="private")
    return Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )


def _ptb_update(text="hello"):
    return Update(update_id=11, message=_ptb_message(text))


def test_real_telegram_message_rejects_reply_text_assignment():
    message = _ptb_message()
    with pytest.raises(AttributeError, match="can't be set"):
        message.reply_text = None
    assert isinstance(message, Message)


def test_real_telegram_update_rejects_callback_query_assignment():
    update = _ptb_update()
    with pytest.raises(AttributeError, match="can't be set"):
        update.callback_query = object()
    assert isinstance(update, Update)


def test_pace_update_wraps_real_update_without_mutating():
    update = _ptb_update("what should I drink")
    pacer = ReplyPacer(lambda: 0.0, AsyncMock(), 1000)
    paced = pace_update(update, pacer)
    assert isinstance(paced, PacedUpdate)
    assert paced is not update
    assert paced.message is not update.message
    assert isinstance(paced.message, PacedMessage)
    assert paced.message.message_id == 7
    assert paced.message.text == "what should I drink"
    assert paced.effective_message.text == "what should I drink"
    assert paced.effective_chat.id == 4242
    assert paced.effective_user.id == 1
    assert update.message.text == "what should I drink"
    assert not hasattr(update, "_whiney_pace_probe")
    assert not hasattr(update.message, "_whiney_pace_probe")
    with pytest.raises(AttributeError, match="can't be set"):
        update.message.reply_text = AsyncMock()


def test_pace_update_leaves_mocks_alone():
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = "hi"
    pacer = ReplyPacer(lambda: 0.0, AsyncMock(), 1000)
    assert pace_update(update, pacer) is update
    assert pace_update(update, None) is update
    zero = ReplyPacer(lambda: 0.0, AsyncMock(), 0)
    real = _ptb_update()
    assert pace_update(real, zero) is real


def test_pace_update_leaves_setattr_raising_fakes_alone():
    """A freeze-probe would wrap this. isinstance must not."""

    class FrozenFake:
        def __setattr__(self, name, value):
            raise AttributeError("can't be set")

    inner = FrozenFake()
    fake = SimpleNamespace(message=inner)
    pacer = ReplyPacer(lambda: 0.0, AsyncMock(), 1000)
    assert pace_update(fake, pacer) is fake


@pytest.mark.asyncio
async def test_paced_message_waits_once_then_delegates():
    inner = SimpleNamespace(
        message_id=7,
        text="hello",
        chat=SimpleNamespace(id=4242),
        reply_text=AsyncMock(return_value="sent"),
        reply_html=AsyncMock(return_value="html"),
    )
    slept = []

    async def sleep(delay):
        slept.append(delay)

    pacer = ReplyPacer(lambda: 0.0, sleep, 1000)
    wrapped = PacedMessage(inner, pacer)
    assert wrapped.message_id == 7
    first = wrapped.reply_text
    assert wrapped.reply_text is first
    result = await wrapped.reply_text("hi", parse_mode="HTML")
    assert result == "sent"
    assert slept == [1.0]
    inner.reply_text.assert_awaited_once()
    await wrapped.reply_html("again")
    assert slept == [1.0]
    inner.reply_html.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_text_paces_link_replies_on_mocks():
    from test_chat_router import FakeChat, _api, _context, _handler, _text, _wine

    slept = []

    async def sleep(delay):
        slept.append(delay)

    handler = _handler(_api([_wine()]), FakeChat())
    handler._min_reply_ms = 1000
    handler._router._sleep = sleep
    handler._router._now = lambda: 0.0
    update, _sent = _text("https://example.test/list")
    original = update.message.reply_text
    await handler._unknown_text(update, _context())
    assert slept == [1.0]
    original.assert_awaited()
    body = original.await_args.args[0]
    assert "screenshot" in body.lower() or "skermkiekie" in body.lower()
    assert update.message.reply_text is original


@pytest.mark.asyncio
async def test_unknown_text_on_real_update_does_not_assign_reply_text():
    from test_chat_router import FakeChat, _api, _context, _handler, _wine

    handler = _handler(_api([_wine()]), FakeChat())
    handler._min_reply_ms = 1000

    async def sleep(_delay):
        return None

    handler._router._sleep = sleep
    handler._router._now = lambda: 0.0
    update = _ptb_update("https://example.test/list")
    with pytest.raises(Exception) as caught:
        await handler._unknown_text(update, MagicMock())
    assert "can't be set" not in str(caught.value)
    with pytest.raises(AttributeError, match="can't be set"):
        update.message.reply_text = AsyncMock()
    assert update.message.text == "https://example.test/list"
