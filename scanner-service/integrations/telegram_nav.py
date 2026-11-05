"""Navigation: the bottom bar, the ☰ Menu home screen, follow-up buttons,
one-shot typed input and double-tap guards.

Everything here is English (menus stay English). Typed conversation keeps its
own language; this module never replies to free text except for a pending
input it asked for.

Callback data lives in the ``mn:`` namespace:

    mn:<screen>        redraw the menu message as that screen (edits in place)
    mn:new             open a fresh menu below a result (follow-up rows)
    mn:x:<action>      run something (fridge, search, invite to household, ...)

All of it fits Telegram's 64-byte callback limit by construction; ``menu_cb``
checks anyway, with the same budget as ``_cb()``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

MENU_PREFIX = "mn:"
PENDING_TTL_SECONDS = 120
# Updates run one at a time, so a queued double tap arrives right after the
# first one finishes. A short cooldown catches it without blocking a real
# retry later (the server-side conditional updates are the long-term guard).
CONFIRM_COOLDOWN_SECONDS = 5
HOUSEHOLD_CACHE_SECONDS = 300
ALREADY_DONE = "Already done"

# Bottom bar. The texts double as LABEL_COMMANDS keys (see chat_router).
BAR_CELLAR = "\U0001F377 Cellar"
BAR_SCAN = "\U0001F4F8 Scan"
BAR_SEARCH = "\U0001F50E Search"
BAR_LIST = "\U0001F4DD List"
BAR_CLUBS = "\U0001F347 Clubs"
BAR_MENU = "☰ Menu"
BAR_PLACEHOLDER = "Tap a button below"

BACK = "⬅️ Back"
MENU = "☰ Menu"
CANCEL = "✖️ Cancel"

SEARCH_PROMPT = (
    "\U0001F50E Type a wine name…\n"
    "<i>Only your next message is searched, for the next 2 minutes.</i>"
)
CANCELLED = "\u2716\uFE0F Cancelled."
SCAN_PROMPT = (
    "\U0001F4F8 <b>Scan a bottle</b>\n\n"
    "Send me a photo of the <b>front label</b> or the <b>barcode</b>. "
    "A photo of a restaurant wine list works too — I'll pick from it."
)
FEATURE_OFF = "That isn't switched on yet."
BAR_INTRO = (
    "The buttons below are always here \u2014 <b>\u2630 Menu</b> has the rest."
)
STAFF_ONLY = "That's for staff only."
LABEL_FLAG = {"list": "feature_lists", "clubs": "feature_clubs"}
NOT_PROVISIONED = "You're not in a cellar any more. Ask for a new invite."


# --------------------------------------------------------------------------
# Callback data

def menu_cb(*parts) -> str:
    """``mn:a:b`` with the same 64-byte budget as ``_cb()``."""
    from integrations.telegram_bot import _fit_callback

    return _fit_callback(MENU_PREFIX + ":".join(str(p) for p in parts))


def parse_menu_cb(data: str) -> list[str] | None:
    if not (data or "").startswith(MENU_PREFIX):
        return None
    return data[len(MENU_PREFIX):].split(":")


# --------------------------------------------------------------------------
# Bottom bar

def bar_labels(flags: dict | None) -> list[str]:
    flags = flags or {}
    labels = [BAR_CELLAR, BAR_SCAN, BAR_SEARCH]
    if flags.get("feature_lists"):
        labels.append(BAR_LIST)
    if flags.get("feature_clubs"):
        labels.append(BAR_CLUBS)
    labels.append(BAR_MENU)
    return labels


def _reflow(labels: list[str]) -> list[list[str]]:
    """6 -> 3+3, 5 -> 3+2, 4 -> 2+2. Never a lonely button on a row."""
    if len(labels) == 4:
        return [labels[:2], labels[2:]]
    return [labels[:3], labels[3:]] if len(labels) > 3 else [labels]


def bottom_bar(flags: dict | None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        _reflow(bar_labels(flags)),
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=BAR_PLACEHOLDER,
    )


# --------------------------------------------------------------------------
# Menu screens

@dataclass
class NavContext:
    flags: dict = field(default_factory=dict)
    is_staff: bool = False
    household: str = ""
    household_count: int = 1

    def on(self, key: str) -> bool:
        return bool(self.flags.get(key))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _header(ctx: NavContext, title: str) -> str:
    where = (
        f"\n\U0001F3E0 <b>{html.escape(ctx.household, quote=False)}</b>"
        if ctx.household else ""
    )
    return f"{title}{where}"


SCREENS = ("home", "cellar", "find", "lists", "clubs", "house", "help", "staff")


def screen_allowed(screen: str, ctx: NavContext) -> bool:
    if screen == "lists":
        return ctx.on("feature_lists")
    if screen == "clubs":
        return ctx.on("feature_clubs")
    if screen == "staff":
        return ctx.is_staff
    return screen in SCREENS


def build_screen(screen: str, ctx: NavContext) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard for one menu screen. Gated screens fall back to home."""
    if not screen_allowed(screen, ctx):
        screen = "home"
    back = [_btn(BACK, menu_cb("home"))]
    x = lambda action: menu_cb("x", action)  # noqa: E731

    if screen == "cellar":
        text = _header(ctx, "\U0001F377 <b>Cellar</b>")
        rows = [
            [_btn("\U0001F9CA Fridge", x("fridge")), _btn("\U0001F552 Recent", x("recent"))],
            [_btn("\U0001F4E6 Stock", x("stock")), _btn("\U0001F4CA Status", x("status"))],
            back,
        ]
    elif screen == "find":
        text = _header(ctx, "\U0001F50E <b>Find</b>")
        rows = [
            [_btn("\U0001F50E Search by name", x("search"))],
            [_btn("\U0001F4CD Browse locations", x("locs"))],
            back,
        ]
    elif screen == "lists":
        text = _header(ctx, "\U0001F4DD <b>Lists</b>")
        rows = [[_btn("\U0001F4DD My to-drink list", x("list"))]]
        if ctx.on("feature_clubs"):
            rows.append([_btn("\U0001F347 Club list", x("clist"))])
        rows.append(back)
    elif screen == "clubs":
        text = _header(ctx, "\U0001F347 <b>Clubs</b>")
        rows = [
            [_btn("\U0001F347 My clubs", x("clubs")), _btn("➕ Start a club", x("cnew"))],
            [_btn("✉️ Invite to club", x("cinv"))],
        ]
        extra = []
        if ctx.on("feature_tasteoffs"):
            extra.append(_btn("\U0001F3C6 Taste-off", x("tasteoff")))
        if ctx.on("feature_global_clubs"):
            extra.append(_btn("\U0001F30D Discover clubs", x("discover")))
        if extra:
            rows.append(extra)
        rows.append(back)
    elif screen == "house":
        text = _header(ctx, "\U0001F3E0 <b>Household</b>")
        rows = [[
            _btn("\U0001F465 Members", x("members")),
            _btn("✉️ Invite to household", x("hinv")),
        ]]
        if ctx.household_count > 1:
            rows.append([_btn("\U0001F501 Switch household", x("switch"))])
        rows.append(back)
    elif screen == "help":
        text = _header(ctx, "❓ <b>Help</b>") + (
            "\n\n"
            "\U0001F4F8 Send a photo of a label or barcode to add a bottle.\n"
            "\U0001F4AC Or just type, like <i>what should I open tonight?</i>\n"
            "☰ The buttons at the bottom are always there."
        )
        rows = [
            [_btn("\U0001F4F8 How to add a wine", x("howto"))],
            [_btn("\U0001F4DC All commands", x("help"))],
            back,
        ]
    elif screen == "staff":
        text = _header(ctx, "\U0001F6E0 <b>Staff</b>")
        rows = [
            [_btn("\U0001F6E0 Staff tools", x("sadmin")), _btn("\U0001F4CA Every cellar", x("sstats"))],
            [_btn("\U0001F4AC Chat stats", x("schat")), _btn("\U0001F3D8 Households", x("shh"))],
            [_btn("\U0001F517 New cellar link", x("snew"))],
            back,
        ]
    else:
        text = _header(ctx, "☰ <b>Menu</b>")
        rows = [[
            _btn("\U0001F377 Cellar", menu_cb("cellar")),
            _btn("\U0001F50E Find", menu_cb("find")),
        ]]
        pair = []
        if ctx.on("feature_lists"):
            pair.append(_btn("\U0001F4DD Lists", menu_cb("lists")))
        if ctx.on("feature_clubs"):
            pair.append(_btn("\U0001F347 Clubs", menu_cb("clubs")))
        if pair:
            rows.append(pair)
        rows.append([
            _btn("\U0001F3E0 Household", menu_cb("house")),
            _btn("❓ Help", menu_cb("help")),
        ])
        if ctx.is_staff:
            rows.append([_btn("\U0001F6E0 Staff", menu_cb("staff"))])
    return text, InlineKeyboardMarkup(rows)


