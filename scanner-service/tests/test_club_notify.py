"""P4 scanner side: Notifications button and screen, mode / quiet / zone
callbacks, typed zone, the 15-member Keep / Recap answers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.copy import catalog_keys, t
from integrations.telegram_bot import CALLBACK_DATA_MAX, TelegramBotHandler
from integrations.telegram_clubs import build_home, club_cb

ON = {"feature_clubs": "on", "feature_club_notify": "on"}
SAFE_EDIT = "integrations.telegram_bot._safe_edit"


def _handler(api, flags=None):
    api.list_all_wines = MagicMock(return_value=[])
    api.web_url = "https://cellar.test"
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.set_flags(ON if flags is None else flags)
    return handler


def _body(mode=None, enabled=True, tz="Europe/London", effective=None, **extra):
    body = {
        "club_id": 3, "mode": mode, "effective": effective or mode or "instant",
        "member_count": 5, "digest_members": 15,
        "quiet": {"enabled": enabled, "start": "22:00", "end": "08:00", "tz": tz},
    }
    body.update(extra)
    return body


def _api(body=None):
    api = MagicMock()
    api.get_club.return_value = {"id": 3, "name": "Thursday reds", "kind": "private"}
    api.club_notify.return_value = body or _body()
    return api


def _query(data):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.message.chat_id = 4242
    query.message.reply_text = AsyncMock()
    return query


def _update(query=None, text="x"):
    u = MagicMock()
    u.effective_chat.id = 4242
    u.effective_user.language_code = "en"
    u.callback_query = query
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _home(flags):
    payload = {
        "id": 3, "name": "Thursday reds", "member_count": 4, "kind": "private",
        "cellar_visibility": "none", "role": "member", "events": [],
    }
    return build_home(payload, "en", flags=flags)[1]


def test_button_only_with_flag():
    assert "Notifications" in _labels(_home({"feature_club_notify": True}))
    assert "Notifications" not in _labels(_home({}))
    button = next(
        b for r in _home({"feature_club_notify": True}).inline_keyboard
        for b in r if b.text == "Notifications"
    )
    assert button.callback_data == "cl:ns:3"


def test_callbacks_fit_limit():
    for data in (club_cb("nm", 123456789, 2), club_cb("nq", 123456789, "t"),
                 club_cb("nz", 123456789, "x"), club_cb("nk", 123456789)):
        assert len(data.encode()) <= CALLBACK_DATA_MAX


@pytest.mark.asyncio
async def test_screen_marks_current_mode_and_lists_zones():
    api = _api(_body(mode="digest"))
    handler = _handler(api)
    query = _query("cl:ns:3")
    with patch(SAFE_EDIT, new=AsyncMock()) as edit:
        assert await handler._club_flow().on_callback(_update(query), MagicMock())
    text = edit.call_args.args[1]
    markup = edit.call_args.kwargs["reply_markup"]
    assert "Thursday reds" in text and "22:00-08:00" in text
    labels = _labels(markup)
    assert "Every update" in labels and "✅ Daily recap" in labels and "Muted" in labels
    assert {"22-08", "23-07", "21-07"} <= {x.replace("✅ ", "") for x in labels}
    for zone in ("Johannesburg", "London", "Amsterdam", "New York", "Sydney", "Type one"):
        assert any(zone in x for x in labels)
    assert "✅ London" in labels
    assert all(len(c.encode()) <= CALLBACK_DATA_MAX for c in _cbs(markup))


@pytest.mark.asyncio
async def test_default_digest_is_explained_and_marked():
    api = _api(_body(mode=None, effective="digest"))
    query = _query("cl:ns:3")
    with patch(SAFE_EDIT, new=AsyncMock()) as edit:
        await _handler(api)._club_flow().on_callback(_update(query), MagicMock())
    assert "✅ Daily recap" in _labels(edit.call_args.kwargs["reply_markup"])
    assert "15 or more members" in edit.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("data,patch_", [
    ("cl:nm:3:0", {"mode": "instant"}),
    ("cl:nm:3:1", {"mode": "digest"}),
    ("cl:nm:3:2", {"mode": "muted"}),
    ("cl:nq:3:1", {"quiet_enabled": True, "quiet_start": "23:00", "quiet_end": "07:00"}),
    ("cl:nz:3:4", {"tz": "Australia/Sydney"}),
    ("cl:nz:3:0", {"tz": "Africa/Johannesburg"}),
])
async def test_buttons_patch_the_right_fields(data, patch_):
    api = _api()
    with patch(SAFE_EDIT, new=AsyncMock()):
        await _handler(api)._club_flow().on_callback(_update(_query(data)), MagicMock())
    api.club_notify.assert_any_call(3, patch_)


@pytest.mark.asyncio
async def test_quiet_toggle_flips_current_state():
    api = _api(_body(enabled=True))
    with patch(SAFE_EDIT, new=AsyncMock()):
        await _handler(api)._club_flow().on_callback(
            _update(_query("cl:nq:3:t")), MagicMock(),
        )
    api.club_notify.assert_any_call(3, {"quiet_enabled": False})


@pytest.mark.asyncio
async def test_type_one_validates_with_zoneinfo():
    api = _api()
    handler = _handler(api)
    flow = handler._club_flow()
    query = _query("cl:nz:3:x")
    await flow.on_callback(_update(query), MagicMock())
    assert query.message.reply_text.call_args.args[0] == t("en", "club.notify.tz_prompt")
    bad = _update(text="Mars/Base")
    assert await flow.on_text(bad, MagicMock()) is True
    assert bad.message.reply_text.call_args.args[0] == t("en", "club.notify.tz_bad")
    api.club_notify.assert_not_called()
    good = _update(text="Europe/Paris")
    assert await flow.on_text(good, MagicMock()) is True
    api.club_notify.assert_any_call(3, {"tz": "Europe/Paris"})
    assert flow.state_for("4242") is None


@pytest.mark.asyncio
async def test_keep_and_recap_answers():
    api = _api()
    with patch(SAFE_EDIT, new=AsyncMock()) as edit:
        await _handler(api)._club_flow().on_callback(_update(_query("cl:nk:3")), MagicMock())
    api.club_notify.assert_called_with(3, {"mode": "instant"})
    assert edit.call_args.args[1] == t("en", "club.notify.kept")
    api2 = _api()
    with patch(SAFE_EDIT, new=AsyncMock()):
        await _handler(api2)._club_flow().on_callback(_update(_query("cl:nr:3")), MagicMock())
    api2.club_notify.assert_called_with(3, {"mode": "digest"})


@pytest.mark.asyncio
async def test_flag_off_does_not_touch_settings():
    api = _api()
    with patch(SAFE_EDIT, new=AsyncMock()) as edit:
        await _handler(api, {"feature_clubs": "on"})._club_flow().on_callback(
            _update(_query("cl:nm:3:2")), MagicMock(),
        )
    api.club_notify.assert_not_called()
    assert edit.call_args.args[1] == t("en", "club.notify.off")


def test_copy_has_english_and_afrikaans_keys():
    keys = [k for k in catalog_keys("en") if k.startswith(("club.notify.", "chat.club.missed"))]
    assert keys
    af = set(catalog_keys("af"))
    assert all(k in af for k in keys)
