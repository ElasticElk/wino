"""The /scan backend, without a USB scanner or a Pi.

These cover the contract the fridge client will POST once hardware exists:
known IN/OUT, unknown barcode, ambiguous match, and a bad device key.
"""

import pytest
from fastapi import HTTPException

import server
from server import ScanRequest, handle_scan


BARCODE = "6001234567890"
KEY = "kitchen-pi-test-key"
TOKEN = "tok"
HOUSEHOLD = 7


class FakeAPI:
    def __init__(self, wines=None, household_id=HOUSEHOLD):
        self.household_id = household_id
        self.wines = list(wines or [])
        self.adds = []
        self.removes = []
        self.lookups = []

    def lookup_barcode(self, barcode):
        self.lookups.append(barcode)
        return list(self.wines)

    def list_all_wines(self):
        return list(getattr(self, "all_wines", []))

    def add_stock(self, wine_id, price=None, storage_name=None, row=None,
                  drank=False, confirm_duplicate=False, check_duplicate=False):
        self.adds.append((wine_id, storage_name, drank))
        return {
            "wine_name": "Test Chenin",
            "stock_count": 3 if not drank else 2,
            "item_id": 80,
            "storage": storage_name or "Big Fridge",
            "storage_id": 2,
            "row": row,
            "drawers": 8,
            "action": "drank" if drank else "added",
        }

    def remove_stock(self, wine_id, storage_name=None):
        self.removes.append((wine_id, storage_name))
        if getattr(self, "remove_error", None) and storage_name == self.remove_error:
            return {"error": f"No stock of this wine in {storage_name}"}
        return {
            "wine_name": "Test Chenin",
            "stock_count": 2,
            "item_id": 79,
            "storage": storage_name or "Kitchen",
            "storage_id": 2 if storage_name == "Big Fridge" else 3,
            "row": 2,
            "drawers": 0,
        }


class FakeOFF:
    def lookup(self, barcode):
        return None


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def send_stock_update_with_actions(self, **kwargs):
        self.calls.append(("stock", kwargs))

    def send_error(self, msg):
        self.calls.append(("error", msg))

    def send_disambiguation(self, *args, **kwargs):
        self.calls.append(("disambiguate", args, kwargs))

    def send_new_wine_prompt(self, barcode, off_data, suggested=None):
        self.calls.append(("new_wine", barcode, off_data, suggested))

    def send(self, msg):
        self.calls.append(("send", msg))

    def send_achievements(self, achievements):
        self.calls.append(("achievements", achievements))


@pytest.fixture
def kitchen_pi():
    api = FakeAPI()
    notifier = RecordingNotifier()
    server.scanner_registry[KEY] = {
        "name": "Kitchen Pi",
        "api_token": TOKEN,
        "household_id": HOUSEHOLD,
        "api_key": KEY,
        "default_storage": "Big Fridge",
    }
    server.api_clients[f"{TOKEN}:{HOUSEHOLD}"] = api
    server.notifiers[KEY] = notifier
    server.off = FakeOFF()
    yield api, notifier
    server.scanner_registry.pop(KEY, None)
    server.api_clients.pop(f"{TOKEN}:{HOUSEHOLD}", None)
    server.notifiers.pop(KEY, None)
    server.pending_store.clear()


@pytest.mark.asyncio
async def test_unknown_api_key_is_forbidden():
    with pytest.raises(HTTPException) as ei:
        await handle_scan(ScanRequest(barcode=BARCODE, mode="in", api_key="nope"))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_empty_barcode_is_rejected():
    with pytest.raises(HTTPException) as ei:
        await handle_scan(ScanRequest(barcode="  ", mode="in", api_key=KEY))
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_bad_mode_is_rejected():
    with pytest.raises(HTTPException) as ei:
        await handle_scan(ScanRequest(barcode=BARCODE, mode="sideways", api_key=KEY))
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_known_wine_in_adds_stock(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = [{"id": 42, "name": "Test Chenin", "stock_count": 2}]

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="in", api_key=KEY))

    assert result.status == "added"
    assert result.beep == "in"
    assert result.wine_name == "Test Chenin"
    assert result.stock_count == 3
    assert api.adds == [(42, "Big Fridge", False)]
    assert notifier.calls and notifier.calls[0][0] == "stock"
    stock = notifier.calls[0][1]
    assert stock["storage"] == "Big Fridge"
    assert stock["item_id"] == 80
    assert stock["drawers"] == 8


@pytest.mark.asyncio
async def test_known_wine_in_does_not_say_added_on_error(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = [{"id": 42, "name": "Test Chenin", "stock_count": 2}]

    def fail(*a, **k):
        return {"error": "recent_duplicate", "stock_count": 2, "wine_name": "Test Chenin"}

    api.add_stock = fail
    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="in", api_key=KEY))
    assert result.status == "error"
    assert result.beep == "unknown"
    assert notifier.calls and notifier.calls[0][0] == "error"


@pytest.mark.asyncio
async def test_known_wine_out_removes_stock(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = [{"id": 42, "name": "Test Chenin", "stock_count": 3}]

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="out", api_key=KEY))

    assert result.status == "removed"
    assert result.beep == "out"
    assert api.removes == [(42, "Big Fridge")]
    stock = notifier.calls[0][1]
    assert stock["action"] == "out"
    assert stock["storage"] == "Big Fridge"
    assert stock["storage_id"] == 2


@pytest.mark.asyncio
async def test_unknown_barcode_out_does_not_invent_stock(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = []

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="out", api_key=KEY))

    assert result.status == "unknown"
    assert result.beep == "unknown"
    assert api.adds == []
    assert api.removes == []
    assert notifier.calls[0][0] == "error"


