#!/usr/bin/env python3
"""Wine Scanner Server — FastAPI service that processes barcode scans.

Runs on the NAS (or cloud). Handles all logic: Wine Cellar API, Telegram,
Grok vision, Open Food Facts. The thin scanner client just sends
{barcode, mode} via HTTP.
"""

import logging
import threading
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from integrations.grok_vision import GrokVisionAPI, merge_wine_data
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


# --- Shared state ---
pending_store: dict = {}
pending_lock = threading.Lock()

# Globals initialised in lifespan
api: WineCellarAPI = None  # type: ignore
off: OpenFoodFactsAPI = None  # type: ignore
grok: GrokVisionAPI | None = None
telegram: TelegramNotifier | None = None
bot_handler: TelegramBotHandler | None = None
config: dict = {}


class ScanRequest(BaseModel):
    barcode: str
    mode: str = "in"  # "in" or "out"


class ScanResponse(BaseModel):
    status: str  # "ok", "added", "removed", "ambiguous", "unknown", "error"
    beep: str  # "in", "out", "unknown", "choose"
    message: str = ""
    wine_name: str = ""
    stock_count: int | None = None


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global api, off, grok, telegram, bot_handler, config

    config = load_config()

    api = WineCellarAPI(
        base_url=config["wine_cellar"]["base_url"],
        api_token=config["wine_cellar"]["api_token"],
        default_storage=config["storage"]["default_storage_name"],
        web_url=config["wine_cellar"].get("web_url", ""),
    )
    off = OpenFoodFactsAPI()

    grok_key = config.get("grok", {}).get("api_key")
    if grok_key:
        grok = GrokVisionAPI(api_key=grok_key)
        logger.info("Grok vision: ACTIVE")
    else:
        logger.info("Grok vision: DISABLED (no API key)")

    tg_config = config.get("telegram", {})
    chat_ids = tg_config.get("chat_ids", [])
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
        )
        if grok:
            bot_handler.set_photo_callback(
                _make_photo_handler(grok, off, pending_store)
            )
            bot_handler.set_barcode_callback(grok.read_barcode)
        bot_handler.run_in_thread()
        logger.info(f"Telegram bot: ACTIVE ({len(chat_ids)} user(s))")

        telegram.send(
            f"🟢 <b>Wine Scanner Server started</b>\n"
            f"Storage: {config['storage']['default_storage_name']}"
        )
    else:
        logger.info("Telegram bot: DISABLED (no token/chat_ids)")

    logger.info("Wine Scanner Server ready")
    yield

    # Shutdown
    if telegram:
        telegram.send("🔴 <b>Wine Scanner Server stopped</b>")
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

    logger.info(f"Scan: {barcode} | Mode: {mode.upper()}")

    # Look up barcode
    try:
        wines = api.lookup_barcode(barcode)
    except Exception as e:
        logger.error(f"Wine Cellar API error: {e}")
        if telegram:
            telegram.send_error(f"Wine Cellar API error: {e}")
        return ScanResponse(
            status="error", beep="unknown",
            message=f"Wine Cellar API error: {e}",
        )

    # --- Unknown barcode ---
    if len(wines) == 0:
        if mode == "out":
            msg = f"Unknown barcode {barcode} — can't remove what we don't have."
            if telegram:
                telegram.send_error(
                    f"Unknown barcode <code>{barcode}</code> — "
                    f"can't remove a wine that isn't in the cellar."
                )
            return ScanResponse(status="unknown", beep="unknown", message=msg)

        return _handle_unknown(barcode)

    # --- Single match ---
    if len(wines) == 1:
        return _handle_known(wines[0], barcode, mode)

    # --- Multiple matches ---
    return _handle_ambiguous(wines, barcode, mode)


def _handle_known(wine: dict, barcode: str, mode: str) -> ScanResponse:
    wine_id = wine["id"]

    if mode == "in":
        result = api.add_stock(wine_id)
        if telegram:
            telegram.send_stock_update_with_actions(
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
            if telegram:
                telegram.send_error(result["error"])
            return ScanResponse(
                status="error", beep="unknown",
                message=result["error"],
            )
        if telegram:
            telegram.send_stock_update_with_actions(
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


def _handle_ambiguous(wines: list[dict], barcode: str, mode: str) -> ScanResponse:
    if telegram:
        telegram.send_disambiguation(
            f"🍷 <b>{'Adding' if mode == 'in' else 'Removing'} — which wine?</b>",
            wines,
            mode,
            barcode=barcode,
        )
    names = [f"{w['name']} {w.get('vintage', '')}" for w in wines]
    return ScanResponse(
        status="ambiguous", beep="choose",
        message=f"Multiple wines: {', '.join(names)}. Check Telegram.",
    )


def _handle_unknown(barcode: str) -> ScanResponse:
    # Check if already pending
    with pending_lock:
        existing = pending_store.get(barcode)
        if existing and not existing.get("resolved"):
            age = time.time() - existing.get("time", 0)
            if age < 600:
                if telegram:
                    telegram.send(
                        f"Barcode <code>{barcode}</code> is already pending.\n"
                        f"Reply to the earlier message with a label photo, "
                        f"or use the buttons there.",
                    )
                return ScanResponse(
                    status="unknown", beep="unknown",
                    message="Already pending — check Telegram.",
                )

    logger.info(f"Looking up {barcode} in Open Food Facts...")
    off_data = off.lookup(barcode)
    if off_data and off_data.get("name"):
        logger.info(f"OFF found: {off_data['name']}")

    with pending_lock:
        pending_store[barcode] = {"off_data": off_data, "time": time.time()}

    if telegram:
        telegram.send_new_wine_prompt(barcode, off_data)

    return ScanResponse(
        status="unknown", beep="unknown",
        message=f"Unknown barcode {barcode}. Check Telegram.",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
