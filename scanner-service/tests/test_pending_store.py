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


def test_claim_pending_blocks_a_second_tap(handler, hh1):
    handler._create_pending(hh1, BARCODE)
    assert handler._claim_pending(hh1, BARCODE) == ("claimed", None)
    assert handler._claim_pending(hh1, BARCODE) == ("busy", None)
    handler._update_pending(hh1, BARCODE, wine_id=68)
    assert handler._claim_pending(hh1, BARCODE) == ("created", 68)


def test_stale_in_progress_does_not_stick_on_card_store(hh1):
    from integrations.card_store import CardStore
    from integrations.telegram_bot import TelegramBotHandler

    h = TelegramBotHandler.__new__(TelegramBotHandler)
    h.pending_store = CardStore()
    h._lock = threading.Lock()
    h._label_seq = 0
    h._create_pending(hh1, BARCODE)
    key = h._pkey(hh1, BARCODE)
    entry = dict(h.pending_store.get(key))
    entry["in_progress"] = True
    h.pending_store[key] = entry
    assert h._claim_pending(hh1, BARCODE) == ("claimed", None)
    h._unclaim_pending(hh1, BARCODE)
    assert h._claim_pending(hh1, BARCODE) == ("claimed", None)


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


# --- wine list reading -------------------------------------------------------

from integrations.telegram_bot import (  # noqa: E402
    _wine_label,
    apply_winelist_correction,
    build_cellar_summary,
    build_winelist_view,
    list_entry_from_label,
    mark_owned_wines,
)

LIST_DATA = {
    "type": "winelist",
    "currency": "GBP",
    "wines": [
        {"name": "Ken Forrester Old Vine Reserve 2021", "vintage": 2021,
         "wine_type": "WH", "note": "Rich, honeyed Chenin.",
         "list_price": 38, "retail_price": 15},
        {"name": "Produttori del Barbaresco 2019", "vintage": 2019,
         "wine_type": "RE", "note": "Tar, roses, firm tannins.",
         "list_price": 78, "retail_price": 30},
        {"name": "Ruinart Blanc de Blancs NV", "wine_type": "SP",
         "note": "Brioche and chalk.", "list_price": 120, "retail_price": None},
    ],
    "recommendation": {"name": "Produttori del Barbaresco 2019",
                       "why": "You lean Italian and this is the classic."},
    "alternates": ["Ruinart Blanc de Blancs NV"],
}

CELLAR = [
    {"name": "Old Vine Reserve Chenin Blanc", "producer": "Ken Forrester Wines"},
    {"name": "Barolo", "producer": "Terre del Barolo"},
]


def test_vintage_is_not_printed_twice():
    """List entries usually already carry the vintage in the printed name."""
    assert _wine_label({"name": "Alheit Cartology 2022", "vintage": 2022}) == "Alheit Cartology 2022"
    assert _wine_label({"name": "Alheit Cartology", "vintage": 2022}) == "Alheit Cartology 2022"
    assert _wine_label({"name": "Ruinart Blanc de Blancs NV"}) == "Ruinart Blanc de Blancs NV"


def test_owned_wines_are_matched_locally():
    """The model missed this one because only a slice of the cellar fit the prompt."""
    data = {k: (list(v) if isinstance(v, list) else v) for k, v in LIST_DATA.items()}
    data["wines"] = [dict(w) for w in LIST_DATA["wines"]]

    hits = mark_owned_wines(data, CELLAR)

    assert hits == 1
    owned = [w["name"] for w in data["wines"] if w["already_owned"]]
    assert owned == ["Ken Forrester Old Vine Reserve 2021"]


def test_owned_matching_needs_two_words_not_one():
    """A shared grape or region alone must not count as a match."""
    data = {"wines": [{"name": "Some Other Chenin Blanc", "producer": "Nobody"}]}
    assert mark_owned_wines(data, CELLAR) == 0

    # ...but the real producer still matches on its own name.
    same = {"wines": [{"name": "Old Vine Reserve", "producer": "Ken Forrester"}]}
    assert mark_owned_wines(same, CELLAR) == 1


def test_owned_matching_is_a_noop_for_an_empty_cellar():
    data = {"wines": [dict(w) for w in LIST_DATA["wines"]]}
    assert mark_owned_wines(data, []) == 0


