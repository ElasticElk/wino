"""Callback action codes must be unique in _dispatch_callback.

'sl' and 'sp' were used for both fridge-scan Move and /stock List all / paging.
The first branch always won, so List all fetched GET /api/wines/None/detail/.
"""

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_bot import (
    CALLBACK_DATA_MAX,
    BrowseSession,
    TelegramBotHandler,
    _build_browse_groups,
    _build_browse_list,
    _build_browse_view,
    _cb,
    _parse_cb,
)


BOT_PY = Path(__file__).resolve().parents[1] / "integrations/telegram_bot.py"


def _dispatch_action_codes(src: str) -> list[str]:
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "TelegramBotHandler":
            continue
        for item in node.body:
            if isinstance(item, ast.AsyncFunctionDef) and item.name == "_dispatch_callback":
                return _actions_in(item)
    raise AssertionError("_dispatch_callback not found")


def _action_codes_in_test(test) -> list[str]:
    codes = []
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not isinstance(left, ast.Name) or left.id != "action":
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.NotIn):
                continue
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                codes.append(str(comparator.value))
            elif isinstance(op, ast.In) and isinstance(comparator, ast.Tuple):
                for elt in comparator.elts:
                    if isinstance(elt, ast.Constant):
                        codes.append(str(elt.value))
    return codes


def _actions_in(fn: ast.AsyncFunctionDef) -> list[str]:
    """Only the top-level if/elif tests — nested action == inside a branch is fine."""
    codes = []
    for stmt in fn.body:
        node = stmt
        while isinstance(node, ast.If):
            codes.extend(_action_codes_in_test(node.test))
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                node = node.orelse[0]
            else:
                break
    return codes


def test_dispatch_callback_action_codes_are_unique():
    src = BOT_PY.read_text()
    codes = _dispatch_action_codes(src)
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    assert not dupes, f"duplicate action codes in _dispatch_callback: {dupes}"
    assert "bl" in codes
    assert "bp" in codes
    assert "sl" in codes
    assert "sp" in codes
    assert "ul" in codes
    assert "ap" in codes
    assert "yb" in codes
    assert "nd" in codes
    assert "ai" in codes
    assert "np" in codes
    assert "gp" in codes


def test_browse_list_all_and_paging_use_their_own_codes():
    wines = [
        {"id": i, "name": f"Wine {i}", "stock_count": 1, "wine_type": "RE",
         "producer": "X", "country": "ES"}
        for i in range(1, 12)
    ]
    session = BrowseSession(wines, mode="stock")
    _, groups = _build_browse_groups(session, "4242:7", wines, "type", 11)
    list_all = groups.inline_keyboard[-1][0].callback_data
    assert list_all.startswith("a:bl,")
    assert "w:" not in list_all

    _, listing = _build_browse_list(session, "4242:7", wines)
    nav = listing.inline_keyboard[-1]
    next_data = nav[-1].callback_data
    assert next_data.startswith("a:bp,")
    assert _cb("sp", original="4242:7:1") != next_data


def test_browse_list_clamps_stale_next_page():
    wines = [
        {"id": i, "name": f"Wine {i}", "stock_count": 1, "wine_type": "RE",
         "producer": "X", "country": "ES"}
        for i in range(1, 12)
    ]
    session = BrowseSession(wines, mode="stock")
    session.page = 99
    text, listing = _build_browse_list(session, "4242:7", wines)
    assert session.page == 1
    assert "Wine 9" in text
    assert listing.inline_keyboard


def test_fridge_list_shows_undo_for_last_removed():
    wines = [
        {"id": 2, "name": "Donkiesbaai Cinsault 2024", "stock_count": 1,
         "wine_type": "RE", "producer": "Jean Engelbrecht"},
    ]
    session = BrowseSession(wines, mode="fridge", storage_name="All storages")
    session.last_removed = {"wine_id": 1, "name": "Barbaresco 2021", "item_id": 10}
    _, listing = _build_browse_list(session, "4242:7", wines)
    labels = [b.text for row in listing.inline_keyboard for b in row]
    assert any("Undo" in t and "Barbaresco" in t for t in labels)
    undo = next(
        b for row in listing.inline_keyboard for b in row if "Undo" in b.text
    )
    assert undo.callback_data.startswith("a:ul,")
    assert "i:10" in undo.callback_data


