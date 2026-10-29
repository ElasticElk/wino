"""Bot-side invite flows. The API is mocked; copy and branching are the point."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_commands import ADMIN_COMMANDS, PUBLIC_COMMANDS
from integrations.copy import t
from integrations.telegram_invites import (
    FRESH_LINK,
    I2_COPY,
    KEEP_SEARCHING,
    PHOTO_NAME_FIRST,
    PHOTO_RESEND,
    STATE_TTL_SECONDS,
    TAKEN_COPY,
    TAP_AGAIN,
    UNREACHABLE,
    InviteFlow,
    default_cellar_name,
    first_bottle_text,
    format_expiry,
    join_card,
    join_share_text,
    mask_invite_code,
    mint_fail_copy,
    new_share_text,
    share_keyboard,
    suburb_from_address,
    validate_typed_name,
    welcome_join,
    welcome_member,
)


def _handler(*, staff=False, known=False, household=""):
    h = MagicMock()
    h.bot_username = "whineybot"
    h.admin_chat_ids = ["1"] if staff else []
    h.chat_ids = ["1"] if known else []
    h.api = MagicMock()
    h.admin_api = MagicMock()
    h._api_factory = None
    h._first_cellars = set()
    h._is_known = MagicMock(return_value=known)
    h._get_api = MagicMock(return_value=h.api)
    if household:
        h.api.get_household.return_value = {"name": household, "id": 7}
    else:
        h.api.get_household.side_effect = Exception("no household")
    h.api.list_households.return_value = [{"id": 7, "name": "Hillside"}]
    h.add_chat_id = MagicMock()
    return h


def _update(chat_id="999", text="/start", first="Sam"):
    u = MagicMock()
    u.effective_chat.id = int(chat_id)
    u.effective_user.first_name = first
    u.message.text = text
    u.message.reply_text = AsyncMock()
    u.message.location = None
    return u


def test_invite_is_public_and_new_cellar_is_staff_only():
    public = {c for c, _ in PUBLIC_COMMANDS}
    admin = {c for c, _ in ADMIN_COMMANDS}
    assert "invite" in public
    assert "newcellar" not in public
    assert "newcellar" in admin


def test_share_copy():
    join = join_share_text("Hillside", "https://t.me/b?start=Ab3xY9k2")
    assert "Hillside" in join
    assert "One use, lasts 30 days." in join_share_text("Hillside", "https://t.me/x?start=a")
    body = new_share_text("https://t.me/b?start=K9m2Pq4r")
    assert "start a cellar" in body
    assert "That's the whole app." in body
    assert "/help" not in body
    for text in (join, body):
        assert "Tap this, then Start" in text
        assert "Forward this, or tap Share" in text


def test_suburb_never_uses_the_street():
    assert suburb_from_address({"road": "Main", "suburb": "Observatory"}) == "Observatory"
    assert suburb_from_address({"road": "Main", "city": "Cape Town"}) == "Cape Town"
    assert suburb_from_address({"house_number": "12", "road": "Long"}) is None


def test_typed_name_rejects_a_command_and_short_names():
    assert validate_typed_name("/fridge") is not None
    assert validate_typed_name("A") is not None
    assert validate_typed_name("Riverside") is None
    assert default_cellar_name("Sam") == "Sam's cellar"
    assert default_cellar_name("") == "My cellar"


@pytest.mark.asyncio
async def test_invite_new_hidden_from_non_staff():
    handler = _handler(staff=False, known=True, household="Hillside")
    flow = InviteFlow(handler)
    u = _update("1")
    ctx = MagicMock()
    ctx.args = ["new"]
    await flow.cmd_invite(u, ctx)
    handler.api.mint_invite.assert_not_called()
    body = u.message.reply_text.call_args.args[0]
    assert "staff" in body.lower()


@pytest.mark.asyncio
async def test_invite_new_is_one_forwardable_message():
    handler = _handler(staff=True, known=True)
    handler.api.mint_invite.return_value = {"code": "K9m2Pq4r", "kind": "new"}
    flow = InviteFlow(handler)
    u = _update("1")
    ctx = MagicMock()
    ctx.args = ["new"]
    await flow.cmd_invite(u, ctx)
    handler.api.mint_invite.assert_called_once_with("new")
    assert u.message.reply_text.await_count == 1
    body = u.message.reply_text.call_args.args[0]
    assert "https://t.me/whineybot?start=K9m2Pq4r" in body
    assert "That's the whole app." in body
    share = u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert share.text == "Share invite"
    assert share.url.startswith("https://t.me/share/url?")
    assert "K9m2Pq4r" in share.url


@pytest.mark.asyncio
async def test_opening_an_invite_uses_the_staff_client():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "join", "used": False, "expired": False,
        "household_name": "Hillside",
    }
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    handler.admin_api.preview_invite.assert_called_once_with("Ab3xY9k2")
    handler.api.preview_invite.assert_not_called()


@pytest.mark.asyncio
async def test_join_is_one_tap_then_one_welcome():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "join",
        "used": False,
        "expired": False,
        "household_name": "Hillside",
        "inviter_display_name": "Alex",
    }
    handler.admin_api.redeem_invite.return_value = {
        "token": "tok",
        "household_id": 7,
        "household_name": "Hillside",
        "display_name": "Sam",
        "role": "member",
        "bottle_count": 12,
        "created_by_chat_id": "111",
    }
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["Ab3xY9k2"]
    ctx.bot.send_message = AsyncMock()
    await flow.start(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert "Alex invited you to <b>Hillside</b>." in body
    assert "Whiney keeps a cellar" in body
    buttons = [
        b
        for row in u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert [b.text for b in buttons] == ["Join", "Not now"]
    assert buttons[0].callback_data == "inv:join:Ab3xY9k2"

    query = MagicMock()
    query.data = buttons[0].callback_data
    query.message.chat_id = 999
    query.message.reply_text = AsyncMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user.first_name = "Sam"
    cb = MagicMock()
    cb.callback_query = query
    await flow.on_callback(cb, ctx)

    welcome = query.edit_message_text.call_args.args[0]
    assert welcome == welcome_join("Hillside", 12)
    assert "/fridge" not in welcome
    assert "/invite" in welcome
    handler.add_chat_id.assert_called_once()
    ctx.bot.send_message.assert_awaited()
    assert "Sam joined Hillside." in ctx.bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_new_cellar_skip_sends_one_photo_pitch():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "new", "used": False, "expired": False,
    }
    handler.admin_api.redeem_invite.return_value = {
        "token": "tok",
        "household_id": 9,
        "household_name": "Sam's cellar",
        "display_name": "Sam",
        "role": "owner",
        "first_cellar": True,
        "created_by_chat_id": "1",
    }
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["K9m2Pq4r"]
    ctx.bot.send_message = AsyncMock()
    await flow.start(u, ctx)
    assert "You're starting your own cellar, Sam." in u.message.reply_text.call_args.args[0]

    u.message.text = "Skip"
    await flow.on_text(u, ctx)
    handler.admin_api.redeem_invite.assert_called_once()
    kwargs = handler.admin_api.redeem_invite.call_args.kwargs
    assert kwargs["household_name"] == "Sam's cellar"
    pitch = u.message.reply_text.call_args.args[0]
    assert pitch == I2_COPY.format(name="Sam's cellar")
    assert "/help" not in pitch
    assert "/invite" not in pitch
    assert "999" in handler._first_cellars


@pytest.mark.asyncio
async def test_typed_name_and_duplicate_offers_buttons():
    handler = _handler()
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    handler.admin_api.redeem_invite.return_value = {
        "error": "A household called Riverside already exists",
        "status_code": 409,
    }
    u = _update(text="Riverside")
    ctx = MagicMock()
    await flow.on_text(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert body == TAKEN_COPY
    buttons = [
        b
        for row in u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    labels = [b.text for b in buttons]
    assert "Riverside (Sam)" in labels
    assert "Type another" in labels
    assert buttons[0].callback_data == "inv:tn:K9m2Pq4r:Riverside (Sam)"
    assert flow.state_for("999") is not None  # not burned on our side either


@pytest.mark.asyncio
async def test_suburb_pin_suggests_without_storing_coords():
    handler = _handler()
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="c", kind="new", display_name="Sam")
    u = _update()
    u.message.location = MagicMock(latitude=-33.9, longitude=18.4)
    ctx = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "integrations.telegram_invites.reverse_geocode",
            lambda lat, lon, timeout=3: "Observatory",
        )
        await flow.on_location(u, ctx)
    assert flow.state_for("999")["suggested"] == "Observatory"
    assert "lat" not in flow.state_for("999")
    assert "Call it Observatory?" in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_geocode_fail_falls_through_without_guilt():
    handler = _handler()
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="c", kind="new", display_name="Sam")
    u = _update()
    u.message.location = MagicMock(latitude=0, longitude=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "integrations.telegram_invites.reverse_geocode",
            lambda lat, lon, timeout=3: None,
        )
        await flow.on_location(u, MagicMock())
    body = u.message.reply_text.call_args.args[0]
    assert "Skip" in body
    assert "sorry" not in body.lower()
    assert "denied" not in body.lower()


@pytest.mark.asyncio
async def test_photo_during_naming_auto_names_and_runs_photo_handler():
    handler = _handler()
    handler._handle_photo = AsyncMock()
    handler.admin_api.redeem_invite.return_value = {
        "token": "tok",
        "household_id": 9,
        "household_name": "Sam's cellar",
        "role": "owner",
        "first_cellar": True,
    }
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="c", kind="new", display_name="Sam")
    u = _update()
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    handled = await flow.on_photo(u, ctx)
    assert handled is True
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Sam's cellar"
    ack = u.message.reply_text.call_args.args[0]
    assert "Sam's cellar it is. Reading the label" in ack
    handler._handle_photo.assert_awaited_once()
    # I2 is skipped — the photo is the next step.
    assert not any(
        "is live" in (c.args[0] if c.args else "")
        for c in u.message.reply_text.await_args_list
    )


@pytest.mark.asyncio
async def test_command_during_naming_is_not_run():
    handler = _handler()
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="c", kind="new", display_name="Sam")
    u = _update(text="/fridge")
    await flow.on_text(u, MagicMock())
    handler.admin_api.redeem_invite.assert_not_called()
    assert "Name the cellar first" in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_same_code_again_resumes_without_a_second_house():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "new", "used": False, "expired": False,
    }
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="c", kind="new", display_name="Sam")
    u = _update()
    ctx = MagicMock()
    ctx.args = ["c"]
    await flow.start(u, ctx)
    handler.admin_api.redeem_invite.assert_not_called()
    assert "You're starting your own cellar, Sam." in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_already_in_a_household_asks_before_a_second_house():
    handler = _handler(known=True, household="Yskas")
    handler.admin_api.preview_invite.return_value = {
        "kind": "new", "used": False, "expired": False,
    }
    flow = InviteFlow(handler)
    u = _update("1")
    ctx = MagicMock()
    ctx.args = ["c"]
    await flow.start(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert "already in Yskas" in body
    labels = [
        b.text
        for row in u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert labels == ["Stay", "Start a new cellar"]
    handler.admin_api.redeem_invite.assert_not_called()


@pytest.mark.asyncio
async def test_expired_code_asks_for_a_fresh_link():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {"error": "gone", "status_code": 410}
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["dead"]
    await flow.start(u, ctx)
    assert u.message.reply_text.call_args.args[0] == FRESH_LINK
    assert "999" not in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_already_owns_that_cellar():
    handler = _handler(known=True, household="Riverside")
    handler.api.list_households.return_value = [{"id": 7, "name": "Riverside"}]
    handler.admin_api.preview_invite.return_value = {
        "kind": "join",
        "used": True,
        "expired": False,
        "household": {"id": 7, "name": "Riverside"},
        "household_name": "Riverside",
    }
    flow = InviteFlow(handler)
    u = _update("1")
    ctx = MagicMock()
    ctx.args = ["used"]
    await flow.start(u, ctx)
    assert "already in Riverside" in u.message.reply_text.call_args.args[0]
    handler.admin_api.redeem_invite.assert_not_called()


@pytest.mark.asyncio
async def test_used_invite_resumes_and_hot_adds_the_same_chat():
    """Lost welcome after a successful redeem: tapping the link again must
    fetch the token and put them on the allow list, not say the code is dead."""
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "new", "used": True, "expired": False,
        "household": {"id": 9, "name": "Home"},
        "household_name": "Home",
    }
    handler.admin_api.redeem_invite.return_value = {
        "token": "tok",
        "household_id": 9,
        "household_name": "Home",
        "already_member": True,
        "role": "owner",
    }
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["inSBXC11"]
    await flow.start(u, ctx)
    handler.admin_api.redeem_invite.assert_called_once_with("inSBXC11", "999", "Sam")
    handler.add_chat_id.assert_called_once()
    assert "already in Home" in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_used_invite_for_someone_else_asks_for_a_fresh_link():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {
        "kind": "join", "used": True, "expired": False,
        "household": {"id": 7, "name": "Hillside"},
        "household_name": "Hillside",
    }
    handler.admin_api.redeem_invite.return_value = {
        "error": "This invite was already used or has expired",
        "status_code": 410,
    }
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    assert u.message.reply_text.call_args.args[0] == FRESH_LINK
    handler.add_chat_id.assert_not_called()


def test_first_bottle_copy_only_fires_once():
    handler = MagicMock()
    handler._first_cellars = {"999"}
    from integrations.telegram_invites import maybe_first_bottle
    text = maybe_first_bottle(handler, "999", "Riverside", "Kanonkop", 1)
    assert text == first_bottle_text("Riverside", "Kanonkop")
    assert "Fridges and extra people can wait." in text
    assert "999" not in handler._first_cellars
    assert maybe_first_bottle(handler, "999", "Riverside", "Kanonkop", 1) is None


def _query(data, chat_id=999, first="Sam"):
    query = MagicMock()
    query.data = data
    query.message.chat_id = chat_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user.first_name = first
    cb = MagicMock()
    cb.callback_query = query
    return cb, query


def _ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


_PROVISIONED = {
    "token": "tok", "household_id": 9, "household_name": "Barolo", "role": "owner",
    "currency": "GBP", "country": "GB",
}


@pytest.mark.asyncio
async def test_state_expires():
    handler = _handler(known=True)
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    flow._states["1"]["updated_at"] -= STATE_TTL_SECONDS + 1
    assert flow.state_for("1") is None
    assert await flow.on_text(_update("1", text="Barolo"), _ctx()) is False
    handler.admin_api.redeem_invite.assert_not_called()


@pytest.mark.asyncio
async def test_known_user_typed_name_does_not_provision():
    handler = _handler(known=True)
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    u = _update("1", text="Barolo")
    assert await flow.on_text(u, _ctx()) is True
    handler.admin_api.redeem_invite.assert_not_called()
    assert u.message.reply_text.call_args.args[0] == "Call it Barolo?"
    first = u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert first.callback_data == "inv:ok:K9m2Pq4r:Barolo"
    assert first.text == "Use Barolo"


@pytest.mark.asyncio
async def test_known_user_confirm_commits_that_name():
    handler = _handler(known=True)
    handler.admin_api.redeem_invite.return_value = dict(_PROVISIONED)
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    cb, query = _query("inv:ok:K9m2Pq4r:Barolo", chat_id=1)
    await flow.on_callback(cb, _ctx())
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Barolo"
    assert flow.state_for("1") is None


@pytest.mark.asyncio
async def test_keep_searching_clears_state():
    handler = _handler(known=True)
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    cb, query = _query("inv:drop", chat_id=1)
    await flow.on_callback(cb, _ctx())
    assert flow.state_for("1") is None
    handler.admin_api.redeem_invite.assert_not_called()
    assert query.edit_message_text.call_args.args[0] == KEEP_SEARCHING


@pytest.mark.asyncio
async def test_confirm_text_falls_through_to_search():
    handler = _handler(known=True)
    flow = InviteFlow(handler)
    flow.remember("1", step="confirm", code="K9m2Pq4r", kind="new", display_name="Sam")
    assert await flow.on_text(_update("1", text="Barolo"), _ctx()) is False
    assert flow.state_for("1") is None
    handler.admin_api.redeem_invite.assert_not_called()


@pytest.mark.asyncio
async def test_photo_during_confirm_does_not_provision():
    handler = _handler(known=True)
    handler._handle_photo = AsyncMock()
    flow = InviteFlow(handler)
    flow.remember("1", step="confirm", code="K9m2Pq4r", kind="new", display_name="Sam")
    assert await flow.on_photo(_update("1"), _ctx()) is False
    handler.admin_api.redeem_invite.assert_not_called()
    assert flow.state_for("1") is None


@pytest.mark.asyncio
async def test_known_user_photo_during_naming_does_not_provision():
    handler = _handler(known=True)
    handler._handle_photo = AsyncMock()
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    u = _update("1")
    assert await flow.on_photo(u, _ctx()) is True
    handler.admin_api.redeem_invite.assert_not_called()
    assert u.message.reply_text.call_args.args[0] == PHOTO_NAME_FIRST


@pytest.mark.asyncio
async def test_known_user_command_leaves_naming():
    handler = _handler(known=True)
    flow = InviteFlow(handler)
    flow.remember("1", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    assert await flow.on_text(_update("1", text="/fridge"), _ctx()) is False
    assert flow.state_for("1") is None


@pytest.mark.asyncio
async def test_stranger_typed_name_still_provisions():
    handler = _handler()
    handler.admin_api.redeem_invite.return_value = dict(_PROVISIONED, household_name="Riverside")
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    await flow.on_text(_update(text="Riverside"), _ctx())
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Riverside"


@pytest.mark.asyncio
async def test_taken_button_commits_its_label():
    handler = _handler()
    handler.admin_api.redeem_invite.return_value = dict(_PROVISIONED, household_name="Riverside (Sam)")
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    cb, _ = _query("inv:tn:K9m2Pq4r:Riverside (Sam)")
    await flow.on_callback(cb, _ctx())
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Riverside (Sam)"


@pytest.mark.asyncio
async def test_skip_of_taken_default_does_not_loop():
    handler = _handler()
    handler.admin_api.redeem_invite.return_value = {
        "error": TAKEN_COPY, "status_code": 409, "suggested_name": "Sam's cellar 2",
    }
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    cb, query = _query("inv:skip:K9m2Pq4r")
    await flow.on_callback(cb, _ctx())
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Sam's cellar"
    button = query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Sam's cellar 2"

    handler.admin_api.redeem_invite.return_value = dict(_PROVISIONED, household_name="Sam's cellar 2")
    cb, _ = _query(button.callback_data)
    await flow.on_callback(cb, _ctx())
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Sam's cellar 2"


@pytest.mark.asyncio
async def test_join_works_with_no_state():
    """A restart wipes memory; the code in the button still redeems."""
    handler = _handler()
    handler.admin_api.redeem_invite.return_value = {
        "token": "tok", "household_id": 7, "household_name": "Hillside",
        "role": "member", "bottle_count": 0,
    }
    flow = InviteFlow(handler)
    cb, query = _query("inv:join:Ab3xY9k2")
    await flow.on_callback(cb, _ctx())
    assert handler.admin_api.redeem_invite.call_args.args[0] == "Ab3xY9k2"
    handler.add_chat_id.assert_called_once()


@pytest.mark.asyncio
async def test_join_without_code_or_state_is_not_expired():
    handler = _handler()
    flow = InviteFlow(handler)
    cb, query = _query("inv:join")
    await flow.on_callback(cb, _ctx())
    assert query.edit_message_text.call_args.args[0] == TAP_AGAIN
    handler.admin_api.redeem_invite.assert_not_called()


@pytest.mark.asyncio
async def test_naming_button_after_restart_re_previews():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {"kind": "new", "used": False, "expired": False}
    handler.admin_api.redeem_invite.return_value = dict(_PROVISIONED, household_name="Sam's cellar")
    flow = InviteFlow(handler)
    cb, _ = _query("inv:skip:K9m2Pq4r")
    await flow.on_callback(cb, _ctx())
    handler.admin_api.preview_invite.assert_called_once_with("K9m2Pq4r")
    assert handler.admin_api.redeem_invite.call_args.kwargs["household_name"] == "Sam's cellar"


@pytest.mark.asyncio
async def test_preview_exception_is_unreachable_not_expired():
    handler = _handler()
    handler.admin_api.preview_invite.side_effect = Exception("connection refused")
    flow = InviteFlow(handler)
    u = _update()
    ctx = _ctx()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    assert u.message.reply_text.call_args.args[0] == UNREACHABLE


@pytest.mark.asyncio
async def test_server_error_is_unreachable_not_expired():
    handler = _handler()
    handler.admin_api.preview_invite.return_value = {"error": "HTTP 502", "status_code": 502}
    flow = InviteFlow(handler)
    u = _update()
    ctx = _ctx()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    assert u.message.reply_text.call_args.args[0] == UNREACHABLE


@pytest.mark.asyncio
async def test_missing_admin_api_is_unreachable():
    handler = _handler()
    handler.admin_api = None
    handler.api = None
    flow = InviteFlow(handler)
    u = _update()
    ctx = _ctx()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    assert u.message.reply_text.call_args.args[0] == UNREACHABLE


@pytest.mark.asyncio
async def test_hot_add_builds_client_from_redeem_token(monkeypatch):
    handler = _handler()
    handler.api.base_url = "http://web:8000"
    handler.api.default_storage = "Wine Fridge"
    handler.api.web_url = ""
    built = {}

    class FakeAPI:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr("integrations.wine_cellar_api.WineCellarAPI", FakeAPI)
    handler.admin_api.preview_invite.return_value = {
        "kind": "new", "used": False, "expired": False,
    }
    handler.admin_api.redeem_invite.return_value = dict(
        _PROVISIONED, token="user-tok", household_id=9, first_cellar=True,
        household_name="Sam's cellar",
    )
    flow = InviteFlow(handler)
    u = _update()
    ctx = MagicMock()
    ctx.args = ["K9m2Pq4r"]
    ctx.bot.send_message = AsyncMock()
    await flow.start(u, ctx)
    u.message.text = "Skip"
    await flow.on_text(u, ctx)
    assert built["api_token"] == "user-tok"
    assert built["household_id"] == 9
    assert built["default_storage"] == "Cellar"
    client = handler.add_chat_id.call_args.kwargs["api_client"]
    assert isinstance(client, FakeAPI)
    handler.set_currency.assert_called_with("999", "GBP")
    handler.set_country.assert_called_with("999", "GB")
    assert any(
        "Where should I price" in (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in ctx.bot.send_message.call_args_list
    )


@pytest.mark.asyncio
async def test_country_tap_saves_aud_for_australia():
    handler = _handler()
    handler.api.update_settings.return_value = {"currency": "AUD", "country": "AU"}
    flow = InviteFlow(handler)
    cb, query = _query("inv:cy:AU")
    await flow.on_callback(cb, _ctx())
    handler.api.update_settings.assert_called_once_with(country="AU")
    handler.set_currency.assert_called_with("999", "AUD")
    handler.set_country.assert_called_with("999", "AU", prompted=True)
    assert "Australia" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_country_skip_keeps_euro():
    handler = _handler()
    handler.api.update_settings.return_value = {"currency": "EUR", "country": ""}
    flow = InviteFlow(handler)
    cb, query = _query("inv:cy:skip")
    await flow.on_callback(cb, _ctx())
    handler.api.update_settings.assert_called_once_with(country="")
    handler.set_currency.assert_called_with("999", "EUR")
    assert "Euro" in query.edit_message_text.call_args.args[0]


def test_new_zealand_is_on_the_country_keyboard():
    from integrations.telegram_invites import COUNTRY_CHOICES, country_keyboard

    assert ("NZ", "New Zealand") in COUNTRY_CHOICES
    labels = [b.text for row in country_keyboard().inline_keyboard for b in row]
    assert "New Zealand" in labels


@pytest.mark.asyncio
async def test_country_save_failure_offers_retry():
    handler = _handler()
    handler.api.update_settings.side_effect = Exception("timeout")
    flow = InviteFlow(handler)
    cb, query = _query("inv:cy:AU")
    await flow.on_callback(cb, _ctx())
    handler.api.update_settings.assert_called_once()
    assert "Couldn't save" in query.edit_message_text.call_args.args[0]
    labels = [
        b.text
        for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "Try again" in labels


@pytest.mark.asyncio
async def test_not_now_says_the_link_still_works():
    handler = _handler()
    flow = InviteFlow(handler)
    flow.remember(
        "999", step="join", code="Ab3xY9k2", kind="join",
        expires_at="2026-10-19T12:00:00+00:00",
    )
    cb, query = _query("inv:no:Ab3xY9k2")
    await flow.on_callback(cb, _ctx())
    body = query.edit_message_text.call_args.args[0]
    assert "still works" in body
    assert "Ask them for a link" not in body


@pytest.mark.asyncio
async def test_share_button_on_join():
    handler = _handler(known=True)
    handler.api.mint_invite.return_value = {
        "code": "Ab3xY9k2", "kind": "join",
        "household": {"id": 7, "name": "Hillside"},
        "expires_at": "2026-10-19T12:00:00+00:00",
    }
    flow = InviteFlow(handler)
    u = _update("1", text="/invite")
    ctx = _ctx()
    ctx.args = []
    await flow.cmd_invite(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert "Tap this, then Start" in body
    assert "Forward this, or tap Share" in body
    share = u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert share.url.startswith("https://t.me/share/url?")


def test_ampersand_names_are_escaped():
    link = "https://t.me/b?start=Ab3xY9k2"
    assert "Ben &amp; Sue" in join_share_text("Ben & Sue", link)
    text, _ = join_card("Ben & Sue", "A < B", "Ab3xY9k2")
    assert "Ben &amp; Sue" in text and "A &lt; B" in text
    member = welcome_member("Ben & Sue", "A < B")
    assert "Ben &amp; Sue" in member and "A &lt; B" in member
    assert "A &lt; B" in welcome_join("A < B", 0)


@pytest.mark.asyncio
async def test_single_household_id_is_passed():
    """Staff with one household used to 400 for lack of household_id."""
    handler = _handler(staff=True, known=True)
    handler.api.mint_invite.return_value = {
        "code": "Ab3xY9k2", "kind": "join", "household": {"id": 7, "name": "Hillside"},
    }
    flow = InviteFlow(handler)
    ctx = _ctx()
    ctx.args = []
    await flow.cmd_invite(_update("1", text="/invite"), ctx)
    handler.api.mint_invite.assert_called_once_with("join", household_id=7)


@pytest.mark.asyncio
async def test_already_there_by_id_not_name():
    handler = _handler(known=True, household="Other")
    handler.api.list_households.return_value = [
        {"id": 7, "name": "Other"}, {"id": 9, "name": "Riverside"},
    ]
    flow = InviteFlow(handler)
    preview = {
        "kind": "join", "used": False, "expired": False,
        "household": {"id": 9, "name": "Riverside"}, "household_name": "Riverside",
    }
    handler.admin_api.preview_invite.return_value = preview
    u = _update("1")
    ctx = _ctx()
    ctx.args = ["Ab3xY9k2"]
    await flow.start(u, ctx)
    assert "already in Riverside" in u.message.reply_text.call_args.args[0]

    handler.admin_api.preview_invite.return_value = dict(preview, household={"id": 99, "name": "Riverside"})
    u = _update("1")
    await flow.start(u, ctx)
    assert "invited you to" in u.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_photo_when_name_taken_asks_for_a_resend():
    handler = _handler()
    handler._handle_photo = AsyncMock()
    handler.admin_api.redeem_invite.return_value = {"error": TAKEN_COPY, "status_code": 409}
    flow = InviteFlow(handler)
    flow.remember("999", step="name", code="K9m2Pq4r", kind="new", display_name="Sam")
    u = _update()
    assert await flow.on_photo(u, _ctx()) is True
    handler._handle_photo.assert_not_awaited()
    assert u.message.reply_text.call_args.args[0] == PHOTO_RESEND


@pytest.mark.asyncio
async def test_cancel_names_the_cellar():
    handler = _handler(known=True, household="Hillside")
    handler.api.cancel_invite.return_value = {
        "status": "cancelled", "code": "Ab3xY9k2", "kind": "join",
        "household_name": "Hillside",
    }
    flow = InviteFlow(handler)
    u = _update("1", text="/invite cancel")
    ctx = _ctx()
    ctx.args = ["cancel"]
    await flow.cmd_invite(u, ctx)
    handler.api.cancel_invite.assert_called_once_with(household_id=7)
    assert u.message.reply_text.call_args.args[0] == "Cancelled the invite to Hillside (Ab3xY9k2)."


def test_invite_codes_are_masked_in_logs():
    assert mask_invite_code("Ab3xY9k2") == "…k2"
    assert mask_invite_code("") == "…"
    assert "Ab3xY9k2" not in mask_invite_code("Ab3xY9k2")


def test_expiry_line():
    from datetime import datetime, timezone
    this_year = datetime.now(timezone.utc).year
    assert format_expiry(f"{this_year}-10-19T12:00:00+00:00") == "Works until 19 Oct, one use."
    assert format_expiry(None) == "One use, lasts 30 days."
    assert format_expiry(f"{this_year + 1}-01-02T00:00:00+00:00") == (
        f"Works until 2 Jan {this_year + 1}, one use."
    )
    assert format_expiry(None, locale="af") == "Een keer, hou 30 dae."


@pytest.mark.asyncio
async def test_mint_503_shows_fail_copy_not_the_exception():
    handler = _handler(known=True, household="Hillside")
    handler.api.mint_invite.return_value = {
        "error": "could not mint a unique invite code",
        "status_code": 503,
    }
    flow = InviteFlow(handler)
    u = _update("1", text="/invite")
    u.effective_user.language_code = "en"
    ctx = MagicMock()
    ctx.args = []
    await flow.cmd_invite(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert body == t("en", "invite.mint.fail")
    assert "could not mint a unique invite code" not in body
    assert "IntegrityError" not in body


@pytest.mark.asyncio
async def test_mint_503_is_afrikaans_when_language_code_is_af():
    handler = _handler(known=True, household="Hillside")
    handler.api.mint_invite.return_value = {
        "error": "could not mint a unique invite code",
        "status_code": 503,
    }
    flow = InviteFlow(handler)
    u = _update("1", text="/invite")
    u.effective_user.language_code = "af"
    ctx = MagicMock()
    ctx.args = []
    await flow.cmd_invite(u, ctx)
    assert u.message.reply_text.call_args.args[0] == t("af", "invite.mint.fail")


@pytest.mark.asyncio
async def test_mint_400_shows_bad_household_copy():
    handler = _handler(known=True, household="Hillside")
    handler.api.mint_invite.return_value = {
        "error": "No household found",
        "status_code": 400,
    }
    flow = InviteFlow(handler)
    u = _update("1", text="/invite")
    u.effective_user.language_code = "en"
    ctx = MagicMock()
    ctx.args = []
    await flow.cmd_invite(u, ctx)
    assert u.message.reply_text.call_args.args[0] == t("en", "invite.mint.bad_hh")


@pytest.mark.asyncio
async def test_mint_exception_does_not_leak():
    handler = _handler(known=True, household="Hillside")
    handler.api.mint_invite.side_effect = RuntimeError("boom")
    flow = InviteFlow(handler)
    u = _update("1", text="/invite")
    u.effective_user.language_code = "en"
    ctx = MagicMock()
    ctx.args = []
    await flow.cmd_invite(u, ctx)
    body = u.message.reply_text.call_args.args[0]
    assert body == t("en", "invite.mint.fail")
    assert "boom" not in body


def test_afrikaans_join_share_and_button():
    this_year = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc,
    ).year
    body = join_share_text(
        "Hillside",
        "https://t.me/b?start=Ab3xY9k2",
        f"{this_year}-11-28T12:00:00+00:00",
        locale="af",
    )
    assert "Sluit by <b>Hillside</b> aan op Whiney." in body
    assert "Tik hier, dan Start" in body
    assert "Werk tot 28 Nov, een keer." in body
    kb = share_keyboard("https://t.me/x", "text", locale="af")
    assert kb.inline_keyboard[0][0].text == "Deel uitnodiging"


def test_mint_fail_copy_never_includes_exception_text():
    assert mint_fail_copy(exc=True) == t("en", "invite.mint.fail")
    assert "boom" not in mint_fail_copy(
        {"error": "boom", "status_code": 503},
    )
