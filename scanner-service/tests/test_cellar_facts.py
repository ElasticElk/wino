"""Shelf answers are pure functions of the stock rows."""

from datetime import date

from integrations.cellar_facts import answer, canonical_grape, quick_pick, resolve_storage


def _item(storage, drawer, price=None):
    return {"storage": storage, "drawer": drawer, "price": price}


def _wine(wine_id, name, grapes, country="ZA", wine_type="RE", items=None,
          comment="", attributes=None, stock=1, votes=None, drink_by=None):
    return {
        "id": wine_id,
        "name": name,
        "grapes": grapes,
        "country": country,
        "wine_type": wine_type,
        "items": items if items is not None else [_item("Big Fridge", 2)],
        "comment": comment,
        "attributes": attributes or [],
        "stock_count": stock,
        "votes": votes or {"up": 0, "meh": 0, "down": 0},
        "drink_by": drink_by,
    }


def _cellar():
    return [
        _wine(1, "Kanonkop", ["Syrah"], items=[_item("Big Fridge", 3)]),
        _wine(2, "Blend & Co", ["Syrah", "Grenache"], items=[_item("Small Fridge", 1)]),
        _wine(3, "Ken Forrester", ["Chenin Blanc"], country="ZA", wine_type="WH",
              items=[_item("Small Fridge", 4)]),
        _wine(4, "Gone", ["Syrah"], stock=0, items=[_item("Big Fridge", 3)]),
    ]


def test_drawer_lists_bottles_in_that_drawer():
    reply = answer(
        {"kind": "drawer", "storage_text": "small fridge", "drawer": 1},
        _cellar(),
    )
    assert reply.kind == "drawer"
    assert "Blend &amp; Co" in reply.text
    assert "D1" in reply.text
    assert "Kanonkop" not in reply.text
    assert reply.wine_ids == [2]


def test_empty_drawer_says_empty():
    reply = answer(
        {"kind": "drawer", "storage_text": "big fridge", "drawer": 5},
        _cellar(),
    )
    assert reply.kind == "empty"
    assert reply.text == "D5 in the Big Fridge is empty."


def test_fridge_with_two_fridges_asks_which():
    reply = answer({"kind": "drawer", "storage_text": "fridge", "drawer": 1}, _cellar())
    assert reply.kind == "choose_storage"
    assert reply.storage_choices == ["Big Fridge", "Small Fridge"]


def test_small_fridge_resolves_uniquely():
    assert resolve_storage("small fridge", ["Big Fridge", "Small Fridge"]) == ["Small Fridge"]


def test_unknown_storage_names_the_real_ones():
    reply = answer(
        {"kind": "drawer", "storage_text": "garage", "drawer": 1},
        _cellar(),
    )
    assert "garage" in reply.text
    assert "Big Fridge" in reply.text
    assert "Small Fridge" in reply.text


def test_country_hit_and_none():
    hit = answer({"kind": "country", "country_code": "ZA"}, _cellar())
    assert hit.kind == "country"
    assert hit.text.startswith("From South Africa")
    assert hit.wine_ids == [1, 2, 3]
    miss = answer({"kind": "country", "country_code": "FR"}, _cellar())
    assert miss.text == "Nothing from France in the cellar."
    assert miss.wine_ids == []


def test_shiraz_finds_syrah_and_says_same_grape():
    reply = answer({"kind": "grape", "grape": "shiraz"}, _cellar())
    assert "same grape" in reply.text
    assert "Syrah" in reply.text
    assert 1 in reply.wine_ids
    assert reply.suggest_close == ""


def test_blend_with_syrah_is_labelled_blend():
    reply = answer({"kind": "grape", "grape": "Syrah"}, _cellar())
    assert "(blend with Syrah)" in reply.text
    assert "Syrah in your cellar:" in reply.text
    assert 2 in reply.wine_ids


def test_grape_miss_sets_suggest_close():
    reply = answer({"kind": "grape", "grape": "Nebbiolo"}, _cellar())
    assert reply.text == "You don't have a Nebbiolo."
    assert reply.suggest_close == "Nebbiolo"
    assert reply.wine_ids == []


def test_list_all_returns_marker():
    reply = answer({"kind": "list_all"}, _cellar())
    assert reply.kind == "list_all"
    assert reply.text == ""


def test_zero_stock_wines_never_listed():
    reply = answer({"kind": "grape", "grape": "Syrah"}, _cellar())
    assert 4 not in reply.wine_ids
    assert "Gone" not in reply.text


def test_names_are_html_escaped():
    wines = [_wine(1, "Smith <Reserve>", ["Syrah"], items=[_item("Big Fridge", 1)])]
    reply = answer({"kind": "drawer", "storage_text": "big fridge", "drawer": 1}, wines)
    assert "Smith &lt;Reserve&gt;" in reply.text
    assert "<Reserve>" not in reply.text


def test_accents_and_case_ignored():
    assert canonical_grape("Mourvèdre") == canonical_grape("mourvedre")
    wines = [_wine(1, "Bandol", ["Mourvèdre"])]
    reply = answer({"kind": "grape", "grape": "Mataro"}, wines)
    assert reply.wine_ids == [1]
    assert "same grape" in reply.text


def test_quick_pick_respects_colour_skips_thumbs_down_and_prefers_note_match():
    wines = [
        _wine(1, "Leather", ["Syrah"], comment="leather and smoke", drink_by="2026-12-01"),
        _wine(2, "Plain", ["Syrah"], drink_by="2035-01-01"),
        _wine(3, "Hated", ["Syrah"], comment="leather", votes={"up": 0, "meh": 0, "down": 2}),
        _wine(4, "White", ["Chenin Blanc"], wine_type="WH", comment="leather"),
    ]
    history = [{"wine_id": 2}]
    picked = quick_pick(
        {"colour": "red", "words": ["leather"]}, wines, history, today=date(2026, 10, 21),
    )
    ids = [row["id"] for row in picked]
    assert ids[0] == 1
    assert 3 not in ids
    assert 4 not in ids
    assert picked[0]["evidence"] == "note"


def test_quick_pick_nothing_fits_says_empty():
    wines = [_wine(1, "White", ["Chenin Blanc"], wine_type="WH")]
    assert quick_pick({"colour": "red", "words": []}, wines, []) == []
