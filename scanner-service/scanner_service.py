#!/usr/bin/env python3
"""Wine Fridge Barcode Scanner Service.

Main entry point. Reads barcodes, checks the toggle switch, and manages
wine inventory via the Wine Cellar API. Sends notifications via Telegram.
"""

import logging
import threading
import time

import yaml

from hardware.barcode_reader import create_reader
from hardware.gpio_controls import ScanMode, create_controls
from integrations.grok_vision import (
    GrokVisionAPI, merge_wine_data, read_barcode_local, log_barcode_engines,
)
from integrations.open_food_facts import OpenFoodFactsAPI
from integrations.telegram_bot import TelegramBotHandler, TelegramNotifier
from integrations.wine_cellar_api import WineCellarAPI


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# Shared store for pending barcodes (awaiting photo enrichment).
# Protected by pending_lock for thread safety between scanner + Telegram threads.
pending_store: dict = {}
pending_lock = threading.Lock()


def do_stock_change(wine_id: int, mode: ScanMode, api: WineCellarAPI, gpio,
                    telegram: TelegramNotifier | None = None) -> dict:
    """Add or remove stock for a wine. Returns the API result dict.

    Handles beeps and console output. Telegram notification is left to the caller
    so it can choose between a plain notification or one with action buttons.
    """
    if mode == ScanMode.IN:
        result = api.add_stock(wine_id)
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            gpio.beep_unknown()
            if telegram:
                telegram.send_error(result["error"])
            return result
        print(f"  Added: {result['wine_name']}")
        print(f"  Now in stock: {result['stock_count']}")
        gpio.beep_in()
        return result
    else:
        result = api.remove_stock(wine_id)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            gpio.beep_unknown()
            if telegram:
                telegram.send_error(result["error"])
        else:
            print(f"  Removed: {result['wine_name']}")
            print(f"  Remaining: {result['stock_count']}")
            gpio.beep_out()
        return result


def handle_known_wine(wine: dict, barcode: str, mode: ScanMode, api: WineCellarAPI,
                      gpio, telegram: TelegramNotifier | None):
    """Handle a scan where we found exactly one matching wine.

    Immediately adds/removes stock — no confirmation needed for speed.
    Sends a notification with undo + "wrong wine" buttons as a safety net.
    """
    result = do_stock_change(wine["id"], mode, api, gpio)
    if "error" in result:
        if telegram:
            telegram.send_error(result["error"])
        return
    if telegram:
        telegram.send_stock_update_with_actions(
            wine_name=result["wine_name"],
            wine_id=wine["id"],
            barcode=barcode,
            action=mode.value,
            stock_count=result["stock_count"],
            storage=result.get("storage") or "",
            item_id=result.get("item_id"),
            drawers=result.get("drawers") or 0,
            row=result.get("row"),
            storage_id=result.get("storage_id"),
        )