# Which actions need which gate. Re-checked on every tap: a button can outlive
# a flag change or be forged.
ACTION_FLAGS = {
    "list": ("feature_lists",),
    "clist": ("feature_lists", "feature_clubs"),
    "clubs": ("feature_clubs",),
    "cnew": ("feature_clubs",),
    "cinv": ("feature_clubs",),
    "tasteoff": ("feature_clubs", "feature_tasteoffs"),
    "discover": ("feature_clubs", "feature_global_clubs"),
}
STAFF_ACTIONS = {"sadmin", "sstats", "schat", "shh", "snew"}
# Where a breadcrumb points, for the menu message an action leaves behind.
ACTION_TITLES = {
    "fridge": "Fridge", "recent": "Recent", "stock": "Stock", "status": "Status",
    "locs": "Browse locations", "list": "My to-drink list", "clist": "Club list",
    "clubs": "My clubs", "cnew": "Start a club", "cinv": "Invite to club",
    "tasteoff": "Taste-off", "discover": "Discover clubs", "members": "Members",
    "hinv": "Invite to household", "switch": "Switch household",
    "help": "All commands", "sadmin": "Staff tools", "sstats": "Every cellar",
    "schat": "Chat stats", "shh": "Households", "snew": "New cellar link",
}


def action_allowed(action: str, ctx: NavContext) -> tuple[bool, str]:
    if action in STAFF_ACTIONS and not ctx.is_staff:
        return False, STAFF_ONLY
    for flag in ACTION_FLAGS.get(action, ()):
        if not ctx.on(flag):
            return False, FEATURE_OFF
    return True, ""


