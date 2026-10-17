"""TelegramNotifier threads must not leak across config reloads."""

from unittest.mock import patch

from integrations.telegram_bot import TelegramNotifier
from server import (
    _chat_ids_key,
    _config_fingerprint,
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
        first = notifiers["tok"]
        _ingest_scanners(scanners, bot_token="bot", base_url="http://x", web_url="")
        assert notifiers["tok"] is first
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
        old = notifiers["tok"]
        _ingest_scanners(empty, bot_token="bot", base_url="http://x", web_url="")
        assert old.closed
        assert "tok" not in notifiers
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


def test_ingest_prunes_removed_scanner_notifiers():
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()
    two = [
        {"api_key": "a", "api_token": "tok-a", "household_id": 1, "notify_chat_ids": ["1"]},
        {"api_key": "b", "api_token": "tok-b", "household_id": 2, "notify_chat_ids": ["2"]},
    ]
    one = [two[0]]
    with patch("server.TelegramNotifier", FakeNotifier), patch("server.WineCellarAPI"):
        _ingest_scanners(two, bot_token="bot", base_url="http://x", web_url="")
        gone = notifiers["tok-b"]
        _ingest_scanners(one, bot_token="bot", base_url="http://x", web_url="")
        assert gone.closed
        assert "tok-b" not in notifiers
        assert "b" not in scanner_registry
        assert "tok-a" in notifiers
    scanner_registry.clear()
    api_clients.clear()
    notifiers.clear()


@patch("integrations.telegram_bot.Bot")
def test_notifier_close_stops_the_thread(_mock_bot):
    n = TelegramNotifier("token", ["1"])
    thread = n._thread
    assert thread.is_alive()
    n.close()
    assert n._loop is None
    assert not thread.is_alive()
    n.close()  # idempotent
