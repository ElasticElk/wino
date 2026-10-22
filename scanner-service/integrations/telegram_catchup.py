"""Staged fridge catch-up: walk a drawer, Apply item_ids, one Undo.

Entered from /fridge, not a slash command. Photos and chat stock writes
are out of scope — leftover mode plus missing-as-drunk is how you wreck
the cellar.

Buttons name the bottle (wine_id / item_id), never a list position: the list
shrinks after every tap, so an index from a double-tap or an older message
would land on a different wine.
"""

from __future__ import annotations

import html
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from integrations.card_store import CardStore, StoreError

logger = logging.getLogger(__name__)

ACTION = "cu"
SESSION_TTL = 1800
PAGE_SIZE = 6
PREFIX = "whiney:catchup:"
# Bottles in a drawer fridge with no drawer recorded (fridge + adds them so).
NO_DRAWER = 0
STAGED_SHOWN = 8

EMPTY_HERE = "This drawer is empty here"
EMPTY_HERE_RACK = "These bottles aren't here"
DRAWER_FINE = "This drawer is fine"
RACK_FINE = "All here"
UNDO_TAP = "Undo last tap"
NOT_SURE = "Not sure which drawer"
CATCHUP_LABEL = "Catch up"
OPS_CAP = 30
CAP_NOTICE = "Apply these first — 30 is the most in one go."
CHANGED_NOTICE = "That changed — check the list again."
APPLY_ERRORS = {
    "duplicate_item": "I had the same bottle twice. Nothing was applied.",
    "storage_not_found": "That fridge isn't in this cellar any more. Nothing was applied.",
    "cap_exceeded": "That's more than 30 bottles at once. Apply a smaller batch.",
    "row_not_allowed": "That fridge doesn't have that drawer. Nothing was applied.",
}
UNDO_ERRORS = {
    "too_late": "Too late to undo — that was over 10 minutes ago, or it's already undone.",
    "stale_item": "Someone already changed one of those bottles. Nothing was undone.",
    "already_restored": "One of those bottles was already put back. Nothing was undone.",
}
UNREACHABLE = "Couldn't reach the cellar. Nothing was applied."
UNDO_UNREACHABLE = "Couldn't reach the cellar. Nothing was undone."


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _in_drawer(item: dict, drawer) -> bool:
    if drawer is None:
        return True
    if drawer == NO_DRAWER:
        return item.get("drawer") is None
    return item.get("drawer") == drawer


def items_for_place(wines: list[dict], storage_name: str, drawer) -> list[dict]:
    """Wine groups whose live items sit in this fridge and drawer.

    drawer None is the whole fridge (a rack); NO_DRAWER is bottles with no row.
    """
    rows = []
    for wine in wines:
        hits = []
        for item in wine.get("items") or []:
            if storage_name and (item.get("storage") or "") != storage_name:
                continue
            if not _in_drawer(item, drawer):
                continue
            if item.get("item_id") is None:
                continue
            hits.append(item)
        if hits:
            rows.append({
                "wine_id": wine.get("id"),
                "name": wine.get("name") or "a bottle",
                "producer": wine.get("producer") or "",
                "wine_type": wine.get("wine_type") or "",
                "items": hits,
            })
    return rows


def unstaged_groups(rows: list[dict], ops: list[dict]) -> list[dict]:
    taken = {op["item_id"] for op in ops}
    groups = []
    for row in rows:
        left = [item for item in row["items"] if item["item_id"] not in taken]
        if left:
            groups.append({**row, "items": left})
    return groups


def drawer_counts(wines: list[dict], storage_name: str, drawers: int) -> dict[int, int]:
    """Bottles per drawer, plus NO_DRAWER for bottles with no row."""
    counts: dict[int, int] = {d: 0 for d in range(1, drawers + 1)}
    counts[NO_DRAWER] = 0
    for wine in wines:
        for item in wine.get("items") or []:
            if storage_name and (item.get("storage") or "") != storage_name:
                continue
            drawer = item.get("drawer")
            if drawer is None:
                counts[NO_DRAWER] += 1
            elif isinstance(drawer, int) and drawer in counts:
                counts[drawer] += 1
    return counts


