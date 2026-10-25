"""Photo triage: 60s timeout, one automatic retry, then a friendlier error."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from integrations.grok_vision import TRIAGE_TIMEOUT, GrokVisionAPI
from integrations.telegram_bot import PHOTO_TRIAGE_FAILED, TelegramBotHandler


def _ok_response(payload: dict):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": __import__("json").dumps(payload)}}],
    }
    return resp


def test_triage_timeout_is_about_a_minute():
    assert TRIAGE_TIMEOUT == 60


def test_triage_retries_once_after_a_timeout():
    grok = GrokVisionAPI(api_key="k")
    seen = {"n": 0}

    def post(*a, **kw):
        seen["n"] += 1
        seen["timeout"] = kw.get("timeout")
        if seen["n"] == 1:
            raise requests.Timeout("slow")
        return _ok_response({"type": "label", "name": "Chenin"})

    with patch("integrations.grok_vision.requests.post", side_effect=post):
        data = grok.triage_photo(b"jpeg")

    assert seen["n"] == 2
    assert seen["timeout"] == 60
    assert data["type"] == "label"
    assert data["name"] == "Chenin"


def test_triage_gives_up_after_two_timeouts():
    grok = GrokVisionAPI(api_key="k")

    with patch(
        "integrations.grok_vision.requests.post",
        side_effect=requests.Timeout("slow"),
    ) as posted:
        assert grok.triage_photo(b"jpeg") is None
    assert posted.call_count == 2


def test_triage_does_not_retry_other_request_errors():
    grok = GrokVisionAPI(api_key="k")

    with patch(
        "integrations.grok_vision.requests.post",
        side_effect=requests.ConnectionError("down"),
    ) as posted:
        assert grok.triage_photo(b"jpeg") is None
    assert posted.call_count == 1


@pytest.mark.asyncio
async def test_photo_handler_uses_the_friendlier_timeout_copy():
    api = MagicMock()
    handler = TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )
    handler.set_triage_callback(lambda *a, **k: None)
    handler._download_photo = AsyncMock(return_value=b"jpeg")
    handler.set_local_barcode_reader(lambda data: None)
    status = AsyncMock()
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.chat_id = 4242
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock(return_value=status)

    await handler._handle_photo_body(update, MagicMock())

    status.edit_text.assert_awaited()
    assert status.edit_text.call_args.args[0] == PHOTO_TRIAGE_FAILED
    assert "could not analyse" not in status.edit_text.call_args.args[0].lower()
