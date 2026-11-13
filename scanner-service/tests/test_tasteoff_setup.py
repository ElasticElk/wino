"""Taste-off setup: only the host gets the setup buttons, and picking a
bottle never dead-ends."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import t
from integrations.telegram_bot import TelegramBotHandler
from integrations.telegram_clubs import club_cb

CLUB = {"id": 1, "name": "Thursday reds", "cellar_visibility": "full"}
SETUP = {
    "id": 9, "title": "Friday flight", "status": "setup", "host_id": 5,
    "host_name": "Riley", "bottles": [],
}


def _handler(*, is_host):
    api = MagicMock()
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    api.list_clubs.return_value = {"clubs": [dict(CLUB)]}
    api.get_club.return_value = dict(CLUB)
    api.list_tasteoffs.return_value = {"tasteoffs": [dict(SETUP, is_host=is_host)]}
    api.get_tasteoff.return_value = dict(SETUP, is_host=is_host)
    api.club_members.return_value = {"members": [
        {"user_id": 5, "display_name": "Riley", "peek": "full"},
    ]}
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    handler.set_flags({"feature_clubs": "on", "feature_tasteoffs": "on"})
    return handler, api


def _tap(data):
    u = MagicMock()
    u.effective_chat.id = 4242
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Sam"
    query = AsyncMock()
    query.data = data
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.reply_markup = None
    query.edit_message_text = AsyncMock()
    u.callback_query = query
    return u


async def _run(handler, data):
    tap = _tap(data)
    await handler._club_flow().on_callback(tap, MagicMock())
    call = tap.callback_query.edit_message_text.call_args
    markup = call.kwargs.get("reply_markup")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
    return call.args[0], datas


@pytest.mark.asyncio
async def test_non_host_sees_who_hosts_not_the_setup_buttons():
    handler, _api = _handler(is_host=False)
    text, datas = await _run(handler, club_cb("to", 1))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")
    assert datas == [club_cb("h", 1)]
    assert club_cb("op", 9) not in datas


@pytest.mark.asyncio
async def test_host_still_gets_the_setup_buttons():
    handler, _api = _handler(is_host=True)
    text, datas = await _run(handler, club_cb("to", 1))
    assert "Friday flight" in text
    for action in ("tb", "tl", "tf", "op", "tx"):
        assert club_cb(action, 9) in datas


@pytest.mark.asyncio
async def test_older_server_without_is_host_keeps_setup_buttons():
    handler, api = _handler(is_host=True)
    api.get_tasteoff.return_value = dict(SETUP)  # no is_host key
    _text, datas = await _run(handler, club_cb("to", 1))
    assert club_cb("op", 9) in datas


@pytest.mark.asyncio
async def test_stale_open_tap_by_non_host_says_who_hosts():
    # Before: 403 forbidden -> "Couldn't reach the club just now".
    handler, api = _handler(is_host=False)
    api.open_tasteoff.return_value = {"error": "forbidden", "status_code": 403}
    text, _datas = await _run(handler, club_cb("op", 9))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")
    assert text != t("en", "club.err")


@pytest.mark.asyncio
async def test_stale_add_from_cellar_by_non_host_says_who_hosts():
    handler, api = _handler(is_host=False)
    api.club_peek.return_value = {"visibility": "full", "wines": [
        {"id": 154, "name": "Chenin Blanc", "vintage": 2022},
    ]}
    api.add_tasteoff_bottle.return_value = {"error": "forbidden", "status_code": 403}
    text, _datas = await _run(handler, club_cb("tw", 9, 154))
    assert text == t("en", "club.to.hosting", host="Riley", title="Friday flight")


@pytest.mark.asyncio
async def test_empty_cellar_offers_type_a_name_and_back():
    # Before: "This club only shares a summary" with no buttons.
    handler, api = _handler(is_host=True)
    api.club_peek.return_value = {"visibility": "full", "wines": []}
    text, datas = await _run(handler, club_cb("tm", 9, 5))
    assert text == t("en", "club.to.cellar_empty")
    assert datas == [club_cb("tb", 9), club_cb("to", 1)]


@pytest.mark.asyncio
async def test_summary_only_club_still_offers_a_way_on():
    handler, api = _handler(is_host=True)
    api.get_club.return_value = dict(CLUB, cellar_visibility="summary")
    text, datas = await _run(handler, club_cb("tf", 9))
    assert text == t("en", "club.to.summary_only")
    assert datas == [club_cb("tb", 9), club_cb("to", 1)]


def test_new_copy_has_afrikaans():
    for key in ("club.to.hosting", "club.to.cellar_empty"):
        assert t("af", key) != t("en", key)


# --- Scoring card -------------------------------------------------------------

SCORING = {
    "id": 9, "title": "Friday flight", "status": "scoring", "host_id": 5,
    "host_name": "Riley", "is_host": False,
    "bottles": [
        {"id": 1, "label": "Chataue", "your_score": None},
        {"id": 2, "label": "Château Lilian Ladouys 2018 Saint-Estèphe", "your_score": None},
    ],
}


def _scoring_handler(**over):
    handler, api = _handler(is_host=False)
    body = dict(SCORING, **over)
    api.list_tasteoffs.return_value = {"tasteoffs": [body]}
    api.get_tasteoff.return_value = body
    api.score_tasteoff.return_value = {"bottle_id": 1, "score": 4}
    return handler, api


def _rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


@pytest.mark.asyncio
async def test_name_button_never_scores():
    # Regression: the wine's name sat on its score-1 button, so tapping the
    # name to read it scored it 1.
    handler, api = _scoring_handler()
    tap = _tap(club_cb("tn", 9, 2))
    await handler._club_flow().on_callback(tap, MagicMock())
    api.score_tasteoff.assert_not_called()
    toast = tap.callback_query.answer.call_args
    assert "Château Lilian Ladouys 2018 Saint-Estèphe" in toast.args[0]
    tap.callback_query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_scoring_card_lists_full_names_and_separates_name_from_scores():
    handler, _api = _scoring_handler()
    text, _datas = await _run(handler, club_cb("to", 1))
    assert "1. Chataue" in text
    assert "2. Château Lilian Ladouys 2018 Saint-Estèphe" in text
    tap = _tap(club_cb("to", 1))
    await handler._club_flow().on_callback(tap, MagicMock())
    rows = _rows(tap.callback_query.edit_message_text.call_args.kwargs["reply_markup"])
    assert rows[0] == [("\U0001F377 1. Chataue", club_cb("tn", 9, 1))]
    assert rows[1] == [(str(n), club_cb("ts", 9, 1, n)) for n in range(1, 6)]
    assert rows[2][0][1] == club_cb("tn", 9, 2)
    assert [d for _t, d in rows[3]] == [club_cb("ts", 9, 2, n) for n in range(1, 6)]
    # No score button carries a wine name.
    for row in (rows[1], rows[3]):
        assert all(text in {"1", "2", "3", "4", "5"} for text, _d in row)


@pytest.mark.asyncio
async def test_scoring_one_wine_keeps_the_other_scorable():
    # Regression: the first score replaced the card and dropped every button.
    handler, api = _scoring_handler()
    scored = dict(SCORING, bottles=[
        dict(SCORING["bottles"][0], your_score=4), dict(SCORING["bottles"][1]),
    ])
    api.get_tasteoff.return_value = scored
    tap = _tap(club_cb("ts", 9, 1, 4))
    await handler._club_flow().on_callback(tap, MagicMock())
    api.score_tasteoff.assert_called_once_with(1, 9, 1, 4)
    call = tap.callback_query.edit_message_text.call_args
    assert "1. Chataue — 4" in call.args[0]
    rows = _rows(call.kwargs["reply_markup"])
    assert ("✅4", club_cb("ts", 9, 1, 4)) in rows[1]
    assert [d for _t, d in rows[3]] == [club_cb("ts", 9, 2, n) for n in range(1, 6)]


@pytest.mark.asyncio
async def test_taste_off_button_during_scoring_opens_the_card():
    # Before: "There's already a tasting open." with no way back to score.
    handler, _api = _scoring_handler()
    text, datas = await _run(handler, club_cb("to", 1))
    assert text != t("en", "club.to.already")
    assert club_cb("ts", 9, 2, 5) in datas
    assert club_cb("tc", 9) not in datas


@pytest.mark.asyncio
async def test_host_scoring_card_has_close():
    handler, _api = _scoring_handler(is_host=True)
    _text, datas = await _run(handler, club_cb("to", 1))
    assert club_cb("tc", 9) in datas


@pytest.mark.asyncio
async def test_score_after_close_says_closed():
    handler, api = _scoring_handler()
    api.score_tasteoff.return_value = {"error": "not_scoring", "status_code": 409}
    text, _datas = await _run(handler, club_cb("ts", 9, 1, 3))
    assert text == t("en", "club.to.closed")


def test_scoring_copy_has_afrikaans():
    for key in ("club.to.scoring_hint", "club.to.name_tap"):
        assert t("af", key) != t("en", key)


# --- Host scores too, who has scored, close confirm, reopen -------------------

SCORERS_WAITING = [
    {"name": "Riley", "scored": 2, "done": True},
    {"name": "Sam", "scored": 0, "done": False},
]


@pytest.mark.asyncio
async def test_host_open_shows_the_scoring_card_not_just_close():
    # Regression: the host's "Scoring is open" had only Close, no scores.
    handler, api = _scoring_handler(is_host=True)
    api.open_tasteoff.return_value = {"id": 9, "status": "scoring", "status_code": 200}
    _text, datas = await _run(handler, club_cb("op", 9))
    assert club_cb("ts", 9, 1, 3) in datas and club_cb("ts", 9, 2, 5) in datas
    assert club_cb("tc", 9) in datas


@pytest.mark.asyncio
async def test_scoring_card_says_who_has_scored():
    handler, _api = _scoring_handler(scorers=SCORERS_WAITING)
    text, _datas = await _run(handler, club_cb("to", 1))
    assert t("en", "club.to.scored_by", names="Riley ✓, Sam …") in text


@pytest.mark.asyncio
async def test_close_asks_first_when_someone_has_not_scored():
    handler, api = _scoring_handler(is_host=True, scorers=SCORERS_WAITING)
    text, datas = await _run(handler, club_cb("tc", 9))
    api.close_tasteoff.assert_not_called()
    assert text == t("en", "club.to.close_ask", names="Sam")
    assert datas == [club_cb("tz", 9), club_cb("to", 1)]


@pytest.mark.asyncio
async def test_close_anyway_closes_and_host_can_reopen():
    handler, api = _scoring_handler(is_host=True, scorers=SCORERS_WAITING)
    api.close_tasteoff.return_value = {
        "id": 9, "title": "Friday flight", "status": "closed", "can_reopen": True,
        "bottles": [{"id": 1, "label": "Chataue", "average": 4.0, "n": 1}],
    }
    text, datas = await _run(handler, club_cb("tz", 9))
    api.close_tasteoff.assert_called_once_with(1, 9)
    assert "Friday flight" in text
    assert datas == [club_cb("tr", 9)]


@pytest.mark.asyncio
async def test_close_when_everyone_scored_closes_straight_away():
    done = [dict(s, done=True) for s in SCORERS_WAITING]
    handler, api = _scoring_handler(is_host=True, scorers=done)
    api.close_tasteoff.return_value = {"id": 9, "title": "Friday flight", "bottles": []}
    await _run(handler, club_cb("tc", 9))
    api.close_tasteoff.assert_called_once_with(1, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,key", [
    (None, "club.to.reopened"),
    ("too_late", "club.to.reopen_late"),
    ("another_open", "club.to.reopen_busy"),
])
async def test_reopen(error, key):
    handler, api = _scoring_handler(is_host=True)
    api.reopen_tasteoff.return_value = (
        {"error": error, "status_code": 409} if error else {"id": 9, "status": "scoring"}
    )
    text, _datas = await _run(handler, club_cb("tr", 9))
    api.reopen_tasteoff.assert_called_once_with(1, 9)
    assert text == t("en", key)


@pytest.mark.asyncio
async def test_taste_off_offers_reopen_of_a_just_closed_tasting():
    handler, api = _handler(is_host=True)
    closed = dict(SETUP, status="closed", can_reopen=True)
    api.list_tasteoffs.return_value = {"tasteoffs": [closed]}
    text, datas = await _run(handler, club_cb("to", 1))
    assert text == t("en", "club.to.reopen_or_new", title="Friday flight")
    assert datas == [club_cb("tr", 9), club_cb("tq", 1)]
    text, _datas = await _run(handler, club_cb("tq", 1))
    assert text.startswith(t("en", "club.to.title"))


@pytest.mark.asyncio
async def test_old_closed_tasting_goes_straight_to_a_new_one():
    handler, api = _handler(is_host=True)
    api.list_tasteoffs.return_value = {"tasteoffs": [dict(SETUP, status="closed", can_reopen=False)]}
    text, _datas = await _run(handler, club_cb("to", 1))
    assert text.startswith(t("en", "club.to.title"))


def test_reopen_copy_has_afrikaans():
    for key in ("club.to.scored_by", "club.to.close_ask", "club.to.close_anyway",
                "club.to.keep_open", "club.to.reopen", "club.to.reopened",
                "club.to.reopen_late", "club.to.reopen_busy", "club.to.reopen_or_new",
                "club.to.start_new"):
        assert t("af", key) != t("en", key), key
