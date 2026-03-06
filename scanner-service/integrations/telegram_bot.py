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
    """Return only non-stale, non-resolved pending barcodes."""
    now = time.time()
    return {
        bc: data for bc, data in pending_store.items()
        if not data.get("resolved")
        and (now - data.get("time", 0)) < PENDING_TIMEOUT_SECONDS
    }


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
    if data.get("grape_variety"):
        text += f"Grape: {data['grape_variety']}\n"
    if data.get("abv"):
        text += f"ABV: {data['abv']}%\n"
    if data.get("description"):
        text += f"\n<i>{data['description']}</i>\n"
    return text


class TelegramNotifier:
    """Handles sending notifications to Telegram. Broadcasts to all configured chat IDs."""

    def __init__(self, bot_token: str, chat_ids: list[str]):
        self.bot = Bot(token=bot_token)
        self.chat_ids = chat_ids
        self._loop = None
        self._lock = threading.Lock()

    def _get_loop(self):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coro):
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    def _send_to_all(self, send_func):
        """Call send_func(chat_id) for each configured chat ID."""
        with self._lock:
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
                f"📸 <b>Reply to this message</b> with a label photo "
                f"for full details + tasting notes."
            )
            keyboard = [
                [InlineKeyboardButton("✅ Add with OFF data (no photo)", callback_data=_cb("off", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
                [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
            ]
        else:
            text = (
                f"❓ <b>Unknown barcode: <code>{barcode}</code></b>\n\n"
                f"Not found in Open Food Facts.\n\n"
                f"📸 <b>Reply to this message</b> with a label photo "
                f"and I'll extract the details + tasting notes."
            )
            keyboard = [
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
        pending_lock: threading.Lock | None = None,
    ):
        self.bot_token = bot_token
        self.chat_ids = [str(cid) for cid in chat_ids]
        self.api = wine_cellar_api
        self.pending_store = pending_store
        self._lock = pending_lock or threading.Lock()
        self._app = None
        self._photo_callback = None
        self._barcode_callback = None

    def set_photo_callback(self, callback: Callable):
        """Set callback for when a photo is received. Called with (barcode, photo_bytes).

        Should return a dict of extracted wine data (from Grok), or None on failure.
        """
        self._photo_callback = callback

    def set_barcode_callback(self, callback: Callable):
        """Set callback for reading a barcode from a photo. Called with (photo_bytes).

        Should return the barcode string, or None on failure.
        """
        self._barcode_callback = callback

    def _build_app(self) -> Application:
        app = Application.builder().token(self.bot_token).build()

        user_filter = filters.Chat(chat_id=[int(cid) for cid in self.chat_ids])

        app.add_handler(CommandHandler("status", self._cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("stock", self._cmd_stock, filters=user_filter))
        app.add_handler(CommandHandler("fridge", self._cmd_fridge, filters=user_filter))
        app.add_handler(CommandHandler("recent", self._cmd_recent, filters=user_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=user_filter))
        app.add_handler(CommandHandler("pending", self._cmd_pending, filters=user_filter))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        app.add_handler(MessageHandler(filters.PHOTO & user_filter, self._handle_photo))

        return app

    # --- Commands ---

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "🍷 <b>Wine Fridge Scanner</b>\n\n"
            "/fridge — What's in the fridge (adjust stock)\n"
            "/status — Inventory summary\n"
            "/stock — All wines in stock\n"
            "/recent — Last 10 changes\n"
            "/pending — Show pending barcodes\n"
            "/help — This message\n\n"
            "📸 <b>Reply to a barcode message</b> with a label photo to add a new wine.\n"
            "📸 <b>Reply to a wine edit message</b> with a label photo to update details.\n"
            "📷 Send a <b>barcode photo</b> (no reply) to scan without the hardware scanner."
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await update.message.reply_text(
                "📊 Use the Wine Cellar web UI for full inventory.\n"
                f"🔗 {self.api.web_url}",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📊 Use the Wine Cellar web UI for full stock list.\n"
            f"🔗 {self.api.web_url}/wines/",
            parse_mode="HTML",
        )

    async def _cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 Use the Wine Cellar web UI for history.\n"
            f"🔗 {self.api.web_url}/storage/history",
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

    FRIDGE_PAGE_SIZE = 8

    async def _cmd_fridge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show what's in the fridge with stock adjustment buttons.

        /fridge          — pick a storage if >1, else show default
        /fridge <name>   — show specific storage
        /fridge all      — show all storages combined
        """
        arg = " ".join(context.args).strip() if context.args else ""

        try:
            storages = self.api.list_storages()
        except Exception as e:
            await update.message.reply_text(f"Error loading storages: {e}")
            return

        if not storages:
            await update.message.reply_text("No storages found. Scan a bottle first!")
            return

        # If a specific storage was requested, show it
        if arg and arg.lower() != "all":
            await self._show_fridge(update, storage_name=arg)
            return

        # "all" or only one storage — show everything
        if arg.lower() == "all" or len(storages) == 1:
            await self._show_fridge(update, storage_name="")
            return

        # Multiple storages — let user pick
        keyboard = []
        for s in storages:
            label = f"{s['name']} ({s['bottles']} bottles)"
            # Use 'o' field to pass storage name (compact encoding)
            keyboard.append([InlineKeyboardButton(
                label, callback_data=_cb("fs", original=s["name"]),
            )])
        keyboard.append([InlineKeyboardButton(
            "All storages", callback_data=_cb("fs", original="__all__"),
        )])
        await update.message.reply_text(
            "🍷 <b>Which fridge?</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    async def _show_fridge(self, update_or_query, storage_name: str = "", page: int = 0):
        """Load wines and display fridge page. Works with both Update and CallbackQuery."""
        try:
            wines = self.api.wines_in_stock(storage_name=storage_name)
        except Exception as e:
            text = f"Error loading fridge: {e}"
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        if not wines:
            text = "🍷 The fridge is empty!"
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        text, keyboard = self._build_fridge_page(wines, page=page, storage_name=storage_name)
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await update_or_query.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

    def _build_fridge_page(self, wines: list[dict], page: int = 0, storage_name: str = ""):
        """Build a fridge inventory page with +/- buttons per wine."""
        total = len(wines)
        start = page * self.FRIDGE_PAGE_SIZE
        end = min(start + self.FRIDGE_PAGE_SIZE, total)
        page_wines = wines[start:end]
        total_pages = (total + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE

        type_emoji = {
            "RE": "🔴", "WH": "⚪", "RO": "🌸", "SP": "🫧",
            "DE": "🍯", "FO": "🏰", "OR": "🟠",
        }

        title = storage_name or "All storages"
        total_bottles = sum(w["stock_count"] for w in wines)
        lines = [f"🍷 <b>{title}</b> — {total} wines, {total_bottles} bottles\n"]
        keyboard = []

        for wine in page_wines:
            emoji = type_emoji.get(wine.get("wine_type", ""), "🍷")

            # Build drawer info from items
            items = wine.get("items", [])
            drawer_parts = []
            for item in items:
                d = item.get("drawer")
                s = item.get("storage", "")
                if d is not None:
                    drawer_parts.append(f"D{d}")
                else:
                    drawer_parts.append("?")
                # If showing all storages, prefix with storage name
                if not storage_name and s:
                    drawer_parts[-1] = f"{s}:{drawer_parts[-1]}"

            drawer_str = ", ".join(drawer_parts) if drawer_parts else ""
            location_hint = f" [{drawer_str}]" if drawer_str else ""

            lines.append(f"{emoji} <b>{wine['name']}</b> — {wine['stock_count']}x{location_hint}")
            keyboard.append([
                InlineKeyboardButton("➖", callback_data=_cb("-", wine_id=wine["id"], page=page)),
                InlineKeyboardButton(f"{wine['stock_count']}x", callback_data=_cb("sq", wine_id=wine["id"], page=page)),
                InlineKeyboardButton("➕", callback_data=_cb("+", wine_id=wine["id"], page=page)),
                InlineKeyboardButton("📦", callback_data=_cb("dw", wine_id=wine["id"])),
                InlineKeyboardButton("✏️", callback_data=_cb("ed", wine_id=wine["id"])),
            ])

        # Pagination
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=_cb("fp", page=page - 1)))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("x")))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ➡️", callback_data=_cb("fp", page=page + 1)))
            keyboard.append(nav)

        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    # --- Helpers ---

    def _build_drawer_view(self, wine_id: int):
        """Build the drawer assignment view for a specific wine.

        Returns (text, InlineKeyboardMarkup) or (None, None) if wine not found.
        """
        wines = self.api.wines_in_stock()
        wine = next((w for w in wines if w["id"] == wine_id), None)
        if not wine:
            return None, None

        # Get drawer count from storage info
        storages = {}
        try:
            for s in self.api.list_storages():
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

        data = _parse_cb(query.data)
        action = data.get("action", "")

        # Fridge +/- use custom answer toasts; all others answer immediately
        if action not in ("+", "-", "mv", "ss"):
            await query.answer()

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

        # --- Stock add/remove (from disambiguation) ---
        if action in ("in", "out"):
            wine_id = data["wine_id"]
            try:
                if action == "in":
                    result = self.api.add_stock(wine_id)
                else:
                    result = self.api.remove_stock(wine_id)

                if "error" in result:
                    await query.edit_message_text(f"⚠️ {result['error']}")
                else:
                    emoji = "📥" if action == "in" else "📤"
                    undo_action = "ua" if action == "in" else "ur"
                    keyboard = [[InlineKeyboardButton(
                        "↩️ Undo",
                        callback_data=_cb(undo_action, wine_id=wine_id),
                    )]]
                    await query.edit_message_text(
                        f"{emoji} <b>{'Added' if action == 'in' else 'Removed'}:</b> "
                        f"{result['wine_name']}\n"
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
                result = self.api.remove_stock(wine_id)
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
                result = self.api.add_stock(wine_id)
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
                    self.api.remove_stock(wine_id)
                else:
                    self.api.add_stock(wine_id)
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
                result = self.api.create_wine(wine_data)
                stock = self.api.add_stock(result["id"])
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
                f"🔗 {self.api.web_url}/wine/add/{barcode}",
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
                merged["barcode"] = barcode
                result = self.api.create_wine(merged)
                stock = self.api.add_stock(result["id"])
                self._resolve_pending(barcode)
                await query.edit_message_text(
                    f"🍷 <b>Wine added:</b> {result['name']}\n"
                    f"📊 In stock: {stock['stock_count']}",
                    parse_mode="HTML",
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

        # --- Fridge: storage select ---
        elif action == "fs":
            storage_name = data.get("original", "")
            if storage_name == "__all__":
                storage_name = ""
            await self._show_fridge(query, storage_name=storage_name)

        # --- Fridge: page navigation ---
        elif action == "fp":
            page = data.get("page", 0)
            try:
                wines = self.api.wines_in_stock()
            except Exception as e:
                await query.edit_message_text(f"Error loading fridge: {e}")
                return
            if not wines:
                await query.edit_message_text("🍷 The fridge is empty!")
                return
            text, keyboard = self._build_fridge_page(wines, page=page)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")

        # --- Fridge: +1 stock ---
        elif action == "+":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                result = self.api.add_stock(wine_id)
                await query.answer(f"Added: {result['wine_name']} ({result['stock_count']}x)")
                # Refresh the fridge view, staying on the same page
                wines = self.api.wines_in_stock()
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
                result = self.api.remove_stock(wine_id)
                if "error" in result:
                    await query.answer(result["error"], show_alert=True)
                    return
                await query.answer(f"Removed: {result['wine_name']} ({result['stock_count']}x)")
                # Refresh the fridge view, staying on the same page
                wines = self.api.wines_in_stock()
                if not wines:
                    await query.edit_message_text("🍷 The fridge is empty!")
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
                text, keyboard = self._build_drawer_view(wine_id)
                if text:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text("Wine not found in stock.")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")

        # --- Move item to drawer ---
        elif action == "mv":
            item_id = data["item_id"]
            row = data["row"]
            try:
                result = self.api.move_item(item_id, row=row)
                await query.answer(f"Moved to drawer {row}")
                wine_id = result.get("wine_id")
                if wine_id:
                    text, keyboard = self._build_drawer_view(wine_id)
                    if text:
                        await query.edit_message_text(
                            text, reply_markup=keyboard, parse_mode="HTML",
                        )
                        return
                # Fallback: go back to fridge
                wines = self.api.wines_in_stock()
                text, kb = self._build_fridge_page(wines, page=0)
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: set quantity (show picker) ---
        elif action == "sq":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                wines = self.api.wines_in_stock()
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
                result = self.api.set_stock(wine_id, quantity)
                await query.answer(
                    f"{result['wine_name']}: set to {result['stock_count']}x"
                )
                # Back to fridge
                wines = self.api.wines_in_stock()
                if not wines:
                    await query.edit_message_text("🍷 The fridge is empty!")
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
                result = self.api.update_wine(wine_id, {})  # GET-like — just fetch current data
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
                result = self.api.update_wine(wine_id, grok_data)
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
        1. Reply to an edit message → update existing wine details
        2. Reply to a barcode message / single pending → new wine label flow
        3. No reply, no pending → try to read barcode from the photo
        """
        if not self._photo_callback:
            await update.message.reply_text(
                "Photo received, but label recognition is not configured.\n"
                "Add a Grok API key to enable it."
            )
            return

        reply = update.message.reply_to_message
        photo_data = await self._download_photo(update, context)

        # --- Mode 1: Edit wine details (reply to edit message) ---
        edit_wine_id = self._detect_edit_reply(reply)
        if edit_wine_id is not None:
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
            await self._handle_label_photo(update, context, barcode, photo_data)
            return

        # --- Mode 3: No barcode context — try to read barcode from photo ---
        if self._barcode_callback:
            status_msg = await update.message.reply_text("📷 Reading barcode from photo...")
            loop = asyncio.get_running_loop()
            try:
                detected = await loop.run_in_executor(
                    None, functools.partial(self._barcode_callback, photo_data),
                )
            except Exception as e:
                await status_msg.edit_text(f"Error reading barcode: {e}")
                return

            if detected:
                await status_msg.edit_text(
                    f"📷 Barcode detected: <code>{detected}</code>\n"
                    f"Looking up...",
                    parse_mode="HTML",
                )
                # Look up the barcode just like the scanner would
                try:
                    wines = self.api.lookup_barcode(detected)
                except Exception as e:
                    await status_msg.edit_text(f"Error looking up barcode: {e}")
                    return

                if len(wines) == 0:
                    # Unknown barcode — start the new wine flow
                    self._create_pending(detected)
                    await status_msg.edit_text(
                        f"❓ <b>Unknown barcode: <code>{detected}</code></b>\n\n"
                        f"📸 <b>Reply to this message</b> with a label photo "
                        f"and I'll extract the details + tasting notes.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=detected))],
                            [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=detected))],
                        ]),
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
                return

            await status_msg.edit_text(
                "Could not read a barcode from that photo.\n\n"
                "Tips:\n"
                "• Make sure the barcode numbers are visible\n"
                "• Try a closer, well-lit photo\n"
                "• Or scan with the hardware scanner instead"
            )
            return

        # No barcode callback — generic message
        await update.message.reply_text(
            "📸 Nice photo, but no barcode is waiting for it!\n\n"
            "Scan a barcode first, then reply to the notification "
            "with a label photo."
        )

    async def _handle_label_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   barcode: str, photo_data: bytes):
        """Process a label photo for a new wine."""
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

        # Store Grok data for confirmation
        with self._lock:
            if barcode in self.pending_store:
                self.pending_store[barcode]["grok_merged"] = grok_data

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
        """Start the bot polling loop. Blocks forever."""
        self._app = self._build_app()
        self._app.run_polling(drop_pending_updates=True)

    def run_in_thread(self):
        """Start the bot in a background thread."""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread
