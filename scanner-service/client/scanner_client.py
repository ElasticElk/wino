#!/usr/bin/env python3
"""Thin Wine Scanner Client — runs on the Pi at the fridge.

Reads barcodes from the USB scanner, reads the IN/OUT toggle switch,
and POSTs to the remote scanner server. Beeps based on the response.

All intelligence lives on the server. This client is ~50 lines of real logic.
"""

import sys
import time

import requests
import yaml

sys.path.insert(0, "..")
from hardware.barcode_reader import create_reader
from hardware.gpio_controls import create_controls


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    server_url = config["server"]["url"].rstrip("/")

    reader = create_reader(config)
    gpio = create_controls(config)

    debounce = config.get("scanner", {}).get("debounce_seconds", 2)
    last_scan_time = 0
    timeout = config.get("server", {}).get("timeout", 10)

    print("=" * 50)
    print("  Wine Scanner Client")
    print("=" * 50)
    print(f"  Server: {server_url}")
    print(f"  Mode: {gpio.read_mode().value.upper()}")

    if hasattr(gpio, "toggle"):
        print("\n  Tip: Type 'm' to toggle IN/OUT mode")

    print()

    try:
        while True:
            barcode = reader.wait_for_scan()

            # Handle mode toggle in mock mode
            if barcode == "m" and hasattr(gpio, "toggle"):
                gpio.toggle()
                continue

            # Debounce
            now = time.time()
            if now - last_scan_time < debounce:
                print("  (debounce — scan ignored)")
                continue
            last_scan_time = now

            mode = gpio.read_mode()
            print(f"\n  Scanned: {barcode}  |  Mode: {mode.value.upper()}")

            try:
                resp = requests.post(
                    f"{server_url}/scan",
                    json={"barcode": barcode, "mode": mode.value},
                    timeout=timeout,
                )
                resp.raise_for_status()
                result = resp.json()

                beep = result.get("beep", "unknown")
                status = result.get("status", "")
                wine_name = result.get("wine_name", "")
                stock = result.get("stock_count")
                msg = result.get("message", "")

                if wine_name:
                    print(f"  {status}: {wine_name} (stock: {stock})")
                elif msg:
                    print(f"  {status}: {msg}")

                # Beep based on server response
                if beep == "in":
                    gpio.beep_in()
                elif beep == "out":
                    gpio.beep_out()
                elif beep == "choose":
                    gpio.beep_choose()
                else:
                    gpio.beep_unknown()

            except requests.ConnectionError:
                print("  ERROR: Cannot reach scanner server!")
                gpio.beep_unknown()
                gpio.beep_unknown()  # double beep = server down
            except requests.Timeout:
                print("  ERROR: Server timed out!")
                gpio.beep_unknown()
            except Exception as e:
                print(f"  ERROR: {e}")
                gpio.beep_unknown()

    except KeyboardInterrupt:
        print("\n\nScanner client stopped.")


if __name__ == "__main__":
    main()
