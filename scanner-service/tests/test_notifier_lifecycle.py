"""TelegramNotifier threads must not leak across config reloads."""

from unittest.mock import patch

from integrations.telegram_bot import TelegramNotifier
from server import (
    _chat_ids_key,
    _config_fingerprint,
    _get_scanner_context,
    _ingest_scanners,
    _replace_notifier,
    api_clients,
    notifiers,
    scanner_registry,
)


class FakeNotifier:
    def __init__(self, token, chat_ids):
        self.token = token
        self.chat_ids = list(chat_ids)
        self.closed = False

    def close(self):
        self.closed = True


def _clear_notifiers():
    notifiers.clear()


def test_fingerprint_stable_for_same_payload():
    cfg = {
        "telegram_users": [
            {"chat_id": "2", "api_token": "a", "household_id": 1, "is_staff": False},
            {"chat_id": "1", "api_token": "a", "household_id": 1, "is_staff": True},
        ],
        "scanners": [
            {"api_key": "k", "api_token": "a", "household_id": 1, "notify_chat_ids": ["2", "1"]},
        ],
    }
    shuffled = {
        "telegram_users": list(reversed(cfg["telegram_users"])),
        "scanners": cfg["scanners"],
    }
    assert _config_fingerprint(cfg) == _config_fingerprint(shuffled)


def test_fingerprint_changes_when_notify_list_changes():
    a = {
        "telegram_users": [{"chat_id": "1", "api_token": "t", "household_id": 1}],
        "scanners": [{"api_key": "k", "api_token": "t", "notify_chat_ids": ["1"]}],
    }
    b = {
        "telegram_users": a["telegram_users"],
        "scanners": [{"api_key": "k", "api_token": "t", "notify_chat_ids": ["1", "2"]}],
    }
    assert _config_fingerprint(a) != _config_fingerprint(b)


def test_replace_notifier_reuses_when_chat_ids_unchanged():
    _clear_notifiers()
    with patch("server.TelegramNotifier", FakeNotifier):
        _replace_notifier("tok", "bot", ["1", "2"])
        first = notifiers["tok"]
        _replace_notifier("tok", "bot", ["1", "2"])
        _replace_notifier("tok", "bot", [1, 2])
        assert notifiers["tok"] is first
        assert not first.closed
    _clear_notifiers()


def test_replace_notifier_closes_previous_when_chat_ids_change():
    _clear_notifiers()
    with patch("server.TelegramNotifier", FakeNotifier):
        _replace_notifier("tok", "bot", ["1"])
        old = notifiers["tok"]
        _replace_notifier("tok", "bot", ["1", "2"])
        assert old.closed
        assert _chat_ids_key(notifiers["tok"].chat_ids) == ("1", "2")
        assert notifiers["tok"] is not old
    _clear_notifiers()


def test_replace_notifier_closes_when_notify_list_cleared():
    _clear_notifiers()
    with patch("server.TelegramNotifier", FakeNotifier):
        _replace_notifier("tok", "bot", ["1"])
        old = notifiers["tok"]
        _replace_notifier("tok", "bot", [])
        assert old.closed
        assert "tok" not in notifiers
    _clear_notifiers()


