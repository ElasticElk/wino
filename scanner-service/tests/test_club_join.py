"""Joining a club through an invite, end to end through the bot.

The member client (``handler.api``) and the staff client (``handler.admin_api``)
are separate mocks here on purpose: redeem is staff-only, and sharing one mock
is how the 403 on Join went unnoticed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.copy import t
from integrations.telegram_bot import TelegramBotHandler
from integrations.telegram_clubs import club_cb
from integrations.telegram_nav import ALREADY_DONE

CODE = "K9m2Pq4r"
CLUB = {"id": 3, "name": "Thursday reds"}
FORBIDDEN = {
    "detail": "You do not have permission to perform this action.",
    "error": "You do not have permission to perform this action.",
    "status_code": 403,
}


def _preview(**over):
    body = {
        "kind": "club",
        "code": CODE,
        "club": dict(CLUB),
        "cellar_visibility": "full",
        "badges_visible": True,
        "inviter_display_name": "Riley",
        "expires_at": "2026-12-13T00:00:00+00:00",
        "expired": False,
        "used": False,
        "dismantle_in_progress": False,
    }
    body.update(over)
    return body


def _joined(**over):
    body = {
        "token": "tok-new",
        "household": {"id": 9, "name": "Hilltop"},
        "household_id": 9,
        "club": dict(CLUB, cellar_visibility="full", badges_visible=True),
        "role": "member",
        "first_cellar": False,
    }
    body.update(over)
    return body


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=55))
    bot.delete_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.send_chat_action = AsyncMock()
    return bot


def _handler():
    member = MagicMock(name="member_api")
    member.list_all_wines = MagicMock(return_value=[])
    member.web_url = "https://cellar.test"
    member.list_households.return_value = [{"id": 5, "name": "Hillside"}]
    member.list_clubs.return_value = {"clubs": []}
    member.get_club.return_value = dict(CLUB, members=[], member_count=2)
    member.redeem_club_invite.return_value = dict(FORBIDDEN)
    member.preview_invite.return_value = dict(FORBIDDEN)
    staff = MagicMock(name="admin_api")
    staff.preview_invite.return_value = _preview()
    staff.redeem_club_invite.return_value = _joined()
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=member,
        pending_store={}, admin_api=staff,
    )
    handler.bot_username = "whineybot"
    handler.set_flags({"feature_clubs": "on"})
    handler._invite_flow()._hot_add = AsyncMock()
    return handler, member, staff


def _message_update(chat_id, text):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Pat"
    u.effective_user.full_name = "Pat"
    u.message.text = text
    u.message.chat_id = chat_id
    u.message.reply_text = AsyncMock()
    u.callback_query = None
    return u


def _tap(chat_id, data, message_id=7):
    u = _message_update(chat_id, "")
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = chat_id
    query.message.message_id = message_id
    query.message.text = "card"
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.get_bot = MagicMock(return_value=_bot())
    u.callback_query = query
    return u


def _context():
    context = MagicMock()
    context.bot = _bot()
    context.args = [CODE]
    return context


async def _open(handler, chat_id):
    update = _message_update(chat_id, f"/start {CODE}")
    await handler._invite_flow().start(update, _context())
    return update


def _edited(update):
    return update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_join_redeems_with_staff_client():
    # Regression: Join used handler.api (a member token) and got a 403 every time.
    handler, member, staff = _handler()
    await _open(handler, 4242)
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    staff.redeem_club_invite.assert_called_once()
    member.redeem_club_invite.assert_not_called()
    assert "Thursday reds" in _edited(tap)
    assert _edited(tap) != t("en", "club.err")


@pytest.mark.asyncio
async def test_existing_user_join_end_to_end():
    handler, member, staff = _handler()
    opened = await _open(handler, 4242)
    staff.preview_invite.assert_called_once_with(CODE)
    card = opened.message.reply_text.call_args
    assert "Hillside" in card.args[0]
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    args, kwargs = staff.redeem_club_invite.call_args
    assert args[0] == CODE and args[1] == "4242"
    assert kwargs["household_id"] == 5
    assert "Thursday reds" in _edited(tap)
    # A known chat keeps its client, active cellar and currency.
    handler._invite_flow()._hot_add.assert_not_called()


@pytest.mark.asyncio
async def test_new_user_join_end_to_end():
    handler, member, staff = _handler()
    staff.redeem_club_invite.return_value = _joined(first_cellar=True)
    opened = await _open(handler, 999)
    assert "call it" in opened.message.reply_text.call_args.args[0].lower() or (
        "own cellar" in opened.message.reply_text.call_args.args[0].lower()
    )
    named = _message_update(999, "Hilltop")
    assert await handler._club_flow().on_text(named, _context())
    tap = _tap(999, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    args, kwargs = staff.redeem_club_invite.call_args
    assert args[1] == "999"
    assert kwargs["household_name"] == "Hilltop"
    handler._invite_flow()._hot_add.assert_awaited_once()
    assert handler._invite_flow()._hot_add.call_args.args[0] == "999"
    assert "Thursday reds" in _edited(tap)


@pytest.mark.asyncio
async def test_double_tap_join_redeems_once():
    handler, _member, staff = _handler()
    await _open(handler, 4242)
    first = _tap(4242, club_cb("j", CODE))
    second = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(first, _context())
    await handler._handle_callback(second, _context())
    staff.redeem_club_invite.assert_called_once()
    second.callback_query.answer.assert_awaited_once_with(ALREADY_DONE)
    second.callback_query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_late_repeat_join_is_already_member_not_expired():
    # After the cooldown the server answers already_member (200): show home.
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = _joined(status="already_member")
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert "Thursday reds" in _edited(tap)
    assert _edited(tap) != t("en", "club.fresh")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["expired", "used"])
async def test_dead_invite_link_says_fresh(flag):
    handler, _member, staff = _handler()
    staff.preview_invite.return_value = _preview(**{flag: True})
    opened = await _open(handler, 4242)
    assert opened.message.reply_text.call_args.args[0] == t("en", "club.fresh")
    staff.redeem_club_invite.assert_not_called()


@pytest.mark.asyncio
async def test_used_up_link_for_a_member_says_already_in():
    handler, member, staff = _handler()
    staff.preview_invite.return_value = _preview(used=True)
    member.list_clubs.return_value = {"clubs": [dict(CLUB)]}
    opened = await _open(handler, 4242)
    texts = [c.args[0] for c in opened.message.reply_text.call_args_list]
    assert texts[0] == t("en", "club.join.already", club="Thursday reds")
    assert t("en", "club.fresh") not in texts
    staff.redeem_club_invite.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_join_on_dead_invite_says_fresh(status):
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = {
        "error": "This invite was already used or has expired", "status_code": status,
    }
    await _open(handler, 4242)
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.fresh")


@pytest.mark.asyncio
async def test_refused_join_says_not_joined_and_logs(caplog):
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = dict(FORBIDDEN)
    await _open(handler, 4242)
    tap = _tap(4242, club_cb("j", CODE))
    with caplog.at_level("ERROR"):
        await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.join.failed")
    assert "haven't joined" in _edited(tap)
    assert any("status=403" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_join_after_lost_cellar_name_asks_to_retap():
    # Bot restarted between naming and Join: the server needs a cellar name.
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = {
        "error": "household_name is required", "status_code": 400,
    }
    tap = _tap(999, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.join.lost_place")


@pytest.mark.asyncio
async def test_join_falls_back_to_api_without_staff_client():
    handler, member, _staff = _handler()
    handler.admin_api = None
    member.redeem_club_invite.return_value = _joined()
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    member.redeem_club_invite.assert_called_once()


@pytest.mark.asyncio
async def test_known_chat_400_says_failed_not_lost_place():
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = {
        "error": "household_name is required", "status_code": 400,
    }
    tap = _tap(4242, club_cb("j", CODE, 5))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.join.failed")


@pytest.mark.asyncio
async def test_clubs_switched_off_server_side_says_off():
    handler, _member, staff = _handler()
    staff.redeem_club_invite.return_value = {"error": "not_enabled", "status_code": 404}
    tap = _tap(4242, club_cb("j", CODE, 5))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.off")


def _two_cellars(member):
    member.list_households.return_value = [
        {"id": 5, "name": "Hillside"}, {"id": 6, "name": "Riverside"},
    ]


@pytest.mark.asyncio
async def test_join_card_button_carries_the_picked_cellar():
    handler, member, _staff = _handler()
    _two_cellars(member)
    opened = await _open(handler, 4242)
    picker = opened.message.reply_text.call_args.kwargs["reply_markup"]
    pick = picker.inline_keyboard[1][0].callback_data
    assert pick == club_cb("ph", CODE, 6)
    tap = _tap(4242, pick)
    await handler._handle_callback(tap, _context())
    card = tap.callback_query.edit_message_text.call_args
    assert "Riverside" in card.args[0]
    join = card.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert join == club_cb("j", CODE, 6)


@pytest.mark.asyncio
async def test_join_after_restart_shares_the_cellar_on_the_button():
    # State gone (restart): the cellar on the button wins, not the default.
    handler, member, staff = _handler()
    _two_cellars(member)
    tap = _tap(4242, club_cb("j", CODE, 6))
    await handler._handle_callback(tap, _context())
    assert staff.redeem_club_invite.call_args.kwargs["household_id"] == 6


@pytest.mark.asyncio
async def test_join_without_a_cellar_asks_which_when_there_are_several():
    # An old button with no cellar and no state: ask, never use the default.
    handler, member, staff = _handler()
    _two_cellars(member)
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    staff.redeem_club_invite.assert_not_called()
    markup = tap.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        club_cb("ph", CODE, 5), club_cb("ph", CODE, 6),
    ]


@pytest.mark.asyncio
async def test_reask_after_restart_card_names_the_club_and_policy():
    handler, member, staff = _handler()
    _two_cellars(member)
    staff.preview_invite.return_value = _preview(cellar_visibility="none")
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert "Thursday reds" in _edited(tap)
    pick = _tap(4242, club_cb("ph", CODE, 6))
    await handler._handle_callback(pick, _context())
    card = _edited(pick)
    assert "Riley" in card and "Thursday reds" in card and "Riverside" in card
    assert t("en", "club.policy_of.none") in card
    staff.redeem_club_invite.assert_not_called()


@pytest.mark.asyncio
async def test_older_card_button_beats_newer_state():
    handler, member, staff = _handler()
    _two_cellars(member)
    handler._club_flow().remember("4242", household_id=5, code=CODE)
    tap = _tap(4242, club_cb("j", CODE, 6))
    await handler._handle_callback(tap, _context())
    assert staff.redeem_club_invite.call_args.kwargs["household_id"] == 6


@pytest.mark.asyncio
async def test_cellar_no_longer_theirs_asks_again():
    handler, member, staff = _handler()
    _two_cellars(member)
    staff.redeem_club_invite.return_value = {
        "error": "household_not_yours", "status_code": 400,
    }
    tap = _tap(4242, club_cb("j", CODE, 7))
    await handler._handle_callback(tap, _context())
    markup = tap.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        club_cb("ph", CODE, 5), club_cb("ph", CODE, 6),
    ]


@pytest.mark.asyncio
async def test_reask_ignores_another_invites_leftover_state():
    handler, member, staff = _handler()
    _two_cellars(member)
    handler._club_flow().remember("4242", code="Other123", club_name="Sunday whites")
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    staff.preview_invite.assert_called_once_with(CODE)
    assert "Thursday reds" in _edited(tap)
    assert "Sunday whites" not in _edited(tap)


@pytest.mark.asyncio
async def test_reask_on_a_dead_invite_says_fresh():
    handler, member, staff = _handler()
    _two_cellars(member)
    staff.preview_invite.return_value = _preview(used=True)
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) == t("en", "club.fresh")
    staff.redeem_club_invite.assert_not_called()


@pytest.mark.asyncio
async def test_reask_failed_preview_drops_another_invites_name():
    handler, member, staff = _handler()
    _two_cellars(member)
    staff.preview_invite.side_effect = RuntimeError("down")
    handler._club_flow().remember("4242", code="Other123", club_name="Sunday whites")
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert "Sunday whites" not in _edited(tap)


@pytest.mark.asyncio
async def test_reask_used_up_invite_for_a_member_opens_the_club():
    handler, member, staff = _handler()
    _two_cellars(member)
    member.list_clubs.return_value = {"clubs": [dict(CLUB)]}
    staff.preview_invite.return_value = _preview(used=True)
    tap = _tap(4242, club_cb("j", CODE))
    await handler._handle_callback(tap, _context())
    assert _edited(tap) != t("en", "club.fresh")
    assert "Thursday reds" in _edited(tap)
    staff.redeem_club_invite.assert_not_called()
