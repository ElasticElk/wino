from abc import ABC, abstractmethod
import glob
import os


def resolve_scanner_device(path: str) -> str | None:
    """Pick a USB HID keyboard-wedge scanner.

    `auto` (or a missing path) uses /dev/input/by-id/*-event-kbd, skipping
    Consumer Control nodes that are not the gun.
    """
    if path and path not in ("auto", "") and os.path.exists(path):
        return path
    cands = [
        p for p in sorted(glob.glob("/dev/input/by-id/*-event-kbd"))
        if "Consumer_Control" not in os.path.basename(p)
    ]
    return cands[0] if cands else None


class BarcodeReader(ABC):
    @abstractmethod
    def wait_for_scan(self) -> str:
        """Block until a barcode is scanned, return the barcode string."""
        pass


class MockBarcodeReader(BarcodeReader):
    """Reads barcode input from keyboard (stdin). For local development."""

    def wait_for_scan(self) -> str:
        while True:
            barcode = input("\n[SCAN] Enter barcode (or 'q' to quit): ").strip()
            if barcode == "q":
                raise KeyboardInterrupt
            if barcode:
                return barcode


class EvdevBarcodeReader(BarcodeReader):
    """Reads barcode input from a USB HID scanner via evdev. For Raspberry Pi."""

    # Key code to character mapping for standard USB barcode scanners
    KEY_MAP = {
        2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
        7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
        # Numeric keypad — some scanners send these instead of the top row.
        71: "7", 72: "8", 73: "9",
        75: "4", 76: "5", 77: "6",
        79: "1", 80: "2", 81: "3",
        82: "0",
    }
    ENTER_CODES = (28, 96)  # KEY_ENTER, KEY_KPENTER

    def __init__(self, device_path: str):
        self.device_path = device_path
        self._device = None

    def _connect(self):
        import evdev
        path = resolve_scanner_device(self.device_path)
        if not path:
            raise FileNotFoundError("no USB HID scanner under /dev/input/by-id/")
        self._device = evdev.InputDevice(path)
        self._device.grab()
        print(f"  Scanner: {path}")

    def wait_for_scan(self) -> str:
        import evdev
        import time

        while True:
            if not self._device:
                try:
                    self._connect()
                except (FileNotFoundError, OSError) as e:
                    print(f"  Waiting for USB scanner ({e})...")
                    time.sleep(2)
                    continue

            try:
                barcode = ""
                for event in self._device.read_loop():
                    if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                        if event.code in self.ENTER_CODES:
                            if barcode:
                                return barcode
                            barcode = ""
                        else:
                            char = self.KEY_MAP.get(event.code, "")
                            barcode += char
            except OSError:
                print("  Scanner disconnected — waiting for reconnection...")
                self._device = None
                time.sleep(2)  # wait before attempting reconnect


def create_reader(config: dict) -> BarcodeReader:
    backend = config["hardware"]["scanner_backend"]
    if backend == "mock":
        return MockBarcodeReader()
    elif backend == "evdev":
        return EvdevBarcodeReader(config["hardware"]["scanner_device"])
    else:
        raise ValueError(f"Unknown scanner backend: {backend}")
