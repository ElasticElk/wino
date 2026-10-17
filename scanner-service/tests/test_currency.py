"""Prices keep the currency they were quoted in."""

from integrations.grok_vision import build_winelist_prompt, currency_code
from integrations.telegram_bot import (
    _price_str,
    build_winelist_view,
    list_entry_from_cellar,
    list_entry_from_label,
)


def test_currency_code_accepts_symbols_and_words():
    assert currency_code("£") == "GBP"
    assert currency_code("euros") == "EUR"
    assert currency_code("zar") == "ZAR"
    assert currency_code("pounds sterling") is None
    assert currency_code(None) is None


def test_winelist_prompt_keeps_printed_prices_in_their_own_currency():
    prompt = build_winelist_prompt("summary", "GBP", "GB")
    assert "do not\n                 convert it" in prompt
    assert '"retail_currency": "GBP"' in prompt


def test_retail_carries_its_own_glyph():
    assert _price_str(30, 18, "€", "menu", "£") == (
        "~€30 on the list · ~£18 retail"
    )
    assert _price_str(None, 18, "€", "menu") == "~€18 retail"


def test_cellar_wine_on_a_euro_menu_shows_its_own_currency():
    entry = list_entry_from_cellar({
        "name": "Jar from Dirt", "market_price": 420, "market_price_currency": "ZAR",
    })
    data = {"currency": "EUR", "retail_currency": "GBP", "scene": "menu",
            "wines": [entry], "recommendation": None, "alternates": []}
    text, _ = build_winelist_view(data, detail_index=0)
    assert "~R420 retail" in text
    assert "€420" not in text


def test_label_entry_estimate_uses_the_users_currency():
    entry = list_entry_from_label({"name": "X", "retail_price": 16}, "GBP")
    assert entry["retail_currency"] == "GBP"
    entry = list_entry_from_label(
        {"name": "X", "market_price": 14, "market_price_currency": "EUR"}, "GBP",
    )
    assert entry["retail_currency"] == "EUR"
