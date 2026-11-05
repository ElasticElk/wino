"""Navigation: bottom bar, ☰ Menu, follow-up rows, one-shot input, double taps.

API is mocked. Menus and buttons are English; typed chat keeps its language.
"""

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReplyKeyboardMarkup
from telegram.error import BadRequest

from integrations.chat_router import LABEL_COMMANDS, match_label, normalize_label
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import club_cb
from integrations.telegram_commands import menu_commands
from integrations.telegram_nav import (
    ALREADY_DONE,
    BAR_CELLAR,
    BAR_CLUBS,
    BAR_LIST,
    BAR_MENU,
    BAR_SCAN,
    BAR_SEARCH,
    SCREENS,
    STAFF_ONLY,
    ConfirmGuard,
    NOT_PROVISIONED,
    NavContext,
    PendingInputs,
    bar_labels,
    bottom_bar,
    build_screen,
    edit_or_send_menu,
    follow_up_rows,
    is_confirm,
    menu_cb,
    parse_menu_cb,
)

FLAG_KEYS = (
    "feature_lists", "feature_clubs", "feature_club_comments",
    "feature_tasteoffs", "feature_global_clubs",
)
ALL_ON = {k: True for k in FLAG_KEYS}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _handler(api=None, *, staff=False, **flag_values):
    api = api or MagicMock()
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    api.household_id = 5
    api.get_household.return_value = {"name": "Riley & Sam", "role": "owner", "members": []}
    api.list_households.return_value = [{"id": 5, "name": "Riley & Sam"}]
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
        admin_chat_ids=["4242"] if staff else [],
    )
    handler.bot_username = "whineybot"
    handler.set_flags({k: "on" for k, v in flag_values.items() if v})
    return handler


def _update(text="hello", chat_id=4242):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.language_code = "en"
    u.effective_user.first_name = "Riley"
    u.effective_user.full_name = "Riley"
    u.message.text = text
    u.message.reply_text = AsyncMock(return_value=MagicMock(message_id=90))
    u.message.chat_id = chat_id
    u.callback_query = None
    return u


def _query(data, *, message_id=7, chat_id=4242):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = chat_id
    query.message.message_id = message_id
    query.message.text = "card"
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock(return_value=MagicMock(message_id=message_id + 1))
    query.edit_message_text = AsyncMock(return_value=MagicMock(message_id=message_id))
    query.get_bot = MagicMock(return_value=_bot())
    return query


def _tap(data, **kw):
    update = _update()
    update.callback_query = _query(data, **kw)
    return update


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=55))
    bot.delete_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.send_chat_action = AsyncMock()
    return bot


def _context(bot=None):
    context = MagicMock()
    context.bot = bot or _bot()
    context.args = None
    return context


