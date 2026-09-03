"""Front-door behaviour: the bot must always answer a stranger.

Telegram shows a START button on first open. When that went unanswered the bot
looked broken, and there was no discoverable way to get a chat ID — which is the
most likely reason six of nine households never added a bottle.
"""

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import TelegramBotHandler, empty_cellar


@pytest.fixture
def handler():
    h = TelegramBotHandler.__new__(TelegramBotHandler)
    h.pending_store = {}
    h._lock = threading.Lock()
    h.chat_ids = ["111", "222"]
    h._chat_id_to_api = {}
    h.api = None
    return h


def _update(chat_id, text="/start"):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.first_name = "Sam"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def test_known_chat_is_recognised(handler):
    assert handler._is_known(_update(111)) is True
    assert handler._is_known(_update("111")) is True


def test_unknown_chat_is_not_recognised(handler):
    assert handler._is_known(_update(999)) is False


@pytest.mark.asyncio
async def test_stranger_gets_their_chat_id(handler):
    u = _update(999)
    await handler._greet_stranger(u)

    u.message.reply_text.assert_awaited_once()
    body = u.message.reply_text.call_args.args[0]
    # The chat ID is the one thing an owner needs to run /adduser.
    assert "999" in body
    assert "<code>999</code>" in body


@pytest.mark.asyncio
async def test_fallback_answers_strangers_and_ignores_members(handler):
    stranger = _update(999, "hello?")
    await handler._stranger_fallback(stranger, MagicMock())
    stranger.message.reply_text.assert_awaited_once()

    # A provisioned user must never reach the fallback's reply — their own
    # handlers run first, and double-answering would be worse than silence.
    member = _update(111, "hello?")
    await handler._stranger_fallback(member, MagicMock())
    member.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_branches_on_provisioning(handler):
    stranger = _update(999)
    await handler._cmd_start(stranger, MagicMock())
    assert "999" in stranger.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_welcome_reports_failure_when_user_never_opened_the_bot(handler):
    """A bot cannot open a conversation, so this genuinely fails in practice."""
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock(side_effect=Exception("Forbidden: bot can't initiate conversation"))

    delivered = await handler._welcome_new_member(ctx, "999", "Sam", "Riverside")

    assert delivered is False  # the adder must be told, not shown a false success


@pytest.mark.asyncio
async def test_welcome_tells_them_how_to_add_a_wine(handler):
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()

    delivered = await handler._welcome_new_member(ctx, "999", "Sam", "Riverside")

    assert delivered is True
    body = ctx.bot.send_message.call_args.kwargs["text"]
    assert "Riverside" in body
    assert "photo" in body.lower()


def test_empty_state_invites_a_photo_and_never_mentions_scanning():
    text, keyboard = empty_cellar()
    assert "photo" in text.lower()
    # "Scan a bottle first!" pointed at hardware being parked.
    assert "scan" not in text.lower()
    assert keyboard.inline_keyboard  # must offer a way forward, not just prose


def test_empty_state_takes_a_context_line():
    text, _ = empty_cellar("Yskas is empty")
    assert "Yskas is empty" in text
