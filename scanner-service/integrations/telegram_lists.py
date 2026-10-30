"""To-drink list Telegram flow.

Wired from telegram_bot like invites. Slash command /list stays English;
dialogue and message buttons use the catalog (EN + AF).
"""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from integrations.copy import t
from integrations.telegram_invites import esc

logger = logging.getLogger(__name__)

LIST_PAGE_SIZE = 5
LIST_ACTIONS = {"td", "lm", "lc", "lx", "ls"}

TYPE_EMOJI = {
    "RE": "\U0001F377",
    "WH": "\U0001F942",
    "RO": "\U0001F339",
    "SP": "\U0001F37E",
    "DE": "\U0001F36F",
    "FO": "\U0001F3FA",
    "OR": "\U0001F34A",
}


def lists_enabled(handler) -> bool:
    flag_on = getattr(handler, "flag_on", None)
    if callable(flag_on):
        return bool(flag_on("feature_lists"))
    return bool((getattr(handler, "flags", None) or {}).get("feature_lists"))


def locale_for(handler, update=None, chat_id=None, language_code=None) -> str:
    getter = getattr(handler, "locale_for", None)
    if callable(getter):
        return getter(update=update, chat_id=chat_id, language_code=language_code)
    from integrations.copy import locale_from_telegram

    user = getattr(update, "effective_user", None) if update is not None else None
    return locale_from_telegram(
        language_code or (getattr(user, "language_code", None) if user else None)
    )


def to_drink_button(handler, chat_id, *, barcode="", wine_id=None, update=None):
    """Inline button, or None when the flag is off."""
    if not lists_enabled(handler):
        return None
    from integrations.telegram_bot import _cb

    loc = locale_for(handler, update=update, chat_id=chat_id)
    return InlineKeyboardButton(
        t(loc, "list.btn.add"),
        callback_data=_cb("td", barcode=barcode, wine_id=wine_id),
    )


def insert_to_drink(keyboard, button):
    if not button:
        return keyboard
    row = [button]
    for i, existing in enumerate(keyboard):
        text = existing[0].text if existing else ""
        if text and "Add to cellar" in text:
            return keyboard[: i + 1] + [row] + keyboard[i + 1 :]
    for i, existing in enumerate(keyboard):
        text = (existing[0].text if existing else "") or ""
        if "wrong wine" in text.lower():
            return keyboard[:i] + [row] + keyboard[i:]
    return keyboard + [row]


def listed_note(handler, chat_id, *, already=False, update=None) -> str:
    loc = locale_for(handler, update=update, chat_id=chat_id)
    key = "list.note.already" if already else "list.note.added"
    return f"\n\n\U0001F4DD <i>{html.escape(t(loc, key), quote=False)}</i>"


def _item_title(item: dict) -> str:
    name = str(item.get("name") or "Wine").strip()
    vintage = item.get("vintage")
    if vintage and str(vintage) not in name:
        name = f"{name} {vintage}".strip()
    return name


def _item_subtitle(item: dict) -> str:
    producer = str(item.get("producer") or "").strip()
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    region = str(
        (payload or {}).get("region") or item.get("grape") or item.get("country") or ""
    ).strip()
    parts = [p for p in (producer, region) if p]
    return " \u00b7 ".join(parts)


def build_list_view(payload: dict, locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    from integrations.telegram_bot import _cb

    title = t(locale, "list.title")
    items = payload.get("items") or []
    count = int(payload.get("count") or 0)
    offset = int(payload.get("offset") or 0)
    limit = int(payload.get("limit") or LIST_PAGE_SIZE)
    page = offset // limit if limit else 0
    if count == 0:
        text = f"\U0001F4DD <b>{esc(title)}</b>\n\n{t(locale, 'list.empty')}"
        return text, None
    start = offset + 1
    end = min(offset + len(items), count)
    lines = [f"\U0001F4DD <b>{esc(title)}</b>  {start}\u2013{end} of {count}", ""]
    keyboard = []
    for i, item in enumerate(items, start=1):
        emoji = TYPE_EMOJI.get(item.get("wine_type") or "", "\U0001F377")
        heading = _item_title(item)
        sub = _item_subtitle(item)
        lines.append(f"{i}. {emoji} {esc(heading)}")
        if sub:
            lines.append(f"    {esc(sub)}")
        item_id = item.get("id")
        if item_id:
            keyboard.append([
                InlineKeyboardButton(
                    t(locale, "list.btn.move"),
                    callback_data=_cb("lc", item_id=item_id),
                ),
                InlineKeyboardButton(
                    t(locale, "list.btn.remove"),
                    callback_data=_cb("lx", item_id=item_id),
                ),
            ])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            t(locale, "list.newer"), callback_data=_cb("lm", page=page - 1),
        ))
    if offset + limit < count:
        nav.append(InlineKeyboardButton(
            t(locale, "list.older"), callback_data=_cb("lm", page=page + 1),
        ))
    if nav:
        keyboard.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(keyboard) if keyboard else None