def _labels(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def _all_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _flag_combos():
    for bits in itertools.product([False, True], repeat=len(FLAG_KEYS)):
        yield dict(zip(FLAG_KEYS, bits))


# --------------------------------------------------------------------------
# Label mapping

@pytest.mark.parametrize("text,action", [
    ("\U0001F377 Cellar", "cellar"),
    ("cellar", "cellar"),
    ("CELLAR", "cellar"),
    ("  Fridge ", "cellar"),
    ("\U0001F4F8 Scan", "scan"),
    ("\U0001F50E Search", "search"),
    ("find", "search"),
    ("☰ Menu", "menu"),
    ("menu", "menu"),
    ("Menu!", "menu"),
    ("☰", None),
    ("home", "menu"),
    ("help", "help"),
])
def test_label_mapping_case_and_emoji_tolerant(text, action):
    assert match_label(text, ALL_ON) == action


def test_bar_labels_all_map_back_to_their_action():
    expected = {
        BAR_CELLAR: "cellar", BAR_SCAN: "scan", BAR_SEARCH: "search",
        BAR_LIST: "list", BAR_CLUBS: "clubs", BAR_MENU: "menu",
    }
    for label, action in expected.items():
        assert match_label(label, ALL_ON) == action
        assert normalize_label(label) in LABEL_COMMANDS


@pytest.mark.parametrize("text", [
    "what's in my cellar?",          # a question, not a label
    "wat moet ek vanaand drink",     # Afrikaans chat
    "watter rooiwyn het ek",
    "chenin blanc",
    "/menu",                         # commands have their own handler
    "cellar " * 10,                  # too long to be a tap
    "",
])
def test_non_labels_fall_through_to_chat(text):
    assert match_label(text, ALL_ON) is None


def test_flagged_labels_respect_flags():
    off = {}
    assert match_label("list", off) is None
    assert match_label("clubs", off) is None
    # A stale bar button (emoji label) still matches, to answer "not on yet".
    assert match_label(BAR_LIST, off) == "list"
    assert match_label(BAR_CLUBS, off) == "clubs"
    assert match_label("list", {"feature_lists": True}) == "list"
    assert match_label("clubs", {"feature_clubs": True}) == "clubs"


# --------------------------------------------------------------------------
# Bottom bar

def test_bottom_bar_all_flags_on_is_two_rows_of_three():
    bar = bottom_bar(ALL_ON)
    assert isinstance(bar, ReplyKeyboardMarkup)
    rows = [[b.text for b in row] for row in bar.keyboard]
    assert rows == [[BAR_CELLAR, BAR_SCAN, BAR_SEARCH], [BAR_LIST, BAR_CLUBS, BAR_MENU]]
    assert bar.resize_keyboard is True
    assert bar.is_persistent is True
    assert bar.input_field_placeholder == "Tap a button below"


def test_bottom_bar_reflows_when_flags_are_off():
    rows = [[b.text for b in r] for r in bottom_bar({}).keyboard]
    assert rows == [[BAR_CELLAR, BAR_SCAN], [BAR_SEARCH, BAR_MENU]]
    rows = [[b.text for b in r] for r in bottom_bar({"feature_clubs": True}).keyboard]
    assert rows == [[BAR_CELLAR, BAR_SCAN, BAR_SEARCH], [BAR_CLUBS, BAR_MENU]]
    assert BAR_LIST not in bar_labels({"feature_clubs": True})
    assert BAR_CLUBS not in bar_labels({"feature_lists": True})


@pytest.mark.asyncio
async def test_start_and_help_send_the_bar():
    handler = _handler(feature_lists=True)
    update = _update("/start")
    handler._invite_flow().start = AsyncMock(return_value=False)
    handler._country_for = MagicMock(return_value="GB")
    await handler._cmd_start(update, _context())
    bar = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(bar, ReplyKeyboardMarkup)
    assert BAR_LIST in [b.text for row in bar.keyboard for b in row]
    assert "☰ Menu" in update.message.reply_text.call_args.args[0]

    update = _update("/help")
    await handler._cmd_help(update, _context())
    assert isinstance(update.message.reply_text.call_args.kwargs["reply_markup"], ReplyKeyboardMarkup)
    assert "/menu" in update.message.reply_text.call_args.args[0]


def test_menu_command_is_in_the_slash_menu():
    assert menu_commands({})[0] == ("menu", "Home menu")


# --------------------------------------------------------------------------
# Callback data budget

def test_every_menu_button_fits_64_bytes_in_every_configuration():
    for flags in _flag_combos():
        for staff in (False, True):
            ctx = NavContext(flags=flags, is_staff=staff, household="x" * 200, household_count=3)
            for screen in SCREENS:
                _text, markup = build_screen(screen, ctx)
                for data in _all_data(markup):
                    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX, data


def test_follow_up_rows_fit_64_bytes_with_huge_ids():
    huge = {"flags": ALL_ON, "wine_id": 999_999_999_999, "club_id": 999_999_999_999,
            "event_id": 999_999_999_999}
    for kind in ("fridge", "status", "added", "removed", "search", "household",
                 "club", "scan", "generic"):
        for row in follow_up_rows(kind, huge):
            for button in row:
                assert len(button.callback_data.encode("utf-8")) <= CALLBACK_DATA_MAX


def test_menu_cb_rejects_oversize_and_round_trips():
    assert parse_menu_cb(menu_cb("x", "fridge")) == ["x", "fridge"]
    assert parse_menu_cb("a:det,w:1") is None
    with pytest.raises(ValueError):
        menu_cb("x" * 70)


# --------------------------------------------------------------------------
# Menu screens: flags, staff, Back, header

def test_home_hides_lists_and_clubs_when_flags_off():
    labels = _labels(build_screen("home", NavContext(flags={}))[1])
    assert not any("Lists" in label or "Clubs" in label for label in labels)
    labels = _labels(build_screen("home", NavContext(flags=ALL_ON))[1])
    assert "\U0001F4DD Lists" in labels and "\U0001F347 Clubs" in labels


def test_gated_screens_fall_back_to_home():
    home = build_screen("home", NavContext(flags={}))
    assert build_screen("clubs", NavContext(flags={})) == home
    assert build_screen("lists", NavContext(flags={})) == home
    assert build_screen("staff", NavContext(flags={})) == home


def test_clubs_screen_flag_gates_tasteoff_and_discover():
    base = {"feature_clubs": True}
    labels = _labels(build_screen("clubs", NavContext(flags=base))[1])
    assert "\U0001F3C6 Taste-off" not in labels
    assert "\U0001F30D Discover clubs" not in labels
    assert "✉️ Invite to club" in labels
    labels = _labels(build_screen("clubs", NavContext(flags=ALL_ON))[1])
    assert "\U0001F3C6 Taste-off" in labels
    assert "\U0001F30D Discover clubs" in labels


def test_lists_screen_club_list_needs_clubs():
    labels = _labels(build_screen("lists", NavContext(flags={"feature_lists": True}))[1])
    assert "\U0001F347 Club list" not in labels
    labels = _labels(build_screen("lists", NavContext(flags=ALL_ON))[1])
    assert "\U0001F347 Club list" in labels


def test_staff_entry_only_for_staff():
    assert "\U0001F6E0 Staff" not in _labels(build_screen("home", NavContext())[1])
    assert "\U0001F6E0 Staff" in _labels(build_screen("home", NavContext(is_staff=True))[1])


def test_every_submenu_has_back_and_household_header():
    ctx = NavContext(flags=ALL_ON, is_staff=True, household="Riley <&> Sam", household_count=2)
    for screen in SCREENS:
        text, markup = build_screen(screen, ctx)
        assert "Riley &lt;&amp;&gt; Sam" in text
        if screen == "home":
            continue
        assert markup.inline_keyboard[-1][0].callback_data == menu_cb("home"), screen


def test_household_invites_are_labelled_unambiguously():
    house = _labels(build_screen("house", NavContext(flags=ALL_ON, household_count=1))[1])
    clubs = _labels(build_screen("clubs", NavContext(flags=ALL_ON))[1])
    assert "✉️ Invite to household" in house
    assert "✉️ Invite to club" in clubs
    assert not any(label.endswith("Invite") for label in house + clubs)
    # Switch only when there is somewhere to switch to.
    assert "\U0001F501 Switch household" not in house
    house2 = _labels(build_screen("house", NavContext(household_count=2))[1])
    assert "\U0001F501 Switch household" in house2


@pytest.mark.asyncio
async def test_submenu_tap_edits_the_same_message_and_back_returns_home():
    handler = _handler(feature_clubs=True)
    update = _tap(menu_cb("cellar"))
    await handler._handle_callback(update, _context())
    query = update.callback_query
    query.edit_message_text.assert_awaited_once()
    query.message.reply_text.assert_not_awaited()
    text = query.edit_message_text.call_args.args[0]
    assert "Cellar" in text and "Riley &amp; Sam" in text

    back = _tap(menu_cb("home"))
    await handler._handle_callback(back, _context())
    assert "☰ <b>Menu</b>" in back.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_forged_staff_and_flag_off_actions_are_refused():
    api = MagicMock()
    handler = _handler(api)
    handler._cmd_club = AsyncMock()
    update = _tap(menu_cb("x", "sstats"))
    await handler._handle_callback(update, _context())
    update.callback_query.answer.assert_any_await(STAFF_ONLY, show_alert=True)

    update = _tap(menu_cb("x", "clubs"))
    await handler._handle_callback(update, _context())
    handler._cmd_club.assert_not_awaited()

    # A forged screen tap for a gated screen just shows home.
    update = _tap(menu_cb("staff"))
    await handler._handle_callback(update, _context())
    assert "Staff" not in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_staff_actions_run_for_staff(monkeypatch):
    handler = _handler(staff=True)
    from integrations import telegram_admin
    called = AsyncMock()
    monkeypatch.setattr(telegram_admin, "cmd_stats", called)
    update = _tap(menu_cb("x", "sstats"))
    await handler._handle_callback(update, _context())
    called.assert_awaited_once()
    tap_update = called.call_args.args[0]
    assert tap_update.message.text == "/stats"


@pytest.mark.asyncio
async def test_menu_action_leaves_breadcrumb_and_posts_result_below():
    handler = _handler()
    handler._cmd_status = AsyncMock()
    nav = handler._nav()
    nav._live_menu["4242"] = 7
    update = _tap(menu_cb("x", "status"), message_id=7)
    await handler._handle_callback(update, _context())
    handler._cmd_status.assert_awaited_once()
    crumb = update.callback_query.edit_message_text.call_args.args[0]
    assert "Status" in crumb
    assert "4242" not in nav._live_menu


@pytest.mark.asyncio
async def test_follow_up_action_does_not_overwrite_the_result():
    handler = _handler()
    handler._cmd_status = AsyncMock()
    update = _tap(menu_cb("x", "status"), message_id=31)  # not the live menu
    await handler._handle_callback(update, _context())
    handler._cmd_status.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_menu_retires_the_old_one():
    handler = _handler()
    bot = _bot()
    nav = handler._nav()
    await nav.send_menu(bot, "4242")
    assert nav._live_menu["4242"] == 55
    bot.send_message.return_value = MagicMock(message_id=56)
    await nav.send_menu(bot, "4242")
    bot.delete_message.assert_awaited_once_with(chat_id=4242, message_id=55)
    assert nav._live_menu["4242"] == 56


@pytest.mark.asyncio
async def test_edit_or_send_menu_ignores_not_modified_and_falls_back():
    query = _query(menu_cb("home"))
    query.edit_message_text.side_effect = BadRequest("Message is not modified")
    assert await edit_or_send_menu(query, "x", None) is None
    query.message.reply_text.assert_not_awaited()

    query = _query(menu_cb("home"))
    query.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await edit_or_send_menu(query, "x", None)
    query.message.reply_text.assert_awaited_once()


# --------------------------------------------------------------------------
# Follow-up rows

def test_follow_up_rows_examples():
    fridge = [b.text for b in follow_up_rows("fridge")[0]]
    assert fridge == ["\U0001F50E Search", "\U0001F4CA Status", "☰ Menu"]
    added = [b.text for b in follow_up_rows("added", {"wine_id": 3})[0]]
    assert added == ["\U0001F377 View wine", "\U0001F4F8 Scan another", "☰ Menu"]
    club = [b.text for b in follow_up_rows("club", {
        "flags": ALL_ON, "club_id": 3, "event_id": 8,
    })[0]]
    assert club == ["\U0001F4AC Say something", "\U0001F3C6 Taste-off", "☰ Menu"]
    off = [b.text for b in follow_up_rows("club", {"flags": {}, "club_id": 3, "event_id": 8})[0]]
    assert off == ["☰ Menu"]
    for kind in ("fridge", "status", "added", "removed", "search", "household", "club", "x"):
        assert follow_up_rows(kind)[0][-1].callback_data == menu_cb("new")


@pytest.mark.asyncio
async def test_status_and_search_results_end_in_follow_ups():
    api = MagicMock()
    api.wines_in_stock.return_value = [{"id": 1, "name": "Chenin", "stock_count": 2, "wine_type": "WH"}]
    api.list_storages.return_value = []
    api.search_wines.return_value = []
    handler = _handler(api)
    update = _update("/status")
    await handler._cmd_status(update, _context())
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert labels[-1] == "☰ Menu"
    update = _update("nothing")
    await handler._do_search(update, "nothing")
    labels = _labels(update.message.reply_text.call_args.kwargs["reply_markup"])
    assert "\U0001F50E Search again" in labels


# --------------------------------------------------------------------------
# One-shot input

def test_pending_input_takes_only_the_next_message_and_expires():
    clock = Clock()
    pending = PendingInputs(ttl=120, clock=clock)
    pending.begin("1", "search")
    assert pending.take("1")["kind"] == "search"
    assert pending.take("1") is None  # only the next message
    pending.begin("1", "search")
    clock.t += 121
    assert pending.peek("1") is None
    assert pending.take("1") is None
    pending.begin("1", "search")
    assert pending.cancel("1") is not None
    assert pending.take("1") is None


@pytest.mark.asyncio
async def test_search_button_then_next_message_is_the_query():
    handler = _handler()
    handler._do_search = AsyncMock()
    tap = _tap(menu_cb("x", "search"), message_id=31)
    await handler._handle_callback(tap, _context())
    prompt = tap.callback_query.message.reply_text.call_args
    assert "Type a wine name" in prompt.args[0]
    assert _labels(prompt.kwargs["reply_markup"]) == ["✖️ Cancel"]

    await handler._unknown_text(_update("ok"), _context())  # 2 chars: not chat
    handler._do_search.assert_awaited_once()
    assert handler._do_search.call_args.args[1] == "ok"
    # Only the next message: the one after that is not a search.
    handler._router = None
    handler._do_search.reset_mock()
    later = _update("no")
    await handler._unknown_text(later, _context())
    handler._do_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_from_menu_edits_the_menu_into_the_prompt():
    handler = _handler()
    handler._nav()._live_menu["4242"] = 7
    tap = _tap(menu_cb("x", "search"), message_id=7)
    await handler._handle_callback(tap, _context())
    assert "Type a wine name" in tap.callback_query.edit_message_text.call_args.args[0]
    tap.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_search_expires_after_two_minutes():
    handler = _handler()
    clock = Clock()
    nav = handler._nav()
    nav.pending = PendingInputs(clock=clock)
    handler._do_search = AsyncMock()
    nav.pending.begin("4242", "search")
    clock.t += 121
    assert await nav.on_pending_text(_update("chenin"), _context(), "chenin") is False
    handler._do_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_clears_pending_and_shows_home():
    handler = _handler()
    handler._do_search = AsyncMock()
    handler._nav().pending.begin("4242", "search", prompt_id=7)
    tap = _tap(menu_cb("x", "cancel"), message_id=7)
    await handler._handle_callback(tap, _context())
    assert handler._nav().pending.peek("4242") is None
    assert "☰ <b>Menu</b>" in tap.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_menu_labels_are_never_captured_as_pending_input():
    handler = _handler()
    handler._do_search = AsyncMock()
    handler._nav().pending.begin("4242", "search")
    context = _context()
    await handler._unknown_text(_update(BAR_MENU), context)
    handler._do_search.assert_not_awaited()
    context.bot.send_message.assert_awaited()  # the menu
    assert handler._nav().pending.peek("4242") is None


@pytest.mark.asyncio
async def test_menu_labels_are_never_club_comments():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {"events": [{"id": 88, "kind": "opened"}], "count": 1}
    handler = _handler(api, feature_clubs=True, feature_club_comments=True)
    handler._cmd_fridge = AsyncMock()
    say = _tap(club_cb("cm", 88))
    await handler._club_flow().on_callback(say, _context())
    assert handler._club_flow().state_for("4242")["step"] == "comment"
    await handler._unknown_text(_update(BAR_CELLAR), _context())
    api.add_club_comment.assert_not_called()
    handler._cmd_fridge.assert_awaited_once()
    assert handler._club_flow().state_for("4242") is None


@pytest.mark.asyncio
async def test_starting_a_comment_replaces_a_pending_search():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {"events": [{"id": 88, "kind": "opened"}], "count": 1}
    handler = _handler(api, feature_clubs=True, feature_club_comments=True)
    handler._nav().pending.begin("4242", "search")
    await handler._club_flow().on_callback(_tap(club_cb("cm", 88)), _context())
    assert handler._nav().pending.peek("4242") is None


@pytest.mark.asyncio
async def test_a_command_drops_a_pending_search():
    handler = _handler()
    handler._nav().pending.begin("4242", "search")
    await handler._abandon_invite_if_command(_update("/fridge"), _context())
    assert handler._nav().pending.peek("4242") is None


@pytest.mark.asyncio
async def test_afrikaans_chat_still_reaches_the_router():
    handler = _handler()
    router = MagicMock()
    router.handle_text = AsyncMock(return_value=True)
    router.abandon = AsyncMock()
    handler._router = router
    await handler._unknown_text(_update("wat moet ek vanaand drink"), _context())
    router.handle_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_clubs_label_with_flag_off_says_not_on():
    handler = _handler()
    handler._cmd_club = AsyncMock()
    update = _update(BAR_CLUBS)
    await handler._unknown_text(update, _context())
    handler._cmd_club.assert_not_awaited()
    reply = update.message.reply_text.call_args
    assert "isn't switched on" in reply.args[0]
    assert BAR_CLUBS not in [b.text for row in reply.kwargs["reply_markup"].keyboard for b in row]


# --------------------------------------------------------------------------
# Double-tap safety

def test_is_confirm():
    assert is_confirm(club_cb("ly", 3))
    assert is_confirm(club_cb("dy", 3))
    assert is_confirm("a:lc,i:9")
    assert not is_confirm(club_cb("h", 3))
    assert not is_confirm("a:det,w:1")
    assert not is_confirm(menu_cb("home"))


def test_confirm_guard_first_wins_then_cooldown_then_retry():
    clock = Clock()
    guard = ConfirmGuard(cooldown=5, clock=clock)
    assert guard.claim("k") is True
    assert guard.claim("k") is False      # in flight
    clock.t += 60
    assert guard.claim("k") is False      # still in flight, however long
    guard.done("k")
    assert guard.claim("k") is False      # queued double tap right after
    clock.t += 6
    assert guard.claim("k") is True       # a real retry later is allowed
    guard.release("k")
    assert guard.claim("k") is True       # failure releases at once


@pytest.mark.asyncio
async def test_double_tap_leave_runs_once_and_says_already_done():
    api = MagicMock()
    api.get_club.return_value = {"id": 3, "name": "Thursday reds"}
    api.leave_club.return_value = {"status": "left"}
    handler = _handler(api, feature_clubs=True)
    first = _tap(club_cb("ly", 3))
    second = _tap(club_cb("ly", 3))
    await handler._handle_callback(first, _context())
    await handler._handle_callback(second, _context())
    api.leave_club.assert_called_once_with(3)
    second.callback_query.answer.assert_awaited_once_with(ALREADY_DONE)


@pytest.mark.asyncio
async def test_failed_confirm_releases_the_claim():
    api = MagicMock()
    api.get_club.return_value = {"id": 3, "name": "Thursday reds"}
    api.leave_club.side_effect = [RuntimeError("down"), {"status": "left"}]
    handler = _handler(api, feature_clubs=True)
    with pytest.raises(RuntimeError):
        await handler._handle_callback(_tap(club_cb("ly", 3)), _context())
    await handler._handle_callback(_tap(club_cb("ly", 3)), _context())
    assert api.leave_club.call_count == 2


@pytest.mark.asyncio
async def test_server_already_voted_is_shown_as_already_done():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "name": "Thursday reds"}]}
    api.get_dismantle.return_value = {"id": 77}
    api.cast_dismantle_ballot.return_value = {
        "error": "already_voted", "status_code": 409, "status": "open",
        "yes": 1, "no": 0, "pending": 1, "waiting_names": ["Sam"],
    }
    handler = _handler(api, feature_clubs=True)
    tap = _tap(club_cb("dy", 77))
    await handler._handle_callback(tap, _context())
    text = tap.callback_query.edit_message_text.call_args.args[0]
    assert text.startswith("✅ Already done.")


