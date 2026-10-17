"""Boot must not drop households just because wine-cellar is briefly down."""

import json
from pathlib import Path

from server import (
    config_reload_enabled,
    load_cached_config,
    resolve_runtime_config,
    save_cached_config,
)


def _cfg(*chat_ids):
    return {
        "config": {"bot_token": "t"},
        "telegram_users": [
            {"chat_id": cid, "api_token": "tok", "household_id": 1}
            for cid in chat_ids
        ],
        "scanners": [],
    }


def test_background_reload_is_off_unless_an_interval_is_set():
    assert config_reload_enabled(0) is False
    assert config_reload_enabled(-1) is False
    assert config_reload_enabled(60) is True


def test_live_db_wins_over_cache():
    live = _cfg("111", "222")
    cached = _cfg("111")
    config, source = resolve_runtime_config(live, cached)
    assert source == "db"
    assert len(config["telegram_users"]) == 2


def test_cache_used_when_live_fetch_fails():
    cached = _cfg("111", "222", "333")
    config, source = resolve_runtime_config(None, cached)
    assert source == "cache"
    assert [u["chat_id"] for u in config["telegram_users"]] == ["111", "222", "333"]


def test_empty_live_payload_does_not_wipe_cache():
    """A 200 with no users must not replace last-good households with yaml."""
    cached = _cfg("111", "222")
    live = {"config": {}, "telegram_users": [], "scanners": []}
    config, source = resolve_runtime_config(live, cached)
    assert source == "cache"
    assert len(config["telegram_users"]) == 2


def test_yaml_only_when_there_is_no_cache_and_no_live():
    config, source = resolve_runtime_config(None, None)
    assert source == "yaml"
    assert config["telegram_users"] == []


def test_empty_install_with_no_cache_is_live():
    live = {"config": {}, "telegram_users": [], "scanners": []}
    config, source = resolve_runtime_config(live, None)
    assert source == "db"
    assert config["telegram_users"] == []


def test_cache_roundtrip(tmp_path: Path):
    path = tmp_path / "scanner_config_cache.json"
    original = _cfg("123456789")
    save_cached_config(original, str(path))
    loaded = load_cached_config(str(path))
    assert loaded["telegram_users"][0]["chat_id"] == "123456789"
    assert json.loads(path.read_text())["telegram_users"][0]["api_token"] == "tok"
