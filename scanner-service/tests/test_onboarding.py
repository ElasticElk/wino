"""Front-door behaviour: the bot must always answer a stranger.

Telegram shows a START button on first open. When that went unanswered the bot
looked broken, and there was no discoverable way to get a chat ID — which is the
most likely reason six of nine households never added a bottle.
"""

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import TelegramBotHandler, empty_cellar


def _update(chat_id, text="/start"):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.first_name = "Sam"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


@pytest.fixture
def handler():
    h = TelegramBotHandler.__new__(TelegramBotHandler)
    h.pending_store = {}
    h._lock = threading.Lock()
    h.chat_ids = ["111", "222"]
    h._chat_id_to_api = {"111": MagicMock()}
    h.api = h._chat_id_to_api["111"]
    h._invites = None
    h._misread_corrections = {}
    h._chat_id_to_country = {"111": "GB", "222": "GB"}
    h._chat_id_to_currency = {}
    h._country_asked = set()
    h.admin_chat_ids = []
    return h


def test_known_chat_is_recognised(handler):
    assert handler._is_known(_update(111)) is True
    assert handler._is_known(_update("111")) is True


def test_unknown_chat_is_not_recognised(handler):
    assert handler._is_known(_update(999)) is False


@pytest.mark.asyncio
async def test_stranger_is_pointed_at_an_invite_not_a_chat_id(handler):
    handler._invites = None
    u = _update(999)
    await handler._greet_stranger(u)

    u.message.reply_text.assert_awaited_once()
    body = u.message.reply_text.call_args.args[0]
    assert "invite" in body.lower()
    # Chat ID stays on /myid. Leading with it was the handshake we killed.
    assert "<code>999</code>" not in body
    assert "/myid" in body
    assert "old path" not in body
    assert "If they ask for a chat ID" in body


@pytest.mark.asyncio
async def test_start_asks_country_when_unset(handler):
    handler._chat_id_to_country["111"] = ""
    handler.api.get_household.return_value = {"name": "Curl Curl", "id": 12}
    u = _update(111)
    ctx = MagicMock()
    ctx.args = []
    ctx.bot.send_message = AsyncMock()
    await handler._cmd_start(u, ctx)
    ctx.bot.send_message.assert_awaited()
    assert "Where should I price wines?" in ctx.bot.send_message.call_args.kwargs["text"]
    ctx.bot.send_message.reset_mock()
    await handler._cmd_start(u, ctx)
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_start_and_help_say_whiney_and_escape_names(handler):
    handler.admin_chat_ids = []
    handler.api.get_household.return_value = {"name": "A & B", "id": 7}
    u = _update(111)
    u.effective_user.first_name = "Ben & Sue"
    ctx = MagicMock()
    ctx.args = []
    await handler._cmd_start(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert "Wine Fridge Scanner" not in body
    assert "Ben &amp; Sue" in body
    assert "A &amp; B" in body
    assert "/status" not in body

    h = _update(111, "/help")
    await handler._cmd_help(h, ctx)
    help_body = h.message.reply_text.call_args.args[0]
    assert "🍷 <b>Whiney</b>" in help_body
    assert "Wine Fridge Scanner" not in help_body


@pytest.mark.asyncio
async def test_command_clears_known_user_naming(handler):
    flow = handler._invite_flow()
    flow.remember("111", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    await handler._abandon_invite_if_command(_update(111, "/start K9m2Pq4r"), MagicMock())
    assert flow.state_for("111") is not None
    await handler._abandon_invite_if_command(_update(111, "/fridge"), MagicMock())
    assert flow.state_for("111") is None

    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    await handler._abandon_invite_if_command(_update(999, "/fridge"), MagicMock())
    assert flow.state_for("999") is not None


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
    handler._invites = None
    stranger = _update(999)
    ctx = MagicMock()
    ctx.args = []
    await handler._cmd_start(stranger, ctx)
    body = stranger.message.reply_text.call_args.args[0]
    assert "invite" in body.lower()
    assert "<code>999</code>" not in body


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
    # No command dump — the camera is the affordance.
    assert "/fridge" not in body
    assert "/help" not in body
    assert "/invite" in body


def test_empty_state_invites_a_photo_and_never_mentions_scanning():
    text, keyboard = empty_cellar()
    assert "photo" in text.lower()
    # "Scan a bottle first!" pointed at hardware being parked.
    assert "scan" not in text.lower()
    assert keyboard.inline_keyboard  # must offer a way forward, not just prose


def test_empty_state_takes_a_context_line():
    text, _ = empty_cellar("Yskas is empty")
    assert "Yskas is empty" in text


def test_empty_state_mentions_shelves_when_unconfigured():
    text, _ = empty_cellar("Your cellar is empty", unconfigured=True, storage_name="Cellar")
    assert "Cellar has no shelves" in text
    assert "photo" in text.lower()


@pytest.mark.asyncio
async def test_a_pasted_link_is_not_searched_for(handler):
    """Someone shared a wine-list URL and the bot searched the cellar for it."""
    handler._do_search = AsyncMock()
    u = _update(111, "WINE LIST — Saviano's\nhttps://share.google/VTAfvGok")

    await handler._unknown_text(u, MagicMock())

    handler._do_search.assert_not_awaited()
    reply = u.message.reply_text.call_args.args[0]
    assert "screenshot" in reply.lower()


@pytest.mark.asyncio
async def test_plain_text_still_searches(handler):
    handler._do_search = AsyncMock()
    u = _update(111, "chenin")

    await handler._unknown_text(u, MagicMock())

    handler._do_search.assert_awaited_once()