@pytest.mark.asyncio
async def test_unknown_barcode_in_goes_pending(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = []

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="in", api_key=KEY))

    assert result.status == "unknown"
    assert result.beep == "unknown"
    assert notifier.calls[0][0] == "new_wine"
    assert any(BARCODE in str(k) for k in server.pending_store)


@pytest.mark.asyncio
async def test_ambiguous_barcode_asks_telegram(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = [
        {"id": 1, "name": "Same Bottle", "vintage": 2021, "stock_count": 1},
        {"id": 2, "name": "Same Bottle", "vintage": 2022, "stock_count": 1},
    ]

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="in", api_key=KEY))

    assert result.status == "ambiguous"
    assert result.beep == "choose"
    assert api.adds == []
    assert notifier.calls[0][0] == "disambiguate"


@pytest.mark.asyncio
async def test_known_wine_out_falls_back_when_default_fridge_is_empty(kitchen_pi):
    api, notifier = kitchen_pi
    api.wines = [{"id": 42, "name": "Test Chenin", "stock_count": 1}]
    api.remove_error = "Big Fridge"

    result = await handle_scan(ScanRequest(barcode=BARCODE, mode="out", api_key=KEY))

    assert result.status == "removed"
    assert api.removes == [(42, "Big Fridge"), (42, None)]
    assert notifier.calls[0][1]["storage"] == "Kitchen"


@pytest.mark.asyncio
async def test_scan_notifies_only_that_scanners_chats():
    """Five Pis share one Django token. Kitchen (hh1) must not ping Garage (hh6)."""
    token = "shared-account"
    kitchen_key = "kitchen-pi"
    andrews_key = "garage-pi"
    kitchen_api = FakeAPI(
        wines=[{"id": 1, "name": "Chenin", "stock_count": 1}], household_id=1,
    )
    andrews_api = FakeAPI(
        wines=[{"id": 2, "name": "Rioja", "stock_count": 1}], household_id=6,
    )
    kitchen_n = RecordingNotifier()
    andrews_n = RecordingNotifier()
    server.scanner_registry[kitchen_key] = {
        "name": "Kitchen Pi", "api_token": token, "household_id": 1,
        "api_key": kitchen_key, "default_storage": "Kitchen",
    }
    server.scanner_registry[andrews_key] = {
        "name": "Garage Scanner", "api_token": token, "household_id": 6,
        "api_key": andrews_key, "default_storage": "Wine Fridge",
    }
    server.api_clients[f"{token}:1"] = kitchen_api
    server.api_clients[f"{token}:6"] = andrews_api
    server.notifiers[kitchen_key] = kitchen_n
    server.notifiers[andrews_key] = andrews_n
    server.off = FakeOFF()
    try:
        kitchen = await handle_scan(
            ScanRequest(barcode=BARCODE, mode="in", api_key=kitchen_key),
        )
        assert kitchen.status == "added"
        assert {c[0] for c in kitchen_n.calls} <= {"stock", "achievements"}
        assert any(c[0] == "stock" for c in kitchen_n.calls)
        assert andrews_n.calls == []
        kitchen_after_own_scan = list(kitchen_n.calls)

        andrews = await handle_scan(
            ScanRequest(barcode=BARCODE, mode="in", api_key=andrews_key),
        )
        assert andrews.status == "added"
        assert {c[0] for c in andrews_n.calls} <= {"stock", "achievements"}
        assert any(c[0] == "stock" for c in andrews_n.calls)
        assert kitchen_n.calls == kitchen_after_own_scan
    finally:
        server.scanner_registry.pop(kitchen_key, None)
        server.scanner_registry.pop(andrews_key, None)
        server.api_clients.pop(f"{token}:1", None)
        server.api_clients.pop(f"{token}:6", None)
        server.notifiers.pop(kitchen_key, None)
        server.notifiers.pop(andrews_key, None)
        server.pending_store.clear()


def test_in_card_offers_move_and_drawer():
    from integrations.telegram_bot import build_stock_update_view

    text, markup = build_stock_update_view(
        "in", "Viña Pomal 2020", 2,
        wine_id=63, barcode=BARCODE, storage="Big Fridge",
        item_id=80, drawers=8, row=None, storage_id=2,
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Move" in label for label in labels)
    assert any("Drawer" in label for label in labels)
    assert "Big Fridge" in text


def test_out_card_offers_another_fridge():
    from integrations.telegram_bot import build_stock_update_view

    text, markup = build_stock_update_view(
        "out", "Viña Pomal 2020", 1,
        wine_id=63, barcode=BARCODE, storage="Big Fridge",
        storage_id=2,
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "From another fridge" in labels
    assert "📍 from Big Fridge" in text
    assert "📦 Move" not in labels


def test_out_card_offers_optional_votes_with_a_dash_not_a_face():
    from integrations.telegram_bot import build_stock_update_view

    text, markup = build_stock_update_view(
        "out", "Viña Pomal 2020", 1,
        wine_id=63, barcode=BARCODE, storage="Big Fridge",
        item_id=79, storage_id=2,
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    undo = next(b for row in markup.inline_keyboard for b in row if "Undo" in b.text)
    assert undo.callback_data.startswith("a:ul,")
    assert "i:79" in undo.callback_data
    assert "\U0001F44E" in labels
    assert "\u2014" in labels
    assert "\U0001F44D" in labels
    assert "\U0001F610" not in "".join(labels)
    assert "How was it? (optional)" in text
