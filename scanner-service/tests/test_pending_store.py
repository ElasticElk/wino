"""Tenant isolation and eviction for the shared pending-barcode store.

The store is process-global and shared by every household on the bot plus the
/scan endpoint, so these are the invariants that keep one household's in-flight
scan from being resolved by another's photo.
"""

import threading
import time

import pytest

from integrations.telegram_bot import (
    PENDING_TIMEOUT_SECONDS,
    TelegramBotHandler,
    _active_pendings,
    _sweep_pendings,
    pending_key,
)

BARCODE = "7798039590229"


class FakeAPI:
    def __init__(self, household_id):
        self.household_id = household_id


@pytest.fixture
def handler():
    h = TelegramBotHandler.__new__(TelegramBotHandler)
    h.pending_store = {}
    h._lock = threading.Lock()
    h._label_seq = 0
    return h


@pytest.fixture
def hh1():
    return FakeAPI(1)


@pytest.fixture
def hh7():
    return FakeAPI(7)


def test_same_barcode_in_two_households_does_not_collide(handler, hh1, hh7):
    handler._create_pending(hh1, BARCODE, off_data={"name": "HH1 wine"})
    handler._create_pending(hh7, BARCODE, off_data={"name": "HH7 wine"})

    assert len(handler.pending_store) == 2
    assert handler._get_pending(hh1, BARCODE)["off_data"]["name"] == "HH1 wine"
    assert handler._get_pending(hh7, BARCODE)["off_data"]["name"] == "HH7 wine"


def test_listing_is_scoped_to_one_household(handler, hh1, hh7):
    handler._create_pending(hh1, BARCODE, off_data=None)
    handler._create_pending(hh7, "5010103802017", off_data=None)

    # /pending and the "single pending -> attach this photo" heuristic both
    # read through here; leaking across households is the bug being guarded.
    assert list(_active_pendings(handler.pending_store, 1)) == [BARCODE]
    assert list(_active_pendings(handler.pending_store, 7)) == ["5010103802017"]
    assert len(_active_pendings(handler.pending_store)) == 2  # unscoped sees both


def test_resolving_one_household_leaves_the_other_intact(handler, hh1, hh7):
    handler._create_pending(hh1, BARCODE)
    handler._create_pending(hh7, BARCODE)

    handler._resolve_pending(hh1, BARCODE)

    assert handler._is_pending(hh1, BARCODE) is False
    assert handler._is_pending(hh7, BARCODE) is True


def test_resolve_evicts_rather_than_flagging(handler, hh1):
    """Entries hold the full label JPEG; flagging them would leak it forever."""
    handler._create_pending(hh1, BARCODE)
    handler._update_pending(hh1, BARCODE, photo_data=b"x" * 4096)
    assert handler.pending_store  # sanity

    handler._resolve_pending(hh1, BARCODE)

    assert handler.pending_store == {}


def test_sweep_evicts_expired_including_internal_keys(handler, hh1):
    handler._create_pending(hh1, BARCODE)  # fresh, must survive
    stale = time.time() - PENDING_TIMEOUT_SECONDS - 1
    # _label_/_edit_ keys never appear in _active_pendings, so only the sweeper
    # can ever reclaim them.
    handler.pending_store["_label_1"] = {"time": stale, "photo_data": b"y" * 4096}
    handler.pending_store["_edit_42"] = {"time": stale, "photo_data": b"z" * 4096}

    assert _sweep_pendings(handler.pending_store) == 2
    assert handler._is_pending(hh1, BARCODE) is True


def test_label_keys_are_unique(handler):
    """Previously keyed on id(grok_data) — a reusable memory address."""
    assert len({handler._next_label_key() for _ in range(500)}) == 500


def test_pending_key_falls_back_to_bare_barcode_without_household():
    # Single-tenant / config.yaml fallback mode has no household id.
    assert pending_key(None, BARCODE) == BARCODE
    assert pending_key(3, BARCODE) == f"3:{BARCODE}"


def test_internal_keys_round_trip_through_the_helpers(handler, hh1):
    """_label_/_edit_ entries are written bare by some call sites.

    The "✅ Add to cellar" button after a label-only photo passes the temp key
    through the same barcode-shaped path, so prefixing it here would make the
    read miss the write and the button would report "already handled".
    """
    temp_key = handler._next_label_key()
    handler.pending_store[temp_key] = {"grok_merged": {"name": "X"}, "time": time.time()}

    assert pending_key(1, temp_key) == temp_key
    assert handler._is_pending(hh1, temp_key) is True
    assert handler._get_pending(hh1, temp_key)["grok_merged"]["name"] == "X"

    handler._resolve_pending(hh1, temp_key)
    assert handler._is_pending(hh1, temp_key) is False


def test_edit_keys_are_not_prefixed(handler, hh1):
    edit_key = "_edit_42"
    handler.pending_store[edit_key] = {"wine_id": 42, "time": time.time()}
    assert handler._is_pending(hh1, edit_key) is True
    assert pending_key(7, edit_key) == edit_key
