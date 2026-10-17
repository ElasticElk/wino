#!/usr/bin/env python3
"""Wine Scanner Server -- FastAPI service that processes barcode scans.

Runs on the NAS (or cloud). Handles all logic: Wine Cellar API, Telegram,
Grok vision, Open Food Facts. The thin scanner client just sends
{barcode, mode, api_key} via HTTP.

Config is loaded from the Wine Cellar DB via /api/scanner/config/.
Only bootstrap settings (base_url, admin token) come from config.yaml.
"""

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from integrations.card_store import CardStore
from integrations.grok_vision import GrokVisionAPI, merge_wine_data, read_barcode_local
from integrations.open_food_facts import OpenFoodFactsAPI
from integrations.telegram_bot import TelegramBotHandler, TelegramNotifier, pending_key
from integrations.wine_cellar_api import WineCellarAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
# httpx logs the full Telegram URL, which includes the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Last successful /api/scanner/config/ payload. Survives a restart so we never
# fall back to yaml's operator-only chat list just because wine-cellar was down.
CONFIG_CACHE_PATH = os.environ.get(
    "SCANNER_CONFIG_CACHE", "/app/data/scanner_config_cache.json",
)
# 0 = off. A 60s poll was writing GET /api/scanner/config/ into the Django
# access log every minute. /adduser and /removeuser still hot-update the allow
# list; set SCANNER_CONFIG_RELOAD_SECONDS to restore a background refresh.
CONFIG_RELOAD_SECONDS = float(os.environ.get("SCANNER_CONFIG_RELOAD_SECONDS", "0"))


def config_reload_enabled(seconds: float | None = None) -> bool:
    value = CONFIG_RELOAD_SECONDS if seconds is None else seconds
    return value > 0


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


