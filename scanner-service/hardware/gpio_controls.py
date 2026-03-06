from abc import ABC, abstractmethod
from enum import Enum


class ScanMode(Enum):
    IN = "in"
    OUT = "out"


class GpioControls(ABC):
    @abstractmethod
    def read_mode(self) -> ScanMode:
        pass

    @abstractmethod
    def beep_in(self):
        pass

    @abstractmethod
    def beep_out(self):
        pass

    @abstractmethod
    def beep_unknown(self):
        pass

    @abstractmethod
    def beep_choose(self):
        pass


class MockGpioControls(GpioControls):
    """Mock GPIO for local development. Toggle mode via keyboard."""

    def __init__(self):
        self._mode = ScanMode.IN

    def toggle(self):
        if self._mode == ScanMode.IN:
            self._mode = ScanMode.OUT
        else:
            self._mode = ScanMode.IN
        print(f"  [GPIO] Mode switched to: {self._mode.value.upper()}")

    def read_mode(self) -> ScanMode:
        return self._mode

    def beep_in(self):
        print("  [BEEP] ✓ Added (high tone)")

    def beep_out(self):
        print("  [BEEP] ✗ Removed (low tone)")

    def beep_unknown(self):
        print("  [BEEP] ? Unknown barcode (double beep)")

    def beep_choose(self):
        print("  [BEEP] ⚡ Multiple matches — check Telegram")


class PiGpioControls(GpioControls):
    """Real GPIO controls for Raspberry Pi."""

    def __init__(self, toggle_pin: int, buzzer_pin: int, led_green_pin: int, led_red_pin: int):
        from gpiozero import LED, Button, Buzzer

        self._toggle = Button(toggle_pin, pull_up=True)
        self._buzzer = Buzzer(buzzer_pin)
        self._led_green = LED(led_green_pin)
        self._led_red = LED(led_red_pin)

    def read_mode(self) -> ScanMode:
        if self._toggle.is_pressed:
            self._led_red.on()
            self._led_green.off()
            return ScanMode.OUT
        else:
            self._led_green.on()
            self._led_red.off()
            return ScanMode.IN

    def _buzz(self, duration: float, count: int = 1):
        import time

        for i in range(count):
            self._buzzer.on()
            time.sleep(duration)
            self._buzzer.off()
            if i < count - 1:
                time.sleep(0.1)

    def beep_in(self):
        self._buzz(0.15, 1)

    def beep_out(self):
        self._buzz(0.3, 1)

    def beep_unknown(self):
        self._buzz(0.1, 2)

    def beep_choose(self):
        self._buzz(0.1, 3)


def create_controls(config: dict) -> GpioControls:
    backend = config["hardware"]["scanner_backend"]
    if backend == "mock":
        return MockGpioControls()
    elif backend == "evdev":
        hw = config["hardware"]
        return PiGpioControls(
            toggle_pin=hw["gpio_toggle_pin"],
            buzzer_pin=hw["gpio_buzzer_pin"],
            led_green_pin=hw["gpio_led_green_pin"],
            led_red_pin=hw["gpio_led_red_pin"],
        )
    else:
        raise ValueError(f"Unknown scanner backend: {backend}")