@pytest.mark.asyncio
async def test_server_already_moved_is_shown_as_already_done():
    api = MagicMock()
    api.list_storages.return_value = [{"id": 1, "name": "Fridge"}]
    api.list_lists.return_value = {"lists": [{"id": 4, "is_default": True}]}
    api.move_list_item_to_cellar.return_value = {
        "error": "already_moved", "status_code": 409,
    }
    handler = _handler(api, feature_lists=True)
    handler._list_flow()._personal_list_id = MagicMock(return_value=4)
    tap = _tap("a:lc,i:9")
    await handler._handle_callback(tap, _context())
    text = tap.callback_query.edit_message_text.call_args.args[0]
    assert text.startswith("Already done.")


@pytest.mark.asyncio
async def test_leave_retry_after_handover_screen_is_not_blocked():
    """Leave -> hand over needed -> Back -> Leave again redraws the same button
    in the same message; once the cooldown passes it must work again."""
    api = MagicMock()
    api.get_club.return_value = {"id": 3, "name": "Thursday reds"}
    api.leave_club.return_value = {"error": "hand_over_required", "status_code": 409}
    api.club_members.return_value = {"members": []}
    handler = _handler(api, feature_clubs=True)
    clock = Clock()
    handler._nav().guard = ConfirmGuard(clock=clock)
    await handler._handle_callback(_tap(club_cb("ly", 3)), _context())
    clock.t += 30
    await handler._handle_callback(_tap(club_cb("ly", 3)), _context())
    assert api.leave_club.call_count == 2