# --------------------------------------------------------------------------
# Follow-up rows

def follow_up_rows(kind: str, ctx: dict | None = None) -> list[list[InlineKeyboardButton]]:
    """Buttons that end every result, so nothing is a dead end.

    ``ctx`` may carry ``flags``, ``wine_id``, ``club_id`` and ``event_id``.
    Every kind ends with ☰ Menu, which opens a fresh menu below the result.
    """
    ctx = ctx or {}
    flags = ctx.get("flags") or {}
    x = lambda action: menu_cb("x", action)  # noqa: E731
    menu = _btn(MENU, menu_cb("new"))
    row: list[InlineKeyboardButton] = []
    if kind in ("fridge", "cellar"):
        row = [_btn("\U0001F50E Search", x("search")), _btn("\U0001F4CA Status", x("status"))]
    elif kind == "status":
        row = [_btn("\U0001F50E Search", x("search")), _btn("\U0001F9CA Fridge", x("fridge"))]
    elif kind == "added":
        from integrations.telegram_bot import _cb

        wine_id = ctx.get("wine_id")
        if wine_id:
            row.append(_btn("\U0001F377 View wine", _cb("det", wine_id=wine_id)))
        row.append(_btn("\U0001F4F8 Scan another", x("scan")))
    elif kind == "removed":
        row = [_btn("\U0001F4F8 Scan another", x("scan")), _btn("\U0001F377 Cellar", x("fridge"))]
    elif kind == "scan":
        from integrations.telegram_bot import _cb

        row = [_btn("\u2139\uFE0F How it works", _cb("howto"))]
    elif kind == "search":
        row = [_btn("\U0001F50E Search again", x("search"))]
    elif kind == "household":
        row = [_btn("✉️ Invite to household", x("hinv"))]
    elif kind == "club":
        from integrations.telegram_clubs import club_cb

        if flags.get("feature_club_comments") and ctx.get("event_id"):
            row.append(_btn("\U0001F4AC Say something", club_cb("cm", ctx["event_id"])))
        if flags.get("feature_tasteoffs") and ctx.get("club_id"):
            row.append(_btn("\U0001F3C6 Taste-off", club_cb("to", ctx["club_id"])))
    row.append(menu)
    return [row]


def with_follow_up(markup: InlineKeyboardMarkup | None, kind: str,
                   ctx: dict | None = None) -> InlineKeyboardMarkup:
    """Existing inline keyboard plus the follow-up row for ``kind``."""
    rows = [list(r) for r in (markup.inline_keyboard if markup is not None else ())]
    return InlineKeyboardMarkup(rows + follow_up_rows(kind, ctx))


