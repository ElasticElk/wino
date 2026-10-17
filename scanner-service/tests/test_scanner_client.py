"""Client-side scan glue, with no USB hardware."""

from unittest.mock import MagicMock, patch

from hardware.barcode_reader import (
    EvdevBarcodeReader,
    create_reader,
    resolve_scanner_device,
)
from hardware.gpio_controls import MockGpioControls, ScanMode, create_controls
from scanner_client import apply_beep, post_scan


def test_mock_backend_is_the_no_hardware_path():
    reader = create_reader({"hardware": {"scanner_backend": "mock"}})
    gpio = create_controls({"hardware": {"scanner_backend": "mock"}})
    assert gpio.read_mode() is ScanMode.IN
    gpio.toggle()
    assert gpio.read_mode() is ScanMode.OUT
    assert reader.__class__.__name__ == "MockBarcodeReader"


def test_auto_device_skips_consumer_control(tmp_path, monkeypatch):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-Gun-event-kbd").touch()
    (by_id / "usb-Gun-event-if01-event-kbd").touch()
    consumer = by_id / "usb-Gun-Consumer_Control-event-kbd"
    consumer.touch()
    monkeypatch.setattr(
        "hardware.barcode_reader.glob.glob",
        lambda pattern: [str(p) for p in sorted(by_id.iterdir())],
    )
    picked = resolve_scanner_device("auto")
    assert picked.endswith("-event-kbd")
    assert "Consumer_Control" not in picked


def test_evdev_maps_digits_and_keypad_enter():
    assert EvdevBarcodeReader.KEY_MAP[2] == "1"
    assert EvdevBarcodeReader.KEY_MAP[11] == "0"
    assert EvdevBarcodeReader.KEY_MAP[82] == "0"
    assert 28 in EvdevBarcodeReader.ENTER_CODES
    assert 96 in EvdevBarcodeReader.ENTER_CODES


def test_post_scan_sends_the_fridge_contract():
    fake = MagicMock()
    fake.json.return_value = {
        "status": "added", "beep": "in",
        "wine_name": "Test Chenin", "stock_count": 3,
    }
    fake.raise_for_status = MagicMock()
    with patch("scanner_client.requests.post", return_value=fake) as posted:
        result = post_scan("http://nas.local:8080", "the-key", "6001", "in")

    assert result["status"] == "added"
    posted.assert_called_once()
    args, kwargs = posted.call_args
    assert args[0] == "http://nas.local:8080/scan"
    assert kwargs["json"] == {
        "barcode": "6001", "mode": "in", "api_key": "the-key",
    }


def test_beep_unknown_is_the_fallback():
    gpio = MockGpioControls()
    gpio.beep_unknown = MagicMock()
    apply_beep(gpio, "nope")
    gpio.beep_unknown.assert_called_once()