@pytest.mark.asyncio
async def test_old_cancel_does_not_cancel_the_live_prompt():
    handler = _handler()
    nav = handler._nav()
    nav.pending.begin("4242", "search", prompt_id=50)
    old = _tap(menu_cb("x", "cancel"), message_id=40)
    await handler._handle_callback(old, _context())
    assert nav.pending.peek("4242") is not None
    old.callback_query.edit_message_reply_markup.assert_awaited()
    live = _tap(menu_cb("x", "cancel"), message_id=50)
    await handler._handle_callback(live, _context())
    assert nav.pending.peek("4242") is None


@pytest.mark.asyncio
async def test_any_other_button_drops_a_pending_search_and_closes_its_prompt():
    handler = _handler()
    nav = handler._nav()
    nav.pending.begin("4242", "search", prompt_id=50)
    context = _context()
    await handler._handle_callback(_tap("a:x"), context)
    assert nav.pending.peek("4242") is None
    context.bot.edit_message_reply_markup.assert_awaited_with(
        chat_id=4242, message_id=50, reply_markup=None,
    )


@pytest.mark.asyncio
async def test_photo_drops_a_pending_club_comment():
    api = MagicMock()
    api.list_clubs.return_value = {"clubs": [{"id": 3, "is_last_used": True}]}
    api.club_feed.return_value = {"events": [{"id": 88, "kind": "opened"}], "count": 1}
    handler = _handler(api, feature_clubs=True, feature_club_comments=True)
    await handler._club_flow().on_callback(_tap(club_cb("cm", 88)), _context())
    handler._handle_photo = AsyncMock()
    handler._invite_flow().on_photo = AsyncMock(return_value=False)
    await handler._handle_photo_routed(_update("photo"), _context())
    assert handler._club_flow().state_for("4242") is None


