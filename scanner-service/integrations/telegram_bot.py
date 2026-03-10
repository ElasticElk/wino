"""Telegram bot for wine scanner notifications, disambiguation, and photo handling."""

import asyncio
import functools
import logging
import re
import threading
import time
from io import BytesIO
from typing import Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

PENDING_TIMEOUT_SECONDS = 600  # 10 minutes

WINE_TYPES = {
    "RE": "Red", "WH": "White", "RO": "Rose", "SP": "Sparkling",
    "DE": "Dessert", "FO": "Fortified", "OR": "Orange",
}

WINE_TYPE_EMOJI = {
    "RE": "\U0001F377", "WH": "\U0001F942", "RO": "\U0001F339", "SP": "\U0001F37E",
    "DE": "\U0001F36F", "FO": "\U0001F3FA", "OR": "\U0001F34A",
}

# Dimensions for smart cascade grouping
BROWSE_DIMENSIONS = {
    "type": ("wine_type", WINE_TYPES, WINE_TYPE_EMOJI),
    "country": ("country", None, None),  # dynamic labels
    "grape": ("grape", None, None),  # extracted from producer/name heuristic or API field
    "producer": ("producer", None, None),
}

BROWSE_PAGE_SIZE = 8
BROWSE_SESSION_TTL = 600  # 10 minutes

# Telegram limits callback_data to 64 bytes. We use short keys to stay under.
# Format: "a:action,w:wine_id,b:barcode,o:original_action"
# Example: "a:ww,w:42,b:7798039590229,o:in" = 37 bytes


def _cb(action: str, wine_id: int | None = None, barcode: str = "",
        original: str = "", page: int | None = None,
        item_id: int | None = None, row: int | None = None) -> str:
    """Build a compact callback_data string that fits Telegram's 64-byte limit."""
    parts = [f"a:{action}"]
    if wine_id is not None:
        parts.append(f"w:{wine_id}")
    if barcode:
        parts.append(f"b:{barcode}")
    if original:
        parts.append(f"o:{original}")
    if page is not None:
        parts.append(f"p:{page}")
    if item_id is not None:
        parts.append(f"i:{item_id}")
    if row is not None:
        parts.append(f"r:{row}")
    return ",".join(parts)


def _parse_cb(data: str) -> dict:
    """Parse a compact callback_data string back into a dict."""
    result = {}
    for part in data.split(","):
        if ":" in part:
            key, value = part.split(":", 1)
            if key == "a":
                result["action"] = value
            elif key == "w":
                result["wine_id"] = int(value)
            elif key == "b":
                result["barcode"] = value
            elif key == "o":
                result["original"] = value
            elif key == "p":
                result["page"] = int(value)
            elif key == "i":
                result["item_id"] = int(value)
            elif key == "r":
                result["row"] = int(value)
    return result


def _extract_barcode_from_message(message) -> str | None:
    """Extract a barcode from a Telegram message.

    Checks text_html first (has <code> tags), then falls back to plain text patterns.
    """
    html = getattr(message, "text_html", None) or ""
    match = re.search(r"<code>(\d+)</code>", html)
    if match:
        return match.group(1)
    text = message.text or ""
    match = re.search(r"Barcode:\s*(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{8,13})\b", text)
    if match:
        return match.group(1)
    return None


def _active_pendings(pending_store: dict) -> dict:
    """Return only non-stale, non-resolved pending barcodes.
    Filters out internal temp keys (_label_, _edit_)."""
    now = time.time()
    return {
        bc: data for bc, data in pending_store.items()
        if not bc.startswith("_")
        and not data.get("resolved")
        and (now - data.get("time", 0)) < PENDING_TIMEOUT_SECONDS
    }


def _clean_wine_data(grok_data: dict) -> dict:
    """Strip non-wine fields and enforce API field limits."""
    clean = {k: v for k, v in grok_data.items()
             if k not in ("type", "barcode") and v is not None}
    # Remove empty strings — avoids serializer blank validation errors
    clean = {k: v for k, v in clean.items() if v != ""}
    if "description" in clean and len(clean["description"]) > 450:
        clean["description"] = clean["description"][:447] + "..."
    if "name" in clean and len(clean["name"]) > 100:
        clean["name"] = clean["name"][:97] + "..."
    if "country" in clean:
        clean["country"] = clean["country"][:2]
    # Ensure list fields are actually lists
    for field in ("food_pairings", "attributes"):
        if field in clean and not isinstance(clean[field], list):
            clean.pop(field)
    # Filter empty strings from lists
    for field in ("food_pairings", "attributes"):
        if field in clean:
            clean[field] = [x for x in clean[field] if x]
    return clean


def _grok_summary(data: dict) -> str:
    """Build a human-readable summary of Grok-extracted wine data."""
    text = f"<b>{data.get('name', 'Unknown')}</b>\n"
    if data.get("producer"):
        text += f"Producer: {data['producer']}\n"
    if data.get("vintage"):
        text += f"Vintage: {data['vintage']}\n"
    if data.get("wine_type"):
        text += f"Type: {WINE_TYPES.get(data['wine_type'], data['wine_type'])}\n"
    if data.get("country"):
        text += f"Country: {data['country']}\n"
    if data.get("region"):
        text += f"Region: {data['region']}\n"
    if data.get("appellation"):
        text += f"Appellation: {data['appellation']}\n"
    if data.get("grape_variety"):
        text += f"Grape: {data['grape_variety']}\n"
    if data.get("abv"):
        text += f"ABV: {data['abv']}%\n"
    if data.get("size"):
        text += f"Size: {data['size']}L\n"
    if data.get("description"):
        text += f"\n<i>{data['description']}</i>\n"
    if data.get("food_pairings"):
        text += f"\n🍽 Pairs with: {', '.join(data['food_pairings'])}\n"
    if data.get("attributes"):
        text += f"✨ {', '.join(data['attributes'])}\n"
    return text


class BrowseSession:
    """Server-side state for a stock browse session.

    Each /stock or /fridge command creates a session keyed by chat_id:message_id.
    Stores the current filter path so callbacks just send a session key + action.

    Modes:
      "stock"  — browse-only: 📋 details, +1, -1
      "fridge" — management: ➖, stock count (set qty), ➕, 📋 details, drawer info
    """

    def __init__(self, wines: list[dict], mode: str = "stock", storage_name: str = ""):
        self.all_wines = wines
        self.filters: dict[str, str] = {}  # dimension -> value, e.g. {"type": "RE", "country": "FR"}
        self.page = 0
        self.time = time.time()
        self.mode = mode  # "stock" or "fridge"
        self.storage_name = storage_name  # for fridge title

    @property
    def filtered_wines(self) -> list[dict]:
        wines = self.all_wines
        for dim, value in self.filters.items():
            field = self._field_for_dim(dim)
            wines = [w for w in wines if self._get_field(w, field) == value]
        return wines

    def _field_for_dim(self, dim: str) -> str:
        mapping = {"type": "wine_type", "country": "country", "grape": "grape", "producer": "producer"}
        return mapping.get(dim, dim)

    def _get_field(self, wine: dict, field: str) -> str:
        val = wine.get(field, "")
        if not val and field == "grape":
            # Grape isn't always a top-level field; try name heuristics later
            return "Unknown"
        return val or "Unknown"

    def add_filter(self, dim: str, value: str):
        self.filters[dim] = value
        self.page = 0

    def remove_last_filter(self):
        if self.filters:
            keys = list(self.filters.keys())
            del self.filters[keys[-1]]
        self.page = 0

    def used_dimensions(self) -> list[str]:
        return list(self.filters.keys())

    def available_dimensions(self) -> list[str]:
        used = set(self.filters.keys())
        return [d for d in ["type", "country", "grape", "producer"] if d not in used]


def _pick_best_dimension(wines: list[dict], available_dims: list[str]) -> str | None:
    """Pick the dimension that creates the most useful split.

    A good split has:
    - Multiple groups (at least 2)
    - No single group dominates (largest group < 80% of total)
    - Not too many groups (prefer 3-8 groups over 20+)
    """
    if not available_dims or len(wines) <= BROWSE_PAGE_SIZE:
        return None

    field_map = {"type": "wine_type", "country": "country", "grape": "grape", "producer": "producer"}
    best_dim = None
    best_score = -1

    for dim in available_dims:
        field = field_map[dim]
        groups: dict[str, int] = {}
        for w in wines:
            val = w.get(field, "") or "Unknown"
            groups[val] = groups.get(val, 0) + 1

        n_groups = len(groups)
        if n_groups < 2:
            continue

        total = len(wines)
        largest = max(groups.values())
        dominance = largest / total  # 1.0 = all in one group, bad

        # Score: prefer low dominance, moderate number of groups
        # Penalize if largest group is >80% (dimension doesn't help)
        if dominance > 0.80:
            score = 0.1  # still better than nothing as fallback
        else:
            # Balance score: ideal is even split
            evenness = 1 - (dominance - 1 / n_groups)
            # Group count score: prefer 3-8 groups
            if 3 <= n_groups <= 8:
                group_score = 1.0
            elif n_groups == 2:
                group_score = 0.8
            elif n_groups <= 12:
                group_score = 0.7
            else:
                group_score = 0.4  # too many small groups
            score = evenness * group_score

        if score > best_score:
            best_score = score
            best_dim = dim

    return best_dim


def _group_wines(wines: list[dict], dim: str) -> list[tuple[str, str, int]]:
    """Group wines by a dimension. Returns [(value, display_label, count)] sorted by count desc."""
    field_map = {"type": "wine_type", "country": "country", "grape": "grape", "producer": "producer"}
    field = field_map[dim]

    groups: dict[str, int] = {}
    for w in wines:
        val = w.get(field, "") or "Unknown"
        groups[val] = groups.get(val, 0) + 1

    result = []
    for val, count in sorted(groups.items(), key=lambda x: -x[1]):
        if dim == "type":
            label = WINE_TYPES.get(val, val)
            emoji = WINE_TYPE_EMOJI.get(val, "\U0001F377")
            display = f"{emoji} {label}"
        elif dim == "country":
            try:
                import pycountry
                c = pycountry.countries.get(alpha_2=val)
                display = f"{c.flag} {c.name}" if c else val
            except Exception:
                display = val
        else:
            display = val

        result.append((val, display, count))

    return result