def fetch_db_config_with_retry(base_url: str, admin_token: str, attempts: int = 12, delay: float = 5.0) -> dict:
    """Retry config fetch so a brief web outage does not boot a yaml-only allow list.

    That fallback is how every household except the operator silently looks
    unprovisioned until the next restart.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            config = fetch_db_config(base_url, admin_token)
            if attempt > 1:
                logger.info(f"Loaded config from Wine Cellar DB on attempt {attempt}")
            else:
                logger.info("Loaded config from Wine Cellar DB")
            return config
        except Exception as e:
            last_error = e
            logger.warning(
                f"DB config fetch failed ({attempt}/{attempts}): {e}"
            )
            if attempt < attempts:
                time.sleep(delay)
    raise last_error


def load_cached_config(path: str | None = None) -> dict | None:
    cache_path = Path(path or CONFIG_CACHE_PATH)
    try:
        data = json.loads(cache_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_cached_config(config: dict, path: str | None = None) -> None:
    cache_path = Path(path or CONFIG_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config))
    tmp.replace(cache_path)


def resolve_runtime_config(live: dict | None, cached: dict | None) -> tuple[dict, str]:
    """Prefer live DB, then last-good cache. Yaml is last-resort only.

    An empty live payload must not wipe a known household list — that is how
    a restart during a web outage made every guest look unprovisioned.
    """
    live_users = (live or {}).get("telegram_users") or []
    cached_users = (cached or {}).get("telegram_users") or []
    if live is not None and live_users:
        return live, "db"
    if cached_users:
        return cached, "cache"
    if live is not None:
        return live, "db"
    return {"config": {}, "telegram_users": [], "scanners": []}, "yaml"


def load_runtime_config(base_url: str, admin_token: str, cache_path: str | None = None) -> tuple[dict, str]:
    """Boot-time config: live DB, else cache, else empty (yaml fallback later)."""
    live = None
    try:
        live = fetch_db_config_with_retry(base_url, admin_token)
    except Exception as e:
        logger.error(f"Failed to fetch DB config after retries: {e}")
    cached = load_cached_config(cache_path)
    config, source = resolve_runtime_config(live, cached)
    if source == "db":
        try:
            save_cached_config(config, cache_path)
        except OSError as e:
            logger.warning(f"Could not write config cache: {e}")
        logger.info(
            f"Using live Wine Cellar config ({len(config.get('telegram_users') or [])} users)"
        )
    elif source == "cache":
        logger.warning(
            f"Wine Cellar unreachable; using last-good config "
            f"({len(config.get('telegram_users') or [])} users). "
            f"Background refresh will retry."
        )
    else:
        logger.error(
            "No DB config and no cache — yaml fallback will only include "
            "operator chat IDs from config.yaml"
        )
    return config, source


# --- Shared state ---
# Replaced with a Redis CardStore in lifespan when REDIS_URL is set.
pending_store = CardStore(prefix="whiney:pending:")
pending_lock = threading.Lock()

# Globals initialised in lifespan
scanner_registry: dict[str, dict] = {}  # api_key -> scanner config
api_clients: dict[str, WineCellarAPI] = {}  # unique key -> WineCellarAPI instance
chat_id_to_api: dict[str, WineCellarAPI] = {}  # chat_id -> WineCellarAPI for that user's default household
chat_id_to_currency: dict[str, str] = {}  # chat_id -> ISO currency code (passed to Grok)
chat_id_to_country: dict[str, str] = {}  # chat_id -> ISO 2-letter country code (price-context grounding)
notifiers: dict[str, TelegramNotifier] = {}  # api_token -> TelegramNotifier
off: OpenFoodFactsAPI = None  # type: ignore
grok: GrokVisionAPI | None = None
bot_handler: TelegramBotHandler | None = None
db_config: dict = {}
file_config: dict = {}
config_source: str = "yaml"
config_reload_stop = threading.Event()
_bootstrap: dict = {}
_last_config_fp = None


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
    def handle_photo(barcode: str, photo_bytes: bytes, target_currency: str = "EUR",
                     target_country: str = "", off_data: dict | None = None) -> dict | None:
        # off_data is supplied by the caller. Reading it out of `store` by bare
        # barcode is no longer possible now that entries are keyed per household,
        # and the caller already holds the entry anyway.
        logger.info(f"Analysing label photo for {barcode} (currency={target_currency}, country={target_country or '-'})...")
        grok_data = grok_api.analyse_label(photo_bytes, target_currency=target_currency, target_country=target_country)
        if not grok_data:
            logger.warning(f"Grok extraction failed for {barcode}")
            return None
        merged = merge_wine_data(off_data, grok_data)
        logger.info(f"Grok extracted: {merged.get('name', 'Unknown')}")
        return merged
    return handle_photo


def _config_fingerprint(cfg: dict) -> tuple:
    """Stable identity of the allow-list / notifier mapping. Used to skip no-op reloads."""
    users = tuple(sorted(
        (
            str(u.get("chat_id", "")),
            str(u.get("api_token", "")),
            u.get("household_id"),
            bool(u.get("is_staff")),
        )
        for u in (cfg.get("telegram_users") or [])
    ))
    scanners = tuple(sorted(
        (
            str(sc.get("api_key", "")),
            str(sc.get("api_token", "")),
            sc.get("household_id"),
            tuple(str(c) for c in (sc.get("notify_chat_ids") or [])),
        )
        for sc in (cfg.get("scanners") or [])
    ))
    return (users, scanners)


def _chat_ids_key(chat_ids) -> tuple[str, ...]:
    return tuple(str(c) for c in (chat_ids or []))


def _replace_notifier(api_token: str, bot_token: str, chat_ids: list | None) -> None:
    """Create or reuse a notifier. Close the previous one if the chat list changed."""
    if not bot_token or not chat_ids:
        old = notifiers.pop(api_token, None)
        if old is not None:
            old.close()
        return
    wanted = _chat_ids_key(chat_ids)
    existing = notifiers.get(api_token)
    if existing is not None and _chat_ids_key(existing.chat_ids) == wanted:
        return
    notifiers[api_token] = TelegramNotifier(bot_token, list(wanted))
    if existing is not None:
        existing.close()


def _close_all_notifiers() -> None:
    for token in list(notifiers):
        n = notifiers.pop(token, None)
        if n is not None:
            n.close()


def _staff_notice(bot_token: str, admin_chat_ids: list, text: str) -> None:
    """One-shot staff ping. Always closed so it cannot leak a thread."""
    if not bot_token or not admin_chat_ids:
        return
    n = TelegramNotifier(bot_token, [str(c) for c in admin_chat_ids])
    try:
        n.send(text)
    except Exception:
        logger.warning("Staff notice failed", exc_info=True)
    finally:
        n.close()


def _register_telegram_user(tu: dict, *, default_storage: str, base_url: str, web_url: str) -> str:
    """Put one DB telegram user into the runtime maps. Returns chat_id as str."""
    api_token = tu["api_token"]
    chat_id = str(tu["chat_id"])
    household_id = tu.get("household_id")
    client_key = f"{api_token}:{household_id}" if household_id else api_token
    if client_key not in api_clients:
        api_clients[client_key] = WineCellarAPI(
            base_url=base_url,
            api_token=api_token,
            default_storage=default_storage,
            web_url=web_url,
            household_id=household_id,
        )
    chat_id_to_api[chat_id] = api_clients[client_key]
    chat_id_to_currency[chat_id] = tu.get("currency") or "EUR"
    chat_id_to_country[chat_id] = tu.get("country") or ""
    return chat_id


def _ingest_scanners(scanners: list[dict], *, bot_token: str, base_url: str, web_url: str) -> None:
    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()
    for sc in scanners:
        api_key = sc["api_key"]
        seen_keys.add(api_key)
        scanner_registry[api_key] = sc
        api_token = sc["api_token"]
        seen_tokens.add(api_token)
        household_id = sc.get("household_id")
        client_key = f"{api_token}:{household_id}" if household_id else api_token
        if client_key not in api_clients:
            api_clients[client_key] = WineCellarAPI(
                base_url=base_url,
                api_token=api_token,
                default_storage=sc.get("default_storage", "Wine Fridge"),
                web_url=web_url,
                household_id=household_id,
            )
        if bot_token:
            _replace_notifier(api_token, bot_token, sc.get("notify_chat_ids") or [])
    for key in list(scanner_registry):
        if key not in seen_keys:
            scanner_registry.pop(key, None)
    for token in list(notifiers):
        if token not in seen_tokens:
            _replace_notifier(token, bot_token, [])


def _reload_db_config_once() -> None:
    """Pull live config and expand/shrink the running allow list. Never yaml."""
    global db_config, config_source, _last_config_fp
    base_url = _bootstrap.get("base_url")
    admin_token = _bootstrap.get("admin_token")
    if not base_url or not admin_token:
        return
    try:
        live = fetch_db_config(base_url, admin_token)
    except Exception as e:
        logger.warning(f"Background config refresh failed: {e}")
        return
    users = live.get("telegram_users") or []
    if not users:
        logger.warning("Background config refresh returned no users; leaving allow list unchanged")
        return
    fp = _config_fingerprint(live)
    if fp == _last_config_fp and config_source == "db":
        return
    db_config = live
    config_source = "db"
    _last_config_fp = fp
    try:
        save_cached_config(live)
    except OSError as e:
        logger.warning(f"Could not write config cache: {e}")

    default_storage = (live.get("config") or {}).get(
        "default_storage",
        file_config.get("storage", {}).get("default_storage_name", "Wine Fridge"),
    )
    web_url = _bootstrap.get("web_url", "")
    bot_token = _bootstrap.get("bot_token", "")
    fresh_ids: set[str] = set()
    for tu in users:
        chat_id = _register_telegram_user(
            tu, default_storage=default_storage, base_url=base_url, web_url=web_url,
        )
        fresh_ids.add(chat_id)
        if bot_handler:
            bot_handler.add_chat_id(chat_id, api_client=chat_id_to_api.get(chat_id))
            if tu.get("is_staff"):
                bot_handler.add_admin_chat_id(chat_id)
            bot_handler.set_currency(chat_id, chat_id_to_currency.get(chat_id, "EUR"))
            bot_handler.set_country(chat_id, chat_id_to_country.get(chat_id, ""))
    if bot_handler:
        for cid in list(bot_handler.chat_ids):
            if cid not in fresh_ids:
                bot_handler.remove_chat_id(cid)
    _ingest_scanners(
        live.get("scanners") or [],
        bot_token=bot_token, base_url=base_url, web_url=web_url,
    )
    logger.info(f"Config refresh: {len(fresh_ids)} user(s) from DB")


def _config_reload_loop() -> None:
    interval = CONFIG_RELOAD_SECONDS
    while not config_reload_stop.wait(interval):
        try:
            _reload_db_config_once()
        except Exception:
            logger.exception("Background config refresh crashed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global off, grok, bot_handler, db_config, file_config, config_source, _last_config_fp
    global scanner_registry, api_clients, notifiers, chat_id_to_api, chat_id_to_currency, chat_id_to_country
    global pending_store

    file_config = load_config()
    base_url = file_config["wine_cellar"]["base_url"]
    admin_token = file_config["wine_cellar"]["api_token"]

    db_config, config_source = load_runtime_config(base_url, admin_token)

    svc = db_config.get("config", {})
    web_url = svc.get("web_url", file_config["wine_cellar"].get("web_url", ""))
    bot_token = svc.get("bot_token", file_config.get("telegram", {}).get("bot_token", ""))
    default_storage = svc.get(
        "default_storage",
        file_config.get("storage", {}).get("default_storage_name", "Wine Fridge"),
    )
    _bootstrap.update({
        "base_url": base_url,
        "admin_token": admin_token,
        "web_url": web_url,
        "bot_token": bot_token,
        "default_storage": default_storage,
    })

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
    admin_chat_ids = []  # staff only — receives operational notices, not broadcasts

    for tu in db_config.get("telegram_users", []):
        chat_id = _register_telegram_user(
            tu, default_storage=default_storage, base_url=base_url, web_url=web_url,
        )
        all_chat_ids.append(chat_id)
        if tu.get("is_staff"):
            admin_chat_ids.append(chat_id)

    # Notifiers are created here (and reused on later reloads). Boot used to
    # also build a second set from token_to_chat_ids, which leaked immediately.
    _ingest_scanners(
        db_config.get("scanners") or [],
        bot_token=bot_token, base_url=base_url, web_url=web_url,
    )
    _last_config_fp = _config_fingerprint(db_config)

    # Last resort only: never used when a last-good cache exists.
    if config_source == "yaml" and not scanner_registry and not api_clients:
        logger.info("No scanners/users in DB or cache, using config.yaml fallback")
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
            _replace_notifier(fallback_token, bot_token, yaml_chat_ids)
            all_chat_ids = yaml_chat_ids
            # No DB to tell us who is staff; in fallback mode the yaml chat_ids
            # are the operator's own, so they're the right audience for notices.
            admin_chat_ids = list(yaml_chat_ids)
            for cid in yaml_chat_ids:
                chat_id_to_api[str(cid)] = api_clients[fallback_token]
        # Register a synthetic scanner so the fallback works
        scanner_registry["_fallback"] = {
            "api_token": fallback_token,
            "default_storage": fallback_storage,
            "notify_chat_ids": yaml_chat_ids,
        }

    # Staff client for invite preview and redeem. Those endpoints return
    # tokens, so they stay IsAdminUser — the clicker has no token of their own.
    admin_api = WineCellarAPI(
        base_url=base_url,
        api_token=admin_token,
        default_storage=default_storage,
        web_url=web_url,
    )

    # Telegram bot handler — per-chat_id routing via chat_id_to_api
    if bot_token and all_chat_ids:
        # Primary API (first available) as fallback
        primary_api = next(iter(api_clients.values()))
        redis_url = os.environ.get("REDIS_URL", "")
        pending_store = CardStore.from_url(redis_url, prefix="whiney:pending:")
        bot_handler = TelegramBotHandler(
            bot_token=bot_token,
            chat_ids=all_chat_ids,
            wine_cellar_api=primary_api,
            pending_store=pending_store,
            pending_lock=pending_lock,
            chat_id_to_api=chat_id_to_api,
            admin_chat_ids=admin_chat_ids,
            admin_api=admin_api,
            card_store=CardStore.from_url(redis_url, prefix="whiney:card:"),
        )
        bot_handler.set_local_barcode_reader(read_barcode_local)
        for cid, ccy in chat_id_to_currency.items():
            bot_handler.set_currency(cid, ccy)
        for cid, cc in chat_id_to_country.items():
            bot_handler.set_country(cid, cc)

        def _api_factory(chat_id: str, household_id: int):
            """Build a new WineCellarAPI for a freshly added user."""
            # Re-fetch config to get the new user's token
            fresh = fetch_db_config(base_url, admin_token)
            try:
                save_cached_config(fresh)
            except OSError:
                pass
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
            currency = tu_info.get("currency") or "EUR"
            country = tu_info.get("country") or ""
            chat_id_to_currency[str(chat_id)] = currency
            chat_id_to_country[str(chat_id)] = country
            if bot_handler:
                bot_handler.set_currency(str(chat_id), currency)
                bot_handler.set_country(str(chat_id), country)
            return client

        bot_handler.set_api_factory(_api_factory)
        if grok:
            bot_handler.set_photo_callback(
                _make_photo_handler(grok, off, pending_store)
            )
            bot_handler.set_triage_callback(grok.triage_photo)
            bot_handler.set_winelist_callback(grok.analyse_wine_list)
            bot_handler.set_identify_callback(grok.identify_wine_from_hint)
        bot_handler.run_in_thread()
        logger.info(f"Telegram bot: ACTIVE ({len(all_chat_ids)} user(s))")

        # Operational notice — staff only. Household members don't need to know
        # the service bounced; they only care when a scan or photo fails.
        if admin_chat_ids:
            _staff_notice(
                bot_token, admin_chat_ids,
                "\U0001F7E2 <b>Wine Scanner Server started</b>",
            )
    else:
        logger.info("Telegram bot: DISABLED (no token/chat_ids)")

    logger.info(
        f"Wine Scanner Server ready — "
        f"{len(scanner_registry)} scanner(s), "
        f"{len(all_chat_ids)} user(s) "
        f"(config={config_source})"
    )
    if config_reload_enabled():
        config_reload_stop.clear()
        threading.Thread(
            target=_config_reload_loop, name="config-reload", daemon=True,
        ).start()
        logger.info(f"Config reload every {CONFIG_RELOAD_SECONDS:.0f}s")
    else:
        logger.info(
            "Config reload off — /adduser and /removeuser still hot-update"
        )
    yield

    config_reload_stop.set()

    # Shutdown — staff only, same reasoning as the startup notice.
    if bot_token and admin_chat_ids:
        _staff_notice(
            bot_token, admin_chat_ids,
            "\U0001F534 <b>Wine Scanner Server stopped</b>",
        )
    _close_all_notifiers()
    logger.info("Wine Scanner Server stopped")


app = FastAPI(title="Wine Scanner Server", lifespan=lifespan)


@app.get("/health")
async def health():
    users = len(bot_handler.chat_ids) if bot_handler else 0
    return {
        "status": "ok" if config_source != "yaml" else "degraded",
        "config_source": config_source,
        "users": users,
        "version": os.environ.get("SCANNER_VERSION", "dev"),
    }


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
        return _handle_known(wines[0], barcode, mode, api, notifier, scanner)

    # --- Multiple matches ---
    return _handle_ambiguous(wines, barcode, mode, notifier)


def _stock_card_kwargs(result: dict) -> dict:
    return {
        "storage": result.get("storage") or "",
        "item_id": result.get("item_id"),
        "drawers": result.get("drawers") or 0,
        "row": result.get("row"),
        "storage_id": result.get("storage_id"),
    }


def _handle_known(wine: dict, barcode: str, mode: str,
                  api: WineCellarAPI, notifier: TelegramNotifier | None,
                  scanner: dict | None = None) -> ScanResponse:
    wine_id = wine["id"]
    storage_name = (scanner or {}).get("default_storage") or None

    if mode == "in":
        result = api.add_stock(wine_id, storage_name=storage_name)
        if result.get("error"):
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
                action="in",
                stock_count=result["stock_count"],
                **_stock_card_kwargs(result),
            )
            send_achievements = getattr(notifier, "send_achievements", None)
            if send_achievements:
                send_achievements(result.get("achievements"))
        return ScanResponse(
            status="added", beep="in",
            wine_name=result["wine_name"],
            stock_count=result["stock_count"],
        )
    else:
        result = api.remove_stock(wine_id, storage_name=storage_name)
        if "error" in result and storage_name:
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
                **_stock_card_kwargs(result),
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
    # The store is shared with the bot and spans every household, so entries
    # must be namespaced the same way on both sides.
    key = pending_key(getattr(api, "household_id", None), barcode)

    # Check if already pending
    with pending_lock:
        existing = pending_store.get(key)
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
        pending_store[key] = {
            "off_data": off_data,
            "time": time.time(),
            "barcode": barcode,
            "household_id": getattr(api, "household_id", None),
        }

    if notifier:
        notifier.send_new_wine_prompt(barcode, off_data)

    return ScanResponse(
        status="unknown", beep="unknown",
        message=f"Unknown barcode {barcode}. Check Telegram.",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