def test_pick_view_leads_with_the_recommendation_and_fits_telegram():
    text, keyboard = build_winelist_view(LIST_DATA)
    assert "I'd go for" in text
    assert "Produttori del Barbaresco 2019" in text
    assert "You lean Italian" in text
    assert "on the list" in text
    assert len(text) < 4096
    rows = keyboard.inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert "More on this" not in labels
    assert not any("Show all" in x for x in labels)
    wine_rows = [r for r in rows if len(r) == 2 and r[1].text == "Wrong"]
    assert len(wine_rows) == 3
    names = [r[0].text for r in wine_rows]
    assert any("Barbaresco" in n for n in names)
    assert any("Ken Forrester" in n for n in names)
    assert any("Ruinart" in n for n in names)
    assert any(n.startswith("⭐") for n in names)


def test_full_view_paginates():
    text, keyboard = build_winelist_view(LIST_DATA, page=0, per_page=2, show_all=True)
    rows = keyboard.inline_keyboard
    labels = [b.text for row in rows for b in row]
    wine_rows = [r for r in rows if len(r) == 2 and r[1].text == "Wrong"]
    assert len(wine_rows) == 2
    names = [r[0].text for r in wine_rows]
    assert any("Ken Forrester" in n for n in names)
    assert not any("Ruinart" in n for n in names)  # page 2
    assert any("1/2" in x for x in labels)
    assert any("Back to the pick" in x for x in labels)


def test_unreadable_list_says_so_rather_than_showing_nothing():
    text, keyboard = build_winelist_view({"wines": [], "recommendation": None})
    assert "couldn't read" in text.lower()
    assert not keyboard.inline_keyboard


def test_bottle_group_does_not_invent_a_list_price():
    """A photo of labels is not a wine list — even if the model still emits one."""
    data = {
        **LIST_DATA,
        "scene": "bottles",
        "wines": [dict(w) for w in LIST_DATA["wines"]],
    }
    text, keyboard = build_winelist_view(data)
    assert "on the list" not in text
    assert "Spotted 3 wines" in text
    assert "I'd go for" in text  # bottle groups still get a pick
    assert "retail" in text  # ballpark shop price is still useful
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert not any(x == "More on this" for x in labels)
    assert any("Ruinart" in x for x in labels)  # named row, Wrong sits beside it
    full, _ = build_winelist_view(data, show_all=True)
    assert "on the list" not in full


def test_shelf_photo_labels_the_tag_not_the_list():
    data = {
        **LIST_DATA,
        "scene": "shelf",
        "wines": [dict(w) for w in LIST_DATA["wines"]],
    }
    text, _ = build_winelist_view(data)
    assert "on the list" not in text
    assert "on the shelf" in text


def test_menu_scene_still_says_on_the_list():
    data = {**LIST_DATA, "scene": "menu"}
    text, _ = build_winelist_view(data)
    assert "on the list" in text
    assert "Read 3 wines" in text


def test_winelist_prompt_forbids_invented_list_prices():
    from integrations.grok_vision import build_winelist_prompt, build_triage_prompt

    prompt = build_winelist_prompt("they drink Chenin", "GBP", "GB")
    assert '"scene": "bottles"' in prompt
    assert "MUST be null" in prompt
    assert "This is NOT a wine list" in prompt
    assert '"detail"' in prompt
    assert "two short sentences" in prompt.lower()

    triage = build_triage_prompt("GBP", "GB")
    assert "MORE THAN ONE WINE" in triage
    assert 'not "it must be a priced wine list"' in triage
    assert "list_price" in triage
    assert "Never copy market_price" in triage


def test_correction_replaces_the_misread_slot():
    data = {
        **LIST_DATA,
        "wines": [dict(w) for w in LIST_DATA["wines"]],
        "recommendation": dict(LIST_DATA["recommendation"]),
    }
    entry = list_entry_from_label({
        "name": "Kanonkop Pinotage", "producer": "Kanonkop",
        "vintage": 2021, "wine_type": "RE",
        "description": "Dark fruit and smoke.", "market_price": 22,
    })
    assert apply_winelist_correction(data, 2, entry, "Ruinart Blanc de Blancs NV")
    assert data["wines"][2]["name"] == "Kanonkop Pinotage"
    assert data["wines"][2]["list_price"] is None
    assert data["recommendation"]["name"] != "Kanonkop Pinotage"  # wasn't the pick


