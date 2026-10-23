"""Local barcode decode: zxing-cpp first, then pyzbar, then retries."""

import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.barcodes import accepted_barcode, gs1_check_ok
from integrations.grok_vision import (
    _barcode_retry_images,
    build_label_prompt,
    build_triage_prompt,
    read_barcode_local,
)
from integrations.telegram_bot import DIFFERENT_BOTTLE, TelegramBotHandler


FIXTURE = Path(__file__).parent / "fixtures" / "quatrino_back_label.jpg"
QUATRINO = "8026856001468"


def test_accepted_barcode_rejects_invalid_check_digit():
    assert gs1_check_ok(QUATRINO)
    assert accepted_barcode(QUATRINO) == QUATRINO
    assert accepted_barcode("8026856001469") is None
    assert accepted_barcode("123") is None
    assert accepted_barcode("506558704244") is None


def test_label_and_triage_prompts_ask_for_barcode():
    label = build_label_prompt("GBP", "GB")
    triage = build_triage_prompt("GBP", "GB")
    assert "barcode" in label.lower()
    assert "EAN" in label
    assert "barcode" in triage.lower()
    assert "EAN" in triage or "UPC" in triage


def test_read_barcode_local_quatrino_back_label(caplog):
    photo = FIXTURE.read_bytes()
    with caplog.at_level(logging.INFO, logger="integrations.grok_vision"):
        assert read_barcode_local(photo) == QUATRINO
    assert "barcode decode engine=zxing" in caplog.text
    assert "size=" in caplog.text
    assert "ms=" in caplog.text


def test_startup_logs_whether_zxing_and_pyzbar_imported(caplog):
    from integrations.grok_vision import HAS_PYZBAR, HAS_ZXING, log_barcode_engines

    with caplog.at_level(logging.INFO, logger="integrations.grok_vision"):
        log_barcode_engines()
    if HAS_ZXING:
        assert "zxing-cpp imported successfully" in caplog.text
    else:
        assert "zxing-cpp not available" in caplog.text
    if HAS_PYZBAR:
        assert "pyzbar imported successfully" in caplog.text
    else:
        assert "pyzbar not available" in caplog.text


def test_server_logs_barcode_engines_at_startup():
    import inspect
    import server

    assert "log_barcode_engines" in inspect.getsource(server.lifespan)


def test_zxing_mocked_out_still_tries_pyzbar_and_retry(monkeypatch):
    photo = FIXTURE.read_bytes()
    calls = {"retry": 0, "pyzbar": 0, "zxing": 0}

    monkeypatch.setattr("integrations.grok_vision._try_zxing", lambda img: calls.__setitem__("zxing", calls["zxing"] + 1) or None)

    orig_pyz = __import__("integrations.grok_vision", fromlist=["_try_pyzbar"])._try_pyzbar

    def spy_pyzbar(img):
        calls["pyzbar"] += 1
        return orig_pyz(img)

    monkeypatch.setattr("integrations.grok_vision._try_pyzbar", spy_pyzbar)

    orig_retry = _barcode_retry_images

    def spy_retry(image):
        calls["retry"] += 1
        yield from orig_retry(image)

    monkeypatch.setattr("integrations.grok_vision._barcode_retry_images", spy_retry)

    read_barcode_local(photo)
    assert calls["zxing"] >= 1
    assert calls["pyzbar"] >= 1
    assert calls["retry"] >= 1


def _handler(api):
    return TelegramBotHandler(
        bot_token="t", chat_ids=["4242"], wine_cellar_api=api, pending_store={},
    )


@pytest.mark.asyncio
async def test_side_photo_path_calls_the_decoder():
    api = MagicMock()
    api.household_id = 1
    api.list_all_wines.return_value = []
    api.lookup_barcode.return_value = []
    handler = _handler(api)
    handler.set_triage_callback(lambda *a, **k: {"type": "other"})
    seen = []
    handler.set_local_barcode_reader(lambda data: seen.append(data) or QUATRINO)
    handler._create_pending(api, "_label_1")
    handler.pending_store["_label_1"]["grok_merged"] = {
        "name": "Quatrino Toscana", "producer": "Riolite Vini",
    }
    handler._await_side_photo["4242"] = {"barcode": "_label_1", "time": time.time()}
    handler._download_photo = AsyncMock(return_value=b"BACKJPEG")
    handler._attach_side_photo = AsyncMock()
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.chat_id = 4242
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    await handler._handle_photo_body(update, MagicMock())
    assert seen == [b"BACKJPEG"]
    handler._attach_side_photo.assert_awaited()
    assert handler._attach_side_photo.await_args.kwargs.get("detected") == QUATRINO


@pytest.mark.asyncio
async def test_side_photo_barcode_on_another_wine_asks_instead_of_attaching():
    api = MagicMock()
    api.household_id = 1
    api.lookup_barcode.return_value = [{"id": 99, "name": "Scaranto"}]
    handler = _handler(api)
    handler.set_triage_callback(lambda *a, **k: {"type": "other"})
    handler.set_local_barcode_reader(lambda data: QUATRINO)
    handler._create_pending(api, "_label_1")
    handler.pending_store["_label_1"]["grok_merged"] = {"name": "Quatrino Toscana"}
    handler._await_side_photo["4242"] = {"barcode": "_label_1", "time": time.time()}
    handler._download_photo = AsyncMock(return_value=b"BACKJPEG")
    handler._attach_side_photo = AsyncMock()
    update = MagicMock()
    update.effective_chat.id = 4242
    update.message.chat_id = 4242
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    await handler._handle_photo_body(update, MagicMock())
    handler._attach_side_photo.assert_not_awaited()
    text = update.message.reply_text.call_args.args[0]
    assert text == DIFFERENT_BOTTLE