def handle_ambiguous(wines: list[dict], barcode: str, mode: ScanMode, api: WineCellarAPI,
                     gpio, telegram: TelegramNotifier | None):
    """Handle a scan where multiple wines share the same barcode."""
    print(f"  Multiple wines found ({len(wines)}):")
    for i, wine in enumerate(wines, 1):
        print(f"    {i}. {wine['name']} {wine.get('vintage', '')} (stock: {wine['stock_count']})")

    gpio.beep_choose()

    if telegram:
        telegram.send_disambiguation(
            f"🍷 <b>{'Adding' if mode == ScanMode.IN else 'Removing'} — which wine?</b>",
            wines,
            mode.value,
            barcode=barcode,
        )
        print("  → Sent to Telegram for selection")
    else:
        # Fallback: prompt locally in mock mode
        while True:
            try:
                choice = input("  Select wine number: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(wines):
                    do_stock_change(wines[idx]["id"], mode, api, gpio, telegram)
                    return
                print("  Invalid selection, try again.")
            except ValueError:
                print("  Enter a number.")


def handle_unknown(barcode: str, off: OpenFoodFactsAPI, api: WineCellarAPI, gpio, telegram: TelegramNotifier | None):
    """Handle a scan where the barcode is not in our database."""
    print(f"  Unknown barcode: {barcode}")

    # Check if this barcode is already pending
    with pending_lock:
        existing = pending_store.get(barcode)
        if existing and not existing.get("resolved"):
            age = time.time() - existing.get("time", 0)
            if age < 600:  # still fresh (10 min)
                print(f"  Already pending ({int(age)}s ago)")
                gpio.beep_unknown()
                if telegram:
                    telegram.send(
                        f"Barcode <code>{barcode}</code> is already pending.\n"
                        f"Reply to the earlier message with a label photo, "
                        f"or use the buttons there.",
                    )
                return

    print("  Looking up in Open Food Facts...")
    off_data = off.lookup(barcode)

    if off_data and off_data.get("name"):
        print(f"  OFF found: {off_data['name']}")

    # Store as pending for photo enrichment
    with pending_lock:
        pending_store[barcode] = {"off_data": off_data, "time": time.time()}

    if telegram:
        # Send prompt to Telegram — user can add via OFF data, send photo, or add manually
        telegram.send_new_wine_prompt(barcode, off_data)
        print("  → Sent to Telegram for new wine setup")
        gpio.beep_unknown()
        return

    # No Telegram — fall back to local prompts
    if off_data and off_data.get("name"):
        print(f"  Producer: {off_data.get('producer', 'Unknown')}")
        print(f"  Country: {off_data.get('country', 'Unknown')}")
        print(f"  ABV: {off_data.get('abv', 'Unknown')}")

        choice = input("\n  Add this wine? [Y/n]: ").strip().lower()
        if choice in ("", "y", "yes"):
            wine_data = {
                "name": off_data["name"],
                "producer": off_data.get("producer", ""),
                "country": off_data.get("country", "CH")[:2],
                "region": off_data.get("region", ""),
                "abv": off_data.get("abv"),
                "barcode": barcode,
                "wine_type": "RE",
            }
            result = api.create_wine(wine_data)
            print(f"  Wine created: {result['name']} (ID: {result['id']})")
            stock = api.add_stock(result["id"])
            if stock.get("error"):
                print(f"  ERROR: {stock['error']}")
                gpio.beep_unknown()
                return
            print(f"  Added to stock. Count: {stock['stock_count']}")
            gpio.beep_in()
            with pending_lock:
                pending_store.pop(barcode, None)
            return
    else:
        print("  Not found in Open Food Facts.")

    # Manual entry fallback
    print("\n  Manual entry:")
    name = input("  Wine name: ").strip()
    if not name:
        print("  Skipped. Barcode logged for later.")
        gpio.beep_unknown()
        return

    wine_type = input("  Type [RE/WH/RO/SP/DE/FO/OR] (default RE): ").strip().upper() or "RE"
    vintage = input("  Vintage (optional): ").strip()
    country = input("  Country code (default CH): ").strip().upper() or "CH"

    wine_data = {
        "name": name,
        "wine_type": wine_type,
        "vintage": int(vintage) if vintage else None,
        "country": country[:2],
        "barcode": barcode,
    }
    result = api.create_wine(wine_data)
    print(f"  Wine created: {result['name']} (ID: {result['id']})")
    stock = api.add_stock(result["id"])
    if stock.get("error"):
        print(f"  ERROR: {stock['error']}")
        gpio.beep_unknown()
        return
    print(f"  Added to stock. Count: {stock['stock_count']}")
    gpio.beep_in()
    pending_store.pop(barcode, None)


def _make_photo_handler(grok, off, store):
    """Create a closure that handles label photos from Telegram.

    Returns merged wine data dict for the bot to show a confirmation step,
    or None if Grok extraction failed.
    """

    def handle_photo(barcode: str, photo_bytes: bytes) -> dict | None:
        off_data = store.get(barcode, {}).get("off_data")

        print(f"  Analysing label photo for {barcode}...")
        grok_data = grok.analyse_label(photo_bytes)
        if not grok_data:
            print(f"  Grok extraction failed for {barcode}")
            return None

        merged = merge_wine_data(off_data, grok_data)
        print(f"  Grok extracted: {merged.get('name', 'Unknown')}")
        return merged

    return handle_photo


def main():
    config = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log_barcode_engines()

    # Init components
    reader = create_reader(config)
    gpio = create_controls(config)
    api = WineCellarAPI(
        base_url=config["wine_cellar"]["base_url"],
        api_token=config["wine_cellar"]["api_token"],
        default_storage=config["storage"]["default_storage_name"],
        web_url=config["wine_cellar"].get("web_url", ""),
    )
    off = OpenFoodFactsAPI()

    # Init Grok vision if configured
    grok = None
    grok_key = config.get("grok", {}).get("api_key")
    if grok_key:
        grok = GrokVisionAPI(api_key=grok_key)
        print("  Grok vision: ACTIVE")
    else:
        print("  Grok vision: DISABLED (no API key)")

    # Init Telegram if configured
    telegram = None
    bot_handler = None
    tg_config = config.get("telegram", {})
    chat_ids = tg_config.get("chat_ids", [])
    # Support legacy single chat_id config
    if not chat_ids and tg_config.get("chat_id"):
        chat_ids = [tg_config["chat_id"]]
    if tg_config.get("bot_token") and chat_ids:
        telegram = TelegramNotifier(tg_config["bot_token"], chat_ids)
        bot_handler = TelegramBotHandler(
            bot_token=tg_config["bot_token"],
            chat_ids=chat_ids,
            wine_cellar_api=api,
            pending_store=pending_store,
            pending_lock=pending_lock,
            admin_chat_ids=chat_ids,
        )
        bot_handler.set_local_barcode_reader(read_barcode_local)
        if grok:
            bot_handler.set_photo_callback(
                _make_photo_handler(grok, off, pending_store)
            )
            bot_handler.set_triage_callback(grok.triage_photo)
        bot_handler.run_in_thread()
        print(f"  Telegram bot: ACTIVE ({len(chat_ids)} user(s))")
    else:
        print("  Telegram bot: DISABLED (no token/chat_ids)")

    debounce = config["scanner"]["debounce_seconds"]
    last_scan_time = 0

    print("=" * 50)
    print("  Wine Fridge Scanner Service")
    print("=" * 50)
    print(f"  Mode: {gpio.read_mode().value.upper()}")
    print(f"  API: {config['wine_cellar']['base_url']}")
    print(f"  Storage: {config['storage']['default_storage_name']}")

    if hasattr(gpio, "toggle"):
        print("\n  Tip: Type 'm' to toggle IN/OUT mode")

    print()

    # Send startup notification
    if telegram:
        telegram.send(
            f"🟢 <b>Wine Scanner started</b>\n"
            f"Storage: {config['storage']['default_storage_name']}\n"
            f"Mode: {gpio.read_mode().value.upper()}"
        )

    try:
        while True:
            barcode = reader.wait_for_scan()

            # Handle mode toggle command in mock mode
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

            # Look up barcode
            try:
                wines = api.lookup_barcode(barcode)
            except Exception as e:
                print(f"  ERROR connecting to Wine Cellar: {e}")
                gpio.beep_unknown()
                if telegram:
                    telegram.send_error(f"Wine Cellar API error: {e}")
                continue

            if len(wines) == 0:
                if mode == ScanMode.OUT:
                    print("  Unknown barcode — can't remove what we don't have.")
                    gpio.beep_unknown()
                    if telegram:
                        telegram.send_error(
                            f"Unknown barcode <code>{barcode}</code> — "
                            f"can't remove a wine that isn't in the cellar."
                        )
                    continue
                handle_unknown(barcode, off, api, gpio, telegram)
            elif len(wines) == 1:
                handle_known_wine(wines[0], barcode, mode, api, gpio, telegram)
            else:
                handle_ambiguous(wines, barcode, mode, api, gpio, telegram)

    except KeyboardInterrupt:
        if telegram:
            telegram.send("🔴 <b>Wine Scanner stopped</b>")
        print("\n\nScanner service stopped.")


if __name__ == "__main__":
    main()
