"""Typed club questions through ChatRouter: flags, pickers, EN/AF copy."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.chat_models import parse_prompt
from integrations.club_chat import club_pick_buttons, render_club
from integrations.copy import t
from integrations.telegram_bot import TelegramBotHandler, _cb, _parse_cb

from test_chat_router import FakeChat, _api, _context, _handler, _labels, _text, _wine


def _club_parse(**club):
    payload = {
        "kind": "members",
        "member_name": "",
        "club_name": "",
        "wine_text": "",
        "grape": "",
        "vintage": None,
        "period": "",
        "when": "",
    }
    payload.update(club)
    return {"intent": "club", "club": payload}


def _club_handler(api, chat, **flags):
    handler = _handler(api, chat)
    config = {"feature_clubs": "on"}
    config.update(flags)
    handler.set_flags(config)
    return handler


@pytest.mark.asyncio
async def test_club_intent_is_honest_when_flag_off():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="members")
    chat.wine_chat_reply = "You only drink wine."
    handler = _handler(api, chat)
    update, sent = _text("who's in my club?")
    assert await handler._router.handle_text(update, _context(), "who's in my club?") is True
    api.club_ask.assert_not_called()
    assert chat.wine_chat_calls == []
    text = update.message.reply_text.await_args.args[0]
    assert "can't help with that yet" in text.lower()
    assert "own cellar" in text.lower()
    assert "You only drink wine" not in text
    assert "club" not in text.lower()


@pytest.mark.asyncio
async def test_members_question_english_and_afrikaans():
    api = _api([_wine()])
    api.club_ask.return_value = {
        "status": "ok",
        "kind": "members",
        "club": {"id": 1, "name": "Thursday reds", "kind": "private"},
        "members": [
            {"display_name": "Riley", "role": "owner"},
            {"display_name": "Sam", "role": "member"},
        ],
    }
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="members")
    handler = _club_handler(api, chat)
    update, _sent = _text("who's in my club?")
    assert await handler._router.handle_text(update, _context(), "who's in my club?") is True
    text = update.message.reply_text.await_args.args[0]
    assert "Thursday reds" in text
    assert "Riley" in text
    assert "Sam" in text
    api.club_ask.assert_called_once()
    handler.set_language("4242", "af")
    update, _sent = _text("wie is in my klub?")
    chat.parse_result = _club_parse(kind="members")
    await handler._router.handle_text(update, _context(), "wie is in my klub?")
    af = update.message.reply_text.await_args.args[0]
    assert "In Thursday reds" in af
    assert "(eienaar)" in af


@pytest.mark.asyncio
async def test_tasteoff_flag_off_never_mentions_the_feature():
    api = _api([_wine()])
    api.club_ask.return_value = {"status": "fallback"}
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="tasteoff", when="next")
    chat.wine_chat_reply = "I would look at a tasting flight if you have one."
    handler = _club_handler(api, chat)
    update, _sent = _text("when is the next taste-off?")
    await handler._router.handle_text(update, _context(), "when is the next taste-off?")
    text = update.message.reply_text.await_args.args[0]
    assert "aren't on" not in text
    assert "nog nie aan" not in text
    assert "taste-off" not in text.lower()
    assert "can't help with that yet" in text.lower()
    assert chat.wine_chat_calls == []
    api.club_ask.assert_not_called()


@pytest.mark.asyncio
async def test_private_list_and_picker_buttons_stay_english():
    api = _api([_wine()])
    api.club_ask.return_value = {
        "status": "private",
        "kind": "list",
        "club": {"id": 4, "name": "Thursday reds"},
        "member": "Riley",
        "reason": "personal_list",
    }
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="list", member_name="Riley")
    handler = _club_handler(api, chat, feature_lists="on")
    handler.set_language("4242", "af")
    update, _sent = _text("wat is op Riley se lys?")
    await handler._router.handle_text(update, _context(), "wat is op Riley se lys?")
    kwargs = update.message.reply_text.await_args
    text = kwargs.args[0]
    assert "privaat" in text
    markup = kwargs.kwargs.get("reply_markup")
    assert _labels(markup) == ["Club list"]


@pytest.mark.asyncio
async def test_ambiguous_member_shows_picker_then_resolves():
    api = _api([_wine()])
    api.club_ask.return_value = {
        "status": "need_member",
        "kind": "cellar",
        "club": {"id": 1, "name": "Thursday reds"},
        "choices": [
            {"id": 2, "label": "Sam", "kind": "member", "club_id": 1},
            {"id": 3, "label": "Samantha", "kind": "member", "club_id": 1},
        ],
    }
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="cellar", member_name="Sam", grape="Merlot")
    handler = _club_handler(api, chat)
    update, sent = _text("how many Merlots does Sam have?")
    await handler._router.handle_text(update, _context(), "how many Merlots does Sam have?")
    kwargs = update.message.reply_text.await_args
    assert "Who in Thursday reds?" in kwargs.args[0]
    assert _labels(kwargs.kwargs.get("reply_markup")) == ["Sam", "Samantha"]
    api.club_ask.return_value = {
        "status": "ok",
        "kind": "cellar",
        "club": {"id": 1, "name": "Thursday reds"},
        "member": "Sam",
        "visibility": "full",
        "count": 2,
        "wines": [{"name": "Hillside Merlot", "vintage": 2018, "producer": "", "stock_count": 2}],
    }
    query = MagicMock()
    query.message = sent
    sent.chat_id = 4242
    sent.chat = MagicMock(id=4242)
    data = _parse_cb(_cb("cm", wine_id=2, page=1))
    await handler._router.on_callback(query, data, _context())
    body = api.club_ask.call_args.args[0]
    assert body["member_id"] == 2
    assert body["club_id"] == 1


@pytest.mark.asyncio
async def test_club_logging_does_not_add_extra_message_text():
    api = _api([_wine()])
    api.club_ask.return_value = {
        "status": "ok", "kind": "my_clubs",
        "clubs": [{"id": 1, "name": "Thursday reds", "is_last_used": True}],
    }
    chat = FakeChat()
    chat.parse_result = _club_parse(kind="my_clubs")
    handler = _club_handler(api, chat)
    update, _sent = _text("what clubs am I in?")
    await handler._router.handle_text(update, _context(), "what clubs am I in?")
    api.club_ask.assert_called_once()


def test_parse_prompt_always_classifies_club_questions():
    off = parse_prompt({})
    assert "club_query" in off
    assert "tasteoff" in off
    on = parse_prompt({"clubs": True, "tasteoffs": True, "lists": True, "comments": True})
    assert "club_query" in on
    assert "list_action" in on
    assert off == on


def test_render_never_dead_ends_without_buttons():
    payload = {
        "status": "not_found",
        "kind": "cellar",
        "target": "member",
        "query": "Pat",
        "club": {"id": 1, "name": "Thursday reds"},
        "choices": [
            {"id": 2, "label": "Riley", "kind": "member", "club_id": 1},
            {"id": 3, "label": "Sam", "kind": "member", "club_id": 1},
        ],
    }
    text = render_club(payload, "en")
    assert "Pat" in text
    labels = [b[0] for b in club_pick_buttons(payload)]
    assert "Riley" in labels
    assert "List members" in labels
    af = render_club(payload, "af")
    assert "sien niemand" in af


def test_chat_club_copy_keys_match():
    required = {
        "chat.club.no_clubs",
        "chat.club.pick_club",
        "chat.club.private_list",
        "chat.club.global_cellar",
        "chat.club.who_has_none",
        "chat.club.tasteoff_next",
        "chat.club.comments",
    }
    for key in required:
        assert t("en", key)
        assert t("af", key)
        assert t("en", key) != t("af", key)
    assert t("en", "chat.club.pick_club") == "Which club?"
    assert t("af", "chat.club.pick_club") == "Watter klub?"