def test_correction_retargets_the_recommendation_when_that_wine_was_wrong():
    data = {
        **LIST_DATA,
        "wines": [dict(w) for w in LIST_DATA["wines"]],
        "recommendation": dict(LIST_DATA["recommendation"]),
    }
    entry = {"name": "The Real Barbaresco", "note": "Tar and roses."}
    assert apply_winelist_correction(
        data, 1, entry, "Produttori del Barbaresco 2019",
    )
    assert data["recommendation"]["name"] == "The Real Barbaresco"


def test_correction_retargets_pick_when_vintage_label_disagrees():
    """The pick is stored as Grok named it; Wrong uses the display label."""
    data = {
        **LIST_DATA,
        "wines": [dict(w) for w in LIST_DATA["wines"]],
        "recommendation": {"name": "Produttori del Barbaresco", "why": "Classic."},
    }
    entry = {"name": "Dézaley Argile Grand Cru Chasselas", "note": "Alpine, tense."}
    assert apply_winelist_correction(
        data, 1, entry, "Produttori del Barbaresco 2019",
    )
    assert data["recommendation"]["name"] == "Dézaley Argile Grand Cru Chasselas"
    text, keyboard = build_winelist_view(data)
    assert "Dézaley" in text
    assert "I'd go for" in text
    names = [r[0].text for r in keyboard.inline_keyboard if len(r) == 2]
    assert any("Dézaley" in n for n in names)


def test_cellar_summary_describes_taste_not_just_names():
    summary = build_cellar_summary([
        {"name": "A", "wine_type": "RE", "country": "ZA", "grape": "Cabernet", "producer": "X"},
        {"name": "B", "wine_type": "RE", "country": "ZA", "grape": "Cabernet", "producer": "Y"},
        {"name": "C", "wine_type": "WH", "country": "FR", "grape": "Chenin", "producer": "Z"},
    ])
    assert "Red (2)" in summary
    assert "ZA (2)" in summary
    assert "Cabernet (2)" in summary


def test_cellar_summary_is_empty_for_an_empty_cellar():
    assert build_cellar_summary([]) == ""


def test_long_list_does_not_put_a_wrong_button_per_wine():
    wines = [
        {"name": f"Wine {i}", "note": "A short tasting note.", "wine_type": "RE",
         "list_price": None, "retail_price": 20}
        for i in range(30)
    ]
    data = {
        **LIST_DATA,
        "wines": wines,
        "recommendation": {"name": "Wine 0", "why": "Closest to what you drink."},
        "alternates": ["Wine 1"],
        "scene": "menu",
    }
    text, keyboard = build_winelist_view(data)
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "I'd go for" in text
    assert any("Show all 30" in x for x in labels)
    wine_rows = [
        r for r in keyboard.inline_keyboard
        if len(r) == 2 and r[1].text == "Wrong"
    ]
    assert len(wine_rows) == 1  # only the pick — not 30 Wrong buttons
    assert "Wine 0" in wine_rows[0][0].text

    full, kb2 = build_winelist_view(data, show_all=True, per_page=5)
    labels2 = [b.text for row in kb2.inline_keyboard for b in row]
    page_rows = [r for r in kb2.inline_keyboard if len(r) == 2 and r[1].text == "Wrong"]
    assert len(page_rows) == 5
    assert any("Wine 0" in r[0].text for r in page_rows)
    assert not any("Wine 5" in r[0].text for r in page_rows)
    assert any("1/6" in x for x in labels2)


def test_detail_card_has_wrong_and_a_longer_note():
    data = {
        **LIST_DATA,
        "wines": [
            {**LIST_DATA["wines"][0],
             "producer": "Ken Forrester",
             "detail": "Stellenbosch Chenin, honeyed and rich. Drink with roast chicken."},
            *[dict(w) for w in LIST_DATA["wines"][1:]],
        ],
    }
    text, keyboard = build_winelist_view(data, detail_index=0)
    assert "Ken Forrester" in text
    assert "roast chicken" in text
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Wrong" in labels
    assert any("Back" in x for x in labels)
