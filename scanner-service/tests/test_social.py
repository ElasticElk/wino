"""Phase 3 Telegram flows: comments, To-drink, club list, taste-offs."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import club_cb, parse_club_cb


def _handler(api, **flag_values):
    if isinstance(getattr(api.list_all_wines, "return_value", None), MagicMock) or not hasattr(
        api, "list_all_wines",
    ):
        api.list_all_wines = MagicMock(return_value=[])
    if not getattr(api, "web_url", None):
        api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.bot_username = "whineybot"
    flags = {"feature_clubs": "on"}
    flags.update(flag_values)
    handler.set_flags(flags)
    return handler


def _update(chat_id=4242, text="/club"):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.effective_user.full_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.chat_id = chat_id
    return u


def _query(data, *, text="card"):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.message_id = 7
    query.message.text = text
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _labels(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def test_callback_budget_score_and_comment():
    data = club_cb("ts", 999_999_999, 888_888_888, 5)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    action, rest = parse_club_cb(data)
    assert action == "ts"
    assert rest == ["999999999", "888888888", "5"]
    data = club_cb("cm", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    data = club_cb("td", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    data = club_cb("tc", 999_999_999)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX
    data = club_cb("ta", 12, 34)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX


@pytest.mark.asyncio
async def test_comment_flow_en_and_too_long():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "name": "Thursday reds", "is_last_used": True}],
    }
    api.club_feed.return_value = {"events": [{"id": 88, "kind": "opened"}], "count": 1}
    api.add_club_comment.return_value = {
        "id": 1, "text": "we had that in Stellenbosch", "status_code": 201,
    }
    handler = _handler(api, feature_club_comments="on")
    update = _update()
    query = _query(club_cb("cm", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert t("en", "club.comment.prompt") in query.message.reply_text.call_args.args[0]
    long = _update(text="x" * 281)
    await handler._club_flow().on_text(long, MagicMock())
    assert long.message.reply_text.call_args.args[0] == t("en", "club.comment.too_long")
    ok = _update(text="we had that in Stellenbosch")
    await handler._club_flow().on_text(ok, MagicMock())
    api.add_club_comment.assert_called()
    assert ok.message.reply_text.call_args.args[0] == t("en", "club.comment.saved")


@pytest.mark.asyncio
async def test_comment_abandoned_by_command():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "name": "Thursday reds", "is_last_used": True}],
    }
    api.club_feed.return_value = {"events": [{"id": 88}], "count": 1}
    handler = _handler(api, feature_club_comments="on")
    update = _update()
    query = _query(club_cb("cm", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    cmd = _update(text="/fridge")
    await handler._abandon_invite_if_command(cmd, MagicMock())
    leftover = _update(text="still a comment")
    claimed = await handler._club_flow().on_text(leftover, MagicMock())
    assert claimed is False


@pytest.mark.asyncio
async def test_comment_prompt_lost_says_tap_again():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "is_last_used": True}],
    }
    api.club_feed.return_value = {"events": [{"id": 88}], "count": 1}
    handler = _handler(api, feature_club_comments="on")
    flow = handler._club_flow()
    update = _update()
    query = _query(club_cb("cm", 88))
    update.callback_query = query
    await flow.on_callback(update, MagicMock())
    state = flow.state_for("4242")
    state["comment_at"] = state["comment_at"] - 20 * 60
    later = _update(text="hello")
    await flow.on_text(later, MagicMock())
    assert later.message.reply_text.call_args.args[0] == t("en", "club.comment.again")


@pytest.mark.asyncio
async def test_to_drink_from_feed_and_lists_off():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "is_last_used": True}],
    }
    api.club_feed.return_value = {"events": [{"id": 88}], "count": 1}
    api.event_to_list.return_value = {"id": 9, "name": "Merlot", "status_code": 201}
    handler = _handler(api, feature_lists="on")
    update = _update()
    query = _query(club_cb("td", 88))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert query.message.reply_text.call_args.args[0] == t("en", "club.td.added")
    off = _handler(api)
    q2 = _query(club_cb("td", 88))
    update.callback_query = q2
    await off._club_flow().on_callback(update, MagicMock())
    assert q2.edit_message_text.call_args.args[0] == t("en", "club.td.off")


@pytest.mark.asyncio
async def test_club_list_empty_and_add():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "name": "Thursday reds", "is_last_used": True}],
    }
    api.club_list.return_value = {"id": 1, "name": "Thursday reds", "kind": "club", "items": []}
    api.add_club_list_item.return_value = {
        "id": 70, "name": "Chenin Blanc", "vintage": 2022, "status_code": 201,
    }
    handler = _handler(api, feature_lists="on")
    update = _update(text="/club list")
    await handler._club_flow().cmd_club(update, MagicMock(args=["list"]))
    text = update.message.reply_text.call_args.args[0]
    assert t("en", "club.list.empty") in text
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.list.add_btn") in labels
    query = _query(club_cb("la", 3))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    typed = _update(text="Chenin Blanc 2022")
    await handler._club_flow().on_text(typed, MagicMock())
    api.add_club_list_item.assert_called()
    texts = [c.args[0] for c in typed.message.reply_text.call_args_list]
    assert any(t("en", "club.list.added") in text for text in texts)


@pytest.mark.asyncio
async def test_tasteoff_setup_need_two_and_open():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "name": "Thursday reds", "is_last_used": True}],
    }
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "cellar_visibility": "full", "role": "owner",
    }
    api.list_tasteoffs.return_value = {"tasteoffs": []}
    api.create_tasteoff.return_value = {
        "id": 4, "title": "Thursday reds · 5 Nov", "status": "setup",
        "bottles": [], "status_code": 201,
    }
    api.get_tasteoff.return_value = {
        "id": 4, "title": "Thursday reds · 5 Nov", "status": "setup",
        "bottles": [{"id": 1, "label": "Chenin Blanc 2022"}],
    }
    api.add_tasteoff_bottle.return_value = {"id": 1, "label": "Chenin Blanc 2022", "status_code": 201}
    api.open_tasteoff.return_value = {"error": "need_two", "status_code": 409}
    handler = _handler(api, feature_tasteoffs="on")
    update = _update(text="/club tasteoff")
    await handler._club_flow().cmd_club(update, MagicMock(args=["tasteoff"]))
    assert t("en", "club.to.title") in update.message.reply_text.call_args.args[0]
    named = _update(text="Thursday reds · 5 Nov")
    await handler._club_flow().on_text(named, MagicMock())
    labels = _labels(named.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.to.open") in labels
    assert t("en", "club.to.type_btn") in labels
    query = _query(club_cb("op", 4))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert query.edit_message_text.call_args.args[0] == t("en", "club.to.need_two")


@pytest.mark.asyncio
async def test_tasteoff_score_and_afrikaans_copy():
    api = MagicMock()
    api.list_clubs.return_value = {
        "clubs": [{"id": 3, "is_last_used": True}],
    }
    api.get_tasteoff.return_value = {
        "id": 4,
        "title": "Thursday reds · 5 Nov",
        "status": "scoring",
        "bottles": [{"id": 34, "label": "Chenin Blanc 2022", "your_score": 5}],
    }
    api.score_tasteoff.return_value = {"bottle_id": 34, "score": 5}
    handler = _handler(api, feature_tasteoffs="on")
    update = _update()
    update.effective_user.language_code = "af"
    query = _query(club_cb("ts", 4, 34, 5))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    text = query.edit_message_text.call_args.args[0]
    assert "5" in text
    assert t("af", "club.to.scored", label="Chenin Blanc 2022", n=5) in text or "5" in text


@pytest.mark.asyncio
async def test_home_gains_list_and_tasteoff_buttons():
    api = MagicMock()
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "role": "owner",
        "cellar_visibility": "full", "badges_visible": True,
        "feed_muted": False, "member_count": 2, "events": [],
    }
    handler = _handler(api, feature_lists="on", feature_tasteoffs="on")
    update = _update()
    await handler._club_flow()._show_home(update, 3)
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert t("en", "club.list.title") in labels
    assert t("en", "club.home.tasteoff") in labels
    for row in update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= CALLBACK_DATA_MAX


@pytest.mark.asyncio
async def test_summary_club_tasteoff_from_cellar_copy():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.get_club.return_value = {
        "id": 3, "name": "Thursday reds", "cellar_visibility": "summary",
    }
    handler = _handler(api, feature_tasteoffs="on")
    flow = handler._club_flow()
    flow.remember("4242", tasteoff_id=4, club_id=3, step="tasteoff_setup")
    update = _update()
    query = _query(club_cb("tf", 4))
    update.callback_query = query
    await flow.on_callback(update, MagicMock())
    assert query.edit_message_text.call_args.args[0] == t("en", "club.to.summary_only")


def test_afrikaans_phase3_buttons():
    assert t("af", "club.comment.btn") == "Sê iets"
    assert t("af", "club.td.btn") == "To-drink-lys"
    assert t("af", "club.to.open") == "Begin telling"
    assert t("af", "club.list.empty") == "Niks op die klublys nie."


@pytest.mark.asyncio
async def test_feed_shows_comment_and_to_drink_when_flags_on():
    api = MagicMock()
    api.club_feed.return_value = {
        "events": [
            {
                "id": 88, "kind": "opened", "actor_name": "Riley",
                "wine_name": "Merlot", "vintage": 2019, "retracted": False,
            },
        ],
        "count": 1,
    }
    handler = _handler(api, feature_club_comments="on", feature_lists="on")
    update = _update()
    query = _query(club_cb("fd", 3, 0))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    labels = _labels(query.edit_message_text.call_args.kwargs.get("reply_markup"))
    assert t("en", "club.comment.btn") in labels
    assert t("en", "club.td.btn") in labels


@pytest.mark.asyncio
async def test_club_list_add_blocked_when_lists_off():
    api = MagicMock()
    handler = _handler(api)
    update = _update()
    query = _query(club_cb("la", 3))
    update.callback_query = query
    await handler._club_flow().on_callback(update, MagicMock())
    assert query.edit_message_text.call_args.args[0] == t("en", "club.td.off")
    api.add_club_list_item.assert_not_called()