# --------------------------------------------------------------------------
# One-shot typed input

class PendingInputs:
    """At most one "the next message is ..." slot per chat.

    Only the very next text message is taken: ``take`` clears the slot whether
    or not it is still fresh. An expired slot returns None, so that message is
    handled as if nothing had been asked.
    """

    def __init__(self, ttl: float = PENDING_TTL_SECONDS, clock=None):
        self.ttl = ttl
        self._clock = clock or time.monotonic
        self._slots: dict[str, dict] = {}

    def begin(self, chat_id, kind: str, **data) -> dict:
        entry = {"kind": kind, "at": self._clock(), **data}
        self._slots[str(chat_id)] = entry
        return entry

    def peek(self, chat_id) -> dict | None:
        entry = self._slots.get(str(chat_id))
        if entry is None:
            return None
        if self._clock() - entry["at"] > self.ttl:
            self._slots.pop(str(chat_id), None)
            return None
        return entry

    def take(self, chat_id, include_expired: bool = False) -> dict | None:
        """Pop the slot. Expired -> None, or (``include_expired``) the entry
        marked ``expired`` so its prompt can still be closed."""
        entry = self._slots.pop(str(chat_id), None)
        if entry is None:
            return None
        if self._clock() - entry["at"] > self.ttl:
            return {**entry, "expired": True} if include_expired else None
        return entry

    def cancel(self, chat_id) -> dict | None:
        return self._slots.pop(str(chat_id), None)


# --------------------------------------------------------------------------
# Double-tap guard

class ConfirmGuard:
    """First tap on a confirm button wins; a repeat right after is "Already done".

    Keyed by chat, message and the exact callback data, so it is per button.
    The claim is held while the action runs and for a few seconds after it
    finishes (``done``); a failed action releases it at once (``release``).
    Longer-lived idempotency is the server's job (conditional updates).
    """

    def __init__(self, cooldown: float = CONFIRM_COOLDOWN_SECONDS, clock=None):
        self.cooldown = cooldown
        self._clock = clock or time.monotonic
        self._until: dict[str, float] = {}

    @staticmethod
    def key(query) -> str:
        msg = getattr(query, "message", None)
        chat = getattr(msg, "chat_id", None)
        mid = getattr(msg, "message_id", None)
        return f"{chat}:{mid}:{getattr(query, 'data', '')}"

    def claim(self, key: str) -> bool:
        now = self._clock()
        for k in [k for k, until in self._until.items() if until <= now]:
            self._until.pop(k, None)
        if key in self._until:
            return False
        self._until[key] = float("inf")  # in flight
        return True

    def done(self, key: str) -> None:
        self._until[key] = self._clock() + self.cooldown

    def release(self, key: str) -> None:
        self._until.pop(key, None)


# Confirm-style buttons (yes/no, commit-once). Club actions are `cl:<a>:...`;
# bot actions are the `a:` field of `_cb()` data.
CLUB_CONFIRMS = {"ly", "za", "dy", "dn", "zc", "ho", "jj", "tc"}
BOT_CONFIRMS = {"lc", "ls", "ok"}


def is_confirm(data: str) -> bool:
    data = data or ""
    if data.startswith("cl:"):
        bits = data.split(":")
        return len(bits) > 1 and bits[1] in CLUB_CONFIRMS
    if data.startswith("a:"):
        action = data.split(",", 1)[0][2:]
        return action in BOT_CONFIRMS
    return False


async def answer_already_done(query) -> None:
    try:
        await query.answer(ALREADY_DONE)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Shims so command handlers can run from a button tap

class _MessageAs:
    """A message that replies into the tapped chat but reads as ``text``."""

    def __init__(self, message, text: str):
        object.__setattr__(self, "_inner", message)
        object.__setattr__(self, "_text", text)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def text(self):
        return self._text


class TapUpdate:
    """Present a callback as a command message: ``.message.reply_text`` posts
    below the menu, ``effective_chat`` / ``effective_user`` are the tapper's."""

    def __init__(self, update, message, text: str):
        self._inner = update
        self.message = _MessageAs(message, text)
        self.effective_message = self.message
        self.callback_query = None

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ArgsContext:
    def __init__(self, context, args=None):
        self._inner = context
        self.args = list(args or [])

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------------------------
# The flow

