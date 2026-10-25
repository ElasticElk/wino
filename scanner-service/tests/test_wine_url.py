"""Wine detail cards link to the public site, never a loopback address."""

from types import SimpleNamespace

from integrations.telegram_bot import (
    TelegramBotHandler,
    WINE_SITE_LINK,
    _append_wine_url,
    _public_web_base,
    _wine_url,
)


WINE = {
    "id": 5, "name": "Barolo Terre del Barolo",
    "producer": "Terre del Barolo", "vintage": 2021,
    "wine_type": "RE", "stock_count": 2,
    "items": [{"storage": "Big Fridge", "drawer": 1}],
}


def _api(web_url):
    return SimpleNamespace(web_url=web_url)


def _handler(web_url):
    api = SimpleNamespace(web_url=web_url, household_id=1)
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    ), api


def test_public_web_base_rejects_missing_and_loopback():
    assert _public_web_base(_api("")) == ""
    assert _public_web_base(_api(None)) == ""
    assert _public_web_base(_api("http://127.0.0.1:8000")) == ""
    assert _public_web_base(_api("http://localhost:8000")) == ""
    assert _public_web_base(_api("https://localhost")) == ""
    assert _public_web_base(_api("http://[::1]:8000")) == ""
    assert _public_web_base(_api("https://cellar.example")) == "https://cellar.example"
    assert _public_web_base(_api("http://nas.local:8085")) == "http://nas.local:8085"


def test_wine_url_only_for_public_base():
    assert _wine_url(_api("https://cellar.test"), 5) == "https://cellar.test/wine/5"
    assert _wine_url(_api("http://127.0.0.1:8000"), 5) == ""
    assert _wine_url(_api("http://localhost:8000"), 5) == ""
    assert _wine_url(_api(""), 5) == ""
    assert _wine_url(_api("https://cellar.test"), None) == ""


def test_append_wine_url_is_a_labelled_link():
    text = _append_wine_url("🍷 Barolo", _api("https://cellar.test"), 5)
    assert WINE_SITE_LINK in text
    assert '<a href="https://cellar.test/wine/5">Open in Whiney Wino</a>' in text
    assert text.strip().endswith("</a>")


def test_append_wine_url_hidden_when_loopback_or_missing():
    for base in ("", None, "http://127.0.0.1:8000", "http://localhost:8000"):
        text = _append_wine_url("🍷 Barolo", _api(base), 5)
        assert text == "🍷 Barolo"
        assert "127.0.0.1" not in text
        assert "localhost" not in text
        assert "/wine/" not in text
        assert WINE_SITE_LINK not in text


def test_detail_card_labelled_public_link():
    handler, api = _handler("https://wino.example")
    text, _kb = handler._build_detail_card(WINE, api=api)
    assert "Open in Whiney Wino" in text
    assert '<a href="https://wino.example/wine/5">Open in Whiney Wino</a>' in text
    assert "127.0.0.1" not in text


def test_detail_card_hides_loopback_and_missing_url():
    for base in ("", "http://127.0.0.1:8000", "http://localhost:8000"):
        handler, api = _handler(base)
        text, _kb = handler._build_detail_card(WINE, api=api)
        assert "Open in Whiney Wino" not in text
        assert "127.0.0.1" not in text
        assert "localhost" not in text
        assert "/wine/5" not in text
        assert "http://" not in text