def stage_count(group: dict, count: int, op: str,
                dest_storage=None, dest_row=None) -> list[dict]:
    chosen = list(group.get("items") or [])[: max(0, count)]
    return stage_items(group, chosen, op, dest_storage=dest_storage, dest_row=dest_row)


def stage_items(group: dict, items: list[dict], op: str,
                dest_storage=None, dest_row=None, tap: int = 0) -> list[dict]:
    staged = []
    for item in items:
        entry = {
            "op": op,
            "item_id": item["item_id"],
            "expected_storage": item.get("storage") or "",
            "expected_row": item.get("drawer"),
            "wine_name": group.get("name") or "a bottle",
            "tap": tap,
        }
        if op == "move":
            entry["storage_name"] = dest_storage
            entry["row"] = dest_row
        staged.append(entry)
    return staged


def api_ops(ops: list[dict]) -> list[dict]:
    """Strip display-only keys before POST /api/stock/bulk/."""
    payload = []
    for op in ops:
        row = {
            "op": op["op"],
            "item_id": op["item_id"],
            "expected_storage": op.get("expected_storage") or "",
            "expected_row": op.get("expected_row"),
        }
        if op["op"] == "move":
            row["storage_name"] = op.get("storage_name")
            row["row"] = op.get("row")
        payload.append(row)
    return payload


def slim_wines(wines: list[dict]) -> list[dict]:
    """Keep the fields the walk needs — not critic scores and pairings."""
    slim = []
    for wine in wines or []:
        slim.append({
            "id": wine.get("id"),
            "name": wine.get("name") or "a bottle",
            "producer": wine.get("producer") or "",
            "wine_type": wine.get("wine_type") or "",
            "items": [
                {
                    "item_id": item.get("item_id"),
                    "storage": item.get("storage") or "",
                    "drawer": item.get("drawer"),
                }
                for item in (wine.get("items") or [])
                if item.get("item_id") is not None
            ],
        })
    return slim


def extend_ops(ops: list[dict], staged: list[dict]) -> int:
    """Append staged ops, skipping item_ids already in the batch."""
    taken = {op["item_id"] for op in ops}
    added = 0
    for entry in staged:
        if entry["item_id"] in taken:
            continue
        if len(ops) >= OPS_CAP:
            break
        ops.append(entry)
        taken.add(entry["item_id"])
        added += 1
    return added


def still_valid(ops: list[dict], wines: list[dict]) -> tuple[list[dict], int]:
    """Re-diff staged ops against fresh stock. Returns (kept, dropped)."""
    live = {}
    for wine in wines:
        for item in wine.get("items") or []:
            if item.get("item_id") is not None:
                live[item["item_id"]] = item
    kept = []
    for op in ops:
        item = live.get(op["item_id"])
        if item is None:
            continue
        if (item.get("storage") or "") != (op.get("expected_storage") or ""):
            continue
        if item.get("drawer") != op.get("expected_row"):
            continue
        kept.append(op)
    return kept, len(ops) - len(kept)


def drawer_label(drawer) -> str:
    if drawer == NO_DRAWER:
        return "No drawer"
    return f"D{drawer}"


def _where(storage: str, row) -> str:
    if row:
        return f"{_esc(storage)} D{row}"
    return _esc(storage)


def op_line(op: dict) -> str:
    name = _esc(op.get("wine_name") or "a bottle")
    if op.get("op") == "remove":
        return f"Drunk · {name}"
    where = _where(op.get("storage_name") or "", op.get("row"))
    return f"Moved · {name} → {where}" if where else f"Moved · {name}"


def apply_summary(ops: list[dict], n: int) -> str:
    lines = [f"Applied {_plural(n, 'change')}."]
    for op in ops[:12]:
        lines.append(op_line(op))
    extra = len(ops) - 12
    if extra > 0:
        lines.append(f"…and {extra} more")
    lines.append("")
    lines.append("Undo works for 10 minutes.")
    return "\n".join(lines)


