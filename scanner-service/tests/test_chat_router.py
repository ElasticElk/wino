"""Plain-text chat: shelf answers, one recommendation, writes, and failures.

The model is a fake. These tests check what the person would see and which
API calls were made, including when the model or the cellar is down.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
from telegram.error import BadRequest

from integrations.card_store import CardStore, StoreError
from integrations.chat_models import ChatFailure
from integrations.telegram_bot import TelegramBotHandler, _cb


def _wine(wine_id=1, name="Kanonkop Shiraz", wine_type="RE", grapes=None,
          comment="", storage="Big Fridge", drawer=3, price=18.0, stock=1,
          votes=None, drink_by=None):
    return {
        "id": wine_id,
        "name": name,
        "wine_type": wine_type,
        "grapes": grapes if grapes is not None else ["Syrah"],
        "country": "ZA",
        "region": "",
        "comment": comment,
        "attributes": [],
        "food_pairings": [],
        "producer": "Kanonkop",
        "vintage": 2019,
        "drink_by": drink_by,
        "rating": None,
        "critic_score": None,
        "vivino_rating": None,
        "votes": votes or {"up": 0, "meh": 0, "down": 0},
        "stock_count": stock,
        "price": price,
        "items": [{
            "item_id": wine_id, "storage": storage, "drawer": drawer, "price": price,
        }],
    }


class FakeChat:
    def __init__(self):
        self.parse_result = {"intent": "search"}
        self.recommend_result = {
            "question": "", "options": [],
            "pick": {"id": 1, "reason": "Fennel in the note.", "evidence": "note"},
            "runner_up": {"id": 0, "reason": "", "evidence": "none"},
            "alternatives": [],
            "no_evidence_for": [],
        }
        self.parse_calls = []
        self.recommend_calls = []
        self.fail = None
        self.recommend_fail = None
        self._healthy = True
        self.gate = None
        self.wine_chat_calls = []
        self.wine_chat_reply = "A Syrah would be lovely."
        self.wine_chat_fail = None
        self.support_calls = []
        self.support_reply = "Send /invite and share the link."
        self.support_fail = None
        self.place_calls = []
        self.place_fail = None
        self.places = [{
            "name": "Tannin & Co", "area": "Bree Street", "why": "340 SA labels, strong on Syrah.",
            "url": "https://example.test/tannin",
        }]

    def healthy(self, _model):
        return self._healthy

    def find_places(self, request, profile, *, area="", lat=None, lon=None, country=""):
        self.place_calls.append({"request": request, "area": area, "lat": lat, "lon": lon, "country": country})
        if self.place_fail:
            raise self.place_fail
        return self.places

    def support(self, sentence, *, staff=False, household_id=None, features=None):
        self.support_calls.append({"sentence": sentence, "staff": staff, "features": features})
        if self.support_fail:
            raise self.support_fail
        return self.support_reply

    def wine_chat(self, sentence, profile, *, household_id=None):
        self.wine_chat_calls.append((sentence, profile))
        if self.wine_chat_fail:
            raise self.wine_chat_fail
        return self.wine_chat_reply

    def parse(self, text, context):
        self.parse_calls.append((text, context))
        if self.fail:
            raise self.fail
        return self.parse_result

    def recommend(self, sentence, taste, block, profile, history, *, allow_question,
                  household_id=None, answer=None, valid_ids=None):
        self.recommend_calls.append({
            "sentence": sentence, "allow_question": allow_question, "answer": answer,
            "household_id": household_id, "valid_ids": valid_ids,
        })
        if self.gate is not None:
            self.gate.wait(2)
        if self.recommend_fail:
            raise self.recommend_fail
        return self.recommend_result


def _api(wines):
    api = MagicMock()
    api.household_id = 14
    api.web_url = "https://cellar.test"
    api.wines_in_stock.return_value = wines
    api.list_storages.return_value = [{"name": "Big Fridge"}, {"name": "Small Fridge"}]
    api.list_all_wines.return_value = []
    api.get_taste.return_value = {"summary": "Mostly red."}
    api.stock_history.return_value = {"items": []}
    api.record_intent.return_value = {"intent_id": 5}
    api.update_intent.return_value = {"id": 5, "payload": {}}
    api.search_wines.return_value = []
    api.update_wine.return_value = {"id": 1}
    return api


def _handler(api, chat=None, asks=None):
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
        admin_chat_ids=["99"],
        ask_store=asks if asks is not None else CardStore(ttl=1800, prefix="whiney:ask:"),
    )
    if chat is not None:
        handler.set_chat_models(chat)
    return handler


def _text(text):
    sent = AsyncMock()
    sent.chat_id = 4242
    sent.message_id = 20
    sent.edit_text = AsyncMock()
    sent.reply_text = AsyncMock()
    msg = AsyncMock()
    msg.text = text
    msg.chat_id = 4242
    msg.message_id = 11
    msg.reply_text = AsyncMock(return_value=sent)
    update = MagicMock()
    update.message = msg
    update.effective_chat.id = 4242
    update.edited_message = None
    return update, sent


def _context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


def _labels(markup):
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def _last_edit(sent):
    return sent.edit_text.await_args


async def _fast(_delay):
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_without_chat_models_text_searches_exactly_as_before():
    api = _api([_wine()])
    handler = _handler(api)
    handler._do_search = AsyncMock()
    update, _sent = _text("Kanonkop")
    await handler._unknown_text(update, _context())
    handler._do_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_wine_word_skips_parse_and_searches():
    api = _api([_wine(name="Kanonkop Shiraz")])
    chat = FakeChat()
    handler = _handler(api, chat)
    update, _sent = _text("Shiraz")
    assert await handler._router.handle_text(update, _context(), "Shiraz") is False
    assert chat.parse_calls == []


@pytest.mark.asyncio
async def test_search_and_short_unsure_fall_back_to_search():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "unsure"}
    handler = _handler(api, chat)
    update, _sent = _text("aux moines")
    assert await handler._router.handle_text(update, _context(), "aux moines") is False
    chat.parse_result = {"intent": "search"}
    update, _sent = _text("kanonkop paul sauer 2019")
    assert await handler._router.handle_text(update, _context(), "kanonkop paul sauer 2019") is False
    assert chat.wine_chat_calls == []


@pytest.mark.asyncio
async def test_restaurant_question_gets_a_wine_answer_not_a_search():
    api = _api([_wine()])
    api.get_taste.return_value = {"summary": "Likes Syrah"}
    chat = FakeChat()
    chat.parse_result = {"intent": "wine_chat"}
    chat.wine_chat_reply = "Go for a Northern Rhône Syrah & send me the list."
    handler = _handler(api, chat)
    handler._do_search = AsyncMock()
    update, _sent = _text("I'm at a steakhouse, what should I order?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.wine_chat_calls == [("I'm at a steakhouse, what should I order?", "Likes Syrah")]
    body = update.message.reply_text.await_args.args[0]
    assert "Northern Rhône Syrah &amp; send" in body
    handler._do_search.assert_not_called()
    api.record_intent.assert_not_called()


def _location_update(lat=-33.92, lon=18.42):
    update, sent = _text("")
    update.message.location = MagicMock(latitude=lat, longitude=lon, horizontal_accuracy=10, live_period=None)
    return update, sent


@pytest.mark.asyncio
async def test_near_me_asks_for_location_then_searches_with_it():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "place", "area": ""}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    handler._chat_id_to_country["4242"] = "ZA"
    update, _sent = _text("Is there a restaurant near me with good wine for my taste?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.place_calls == []
    prompt = update.message.reply_text.await_args
    assert "share where you are" in prompt.args[0]
    keyboard = prompt.kwargs["reply_markup"]
    assert keyboard.keyboard[0][0].request_location is True

    loc, sent = _location_update()
    assert await handler._router.on_location(loc, _context()) is True
    call = chat.place_calls[0]
    assert call["lat"] == -33.92 and call["lon"] == 18.42
    assert call["request"] == "Is there a restaurant near me with good wine for my taste?"
    assert call["country"] == "ZA"
    final = sent.edit_text.await_args.args[0]
    assert "Tannin &amp; Co" in final
    assert 'href="https://example.test/tannin"' in final
    assert handler._asks.get("place:4242") is None
    api.update_intent.assert_not_called()


@pytest.mark.asyncio
async def test_keyboard_removal_is_its_own_message_so_the_placeholder_stays_editable():
    from telegram import ReplyKeyboardRemove

    handler = _handler(_api([_wine()]), FakeChat())
    handler._router._sleep = _fast
    handler._asks["place:4242"] = {"query": "restaurant near me"}
    loc, _sent = _location_update()
    await handler._router.on_location(loc, _context())
    first, second = loc.message.reply_text.await_args_list[:2]
    assert isinstance(first.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    assert "reply_markup" not in second.kwargs


@pytest.mark.asyncio
async def test_place_progress_keeps_rotating_after_a_failed_edit():
    from integrations.chat_router import PLACE_LINES, PLACE_STEPS, ProgressTicker

    message = AsyncMock()
    calls = []

    async def edit(text, **_kw):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram hiccup")

    message.edit_text = edit
    ticker = ProgressTicker(message, dict(enumerate(PLACE_LINES)), sleep=_fast, steps=PLACE_STEPS)
    async with ticker:
        for _ in range(60):
            await asyncio.sleep(0)
    assert len(calls) == len(PLACE_STEPS)
    assert len(set(calls)) == len(PLACE_LINES)
    assert calls[0] != PLACE_LINES[0]


def test_place_progress_outlasts_the_search_timeout():
    from integrations.chat_models import PLACE_TIMEOUT
    from integrations.chat_router import PLACE_STEPS

    assert PLACE_STEPS[-1][0] > PLACE_TIMEOUT


@pytest.mark.asyncio
async def test_named_area_searches_straight_away():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "place", "area": "Stellenbosch"}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("good wine bars in Stellenbosch?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.place_calls[0]["area"] == "Stellenbosch"
    assert "Good wine in Stellenbosch" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_typed_suburb_answers_the_location_prompt():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    handler._asks["place:4242"] = {"query": "restaurant near me with good wine"}
    update, _sent = _text("Sea Point")
    assert await handler._router.handle_text(update, _context(), "Sea Point") is True
    assert chat.place_calls[0]["area"] == "Sea Point"
    assert chat.parse_calls == []


@pytest.mark.asyncio
async def test_a_new_question_drops_the_location_prompt():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "wine_chat"}
    handler = _handler(api, chat)
    handler._asks["place:4242"] = {"query": "restaurant near me"}
    update, _sent = _text("actually, what goes with lamb tonight?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.place_calls == []
    assert handler._asks.get("place:4242") is None
    update, _sent = _text("Kanonkop")
    await handler._router.handle_text(update, _context(), "Kanonkop")
    assert chat.place_calls == []


@pytest.mark.asyncio
async def test_slash_command_drops_the_location_prompt():
    handler = _handler(_api([_wine()]), FakeChat())
    handler._asks["place:4242"] = {"query": "restaurant near me"}
    await handler._router.abandon("4242")
    assert handler._asks.get("place:4242") is None


@pytest.mark.asyncio
async def test_place_search_failure_and_empty_results_are_friendly():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "place", "area": "Paarl"}
    chat.place_fail = ChatFailure("timeout", "https://api.x.ai exploded")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("wine farms in Paarl?")
    await handler._router.handle_text(update, _context(), update.message.text)
    body = sent.edit_text.await_args.args[0]
    assert "couldn't search for places" in body
    assert "x.ai" not in body

    chat.place_fail = None
    chat.places = []
    update, sent = _text("wine farms in Paarl?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "anywhere there I'd vouch for" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_location_without_a_pending_place_keeps_the_old_behaviour():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    loc, _sent = _location_update()
    loc.message.date = None
    assert await handler._router.on_location(loc, _context()) is True
    assert chat.place_calls == []
    assert api.update_intent.call_args.args[1]["place"]["lat"] == -33.92


@pytest.mark.asyncio
async def test_unsure_full_sentence_asks_instead_of_inventing():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "unsure"}
    handler = _handler(api, chat)
    update, _sent = _text("is it ok to chill a pinot noir?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.wine_chat_calls == []
    body = update.message.reply_text.await_args.args[0]
    assert "not sure" in body.lower() or "didn't quite catch" in body.lower()
    labels = _labels(update.message.reply_text.await_args.kwargs.get("reply_markup"))
    assert "What's in stock" in labels
    assert "What should I drink" in labels


@pytest.mark.asyncio
async def test_help_invite_asks_the_support_model_not_wine_chat():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "help", "help_topic": "invite"}
    handler = _handler(api, chat)
    update, _sent = _text("how do I add someone to my house?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.support_calls[0]["sentence"] == "how do I add someone to my house?"
    assert chat.support_calls[0]["staff"] is False
    assert "/invite" in update.message.reply_text.await_args.args[0]
    assert chat.wine_chat_calls == []
    assert chat.recommend_calls == []


@pytest.mark.asyncio
async def test_help_for_an_unlisted_question_still_answers():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "help", "help_topic": "other"}
    chat.support_reply = "They need their own Telegram, then you send /invite."
    handler = _handler(api, chat)
    update, _sent = _text("does my wife need her own Telegram to join?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.support_calls[0]["sentence"] == "does my wife need her own Telegram to join?"
    assert "own Telegram" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_help_tells_staff_they_are_staff():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "help", "help_topic": "new_cellar"}
    handler = _handler(api, chat)
    handler.admin_chat_ids = ["4242"]
    update, _sent = _text("can I create a new house?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.support_calls[0]["staff"] is True


@pytest.mark.asyncio
async def test_help_take_out_offers_catch_up():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "help", "help_topic": "take_out"}
    chat.support_reply = "After a shuffle, /fridge then Catch up walks a drawer."
    handler = _handler(api, chat)
    update, _sent = _text("the fridge is wrong, I moved things around")
    await handler._router.handle_text(update, _context(), update.message.text)
    kwargs = update.message.reply_text.await_args.kwargs
    markup = kwargs.get("reply_markup")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Catch up" in labels
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "a:cu,o:go" in data


@pytest.mark.asyncio
async def test_help_falls_back_to_canned_when_the_model_is_down():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "help", "help_topic": "invite"}
    chat.support_fail = ChatFailure("timeout", "https://api.x.ai exploded")
    handler = _handler(api, chat)
    update, _sent = _text("how do I add someone to my house?")
    await handler._router.handle_text(update, _context(), update.message.text)
    body = update.message.reply_text.await_args.args[0]
    assert "/invite" in body
    assert "x.ai" not in body


@pytest.mark.asyncio
async def test_off_topic_is_declined_without_a_model_call():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "offtopic"}
    handler = _handler(api, chat)
    update, _sent = _text("how high does a plane fly?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert "only any good at wine" in update.message.reply_text.await_args.args[0]
    assert chat.wine_chat_calls == []
    assert chat.recommend_calls == []


@pytest.mark.asyncio
async def test_wine_chat_failure_says_resting_and_never_shows_the_error():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "wine_chat"}
    chat.wine_chat_fail = ChatFailure("timeout", "https://api.x.ai exploded")
    handler = _handler(api, chat)
    update, _sent = _text("what's a good Barolo year?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    body = update.message.reply_text.await_args.args[0]
    assert "having a rest" in body
    assert "x.ai" not in body


@pytest.mark.asyncio
async def test_wine_chat_without_taste_profile_still_answers():
    api = _api([_wine()])
    api.get_taste.side_effect = RuntimeError("down")
    chat = FakeChat()
    chat.parse_result = {"intent": "wine_chat"}
    handler = _handler(api, chat)
    update, _sent = _text("should I decant a 2015 Rioja?")
    assert await handler._router.handle_text(update, _context(), update.message.text) is True
    assert chat.wine_chat_calls[0][1] == ""


@pytest.mark.asyncio
async def test_drawer_question_replies_from_stock_without_search():
    api = _api([_wine(storage="Small Fridge", drawer=3)])
    chat = FakeChat()
    chat.parse_result = {"intent": "fact", "fact": {
        "kind": "drawer", "storage_text": "small fridge", "drawer": 3,
    }}
    handler = _handler(api, chat)
    handler._do_search = AsyncMock()
    update, sent = _text("what is in D3 of my small fridge")
    await handler._unknown_text(update, _context())
    handler._do_search.assert_not_called()
    body = update.message.reply_text.await_args.args[0]
    assert "Kanonkop Shiraz" in body
    assert "Looking" not in body
    assert sent.edit_text.await_count == 0


@pytest.mark.asyncio
async def test_two_fridges_offers_buttons_and_tap_answers():
    wines = [
        _wine(1, storage="Big Fridge", drawer=1),
        _wine(2, name="Other", storage="Small Fridge", drawer=1),
    ]
    api = _api(wines)
    chat = FakeChat()
    chat.parse_result = {"intent": "fact", "fact": {
        "kind": "drawer", "storage_text": "fridge", "drawer": 1,
    }}
    handler = _handler(api, chat)
    update, _sent = _text("what's in D1 of the fridge")
    await handler._router.handle_text(update, _context(), update.message.text)
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert _labels(markup) == ["Big Fridge", "Small Fridge"]
    query = AsyncMock()
    query.message = update.message
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "cf", "page": 0}, _context())
    bodies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("Kanonkop Shiraz" in body for body in bodies)


@pytest.mark.asyncio
async def test_list_all_reuses_stock_card():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "fact", "fact": {"kind": "list_all"}}
    handler = _handler(api, chat)
    handler._cmd_stock = AsyncMock()
    update, _sent = _text("list all my wines")
    await handler._router.handle_text(update, _context(), update.message.text)
    handler._cmd_stock.assert_awaited_once()


@pytest.mark.asyncio
async def test_shiraz_miss_offers_suggest_close_button():
    api = _api([_wine(grapes=["Chenin Blanc"], wine_type="WH")])
    chat = FakeChat()
    chat.parse_result = {"intent": "fact", "fact": {"kind": "grape", "grape": "Nebbiolo"}}
    handler = _handler(api, chat)
    update, _sent = _text("any nebbiolo?")
    await handler._router.handle_text(update, _context(), update.message.text)
    body = update.message.reply_text.await_args.args[0]
    assert "don't have a Nebbiolo" in body
    assert _labels(update.message.reply_text.await_args.kwargs["reply_markup"]) == [
        "Suggest something close",
    ]


@pytest.mark.asyncio
async def test_empty_cellar_uses_empty_card():
    api = _api([])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    handler = _handler(api, chat)
    update, _sent = _text("what should I drink")
    await handler._router.handle_text(update, _context(), update.message.text)
    body = update.message.reply_text.await_args.args[0]
    assert "Send me a photo" in body
    assert chat.recommend_calls == []


@pytest.mark.asyncio
async def test_early_exits_still_win_with_chat_enabled():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._await_producer["4242"] = {"barcode": "_label_1", "time": time.time()}
    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Glen"}, "time": time.time(),
    }
    handler._add_pending_wine = AsyncMock()
    update, _sent = _text("G.W.M")
    await handler._unknown_text(update, _context())
    assert chat.parse_calls == []
    handler._add_pending_wine.assert_awaited()


@pytest.mark.asyncio
async def test_slash_command_abandons_open_ask():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"intent_id": 5, "stage": "shown"}
    update, _sent = _text("/stock")
    await handler._abandon_invite_if_command(update, _context())
    assert handler._asks.get("ask:4242") is None
    api.update_intent.assert_called_once()
    assert api.update_intent.call_args.args[1]["outcome"] == "abandoned"


@pytest.mark.asyncio
async def test_recommend_sends_placeholder_then_edits_it_into_the_pick():
    api = _api([_wine(comment="fennel")])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["fennel"]}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("a deep red with fennel")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert update.message.reply_text.await_count == 1
    assert "Looking through your cellar" in update.message.reply_text.await_args.args[0]
    final = sent.edit_text.await_args_list[-1].args[0]
    assert "Kanonkop Shiraz" in final
    assert "Fennel in the note." in final
    assert _labels(sent.edit_text.await_args_list[-1].kwargs["reply_markup"]) == [
        "That's the one", "Something else", "Not now",
    ]


@pytest.mark.asyncio
async def test_progress_lines_appear_in_order_while_waiting():
    api = _api([_wine(comment="fennel")])
    chat = FakeChat()
    chat.gate = threading.Event()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["fennel"]}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("a deep red with fennel")
    seen = []

    async def edit(text, **kwargs):
        seen.append(text)
        if len(seen) >= 3:
            chat.gate.set()

    sent.edit_text = edit
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "fennel" in seen[0]
    assert "Down to a few" in seen[1]
    assert "longer than usual" in seen[2]
    assert "Kanonkop Shiraz" in seen[-1]
    assert seen.index(seen[0]) < seen.index(seen[-1])


@pytest.mark.asyncio
async def test_no_progress_edit_after_the_pick_lands():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight please")
    await handler._router.handle_text(update, _context(), update.message.text)
    count = sent.edit_text.await_count
    await asyncio.sleep(0.05)
    assert sent.edit_text.await_count == count
    assert "Kanonkop" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_tie_renders_both_in_one_message():
    wines = [_wine(1, name="Alpha"), _wine(2, name="Beta")]
    api = _api(wines)
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["leather"]}}
    chat.recommend_result["runner_up"] = {"id": 2, "reason": "Riper.", "evidence": "note"}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("something leathery please")
    await handler._router.handle_text(update, _context(), update.message.text)
    final = sent.edit_text.await_args.args[0]
    assert "Two fit equally well" in final
    assert "Alpha" in final and "Beta" in final
    assert "That's the one" not in _labels(sent.edit_text.await_args.kwargs["reply_markup"])
    assert "Alpha" in _labels(sent.edit_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_something_else_walks_queue_without_calling_recommend():
    wines = [_wine(1, name="Alpha"), _wine(2, name="Beta")]
    api = _api(wines)
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    chat.recommend_result["alternatives"] = [
        {"id": 2, "reason": "Also good.", "evidence": "grape"},
    ]
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    calls = len(chat.recommend_calls)
    query = AsyncMock()
    query.message = sent
    query.message.chat.id = 4242
    query.message.reply_text = AsyncMock()
    await handler._router.on_callback(query, {"action": "cn"}, _context())
    assert len(chat.recommend_calls) == calls
    assert "Beta" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_queue_exhausted_says_everything_that_fits():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "queue": [], "intent_id": 5}
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "cn"}, _context())
    assert "everything that fits" in query.message.edit_text.await_args.args[0]
    assert api.update_intent.call_args.args[1]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_keep_patches_accepted_and_clears_ask():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "queue": [], "intent_id": 5}
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "ck", "wine_id": 1}, _context())
    assert api.update_intent.call_args.args[1]["outcome"] == "accepted"
    assert handler._asks.get("ask:4242") is None
    assert "Enjoy" in query.message.edit_text.await_args.args[0]


def _tap(chat_id=4242):
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = chat_id
    query.message.chat.id = chat_id
    return query


def _data(markup, label):
    from integrations.telegram_bot import _parse_cb
    for row in markup.inline_keyboard:
        for button in row:
            if button.text == label:
                return _parse_cb(button.callback_data)
    raise AssertionError(f"no {label!r} button in {_labels(markup)}")


@pytest.mark.asyncio
async def test_keep_offers_take_it_out_and_tap_removes_from_that_fridge():
    api = _api([_wine(storage="Big Fridge", drawer=3)])
    api.list_storages.return_value = [{"id": 7, "name": "Big Fridge"}]
    api.remove_stock.return_value = {
        "item_id": 81, "wine_id": 1, "wine_name": "Kanonkop Shiraz 2019",
        "stock_count": 0, "storage": "Big Fridge",
    }
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    query = _tap()
    await handler._router.on_callback(query, {"action": "ck", "wine_id": 1}, _context())
    markup = query.message.edit_text.await_args.kwargs["reply_markup"]
    assert _labels(markup) == ["📤 Take it out now"]

    data = _data(markup, "📤 Take it out now")
    assert data["action"] == "co"
    tap = _tap()
    await handler._router.on_callback(tap, data, _context())
    api.remove_stock.assert_called_once_with(1, storage_name="Big Fridge")
    text = tap.message.edit_text.await_args.args[0]
    assert "Taken out" in text
    assert "from Big Fridge" in text
    assert "How was it?" in text
    labels = _labels(tap.message.edit_text.await_args.kwargs["reply_markup"])
    assert "↩️ Undo" in labels
    assert "👍" in labels


@pytest.mark.asyncio
async def test_bottle_in_two_fridges_gets_one_button_each():
    wine = _wine()
    wine["items"] = [
        {"item_id": 1, "storage": "Big Fridge", "drawer": 3, "price": None},
        {"item_id": 2, "storage": "Small Fridge", "drawer": None, "price": None},
    ]
    api = _api([wine])
    api.list_storages.return_value = [{"id": 7, "name": "Big Fridge"}, {"id": 8, "name": "Small Fridge"}]
    handler = _handler(api, FakeChat())
    query = _tap()
    await handler._router.on_callback(query, {"action": "ck", "wine_id": 1}, _context())
    markup = query.message.edit_text.await_args.kwargs["reply_markup"]
    assert _labels(markup) == ["📤 From Big Fridge", "📤 From Small Fridge"]
    assert _data(markup, "📤 From Small Fridge")["page"] == 8


@pytest.mark.asyncio
async def test_take_it_out_works_after_the_ask_has_expired():
    api = _api([_wine()])
    api.list_storages.return_value = [{"id": 7, "name": "Big Fridge"}]
    api.remove_stock.return_value = {"item_id": 81, "wine_name": "Kanonkop", "stock_count": 2}
    handler = _handler(api, FakeChat())
    tap = _tap()
    await handler._router.on_callback(tap, {"action": "co", "wine_id": 1, "page": 7}, _context())
    api.remove_stock.assert_called_once_with(1, storage_name="Big Fridge")
    assert "In stock: 2" in tap.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_take_it_out_when_already_gone_says_so():
    api = _api([_wine()])
    api.list_storages.return_value = [{"id": 7, "name": "Big Fridge"}]
    api.remove_stock.return_value = {"error": "No stock of this wine in Big Fridge"}
    handler = _handler(api, FakeChat())
    tap = _tap()
    await handler._router.on_callback(tap, {"action": "co", "wine_id": 1, "page": 7}, _context())
    assert "already out of stock" in tap.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_take_it_out_network_failure_changes_nothing_and_says_so():
    api = _api([_wine()])
    api.list_storages.return_value = [{"id": 7, "name": "Big Fridge"}]
    api.remove_stock.side_effect = requests.ConnectionError("down")
    handler = _handler(api, FakeChat())
    tap = _tap()
    await handler._router.on_callback(tap, {"action": "co", "wine_id": 1, "page": 7}, _context())
    assert "nothing changed" in tap.message.reply_text.await_args.args[0]
    tap.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_keep_without_a_known_fridge_has_no_take_out_button():
    api = _api([])
    api.list_storages.side_effect = RuntimeError("down")
    handler = _handler(api, FakeChat())
    query = _tap()
    await handler._router.on_callback(query, {"action": "ck", "wine_id": 1}, _context())
    assert "Enjoy" in query.message.edit_text.await_args.args[0]
    assert query.message.edit_text.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_take_out_button_is_routed_by_the_bot():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._router.on_callback = AsyncMock()
    query = _tap()
    query.data = _cb("co", wine_id=1, page=7)
    update = MagicMock()
    update.callback_query = query
    await handler._dispatch_callback(update, _context())
    handler._router.on_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_pick_records_model_and_latency():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, _sent = _text("a red for tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    payload = api.record_intent.call_args.kwargs["payload"]
    assert payload["model"] == "grok-4.20-0309-non-reasoning"
    assert isinstance(payload["latency_ms"], int)


@pytest.mark.asyncio
async def test_quick_pick_records_model_as_quick():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    chat.recommend_fail = ChatFailure("unavailable", "down")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, _sent = _text("a red for tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    payload = api.record_intent.call_args.kwargs["payload"]
    assert payload["model"] == "quick"
    assert payload["fallback"] is True


@pytest.mark.asyncio
async def test_answer_turn_patches_model_onto_the_existing_ask():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "answer", "answer_index": 0, "taste": {"colour": "any", "words": []}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    handler._asks["ask:4242"] = {
        "stage": "question", "sentence": "something nice", "taste": {"colour": "any", "words": []},
        "question": "Red or white?", "options": ["Red", "White"], "intent_id": 5,
    }
    update, _sent = _text("red please")
    await handler._router.handle_text(update, _context(), update.message.text)
    api.record_intent.assert_not_called()
    changes = api.update_intent.call_args.args[1]
    assert changes["model"] == "grok-4.20-0309-non-reasoning"
    assert changes["shown"] == 1
    assert set(changes) <= {"outcome", "shown", "rejected", "place", "model", "latency_ms", "fallback"}


@pytest.mark.asyncio
async def test_not_now_patches_abandoned():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "cx"}, _context())
    assert api.update_intent.call_args.args[1]["outcome"] == "abandoned"
    assert "No problem" in query.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_question_then_option_tap_recommends_without_question():
    api = _api([_wine(1), _wine(2, name="White", wine_type="WH")])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_result = {
        "question": "Red or white?", "options": ["Red", "White"],
        "pick": {"id": 0, "reason": "", "evidence": "none"},
        "runner_up": {"id": 0, "reason": "", "evidence": "none"},
        "alternatives": [], "no_evidence_for": [],
    }
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.recommend_calls[0]["allow_question"] is True
    assert "Red or white?" in sent.edit_text.await_args.args[0]
    chat.recommend_result = {
        "question": "", "options": [],
        "pick": {"id": 1, "reason": "The red.", "evidence": "grape"},
        "runner_up": {"id": 0, "reason": "", "evidence": "none"},
        "alternatives": [], "no_evidence_for": [],
    }
    query = AsyncMock()
    query.message = sent
    query.message.chat.id = 4242
    query.message.reply_text = AsyncMock(return_value=sent)
    await handler._router.on_callback(query, {"action": "cq", "page": 0}, _context())
    assert chat.recommend_calls[-1]["allow_question"] is False
    assert chat.recommend_calls[-1]["answer"] == "Red"


@pytest.mark.asyncio
async def test_typed_answer_to_question_uses_answer_index():
    api = _api([_wine()])
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    handler._asks["ask:4242"] = {
        "stage": "question", "sentence": "what should I drink",
        "taste": {"colour": "any", "words": []},
        "question": "Red or white?", "options": ["Red", "White"], "intent_id": 5,
    }
    chat.parse_result = {"intent": "answer", "answer_index": 1, "taste": {}}
    update, _sent = _text("the second one")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.recommend_calls[-1]["answer"] == "White"
    assert chat.recommend_calls[-1]["allow_question"] is False


@pytest.mark.asyncio
async def test_recommend_timeout_still_lands_a_bottle():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_fail = ChatFailure("timeout", "slow")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    final = sent.edit_text.await_args.args[0]
    assert "Kanonkop" in final
    assert "Try properly" in _labels(sent.edit_text.await_args.kwargs["reply_markup"])
    assert len(chat.recommend_calls) == 1


@pytest.mark.asyncio
async def test_bad_output_retries_once_then_quick_pick():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_fail = ChatFailure("bad_output", "junk")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert len(chat.recommend_calls) == 2
    assert "Kanonkop" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_recommend_unavailable_gives_quick_pick_with_try_properly():
    api = _api([_wine(comment="leather and spice")])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["leather"]}}
    chat.recommend_fail = ChatFailure("unavailable", "down")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("something leathery tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    final = sent.edit_text.await_args.args[0]
    assert "sommelier brain is offline" in final
    assert "Kanonkop" in final
    assert "Try properly" in _labels(sent.edit_text.await_args.kwargs["reply_markup"])
    assert api.record_intent.call_args.kwargs["payload"]["fallback"] is True


@pytest.mark.asyncio
async def test_quick_pick_nothing_fits_says_chat_resting():
    api = _api([_wine(wine_type="WH", grapes=["Chenin Blanc"])])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    chat.recommend_fail = ChatFailure("unavailable", "down")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("a really deep red please")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "having a rest" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_auth_failure_alerts_staff_once_and_user_sees_no_key_wording():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_fail = ChatFailure("auth", "401")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    handler._router._now = lambda: 0
    context = _context()
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, context, update.message.text)
    await handler._router.handle_text(update, context, update.message.text)
    assert context.bot.send_message.await_count == 1
    staff = context.bot.send_message.await_args.args[1]
    assert "401" in staff
    user = sent.edit_text.await_args.args[0]
    assert "key" not in user.lower()
    assert "credit" not in user.lower()


@pytest.mark.asyncio
async def test_parse_down_short_phrase_still_searches():
    api = _api([_wine(name="Plain Red")])
    chat = FakeChat()
    chat.fail = ChatFailure("unavailable", "down")
    handler = _handler(api, chat)
    update, _sent = _text("Meerlust")
    assert await handler._router.handle_text(update, _context(), "Meerlust") is False


@pytest.mark.asyncio
async def test_parse_down_sentence_says_chat_resting_and_does_not_search():
    api = _api([_wine()])
    chat = FakeChat()
    chat.fail = ChatFailure("unavailable", "down")
    handler = _handler(api, chat)
    handler._do_search = AsyncMock()
    update, _sent = _text("what should I drink tonight?")
    await handler._unknown_text(update, _context())
    handler._do_search.assert_not_called()
    assert "having a rest" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_cellar_down_says_cant_reach_cellar_with_try_again_not_search():
    api = _api([_wine()])
    api.wines_in_stock.side_effect = requests.ConnectionError("nope")
    chat = FakeChat()
    handler = _handler(api, chat)
    handler._do_search = AsyncMock()
    update, _sent = _text("what is in D3?")
    await handler._unknown_text(update, _context())
    handler._do_search.assert_not_called()
    assert "can't reach your cellar" in update.message.reply_text.await_args.args[0]
    assert "Try again" in _labels(update.message.reply_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_taste_or_history_failure_still_recommends():
    api = _api([_wine()])
    api.get_taste.side_effect = requests.ConnectionError("nope")
    api.stock_history.side_effect = requests.ConnectionError("nope")
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["fennel"]}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("a fennel red please")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert chat.recommend_calls
    assert "Kanonkop" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_price_on_named_wine_patches_purchase_price():
    api = _api([_wine()])
    api.search_wines.return_value = [_wine()]
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "Kanonkop",
        "price": {"amount": 18, "currency": "GBP"},
    }
    handler = _handler(api, chat)
    handler._chat_id_to_currency["4242"] = "USD"
    update, _sent = _text("£18 on the Chenin")
    await handler._router.handle_text(update, _context(), update.message.text)
    payload = api.update_wine.call_args.args[1]
    assert payload == {"price": 18}
    assert "market_price" not in payload
    body = update.message.reply_text.await_args.args[0]
    assert "£18.00" in body
    assert "stored as USD" in body


@pytest.mark.asyncio
async def test_bare_price_with_open_ask_patches_shown_bottle():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "this",
        "price": {"amount": 12.5, "currency": ""},
    }
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    update, _sent = _text("it was 12.50")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert api.update_wine.call_args.args == (1, {"price": 12.5})
    api.search_wines.assert_not_called()


@pytest.mark.asyncio
async def test_named_price_with_open_ask_patches_the_named_wine():
    shown = _wine(1, name="Kanonkop Shiraz")
    named = _wine(2, name="Reyneke Chenin")
    api = _api([shown, named])
    api.search_wines.return_value = [named]
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "Chenin",
        "price": {"amount": 18, "currency": ""},
    }
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    update, _sent = _text("18 on the Chenin")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert api.update_wine.call_args.args == (2, {"price": 18})


@pytest.mark.asyncio
async def test_placeholder_does_not_count_bottles():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": []}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, _sent = _text("what red should I open")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert update.message.reply_text.await_args_list[0].args[0] == "🍷 Looking through your cellar…"


@pytest.mark.asyncio
async def test_price_with_open_unsaved_card_sets_grok_merged():
    api = _api([])
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "this",
        "price": {"amount": 9, "currency": "GBP"},
    }
    handler = _handler(api, chat)
    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Glen"}, "time": time.time(),
    }
    update, _sent = _text("it was 9")
    await handler._router.handle_text(update, _context(), update.message.text)
    api.update_wine.assert_not_called()
    assert handler.pending_store["_label_1"]["grok_merged"]["price"] == 9


@pytest.mark.asyncio
async def test_two_matches_ask_which_and_tap_writes():
    wines = [_wine(1, name="One"), _wine(2, name="Two")]
    api = _api(wines)
    api.search_wines.return_value = wines
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "pinot",
        "price": {"amount": 10, "currency": ""},
    }
    handler = _handler(api, chat)
    update, _sent = _text("£10 on the pinot")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "Which bottle?" in update.message.reply_text.await_args.args[0]
    api.update_wine.assert_not_called()
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    query.message.reply_text = AsyncMock()
    await handler._router.on_callback(query, {"action": "cw", "wine_id": 2}, _context())
    assert api.update_wine.call_args.args[0] == 2


@pytest.mark.asyncio
async def test_no_match_says_not_found():
    api = _api([_wine()])
    api.search_wines.return_value = []
    chat = FakeChat()
    chat.parse_result = {
        "intent": "correct", "wine_ref": "Aux Moines",
        "correction": {"name": "Aux Moines", "producer": "", "vintage": None, "grape": "", "country_code": ""},
    }
    handler = _handler(api, chat)
    update, _sent = _text("wrong wine, it's Aux Moines")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "couldn't find" in update.message.reply_text.await_args.args[0]
    api.update_wine.assert_not_called()


@pytest.mark.asyncio
async def test_correction_on_open_card_updates_pending_data_not_api():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {
        "intent": "correct", "wine_ref": "this",
        "correction": {"name": "Aux Moines", "producer": "Aux", "vintage": 2019, "grape": "", "country_code": ""},
    }
    handler = _handler(api, chat)
    handler.pending_store["_label_1"] = {
        "grok_merged": {"name": "Wrong"}, "time": time.time(),
    }
    update, _sent = _text("wrong wine, it's Aux Moines")
    await handler._router.handle_text(update, _context(), update.message.text)
    api.update_wine.assert_not_called()
    merged = handler.pending_store["_label_1"]["grok_merged"]
    assert merged["name"] == "Aux Moines"
    assert merged["producer"] == "Aux"
    assert merged["vintage"] == 2019


@pytest.mark.asyncio
async def test_correction_on_saved_wine_patches_only_given_fields():
    api = _api([_wine()])
    api.search_wines.return_value = [_wine()]
    chat = FakeChat()
    chat.parse_result = {
        "intent": "correct", "wine_ref": "Kanonkop",
        "correction": {"name": "", "producer": "Aux Moines", "vintage": 2019, "grape": "", "country_code": ""},
    }
    handler = _handler(api, chat)
    update, _sent = _text("vintage is 2019, producer Aux Moines")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert api.update_wine.call_args.args[1] == {"producer": "Aux Moines", "vintage": 2019}


@pytest.mark.asyncio
async def test_correction_never_targets_recommended_bottle():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {
        "intent": "correct", "wine_ref": "this",
        "correction": {"name": "Aux Moines", "producer": "", "vintage": None, "grape": "", "country_code": ""},
    }
    handler = _handler(api, chat)
    handler._asks["ask:4242"] = {"stage": "shown", "shown": 1, "intent_id": 5}
    update, _sent = _text("wrong wine, it's Aux Moines")
    await handler._router.handle_text(update, _context(), update.message.text)
    api.update_wine.assert_not_called()
    assert "Which bottle" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_price_write_5xx_keeps_pending_write_and_retry_saves():
    api = _api([_wine()])
    response = MagicMock(status_code=500)
    response.json.return_value = {}
    error = requests.HTTPError("500")
    error.response = response
    api.update_wine.side_effect = error
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "Kanonkop",
        "price": {"amount": 18, "currency": ""},
    }
    api.search_wines.return_value = [_wine()]
    handler = _handler(api, chat)
    update, _sent = _text("18 on the kanonkop")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "Couldn't save" in update.message.reply_text.await_args.args[0]
    assert handler._asks["ask:4242"]["write"]["payload"] == {"price": 18}
    api.update_wine.side_effect = None
    api.update_wine.return_value = {"id": 1}
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    query.message.reply_text = AsyncMock()
    await handler._router.on_callback(query, {"action": "cr"}, _context())
    assert api.update_wine.call_args.args[1] == {"price": 18}


@pytest.mark.asyncio
async def test_price_write_404_says_wine_gone():
    api = _api([_wine()])
    response = MagicMock(status_code=404)
    response.json.return_value = {}
    error = requests.HTTPError("404")
    error.response = response
    api.update_wine.side_effect = error
    api.search_wines.return_value = [_wine()]
    chat = FakeChat()
    chat.parse_result = {
        "intent": "price", "wine_ref": "Kanonkop",
        "price": {"amount": 18, "currency": ""},
    }
    handler = _handler(api, chat)
    update, _sent = _text("18 please on this one now")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "isn't in your cellar" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_record_intent_failure_is_silent():
    api = _api([_wine()])
    api.record_intent.side_effect = RuntimeError("db down")
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["fennel"]}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("a fennel red please")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "Kanonkop" in sent.edit_text.await_args.args[0]
    assert "Error" not in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_store_error_still_shows_pick_without_something_else():
    class BoomStore:
        def get(self, key, default=None):
            return None
        def __setitem__(self, key, value):
            raise StoreError("down")
        def pop(self, key, default=None):
            return default

    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    handler = _handler(api, chat, asks=BoomStore())
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    final = sent.edit_text.await_args.args[0]
    assert "Kanonkop" in final
    assert "can't remember follow-ups" in final
    assert "Something else" not in _labels(sent.edit_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_store_error_on_chat_callback_says_ask_lost():
    class BoomStore:
        def get(self, key, default=None):
            raise StoreError("down")
        def __setitem__(self, key, value):
            raise StoreError("down")
        def pop(self, key, default=None):
            raise StoreError("down")

    api = _api([_wine()])
    handler = _handler(api, FakeChat(), asks=BoomStore())
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "cn"}, _context())
    assert "lost track" in query.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_placeholder_edit_failure_sends_new_message():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    sent.edit_text.side_effect = BadRequest("message to edit not found")
    await handler._router.handle_text(update, _context(), update.message.text)
    sent_again = [call.args[0] for call in sent.reply_text.await_args_list]
    assert any("Kanonkop" in body for body in sent_again)


@pytest.mark.asyncio
async def test_unexpected_exception_never_escapes_and_placeholder_is_cleared():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_fail = RuntimeError("boom")
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    handled = await handler._router.handle_text(update, _context(), update.message.text)
    assert handled is True
    assert "having a rest" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_status_shows_chat_resting_when_breaker_open():
    api = _api([_wine()])
    chat = FakeChat()
    chat._healthy = False
    handler = _handler(api, chat)
    update, _sent = _text("/status")
    await handler._cmd_status(update, _context())
    assert "Chat: resting" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_no_evidence_line_shown():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["leather"]}}
    chat.recommend_result["no_evidence_for"] = ["leather"]
    chat.recommend_result["pick"]["evidence"] = "none"
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, sent = _text("something leathery tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert "None of your notes mention leather" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_ask_recorded_with_payload_and_intent_id_saved():
    api = _api([_wine(comment="fennel", price=20)])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "red", "words": ["fennel"]}}
    handler = _handler(api, chat)
    handler._router._sleep = _fast
    update, _sent = _text("a fennel red please")
    await handler._router.handle_text(update, _context(), update.message.text)
    payload = api.record_intent.call_args.kwargs["payload"]
    assert payload["sentence"] == "a fennel red please"
    assert payload["offer"] is None
    assert payload["pick"]["id"] == 1
    assert payload["price_band"]["n"] == 1
    assert handler._asks["ask:4242"]["intent_id"] == 5


@pytest.mark.asyncio
async def test_ask_survives_handler_restart():
    store = CardStore(ttl=1800, prefix="whiney:ask:")
    api = _api([_wine(1), _wine(2, name="Beta")])
    chat = FakeChat()
    chat.parse_result = {"intent": "recommend", "taste": {"colour": "any", "words": []}}
    chat.recommend_result["alternatives"] = [
        {"id": 2, "reason": "Next.", "evidence": "grape"},
    ]
    handler = _handler(api, chat, asks=store)
    handler._router._sleep = _fast
    update, sent = _text("what should I drink tonight")
    await handler._router.handle_text(update, _context(), update.message.text)
    restarted = _handler(api, FakeChat(), asks=store)
    query = AsyncMock()
    query.message = sent
    query.message.chat.id = 4242
    await restarted._router.on_callback(query, {"action": "cn"}, _context())
    assert "Beta" in sent.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_button_after_ttl_says_finished():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    query = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.chat.id = 4242
    await handler._router.on_callback(query, {"action": "cn"}, _context())
    assert "finished" in query.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_location_during_open_ask_patches_place_gps():
    from datetime import datetime, timezone
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"intent_id": 5, "stage": "shown"}
    update, _sent = _text("ignored")
    update.message.location = MagicMock(
        latitude=51.5, longitude=-0.12, horizontal_accuracy=12, live_period=None,
    )
    update.message.date = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert await handler._router.on_location(update) is True
    place = api.update_intent.call_args.args[1]["place"]
    assert place["fix"] == "gps"
    assert place["accuracy_m"] == 12
    assert place["purpose"] == "weather"
    assert "Thanks" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_location_without_accuracy_is_map_pin():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"intent_id": 5, "stage": "shown"}
    update, _sent = _text("ignored")
    update.message.location = MagicMock(
        latitude=1, longitude=2, horizontal_accuracy=None, live_period=None,
    )
    update.message.date = None
    await handler._router.on_location(update)
    assert api.update_intent.call_args.args[1]["place"]["fix"] == "map_pin"


@pytest.mark.asyncio
async def test_live_location_is_live_start():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"intent_id": 5, "stage": "shown"}
    update, _sent = _text("ignored")
    update.message.location = MagicMock(
        latitude=1, longitude=2, horizontal_accuracy=5, live_period=60,
    )
    update.message.date = None
    await handler._router.on_location(update)
    assert api.update_intent.call_args.args[1]["place"]["fix"] == "live_start"


@pytest.mark.asyncio
async def test_location_with_no_ask_writes_nothing():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    update, _sent = _text("ignored")
    update.message.location = MagicMock(latitude=1, longitude=2, horizontal_accuracy=5, live_period=None)
    update.message.date = None
    assert await handler._router.on_location(update) is False
    api.update_intent.assert_not_called()


@pytest.mark.asyncio
async def test_invite_flow_location_still_handled_first():
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._asks["ask:4242"] = {"intent_id": 5, "stage": "shown"}
    handler._invite_flow().on_location = AsyncMock(return_value=True)
    update, _sent = _text("ignored")
    update.message.location = MagicMock(latitude=1, longitude=2, horizontal_accuracy=5, live_period=None)
    await handler._handle_location(update, _context())
    api.update_intent.assert_not_called()


@pytest.mark.asyncio
async def test_no_user_facing_text_contains_exception_repr():
    banned = ("Error", "Exception", "Traceback", "http")
    api = _api([_wine()])
    api.wines_in_stock.side_effect = requests.ConnectionError("http://cellar down")
    chat = FakeChat()
    chat.fail = ChatFailure("timeout", "https://api.x.ai exploded")
    handler = _handler(api, chat)
    update, _sent = _text("what should I drink tonight?")
    # Cellar is checked first, so this path is the cellar message.
    await handler._router.handle_text(update, _context(), update.message.text)
    body = update.message.reply_text.await_args.args[0]
    for word in banned:
        assert word not in body
