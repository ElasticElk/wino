#!/usr/bin/env python3
"""Wine Scanner Server -- FastAPI service that processes barcode scans.

Runs on the NAS (or cloud). Handles all logic: Wine Cellar API, Telegram,
Grok vision, Open Food Facts. The thin scanner client just sends
{barcode, mode, api_key} via HTTP.

Config is loaded from the Wine Cellar DB via /api/scanner/config/.
Only bootstrap settings (base_url, admin token) come from config.yaml.
"""

import logging
import threading
import time
from contextlib import asynccontextmanager

import requests
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from integrations.grok_vision import GrokVisionAPI, merge_wine_data, read_barcode_local
from integrations.open_food_facts import OpenFoodFactsAPI
from integrations.telegram_bot import TelegramBotHandler, TelegramNotifier
from integrations.wine_cellar_api import WineCellarAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def fetch_db_config(base_url: str, admin_token: str) -> dict:
    """Fetch scanner config from the Wine Cellar DB via the API."""
    resp = requests.get(
        f"{base_url.rstrip('/')}/api/scanner/config/",
        headers={"Authorization": f"Token {admin_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# --- Shared state ---
pending_store: dict = {}
pending_lock = threading.Lock()

# Globals initialised in lifespan
scanner_registry: dict[str, dict] = {}  # api_key -> scanner config
api_clients: dict[str, WineCellarAPI] = {}  # unique key -> WineCellarAPI instance
chat_id_to_api: dict[str, WineCellarAPI] = {}  # chat_id -> WineCellarAPI for that user's default household
notifiers: dict[str, TelegramNotifier] = {}  # api_token -> TelegramNotifier
off: OpenFoodFactsAPI = None  # type: ignore
grok: GrokVisionAPI | None = None
bot_handler: TelegramBotHandler | None = None
db_config: dict = {}
file_config: dict = {}


class ScanRequest(BaseModel):
    barcode: str
    mode: str = "in"  # "in" or "out"
    api_key: str = ""  # identifies which scanner device


class ScanResponse(BaseModel):
    status: str  # "ok", "added", "removed", "ambiguous", "unknown", "error"
    beep: str  # "in", "out", "unknown", "choose"
    message: str = ""
    wine_name: str = ""
    stock_count: int | None = None


def _get_scanner_context(api_key: str) -> tuple[WineCellarAPI, TelegramNotifier | None, dict]:
    """Look up the WineCellarAPI client and notifier for a scanner's api_key."""
    scanner = scanner_registry.get(api_key)
    if not scanner:
        raise HTTPException(status_code=403, detail="Unknown scanner api_key")

    api_token = scanner["api_token"]
    household_id = scanner.get("household_id")

    # Use household-specific API client
    client_key = f"{api_token}:{household_id}" if household_id else api_token
    api = api_clients.get(client_key)
    if not api:
        raise HTTPException(status_code=500, detail="No API client for this scanner")

    notifier = notifiers.get(api_token)
    return api, notifier, scanner


def _make_photo_handler(grok_api, off_api, store):
    def handle_photo(barcode: str, photo_bytes: bytes) -> dict | None:
        logger.info(f"Analysing label photo for {barcode}...")
        grok_data = grok_api.analyse_label(photo_bytes)
        if not grok_data:
            logger.warning(f"Grok extraction failed for {barcode}")
            return None
        off_data = store.get(barcode, {}).get("off_data")
        merged = merge_wine_data(off_data, grok_data)
        logger.info(f"Grok extracted: {merged.get('name', 'Unknown')}")
        return merged
    return handle_photo


def _build_notifier(bot_token: str, chat_ids: list[str]) -> TelegramNotifier | None:
    if bot_token and chat_ids:
        return TelegramNotifier(bot_token, chat_ids)
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global off, grok, bot_handler, db_config, file_config
    global scanner_registry, api_clients, notifiers, chat_id_to_api

    file_config = load_config()
    base_url = file_config["wine_cellar"]["base_url"]
    admin_token = file_config["wine_cellar"]["api_token"]

    # Fetch config from DB
    try:
        db_config = fetch_db_config(base_url, admin_token)
        logger.info("Loaded config from Wine Cellar DB")
    except Exception as e:
        logger.error(f"Failed to fetch DB config: {e}")
        logger.info("Falling back to config.yaml only")
        db_config = {"config": {}, "telegram_users": [], "scanners": []}

    svc = db_config.get("config", {})
    web_url = svc.get("web_url", file_config["wine_cellar"].get("web_url", ""))
    bot_token = svc.get("bot_token", file_config.get("telegram", {}).get("bot_token", ""))

    off = OpenFoodFactsAPI()

    # Grok vision
    grok_key = svc.get("grok_api_key", file_config.get("grok", {}).get("api_key", ""))
    if grok_key:
        grok = GrokVisionAPI(api_key=grok_key)
        logger.info("Grok vision: ACTIVE")
    else:
        logger.info("Grok vision: DISABLED (no API key)")

    # Build per-user/household API clients from telegram_users
    all_chat_ids = []
    token_to_chat_ids: dict[str, list[str]] = {}

    for tu in db_config.get("telegram_users", []):
        api_token = tu["api_token"]
        chat_id = tu["chat_id"]
        household_id = tu.get("household_id")
        all_chat_ids.append(chat_id)

        # Build a household-specific API client for this user
        client_key = f"{api_token}:{household_id}" if household_id else api_token
        if client_key not in api_clients:
            api_clients[client_key] = WineCellarAPI(
                base_url=base_url,
                api_token=api_token,
                default_storage=svc.get(
                    "default_storage",
                    file_config.get("storage", {}).get("default_storage_name", "Wine Fridge"),
                ),
                web_url=web_url,
                household_id=household_id,
            )

        # Map chat_id -> API client for Telegram bot routing
        chat_id_to_api[chat_id] = api_clients[client_key]

        token_to_chat_ids.setdefault(api_token, []).append(chat_id)

    # Build per-token notifiers
    for api_token, chat_ids in token_to_chat_ids.items():
        notifiers[api_token] = TelegramNotifier(bot_token, chat_ids)

    # Build scanner registry
    for sc in db_config.get("scanners", []):
        scanner_registry[sc["api_key"]] = sc
        api_token = sc["api_token"]
        household_id = sc.get("household_id")

        # Ensure API client exists for this scanner's household
        client_key = f"{api_token}:{household_id}" if household_id else api_token
        if client_key not in api_clients:
            api_clients[client_key] = WineCellarAPI(
                base_url=base_url,
                api_token=api_token,
                default_storage=sc.get("default_storage", "Wine Fridge"),
                web_url=web_url,
                household_id=household_id,
            )

        # Build a notifier for this scanner's notify list
        if bot_token and sc.get("notify_chat_ids"):
            notifiers[api_token] = TelegramNotifier(bot_token, sc["notify_chat_ids"])

    # Fallback: if no DB config, use config.yaml values directly
    if not scanner_registry and not api_clients:
        logger.info("No scanners/users in DB, using config.yaml fallback")
        fallback_token = admin_token
        fallback_storage = file_config.get("storage", {}).get("default_storage_name", "Wine Fridge")
        api_clients[fallback_token] = WineCellarAPI(
            base_url=base_url,
            api_token=fallback_token,
            default_storage=fallback_storage,
            web_url=web_url,
        )
        yaml_chat_ids = file_config.get("telegram", {}).get("chat_ids", [])
        if bot_token and yaml_chat_ids:
            notifiers[fallback_token] = TelegramNotifier(bot_token, yaml_chat_ids)
            all_chat_ids = yaml_chat_ids
            for cid in yaml_chat_ids:
                chat_id_to_api[str(cid)] = api_clients[fallback_token]
        # Register a synthetic scanner so the fallback works
        scanner_registry["_fallback"] = {
            "api_token": fallback_token,
            "default_storage": fallback_storage,
            "notify_chat_ids": yaml_chat_ids,
        }

    # Telegram bot handler — per-chat_id routing via chat_id_to_api
    if bot_token and all_chat_ids:
        # Primary API (first available) as fallback
        primary_api = next(iter(api_clients.values()))
        bot_handler = TelegramBotHandler(
            bot_token=bot_token,
            chat_ids=all_chat_ids,
            wine_cellar_api=primary_api,
            pending_store=pending_store,
            pending_lock=pending_lock,
            chat_id_to_api=chat_id_to_api,
        )
        bot_handler.set_local_barcode_reader(read_barcode_local)

        def _api_factory(chat_id: str, household_id: int):
            """Build a new WineCellarAPI for a freshly added user."""
            # Re-fetch config to get the new user's token
            fresh = fetch_db_config(base_url, admin_token)
            tu_info = next((t for t in fresh.get("telegram_users", [])
                            if str(t["chat_id"]) == str(chat_id)), None)
            if not tu_info:
                return None
            client = WineCellarAPI(
                base_url=base_url,
                api_token=tu_info["api_token"],
                default_storage=svc.get("default_storage", "Wine Fridge"),
                web_url=web_url,
                household_id=household_id,
            )
            # Also register in the global maps
            client_key = f"{tu_info['api_token']}:{household_id}"
            api_clients[client_key] = client
            chat_id_to_api[str(chat_id)] = client
            return client

        bot_handler.set_api_factory(_api_factory)
        if grok:
            bot_handler.set_photo_callback(
                _make_photo_handler(grok, off, pending_store)
            )
            bot_handler.set_triage_callback(grok.triage_photo)
        bot_handler.run_in_thread()
        logger.info(f"Telegram bot: ACTIVE ({len(all_chat_ids)} user(s))")

        # Notify all users
        broadcast = TelegramNotifier(bot_token, all_chat_ids)
        broadcast.send("\U0001F7E2 <b>Wine Scanner Server started</b>")
    else:
        logger.info("Telegram bot: DISABLED (no token/chat_ids)")

    logger.info(
        f"Wine Scanner Server ready — "
        f"{len(scanner_registry)} scanner(s), "
        f"{len(all_chat_ids)} user(s)"
    )
    yield

    # Shutdown
    if bot_token and all_chat_ids:
        try:
            broadcast = TelegramNotifier(bot_token, all_chat_ids)
            broadcast.send("\U0001F534 <b>Wine Scanner Server stopped</b>")
        except Exception:
            pass
    logger.info("Wine Scanner Server stopped")


app = FastAPI(title="Wine Scanner Server", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/scan", response_model=ScanResponse)
async def handle_scan(req: ScanRequest):
    barcode = req.barcode.strip()
    mode = req.mode.strip().lower()

    if not barcode:
        raise HTTPException(status_code=400, detail="barcode is required")
    if mode not in ("in", "out"):
        raise HTTPException(status_code=400, detail="mode must be 'in' or 'out'")

    # Resolve scanner context
    api_key = req.api_key.strip() or "_fallback"
    api, notifier, scanner = _get_scanner_context(api_key)

    logger.info(f"Scan: {barcode} | Mode: {mode.upper()} | Scanner: {scanner.get('name', api_key)}")

    # Look up barcode
    try:
        wines = api.lookup_barcode(barcode)
    except Exception as e:
        logger.error(f"Wine Cellar API error: {e}")
        if notifier:
            notifier.send_error(f"Wine Cellar API error: {e}")
        return ScanResponse(
            status="error", beep="unknown",
            message=f"Wine Cellar API error: {e}",
        )

    # --- Unknown barcode ---
    if len(wines) == 0:
        if mode == "out":
            msg = f"Unknown barcode {barcode} -- can't remove what we don't have."
            if notifier:
                notifier.send_error(
                    f"Unknown barcode <code>{barcode}</code> -- "
                    f"can't remove a wine that isn't in the cellar."
                )
            return ScanResponse(status="unknown", beep="unknown", message=msg)

        return _handle_unknown(barcode, api, notifier)

    # --- Single match ---
    if len(wines) == 1:
        return _handle_known(wines[0], barcode, mode, api, notifier)

    # --- Multiple matches ---
    return _handle_ambiguous(wines, barcode, mode, notifier)


def _handle_known(wine: dict, barcode: str, mode: str,
                  api: WineCellarAPI, notifier: TelegramNotifier | None) -> ScanResponse:
    wine_id = wine["id"]

    if mode == "in":
        result = api.add_stock(wine_id)
        if notifier:
            notifier.send_stock_update_with_actions(
                wine_name=result["wine_name"],
                wine_id=wine_id,
                barcode=barcode,
                action="in",
                stock_count=result["stock_count"],
            )
        return ScanResponse(
            status="added", beep="in",
            wine_name=result["wine_name"],
            stock_count=result["stock_count"],
        )
    else:
        result = api.remove_stock(wine_id)
        if "error" in result:
            if notifier:
                notifier.send_error(result["error"])
            return ScanResponse(
                status="error", beep="unknown",
                message=result["error"],
            )
        if notifier:
            notifier.send_stock_update_with_actions(
                wine_name=result["wine_name"],
                wine_id=wine_id,
                barcode=barcode,
                action="out",
                stock_count=result["stock_count"],
            )
        return ScanResponse(
            status="removed", beep="out",
            wine_name=result["wine_name"],
            stock_count=result["stock_count"],
        )


def _handle_ambiguous(wines: list[dict], barcode: str, mode: str,
                      notifier: TelegramNotifier | None) -> ScanResponse:
    if notifier:
        notifier.send_disambiguation(
            f"\U0001F377 <b>{'Adding' if mode == 'in' else 'Removing'} -- which wine?</b>",
            wines,
            mode,
            barcode=barcode,
        )
    names = [f"{w['name']} {w.get('vintage', '')}" for w in wines]
    return ScanResponse(
        status="ambiguous", beep="choose",
        message=f"Multiple wines: {', '.join(names)}. Check Telegram.",
    )


def _handle_unknown(barcode: str, api: WineCellarAPI,
                    notifier: TelegramNotifier | None) -> ScanResponse:
    # Check if already pending
    with pending_lock:
        existing = pending_store.get(barcode)
        if existing and not existing.get("resolved"):
            age = time.time() - existing.get("time", 0)
            if age < 600:
                if notifier:
                    notifier.send(
                        f"Barcode <code>{barcode}</code> is already pending.\n"
                        f"Reply to the earlier message with a label photo, "
                        f"or use the buttons there.",
                    )
                return ScanResponse(
                    status="unknown", beep="unknown",
                    message="Already pending -- check Telegram.",
                )

    logger.info(f"Looking up {barcode} in Open Food Facts...")
    off_data = off.lookup(barcode)
    if off_data and off_data.get("name"):
        logger.info(f"OFF found: {off_data['name']}")

    with pending_lock:
        pending_store[barcode] = {"off_data": off_data, "time": time.time()}

    if notifier:
        notifier.send_new_wine_prompt(barcode, off_data)

    return ScanResponse(
        status="unknown", beep="unknown",
        message=f"Unknown barcode {barcode}. Check Telegram.",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