COOP_PRODUCER = "Union Vinicole Cully Société Coopérative"
LONG_GRAPE = "Chasselas de Dézaley Grand Cru Lavaux — Vieilles Vignes"


def _assert_markup_callbacks_fit(markup):
    for row in markup.inline_keyboard:
        for btn in row:
            raw = (btn.callback_data or "").encode("utf-8")
            assert len(raw) <= CALLBACK_DATA_MAX, (len(raw), btn.callback_data)


def _gat_wines():
    wines = [{
        "id": 109, "name": "Dezaley Grand Cru Chasselas", "stock_count": 2,
        "wine_type": "WH", "producer": COOP_PRODUCER, "country": "CH",
        "grape": LONG_GRAPE,
    }]
    for i in range(8):
        wines.append({
            "id": 200 + i, "name": f"Other {i}", "stock_count": 1,
            "wine_type": "RE", "producer": f"Producer {i}", "country": "FR",
            "grape": "Pinot Noir",
        })
    return wines


def test_cb_rejects_overlong_callback_data():
    long_original = f"4242:7:producer:{COOP_PRODUCER}"
    with pytest.raises(ValueError, match="64"):
        _cb("sd2", original=long_original)


def test_gat_grouped_by_producer_long_name_fits_and_resolves():
    wines = _gat_wines()
    session = BrowseSession(wines, mode="fridge", storage_name="Gat")
    text, markup = _build_browse_groups(session, "4242:7", wines, "producer", 10)
    assert COOP_PRODUCER in text
    _assert_markup_callbacks_fit(markup)
    coop = next(
        b for row in markup.inline_keyboard for b in row
        if COOP_PRODUCER in b.text
    )
    assert COOP_PRODUCER not in coop.callback_data
    parsed = _parse_cb(coop.callback_data)
    assert parsed["action"] == "sd2"
    assert parsed["original"] == "4242:7"
    dim, value = session.group_values[parsed["page"]]
    assert dim == "producer"
    assert value == COOP_PRODUCER
    session.add_filter(dim, value)
    assert [w["id"] for w in session.filtered_wines] == [109]


def test_gat_grouped_by_grape_long_name_fits_and_resolves():
    wines = _gat_wines()
    session = BrowseSession(wines, mode="fridge", storage_name="Gat")
    text, markup = _build_browse_groups(session, "4242:7", wines, "grape", 10)
    assert LONG_GRAPE in text
    _assert_markup_callbacks_fit(markup)
    grape = next(
        b for row in markup.inline_keyboard for b in row
        if "Chasselas" in b.text
    )
    assert LONG_GRAPE not in grape.callback_data
    parsed = _parse_cb(grape.callback_data)
    dim, value = session.group_values[parsed["page"]]
    assert dim == "grape"
    assert value == LONG_GRAPE
    session.add_filter(dim, value)
    assert [w["id"] for w in session.filtered_wines] == [109]


def test_opening_gat_browse_view_does_not_embed_long_producer_in_callbacks():
    """Grouped fridge tap: grouped view must build without Button_data_invalid."""
    wines = _gat_wines()
    session = BrowseSession(wines, mode="fridge", storage_name="Gat")
    _text, markup = _build_browse_view(session, "4242:7")
    _assert_markup_callbacks_fit(markup)
    raw = "".join(b.callback_data or "" for row in markup.inline_keyboard for b in row)
    assert COOP_PRODUCER not in raw
    assert LONG_GRAPE not in raw


@pytest.mark.asyncio
async def test_tapping_long_producer_group_opens_that_group():
    wines = _gat_wines()
    api = MagicMock()
    api.household_id = 6
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    session = BrowseSession(wines, mode="fridge", storage_name="Gat")
    handler._browse_sessions["4242:7"] = session
    _, markup = _build_browse_groups(session, "4242:7", wines, "producer", 10)
    coop = next(
        b for row in markup.inline_keyboard for b in row
        if COOP_PRODUCER in b.text
    )
    query = AsyncMock()
    query.data = coop.callback_data
    query.message = MagicMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    update = MagicMock()
    update.callback_query = query
    await handler._handle_callback(update, MagicMock())
    assert session.filters["producer"] == COOP_PRODUCER
    text = query.edit_message_text.call_args.args[0]
    assert "Dezaley Grand Cru Chasselas" in text
    assert "Error" not in text