async def edit_or_send_menu(query, text: str, markup):
    """Edit the tapped message; a "not modified" double-tap is fine; anything
    else (too old, deleted, a photo caption) sends a fresh message instead."""
    try:
        await query.answer()
    except Exception:
        pass
    try:
        return await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return None
        logger.info("[nav] edit failed, sending instead: %s", e)
    except Exception as e:
        logger.info("[nav] edit failed, sending instead: %s", e)
    message = getattr(query, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        return await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return None


class NavFlow:
    def __init__(self, handler, clock=None):
        self.handler = handler
        self.pending = PendingInputs(clock=clock)
        self.guard = ConfirmGuard(clock=clock)
        self._households: dict[str, tuple] = {}
        self._clock = clock or time.monotonic
        # chat_id -> message_id of the menu that still has live buttons.
        self._live_menu: dict[str, int] = {}
        # chat_id -> signature of the bar it was last sent (memory fallback
        # for the Redis store).
        self._bar_given: dict[str, str] = {}

    # -- context ----------------------------------------------------------

    def flags(self) -> dict:
        return {k: bool(v) for k, v in (getattr(self.handler, "flags", None) or {}).items()}

    def is_staff(self, chat_id) -> bool:
        return str(chat_id) in [str(c) for c in (getattr(self.handler, "admin_chat_ids", None) or [])]

    def bar(self) -> ReplyKeyboardMarkup:
        return bottom_bar(self.flags())

    async def context_for(self, chat_id) -> NavContext:
        name, count = await self._household(chat_id)
        return NavContext(
            flags=self.flags(),
            is_staff=self.is_staff(chat_id),
            household=name,
            household_count=count,
        )

    async def _household(self, chat_id) -> tuple[str, int]:
        cid = str(chat_id)
        try:
            api = self.handler._get_api(cid)
        except Exception:
            return "", 1
        hh_id = getattr(api, "household_id", None)
        cached = self._households.get(cid)
        if cached and cached[0] == hh_id and self._clock() - cached[3] < HOUSEHOLD_CACHE_SECONDS:
            return cached[1], cached[2]

        def _fetch():
            name = ""
            count = 1
            try:
                data = api.get_household() or {}
                name = str(data.get("name") or "")
            except Exception:
                pass
            try:
                count = len(api.list_households() or []) or 1
            except Exception:
                pass
            return name, count

        try:
            name, count = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except RuntimeError:
            name, count = _fetch()
        self._households[cid] = (hh_id, name, count, self._clock())
        return name, count

    def forget_household(self, chat_id) -> None:
        self._households.pop(str(chat_id), None)

    # -- menu messages ----------------------------------------------------

    async def send_bar_intro(self, bot, chat_id):
        """Install the bottom bar with one short line (after an edited welcome)."""
        self.mark_bar_given(chat_id)
        return await bot.send_message(
            chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
            text=BAR_INTRO, parse_mode="HTML", reply_markup=self.bar(),
        )

    async def send_menu(self, bot, chat_id, screen: str = "home"):
        """A new menu message at the bottom. The previous live menu goes, so
        there is only ever one set of menu buttons in the chat."""
        ctx = await self.context_for(chat_id)
        text, markup = build_screen(screen, ctx)
        await self._retire_menu(bot, chat_id)
        msg = await bot.send_message(
            chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
            text=text, parse_mode="HTML", reply_markup=markup,
        )
        mid = getattr(msg, "message_id", None)
        if mid is not None:
            self._live_menu[str(chat_id)] = mid
        return msg

    async def _retire_menu(self, bot, chat_id, keep: int | None = None) -> None:
        old = self._live_menu.pop(str(chat_id), None)
        if old is None or old == keep:
            return
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=old)
            return
        except Exception:
            pass
        try:
            await bot.edit_message_reply_markup(chat_id=int(chat_id), message_id=old, reply_markup=None)
        except Exception:
            pass

    async def show_screen(self, query, screen: str, chat_id, note: str = ""):
        ctx = await self.context_for(chat_id)
        text, markup = build_screen(screen, ctx)
        if note:
            text = f"{note}\n\n{text}"
        tapped = getattr(getattr(query, "message", None), "message_id", None)
        msg = await edit_or_send_menu(query, text, markup)
        mid = getattr(msg, "message_id", None)
        live = mid if isinstance(mid, int) else tapped
        if live is None:
            return
        previous = self._live_menu.get(str(chat_id))
        if previous is not None and previous != live:
            bot = _bot_of(query)
            if bot is not None:
                await self._retire_menu(bot, chat_id, keep=live)
        self._live_menu[str(chat_id)] = live

    def from_live_menu(self, query, chat_id) -> bool:
        tapped = getattr(getattr(query, "message", None), "message_id", None)
        return tapped is not None and self._live_menu.get(str(chat_id)) == tapped

    async def _breadcrumb(self, query, action: str, chat_id) -> None:
        """The menu an action came from keeps a one-line trail, no buttons.
        Only the live menu: a follow-up row under a result keeps the result."""
        title = ACTION_TITLES.get(action)
        if not title or not self.from_live_menu(query, chat_id):
            return
        try:
            await query.edit_message_text(f"\u2630 Menu \u203A {title} \u2193")
        except Exception:
            return
        self._live_menu.pop(str(chat_id), None)

    # -- labels -----------------------------------------------------------

    async def on_label(self, update, context, action: str) -> None:
        chat_id = str(update.effective_chat.id)
        bot = getattr(context, "bot", None)
        if await self.handler._abandon_pending_states(chat_id, bot) and bot is not None:
            # A naming keyboard was showing; put the bar back first.
            try:
                await self.send_bar_intro(bot, chat_id)
            except Exception as e:
                logger.info("[nav] bar after naming failed: %s", e)
        elif action not in ("list", "clubs") or self.flags().get(LABEL_FLAG.get(action, "")):
            # Missing or out-of-date bar. (A flag-off List/Clubs tap gets
            # "not on yet" with a fresh bar below instead.)
            await self.ensure_bar(bot, chat_id)
        if action == "menu":
            if bot is None:
                ctx = await self.context_for(chat_id)
                text, markup = build_screen("home", ctx)
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
                return
            await self.send_menu(bot, chat_id)
            return
        if action == "help":
            if bot is None:
                ctx = await self.context_for(chat_id)
                text, markup = build_screen("help", ctx)
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
                return
            await self.send_menu(bot, chat_id, "help")
            return
        if action == "search":
            await self.ask_search(update.message, chat_id)
            return
        if action == "scan":
            await update.message.reply_text(
                SCAN_PROMPT, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(follow_up_rows("scan")),
            )
            return
        if action == "cellar":
            await self.handler._cmd_fridge(update, ArgsContext(context))
            return
        if action == "list":
            if not self.flags().get("feature_lists"):
                await update.message.reply_text(FEATURE_OFF, reply_markup=self.bar())
                self.mark_bar_given(chat_id)
                return
            await self.handler._cmd_list(update, ArgsContext(context))
            return
        if action == "clubs":
            if not self.flags().get("feature_clubs"):
                await update.message.reply_text(FEATURE_OFF, reply_markup=self.bar())
                self.mark_bar_given(chat_id)
                return
            await self.handler._cmd_club(update, ArgsContext(context))
            return

    # -- pending input ----------------------------------------------------

    async def ask_search(self, message, chat_id, query=None):
        markup = InlineKeyboardMarkup([[_btn(CANCEL, menu_cb("x", "cancel"))]])
        if query is not None:
            try:
                await query.edit_message_text(SEARCH_PROMPT, parse_mode="HTML", reply_markup=markup)
                prompt_id = getattr(query.message, "message_id", None)
                if self._live_menu.get(str(chat_id)) == prompt_id:
                    self._live_menu.pop(str(chat_id), None)
                self._begin(chat_id, "search", prompt_id=prompt_id)
                return
            except Exception as e:
                logger.info("[nav] search prompt edit failed, sending: %s", e)
        sent = await message.reply_text(SEARCH_PROMPT, parse_mode="HTML", reply_markup=markup)
        self._begin(chat_id, "search", prompt_id=getattr(sent, "message_id", None))

    def _begin(self, chat_id, kind: str, **data) -> None:
        # One slot per chat: a club comment in progress is replaced too.
        club = self.handler._club_flow()
        state = club.state_for(str(chat_id))
        if state and state.get("step") == "comment":
            club.clear(str(chat_id))
        self.pending.begin(chat_id, kind, **data)

    async def on_pending_text(self, update, context, text: str) -> bool:
        chat_id = str(update.effective_chat.id)
        entry = self.pending.take(chat_id, include_expired=True)
        if entry is None:
            return False
        await self._close_prompt(context, chat_id, entry.get("prompt_id"))
        if entry.get("expired"):
            return False  # too late: handled as a normal message
        if entry["kind"] == "search":
            await self.handler._do_search(update, text)
            return True
        return False

    async def _close_prompt(self, context, chat_id, prompt_id) -> None:
        bot = getattr(context, "bot", None)
        if bot is None or not prompt_id:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(chat_id), message_id=prompt_id, reply_markup=None,
            )
        except Exception:
            pass

    async def cancel(self, query, chat_id) -> None:
        """Cancel only the prompt this button belongs to. An older prompt's
        Cancel just loses its button; whatever is live now is left alone."""
        tapped = getattr(getattr(query, "message", None), "message_id", None)
        entry = self.pending.peek(chat_id)
        club = self.handler._club_flow()
        state = club.state_for(str(chat_id)) or {}
        comment = state if state.get("step") == "comment" else None
        if entry is not None and entry.get("prompt_id") == tapped:
            self.pending.cancel(chat_id)
        elif comment is not None and comment.get("prompt_id") == tapped:
            club.clear(str(chat_id))
        else:
            try:
                await query.answer()
            except Exception:
                pass
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        await self.show_screen(query, "home", chat_id, note=CANCELLED)

    async def drop_pending(self, chat_id, bot=None) -> None:
        """Drop the "next message" slot (search or club comment) and take the
        Cancel button off its prompt, so no stale prompt looks live."""
        prompts = []
        entry = self.pending.cancel(chat_id)
        if entry is not None:
            prompts.append(entry.get("prompt_id"))
        club = self.handler._club_flow()
        state = club.state_for(str(chat_id)) or {}
        if state.get("step") == "comment":
            prompts.append(state.get("prompt_id"))
            club.clear(str(chat_id))
        if bot is None:
            return
        for prompt_id in prompts:
            if not prompt_id:
                continue
            try:
                await bot.edit_message_reply_markup(
                    chat_id=int(chat_id), message_id=prompt_id, reply_markup=None,
                )
            except Exception:
                pass

    # -- first contact after a deploy ------------------------------------

    def _bar_signature(self) -> str:
        return "|".join(bar_labels(self.flags()))

    def _bar_seen(self, chat_id) -> str:
        store = getattr(self.handler, "_bar_store", None)
        if store is not None:
            try:
                entry = store.get(str(chat_id))
                if entry is not None:
                    return str(entry.get("sig") or "")
            except Exception:
                pass
        return self._bar_given.get(str(chat_id), "")

    def needs_bar(self, chat_id) -> bool:
        """Existing chats never saw /start since the bar shipped, and a flag
        switched on later changes it. True when this chat's bar is missing or
        out of date (per-chat signature, kept in Redis across restarts)."""
        if not getattr(self.handler, "_bar_rollout", False):
            return False
        return self._bar_seen(chat_id) != self._bar_signature()

    async def ensure_bar(self, bot, chat_id) -> None:
        if bot is None or not self.needs_bar(chat_id):
            return
        try:
            await self.send_bar_intro(bot, chat_id)
        except Exception as e:
            logger.info("[nav] bar intro failed for %s: %s", chat_id, e)

    def forget_bar(self, chat_id) -> None:
        """Another reply keyboard replaced the bar (e.g. "Send my location");
        the next typed message re-sends it, however late that is."""
        self._bar_given.pop(str(chat_id), None)
        store = getattr(self.handler, "_bar_store", None)
        if store is not None:
            try:
                store.pop(str(chat_id), None)
            except Exception:
                pass

    def mark_bar_given(self, chat_id) -> None:
        sig = self._bar_signature()
        self._bar_given[str(chat_id)] = sig
        store = getattr(self.handler, "_bar_store", None)
        if store is not None:
            try:
                store[str(chat_id)] = {"sig": sig}
            except Exception:
                pass

    # -- callbacks --------------------------------------------------------

    async def on_callback(self, update, context) -> bool:
        query = update.callback_query
        parts = parse_menu_cb(getattr(query, "data", "") or "")
        if parts is None:
            return False
        chat_id = str(update.effective_chat.id)
        if not self.handler._is_known(update):
            # A removed member keeps old menus and the bar; never fall back
            # to another client for them.
            try:
                await query.answer(NOT_PROVISIONED, show_alert=True)
            except Exception:
                pass
            return True
        head = parts[0] if parts else "home"
        if not (head == "x" and len(parts) > 1 and parts[1] == "cancel"):
            # Navigating away means the person moved on from a search or
            # comment prompt; only its own Cancel keeps it to act on.
            await self.drop_pending(chat_id, getattr(context, "bot", None))
        if head == "new":
            try:
                await query.answer()
            except Exception:
                pass
            bot = getattr(context, "bot", None) or query.get_bot()
            await self.send_menu(bot, chat_id)
            return True
        if head != "x":
            ctx = await self.context_for(chat_id)
            if head not in SCREENS or not screen_allowed(head, ctx):
                head = "home"
            await self.show_screen(query, head, chat_id)
            return True
        action = parts[1] if len(parts) > 1 else ""
        await self.run_action(update, context, action)
        return True

    async def run_action(self, update, context, action: str) -> None:
        query = update.callback_query
        chat_id = str(update.effective_chat.id)
        if action == "cancel":
            await self.cancel(query, chat_id)
            return
        ctx = NavContext(flags=self.flags(), is_staff=self.is_staff(chat_id))
        ok, why = action_allowed(action, ctx)
        if not ok:
            try:
                await query.answer(why, show_alert=True)
            except Exception:
                pass
            if why == FEATURE_OFF:
                await self.show_screen(query, "home", chat_id)
            return
        try:
            await query.answer()
        except Exception:
            pass
        bot = getattr(context, "bot", None)
        if await self.handler._abandon_pending_states(chat_id, bot) and bot is not None:
            try:
                await self.send_bar_intro(bot, chat_id)
            except Exception as e:
                logger.info("[nav] bar after naming failed: %s", e)
        if action == "search":
            from_menu = self.from_live_menu(query, chat_id)
            await self.ask_search(query.message, chat_id, query=query if from_menu else None)
            return
        if action == "scan":
            await query.message.reply_text(
                SCAN_PROMPT, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(follow_up_rows("scan")),
            )
            return
        if action == "howto":
            from integrations.telegram_bot import HOWTO_TEXT

            await edit_or_send_menu(
                query, HOWTO_TEXT,
                InlineKeyboardMarkup([[_btn(BACK, menu_cb("help"))]]),
            )
            return
        await self._breadcrumb(query, action, chat_id)
        await self._run_command(update, context, action, query)

    async def _run_command(self, update, context, action: str, query) -> None:
        h = self.handler
        commands = {
            "fridge": ("/fridge", h._cmd_fridge, []),
            "recent": ("/recent", h._cmd_recent, []),
            "stock": ("/stock", h._cmd_stock, []),
            "status": ("/status", h._cmd_status, []),
            "locs": ("/storages", h._cmd_storages, []),
            "list": ("/list", h._cmd_list, []),
            "clist": ("/club list", h._cmd_club, ["list"]),
            "clubs": ("/club", h._cmd_club, []),
            "cnew": ("/club new", h._cmd_club, ["new"]),
            "cinv": ("/club invite", h._cmd_club, ["invite"]),
            "tasteoff": ("/club tasteoff", h._cmd_club, ["tasteoff"]),
            "discover": ("/club discover", h._cmd_club, ["discover"]),
            "members": ("/household", h._cmd_household, []),
            "hinv": ("/invite", h._cmd_invite, []),
            "switch": ("/switch", h._cmd_switch, []),
            "help": ("/help", h._cmd_help, []),
            "snew": ("/newcellar", h._cmd_invite_new, []),
        }
        staff = {
            "sadmin": ("/admin", "cmd_admin"),
            "sstats": ("/stats", "cmd_stats"),
            "schat": ("/chatstats", "cmd_chatstats"),
            "shh": ("/households", "cmd_households"),
        }
        message = getattr(query, "message", None)
        if action in commands:
            text, fn, args = commands[action]
            await fn(TapUpdate(update, message, text), ArgsContext(context, args))
            return
        if action in staff:
            from integrations import telegram_admin

            text, name = staff[action]
            fn = getattr(telegram_admin, name)
            await fn(TapUpdate(update, message, text), ArgsContext(context), h)
            return
        logger.info("[nav] unknown action %r", action)
        await self.show_screen(query, "home", chat_id=str(update.effective_chat.id))


def _bot_of(query):
    getter = getattr(query, "get_bot", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None
