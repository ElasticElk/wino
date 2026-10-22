"""Fridge catch-up: drawer walk stages item_ids; Empty is a move, never drunk."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.card_store import CardStore
from integrations.telegram_bot import (
    CALLBACK_DATA_MAX,
    BrowseSession,
    TelegramBotHandler,
    _build_browse_list,
    _parse_cb,
)
from integrations.telegram_catchup import (
    ACTION,
    CATCHUP_LABEL,
    NO_DRAWER,
    UNDO_TAP,
    CatchUpFlow,
    api_ops,
    apply_summary,
    drawer_counts,
    extend_ops,
    items_for_place,
    slim_wines,
    stage_count,
    still_valid,
    unstaged_groups,
)

BATCH = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _wines():
    return [
        {
            "id": 1,
            "name": "Kanonkop Pinotage 2019",
            "producer": "Kanonkop",
            "wine_type": "RE",
            "stock_count": 1,
            "items": [
                {"item_id": 10, "storage": "Gat", "drawer": 3},
            ],
        },
        {
            "id": 2,
            "name": "Mullineux Syrah 2020",
            "producer": "Mullineux",
            "wine_type": "RE",
            "stock_count": 2,
            "items": [
                {"item_id": 21, "storage": "Gat", "drawer": 3},
                {"item_id": 22, "storage": "Gat", "drawer": 3},
                {"item_id": 23, "storage": "Gat", "drawer": 5},
            ],
        },
        {
            "id": 3,
            "name": "Chenin 2022",
            "producer": "",
            "wine_type": "WH",
            "stock_count": 1,
            "items": [
                {"item_id": 30, "storage": "Gat", "drawer": None},
            ],
        },
    ]


def test_items_for_place_keeps_the_other_drawer():
    groups = items_for_place(_wines(), "Gat", 3)
    by_id = {g["wine_id"]: g for g in groups}
    assert [i["item_id"] for i in by_id[2]["items"]] == [21, 22]
    assert 23 not in [i["item_id"] for g in groups for i in g["items"]]
    assert 3 not in by_id


def test_bottles_with_no_drawer_get_their_own_bucket():
    counts = drawer_counts(_wines(), "Gat", 8)
    assert counts[NO_DRAWER] == 1
    groups = items_for_place(_wines(), "Gat", NO_DRAWER)
    assert [g["wine_id"] for g in groups] == [3]


def test_drunk_one_of_x2_does_not_take_the_other_bottle():
    groups = items_for_place(_wines(), "Gat", 3)
    syrah = next(g for g in groups if g["wine_id"] == 2)
    ops = stage_count(syrah, 1, "remove")
    assert [op["item_id"] for op in ops] == [21]
    left = unstaged_groups(groups, ops)
    leftover = next(g for g in left if g["wine_id"] == 2)
    assert [i["item_id"] for i in leftover["items"]] == [22]


def test_empty_here_stages_moves_not_removes():
    groups = items_for_place(_wines(), "Gat", 3)
    ops = []
    for group in groups:
        ops.extend(stage_count(
            group, len(group["items"]), "move",
            dest_storage="Small", dest_row=1,
        ))
    assert {op["op"] for op in ops} == {"move"}
    assert {op["item_id"] for op in ops} == {10, 21, 22}
    payload = api_ops(ops)
    assert all("wine_name" not in row for row in payload)
    assert all("tap" not in row for row in payload)
    assert all(row["storage_name"] == "Small" for row in payload)


def test_fridge_browse_has_catch_up():
    wines = [{"id": 1, "name": "A", "stock_count": 1, "wine_type": "RE", "producer": ""}]
    session = BrowseSession(wines, mode="fridge", storage_name="Gat")
    _, markup = _build_browse_list(session, "4242:7", wines)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert CATCHUP_LABEL in labels
    assert any("\u2796" in t for t in labels)
    assert any("\u2795" in t for t in labels)
    btn = next(b for row in markup.inline_keyboard for b in row if b.text == CATCHUP_LABEL)
    parsed = _parse_cb(btn.callback_data)
    assert parsed["action"] == ACTION
    assert parsed["original"] == "go"


def test_extend_ops_skips_a_bottle_already_staged():
    groups = items_for_place(_wines(), "Gat", 3)
    syrah = next(g for g in groups if g["wine_id"] == 2)
    ops = stage_count(syrah, 1, "remove")
    extend_ops(ops, stage_count(syrah, 1, "remove"))
    assert [op["item_id"] for op in ops] == [21]


def test_apply_summary_names_the_bottle():
    text = apply_summary([{
        "op": "remove", "item_id": 21, "wine_name": "Mullineux Syrah 2020",
    }], 1)
    assert "Applied 1 change." in text
    assert "Drunk · Mullineux Syrah 2020" in text


def test_slim_wines_drops_reference_fields():
    slim = slim_wines([{
        "id": 1, "name": "Kanonkop Pinotage 2019", "producer": "Kanonkop",
        "wine_type": "RE", "critic_score": 94, "food_pairings": ["steak"],
        "items": [{"item_id": 10, "storage": "Gat", "drawer": 3, "price": 12}],
    }])
    assert "critic_score" not in slim[0]
    assert slim[0]["items"] == [{"item_id": 10, "storage": "Gat", "drawer": 3}]


def test_still_valid_drops_only_the_bottle_that_changed():
    groups = items_for_place(_wines(), "Gat", 3)
    ops = []
    for group in groups:
        ops.extend(stage_count(group, len(group["items"]), "remove"))
    fresh = _wines()
    fresh[1]["items"][0]["drawer"] = 4
    kept, dropped = still_valid(ops, fresh)
    assert dropped == 1
    assert {op["item_id"] for op in kept} == {10, 22}


# --- Flow ---

def _handler(storages=None, wines=None):
    api = MagicMock()
    api.household_id = 1
    api.list_storages.return_value = storages or [
        {"id": 4, "name": "Gat", "drawers": 8, "bottles": 3},
        {"id": 5, "name": "Small", "drawers": 4, "bottles": 0},
    ]
    api.wines_in_stock.return_value = wines if wines is not None else _wines()
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler._catchup_store = CardStore(ttl=1800, prefix="test:catchup:")
    handler._browse_sessions["4242:9"] = BrowseSession(
        api.wines_in_stock.return_value, mode="fridge", storage_name="Gat",
    )
    return handler, api


def _query(data: str):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 9
    return query


async def _tap(flow, data: str):
    query = _query(data)
    update = MagicMock()
    update.callback_query = query
    assert await flow.on_callback(update, MagicMock()) is True
    return query


def _markup(query):
    return query.edit_message_text.call_args.kwargs.get("reply_markup")


def _buttons(query):
    markup = _markup(query)
    return [b for row in markup.inline_keyboard for b in row] if markup else []


def _button(query, text):
    return next(b for b in _buttons(query) if b.text == text)


@pytest.mark.asyncio
async def test_catchup_walk_drunk_one_then_apply():
    handler, api = _handler()
    api.bulk_stock.return_value = {"batch_id": BATCH, "applied": 1}
    flow = CatchUpFlow(handler)
    query = await _tap(flow, "a:cu,o:go")
    query.answer.assert_awaited()
    labels = [b.text for b in _buttons(query)]
    assert any(t.startswith("No drawer") for t in labels)

    query = await _tap(flow, "a:cu,o:d,r:3")
    text = query.edit_message_text.call_args.args[0]
    assert "D3" in text and "Mullineux" in text and "Kanonkop" in text
    for button in _buttons(query):
        assert len(button.callback_data.encode()) <= CALLBACK_DATA_MAX

    drunk_1 = _button(query, "Drunk 1")
    assert _parse_cb(drunk_1.callback_data)["wine_id"] == 2
    query = await _tap(flow, drunk_1.callback_data)
    session = flow.load(4242)
    assert [op["item_id"] for op in session["ops"]] == [21]
    text = query.edit_message_text.call_args.args[0]
    assert "Staged here" in text
    assert "Drunk · Mullineux Syrah 2020" in text
    assert "Nothing is saved until Apply" in text

    query = await _tap(flow, "a:cu,o:ap")
    assert api.bulk_stock.call_args.args[0] == [{
        "op": "remove",
        "item_id": 21,
        "expected_storage": "Gat",
        "expected_row": 3,
    }]
    text = query.edit_message_text.call_args.args[0]
    assert "Drunk · Mullineux Syrah 2020" in text
    undo = _button(query, "Undo")
    assert _parse_cb(undo.callback_data)["barcode"] == BATCH


@pytest.mark.asyncio
async def test_double_tap_never_lands_on_another_wine():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    kanonkop = _button(query, "Drunk")
    await _tap(flow, kanonkop.callback_data)
    await _tap(flow, kanonkop.callback_data)
    session = flow.load(4242)
    assert [op["item_id"] for op in session["ops"]] == [10]


@pytest.mark.asyncio
async def test_stale_drunk_both_after_drunk_one_does_not_stage():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    both = _button(query, "Drunk both")
    await _tap(flow, _button(query, "Drunk 1").callback_data)
    query = await _tap(flow, both.callback_data)
    session = flow.load(4242)
    assert [op["item_id"] for op in session["ops"]] == [21]
    assert "changed" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_undo_last_tap_unstages_it():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    query = await _tap(flow, _button(query, "Drunk both").callback_data)
    assert len(flow.load(4242)["ops"]) == 2
    query = await _tap(flow, _button(query, UNDO_TAP).callback_data)
    assert flow.load(4242)["ops"] == []
    assert "Mullineux" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_empty_here_asks_destination_and_does_not_remove():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    await _tap(flow, "a:cu,o:d,r:3")
    query = await _tap(flow, "a:cu,o:eh")
    session = flow.load(4242)
    assert session["ops"] == []
    assert sorted(session["pending_move"]["item_ids"]) == [10, 21, 22]
    text = query.edit_message_text.call_args.args[0]
    assert "these 3 bottles" in text
    api.bulk_stock.assert_not_called()

    await _tap(flow, "a:cu,w:5,o:mf")
    await _tap(flow, "a:cu,w:5,o:md,r:1")
    session = flow.load(4242)
    assert {op["op"] for op in session["ops"]} == {"move"}
    assert {op["item_id"] for op in session["ops"]} == {10, 21, 22}
    assert all(op["storage_name"] == "Small" and op["row"] == 1 for op in session["ops"])


@pytest.mark.asyncio
async def test_not_sure_which_drawer_moves_with_no_row():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    await _tap(flow, _button(query, "Moved").callback_data)
    query = await _tap(flow, "a:cu,w:5,o:mf")
    not_sure = _button(query, "Not sure which drawer")
    await _tap(flow, not_sure.callback_data)
    session = flow.load(4242)
    assert session["ops"][0]["storage_name"] == "Small"
    assert session["ops"][0]["row"] is None


@pytest.mark.asyncio
async def test_dest_back_returns_to_the_drawer():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    await _tap(flow, "a:cu,o:d,r:3")
    await _tap(flow, "a:cu,o:eh")
    query = await _tap(flow, "a:cu,o:w")
    session = flow.load(4242)
    assert session["pending_move"] is None
    assert session["ops"] == []
    text = query.edit_message_text.call_args.args[0]
    assert "D3" in text and "Mullineux" in text


@pytest.mark.asyncio
async def test_single_rack_has_no_moved_button():
    rack_wines = [{
        "id": 1, "name": "Kanonkop Pinotage 2019", "producer": "", "wine_type": "RE",
        "items": [{"item_id": 10, "storage": "Rack", "drawer": None}],
    }]
    handler, api = _handler(
        storages=[{"id": 9, "name": "Rack", "drawers": 0, "bottles": 1}],
        wines=rack_wines,
    )
    handler._browse_sessions["4242:9"].storage_name = "Rack"
    flow = CatchUpFlow(handler)
    query = await _tap(flow, "a:cu,o:go")
    labels = [b.text for b in _buttons(query)]
    assert "Drunk" in labels
    assert "Moved" not in labels
    assert not any("aren't here" in t for t in labels)


@pytest.mark.asyncio
async def test_stale_apply_keeps_the_valid_changes():
    handler, api = _handler()
    api.bulk_stock.return_value = {"error": "stale_item", "item_id": 21, "status_code": 409}
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    query = await _tap(flow, _button(query, "Drunk").callback_data)
    await _tap(flow, _button(query, "Drunk 1").callback_data)
    fresh = _wines()
    fresh[1]["items"][0]["drawer"] = 4
    api.wines_in_stock.return_value = fresh
    query = await _tap(flow, "a:cu,o:ap")
    session = flow.load(4242)
    assert [op["item_id"] for op in session["ops"]] == [10]
    text = query.edit_message_text.call_args.args[0]
    assert "1 bottle had already changed" in text
    assert "Nothing was applied" in text


@pytest.mark.asyncio
async def test_apply_network_failure_offers_retry_and_keeps_ops():
    handler, api = _handler()
    api.bulk_stock.side_effect = RuntimeError("http://web:8000 exploded")
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    query = await _tap(flow, "a:cu,o:d,r:3")
    await _tap(flow, _button(query, "Drunk").callback_data)
    query = await _tap(flow, "a:cu,o:ap")
    text = query.edit_message_text.call_args.args[0]
    assert "web:8000" not in text
    assert "Nothing was applied" in text
    assert _button(query, "Try again")
    assert len(flow.load(4242)["ops"]) == 1


@pytest.mark.asyncio
async def test_undo_still_works_after_a_new_catch_up():
    handler, api = _handler()
    api.bulk_stock_undo.return_value = {"undone": 1, "batch_id": BATCH}
    flow = CatchUpFlow(handler)
    await _tap(flow, "a:cu,o:go")
    flow.drop(4242)
    query = await _tap(flow, f"a:cu,b:{BATCH},o:un")
    api.bulk_stock_undo.assert_called_once_with(BATCH)
    assert "Undone 1 change" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_search_and_help_do_not_drop_catchup():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    handler._catchups = flow
    handler.admin_chat_ids = []
    flow.save(4242, {"storage_name": "Gat", "ops": [{"item_id": 10, "op": "remove"}]})
    update = MagicMock()
    update.message = AsyncMock()
    update.effective_chat.id = 4242
    await handler._cmd_help(update, MagicMock())
    assert flow.load(4242)["ops"]
    context = MagicMock()
    context.args = []
    await handler._cmd_search(update, context)
    assert flow.load(4242)["ops"]


@pytest.mark.asyncio
async def test_switch_drops_catchup():
    handler, api = _handler()
    flow = CatchUpFlow(handler)
    handler._catchups = flow
    handler._api_factory = MagicMock(return_value=api)
    flow.save(4242, {"storage_name": "Gat", "ops": [{"item_id": 10}]})
    query = AsyncMock()
    query.edit_message_text = AsyncMock()
    await handler._do_switch(query, "4242", 2, "Other")
    assert flow.load(4242) is None