def test_exact_only_keeps_typed_names_as_names():
    assert match_label("Home", ALL_ON, exact_only=True) is None
    assert match_label("My cellar", ALL_ON, exact_only=True) is None
    assert match_label(BAR_MENU, ALL_ON, exact_only=True) == "menu"


def test_flag_off_word_with_punctuation_is_not_a_label():
    assert match_label("Clubs?", {}) is None
    assert match_label("list!", {}) is None


@pytest.mark.asyncio
async def test_naming_a_cellar_home_is_not_hijacked_by_labels():
    handler = _handler()
    handler._invite_flow().state_for = MagicMock(return_value={"step": "name"})
    handler._invite_flow().on_text = AsyncMock(return_value=True)
    context = _context()
    await handler._unknown_text(_update("Home"), context)
    handler._invite_flow().on_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_removed_member_menu_tap_is_refused():
    handler = _handler()
    handler._cmd_fridge = AsyncMock()
    update = _tap(menu_cb("x", "fridge"), chat_id=999)
    update.effective_chat.id = 999
    await handler._handle_callback(update, _context())
    handler._cmd_fridge.assert_not_awaited()
    update.callback_query.answer.assert_any_await(NOT_PROVISIONED, show_alert=True)


@pytest.mark.asyncio
async def test_existing_users_get_the_bar_once_after_a_deploy():
    handler = _handler()
    handler._bar_rollout = True
    handler._router = None
    handler._do_search = AsyncMock()
    context = _context()
    await handler._unknown_text(_update("chenin"), context)
    await handler._unknown_text(_update("merlot"), context)
    intros = [c for c in context.bot.send_message.await_args_list
              if isinstance(c.kwargs.get("reply_markup"), ReplyKeyboardMarkup)]
    assert len(intros) == 1