def _build_browse_view(session: BrowseSession, session_key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the appropriate view for the current browse state."""
    wines = session.filtered_wines
    total_bottles = sum(w.get("stock_count", 0) for w in wines)

    if not wines:
        keyboard = []
        if session.filters:
            keyboard.append([InlineKeyboardButton(
                "\u2b05\ufe0f Back", callback_data=_cb("sb", original=session_key),
            )])
        return "\U0001F377 No wines match this filter.", InlineKeyboardMarkup(keyboard)

    available = session.available_dimensions()
    best_dim = _pick_best_dimension(wines, available)

    # If small enough or no useful split, show flat list
    if len(wines) <= BROWSE_PAGE_SIZE or best_dim is None:
        return _build_browse_list(session, session_key, wines)

    # Show grouped view
    return _build_browse_groups(session, session_key, wines, best_dim, total_bottles)


def _build_browse_groups(
    session: BrowseSession, session_key: str,
    wines: list[dict], dim: str, total_bottles: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a grouped view for a dimension."""
    groups = _group_wines(wines, dim)

    # Build breadcrumb
    breadcrumb = _build_breadcrumb(session)
    title = f"{breadcrumb}\n" if breadcrumb else ""
    title += f"\U0001F4CA <b>{len(wines)} wines, {total_bottles} bottles</b>\n"

    dim_labels = {"type": "Type", "country": "Country", "grape": "Grape", "producer": "Producer"}
    title += f"\nGrouped by <b>{dim_labels.get(dim, dim)}</b>:\n"

    lines = [title]
    keyboard = []

    for val, display, count in groups:
        bottles = sum(w.get("stock_count", 0) for w in wines
                      if (w.get({"type": "wine_type"}.get(dim, dim), "") or "Unknown") == val)
        lines.append(f"  {display} \u2014 {count} wines, {bottles} bottles")
        keyboard.append([InlineKeyboardButton(
            f"{display} ({count})",
            callback_data=_cb("sd2", original=f"{session_key}:{dim}:{val}"),
        )])

    # "Group by" alternatives
    other_dims = [d for d in session.available_dimensions() if d != dim]
    if other_dims:
        alt_row = []
        for d in other_dims:
            d_label = dim_labels.get(d, d)
            alt_row.append(InlineKeyboardButton(
                f"By {d_label}",
                callback_data=_cb("sg", original=f"{session_key}:{d}"),
            ))
        keyboard.append(alt_row)

    # "List all" option
    keyboard.append([InlineKeyboardButton(
        f"\U0001F4CB List all {len(wines)}",
        callback_data=_cb("sl", original=session_key),
    )])

    # Back button
    if session.filters:
        keyboard.append([InlineKeyboardButton(
            "\u2b05\ufe0f Back",
            callback_data=_cb("sb", original=session_key),
        )])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _build_browse_list(
    session: BrowseSession, session_key: str, wines: list[dict],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a paginated flat list of wines. Layout depends on session.mode."""
    total = len(wines)
    page = session.page
    start = page * BROWSE_PAGE_SIZE
    end = min(start + BROWSE_PAGE_SIZE, total)
    page_wines = wines[start:end]
    total_pages = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)

    breadcrumb = _build_breadcrumb(session)
    total_bottles = sum(w.get("stock_count", 0) for w in wines)
    title = f"{breadcrumb}\n" if breadcrumb else ""
    if session.storage_name and not breadcrumb:
        title += f"\U0001F377 <b>{session.storage_name}</b> \u2014 "
    title += f"\U0001F4CA <b>{total} wines, {total_bottles} bottles</b>\n"

    lines = [title]
    keyboard = []
    is_fridge = session.mode == "fridge"

    for idx, w in enumerate(page_wines, start=start + 1):
        emoji = WINE_TYPE_EMOJI.get(w.get("wine_type", ""), "\U0001F377")
        producer = w.get("producer", "")
        producer_str = f" <i>({producer})</i>" if producer else ""

        if is_fridge:
            # Drawer info
            items = w.get("items", [])
            drawer_parts = []
            for item in items:
                d = item.get("drawer")
                if d is not None:
                    s = item.get("storage", "")
                    label = f"D{d}"
                    if not session.storage_name and s:
                        label = f"{s}:{label}"
                    drawer_parts.append(label)
            drawer_str = ", ".join(drawer_parts) if drawer_parts else ""
            location_hint = f" [{drawer_str}]" if drawer_str else ""
            lines.append(f"{idx}. {emoji} <b>{w['name']}</b>{producer_str} \u2014 {w['stock_count']}x{location_hint}")
            keyboard.append([
                InlineKeyboardButton(f"{idx}. \u2796", callback_data=_cb("-", wine_id=w["id"], page=page)),
                InlineKeyboardButton(f"{w['stock_count']}x", callback_data=_cb("sq", wine_id=w["id"], page=page)),
                InlineKeyboardButton("\u2795", callback_data=_cb("+", wine_id=w["id"], page=page)),
                InlineKeyboardButton("\U0001F4CB", callback_data=_cb("det", wine_id=w["id"])),
            ])
        else:
            lines.append(f"{idx}. {emoji} <b>{w['name']}</b>{producer_str} \u2014 {w['stock_count']}x")
            keyboard.append([
                InlineKeyboardButton(f"{idx}. \U0001F4CB", callback_data=_cb("det", wine_id=w["id"])),
                InlineKeyboardButton("\U0001F4E5 +1", callback_data=_cb("in", wine_id=w["id"])),
                InlineKeyboardButton("\U0001F4E4 -1", callback_data=_cb("out", wine_id=w["id"])),
            ])

    # Pagination
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev", callback_data=_cb("sp", original=f"{session_key}:{page - 1}")))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("x")))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next \u27a1\ufe0f", callback_data=_cb("sp", original=f"{session_key}:{page + 1}")))
        keyboard.append(nav)

    # Back button
    if session.filters:
        keyboard.append([InlineKeyboardButton(
            "\u2b05\ufe0f Back",
            callback_data=_cb("sb", original=session_key),
        )])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _build_breadcrumb(session: BrowseSession) -> str:
    """Build a breadcrumb string from the current filter path."""
    if not session.filters:
        return ""
    dim_labels = {"type": "Type", "country": "Country", "grape": "Grape", "producer": "Producer"}
    parts = []
    for dim, val in session.filters.items():
        if dim == "type":
            emoji = WINE_TYPE_EMOJI.get(val, "")
            label = WINE_TYPES.get(val, val)
            parts.append(f"{emoji} {label}")
        elif dim == "country":
            try:
                import pycountry
                c = pycountry.countries.get(alpha_2=val)
                parts.append(f"{c.flag} {c.name}" if c else val)
            except Exception:
                parts.append(val)
        else:
            parts.append(val)
    return " \u203a ".join(parts)


class TelegramNotifier:
    """Handles sending notifications to Telegram. Broadcasts to all configured chat IDs.

    Works from both sync contexts (standalone mode) and when an async event loop
    is already running (server mode with uvicorn).
    """

    def __init__(self, bot_token: str, chat_ids: list[str]):
        self.bot = Bot(token=bot_token)
        self.chat_ids = chat_ids
        # Dedicated event loop + thread for sending, avoids conflicts with uvicorn's loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=15)

    def _send_to_all(self, send_func):
        """Call send_func(chat_id) for each configured chat ID."""
        for chat_id in self.chat_ids:
            try:
                self._run(send_func(chat_id))
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")

    def send(self, text: str):
        self._send_to_all(lambda cid: self.bot.send_message(
            chat_id=cid, text=text, parse_mode="HTML"
        ))

    def send_stock_update_with_actions(self, wine_name: str, wine_id: int,
                                       barcode: str, action: str, stock_count: int):
        """Send a stock update with undo + wrong-wine buttons as a safety net."""
        if action == "in":
            emoji = "📥"
            verb = "Added"
        else:
            emoji = "📤"
            verb = "Removed"

        text = (
            f"{emoji} <b>{verb}:</b> {wine_name}\n"
            f"📊 In stock: {stock_count}"
        )
        if stock_count == 0:
            text += "\n\n⚠️ <b>Last bottle removed!</b>"

        undo_action = "ua" if action == "in" else "ur"
        keyboard = [[
            InlineKeyboardButton("↩️ Undo", callback_data=_cb(undo_action, wine_id=wine_id)),
            InlineKeyboardButton("🆕 Wrong wine", callback_data=_cb("ww", wine_id=wine_id, barcode=barcode, original=action)),
        ]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        self._send_to_all(lambda cid: self.bot.send_message(
            chat_id=cid, text=text, reply_markup=reply_markup, parse_mode="HTML",
        ))

    def send_disambiguation(self, message: str, wines: list[dict], mode: str,
                            barcode: str = ""):
        """Send inline keyboard for choosing between multiple wines."""
        keyboard = []
        for wine in wines:
            label = f"{wine['name']} {wine.get('vintage', '')} (stock: {wine['stock_count']})"
            keyboard.append([InlineKeyboardButton(
                label, callback_data=_cb(mode, wine_id=wine["id"]),
            )])

        if barcode:
            keyboard.append([InlineKeyboardButton(
                "🆕 None of these — new wine",
                callback_data=_cb("nw", barcode=barcode),
            )])

        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))])
        reply_markup = InlineKeyboardMarkup(keyboard)

        self._send_to_all(lambda cid: self.bot.send_message(
            chat_id=cid, text=message, reply_markup=reply_markup, parse_mode="HTML",
        ))

    def send_new_wine_prompt(self, barcode: str, off_data: dict | None = None):
        """Prompt user about a new unknown barcode — always ask for a label photo."""
        if off_data and off_data.get("name"):
            text = (
                f"🍷 <b>New wine scanned!</b>\n\n"
                f"OFF found: <b>{off_data['name']}</b>\n"
                f"Producer: {off_data.get('producer', 'Unknown')}\n"
                f"Country: {off_data.get('country', 'Unknown')}\n"
                f"ABV: {off_data.get('abv', 'Unknown')}\n"
                f"Barcode: <code>{barcode}</code>\n\n"
                f"📸 Send a label photo to enrich with tasting notes, "
                f"pairings and more — or add with OFF data below."
            )
            keyboard = [
                [InlineKeyboardButton("📸 Send label photo", callback_data=_cb("retry", barcode=barcode))],
                [InlineKeyboardButton("✅ Add with OFF data", callback_data=_cb("off", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
                [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
            ]
        else:
            text = (
                f"❓ <b>Unknown barcode: <code>{barcode}</code></b>\n\n"
                f"Not found in Open Food Facts.\n\n"
                f"📸 Send a label photo and I'll extract the details + tasting notes."
            )
            keyboard = [
                [InlineKeyboardButton("📸 Send label photo", callback_data=_cb("retry", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
                [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        self._send_to_all(lambda cid: self.bot.send_message(
            chat_id=cid, text=text, reply_markup=reply_markup, parse_mode="HTML",
        ))

    def send_error(self, message: str):
        self.send(f"⚠️ {message}")


class TelegramBotHandler:
    """Runs the Telegram bot polling loop to handle commands and callbacks.

    This runs in a separate thread from the scanner service.
    """

    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str],
        wine_cellar_api,
        pending_store: dict,
        pending_lock=None,
        chat_id_to_api: dict | None = None,
    ):
        self.bot_token = bot_token
        self.chat_ids = [str(cid) for cid in chat_ids]
        self.api = wine_cellar_api  # fallback/default API client
        self._chat_id_to_api = chat_id_to_api or {}  # per-user household routing
        self.pending_store = pending_store
        self._lock = pending_lock or threading.Lock()
        self._app = None
        self._user_filter = None
        self._photo_callback = None
        self._api_factory = None
        self._triage_callback = None
        self._local_barcode_reader = None
        self._browse_sessions: dict[str, BrowseSession] = {}

    def _get_api(self, update_or_id=None):
        """Get the household-specific API client for a chat_id, or fallback.

        Accepts: Update, CallbackQuery, chat_id (str/int), or None for default.
        """
        if update_or_id is None:
            return self.api
        # Extract chat_id from various telegram objects
        chat_id = None
        if hasattr(update_or_id, "effective_chat"):
            chat_id = str(update_or_id.effective_chat.id)
        elif hasattr(update_or_id, "message") and hasattr(update_or_id.message, "chat_id"):
            chat_id = str(update_or_id.message.chat_id)
        else:
            chat_id = str(update_or_id)
        return self._chat_id_to_api.get(chat_id, self.api)

    def set_photo_callback(self, callback: Callable):
        """Set callback for when a label photo is received. Called with (barcode, photo_bytes).

        Should return a dict of extracted wine data (from Grok), or None on failure.
        """
        self._photo_callback = callback

    def set_triage_callback(self, callback: Callable):
        """Set callback for classifying + processing an unknown photo.
        Called with (photo_bytes). Returns a dict with "type" key:
        - {"type": "barcode", "barcode": "123..."}
        - {"type": "label", "name": ..., ...}
        - {"type": "other"}
        """
        self._triage_callback = callback

    def set_local_barcode_reader(self, callback: Callable):
        """Set callback for fast local barcode reading (pyzbar).
        Called with (photo_bytes). Returns barcode string or None.
        """
        self._local_barcode_reader = callback

    def set_api_factory(self, factory: Callable):
        """Set a factory function to build new WineCellarAPI clients.
        Called with (api_token, household_id) -> WineCellarAPI.
        """
        self._api_factory = factory

    def add_chat_id(self, chat_id: str, api_client=None):
        """Hot-add a chat_id to the bot's allowed list (no restart needed)."""
        chat_id = str(chat_id)
        if chat_id not in self.chat_ids:
            self.chat_ids.append(chat_id)
        if self._user_filter is not None:
            self._user_filter.add_chat_ids(int(chat_id))
        if api_client is not None:
            self._chat_id_to_api[chat_id] = api_client

    def remove_chat_id(self, chat_id: str):
        """Hot-remove a chat_id from the bot's allowed list."""
        chat_id = str(chat_id)
        if chat_id in self.chat_ids:
            self.chat_ids.remove(chat_id)
        if self._user_filter is not None:
            self._user_filter.remove_chat_ids(int(chat_id))
        self._chat_id_to_api.pop(chat_id, None)

    def _build_app(self) -> Application:
        app = Application.builder().token(self.bot_token).build()

        self._user_filter = filters.Chat(chat_id=[int(cid) for cid in self.chat_ids])
        user_filter = self._user_filter

        app.add_handler(CommandHandler("status", self._cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("stock", self._cmd_stock, filters=user_filter))
        app.add_handler(CommandHandler("fridge", self._cmd_fridge, filters=user_filter))
        app.add_handler(CommandHandler("recent", self._cmd_recent, filters=user_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=user_filter))
        app.add_handler(CommandHandler("pending", self._cmd_pending, filters=user_filter))
        app.add_handler(CommandHandler("flush", self._cmd_flush, filters=user_filter))
        app.add_handler(CommandHandler("storages", self._cmd_storages, filters=user_filter))
        app.add_handler(CommandHandler("household", self._cmd_household, filters=user_filter))
        app.add_handler(CommandHandler("switch", self._cmd_switch, filters=user_filter))
        app.add_handler(CommandHandler("adduser", self._cmd_adduser, filters=user_filter))
        app.add_handler(CommandHandler("removeuser", self._cmd_removeuser, filters=user_filter))
        app.add_handler(CommandHandler("myid", self._cmd_myid))  # no filter — anyone can get their ID
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        app.add_handler(MessageHandler(filters.PHOTO & user_filter, self._handle_photo))

        return app

    # --- Commands ---

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "🍷 <b>Wine Fridge Scanner</b>\n\n"
            "/fridge \u2014 What's in the fridge (adjust stock)\n"
            "/status \u2014 Inventory summary\n"
            "/stock \u2014 Browse wines by type\n"
            "/storages \u2014 View storage locations\n"
            "/recent \u2014 Last 10 changes\n"
            "/pending \u2014 Show pending barcodes\n"
            "/flush \u2014 Clear all pending barcodes\n"
            "/household \u2014 View household & members\n"
            "/switch \u2014 Switch active household\n"
            "/adduser \u2014 Add a member to a household\n"
            "/removeuser \u2014 Remove a member\n"
            "/myid \u2014 Show your Telegram chat ID\n"
            "/help \u2014 This message\n\n"
            "📸 <b>Reply to a barcode message</b> with a label photo to add a new wine.\n"
            "📸 <b>Reply to a wine edit message</b> with a label photo to update details.\n"
            "📷 Send a <b>barcode photo</b> (no reply) to scan without the hardware scanner."
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        api = self._get_api(update)
        try:
            wines = api.wines_in_stock()
            total_bottles = sum(w.get("stock_count", 0) for w in wines)

            type_emoji = WINE_TYPE_EMOJI

            # Count by type
            type_counts = {}
            for w in wines:
                wt = w.get("wine_type", "RE")
                type_counts[wt] = type_counts.get(wt, 0) + w.get("stock_count", 0)

            text = f"📊 <b>Wine Cellar</b>\n\n"
            text += f"<b>{len(wines)}</b> wines  ·  <b>{total_bottles}</b> bottles\n"

            if type_counts:
                parts = []
                for wt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                    emoji = type_emoji.get(wt, "🍷")
                    parts.append(f"{emoji} {count}")
                text += " ".join(parts) + "\n"

            # Storage breakdown
            try:
                storages = api.list_storages()
                if storages:
                    text += "\n"
                    for s in storages:
                        if s.get("bottles", 0) > 0:
                            text += f"📍 {s['name']}: {s['bottles']} bottles\n"
            except Exception:
                pass

            if wines:
                text += "\n"
                for w in wines[:10]:
                    emoji = type_emoji.get(w.get("wine_type", ""), "🍷")
                    producer = w.get("producer", "")
                    name = w["name"]
                    if producer:
                        name += f" <i>({producer})</i>"
                    text += f"{emoji} {name} — {w['stock_count']}x\n"
                if len(wines) > 10:
                    text += f"<i>... and {len(wines) - 10} more</i>\n"

            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = update.effective_user
        name = user.full_name if user else "Unknown"
        await update.message.reply_text(
            f"👤 <b>{name}</b>\n"
            f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
            f"Give this ID to the admin to be added to the system.",
            parse_mode="HTML",
        )

    async def _cmd_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Smart cascading stock browser."""
        api = self._get_api(update)
        try:
            wines = api.wines_in_stock()
            if not wines:
                await update.message.reply_text("\U0001F377 No wines in stock.")
                return

            # Clean up stale sessions
            now = time.time()
            stale = [k for k, s in self._browse_sessions.items()
                     if now - s.time > BROWSE_SESSION_TTL]
            for k in stale:
                del self._browse_sessions[k]

            session = BrowseSession(wines)
            # Send message first to get its ID for the session key
            msg = await update.message.reply_text("\U0001F504 Loading...")
            session_key = f"{msg.chat_id}:{msg.message_id}"
            self._browse_sessions[session_key] = session

            text, keyboard = _build_browse_view(session, session_key)
            await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        api = self._get_api(update)
        await update.message.reply_text(
            "📋 Use the Wine Cellar web UI for history.\n"
            f"\U0001F517 {api.web_url}/storage/history",
            parse_mode="HTML",
        )

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with self._lock:
            active = _active_pendings(self.pending_store)
        if not active:
            await update.message.reply_text("No pending barcodes.")
            return

        lines = ["📋 <b>Pending barcodes:</b>\n"]
        for bc, data in active.items():
            age = int(time.time() - data.get("time", 0))
            mins = age // 60
            name = ""
            off = data.get("off_data")
            if off and off.get("name"):
                name = f" — {off['name']}"
            lines.append(f"  <code>{bc}</code>{name} ({mins}m ago)")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_flush(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with self._lock:
            active = _active_pendings(self.pending_store)
            if not active:
                await update.message.reply_text("Nothing to flush.")
                return
            count = len(active)
            for bc in list(active.keys()):
                self.pending_store.pop(bc, None)
        await update.message.reply_text(f"🗑 Flushed {count} pending barcode(s).")

    async def _cmd_storages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        api = self._get_api(update)
        try:
            storages = api.list_storages()
            if not storages:
                await update.message.reply_text("No storage locations configured.")
                return

            text = "📍 <b>Storage Locations</b>\n\n"
            for s in storages:
                text += f"<b>{s['name']}</b>"
                if s.get("location"):
                    text += f" — {s['location']}"
                text += "\n"
                if s.get("drawers"):
                    text += f"  📦 {s['drawers']} drawers/shelves"
                else:
                    text += "  📦 No drawers"
                text += f" | 🍾 {s.get('bottles', 0)} bottles\n"

            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch active household: /switch [household_name]"""
        api = self._get_api(update)
        chat_id = str(update.effective_chat.id)

        try:
            households = api.list_households()
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return

        if not households or len(households) < 2:
            await update.message.reply_text("You only belong to one household — nothing to switch to.")
            return

        # If a name was provided, switch directly
        arg = " ".join(context.args).strip() if context.args else ""
        if arg:
            match = next((h for h in households if h["name"].lower() == arg.lower()), None)
            if not match:
                names = ", ".join(h["name"] for h in households)
                await update.message.reply_text(f"Household not found. Your households: {names}")
                return
            await self._do_switch(update, chat_id, match["id"], match["name"])
            return

        # Show buttons
        buttons = []
        current_id = api.household_id
        for h in households:
            label = h["name"]
            if h["id"] == current_id:
                label = f"\u2705 {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"swh:{h['id']}")])
        await update.message.reply_text(
            "\U0001F3E0 <b>Switch household</b>\n\nSelect your active household:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _do_switch(self, update_or_query, chat_id: str, household_id: int, household_name: str):
        """Perform the actual household switch."""
        new_api = None
        if self._api_factory:
            try:
                new_api = self._api_factory(chat_id, household_id)
            except Exception:
                pass

        if new_api:
            self._chat_id_to_api[chat_id] = new_api
            text = f"\u2705 Switched to <b>{household_name}</b>"
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\U0001f9ca Fridge", callback_data="cmd:fridge"),
                    InlineKeyboardButton("\U0001f4ca Status", callback_data="cmd:status"),
                ],
            ])
        else:
            text = f"\u26a0\ufe0f Could not switch to {household_name}"
            buttons = None

        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=buttons)
        elif hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=buttons)

    async def _cmd_household(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show household info and members."""
        api = self._get_api(update)
        try:
            data = api.get_household()
            text = f"\U0001F3E0 <b>{data['name']}</b>\n"
            text += f"Your role: {data['role']}\n\n"
            text += "<b>Members:</b>\n"
            for m in data.get("members", []):
                role_icon = "\U0001F451" if m["role"] == "owner" else "\U0001F464"
                name = m.get("telegram_name") or m["username"]
                text += f"  {role_icon} {name}"
                if m.get("telegram_chat_id"):
                    text += f" (<code>{m['telegram_chat_id']}</code>)"
                text += "\n"
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_adduser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a user to a household: /adduser <chat_id> <name> [household]"""
        api = self._get_api(update)
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /adduser &lt;chat_id&gt; &lt;display_name&gt; [household_name]\n\n"
                "If no household is specified, uses your default household.\n"
                "The person should message this bot with /myid to get their chat ID.",
                parse_mode="HTML",
            )
            return

        chat_id = args[0]

        # Check if last arg(s) match a household name
        household_id = None
        display_name = " ".join(args[1:])

        # Try to resolve household name from the tail of args
        try:
            households = api.list_households()
        except Exception:
            households = []

        if households and len(args) > 2:
            # Try progressively longer suffixes as household name
            for split_at in range(len(args) - 1, 1, -1):
                candidate = " ".join(args[split_at:])
                match = next((h for h in households if h["name"].lower() == candidate.lower()), None)
                if match:
                    household_id = match["id"]
                    display_name = " ".join(args[1:split_at])
                    break

        try:
            result = api.add_household_member(chat_id, display_name, household_id=household_id)
            if "error" in result:
                await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
                return
            status_text = result.get("status", "done")
            # Hot-add new chat_id — build a proper API client for their household
            new_api = None
            if self._api_factory and household_id:
                try:
                    new_api = self._api_factory(chat_id, household_id)
                except Exception:
                    pass
            self.add_chat_id(chat_id, api_client=new_api or api)

            if status_text == "created":
                text = (
                    f"\u2705 <b>New user created:</b> {display_name}\n"
                    f"Added to <b>{result['household']}</b>\n"
                    f"Username: <code>{result['username']}</code>\n\n"
                    f"They can now use this bot immediately!"
                )
            else:
                text = (
                    f"\u2705 <b>{result['display_name']}</b> added to <b>{result['household']}</b>"
                )
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_removeuser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a user from a household: /removeuser <chat_id> [household]"""
        api = self._get_api(update)
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /removeuser &lt;chat_id&gt; [household_name]\n\n"
                "If no household is specified, uses your default household.\n"
                "Use /household to see member chat IDs.",
                parse_mode="HTML",
            )
            return

        chat_id = args[0]

        # Check if remaining args match a household name
        household_id = None
        if len(args) > 1:
            household_name_candidate = " ".join(args[1:])
            try:
                households = api.list_households()
                match = next((h for h in households if h["name"].lower() == household_name_candidate.lower()), None)
                if match:
                    household_id = match["id"]
            except Exception:
                pass

        try:
            result = api.remove_household_member(chat_id, household_id=household_id)
            if "error" in result:
                await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
                return
            await update.message.reply_text(
                f"\u2705 <b>{result['display_name']}</b> removed from <b>{result['household']}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_fridge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show what's in the fridge with stock adjustment buttons.

        /fridge          -- pick a storage if >1, else show default
        /fridge <name>   -- show specific storage
        /fridge all      -- show all storages combined
        """
        arg = " ".join(context.args).strip() if context.args else ""

        api = self._get_api(update)
        try:
            storages = api.list_storages()
        except Exception as e:
            await update.message.reply_text(f"Error loading storages: {e}")
            return

        if not storages:
            await update.message.reply_text("No storages found. Scan a bottle first!")
            return

        # Determine which storage to show
        if arg and arg.lower() != "all":
            storage_name = arg
        elif arg.lower() == "all" or len(storages) == 1:
            storage_name = ""
        else:
            # Multiple storages -- let user pick
            keyboard = []
            for s in storages:
                label = f"{s['name']} ({s['bottles']} bottles)"
                keyboard.append([InlineKeyboardButton(
                    label, callback_data=_cb("fs", original=s["name"]),
                )])
            keyboard.append([InlineKeyboardButton(
                "All storages", callback_data=_cb("fs", original="__all__"),
            )])
            await update.message.reply_text(
                "\U0001F377 <b>Which fridge?</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return

        await self._start_fridge_browse(update, storage_name=storage_name)

    async def _start_fridge_browse(self, update_or_query, storage_name: str = ""):
        """Start a fridge browse session using the smart cascade."""
        api = self._get_api(update_or_query)
        try:
            wines = api.wines_in_stock(storage_name=storage_name)
        except Exception as e:
            text = f"Error loading fridge: {e}"
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        if not wines:
            text = "\U0001F377 The fridge is empty!"
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        # Clean stale sessions
        now = time.time()
        stale = [k for k, s in self._browse_sessions.items()
                 if now - s.time > BROWSE_SESSION_TTL]
        for k in stale:
            del self._browse_sessions[k]

        session = BrowseSession(wines, mode="fridge", storage_name=storage_name or "All storages")

        if hasattr(update_or_query, "edit_message_text"):
            # Callback query -- edit the existing message
            chat_id = update_or_query.message.chat_id
            msg_id = update_or_query.message.message_id
            session_key = f"{chat_id}:{msg_id}"
            self._browse_sessions[session_key] = session
            text, keyboard = _build_browse_view(session, session_key)
            await update_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            # New message
            msg = await update_or_query.message.reply_text("\U0001F504 Loading...")
            session_key = f"{msg.chat_id}:{msg.message_id}"
            self._browse_sessions[session_key] = session
            text, keyboard = _build_browse_view(session, session_key)
            await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    # Legacy fridge page builder — used by +/- callbacks that refresh inline
    FRIDGE_PAGE_SIZE = 8

    def _build_fridge_page(self, wines: list[dict], page: int = 0, storage_name: str = ""):
        """Build a fridge inventory page with +/- buttons per wine."""
        total = len(wines)
        start = page * self.FRIDGE_PAGE_SIZE
        end = min(start + self.FRIDGE_PAGE_SIZE, total)
        page_wines = wines[start:end]
        total_pages = (total + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE

        title = storage_name or "All storages"
        total_bottles = sum(w["stock_count"] for w in wines)
        lines = [f"\U0001F377 <b>{title}</b> \u2014 {total} wines, {total_bottles} bottles\n"]
        keyboard = []

        for idx, wine in enumerate(page_wines, start=start + 1):
            emoji = WINE_TYPE_EMOJI.get(wine.get("wine_type", ""), "\U0001F377")
            items = wine.get("items", [])
            drawer_parts = []
            for item in items:
                d = item.get("drawer")
                if d is not None:
                    s = item.get("storage", "")
                    label = f"D{d}"
                    if not storage_name and s:
                        label = f"{s}:{label}"
                    drawer_parts.append(label)
            drawer_str = ", ".join(drawer_parts) if drawer_parts else ""
            location_hint = f" [{drawer_str}]" if drawer_str else ""
            producer = wine.get("producer", "")
            producer_str = f" <i>({producer})</i>" if producer else ""
            lines.append(f"{idx}. {emoji} <b>{wine['name']}</b>{producer_str} \u2014 {wine['stock_count']}x{location_hint}")
            keyboard.append([
                InlineKeyboardButton(f"{idx}. \u2796", callback_data=_cb("-", wine_id=wine["id"], page=page)),
                InlineKeyboardButton(f"{wine['stock_count']}x", callback_data=_cb("sq", wine_id=wine["id"], page=page)),
                InlineKeyboardButton("\u2795", callback_data=_cb("+", wine_id=wine["id"], page=page)),
                InlineKeyboardButton("\U0001F4CB", callback_data=_cb("det", wine_id=wine["id"])),
            ])

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev", callback_data=_cb("fp", page=page - 1)))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("x")))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next \u27a1\ufe0f", callback_data=_cb("fp", page=page + 1)))
            keyboard.append(nav)

        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    # --- Helpers ---

    def _get_browse_session(self, query) -> tuple[str | None, BrowseSession | None]:
        """Look up the browse session for the message this callback came from."""
        chat_id = query.message.chat_id
        msg_id = query.message.message_id
        session_key = f"{chat_id}:{msg_id}"
        session = self._browse_sessions.get(session_key)
        return (session_key, session) if session else (None, None)

    async def _refresh_browse_session(self, query, session_key: str, session: BrowseSession):
        """Refresh wine data in a browse session and rebuild the current view."""
        api = self._get_api(query)
        storage_name = session.storage_name if session.storage_name != "All storages" else ""
        wines = api.wines_in_stock(storage_name=storage_name)
        session.all_wines = wines
        if not wines:
            del self._browse_sessions[session_key]
            await query.edit_message_text("\U0001F377 The fridge is empty!")
            return
        # If currently showing a flat list, stay on the same page (clamped)
        filtered = session.filtered_wines
        total_pages = max(1, (len(filtered) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
        session.page = min(session.page, total_pages - 1)
        text, keyboard = _build_browse_list(session, session_key, filtered)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

    def _build_detail_card(self, wine: dict) -> tuple[str, InlineKeyboardMarkup]:
        """Build the wine detail card text and keyboard."""
        wine_id = wine["id"]
        text = f"\U0001F377 <b>{wine['name']}</b>\n\n"
        producer = wine.get("producer", "")
        if not producer:
            comment = wine.get("comment", "")
            if comment:
                parts = comment.split(" | ", 1)
                if parts[0].startswith("Producer:"):
                    producer = parts[0].replace("Producer: ", "")
        if producer:
            text += f"\U0001F464 {producer}\n"
        if wine.get("vintage"):
            text += f"\U0001F4C5 {wine['vintage']}\n"
        wt = WINE_TYPES.get(wine.get('wine_type', ''), wine.get('wine_type', ''))
        if wine.get("category"):
            cats = {"DR": "Dry", "SD": "Semi-Dry", "MS": "Medium Sweet", "SW": "Sweet"}
            wt += f" ({cats.get(wine['category'], '')})"
        text += f"\U0001F3F7 {wt}\n"
        if wine.get("country"):
            text += f"\U0001F30D {wine['country']}"
            if wine.get("region_name"):
                text += f" \u2014 {wine['region_name']}"
            text += "\n"
        if wine.get("grape_variety"):
            text += f"\U0001F347 {wine['grape_variety']}\n"
        if wine.get("vineyards"):
            text += f"\U0001F3E1 {', '.join(wine['vineyards'])}\n"

        items = wine.get("items", [])
        stock_count = wine.get("stock_count", 0)
        text += f"\n\U0001F4E6 <b>{stock_count}x in stock</b>\n"
        if items:
            for item in items:
                storage = item.get("storage", "Unknown")
                drawer = item.get("drawer")
                loc = f"  \U0001F4CD {storage}"
                if drawer is not None:
                    loc += f", Drawer {drawer}"
                text += loc + "\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\U0001F4E5 +1", callback_data=_cb("in", wine_id=wine_id)),
                InlineKeyboardButton("\U0001F4E4 -1", callback_data=_cb("out", wine_id=wine_id)),
            ],
            [
                InlineKeyboardButton("\U0001F4DD Notes", callback_data=_cb("nt", wine_id=wine_id)),
                InlineKeyboardButton("\U0001F37D Pairings", callback_data=_cb("pr", wine_id=wine_id)),
                InlineKeyboardButton("\U0001F4F8 Photo", callback_data=_cb("ph", wine_id=wine_id)),
            ],
            [
                InlineKeyboardButton("\U0001F4E6 Move", callback_data=_cb("ml", wine_id=wine_id)),
                InlineKeyboardButton("\U0001F5C4 Drawer", callback_data=_cb("dw", wine_id=wine_id)),
            ],
            [InlineKeyboardButton("\u270f\ufe0f Update from label", callback_data=_cb("ed", wine_id=wine_id))],
        ])
        return text, keyboard

    def _build_drawer_view(self, wine_id: int, api=None):
        """Build the drawer assignment view for a specific wine.

        Returns (text, InlineKeyboardMarkup) or (None, None) if wine not found.
        """
        api = api or self.api
        wines = api.wines_in_stock()
        wine = next((w for w in wines if w["id"] == wine_id), None)
        if not wine:
            return None, None

        # Get drawer count from storage info
        storages = {}
        try:
            for s in api.list_storages():
                storages[s["name"]] = s.get("drawers", 0)
        except Exception:
            pass

        items = wine.get("items", [])
        lines = [f"📦 <b>Drawer assignment: {wine['name']}</b>\n"]
        keyboard = []

        for idx, item in enumerate(items):
            d = item.get("drawer")
            s = item.get("storage", "")
            label = f"Drawer {d}" if d is not None else "Unassigned"
            if s:
                label = f"{s} — {label}"
            bottle_label = f"Bottle {idx + 1}" if len(items) > 1 else "Location"
            lines.append(f"  {bottle_label}: {label}")

            # Determine how many drawers this storage has
            num_drawers = storages.get(s, 0)
            if num_drawers == 0:
                num_drawers = 7  # sensible default

            row_buttons = []
            for drawer_num in range(1, num_drawers + 1):
                marker = f"[{drawer_num}]" if d == drawer_num else str(drawer_num)
                row_buttons.append(InlineKeyboardButton(
                    marker,
                    callback_data=_cb("mv", item_id=item["item_id"], row=drawer_num),
                ))
            keyboard.append(row_buttons)

        keyboard.append([InlineKeyboardButton(
            "⬅️ Back to fridge", callback_data=_cb("fp", page=0),
        )])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    def _is_pending(self, barcode: str) -> bool:
        with self._lock:
            data = self.pending_store.get(barcode)
            if not data or data.get("resolved"):
                return False
            return (time.time() - data.get("time", 0)) < PENDING_TIMEOUT_SECONDS

    def _resolve_pending(self, barcode: str):
        with self._lock:
            if barcode in self.pending_store:
                self.pending_store[barcode]["resolved"] = True

    def _create_pending(self, barcode: str, off_data=None):
        """Create a new pending entry for a barcode (e.g. when user says 'new wine')."""
        with self._lock:
            self.pending_store[barcode] = {
                "off_data": off_data,
                "time": time.time(),
            }

    # --- Callback handler ---

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        api = self._get_api(query)

        data = _parse_cb(query.data)
        action = data.get("action", "")

        # Fridge +/- use custom answer toasts; all others answer immediately
        if action not in ("+", "-", "mv", "ss"):
            await query.answer()

        if action == "swh":
            household_id = data.get("wine_id") or int(query.data.split(":")[1])
            chat_id = str(query.message.chat_id)
            # Look up the household name
            try:
                households = api.list_households()
                h = next((h for h in households if h["id"] == household_id), None)
                name = h["name"] if h else f"Household {household_id}"
            except Exception:
                name = f"Household {household_id}"
            await self._do_switch(query, chat_id, household_id, name)
            return

        if query.data.startswith("cmd:"):
            cmd = query.data.split(":")[1]
            api = self._get_api(query)  # re-fetch after potential switch
            if cmd == "fridge":
                await query.edit_message_text("\U0001f9ca Loading fridge...")
                await self._cmd_fridge(update, context)
            elif cmd == "status":
                await query.edit_message_text("\U0001f4ca Loading status...")
                await self._cmd_status(update, context)
            return

        if action == "x":
            barcode = data.get("barcode", "")
            if barcode:
                self._resolve_pending(barcode)
            await query.edit_message_text("Cancelled.")
            return

        if action == "skip":
            barcode = data.get("barcode", "")
            self._resolve_pending(barcode)
            await query.edit_message_text("Skipped.")
            return

        # --- Stock add: show storage picker ---
        if action == "in":
            wine_id = data["wine_id"]
            try:
                storages = api.list_storages()
                if len(storages) <= 1:
                    # Only one storage — skip picker, add directly
                    storage = storages[0] if storages else None
                    storage_name = storage["name"] if storage else api.default_storage
                    drawers = storage.get("drawers", 0) if storage else 0
                    result = api.add_stock(wine_id, storage_name=storage_name)
                    if "error" in result:
                        await query.edit_message_text(f"⚠️ {result['error']}")
                    else:
                        item_id = result.get("item_id")
                        if drawers and drawers > 0 and item_id:
                            keyboard = []
                            row_buttons = []
                            for d in range(1, drawers + 1):
                                row_buttons.append(InlineKeyboardButton(
                                    str(d),
                                    callback_data=_cb("sd", item_id=item_id, row=d, wine_id=wine_id),
                                ))
                                if len(row_buttons) == 4:
                                    keyboard.append(row_buttons)
                                    row_buttons = []
                            if row_buttons:
                                keyboard.append(row_buttons)
                            keyboard.append([InlineKeyboardButton(
                                "Skip", callback_data=_cb("sd", item_id=item_id, row=-1, wine_id=wine_id),
                            )])
                            await query.edit_message_text(
                                f"📥 <b>Added:</b> {result['wine_name']}\n"
                                f"📍 {storage_name}\n\n"
                                f"🗄 <b>Which drawer?</b>",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(keyboard),
                            )
                        else:
                            keyboard = [[InlineKeyboardButton(
                                "↩️ Undo", callback_data=_cb("ua", wine_id=wine_id),
                            )]]
                            await query.edit_message_text(
                                f"📥 <b>Added:</b> {result['wine_name']}\n"
                                f"📍 {storage_name}\n"
                                f"📊 In stock: {result['stock_count']}",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(keyboard),
                            )
                    return

                # Multiple storages — show picker
                keyboard = []
                for s in storages:
                    label = s["name"]
                    if s.get("bottles"):
                        label += f" ({s['bottles']})"
                    keyboard.append([InlineKeyboardButton(
                        label,
                        callback_data=_cb("si", wine_id=wine_id, page=s["id"]),
                    )])
                await query.edit_message_text(
                    "📍 <b>Where are you storing this?</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Stock add with chosen storage ---
        elif action == "si":
            wine_id = data["wine_id"]
            storage_id = data.get("page", 0)  # reusing page field for storage_id
            try:
                # Look up storage name from id
                storages = api.list_storages()
                storage = next((s for s in storages if s["id"] == storage_id), None)
                storage_name = storage["name"] if storage else api.default_storage
                drawers = storage.get("drawers", 0) if storage else 0

                result = api.add_stock(wine_id, storage_name=storage_name)
                if "error" in result:
                    await query.edit_message_text(f"⚠️ {result['error']}")
                else:
                    item_id = result.get("item_id")
                    # If storage has drawers, offer to assign one
                    if drawers and drawers > 0 and item_id:
                        keyboard = []
                        row_buttons = []
                        for d in range(1, drawers + 1):
                            row_buttons.append(InlineKeyboardButton(
                                str(d),
                                callback_data=_cb("sd", item_id=item_id, row=d, wine_id=wine_id),
                            ))
                            if len(row_buttons) == 4:
                                keyboard.append(row_buttons)
                                row_buttons = []
                        if row_buttons:
                            keyboard.append(row_buttons)
                        keyboard.append([InlineKeyboardButton(
                            "Skip", callback_data=_cb("sd", item_id=item_id, row=-1, wine_id=wine_id),
                        )])
                        await query.edit_message_text(
                            f"📥 <b>Added:</b> {result['wine_name']}\n"
                            f"📍 {storage_name}\n\n"
                            f"🗄 <b>Which drawer?</b>",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
                    else:
                        keyboard = [[InlineKeyboardButton(
                            "↩️ Undo", callback_data=_cb("ua", wine_id=wine_id),
                        )]]
                        await query.edit_message_text(
                            f"📥 <b>Added:</b> {result['wine_name']}\n"
                            f"📍 {storage_name}\n"
                            f"📊 In stock: {result['stock_count']}",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Set drawer after stock-in ---
        elif action == "sd":
            item_id = data.get("item_id")
            row = data.get("row")
            wine_id = data.get("wine_id")
            try:
                if row and row > 0 and item_id:
                    api.move_item(item_id, row=row)
                    await query.edit_message_text(
                        f"✅ Placed in drawer {row}.",
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text("✅ Added (no drawer assigned).")
            except Exception as e:
                await query.edit_message_text(f"Error setting drawer: {e}")

        # --- Stock remove ---
        elif action == "out":
            wine_id = data["wine_id"]
            try:
                result = api.remove_stock(wine_id)
                if "error" in result:
                    await query.edit_message_text(f"⚠️ {result['error']}")
                else:
                    keyboard = [[InlineKeyboardButton(
                        "↩️ Undo", callback_data=_cb("ur", wine_id=wine_id),
                    )]]
                    await query.edit_message_text(
                        f"📤 <b>Removed:</b> {result['wine_name']}\n"
                        f"📊 In stock: {result['stock_count']}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- "It's a new/different wine" with a duplicate barcode ---
        elif action == "nw":
            barcode = data["barcode"]
            self._create_pending(barcode)
            await query.edit_message_text(
                f"🆕 <b>New wine with barcode <code>{barcode}</code></b>\n\n"
                f"📸 <b>Reply to this message</b> with a label photo "
                f"and I'll extract the details + tasting notes.\n\n"
                f"A separate wine entry will be created.",
                parse_mode="HTML",
            )

        # --- Undo add (reverse = remove) ---
        elif action == "ua":
            wine_id = data["wine_id"]
            try:
                result = api.remove_stock(wine_id)
                if "error" in result:
                    await query.edit_message_text(f"⚠️ Could not undo: {result['error']}")
                else:
                    await query.edit_message_text(
                        f"↩️ <b>Undone.</b> {result['wine_name']}\n"
                        f"📊 In stock: {result['stock_count']}",
                        parse_mode="HTML",
                    )
            except Exception as e:
                await query.edit_message_text(f"Error undoing: {e}")

        # --- Undo remove (reverse = add) ---
        elif action == "ur":
            wine_id = data["wine_id"]
            try:
                result = api.add_stock(wine_id)
                await query.edit_message_text(
                    f"↩️ <b>Undone.</b> {result['wine_name']}\n"
                    f"📊 In stock: {result['stock_count']}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.edit_message_text(f"Error undoing: {e}")

        # --- Wrong wine: undo the stock change AND start new-wine photo flow ---
        elif action == "ww":
            wine_id = data["wine_id"]
            barcode = data.get("barcode", "")
            original = data.get("original", "in")
            undo_ok = True
            try:
                if original == "in":
                    api.remove_stock(wine_id)
                else:
                    api.add_stock(wine_id)
            except Exception as e:
                undo_ok = False
                logger.warning(f"Failed to undo stock change for wine {wine_id}: {e}")

            undo_line = "↩️ Stock change undone.\n\n" if undo_ok else "⚠️ Could not undo the stock change — please fix manually.\n\n"
            self._create_pending(barcode)
            await query.edit_message_text(
                f"{undo_line}"
                f"🆕 <b>New wine with barcode <code>{barcode}</code></b>\n\n"
                f"📸 <b>Reply to this message</b> with a label photo "
                f"and I'll extract the details + tasting notes.",
                parse_mode="HTML",
            )

        # --- Add with Open Food Facts data ---
        elif action == "off":
            barcode = data["barcode"]
            if not self._is_pending(barcode):
                await query.edit_message_text("This barcode was already handled or expired.")
                return

            with self._lock:
                off_data = self.pending_store.get(barcode, {}).get("off_data", {})
            if not off_data:
                await query.edit_message_text("No OFF data found for this barcode.")
                return

            try:
                wine_data = {
                    "name": off_data.get("name", "Unknown"),
                    "producer": off_data.get("producer", ""),
                    "country": off_data.get("country", "CH")[:2],
                    "region": off_data.get("region", ""),
                    "abv": off_data.get("abv"),
                    "barcode": barcode,
                    "wine_type": "RE",
                }
                result = api.create_wine(wine_data)
                stock = api.add_stock(result["id"])
                self._resolve_pending(barcode)
                await query.edit_message_text(
                    f"🍷 <b>Wine added:</b> {result['name']}\n"
                    f"📊 In stock: {stock['stock_count']}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.edit_message_text(f"Error creating wine: {e}")

        # --- Add manually via web UI ---
        elif action == "man":
            barcode = data["barcode"]
            if not self._is_pending(barcode):
                await query.edit_message_text("This barcode was already handled or expired.")
                return
            self._resolve_pending(barcode)
            await query.edit_message_text(
                f"Add manually in Wine Cellar:\n"
                f"🔗 {api.web_url}/wine/add/{barcode}",
                parse_mode="HTML",
            )

        # --- Confirm Grok extraction and create wine ---
        elif action == "grok":
            barcode = data["barcode"]
            if not self._is_pending(barcode):
                await query.edit_message_text("This barcode was already handled or expired.")
                return

            with self._lock:
                merged = self.pending_store.get(barcode, {}).get("grok_merged")
            if not merged:
                await query.edit_message_text("Error: no extracted data found.")
                return

            try:
                wine_data = _clean_wine_data(merged)
                if not barcode.startswith("_"):
                    wine_data["barcode"] = barcode
                result = api.create_wine(wine_data)
                wine_id = result["id"]

                # Upload label photo if available
                with self._lock:
                    pending_entry = self.pending_store.get(barcode, {})
                    photo = pending_entry.get("photo_data")
                    logger.info(f"[PHOTO] grok handler: barcode={barcode}, has_photo={photo is not None}, "
                                f"photo_size={len(photo) if photo else 0}, "
                                f"pending_keys={list(pending_entry.keys())}")
                if photo:
                    try:
                        api.upload_wine_image(wine_id, photo)
                        logger.info(f"[PHOTO] Uploaded label photo for wine #{wine_id}")
                    except Exception as e:
                        logger.warning(f"[PHOTO] Failed to upload image: {e}")
                else:
                    logger.warning(f"[PHOTO] No photo_data found in pending_store for barcode={barcode}")

                self._resolve_pending(barcode)

                # Show storage picker
                storages = api.list_storages()
                if len(storages) <= 1:
                    storage = storages[0] if storages else None
                    storage_name = storage["name"] if storage else api.default_storage
                    drawers = storage.get("drawers", 0) if storage else 0
                    stock = api.add_stock(wine_id, storage_name=storage_name)
                    item_id = stock.get("item_id")
                    if drawers and drawers > 0 and item_id:
                        keyboard = []
                        row_buttons = []
                        for d in range(1, drawers + 1):
                            row_buttons.append(InlineKeyboardButton(
                                str(d),
                                callback_data=_cb("sd", item_id=item_id, row=d, wine_id=wine_id),
                            ))
                            if len(row_buttons) == 4:
                                keyboard.append(row_buttons)
                                row_buttons = []
                        if row_buttons:
                            keyboard.append(row_buttons)
                        keyboard.append([InlineKeyboardButton(
                            "Skip", callback_data=_cb("sd", item_id=item_id, row=-1, wine_id=wine_id),
                        )])
                        await query.edit_message_text(
                            f"🍷 <b>Wine created:</b> {result['name']}\n"
                            f"📍 {storage_name}\n\n"
                            f"🗄 <b>Which drawer?</b>",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
                    else:
                        await query.edit_message_text(
                            f"🍷 <b>Wine created:</b> {result['name']}\n"
                            f"📍 {storage_name}\n"
                            f"📊 In stock: {stock['stock_count']}",
                            parse_mode="HTML",
                        )
                else:
                    keyboard = []
                    for s in storages:
                        label = s["name"]
                        if s.get("bottles"):
                            label += f" ({s['bottles']})"
                        keyboard.append([InlineKeyboardButton(
                            label,
                            callback_data=_cb("si", wine_id=wine_id, page=s["id"]),
                        )])
                    await query.edit_message_text(
                        f"🍷 <b>Wine created:</b> {result['name']}\n\n"
                        f"📍 <b>Where are you storing this?</b>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
            except Exception as e:
                await query.edit_message_text(f"Error creating wine: {e}")

        # --- Retry with a new photo ---
        elif action == "retry":
            barcode = data["barcode"]
            if not self._is_pending(barcode):
                await query.edit_message_text("This barcode was already handled or expired.")
                return
            await query.edit_message_text(
                f"📸 Send another photo — <b>reply to this message</b> "
                f"with a label photo.\n\nBarcode: <code>{barcode}</code>",
                parse_mode="HTML",
            )

        # --- Browse: drill into a group ---
        elif action == "sd2":
            parts = data.get("original", "").split(":", 2)
            if len(parts) < 3:
                await query.edit_message_text("Invalid browse action.")
                return
            session_key = f"{parts[0]}:{parts[1]}"
            # parts[2] is "dim:value" but value may contain ":"
            rest = data["original"][len(session_key) + 1:]
            dim, _, value = rest.partition(":")
            session = self._browse_sessions.get(session_key)
            if not session:
                await query.edit_message_text("Session expired. Use /stock again.")
                return
            session.add_filter(dim, value)
            try:
                text, keyboard = _build_browse_view(session, session_key)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Browse: regroup by a different dimension ---
        elif action == "sg":
            parts = data.get("original", "").split(":", 2)
            if len(parts) < 3:
                await query.edit_message_text("Invalid browse action.")
                return
            session_key = f"{parts[0]}:{parts[1]}"
            dim = parts[2]
            session = self._browse_sessions.get(session_key)
            if not session:
                await query.edit_message_text("Session expired. Use /stock again.")
                return
            # Rebuild the grouped view with the chosen dimension
            wines = session.filtered_wines
            total_bottles = sum(w.get("stock_count", 0) for w in wines)
            try:
                text, keyboard = _build_browse_groups(session, session_key, wines, dim, total_bottles)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Browse: list all (flat, paginated) ---
        elif action == "sl":
            session_key = data.get("original", "")
            session = self._browse_sessions.get(session_key)
            if not session:
                await query.edit_message_text("Session expired. Use /stock again.")
                return
            session.page = 0
            try:
                wines = session.filtered_wines
                text, keyboard = _build_browse_list(session, session_key, wines)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Browse: page navigation ---
        elif action == "sp":
            parts = data.get("original", "").rsplit(":", 1)
            if len(parts) < 2:
                await query.edit_message_text("Invalid page action.")
                return
            session_key = parts[0]
            page = int(parts[1])
            session = self._browse_sessions.get(session_key)
            if not session:
                await query.edit_message_text("Session expired. Use /stock again.")
                return
            session.page = page
            try:
                wines = session.filtered_wines
                text, keyboard = _build_browse_list(session, session_key, wines)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Browse: back one level ---
        elif action == "sb":
            session_key = data.get("original", "")
            session = self._browse_sessions.get(session_key)
            if not session:
                await query.edit_message_text("Session expired. Use /stock again.")
                return
            session.remove_last_filter()
            try:
                text, keyboard = _build_browse_view(session, session_key)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Fridge: storage select ---
        elif action == "fs":
            storage_name = data.get("original", "")
            if storage_name == "__all__":
                storage_name = ""
            await self._start_fridge_browse(query, storage_name=storage_name)

        # --- Fridge: page navigation ---
        elif action == "fp":
            page = data.get("page", 0)
            session_key, session = self._get_browse_session(query)
            if session:
                session.page = page
                try:
                    await self._refresh_browse_session(query, session_key, session)
                except Exception as e:
                    await query.edit_message_text(f"Error: {e}")
            else:
                try:
                    wines = api.wines_in_stock()
                except Exception as e:
                    await query.edit_message_text(f"Error loading fridge: {e}")
                    return
                if not wines:
                    await query.edit_message_text("\U0001F377 The fridge is empty!")
                    return
                text, keyboard = self._build_fridge_page(wines, page=page)
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

        # --- Fridge: +1 stock ---
        elif action == "+":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                result = api.add_stock(wine_id)
                await query.answer(f"Added: {result['wine_name']} ({result['stock_count']}x)")
                # Try browse session first, fall back to legacy
                session_key, session = self._get_browse_session(query)
                if session:
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: -1 stock ---
        elif action == "-":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                result = api.remove_stock(wine_id)
                if "error" in result:
                    await query.answer(result["error"], show_alert=True)
                    return
                await query.answer(f"Removed: {result['wine_name']} ({result['stock_count']}x)")
                # Try browse session first, fall back to legacy
                session_key, session = self._get_browse_session(query)
                if session:
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    if not wines:
                        await query.edit_message_text("\U0001F377 The fridge is empty!")
                        return
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: show drawer assignment for a wine ---
        elif action == "dw":
            wine_id = data["wine_id"]
            try:
                text, keyboard = self._build_drawer_view(wine_id, api=api)
                if text:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text("Wine not found in stock.")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Wine details (compact card) ---
        elif action == "det":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await query.edit_message_text("Wine not found.")
                    return
                text, keyboard = self._build_detail_card(wine)
                await query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=keyboard,
                )
            except Exception as e:
                try:
                    await query.edit_message_text(f"Error: {e}")
                except Exception:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id, text=f"Error: {e}",
                    )

        # --- Wine tasting notes ---
        elif action == "nt":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await query.edit_message_text("Wine not found.")
                    return

                text = f"📝 <b>{wine['name']}</b>\n\n"

                comment = wine.get("comment", "")
                if comment:
                    parts = comment.split(" | ", 1)
                    if len(parts) == 2 and parts[0].startswith("Producer:"):
                        text += f"👤 {parts[0]}\n\n"
                        text += f"{parts[1]}\n"
                    elif parts[0].startswith("Producer:"):
                        text += f"👤 {parts[0]}\n"
                    else:
                        text += f"{comment}\n"
                else:
                    text += "<i>No tasting notes yet.</i>\n"

                text += "\n"
                if wine.get("appellation_name"):
                    text += f"🏛 Appellation: {wine['appellation_name']}\n"
                if wine.get("vineyards"):
                    text += f"🏡 Vineyard: {', '.join(wine['vineyards'])}\n"
                if wine.get("abv"):
                    text += f"🔢 ABV: {wine['abv']}%\n"
                if wine.get("size_ml"):
                    text += f"📐 Size: {wine['size_ml']}L\n"
                if wine.get("drink_by"):
                    text += f"📆 Drink by: {wine['drink_by']}\n"
                if wine.get("barcode"):
                    text += f"📊 Barcode: <code>{wine['barcode']}</code>\n"
                if wine.get("attributes"):
                    text += f"\n✨ {', '.join(wine['attributes'])}\n"

                keyboard = [[
                    InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id)),
                ]]
                await query.edit_message_text(
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Food pairings ---
        elif action == "pr":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await query.edit_message_text("Wine not found.")
                    return

                text = f"🍽 <b>{wine['name']}</b>\n\n"

                pairings = wine.get("food_pairings", [])
                if pairings:
                    text += "<b>Pairs well with:</b>\n"
                    for fp in pairings:
                        text += f"  • {fp}\n"
                else:
                    text += "<i>No food pairings yet.</i>\n"

                attrs = wine.get("attributes", [])
                if attrs:
                    text += f"\n✨ <b>Attributes:</b> {', '.join(attrs)}\n"

                keyboard = [[
                    InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id)),
                ]]
                await query.edit_message_text(
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Wine photo ---
        elif action == "ph":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                wine_name = wine["name"] if wine else f"Wine #{wine_id}"

                images = []
                try:
                    images = api.get_wine_images(wine_id)
                except Exception:
                    pass

                if images:
                    image_id = images[0].get("id")
                    if image_id:
                        try:
                            photo_bytes = api.get_image_file(image_id)
                            # Store original message ID so "back" can restore it
                            orig_msg_id = query.message.message_id
                            keyboard = [[
                                InlineKeyboardButton(
                                    "\u2b05\ufe0f Back to details",
                                    callback_data=_cb("phb", wine_id=wine_id, page=orig_msg_id),
                                ),
                            ]]
                            await query.message.reply_photo(
                                photo=photo_bytes,
                                caption=f"\U0001F4F8 <b>{wine_name}</b>",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(keyboard),
                            )
                            await query.edit_message_text(
                                f"\U0001F4F8 {wine_name} \u2014 photo below \u2b07\ufe0f"
                            )
                            return
                        except Exception as e:
                            logger.warning(f"[PHOTO] Failed to send wine image: {e}")

                keyboard = [
                    [InlineKeyboardButton("📸 Upload label photo", callback_data=_cb("ed", wine_id=wine_id))],
                    [InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id))],
                ]
                await query.edit_message_text(
                    f"📸 <b>{wine_name}</b>\n\nNo photo yet.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Photo back: delete photo, restore original message to details ---
        elif action == "phb":
            wine_id = data["wine_id"]
            orig_msg_id = data.get("page")  # reusing page field for original message ID
            chat_id = query.message.chat_id
            try:
                # Delete the photo message
                await query.message.delete()
                # Rebuild details on the original "photo below" message
                wine = api.get_wine_detail(wine_id)
                if wine:
                    text, keyboard = self._build_detail_card(wine)
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=orig_msg_id,
                        text=text, parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=orig_msg_id,
                        text="Wine not found.",
                    )
            except Exception as e:
                logger.warning(f"[PHB] Error returning to details: {e}")

        # --- Move wine: show storage picker for all items ---
        elif action == "ml":
            wine_id = data["wine_id"]
            try:
                # Get wine's current items
                wines = api.wines_in_stock()
                wine = next((w for w in wines if w["id"] == wine_id), None)
                if not wine:
                    await query.edit_message_text("Wine not found in stock.")
                    return

                items = wine.get("items", [])
                current_locations = set()
                for item in items:
                    current_locations.add(item.get("storage", ""))

                storages = api.list_storages()
                text = f"📦 <b>Move: {wine['name']}</b>\n\n"
                text += f"Currently in: {', '.join(current_locations) or 'Unknown'}\n"
                text += f"({len(items)} bottle(s))\n\n"
                text += "Move all bottles to:"

                keyboard = []
                for s in storages:
                    label = s["name"]
                    if s["name"] in current_locations:
                        label = f"📍 {label} (current)"
                    keyboard.append([InlineKeyboardButton(
                        label,
                        callback_data=_cb("ma", wine_id=wine_id, page=s["id"]),
                    )])
                keyboard.append([InlineKeyboardButton(
                    "⬅️ Back", callback_data=_cb("det", wine_id=wine_id),
                )])

                await query.edit_message_text(
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Move all items to chosen storage ---
        elif action == "ma":
            wine_id = data["wine_id"]
            storage_id = data.get("page", 0)
            try:
                storages = api.list_storages()
                storage = next((s for s in storages if s["id"] == storage_id), None)
                storage_name = storage["name"] if storage else api.default_storage

                wines = api.wines_in_stock()
                wine = next((w for w in wines if w["id"] == wine_id), None)
                if not wine:
                    await query.edit_message_text("Wine not found in stock.")
                    return

                moved = 0
                for item in wine.get("items", []):
                    if item.get("storage") != storage_name:
                        api.move_item(item["item_id"], storage_name=storage_name)
                        moved += 1

                await query.edit_message_text(
                    f"📦 <b>Moved:</b> {wine['name']}\n"
                    f"📍 → {storage_name}\n"
                    f"({moved} bottle(s) moved)",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id)),
                    ]]),
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Move item to drawer ---
        elif action == "mv":
            item_id = data["item_id"]
            row = data["row"]
            try:
                result = api.move_item(item_id, row=row)
                await query.answer(f"Moved to drawer {row}")
                wine_id = result.get("wine_id")
                if wine_id:
                    text, keyboard = self._build_drawer_view(wine_id, api=api)
                    if text:
                        await query.edit_message_text(
                            text, reply_markup=keyboard, parse_mode="HTML",
                        )
                        return
                # Fallback: go back to fridge
                session_key, session = self._get_browse_session(query)
                if session:
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    text, kb = self._build_fridge_page(wines, page=0)
                    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: set quantity (show picker) ---
        elif action == "sq":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                wines = api.wines_in_stock()
                wine = next((w for w in wines if w["id"] == wine_id), None)
                if not wine:
                    await query.edit_message_text("Wine not found in stock.")
                    return

                current = wine["stock_count"]
                text = (
                    f"📊 <b>Set quantity: {wine['name']}</b>\n"
                    f"Currently: {current}x"
                )

                # Build number buttons 0-6, highlight current
                num_buttons = []
                for n in range(7):
                    label = f"[{n}]" if n == current else str(n)
                    num_buttons.append(InlineKeyboardButton(
                        label,
                        callback_data=_cb("ss", wine_id=wine_id, row=n, page=page),
                    ))
                keyboard = [
                    num_buttons,
                    [InlineKeyboardButton(
                        "⬅️ Back to fridge", callback_data=_cb("fp", page=page),
                    )],
                ]
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Fridge: set stock to exact number ---
        elif action == "ss":
            wine_id = data["wine_id"]
            quantity = data.get("row", 0)  # reusing 'r' field for quantity
            page = data.get("page", 0)
            try:
                result = api.set_stock(wine_id, quantity)
                await query.answer(
                    f"{result['wine_name']}: set to {result['stock_count']}x"
                )
                # Back to fridge — try browse session first
                session_key, session = self._get_browse_session(query)
                if session:
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    if not wines:
                        await query.edit_message_text("\U0001F377 The fridge is empty!")
                        return
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: edit wine details ---
        elif action == "ed":
            wine_id = data["wine_id"]
            try:
                result = api.update_wine(wine_id, {})  # GET-like — just fetch current data
            except Exception:
                result = {"name": f"Wine #{wine_id}"}

            # Store wine_id for the photo handler
            with self._lock:
                self.pending_store[f"_edit_{wine_id}"] = {
                    "wine_id": wine_id,
                    "time": time.time(),
                }

            await query.edit_message_text(
                f"✏️ <b>Edit: {result.get('name', f'Wine #{wine_id}')}</b>\n\n"
                f"📸 <b>Reply to this message</b> with a label photo "
                f"to update this wine's details.\n\n"
                f"Wine ID: <code>edit:{wine_id}</code>",
                parse_mode="HTML",
            )

        # --- Confirm wine update from Grok ---
        elif action == "edc":
            wine_id = data["wine_id"]
            edit_key = f"_edit_{wine_id}"
            with self._lock:
                edit_data = self.pending_store.get(edit_key, {})
                grok_data = edit_data.get("grok_merged")
            if not grok_data:
                await query.edit_message_text("Error: no extracted data found.")
                return
            try:
                wine_fields = _clean_wine_data(grok_data)
                result = api.update_wine(wine_id, wine_fields)

                # Upload label photo if available
                with self._lock:
                    photo = edit_data.get("photo_data")
                if photo:
                    try:
                        api.upload_wine_image(wine_id, photo)
                        logger.info(f"[PHOTO] Uploaded label photo for wine #{wine_id}")
                    except Exception as e:
                        logger.warning(f"[PHOTO] Failed to upload image: {e}")

                with self._lock:
                    self.pending_store.pop(edit_key, None)
                await query.edit_message_text(
                    f"✅ <b>Updated:</b> {result.get('name', 'Wine')}\n"
                    f"📊 In stock: {result.get('stock_count', '?')}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.edit_message_text(f"Error updating wine: {e}")

    # --- Photo handler ---

    async def _download_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes:
        """Download the highest-resolution photo from a message."""
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        return buf.getvalue()

    def _detect_edit_reply(self, reply) -> int | None:
        """Check if a replied-to message is a wine edit prompt. Returns wine_id or None."""
        if not reply:
            return None
        text = reply.text or ""
        match = re.search(r"edit:(\d+)", text)
        if match:
            return int(match.group(1))
        html = getattr(reply, "text_html", None) or ""
        match = re.search(r"edit:(\d+)", html)
        if match:
            return int(match.group(1))
        return None

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a photo sent by the user.

        Three modes:
        1. Reply to an edit message -> update existing wine details
        2. Reply to a barcode message / single pending → new wine label flow
        3. No reply, no pending -> try to read barcode from the photo
        """
        api = self._get_api(update)
        logger.info(f"[PHOTO] Received photo from chat {update.message.chat_id}")

        if not self._photo_callback and not self._triage_callback:
            logger.info("[PHOTO] No photo or triage callback configured")
            await update.message.reply_text(
                "Photo received, but label recognition is not configured.\n"
                "Add a Grok API key to enable it."
            )
            return

        reply = update.message.reply_to_message
        photo_data = await self._download_photo(update, context)
        logger.info(f"[PHOTO] Downloaded {len(photo_data)} bytes, reply={reply is not None}")

        # --- Mode 1: Edit wine details (reply to edit message) ---
        edit_wine_id = self._detect_edit_reply(reply)
        if edit_wine_id is not None:
            logger.info(f"[PHOTO] Mode 1: edit wine #{edit_wine_id}")
            await self._handle_edit_photo(update, context, edit_wine_id, photo_data)
            return

        # --- Mode 2: New wine label (reply to barcode message or single pending) ---
        barcode = None
        if reply:
            barcode = _extract_barcode_from_message(reply)
            if barcode and not self._is_pending(barcode):
                await update.message.reply_text(
                    f"Barcode <code>{barcode}</code> was already handled or expired.\n"
                    f"Scan a new barcode to try again.",
                    parse_mode="HTML",
                )
                return

        if not barcode:
            with self._lock:
                active = _active_pendings(self.pending_store)
            if len(active) == 1:
                barcode = next(iter(active))
            elif len(active) > 1:
                lines = [
                    "📸 Multiple barcodes are pending — I don't know which "
                    "wine this photo is for!\n\n"
                    "<b>Reply to the specific barcode message</b> with your photo.\n\n"
                    "Pending barcodes:"
                ]
                for bc, pdata in active.items():
                    name = ""
                    off = pdata.get("off_data")
                    if off and off.get("name"):
                        name = f" — {off['name']}"
                    lines.append(f"  <code>{bc}</code>{name}")
                await update.message.reply_text("\n".join(lines), parse_mode="HTML")
                return

        # If we have a barcode, do the label analysis flow
        if barcode:
            logger.info(f"[PHOTO] Mode 2: label photo for barcode={barcode}")
            await self._handle_label_photo(update, context, barcode, photo_data)
            return

        # --- Mode 3: No barcode context — triage the photo ---
        logger.info(f"[PHOTO] Mode 3: no barcode context, photo size={len(photo_data)} bytes")

        # Step 1: Try pyzbar locally (free, instant)
        loop = asyncio.get_running_loop()
        detected = None
        if self._local_barcode_reader:
            logger.info("[PHOTO] Step 1: trying pyzbar local barcode reader...")
            try:
                detected = await loop.run_in_executor(
                    None, functools.partial(self._local_barcode_reader, photo_data),
                )
            except Exception as e:
                logger.warning(f"[PHOTO] pyzbar failed: {e}")

            if detected:
                logger.info(f"[PHOTO] pyzbar SUCCESS: barcode={detected}")
            else:
                logger.info("[PHOTO] pyzbar: no barcode found")
        else:
            logger.info("[PHOTO] pyzbar not available, skipping")

        if detected:
            status_msg = await update.message.reply_text(
                f"📷 Barcode detected: <code>{detected}</code>\nLooking up...",
                parse_mode="HTML",
            )
            await self._handle_barcode_result(status_msg, detected)
            return

        # Step 2: pyzbar didn't find anything — ask Grok to triage
        if not self._triage_callback:
            logger.info("[PHOTO] No triage callback (no Grok API key)")
            await update.message.reply_text(
                "📸 Send a barcode or wine label photo.\n"
                "Vision processing is not configured (no Grok API key)."
            )
            return

        logger.info("[PHOTO] Step 2: sending to Grok triage...")
        status_msg = await update.message.reply_text("📷 Analysing photo...")
        try:
            result = await loop.run_in_executor(
                None, functools.partial(self._triage_callback, photo_data),
            )
        except Exception as e:
            logger.error(f"[PHOTO] Grok triage exception: {e}")
            await status_msg.edit_text(f"Error analysing photo: {e}")
            return

        if not result:
            logger.error("[PHOTO] Grok triage returned None")
            await status_msg.edit_text("Error: could not analyse the photo. Try again.")
            return

        photo_type = result.get("type", "other")
        logger.info(f"[PHOTO] Grok triage result: type={photo_type}")

        # --- Grok says it's a barcode ---
        if photo_type == "barcode":
            barcode_digits = result.get("barcode", "")
            logger.info(f"[PHOTO] Grok read barcode: {barcode_digits}")
            if barcode_digits:
                await status_msg.edit_text(
                    f"📷 Barcode detected: <code>{barcode_digits}</code>\nLooking up...",
                    parse_mode="HTML",
                )
                await self._handle_barcode_result(status_msg, barcode_digits)
                return
            logger.warning("[PHOTO] Grok detected barcode but digits invalid")
            await status_msg.edit_text("Detected a barcode but couldn't read the number. Try a clearer photo.")
            return

        # --- Grok says it's a wine label ---
        if photo_type == "label":
            logger.info(f"[PHOTO] Grok label analysis: {result.get('name', '?')}")
            await self._handle_triage_label(status_msg, result, photo_data)
            return

        # --- Neither ---
        logger.info(f"[PHOTO] Grok says: not a barcode or label (type={photo_type})")
        await status_msg.edit_text(
            "That doesn't look like a barcode or wine label.\n\n"
            "Send a photo of a barcode to scan, or a wine label for details."
        )

    async def _handle_barcode_result(self, status_msg, barcode: str):
        """Handle a barcode that was read from a photo (pyzbar or Grok).
        Looks up in DB and shows appropriate response."""
        logger.info(f"[BARCODE] Looking up barcode={barcode} in Wine Cellar DB...")
        try:
            wines = api.lookup_barcode(barcode)
        except Exception as e:
            logger.error(f"[BARCODE] DB lookup failed: {e}")
            await status_msg.edit_text(f"Error looking up barcode: {e}")
            return

        logger.info(f"[BARCODE] Found {len(wines)} wine(s) for barcode={barcode}")

        if len(wines) == 0:
            # Unknown — start new wine flow, ask for label photo
            off_data = None
            try:
                from integrations.open_food_facts import OpenFoodFactsAPI
                logger.info(f"[BARCODE] Barcode unknown — querying Open Food Facts...")
                off_data = OpenFoodFactsAPI().lookup(barcode)
                if off_data:
                    logger.info(f"[BARCODE] OFF found: {off_data.get('name', '?')} by {off_data.get('producer', '?')}")
                else:
                    logger.info("[BARCODE] OFF returned no results")
            except Exception as e:
                logger.warning(f"[BARCODE] OFF lookup failed: {e}")
            self._create_pending(barcode, off_data=off_data)

            text = f"❓ <b>Unknown barcode: <code>{barcode}</code></b>\n\n"
            if off_data and off_data.get("name"):
                text += (
                    f"OFF found: <b>{off_data['name']}</b>\n"
                    f"Producer: {off_data.get('producer', 'Unknown')}\n\n"
                )
            text += (
                "📸 <b>Reply to this message</b> with a label photo "
                "and I'll extract the full details + tasting notes."
            )
            keyboard = [
                [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
                [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
            ]
            if off_data and off_data.get("name"):
                keyboard.insert(0, [InlineKeyboardButton(
                    "✅ Add with OFF data (no photo)",
                    callback_data=_cb("off", barcode=barcode),
                )])
            await status_msg.edit_text(
                text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        elif len(wines) == 1:
            wine = wines[0]
            keyboard = [[
                InlineKeyboardButton("📥 Add", callback_data=_cb("in", wine_id=wine["id"])),
                InlineKeyboardButton("📤 Remove", callback_data=_cb("out", wine_id=wine["id"])),
            ]]
            await status_msg.edit_text(
                f"🍷 <b>{wine['name']}</b>\n"
                f"📊 In stock: {wine['stock_count']}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            keyboard = []
            for wine in wines:
                label = f"{wine['name']} {wine.get('vintage', '')} (stock: {wine['stock_count']})"
                keyboard.append([
                    InlineKeyboardButton(f"📥 {label}", callback_data=_cb("in", wine_id=wine["id"])),
                ])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))])
            await status_msg.edit_text(
                "🍷 <b>Multiple wines found — which one?</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    def _find_matching_wine(self, grok_data: dict) -> dict | None:
        """Check if a Grok-analysed wine already exists in stock.
        Returns the matching wine dict or None."""
        grok_name = (grok_data.get("name") or "").lower().strip()
        if not grok_name:
            return None

        try:
            wines = api.list_all_wines()
        except Exception:
            logger.warning("[MATCH] Failed to fetch wines for matching, falling back to in-stock")
            try:
                wines = api.wines_in_stock()
            except Exception:
                return None

        # Try exact-ish match first (name contains or is contained)
        for wine in wines:
            existing = wine["name"].lower().strip()
            if grok_name in existing or existing in grok_name:
                logger.info(f"[MATCH] Grok '{grok_name}' matched existing '{wine['name']}' (id={wine['id']})")
                return wine

        # Try matching on key words (drop common words)
        skip = {"wine", "the", "de", "du", "le", "la", "les", "di", "del", "das", "der", "reserve", "reserva"}
        grok_words = {w for w in grok_name.split() if w not in skip and len(w) > 2}
        if not grok_words:
            return None

        best_match = None
        best_score = 0
        for wine in wines:
            existing_words = {w for w in wine["name"].lower().split() if w not in skip and len(w) > 2}
            overlap = len(grok_words & existing_words)
            if overlap > best_score and overlap >= 2:
                best_score = overlap
                best_match = wine

        if best_match:
            logger.info(f"[MATCH] Grok '{grok_name}' fuzzy-matched '{best_match['name']}' (id={best_match['id']}, score={best_score})")
        return best_match

    async def _handle_triage_label(self, status_msg, grok_data: dict, photo_data: bytes = b""):
        """Handle a photo that Grok identified as a wine label.
        Grok already did the full analysis in the triage call.
        Checks for existing wines before creating a new one."""
        # Check if there's a single pending barcode to attach
        barcode = ""
        with self._lock:
            active = _active_pendings(self.pending_store)
            if len(active) == 1:
                barcode = next(iter(active))

        if barcode:
            # Store Grok data for confirmation, linked to the pending barcode
            with self._lock:
                if barcode in self.pending_store:
                    self.pending_store[barcode]["grok_merged"] = grok_data
                    if photo_data:
                        self.pending_store[barcode]["photo_data"] = photo_data

            text = "🔍 <b>Label analysis complete!</b>\n\n"
            text += _grok_summary(grok_data)
            text += f"\nBarcode: <code>{barcode}</code>"

            keyboard = [
                [InlineKeyboardButton("✅ Confirm & add to cellar", callback_data=_cb("grok", barcode=barcode))],
                [InlineKeyboardButton("📸 Try another photo", callback_data=_cb("retry", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually instead", callback_data=_cb("man", barcode=barcode))],
            ]

            await status_msg.edit_text(
                text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # No pending barcode — check if this wine already exists
        existing = self._find_matching_wine(grok_data)

        # Store Grok data under a temp key for the confirm buttons
        temp_key = f"_label_{id(grok_data)}"
        with self._lock:
            entry = {
                "grok_merged": grok_data,
                "time": time.time(),
            }
            if photo_data:
                entry["photo_data"] = photo_data
            self.pending_store[temp_key] = entry

        text = "🔍 <b>Label analysis complete!</b>\n\n"
        text += _grok_summary(grok_data)

        if existing:
            # Found a match — ask if they want to update or add new
            logger.info(f"[TRIAGE] Label matches existing wine: {existing['name']} (id={existing['id']})")
            text += f"\n🍷 This looks like <b>{existing['name']}</b> already in your cellar ({existing['stock_count']}x)."

            # Store the edit data for the confirm button
            edit_key = f"_edit_{existing['id']}"
            with self._lock:
                entry = {
                    "wine_id": existing["id"],
                    "grok_merged": grok_data,
                    "time": time.time(),
                }
                if photo_data:
                    entry["photo_data"] = photo_data
                self.pending_store[edit_key] = entry

            keyboard = [
                [InlineKeyboardButton(
                    f"✏️ Update {existing['name']}",
                    callback_data=_cb("edc", wine_id=existing["id"]),
                )],
                [InlineKeyboardButton("📥 Add stock (+1)", callback_data=_cb("in", wine_id=existing["id"]))],
                [InlineKeyboardButton("🆕 No, it's a different wine", callback_data=_cb("grok", barcode=temp_key))],
                [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
            ]
        else:
            # No match — offer to create new
            text += "\n(No barcode attached — wine will be created without one)"
            keyboard = [
                [InlineKeyboardButton("✅ Add to cellar", callback_data=_cb("grok", barcode=temp_key))],
                [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
            ]

        await status_msg.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _handle_label_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   barcode: str, photo_data: bytes):
        """Process a label photo for a new wine (reply to barcode message)."""
        status_msg = await update.message.reply_text(
            f"📸 Analysing label for <code>{barcode}</code>...",
            parse_mode="HTML",
        )

        loop = asyncio.get_running_loop()
        try:
            grok_data = await loop.run_in_executor(
                None,
                functools.partial(self._photo_callback, barcode, photo_data),
            )
        except Exception as e:
            await status_msg.edit_text(f"Error processing photo: {e}")
            return

        if not grok_data:
            keyboard = [
                [InlineKeyboardButton("📸 Try another photo", callback_data=_cb("retry", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
                [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
            ]
            with self._lock:
                off_data = self.pending_store.get(barcode, {}).get("off_data")
            if off_data and off_data.get("name"):
                keyboard.insert(1, [InlineKeyboardButton(
                    "✅ Add with OFF data instead",
                    callback_data=_cb("off", barcode=barcode),
                )])

            await status_msg.edit_text(
                "Could not extract wine details from that photo.\n\n"
                "Try a clearer photo of the front label, or choose an option below.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # Store Grok data and photo for confirmation
        with self._lock:
            if barcode in self.pending_store:
                self.pending_store[barcode]["grok_merged"] = grok_data
                self.pending_store[barcode]["photo_data"] = photo_data

        text = "🔍 <b>Label analysis complete!</b>\n\n"
        text += _grok_summary(grok_data)
        text += f"\nBarcode: <code>{barcode}</code>"

        keyboard = [
            [InlineKeyboardButton("✅ Confirm & add to cellar", callback_data=_cb("grok", barcode=barcode))],
            [InlineKeyboardButton("📸 Try another photo", callback_data=_cb("retry", barcode=barcode))],
            [InlineKeyboardButton("✏️ Add manually instead", callback_data=_cb("man", barcode=barcode))],
        ]

        await status_msg.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    async def _handle_edit_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  wine_id: int, photo_data: bytes):
        """Process a label photo to update an existing wine's details."""
        api = self._get_api(update)
        status_msg = await update.message.reply_text(
            f"📸 Analysing label to update wine #{wine_id}...",
            parse_mode="HTML",
        )

        loop = asyncio.get_running_loop()
        try:
            # Use the photo callback with a dummy barcode — we just need Grok's analysis
            grok_data = await loop.run_in_executor(
                None,
                functools.partial(self._photo_callback, "", photo_data),
            )
        except Exception as e:
            await status_msg.edit_text(f"Error processing photo: {e}")
            return

        if not grok_data:
            await status_msg.edit_text(
                "Could not extract wine details from that photo.\n"
                "Try a clearer photo of the front label."
            )
            return

        # Store for confirmation
        edit_key = f"_edit_{wine_id}"
        with self._lock:
            if edit_key not in self.pending_store:
                self.pending_store[edit_key] = {"wine_id": wine_id, "time": time.time()}
            self.pending_store[edit_key]["grok_merged"] = grok_data

        text = "🔍 <b>Update details:</b>\n\n"
        text += _grok_summary(grok_data)

        keyboard = [
            [InlineKeyboardButton("✅ Confirm update", callback_data=_cb("edc", wine_id=wine_id))],
            [InlineKeyboardButton("📸 Try another photo", callback_data=_cb("ed", wine_id=wine_id))],
            [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
        ]

        await status_msg.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    def run(self):
        """Start the bot polling loop. Blocks forever (standalone mode)."""
        self._app = self._build_app()
        self._app.run_polling(drop_pending_updates=True)

    def _run_in_own_loop(self):
        """Run the bot in its own event loop (for background thread use)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._app = self._build_app()

        async def _start():
            await self._app.initialize()
            await self._app.updater.start_polling(drop_pending_updates=True)
            await self._app.start()

        loop.run_until_complete(_start())
        loop.run_forever()

    def run_in_thread(self):
        """Start the bot in a background thread."""
        thread = threading.Thread(target=self._run_in_own_loop, daemon=True)
        thread.start()
        return thread
