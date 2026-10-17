#!/usr/bin/env python3
"""Thin Wine Scanner Client — runs on the Pi at the fridge.

Reads barcodes from the USB scanner, reads the IN/OUT toggle switch,
and POSTs to the remote scanner server. Beeps based on the response.

All intelligence lives on the server. This client is the hardware glue.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hardware.barcode_reader import create_reader  # noqa: E402
from hardware.gpio_controls import create_controls  # noqa: E402


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def ping(server_url: str, timeout: float = 5) -> dict:
    """Hit the scanner server's /health. Raises on network/HTTP errors."""
    resp = requests.get(f"{server_url.rstrip('/')}/health", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def post_scan(
    server_url: str,
    api_key: str,
    barcode: str,
    mode: str,
    timeout: float = 10,
) -> dict:
    """POST one scan. Returns the server JSON. Raises for transport errors."""
    resp = requests.post(
        f"{server_url.rstrip('/')}/scan",
        json={"barcode": barcode, "mode": mode, "api_key": api_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def apply_beep(gpio, beep: str) -> None:
    if beep == "in":
        gpio.beep_in()
    elif beep == "out":
        gpio.beep_out()
    elif beep == "choose":
        gpio.beep_choose()
    else:
        gpio.beep_unknown()


def report_result(result: dict) -> None:
    status = result.get("status", "")
    wine_name = result.get("wine_name", "")
    stock = result.get("stock_count")
    msg = result.get("message", "")
    if wine_name:
        print(f"  {status}: {wine_name} (stock: {stock})")
    elif msg:
        print(f"  {status}: {msg}")
    else:
        print(f"  {status}")


def handle_scan(server_url: str, api_key: str, barcode: str, mode: str,
                gpio, timeout: float) -> None:
    print(f"\n  Scanned: {barcode}  |  Mode: {mode.upper()}")
    try:
        result = post_scan(server_url, api_key, barcode, mode, timeout=timeout)
        report_result(result)
        apply_beep(gpio, result.get("beep", "unknown"))
    except requests.ConnectionError:
        print("  ERROR: Cannot reach scanner server!")
        gpio.beep_unknown()
        gpio.beep_unknown()
    except requests.Timeout:
        print("  ERROR: Server timed out!")
        gpio.beep_unknown()
    except Exception as e:
        print(f"  ERROR: {e}")
        gpio.beep_unknown()


def default_config_path() -> str:
    return str(Path(__file__).resolve().parent / "config.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wine fridge barcode scanner client")
    parser.add_argument("--config", default=default_config_path(),
                        help="Path to client config.yaml")
    parser.add_argument("--ping", action="store_true",
                        help="Check the scanner server and exit")
    parser.add_argument("--once", metavar="BARCODE",
                        help="Send one barcode and exit (no USB scanner needed)")
    parser.add_argument("--mode", choices=("in", "out"),
                        help="Override the toggle switch for --once")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    server_url = (
        os.environ.get("SCANNER_URL")
        or config["server"]["url"]
    ).rstrip("/")
    api_key = (
        os.environ.get("SCANNER_API_KEY")
        or config["server"].get("api_key", "")
    )
    timeout = config.get("server", {}).get("timeout", 10)

    if args.ping:
        try:
            body = ping(server_url, timeout=timeout)
        except Exception as e:
            print(f"Scanner server unreachable at {server_url}: {e}")
            return 1
        print(f"Scanner server ok at {server_url}: {body}")
        return 0

    gpio = create_controls(config)

    if args.once:
        mode = args.mode or gpio.read_mode().value
        handle_scan(server_url, api_key, args.once.strip(), mode, gpio, timeout)
        return 0

    reader = create_reader(config)
    debounce = config.get("scanner", {}).get("debounce_seconds", 2)
    last_scan_time = 0.0

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
            if barcode == "m" and hasattr(gpio, "toggle"):
                gpio.toggle()
                continue
            now = time.time()
            if now - last_scan_time < debounce:
                print("  (debounce — scan ignored)")
                continue
            last_scan_time = now
            mode = args.mode or gpio.read_mode().value
            handle_scan(server_url, api_key, barcode, mode, gpio, timeout)
    except KeyboardInterrupt:
        print("\n\nScanner client stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
