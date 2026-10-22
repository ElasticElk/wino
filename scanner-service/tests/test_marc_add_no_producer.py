"""Marc's first bottle: empty producer, silent Add to cellar, typed Confirm.

Grok read "G.W.M Grenache Noir" with no producer. Tapping Add stripped the
keyboard; typing Confirm was stolen by the misread fallback. No wine created.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import (
    ADD_TO_CELLAR,
    ADD_WITHOUT_PRODUCER,
    WHO_MAKES_THIS,
    TelegramBotHandler,
    _cb,
    _plausible_producer,
    _suggested_producer,
)


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


class ClaimDict(dict):
    def __init__(self):
        super().__init__()
        self._held = set()

    def try_claim(self, key):
        if key in self._held:
            return False
        self._held.add(key)
        return True

    def release_claim(self, key):
        self._held.discard(key)


MARC = {
    "type": "label",
    "name": "G.W.M Grenache Noir",
    "producer": None,
    "wine_type": "RE",
}


def _handler(api, pending=None):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock):
        api.list_all_wines.return_value = []
    if isinstance(getattr(api.list_storages, "return_value", None), MagicMock):
        api.list_storages.return_value = [
            {"name": "Cellar", "id": 35, "bottles": 0},
            {"name": "Kitchen", "id": 36, "bottles": 0},
        ]
    if isinstance(getattr(api, "web_url", None), MagicMock) or not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
    api.lookup_references.return_value = {"updated": False}
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api,
        pending_store=pending if pending is not None else ClaimDict(),
    )


def _photo_update():
    status = AsyncMock()
    status.chat_id = 4242
    status.message_id = 7
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.chat_id = 4242
    update.message.message_id = 9
    update.message.reply_text = AsyncMock(return_value=status)
    return update, status


def _text_update(text):
    msg = AsyncMock()
    msg.text = text
    msg.chat_id = 4242
    msg.message_id = 11
    update = MagicMock()
    update.message = msg
    update.effective_chat.id = 4242
    return update


async def _drain():
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _show_card(handler, grok=None):
    update, status = _photo_update()
    await handler._handle_triage_label(
        status, dict(grok or MARC), photo_data=b"JPEG", update=update,
    )
    await _drain()
    return update, status


def test_suggested_producer_from_initials():
    assert _suggested_producer("G.W.M Grenache Noir") == "G.W.M"
    assert _suggested_producer("Bosman Chenin Blanc") == "Bosman"
    assert _suggested_producer("Cepa Lebrel") == ""


def test_plausible_producer_rejects_questions_and_long_text():
    assert _plausible_producer("G.W.M")
    assert not _plausible_producer("What wines do I have and where?")
    assert not _plausible_producer("one two three four five")
    assert not _plausible_producer("Confirm")
    assert not _plausible_producer("add to cellar")


@pytest.mark.asyncio
async def test_photo_then_tap_add_keeps_buttons_and_releases_claim(caplog):
    api = MagicMock()
    api.household_id = 14
    store = ClaimDict()
    handler = _handler(api, store)
    await _show_card(handler)
    assert "_label_1" in handler.pending_store

    query = FrozenCallbackQuery(_cb("grok", barcode="_label_1"))
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    with caplog.at_level(logging.INFO, logger="integrations.telegram_bot"):
        await handler._handle_callback(update, MagicMock())

    api.create_wine.assert_not_called()
    assert "reason=no_producer" in caplog.text
    text = query.edit_message_text.call_args.args[0]
    assert text == WHO_MAKES_THIS
    markup = query.edit_message_text.call_args.kwargs.get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(t == ADD_WITHOUT_PRODUCER or "Add without producer" in t for t in labels)
    assert any("Cancel" in t for t in labels)
    assert any("G.W.M" in t for t in labels)
    assert store.try_claim("_label_1") is True
    store.release_claim("_label_1")


@pytest.mark.asyncio
async def test_typed_confirm_does_not_search_or_misread():
    api = MagicMock()
    api.household_id = 14
    handler = _handler(api)
    handler._identify_callback = MagicMock(side_effect=AssertionError("misread"))
    await _show_card(handler)

    query = FrozenCallbackQuery(_cb("grok", barcode="_label_1"))
    tap = MagicMock()
    tap.callback_query = query
    tap.effective_chat.id = 4242
    await handler._handle_callback(tap, MagicMock())
    api.create_wine.assert_not_called()

    update = _text_update("Confirm")
    await handler._unknown_text(update, MagicMock())
    api.search_wines.assert_not_called()
    handler._identify_callback.assert_not_called()
    if api.create_wine.called:
        return
    replies = [
        c.args[0] for c in update.message.reply_text.call_args_list if c.args
    ]
    edits = [
        c.args[0] for c in query.edit_message_text.call_args_list if c.args
    ]
    texts = replies + edits
    assert any(WHO_MAKES_THIS == t or "Who makes this" in str(t) for t in texts)
    markups = []
    for call in list(update.message.reply_text.call_args_list) + list(
        query.edit_message_text.call_args_list
    ):
        markup = (call.kwargs or {}).get("reply_markup")
        if markup is not None:
            markups.append(markup)
    assert markups
    labels = [b.text for m in markups for row in m.inline_keyboard for b in row]
    assert labels


@pytest.mark.asyncio
async def test_typed_producer_creates_wine_and_shows_storage_picker():
    api = MagicMock()
    api.household_id = 14
    api.create_wine.return_value = {"id": 101, "name": "G.W.M Grenache Noir"}
    handler = _handler(api)
    handler._identify_callback = MagicMock(side_effect=AssertionError("misread"))
    await _show_card(handler)

    query = FrozenCallbackQuery(_cb("grok", barcode="_label_1"))
    tap = MagicMock()
    tap.callback_query = query
    tap.effective_chat.id = 4242
    await handler._handle_callback(tap, MagicMock())

    update = _text_update("G.W.M")
    await handler._unknown_text(update, MagicMock())
    api.search_wines.assert_not_called()
    handler._identify_callback.assert_not_called()
    api.create_wine.assert_called_once()
    payload = api.create_wine.call_args.args[0]
    assert payload["name"] == "G.W.M Grenache Noir"
    assert payload["producer"] == "G.W.M"
    replies = " ".join(
        str(c.args[0]) for c in update.message.reply_text.call_args_list if c.args
    )
    assert "Where are you storing" in replies
    markup = None
    for call in update.message.reply_text.call_args_list:
        markup = (call.kwargs or {}).get("reply_markup") or markup
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Cellar" in t for t in labels)


@pytest.mark.asyncio
async def test_question_while_producer_prompt_is_not_the_producer():
    api = MagicMock()
    api.household_id = 14
    api.search_wines.return_value = []
    handler = _handler(api)
    await _show_card(handler)

    query = FrozenCallbackQuery(_cb("grok", barcode="_label_1"))
    tap = MagicMock()
    tap.callback_query = query
    tap.effective_chat.id = 4242
    await handler._handle_callback(tap, MagicMock())
    assert "4242" in handler._await_producer

    update = _text_update("What wines do I have and where?")
    await handler._unknown_text(update, MagicMock())
    api.create_wine.assert_not_called()
    merged = handler.pending_store["_label_1"]["grok_merged"]
    assert not merged.get("producer")
    api.search_wines.assert_called_once_with("What wines do I have and where?")
    assert "4242" in handler._await_producer


@pytest.mark.asyncio
async def test_typed_confirm_with_no_card_says_send_a_photo():
    api = MagicMock()
    api.household_id = 14
    handler = _handler(api)
    update = _text_update("add to cellar")
    await handler._unknown_text(update, MagicMock())
    api.search_wines.assert_not_called()
    assert "Nothing waiting to add" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_label_card_offers_guessed_producer_button():
    api = MagicMock()
    api.household_id = 14
    handler = _handler(api)
    _, status = await _show_card(handler)
    labels = [
        b.text
        for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert ADD_TO_CELLAR in labels
    assert any("Producer: G.W.M" in t for t in labels)
    assert not any("Confirm" in t for t in labels)