class ListFlow:
    def __init__(self, handler):
        self.handler = handler

    def locale(self, update=None, chat_id=None) -> str:
        return locale_for(self.handler, update=update, chat_id=chat_id)

    def _api(self, update):
        return self.handler._get_api(update)

    def _personal_list_id(self, api) -> int | None:
        try:
            data = api.list_lists()
        except Exception as e:
            logger.warning("[list] list_lists failed: %s", e)
            return None
        if data.get("error"):
            return None
        lists = data.get("lists") or []
        personal = next((lst for lst in lists if lst.get("kind") == "personal"), None)
        if personal:
            return personal.get("id")
        if lists:
            return lists[0].get("id")
        return None

    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loc = self.locale(update)
        if not lists_enabled(self.handler):
            await update.message.reply_text(t(loc, "list.off"))
            return
        await self._show_page(update, context, page=0)

    async def _show_page(self, update, context, page: int, query=None):
        from integrations.telegram_bot import _safe_edit

        loc = self.locale(update)
        api = self._api(update)
        list_id = self._personal_list_id(api)
        if list_id is None:
            text = t(loc, "list.err")
            if query is not None:
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        try:
            payload = api.get_list(
                list_id, limit=LIST_PAGE_SIZE, offset=max(0, page) * LIST_PAGE_SIZE,
            )
        except Exception as e:
            logger.warning("[list] get_list failed: %s", e)
            payload = {"error": str(e)}
        if payload.get("error"):
            code = payload.get("status_code")
            text = t(loc, "list.off") if code == 404 else t(loc, "list.err")
            if query is not None:
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        text, markup = build_list_view(payload, loc)
        if query is not None:
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=markup,
            )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = update.callback_query
        from integrations.telegram_bot import _parse_cb

        data = _parse_cb(query.data or "")
        action = data.get("action") or ""
        if action not in LIST_ACTIONS:
            return False
        try:
            await query.answer()
        except Exception as e:
            logger.info("[list] answer() failed (likely a stale button): %s", e)
        loc = self.locale(update)
        if not lists_enabled(self.handler):
            try:
                await query.answer()
            except Exception:
                pass
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "list.off"))
            return True
        if action == "td":
            await self._add_from_card(update, context, data)
        elif action == "lm":
            await self._show_page(update, context, page=data.get("page") or 0, query=query)
        elif action == "lc":
            await self._move_to_cellar(update, context, data)
        elif action == "lx":
            await self._remove(update, context, data)
        elif action == "ls":
            await self._move_picked_storage(update, context, data)
        return True

    def _snapshot_from_pending(self, api, barcode: str) -> dict:
        pending = {}
        getter = getattr(self.handler, "_get_pending", None)
        if callable(getter) and barcode:
            pending = getter(api, barcode) or {}
        grok = dict(pending.get("grok_merged") or {})
        if barcode and not str(barcode).startswith("_"):
            grok.setdefault("barcode", barcode)
        return grok

    async def _add_from_card(self, update, context, data):
        from integrations.telegram_bot import _clean_wine_data, _safe_edit

        query = update.callback_query
        loc = self.locale(update)
        api = self._api(update)
        barcode = data.get("barcode") or ""
        wine_id = data.get("wine_id")
        grok = {}
        if barcode:
            getter = getattr(self.handler, "_get_pending", None)
            pending = getter(api, barcode) if callable(getter) else {}
            if pending:
                grok = dict(pending.get("grok_merged") or {})
                if barcode and not str(barcode).startswith("_"):
                    grok.setdefault("barcode", barcode)
            elif not wine_id:
                await _safe_edit(query, t(loc, "list.card.gone"))
                return
        if wine_id and not grok:
            try:
                detail = api.get_wine_detail(wine_id) or {}
            except Exception:
                detail = {}
            grok = {
                "name": detail.get("name") or "",
                "producer": detail.get("producer") or "",
                "vintage": detail.get("vintage"),
                "wine_type": detail.get("wine_type") or "",
                "country": detail.get("country") or "",
                "grape_variety": (detail.get("grapes") or [None])[0]
                    if isinstance(detail.get("grapes"), list) else detail.get("grape") or "",
                "barcode": detail.get("barcode") or "",
            }
        name = str(grok.get("name") or "").strip()
        if not name:
            await _safe_edit(
                query,
                ((query.message.text_html if query.message else "") or "")
                + f"\n\n<i>{esc(t(loc, 'list.need_name'))}</i>",
                parse_mode="HTML",
                reply_markup=query.message.reply_markup if query.message else None,
            )
            return
        list_id = self._personal_list_id(api)
        if list_id is None:
            await query.message.reply_text(t(loc, "list.err")) if query.message else None
            return
        clean = {}
        try:
            clean = _clean_wine_data(grok) if grok else {}
        except Exception:
            clean = {k: v for k, v in grok.items() if v not in (None, "")}
        body = {
            "name": name[:100],
            "producer": str(grok.get("producer") or "")[:100],
            "vintage": grok.get("vintage"),
            "wine_type": str(grok.get("wine_type") or "")[:2],
            "country": str(grok.get("country") or "")[:3],
            "grape": str(grok.get("grape_variety") or grok.get("grape") or "")[:100],
            "barcode": str(grok.get("barcode") or "")[:100],
            "wine_id": wine_id,
            "source": "label" if not (barcode and not str(barcode).startswith("_")) else "barcode",
            "payload": clean,
        }
        try:
            result = api.add_list_item(list_id, body)
        except Exception as e:
            logger.warning("[list] add_list_item failed: %s", e)
            result = {"error": str(e)}
        if result.get("error") and result.get("status_code", 200) >= 400:
            if result.get("status_code") == 404:
                await query.message.reply_text(t(loc, "list.off"))
            else:
                await query.message.reply_text(t(loc, "list.err"))
            return
        already = bool(result.get("already")) or result.get("status_code") == 200
        chat_id = getattr(query.message, "chat_id", None) if query.message else None
        self.handler._touch_label_card(query.message, listed=True, looking=False)
        text = (query.message.text_html if query.message else "") or ""
        from integrations.telegram_bot import REFS_PENDING_NOTE
        text = text.replace(REFS_PENDING_NOTE.strip(), "").rstrip()
        note = listed_note(
            self.handler, chat_id, already=already, update=update,
        )
        try:
            await _safe_edit(query, text + note, parse_mode="HTML")
        except Exception as e:
            logger.info("[list] listed edit failed: %s", e)

    async def _remove(self, update, context, data):
        from integrations.telegram_bot import _safe_edit

        query = update.callback_query
        loc = self.locale(update)
        api = self._api(update)
        item_id = data.get("item_id")
        list_id = self._personal_list_id(api)
        if list_id is None or not item_id:
            await _safe_edit(query, t(loc, "list.err"))
            return
        try:
            result = api.delete_list_item(list_id, item_id)
        except Exception as e:
            logger.warning("[list] delete failed: %s", e)
            result = {"error": str(e), "status_code": 500}
        if result.get("error") and result.get("status_code", 200) >= 400:
            if result.get("error") == "already_moved":
                await _safe_edit(query, t(loc, "list.err"))
            else:
                await _safe_edit(query, t(loc, "list.err"))
            return
        await self._show_page(update, context, page=0, query=query)

    async def _move_to_cellar(self, update, context, data):
        from integrations.telegram_bot import _cb, _safe_edit

        query = update.callback_query
        loc = self.locale(update)
        api = self._api(update)
        item_id = data.get("item_id")
        original = data.get("original") or ""
        list_id = self._personal_list_id(api)
        if list_id is None or not item_id:
            await _safe_edit(query, t(loc, "list.err"))
            return
        if original == "dup":
            await self._finish_move(
                query, api, list_id, item_id, loc, update,
                confirm_duplicate=True,
            )
            return
        if original == "ack":
            await self._finish_move(
                query, api, list_id, item_id, loc, update, ack_duplicate=True,
            )
            return
        try:
            storages = api.list_storages() or []
        except Exception:
            storages = []
        if len(storages) > 1:
            keyboard = []
            for storage in storages:
                label = storage.get("name") or "Storage"
                if storage.get("bottles"):
                    label += f" ({storage['bottles']})"
                keyboard.append([InlineKeyboardButton(
                    label,
                    callback_data=_cb("ls", item_id=item_id, page=storage.get("id") or 0),
                )])
            await _safe_edit(
                query,
                f"\U0001F4CD <b>{esc(t(loc, 'list.where'))}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        storage_name = storages[0]["name"] if storages else None
        await self._finish_move(
            query, api, list_id, item_id, loc, update, storage_name=storage_name,
        )

    async def _move_picked_storage(self, update, context, data):
        from integrations.telegram_bot import _safe_edit

        query = update.callback_query
        loc = self.locale(update)
        api = self._api(update)
        item_id = data.get("item_id")
        storage_id = data.get("page")
        list_id = self._personal_list_id(api)
        if list_id is None or not item_id:
            await _safe_edit(query, t(loc, "list.err"))
            return
        storage_name = None
        try:
            storages = api.list_storages() or []
            match = next((s for s in storages if s.get("id") == storage_id), None)
            storage_name = match["name"] if match else None
        except Exception:
            storage_name = None
        await self._finish_move(
            query, api, list_id, item_id, loc, update, storage_name=storage_name,
        )

    async def _finish_move(
        self, query, api, list_id, item_id, loc, update,
        storage_name=None, confirm_duplicate=False, ack_duplicate=False,
    ):
        from integrations.telegram_bot import _cb, _safe_edit

        try:
            result = api.move_list_item_to_cellar(
                list_id,
                item_id,
                storage_name=storage_name,
                confirm_duplicate=confirm_duplicate,
                ack_duplicate=ack_duplicate,
            )
        except Exception as e:
            logger.warning("[list] to-cellar failed: %s", e)
            await _safe_edit(query, t(loc, "list.err"))
            return
        if result.get("error") == "recent_duplicate":
            wine_id = result.get("wine_id")
            stock_item_id = result.get("item_id")
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    t(loc, "list.dup.add_another"),
                    callback_data=_cb("lc", item_id=item_id, original="dup"),
                )],
                [InlineKeyboardButton(
                    t(loc, "list.dup.thats_the_one"),
                    callback_data=_cb("lc", item_id=item_id, original="ack"),
                )],
            ])
            name = result.get("wine_name") or "that wine"
            await _safe_edit(
                query,
                t(loc, "list.dup.body", name=f"<b>{esc(name)}</b>"),
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        if result.get("error") and result.get("status_code", 200) >= 400:
            err = result.get("error")
            if err == "already_moved":
                await _safe_edit(
                    query, t(loc, "list.moved", name=result.get("wine_name") or "that wine"),
                )
            elif result.get("status_code") == 404:
                await _safe_edit(query, t(loc, "list.off"))
            else:
                await _safe_edit(query, t(loc, "list.err"))
            return
        name = result.get("wine_name") or "that wine"
        text = f"\u2705 {esc(t(loc, 'list.moved', name=name))}"
        storage = result.get("storage") or ""
        if storage:
            text += f"\n\U0001F4CD {esc(storage)}"
        stock = result.get("stock_count")
        if stock is not None:
            text += f"\n\U0001F4CA In stock: {stock}"
        wine_id = result.get("wine_id")
        stock_item_id = result.get("item_id")
        drawers = result.get("drawers") or 0
        keyboard = []
        if drawers and drawers > 0 and stock_item_id and wine_id:
            row_buttons = []
            for d in range(1, drawers + 1):
                row_buttons.append(InlineKeyboardButton(
                    str(d),
                    callback_data=_cb("sd", item_id=stock_item_id, row=d, wine_id=wine_id),
                ))
                if len(row_buttons) == 4:
                    keyboard.append(row_buttons)
                    row_buttons = []
            if row_buttons:
                keyboard.append(row_buttons)
            keyboard.append([InlineKeyboardButton(
                "Skip",
                callback_data=_cb("sd", item_id=stock_item_id, row=-1, wine_id=wine_id),
            )])
            text += f"\n\n\U0001F5C4 <b>{esc(t(loc, 'list.drawer'))}</b>"
        elif wine_id:
            keyboard.append([InlineKeyboardButton(
                "\u21a9 Undo", callback_data=_cb("ua", wine_id=wine_id),
            )])
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        achievements = result.get("achievements")
        chat_id = getattr(query.message, "chat_id", None) if query.message else None
        if achievements and chat_id:
            celebrate = getattr(self.handler, "_celebrate", None)
            if callable(celebrate):
                try:
                    await celebrate(None, chat_id, result)
                except Exception as e:
                    logger.info("[list] celebrate failed: %s", e)
