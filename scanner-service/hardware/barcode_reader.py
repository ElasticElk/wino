from abc import ABC, abstractmethod


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
    }

    def __init__(self, device_path: str):
        self.device_path = device_path
        self._device = None

    def _connect(self):
        import evdev
        self._device = evdev.InputDevice(self.device_path)
        self._device.grab()

    def wait_for_scan(self) -> str:
        import evdev
        import time

        while True:
            if not self._device:
                self._connect()

            try:
                barcode = ""
                for event in self._device.read_loop():
                    if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                        if event.code == 28:  # Enter key = end of barcode
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