def _cb(sub: str, page: int | None = None, row: int | None = None,
        wine_id: int | None = None, item_id: int | None = None,
        batch: str = "") -> str:
    from integrations.telegram_bot import _cb as make
    return make(
        ACTION, original=sub, page=page, row=row, wine_id=wine_id,
        item_id=item_id, barcode=batch,
    )


def _safe_edit(*args, **kwargs):
    from integrations.telegram_bot import _safe_edit as edit
    return edit(*args, **kwargs)


class CatchUpFlow:
    def __init__(self, handler):
        self.handler = handler
        store = getattr(handler, "_catchup_store", None)
        if store is None:
            store = CardStore.from_url(
                os.environ.get("REDIS_URL", ""), ttl=SESSION_TTL, prefix=PREFIX,
            )
            handler._catchup_store = store
        self.store = store

    def _key(self, chat_id) -> str:
        return str(chat_id)

    def load(self, chat_id) -> dict | None:
        try:
            data = self.store.get(self._key(chat_id))
        except StoreError:
            logger.warning("[catchup] store down chat=%s", chat_id)
            return None
        return dict(data) if isinstance(data, dict) else None

    def save(self, chat_id, data: dict) -> None:
        try:
            self.store[self._key(chat_id)] = data
        except StoreError:
            logger.warning("[catchup] store down chat=%s", chat_id)

    def drop(self, chat_id) -> None:
        try:
            self.store.pop(self._key(chat_id), None)
        except StoreError:
            logger.warning("[catchup] store down chat=%s", chat_id)

    def _api(self, query):
        return self.handler._get_api(query)

    async def on_callback(self, update, context) -> bool:
        query = update.callback_query
        from integrations.telegram_bot import _parse_cb
        data = _parse_cb(query.data or "")
        if data.get("action") != ACTION:
            return False
        try:
            await query.answer()
        except Exception:
            pass
        sub = data.get("original") or ""
        chat_id = query.message.chat_id
        api = self._api(query)
        logger.info("[catchup] chat=%s sub=%s data=%s", chat_id, sub, (query.data or "")[:64])
        if sub == "go":
            await self._start_from_browse(query, api)
            return True
        if sub == "fs":
            await self._start_storage(query, api, storage_id=data.get("wine_id"))
            return True
        session = self.load(chat_id)
        if sub == "un":
            await self._undo(query, api, session, data)
            return True
        if sub == "x":
            await self._cancel(query, api, session, data)
            return True
        if session is None:
            await _safe_edit(
                query,
                "That catch-up expired. Open /fridge and tap Catch up.",
            )
            return True
        handlers = {
            "d": self._pick_drawer,
            "p": self._page,
            "dk": self._drunk_one,
            "db": self._drunk_all,
            "m1": self._move_one,
            "mb": self._move_all,
            "mf": self._dest_fridge,
            "md": self._dest_drawer,
            "eh": self._empty_here,
            "ok": self._drawer_fine,
            "ap": self._apply,
            "bk": self._resume,
            "w": self._dest_back,
            "ut": self._undo_tap,
        }
        fn = handlers.get(sub)
        if fn is None:
            return True
        await fn(query, api, session, data)
        return True

    # --- Entry ---

    async def _start_from_browse(self, query, api):
        _key, browse = self.handler._get_browse_session(query)
        storage_name = ""
        if browse is not None:
            storage_name = browse.storage_name or ""
        if storage_name in ("", "All storages"):
            await self._pick_fridge(query, api)
            return
        await self._open(query, api, storage_name)

    async def _start_storage(self, query, api, storage_id):
        try:
            storages = api.list_storages()
        except Exception:
            logger.exception("[catchup] list_storages")
            await _safe_edit(query, "Couldn't reach the cellar. Try /fridge again in a moment.")
            return
        match = next((s for s in storages if s.get("id") == storage_id), None)
        if match is None:
            await self._pick_fridge(query, api)
            return
        await self._open(query, api, match["name"], storages=storages)

    async def _pick_fridge(self, query, api):
        try:
            storages = api.list_storages()
        except Exception:
            logger.exception("[catchup] list_storages")
            await _safe_edit(query, "Couldn't reach the cellar. Try /fridge again in a moment.")
            return
        if not storages:
            await _safe_edit(query, "No fridges yet.")
            return
        if len(storages) == 1:
            await self._open(query, api, storages[0]["name"], storages=storages)
            return
        keyboard = []
        for storage in storages:
            keyboard.append([InlineKeyboardButton(
                storage["name"],
                callback_data=_cb("fs", wine_id=storage["id"]),
            )])
        keyboard.append([InlineKeyboardButton("Cancel", callback_data=_cb("x"))])
        await _safe_edit(
            query,
            "<b>Catch up</b>\nWhich fridge are you standing at?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _open(self, query, api, storage_name: str, storages=None):
        try:
            if storages is None:
                storages = api.list_storages()
            wines = api.wines_in_stock(storage_name=storage_name)
        except Exception:
            logger.exception("[catchup] open %s", storage_name)
            await _safe_edit(query, "Couldn't reach the cellar. Try /fridge again in a moment.")
            return
        match = next((s for s in storages if s.get("name") == storage_name), None)
        drawers = int((match or {}).get("drawers") or 0)
        session = {
            "storage_name": storage_name,
            "drawers": drawers,
            "drawer": None,
            "wines": slim_wines(wines),
            "storages": [
                {"id": s.get("id"), "name": s.get("name"), "drawers": s.get("drawers") or 0}
                for s in storages
            ],
            "ops": [],
            "tap": 0,
            "checked": [],
            "pending_move": None,
            "page": 0,
        }
        if drawers:
            await self._show_drawers(query, session)
        else:
            await self._show_walk(query, session)

    # --- Screens ---

    def _place_ops(self, session) -> list[dict]:
        """Staged ops for bottles that started in the drawer on screen."""
        name = session.get("storage_name") or ""
        drawer = session.get("drawer")
        out = []
        for op in session.get("ops") or []:
            if (op.get("expected_storage") or "") != name:
                continue
            if not _in_drawer({"drawer": op.get("expected_row")}, drawer):
                continue
            out.append(op)
        return out

    def _drawer_ops(self, session, drawer) -> int:
        n = 0
        for op in session.get("ops") or []:
            if (op.get("expected_storage") or "") != (session.get("storage_name") or ""):
                continue
            if _in_drawer({"drawer": op.get("expected_row")}, drawer):
                n += 1
        return n

    def _destinations(self, session) -> list[dict]:
        """Fridges a bottle here could have gone to. A rack can't move within itself."""
        source = session.get("storage_name")
        return [
            storage for storage in session.get("storages") or []
            if storage.get("name") != source or int(storage.get("drawers") or 0)
        ]

    def _bottom_rows(self, session) -> list[list[InlineKeyboardButton]]:
        rows = []
        ops = session.get("ops") or []
        if ops:
            rows.append([InlineKeyboardButton(UNDO_TAP, callback_data=_cb("ut"))])
            rows.append([InlineKeyboardButton(
                f"Apply {_plural(len(ops), 'change')}", callback_data=_cb("ap"),
            )])
        rows.append([InlineKeyboardButton("Cancel", callback_data=_cb("x"))])
        return rows

    async def _show_drawers(self, query, session, notice: str | None = None):
        session["drawer"] = None
        session["pending_move"] = None
        name = session.get("storage_name") or "Fridge"
        drawers = int(session.get("drawers") or 0)
        counts = drawer_counts(session.get("wines") or [], name, drawers)
        checked = set(session.get("checked") or [])
        lines = [f"<b>Catch up — {_esc(name)}</b>"]
        if notice:
            lines.append(notice)
        lines.append("Which drawer are you looking at?")
        n_ops = len(session.get("ops") or [])
        if n_ops:
            lines.append(f"\n{_plural(n_ops, 'change')} staged — nothing is saved until Apply.")
        keyboard = []
        row = []

        def label(drawer) -> str:
            text = f"{drawer_label(drawer)} · {counts.get(drawer, 0)}"
            staged = self._drawer_ops(session, drawer)
            if staged:
                text += f" · {staged} staged"
            elif drawer in checked:
                text += " ✓"
            return text

        for drawer in range(1, drawers + 1):
            row.append(InlineKeyboardButton(label(drawer), callback_data=_cb("d", row=drawer)))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        if counts.get(NO_DRAWER):
            keyboard.append([InlineKeyboardButton(
                label(NO_DRAWER), callback_data=_cb("d", row=NO_DRAWER),
            )])
        keyboard.extend(self._bottom_rows(session))
        self.save(query.message.chat_id, session)
        await _safe_edit(
            query, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_walk(self, query, session, notice: str | None = None):
        from integrations.telegram_bot import WINE_TYPE_EMOJI
        session["pending_move"] = None
        name = session.get("storage_name") or "Fridge"
        drawer = session.get("drawer")
        rows = items_for_place(session.get("wines") or [], name, drawer)
        groups = unstaged_groups(rows, session.get("ops") or [])
        bottles = sum(len(g["items"]) for g in groups)
        place = _esc(name) if drawer is None else f"{drawer_label(drawer)} — {_esc(name)}"
        lines = [f"<b>{place} · {_plural(bottles, 'bottle')}</b>"]
        if notice:
            lines.append(notice)
        if groups:
            lines.append("Tap what's wrong. The rest stay.")
        total = len(groups)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(int(session.get("page") or 0), 0), pages - 1)
        session["page"] = page
        shown = groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        movable = bool(self._destinations(session))
        keyboard = []
        if not groups:
            lines.append("\nNothing left to check here.")
        for group in shown:
            emoji = WINE_TYPE_EMOJI.get(group.get("wine_type") or "", "\U0001F377")
            producer = group.get("producer") or ""
            extra = f" <i>({_esc(producer)})</i>" if producer else ""
            n = len(group["items"])
            suffix = f"  ×{n}" if n > 1 else ""
            lines.append(f"\n{emoji} <b>{_esc(group['name'])}</b>{extra}{suffix}")
            wid = group["wine_id"]
            first = group["items"][0]["item_id"]
            all_label = "both" if n == 2 else str(n)
            if n > 1:
                keyboard.append([
                    InlineKeyboardButton("Drunk 1", callback_data=_cb("dk", wine_id=wid, item_id=first)),
                    InlineKeyboardButton(f"Drunk {all_label}", callback_data=_cb("db", wine_id=wid, page=n)),
                ])
                if movable:
                    keyboard.append([
                        InlineKeyboardButton("Moved 1", callback_data=_cb("m1", wine_id=wid, item_id=first)),
                        InlineKeyboardButton(f"Moved {all_label}", callback_data=_cb("mb", wine_id=wid, page=n)),
                    ])
            else:
                buttons = [InlineKeyboardButton(
                    "Drunk", callback_data=_cb("dk", wine_id=wid, item_id=first),
                )]
                if movable:
                    buttons.append(InlineKeyboardButton(
                        "Moved", callback_data=_cb("m1", wine_id=wid, item_id=first),
                    ))
                keyboard.append(buttons)

        staged = self._place_ops(session)
        if staged:
            lines.append("\n<b>Staged here</b>")
            for op in staged[:STAGED_SHOWN]:
                lines.append(op_line(op))
            if len(staged) > STAGED_SHOWN:
                lines.append(f"…and {len(staged) - STAGED_SHOWN} more")
        elsewhere = len(session.get("ops") or []) - len(staged)
        if elsewhere:
            lines.append(f"\n{_plural(elsewhere, 'more change')} staged in other drawers.")
        if session.get("ops"):
            lines.append("Nothing is saved until Apply.")

        if pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("Prev", callback_data=_cb("p", page=page - 1)))
            nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=_cb("p", page=page)))
            if page < pages - 1:
                nav.append(InlineKeyboardButton("Next", callback_data=_cb("p", page=page + 1)))
            keyboard.append(nav)
        if groups and movable:
            empty_label = EMPTY_HERE if session.get("drawers") else EMPTY_HERE_RACK
            keyboard.append([InlineKeyboardButton(empty_label, callback_data=_cb("eh"))])
        if session.get("drawers"):
            keyboard.append([InlineKeyboardButton(DRAWER_FINE, callback_data=_cb("ok"))])
        elif not session.get("ops"):
            keyboard.append([InlineKeyboardButton(RACK_FINE, callback_data=_cb("ok"))])
        keyboard.extend(self._bottom_rows(session))
        if session.get("drawers"):
            keyboard.insert(len(keyboard) - 1, [InlineKeyboardButton(
                "Other drawers", callback_data=_cb("bk"),
            )])
        self.save(query.message.chat_id, session)
        await _safe_edit(
            query, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _ask_dest(self, query, session, *, fridge=None):
        pending = session.get("pending_move") or {}
        n = len(pending.get("item_ids") or [])
        what = pending.get("label") or "a bottle"
        subject = f"<b>{_esc(what)}</b>" if n == 1 else f"these {_plural(n, 'bottle')}"
        dests = self._destinations(session)
        if fridge is None and len(dests) == 1:
            fridge = dests[0]
        back = [InlineKeyboardButton("Back", callback_data=_cb("w"))]
        if fridge is None:
            keyboard = [
                [InlineKeyboardButton(s["name"], callback_data=_cb("mf", wine_id=s["id"]))]
                for s in dests
            ]
            keyboard.append(back)
            self.save(query.message.chat_id, session)
            await _safe_edit(
                query, f"Where did {subject} go?", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        drawers = int(fridge.get("drawers") or 0)
        if drawers == 0:
            await self._commit_move(query, session, fridge["name"], None)
            return
        same = fridge.get("name") == session.get("storage_name")
        here = session.get("drawer") if same else None
        keyboard = []
        row = []
        for drawer in range(1, drawers + 1):
            if drawer == here:
                continue
            row.append(InlineKeyboardButton(
                f"D{drawer}", callback_data=_cb("md", wine_id=fridge["id"], row=drawer),
            ))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        if not (same and here in (None, NO_DRAWER)):
            keyboard.append([InlineKeyboardButton(
                NOT_SURE, callback_data=_cb("md", wine_id=fridge["id"], row=NO_DRAWER),
            )])
        keyboard.append(back)
        self.save(query.message.chat_id, session)
        await _safe_edit(
            query,
            f"Where did {subject} go in <b>{_esc(fridge.get('name') or '')}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # --- Taps ---

    def _group(self, session, wine_id):
        rows = items_for_place(
            session.get("wines") or [], session.get("storage_name") or "",
            session.get("drawer"),
        )
        for group in unstaged_groups(rows, session.get("ops") or []):
            if group["wine_id"] == wine_id:
                return group
        return None

    def _next_tap(self, session) -> int:
        session["tap"] = int(session.get("tap") or 0) + 1
        return session["tap"]

    def _cap_notice(self, session):
        return CAP_NOTICE if len(session.get("ops") or []) >= OPS_CAP else None

    async def _pick_drawer(self, query, api, session, data):
        drawer = data.get("row")
        if drawer is None:
            await self._show_drawers(query, session)
            return
        session["drawer"] = drawer
        session["page"] = 0
        await self._show_walk(query, session)

    async def _page(self, query, api, session, data):
        session["page"] = data.get("page") or 0
        await self._show_walk(query, session)

    async def _drunk_one(self, query, api, session, data):
        group = self._group(session, data.get("wine_id"))
        item = None
        if group is not None:
            item = next((i for i in group["items"] if i["item_id"] == data.get("item_id")), None)
        if item is None:
            # Already staged (a double tap) or gone: never pick another bottle.
            await self._show_walk(query, session)
            return
        extend_ops(session.setdefault("ops", []), stage_items(
            group, [item], "remove", tap=self._next_tap(session),
        ))
        await self._show_walk(query, session, notice=self._cap_notice(session))

    async def _drunk_all(self, query, api, session, data):
        group = self._group(session, data.get("wine_id"))
        if group is None:
            await self._show_walk(query, session)
            return
        if data.get("page") is not None and len(group["items"]) != data.get("page"):
            await self._show_walk(query, session, notice=CHANGED_NOTICE)
            return
        extend_ops(session.setdefault("ops", []), stage_items(
            group, group["items"], "remove", tap=self._next_tap(session),
        ))
        await self._show_walk(query, session, notice=self._cap_notice(session))

    async def _move_one(self, query, api, session, data):
        group = self._group(session, data.get("wine_id"))
        item = None
        if group is not None:
            item = next((i for i in group["items"] if i["item_id"] == data.get("item_id")), None)
        if item is None:
            await self._show_walk(query, session)
            return
        session["pending_move"] = {"item_ids": [item["item_id"]], "label": group["name"]}
        await self._ask_dest(query, session)

    async def _move_all(self, query, api, session, data):
        group = self._group(session, data.get("wine_id"))
        if group is None:
            await self._show_walk(query, session)
            return
        if data.get("page") is not None and len(group["items"]) != data.get("page"):
            await self._show_walk(query, session, notice=CHANGED_NOTICE)
            return
        session["pending_move"] = {
            "item_ids": [i["item_id"] for i in group["items"]],
            "label": group["name"],
        }
        await self._ask_dest(query, session)

    async def _empty_here(self, query, api, session, data):
        rows = items_for_place(
            session.get("wines") or [], session.get("storage_name") or "",
            session.get("drawer"),
        )
        left = unstaged_groups(rows, session.get("ops") or [])
        ids = [i["item_id"] for g in left for i in g["items"]]
        if not ids:
            await self._show_walk(query, session)
            return
        label = left[0]["name"] if len(ids) == 1 else ""
        session["pending_move"] = {"item_ids": ids, "label": label}
        await self._ask_dest(query, session)

    async def _dest_fridge(self, query, api, session, data):
        if not (session.get("pending_move") or {}).get("item_ids"):
            await self._show_walk(query, session)
            return
        fridge = next(
            (s for s in self._destinations(session) if s.get("id") == data.get("wine_id")),
            None,
        )
        await self._ask_dest(query, session, fridge=fridge)

    async def _dest_drawer(self, query, api, session, data):
        if not (session.get("pending_move") or {}).get("item_ids"):
            await self._show_walk(query, session)
            return
        fridge = next(
            (s for s in self._destinations(session) if s.get("id") == data.get("wine_id")),
            None,
        )
        if fridge is None:
            await self._ask_dest(query, session)
            return
        row = data.get("row")
        await self._commit_move(query, session, fridge["name"], row or None)

    async def _commit_move(self, query, session, dest_storage, dest_row):
        pending = session.get("pending_move") or {}
        wanted = set(pending.get("item_ids") or [])
        rows = items_for_place(
            session.get("wines") or [], session.get("storage_name") or "",
            session.get("drawer"),
        )
        ops = session.setdefault("ops", [])
        tap = self._next_tap(session)
        for group in unstaged_groups(rows, ops):
            items = [i for i in group["items"] if i["item_id"] in wanted]
            if items:
                extend_ops(ops, stage_items(
                    group, items, "move",
                    dest_storage=dest_storage, dest_row=dest_row, tap=tap,
                ))
        await self._show_walk(query, session, notice=self._cap_notice(session))

    async def _undo_tap(self, query, api, session, data):
        ops = session.get("ops") or []
        if ops:
            last = max(int(op.get("tap") or 0) for op in ops)
            session["ops"] = [op for op in ops if int(op.get("tap") or 0) != last]
        if session.get("drawer") is None and session.get("drawers"):
            await self._show_drawers(query, session)
        else:
            await self._show_walk(query, session)

    async def _drawer_fine(self, query, api, session, data):
        if session.get("drawers"):
            drawer = session.get("drawer")
            checked = list(session.get("checked") or [])
            if drawer is not None and drawer not in checked:
                checked.append(drawer)
            session["checked"] = checked
            await self._show_drawers(query, session)
            return
        if session.get("ops"):
            await self._show_walk(query, session)
            return
        self.drop(query.message.chat_id)
        await _safe_edit(query, "Good — nothing to change.")

    async def _resume(self, query, api, session, data):
        if session.get("drawers"):
            await self._show_drawers(query, session)
        else:
            await self._show_walk(query, session)

    async def _dest_back(self, query, api, session, data):
        await self._show_walk(query, session)

    # --- Apply / Undo ---

    def _retry_markup(self, session):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Try again", callback_data=_cb("ap"))],
            [InlineKeyboardButton("Back", callback_data=_cb("bk"))],
        ])

    async def _apply(self, query, api, session, data):
        ops = session.get("ops") or []
        if not ops:
            await self._resume(query, api, session, data)
            return
        try:
            result = api.bulk_stock(api_ops(ops))
        except Exception:
            logger.exception("[catchup] bulk apply")
            await _safe_edit(query, UNREACHABLE, reply_markup=self._retry_markup(session))
            return
        err = result.get("error")
        if err == "stale_item":
            self._reload_wines(session, api)
            kept, dropped = still_valid(ops, session.get("wines") or [])
            session["ops"] = kept
            notice = (
                f"{_plural(dropped, 'bottle')} had already changed (scanned or "
                "moved by someone else), so I dropped "
                f"{'it' if dropped == 1 else 'them'}. Nothing was applied yet."
            )
            if not dropped:
                notice = "Something changed while you were catching up. Nothing was applied yet."
            await self._resume_with(query, session, notice)
            return
        if err:
            text = APPLY_ERRORS.get(err)
            if text is None:
                logger.warning("[catchup] bulk apply error=%s", err)
                text = UNREACHABLE if result.get("status_code", 0) >= 500 else (
                    "That didn't work. Nothing was applied."
                )
            await _safe_edit(query, text, reply_markup=self._retry_markup(session))
            return
        batch_id = result.get("batch_id") or ""
        n = result.get("applied") or len(ops)
        summary = apply_summary(ops, n)
        session["ops"] = []
        session["checked"] = []
        session["tap"] = 0
        self._reload_wines(session, api)
        self.save(query.message.chat_id, session)
        keyboard = []
        if batch_id:
            keyboard.append([InlineKeyboardButton(
                "Undo", callback_data=_cb("un", batch=batch_id),
            )])
        keyboard.append([InlineKeyboardButton("Catch up more", callback_data=_cb("bk"))])
        await _safe_edit(
            query, summary, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _resume_with(self, query, session, notice):
        if session.get("drawers") and session.get("drawer") is None:
            await self._show_drawers(query, session, notice=notice)
        else:
            await self._show_walk(query, session, notice=notice)

    async def _undo(self, query, api, session, data):
        batch_id = data.get("barcode") or ""
        if not batch_id:
            await _safe_edit(query, "Nothing to undo.")
            return
        try:
            result = api.bulk_stock_undo(batch_id)
        except Exception:
            logger.exception("[catchup] bulk undo")
            await _safe_edit(
                query, UNDO_UNREACHABLE,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Try again", callback_data=_cb("un", batch=batch_id),
                )]]),
            )
            return
        more = None
        if session is not None:
            more = InlineKeyboardMarkup([[InlineKeyboardButton(
                "Catch up more", callback_data=_cb("bk"),
            )]])
        err = result.get("error")
        if err:
            await _safe_edit(
                query, UNDO_ERRORS.get(err, "That didn't work. Nothing was undone."),
                reply_markup=more,
            )
            return
        if session is not None:
            self._reload_wines(session, api)
            session["ops"] = []
            self.save(query.message.chat_id, session)
        n = result.get("undone") or 0
        await _safe_edit(
            query, f"Undone {_plural(n, 'change')}. Those bottles are back where they were.",
            reply_markup=more,
        )

    def _reload_wines(self, session, api):
        try:
            session["wines"] = slim_wines(api.wines_in_stock(
                storage_name=session.get("storage_name") or "",
            ))
        except Exception:
            logger.warning("[catchup] reload failed", exc_info=True)

    async def _cancel(self, query, api, session, data):
        self.drop(query.message.chat_id)
        if session and session.get("ops"):
            await _safe_edit(
                query, f"Catch up cancelled. {_plural(len(session['ops']), 'change')} not applied.",
            )
            return
        await _safe_edit(query, "Catch up cancelled.")