def test_ingest_scanners_does_not_recreate_notifiers():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    scanners = [{
        "api_key": "scan-1",
        "api_token": "tok",
        "household_id": 1,
        "default_storage": "Fridge",
        "notify_chat_ids": ["111"],
    }]
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(scanners, bot_token="bot", base_url="http://x", web_url="")
        first = notifiers["scan-1"]
        _ingest_scanners(scanners, bot_token="bot", base_url="http://x", web_url="")
        assert notifiers["scan-1"] is first
        assert not first.closed
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def test_ingest_closes_notifier_when_notify_list_cleared():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    with_ids = [{
        "api_key": "scan-1",
        "api_token": "tok",
        "household_id": 1,
        "notify_chat_ids": ["111"],
    }]
    empty = [{
        "api_key": "scan-1",
        "api_token": "tok",
        "household_id": 1,
        "notify_chat_ids": [],
    }]
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(with_ids, bot_token="bot", base_url="http://x", web_url="")
        old = notifiers["scan-1"]
        _ingest_scanners(empty, bot_token="bot", base_url="http://x", web_url="")
        assert old.closed
        assert "scan-1" not in notifiers
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def test_ingest_prunes_removed_scanner_notifiers():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    two = [
        {"api_key": "a", "api_token": "shared", "household_id": 1, "notify_chat_ids": ["1"]},
        {"api_key": "b", "api_token": "shared", "household_id": 2, "notify_chat_ids": ["2"]},
    ]
    one = [two[0]]
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(two, bot_token="bot", base_url="http://x", web_url="")
        gone = notifiers["b"]
        _ingest_scanners(one, bot_token="bot", base_url="http://x", web_url="")
        assert gone.closed
        assert "b" not in notifiers
        assert "b" not in scanner_registry
        assert "a" in notifiers
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def _shared_token_scanners(*, reverse=False):
    kitchen = {
        "name": "Kitchen Pi",
        "api_key": "kitchen-pi",
        "api_token": "shared-account",
        "household_id": 1,
        "notify_chat_ids": ["alex", "sam"],
        "default_storage": "Kitchen",
    }
    st_andrews = {
        "name": "Garage Scanner",
        "api_key": "garage-pi",
        "api_token": "shared-account",
        "household_id": 6,
        "notify_chat_ids": ["alex", "lee", "pat"],
        "default_storage": "Wine Fridge",
    }
    scanners = [kitchen, st_andrews]
    return list(reversed(scanners)) if reverse else scanners


def test_shared_token_scanners_keep_separate_notify_lists():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(
            _shared_token_scanners(), bot_token="bot", base_url="http://x", web_url="",
        )
        kitchen = notifiers["kitchen-pi"]
        andrews = notifiers["garage-pi"]
        assert kitchen is not andrews
        assert kitchen.chat_ids == ["alex", "sam"]
        assert andrews.chat_ids == ["alex", "lee", "pat"]
        _, n_k, sc_k = _get_scanner_context("kitchen-pi")
        _, n_a, sc_a = _get_scanner_context("garage-pi")
        assert n_k is kitchen
        assert n_a is andrews
        assert sc_k["household_id"] == 1
        assert sc_a["household_id"] == 6
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def test_reload_order_does_not_swap_notify_lists():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(
            _shared_token_scanners(), bot_token="bot", base_url="http://x", web_url="",
        )
        first_kitchen = notifiers["kitchen-pi"]
        first_andrews = notifiers["garage-pi"]
        _ingest_scanners(
            _shared_token_scanners(reverse=True),
            bot_token="bot", base_url="http://x", web_url="",
        )
        assert notifiers["kitchen-pi"] is first_kitchen
        assert notifiers["garage-pi"] is first_andrews
        assert notifiers["kitchen-pi"].chat_ids == ["alex", "sam"]
        assert notifiers["garage-pi"].chat_ids == ["alex", "lee", "pat"]
        assert not first_kitchen.closed
        assert not first_andrews.closed
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def test_yaml_fallback_notifier_is_keyed_by_fallback_scanner():
    scanner_registry.clear()
    api_clients.clear()
    _clear_notifiers()
    scanner_registry["_fallback"] = {
        "api_token": "admin-tok",
        "default_storage": "Wine Fridge",
        "notify_chat_ids": ["op"],
    }
    api_clients["admin-tok"] = object()
    with patch("server.TelegramNotifier", FakeNotifier):
        _replace_notifier("_fallback", "bot", ["op"])
        assert list(notifiers) == ["_fallback"]
        api, n, sc = _get_scanner_context("_fallback")
        assert api is api_clients["admin-tok"]
        assert n.chat_ids == ["op"]
        assert sc["api_token"] == "admin-tok"
    scanner_registry.clear()
    api_clients.clear()
    _clear_notifiers()


@patch("integrations.telegram_bot.Bot")
def test_notifier_close_stops_the_thread(_mock_bot):
    n = TelegramNotifier("token", ["1"])
    thread = n._thread
    assert thread.is_alive()
    n.close()
    assert n._loop is None
    assert not thread.is_alive()
    n.close()  # idempotent
