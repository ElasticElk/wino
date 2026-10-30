"""To-drink list bot flows. API is mocked; copy, buttons and branching matter."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import catalog_keys, t
from integrations.telegram_bot import (
    ADD_TO_CELLAR,
    CALLBACK_DATA_MAX,
    JUST_LOOKING_NOTE,
    TelegramBotHandler,
    _cb,
)
from integrations.telegram_commands import PUBLIC_COMMANDS, menu_commands
from integrations.telegram_lists import (
    LIST_PAGE_SIZE,
    build_list_view,
    insert_to_drink,
    listed_note,
    to_drink_button,
)

LABEL = {
    "type": "label",
    "name": "Chenin Blanc",
    "producer": "Coastal Cellars",
    "vintage": 2022,
    "wine_type": "WH",
    "country": "ZA",
    "grape_variety": "Chenin Blanc",
    "region": "Stellenbosch",
}


def _handler(api, *, lists=True):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock):
        api.list_all_wines.return_value = []
    if isinstance(getattr(api, "web_url", None), MagicMock) or not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    if lists:
        handler.set_flags({"feature_lists": "on"})
    return handler


def _status_msg():
    msg = AsyncMock()
    msg.chat_id = 4242
    msg.message_id = 7
    return msg


def _update(chat_id=4242, text="/list"):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Sam"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    return u


def _query(data, *, text="card", html=None):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.text = text
    query.message.text_html = html or text
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _labels(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_unmatched_label_keyboard_has_to_drink_when_flag_on():
    api = MagicMock()
    api.household_id = 1
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), b"img")
    labels = _labels(status.edit_text.call_args.kwargs["reply_markup"])
    assert ADD_TO_CELLAR in labels
    assert t("en", "list.btn.add") in labels
    assert any("Just looking" in x for x in labels)


@pytest.mark.asyncio
async def test_unmatched_label_hides_to_drink_when_flag_off():
    api = MagicMock()
    api.household_id = 1
    api.lookup_references.return_value = {"updated": False}
    handler = _handler(api, lists=False)
    status = _status_msg()
    await handler._handle_triage_label(status, dict(LABEL), b"img")
    labels = _labels(status.edit_text.call_args.kwargs["reply_markup"])
    assert t("en", "list.btn.add") not in labels
    assert ADD_TO_CELLAR in labels


@pytest.mark.asyncio
async def test_tap_td_calls_api_appends_note_drops_buttons():
    api = MagicMock()
    api.household_id = 1
    api.list_lists.return_value = {
        "lists": [{"id": 1, "name": "To drink", "kind": "personal", "item_count": 0}],
    }
    api.add_list_item.return_value = {
        "id": 44, "name": "Chenin Blanc", "status_code": 201,
    }
    handler = _handler(api)
    handler.pending_store["_label_1"] = {
        "grok_merged": dict(LABEL), "household_id": 1, "barcode": "_label_1",
    }
    query = _query(_cb("td", barcode="_label_1"), html="Label analysis complete!")
    update = MagicMock()
    update.callback_query = query
    update.effective_user.language_code = "en"
    update.effective_chat.id = 4242
    handled = await handler._list_flow().on_callback(update, MagicMock())
    assert handled is True
    api.add_list_item.assert_called_once()
    posted = api.add_list_item.call_args.args[1]
    assert posted["name"] == "Chenin Blanc"
    text = query.edit_message_text.call_args.args[0]
    assert t("en", "list.note.added") in text
    assert query.edit_message_text.call_args.kwargs.get("reply_markup") in (None, )
    card = handler._label_cards.get("4242:7") or {}
    assert card.get("listed") is True


@pytest.mark.asyncio
async def test_just_looking_does_not_call_list_api():
    api = MagicMock()
    api.household_id = 1
    handler = _handler(api)
    query = _query(_cb("look"), html="Label analysis complete!")
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    update.effective_user = MagicMock()
    await handler._dispatch_callback(update, MagicMock())
    api.add_list_item.assert_not_called()
    api.list_lists.assert_not_called()
    text = query.edit_message_text.call_args.args[0]
    assert JUST_LOOKING_NOTE.strip() in text


@pytest.mark.asyncio
async def test_list_command_empty_and_off():
    api = MagicMock()
    api.list_lists.return_value = {
        "lists": [{"id": 1, "name": "To drink", "kind": "personal", "item_count": 0}],
    }
    api.get_list.return_value = {
        "id": 1, "name": "To drink", "count": 0, "offset": 0, "limit": 5, "items": [],
    }
    handler = _handler(api)
    update = _update()
    await handler._cmd_list(update, MagicMock())
    body = update.message.reply_text.call_args.args[0]
    assert t("en", "list.title") in body or "To drink" in body
    assert t("en", "list.empty").split("\n")[0] in body

    handler.set_flags({})
    off = _update()
    await handler._cmd_list(off, MagicMock())
    assert off.message.reply_text.call_args.args[0] == t("en", "list.off")


@pytest.mark.asyncio
async def test_list_page_move_and_remove():
    api = MagicMock()
    api.list_lists.return_value = {
        "lists": [{"id": 1, "name": "To drink", "kind": "personal", "item_count": 2}],
    }
    api.get_list.return_value = {
        "id": 1,
        "name": "To drink",
        "count": 2,
        "offset": 0,
        "limit": 5,
        "items": [
            {
                "id": 44, "name": "Chenin Blanc", "producer": "Coastal Cellars",
                "vintage": 2022, "wine_type": "WH", "country": "ZA",
                "grape": "Chenin Blanc", "payload": {"region": "Stellenbosch"},
            },
        ],
    }
    api.list_storages.return_value = [{"id": 9, "name": "Fridge", "drawers": 0}]
    api.move_list_item_to_cellar.return_value = {
        "item_id": 9001,
        "wine_id": 55,
        "wine_name": "Chenin Blanc 2022",
        "stock_count": 1,
        "storage": "Fridge",
        "action": "added",
        "achievements": [],
        "list_item_id": 44,
        "status_code": 201,
    }
    api.delete_list_item.return_value = {"status": "removed", "id": 44, "status_code": 200}
    handler = _handler(api)

    shown = build_list_view(api.get_list.return_value, "en")
    assert "Chenin Blanc" in shown[0]
    labels = _labels(shown[1])
    assert t("en", "list.btn.move") in labels
    assert t("en", "list.btn.remove") in labels
    for row in shown[1].inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= CALLBACK_DATA_MAX

    query = _query(_cb("lc", item_id=44))
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 4242
    update.effective_user.language_code = "en"
    await handler._list_flow().on_callback(update, MagicMock())
    api.move_list_item_to_cellar.assert_called()
    assert "Chenin Blanc 2022 is in the cellar" in query.edit_message_text.call_args.args[0]

    api.get_list.return_value = {
        "id": 1,
        "name": "To drink",
        "count": 0,
        "offset": 0,
        "limit": 5,
        "items": [],
    }
    query = _query(_cb("lx", item_id=44))
    update.callback_query = query
    await handler._list_flow().on_callback(update, MagicMock())
    api.delete_list_item.assert_called()
    removed = query.edit_message_text.call_args.args[0]
    assert t("en", "list.title") in removed
    assert t("en", "list.empty") in removed


def test_afrikaans_list_keys_exist():
    required = {
        "list.btn.add",
        "list.note.added",
        "list.note.already",
        "list.note.in_cellar",
        "list.need_name",
        "list.empty",
        "list.moved",
        "list.removed",
        "list.off",
        "list.err",
        "list.title",
        "list.btn.move",
        "list.btn.remove",
        "list.card.gone",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert t("af", "list.note.added") == "Op jou To-drink-lys."
    assert t("af", "list.btn.add") == "To-drink-lys"
    assert "To-drink-lys" in t("af", "list.empty")


def test_list_stays_out_of_the_english_menu_until_flag_on():
    public = {c for c, _ in PUBLIC_COMMANDS}
    assert "list" not in public
    assert ("list", "Things to drink or buy") in menu_commands({"feature_lists": True})
    assert "list" not in {c for c, _ in menu_commands({})}


def test_to_drink_callback_fits_64_bytes():
    raw = _cb("td", barcode="_label_99", wine_id=123456)
    assert len(raw.encode()) <= CALLBACK_DATA_MAX
    raw = _cb("lc", item_id=999999)
    assert len(raw.encode()) <= CALLBACK_DATA_MAX
    raw = _cb("ls", item_id=44, page=88)
    assert len(raw.encode()) <= CALLBACK_DATA_MAX


def test_insert_to_drink_lands_after_add_to_cellar():
    from telegram import InlineKeyboardButton

    keyboard = [
        [InlineKeyboardButton(ADD_TO_CELLAR, callback_data="a:grok")],
        [InlineKeyboardButton("Just looking", callback_data="a:look")],
    ]
    button = InlineKeyboardButton("To-drink list", callback_data="a:td")
    out = insert_to_drink(keyboard, button)
    assert out[0][0].text == ADD_TO_CELLAR
    assert out[1][0].text == "To-drink list"


def test_listed_note_afrikaans():
    handler = MagicMock()
    handler.locale_for = MagicMock(return_value="af")
    note = listed_note(handler, 1)
    assert "Op jou To-drink-lys." in note


@pytest.mark.asyncio
async def test_help_mentions_list_only_when_on():
    api = MagicMock()
    handler = _handler(api, lists=False)
    update = _update("/help")
    await handler._cmd_help(update, MagicMock())
    assert "/list" not in update.message.reply_text.call_args.args[0]
    handler.set_flags({"feature_lists": "on"})
    on = _update("/help")
    await handler._cmd_help(on, MagicMock())
    assert "/list" in on.message.reply_text.call_args.args[0]


def test_page_size_is_five_like_recent():
    assert LIST_PAGE_SIZE == 5
