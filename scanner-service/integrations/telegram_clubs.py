"""Private wine clubs: /club, join via /start, feed, peek, dismantle.

Slash command /club stays English. Dialogue and message buttons use the catalog.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from integrations.copy import t
from integrations.telegram_invites import (
    CALLBACK_BUDGET,
    I1_COPY,
    NAME_MAX,
    default_cellar_name,
    esc,
    format_until,
    invite_link,
    name_keyboard,
    reverse_geocode,
    share_keyboard,
    validate_typed_name,
)

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 30 * 60
# "Say something" takes only the next message, and only within two minutes —
# the same rule as the menu's search prompt (telegram_nav.PENDING_TTL_SECONDS).
COMMENT_TTL_SECONDS = 2 * 60
COMMENT_MAX = 280
# feature_club_threads: the Say something / Reply prompt lasts 15 minutes and
# lives in a CardStore (Redis when set), so it survives a scanner restart.
THREAD_PROMPT_TTL_SECONDS = 15 * 60
# Inside this window (or as a Telegram reply to the prompt/card/ping) the next
# message is taken as the comment; after it the sender is asked first.
THREAD_DIRECT_SECONDS = 2 * 60
THREAD_PAGE_CHARS = 3600
VOTE_GLYPH = {"down": "\U0001F44E", "up": "\U0001F44D", "meh": ""}
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
TYPE_LABEL = {
    "RE": "Red", "WH": "White", "RO": "Rosé", "SP": "Sparkling",
    "DE": "Dessert", "FO": "Fortified", "OR": "Orange",
}


def _type_label(loc: str, code: str) -> str:
    key = f"club.type.{code}"
    label = t(loc, key)
    if label == key:
        return TYPE_LABEL.get(code, code)
    return label


def clubs_enabled(handler) -> bool:
    flag_on = getattr(handler, "flag_on", None)
    if callable(flag_on):
        return bool(flag_on("feature_clubs"))
    return bool((getattr(handler, "flags", None) or {}).get("feature_clubs"))


def global_clubs_enabled(handler) -> bool:
    if not clubs_enabled(handler):
        return False
    flag_on = getattr(handler, "flag_on", None)
    if callable(flag_on):
        return bool(flag_on("feature_global_clubs"))
    return bool((getattr(handler, "flags", None) or {}).get("feature_global_clubs"))


def _is_staff(handler, update) -> bool:
    chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
    return chat_id in [str(c) for c in (getattr(handler, "admin_chat_ids", None) or [])]


def locale_for(handler, update=None, chat_id=None, language_code=None) -> str:
    getter = getattr(handler, "locale_for", None)
    if callable(getter):
        return getter(update=update, chat_id=chat_id, language_code=language_code)
    from integrations.copy import locale_from_telegram

    user = getattr(update, "effective_user", None) if update is not None else None
    return locale_from_telegram(
        language_code or (getattr(user, "language_code", None) if user else None)
    )


def club_cb(*parts) -> str:
    data = "cl:" + ":".join(str(p) for p in parts)
    if len(data.encode("utf-8")) > CALLBACK_BUDGET:
        raise ValueError(f"callback_data is too long: {data!r}")
    return data


REACTION_PALETTE = ("\U0001F377", "\U0001F60D", "\U0001F44D", "\U0001F602", "\U0001F914")
PALETTE = REACTION_PALETTE


def reaction_buttons(event_id, counts=None) -> list:
    """One compact row of emoji buttons ('🍷 3' when the count is > 0).
    Reusable for cards, comment pings and relay asks."""
    counts = list(counts or [0] * len(PALETTE))
    return [
        InlineKeyboardButton(
            f"{emoji} {n}" if n else emoji,
            callback_data=club_cb("rx", event_id, i),
        )
        for i, (emoji, n) in enumerate(zip(PALETTE, counts))
    ]


def parse_club_cb(data: str) -> tuple[str, list[str]] | None:
    if not (data or "").startswith("cl:"):
        return None
    bits = data.split(":")
    return bits[1], bits[2:]


def policy_key(visibility: str, badges: bool) -> str:
    vis = visibility if visibility in ("none", "summary", "full") else "full"
    return f"club.policy.{vis}"


def policy_of_key(visibility: str) -> str:
    vis = visibility if visibility in ("none", "summary", "full") else "full"
    return f"club.policy_of.{vis}"


def _visibility_label(loc, vis: str) -> str:
    return t(loc, f"club.share.{vis if vis in ('none', 'summary', 'full') else 'full'}")


def _event_action_row(event: dict, loc: str, flags=None) -> list:
    if (event.get("kind") or "opened") != "opened" or event.get("retracted"):
        return []
    event_id = event.get("id")
    if not event_id:
        return []
    flags = flags or {}
    row = []
    if flags.get("feature_club_comments"):
        row.append(InlineKeyboardButton(
            t(loc, "club.comment.btn"), callback_data=club_cb("cm", event_id),
        ))
    if flags.get("feature_lists"):
        row.append(InlineKeyboardButton(
            t(loc, "club.td.btn"), callback_data=club_cb("td", event_id),
        ))
    return row


class _HeldTextUpdate:
    """A callback tap replaying the sender's held text as a normal message.
    Telegram objects are frozen, so wrap instead of mutating."""

    def __init__(self, update, message, text):
        self._update = update
        self.message = _HeldTextMessage(message, text)
        self.effective_message = self.message
        self.callback_query = None

    def __getattr__(self, name):
        return getattr(self._update, name)


class _HeldTextMessage:
    def __init__(self, inner, text):
        self._inner = inner
        self.text = text
        self.reply_to_message = None

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cancel_markup() -> InlineKeyboardMarkup:
    from integrations.telegram_nav import CANCEL, menu_cb
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(CANCEL, callback_data=menu_cb("x", "cancel")),
    ]])


def _menu_markup() -> InlineKeyboardMarkup:
    from integrations.telegram_nav import MENU, menu_cb
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(MENU, callback_data=menu_cb("new")),
    ]])


def _wine_line(event: dict, loc: str) -> str:
    kind = event.get("kind")
    name = event.get("actor_name") or "Someone"
    wine = event.get("wine_name") or "wine"
    vintage = event.get("vintage") or ""
    if kind == "joined":
        return t(loc, "club.feed.joined", name=esc(name))
    if kind == "staff_note":
        note = ((event.get("payload") or {}).get("text") or event.get("wine_name") or "")
        return f"\U0001F30D {esc(note)}"
    if kind == "tasteoff":
        title = event.get("wine_name") or "Tasting"
        return f"\U0001F3C6 {esc(title)}"
    if event.get("retracted"):
        body = t(loc, "club.feed.put_back", name=esc(name), vintage=vintage, wine=esc(wine))
        return " ".join(body.split())
    body = t(loc, "club.feed.opened", name=esc(name), vintage=vintage, wine=esc(wine))
    body = " ".join(body.split())
    glyph = VOTE_GLYPH.get(event.get("vote") or "", "")
    return f"{body} {glyph}".rstrip()


def _discover_row(loc, flags) -> list:
    if not (flags or {}).get("feature_global_clubs"):
        return []
    return [InlineKeyboardButton(
        t(loc, "club.discover.btn"), callback_data=club_cb("dc"),
    )]


def _short_date(iso) -> str:
    """'2026-11-13T22:30:03+00:00' -> '13 Nov'."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso)).strftime("%-d %b")
    except (TypeError, ValueError):
        return ""


def _winner(row: dict) -> str:
    scored = [b for b in row.get("bottles") or [] if b.get("average") is not None]
    if not scored:
        return ""
    best = max(scored, key=lambda b: b["average"])
    return f"{best.get('label') or ''} ({best['average']:.1f})"


def build_scoring(body: dict, loc: str, *, host: bool = False):
    """The scoring card: every wine's full name in the text, then per wine a
    name button (tapping it only shows the name) and a 1-5 row with a tick on
    your current score. It stays up after each tap so every wine can be
    scored, and re-scored."""
    tid = body.get("id")
    bottles = body.get("bottles") or []
    lines = [
        f"<b>{esc(body.get('title') or '')}</b>",
        t(loc, "club.to.scoring_hint"),
        "",
    ]
    rows = []
    for n, bottle in enumerate(bottles, start=1):
        label = bottle.get("label") or "wine"
        mine = bottle.get("your_score")
        lines.append(f"{n}. {esc(label)}" + (f" — {mine}" if mine else ""))
        rows.append([InlineKeyboardButton(
            f"\U0001F377 {n}. {label[:40]}",
            callback_data=club_cb("tn", tid, bottle["id"]),
        )])
        rows.append([
            InlineKeyboardButton(
                f"\u2705{score}" if mine == score else str(score),
                callback_data=club_cb("ts", tid, bottle["id"], score),
            )
            for score in range(1, 6)
        ])
    scorers = body.get("scorers") or []
    if scorers:
        names = ", ".join(
            f"{esc(s.get('name') or '?')} " + ("\u2713" if s.get("done") else "\u2026")
            for s in scorers
        )
        lines += ["", t(loc, "club.to.scored_by", names=names)]
    if host:
        rows.append([InlineKeyboardButton(t(loc, "club.to.close"), callback_data=club_cb("tc", tid))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_home(payload: dict, loc: str, flags=None) -> tuple[str, InlineKeyboardMarkup]:
    name = payload.get("name") or "Club"
    members = payload.get("member_count") or 1
    vis = payload.get("cellar_visibility") or "full"
    badges = payload.get("badges_visible")
    policy = _visibility_label(loc, vis)
    badge_bit = t(loc, "club.share.badges_on" if badges else "club.share.badges_off")
    is_global = payload.get("kind") == "global"
    prefix = "\U0001F30D " if is_global else "\U0001F377 "
    lines = [
        f"{prefix}<b>{esc(name)}</b> · {t(loc, 'club.home.n_members', n=members)}",
    ]
    if is_global:
        desc = (payload.get("description") or "").strip()
        if desc:
            lines.append(esc(desc))
        if payload.get("frozen"):
            lines.append(t(loc, "club.global.frozen"))
        lines.append("")
    else:
        lines.extend([f"{esc(policy)} · {esc(badge_bit)}", ""])
    events = [e for e in (payload.get("events") or []) if not e.get("retracted")]
    if not events:
        lines.append(t(loc, "club.feed.empty"))
    else:
        for event in events[:5]:
            lines.append(_wine_line(event, loc))
    flags = flags or {}
    first = []
    if not is_global:
        first.append(
            InlineKeyboardButton(t(loc, "club.home.invite"), callback_data=club_cb("i", payload["id"])),
        )
    first.append(
        InlineKeyboardButton(t(loc, "club.home.members"), callback_data=club_cb("m", payload["id"])),
    )
    keyboard = [
        first,
        [
            InlineKeyboardButton(t(loc, "club.home.feed"), callback_data=club_cb("fd", payload["id"], 0)),
            InlineKeyboardButton(
                t(loc, "club.home.unmute" if payload.get("feed_muted") else "club.home.mute"),
                callback_data=club_cb("u", payload["id"]),
            ),
        ],
    ]
    if flags.get("feature_lists"):
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.list.title"), callback_data=club_cb("li", payload["id"]),
        )])
    last = []
    if payload.get("role") == "owner" and not is_global:
        last.append(InlineKeyboardButton(
            t(loc, "club.home.settings"), callback_data=club_cb("s", payload["id"]),
        ))
    last.append(InlineKeyboardButton(
        t(loc, "club.home.leave"), callback_data=club_cb("l", payload["id"]),
    ))
    keyboard.append(last)
    discover = _discover_row(loc, flags)
    if discover:
        keyboard.append(discover)
    # Follow-ups: say something on the newest bottle opened, taste-off, menu.
    from integrations.telegram_nav import follow_up_rows
    latest = next(
        (e for e in events if (e.get("kind") or "opened") == "opened" and e.get("id")),
        None,
    )
    keyboard.extend(follow_up_rows("club", {
        "flags": flags,
        "club_id": payload["id"],
        "event_id": (latest or {}).get("id"),
    }))
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


class ClubFlow:
    def __init__(self, handler):
        self.handler = handler
        self._states: dict[str, dict] = {}
        # event_id -> feed event, from the last lookup (names the comment prompt).
        self._events_seen: dict[int, dict] = {}

    def locale(self, update=None, chat_id=None) -> str:
        return locale_for(self.handler, update=update, chat_id=chat_id)

    # -- threads (feature_club_threads) ------------------------------------
    def threads_on(self) -> bool:
        return bool(
            self.handler.flag_on("feature_club_comments")
            and self.handler.flag_on("feature_club_threads")
        )

    def _prompt_store(self):
        store = getattr(self.handler, "_club_comment_store", None)
        if store is None:
            from integrations.card_store import CardStore
            store = CardStore(
                ttl=THREAD_PROMPT_TTL_SECONDS, prefix="whiney:clubcm:",
            )
            try:
                self.handler._club_comment_store = store
            except Exception:
                pass
        return store

    def _prompt_get(self, chat_id) -> dict | None:
        try:
            entry = self._prompt_store().get(str(chat_id))
        except Exception as e:
            logger.info("[club] prompt store read failed: %s", e)
            return None
        if not entry:
            return None
        if time.time() - float(entry.get("at") or 0) > THREAD_PROMPT_TTL_SECONDS:
            self._prompt_drop(chat_id)
            return None
        return dict(entry)

    def _prompt_put(self, chat_id, **fields) -> None:
        try:
            self._prompt_store()[str(chat_id)] = dict(fields, at=time.time())
        except Exception as e:
            logger.info("[club] prompt store write failed: %s", e)

    def _prompt_hold(self, chat_id, prompt: dict, text: str) -> None:
        """Keep the prompt and remember a held text, with a fresh window so
        Post / No still works when the user taps a while later."""
        try:
            self._prompt_store()[str(chat_id)] = dict(prompt, held=text, at=time.time())
        except Exception as e:
            logger.info("[club] prompt store write failed: %s", e)

    def _prompt_drop(self, chat_id) -> None:
        try:
            self._prompt_store().pop(str(chat_id))
        except Exception as e:
            logger.info("[club] prompt store drop failed: %s", e)

    def thread_prompt(self, chat_id) -> dict | None:
        if not self.threads_on():
            return None
        return self._prompt_get(chat_id)

    def drop_comment_prompt(self, chat_id) -> None:
        """A command, label or other flow takes the one 'next message' slot."""
        if self.threads_on():
            self._prompt_drop(chat_id)

    def _api(self, update):
        return self.handler._get_api(update)

    def _staff_api(self):
        """Club redeem is staff-only (it returns the joiner's token). The
        default client is a member's token and gets a 403, so use the staff
        client server.py builds for invite preview and redeem."""
        api = getattr(self.handler, "admin_api", None)
        if api is not None:
            return api
        logger.warning("club staff client missing; falling back to handler.api")
        return getattr(self.handler, "api", None)

    def bot_username(self) -> str:
        return getattr(self.handler, "bot_username", None) or "Whiney"

    def state_for(self, chat_id) -> dict | None:
        key = str(chat_id)
        state = self._states.get(key)
        if state is None:
            return None
        now = time.monotonic()
        if now - state.get("updated_at", now) > STATE_TTL_SECONDS:
            self._states.pop(key, None)
            return None
        state["updated_at"] = now
        return state

    def clear(self, chat_id) -> None:
        self._states.pop(str(chat_id), None)

    def remember(self, chat_id, **fields) -> dict:
        key = str(chat_id)
        state = self.state_for(key) or {}
        state.update(fields)
        state["updated_at"] = time.monotonic()
        self._states[key] = state
        return state

    async def _reply(self, update, text, query=None, **kwargs):
        if query is not None and getattr(query, "message", None) is not None:
            await query.message.reply_text(text, **kwargs)
            return
        if getattr(update, "message", None) is not None:
            await update.message.reply_text(text, **kwargs)

    async def cmd_club(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loc = self.locale(update)
        if not clubs_enabled(self.handler):
            await update.message.reply_text(t(loc, "club.off"))
            return
        args = list(context.args or [])
        sub = (args[0].lower() if args else "")
        if sub == "new":
            await self._start_found(update)
            return
        if sub == "invite":
            if len(args) > 1 and args[1].lower() == "cancel":
                await self._invite_cancel(update)
            else:
                await self._invite(update)
            return
        if sub == "feed":
            await self._show_feed(update, page=0)
            return
        if sub == "members":
            await self._show_members(update)
            return
        if sub == "leave":
            await self._leave_confirm(update)
            return
        if sub == "list":
            await self._show_club_list(update)
            return
        if sub == "tasteoff":
            await self._start_tasteoff(update)
            return
        if sub == "discover":
            await self._show_discover(update)
            return
        if sub == "post":
            await self.cmd_clubpost(update, context)
            return
        await self._entry(update)

    async def _entry(self, update):
        loc = self.locale(update)
        api = self._api(update)
        try:
            data = api.list_clubs()
        except Exception as e:
            logger.warning("[club] list failed: %s", e)
            await update.message.reply_text(t(loc, "club.err"))
            return
        if data.get("status_code") == 404:
            await update.message.reply_text(t(loc, "club.off"))
            return
        if data.get("error"):
            await update.message.reply_text(t(loc, "club.err"))
            return
        clubs = data.get("clubs") or []
        flags = self._flags()
        discover = _discover_row(loc, flags)
        if not clubs:
            rows = [[
                InlineKeyboardButton(t(loc, "club.found.btn"), callback_data=club_cb("f")),
            ]]
            if discover:
                rows.append(discover)
            await update.message.reply_text(
                t(loc, "club.pitch"), reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if len(clubs) == 1:
            await self._show_home(update, clubs[0]["id"])
            return
        rows = []
        for club in clubs:
            mark = "\u2705 " if club.get("is_last_used") else ""
            prefix = "\U0001F30D " if club.get("kind") == "global" else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{prefix}{club.get('name') or 'Club'}",
                callback_data=club_cb("x", club["id"]),
            )])
        rows.append([InlineKeyboardButton(t(loc, "club.found.btn"), callback_data=club_cb("f"))])
        if discover:
            rows.append(discover)
        await update.message.reply_text(
            t(loc, "club.picker.title"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _start_found(self, update, query=None):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        self.remember(chat_id, step="name", visibility="full", badges=True)
        text = t(loc, "club.found.name")
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text)
        else:
            await update.message.reply_text(text)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        chat_id = str(update.effective_chat.id)
        if clubs_enabled(self.handler) and self.threads_on():
            prompt = self._prompt_get(chat_id)
            if prompt:
                return await self._take_or_ask(update, context, chat_id, prompt)
        state = self.state_for(chat_id)
        if not state:
            return False
        step = state.get("step")
        loc = self.locale(update)
        text = (update.message.text or "").strip()
        if not clubs_enabled(self.handler):
            await update.message.reply_text(t(loc, "club.off"))
            self.clear(chat_id)
            return True
        if step == "comment" and not self.handler.flag_on("feature_club_comments"):
            self.clear(chat_id)
            await update.message.reply_text(t(loc, "club.comment.off"))
            return True
        if step in ("tasteoff_title", "tasteoff_label") and not self.handler.flag_on(
            "feature_tasteoffs",
        ):
            self.clear(chat_id)
            await update.message.reply_text(t(loc, "club.to.off"))
            return True
        if step == "club_list_add" and not self.handler.flag_on("feature_lists"):
            self.clear(chat_id)
            await update.message.reply_text(t(loc, "club.td.off"))
            return True
        if step == "club_post" and not global_clubs_enabled(self.handler):
            self.clear(chat_id)
            await update.message.reply_text(t(loc, "club.global.off"))
            return True
        if step == "name":
            err = validate_typed_name(text)
            if err:
                await update.message.reply_text(t(loc, "club.name.retry"))
                return True
            self.remember(chat_id, step="share", name=text.strip()[:NAME_MAX])
            await self._share_card(update)
            return True
        if step in ("compound_name", "compound_consent"):
            if text.casefold() == "skip":
                display = ""
                user = getattr(update, "effective_user", None)
                if user is not None:
                    display = user.first_name or ""
                text = default_cellar_name(display)
            else:
                err = validate_typed_name(text)
                if err:
                    await update.message.reply_text(
                        t(loc, "club.name.retry"), reply_markup=name_keyboard(),
                    )
                    return True
            self.remember(
                chat_id, step="join", household_name=text.strip()[:NAME_MAX],
            )
            await update.message.reply_text(
                t(loc, "club.named.ok"), reply_markup=self.handler._invite_flow()._bar_if_known(update),
            )
            await self._join_card(update, query=None)
            return True
        if step == "join" and text.casefold() == "skip":
            return True
        if step == "comment":
            return await self._submit_comment(update, text)
        if step == "tasteoff_title":
            return await self._tasteoff_named(update, text)
        if step == "tasteoff_label":
            return await self._tasteoff_add_typed(update, text)
        if step == "club_list_add":
            return await self._club_list_add_typed(update, text)
        if step == "club_post":
            return await self._submit_club_post(update, text)
        return False

    async def on_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id)
        if not state or state.get("step") != "compound_name":
            return False
        loc = update.message.location
        suburb = None
        if loc is not None:
            suburb = reverse_geocode(loc.latitude, loc.longitude)
        if not suburb:
            await update.message.reply_text(
                t(self.locale(update), "club.name.retry"),
                reply_markup=name_keyboard(),
            )
            return True
        self.remember(chat_id, step="join", household_name=suburb[:NAME_MAX])
        await update.message.reply_text(
            t(self.locale(update), "club.named.ok"), reply_markup=self.handler._invite_flow()._bar_if_known(update),
        )
        await self._join_card(update, query=None)
        return True

    async def _share_card(self, update, query=None):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        name = state.get("name") or "Club"
        vis = state.get("visibility") or "full"
        badges = bool(state.get("badges", True))
        text = t(loc, "club.share.ask", name=esc(name))
        mark = lambda wanted, current: "\u2705 " if wanted == current else ""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{mark('full', vis)}{t(loc, 'club.share.full')}",
                callback_data=club_cb("v", 2),
            )],
            [InlineKeyboardButton(
                f"{mark('summary', vis)}{t(loc, 'club.share.summary')}",
                callback_data=club_cb("v", 1),
            )],
            [InlineKeyboardButton(
                f"{mark('none', vis)}{t(loc, 'club.share.none')}",
                callback_data=club_cb("v", 0),
            )],
            [
                InlineKeyboardButton(
                    t(loc, "club.share.badges_on" if badges else "club.share.badges_off"),
                    callback_data=club_cb("b", 0 if badges else 1),
                ),
            ],
            [InlineKeyboardButton(t(loc, "club.create.btn"), callback_data=club_cb("c"))],
        ])
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _create(self, update, query):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id)
        if not state or not state.get("name"):
            await self._start_found(update, query=query)
            return
        vis_map = {0: "none", 1: "summary", 2: "full"}
        vis = state.get("visibility") or "full"
        if vis not in vis_map.values():
            vis = vis_map.get(int(vis), "full") if str(vis).isdigit() else vis
        api = self._api(update)
        try:
            result = api.create_club(
                state["name"],
                cellar_visibility=vis,
                badges_visible=bool(state.get("badges", True)),
            )
        except Exception as e:
            logger.warning("[club] create failed: %s", e)
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.err"))
            return
        if result.get("status_code") == 409:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.name_taken"))
            return
        if result.get("error") or not result.get("id"):
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.err"))
            return
        self.clear(chat_id)
        await self._show_home(update, result["id"], query=query)
        await self._invite(update, club_id=result["id"], query=query)

    async def _invite(self, update, club_id=None, query=None):
        loc = self.locale(update)
        api = self._api(update)
        club_id = club_id or await self._current_club_id(update)
        if not club_id:
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        home = {}
        try:
            home = api.get_club(club_id) or {}
        except Exception:
            home = {}
        if home.get("kind") == "global":
            await self._show_home(update, club_id, query=query)
            return
        try:
            result = api.mint_club_invite(club_id)
        except Exception as e:
            logger.warning("[club] mint invite failed: %s", e)
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        if result.get("status_code") == 409:
            await self._reply(update, t(loc, "club.end.in_progress"), query=query)
            return
        if result.get("error") or not result.get("code"):
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        vis = result.get("cellar_visibility") or "full"
        policy = t(loc, policy_key(vis, result.get("badges_visible", True)))
        link = invite_link(self.bot_username(), result["code"])
        when = format_until(result.get("expires_at")) or ""
        text = t(
            loc, "club.invite.body",
            name=esc(result.get("club_name") or "club"),
            policy=policy,
            link=link,
            date=when,
            n=result.get("max_uses") or 20,
        )
        share = t(loc, "club.invite.share", name=result.get("club_name") or "us")
        keyboard = share_keyboard(link, share, loc)
        await self._reply(update, text, query=query, parse_mode="HTML", reply_markup=keyboard)

    async def _invite_cancel(self, update):
        loc = self.locale(update)
        club_id = await self._current_club_id(update)
        if not club_id:
            await update.message.reply_text(t(loc, "club.err"))
            return
        api = self._api(update)
        try:
            result = api.cancel_club_invite(club_id)
        except Exception:
            await update.message.reply_text(t(loc, "club.err"))
            return
        if result.get("error"):
            await update.message.reply_text(t(loc, "club.err"))
            return
        await update.message.reply_text(
            t(loc, "club.invite.cancelled", code=result.get("code") or ""),
        )

    async def _current_club_id(self, update):
        api = self._api(update)
        try:
            data = api.list_clubs()
        except Exception:
            return None
        clubs = data.get("clubs") or []
        last = next((c for c in clubs if c.get("is_last_used")), None)
        if last:
            return last["id"]
        if len(clubs) == 1:
            return clubs[0]["id"]
        return None

    async def _show_home(self, update, club_id, query=None):
        loc = self.locale(update)
        api = self._api(update)
        try:
            payload = api.get_club(club_id)
        except Exception as e:
            logger.warning("[club] home failed: %s", e)
            payload = {"error": str(e)}
        if payload.get("error"):
            text = t(loc, "club.not_member" if payload.get("status_code") == 404 else "club.err")
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        flags = self._flags()
        text, markup = build_home(payload, loc, flags=flags)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _show_members(self, update, club_id=None, query=None):
        loc = self.locale(update)
        club_id = club_id or await self._current_club_id(update)
        api = self._api(update)
        try:
            home = api.get_club(club_id)
            payload = api.club_members(club_id)
        except Exception as e:
            logger.warning("[club] members failed: %s", e)
            text = t(loc, "club.err")
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        vis = home.get("cellar_visibility") or "full"
        lines = [
            f"<b>{esc(home.get('name') or 'Club')}</b>",
            _visibility_label(loc, vis),
            "",
        ]
        members = payload.get("members") or []
        households = payload.get("households") or []
        shown_hh = set()
        if not members:
            lines.append(t(loc, "club.members.empty"))
        keyboard = []
        for member in members:
            hh_id = member.get("household_id")
            if hh_id and hh_id in shown_hh:
                continue
            label = member.get("display_name") or "Member"
            if member.get("role") == "owner":
                label = f"\U0001F451 {label}"
            if hh_id and home.get("kind") != "global":
                shown_hh.add(hh_id)
                hh = next((h for h in households if h.get("household_id") == hh_id), None)
                if hh and len(hh.get("user_ids") or []) > 1:
                    label = f"{hh.get('name')} · {label}"
            lines.append(esc(label))
            uid = member.get("user_id")
            row = []
            if (member.get("peek") or vis) in ("summary", "full"):
                row.append(InlineKeyboardButton(
                    t(loc, "club.peek.btn"),
                    callback_data=club_cb("p", club_id, uid),
                ))
            if member.get("badges") if "badges" in member else home.get("badges_visible"):
                row.append(InlineKeyboardButton(
                    t(loc, "club.badges.btn"),
                    callback_data=club_cb("g", club_id, uid),
                ))
            if row:
                keyboard.append(row)
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.back"), callback_data=club_cb("h", club_id),
        )])
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _show_peek(self, update, club_id, user_id, query):
        loc = self.locale(update)
        api = self._api(update)
        from integrations.telegram_bot import _safe_edit
        try:
            payload = api.club_peek(club_id, user_id)
        except Exception as e:
            logger.warning("[club] peek failed: %s", e)
            await _safe_edit(query, t(loc, "club.err"))
            return
        if payload.get("error"):
            await _safe_edit(query, t(loc, "club.peek.none"))
            return
        vis = payload.get("visibility")
        hh = payload.get("household_name") or "Cellar"
        if vis == "summary":
            title = t(loc, "club.peek.summary_title", household=esc(hh))
            types = payload.get("counts_by_type") or {}
            grapes = payload.get("counts_by_grape") or {}
            type_bits = [
                f"{_type_label(loc, code)} ({n})"
                for code, n in types.items()
            ]
            grape_bits = [f"{name} ×{n}" for name, n in list(grapes.items())[:8]]
            lines = [f"<b>{title}</b>", "", " · ".join(type_bits), " · ".join(grape_bits)]
        else:
            title = t(loc, "club.peek.full_title", household=esc(hh))
            lines = [f"<b>{title}</b>", ""]
            wines = payload.get("wines") or []
            for wine in wines:
                vintage = wine.get("vintage") or ""
                region = wine.get("region") or wine.get("producer") or wine.get("country") or ""
                bit = f"{esc(wine.get('name') or '')} {vintage}".strip()
                if region:
                    bit += f" · {esc(region)}"
                bit += f" · ×{wine.get('stock_count') or 0}"
                lines.append(bit)
            if payload.get("truncated"):
                extra = max(0, (payload.get("total_bottles") or 0) - len(wines))
                if extra:
                    lines.append(t(loc, "club.peek.more", n=extra))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("m", club_id)),
        ]])
        await _safe_edit(query, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

    async def _show_badges(self, update, club_id, user_id, query):
        loc = self.locale(update)
        api = self._api(update)
        from integrations.telegram_bot import _safe_edit
        try:
            payload = api.club_badges(club_id, user_id)
        except Exception:
            await _safe_edit(query, t(loc, "club.err"))
            return
        if payload.get("error") or not payload.get("enabled"):
            await _safe_edit(query, t(loc, "club.badges.off"))
            return
        lines = []
        for item in (payload.get("achievements") or [])[:12]:
            lines.append(f"{item.get('emoji') or ''} {esc(item.get('title') or '')}".strip())
        if not lines:
            lines = [t(loc, "club.badges.off")]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("m", club_id)),
        ]])
        await _safe_edit(query, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

    async def _show_feed(self, update, club_id=None, page=0, query=None):
        loc = self.locale(update)
        club_id = club_id or await self._current_club_id(update)
        api = self._api(update)
        try:
            payload = api.club_feed(club_id, limit=5, offset=max(0, page) * 5)
        except Exception:
            payload = {"error": "err"}
        keyboard = []
        if payload.get("error"):
            text = t(loc, "club.err")
        else:
            events = payload.get("events") or []
            lines = [f"\U0001F377 <b>{esc(t(loc, 'club.home.feed'))}</b>", ""]
            if not events:
                lines.append(t(loc, "club.feed.empty"))
            flags = {
                "feature_lists": self.handler.flag_on("feature_lists"),
                "feature_club_comments": self.handler.flag_on("feature_club_comments"),
            }
            for event in events:
                line = _wine_line(event, loc)
                if event.get("retracted"):
                    line = f"<i>{line}</i>"
                lines.append(line)
                row = _event_action_row(event, loc, flags)
                if (
                    self.threads_on() and event.get("id")
                    and (event.get("comment_count") or 0) > 0
                ):
                    row = list(row) + [InlineKeyboardButton(
                        t(loc, "club.thread.btn_view", n=event["comment_count"]),
                        callback_data=club_cb("th", club_id, event["id"], 0),
                    )]
                if row:
                    keyboard.append(row)
            text = "\n".join(lines)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                t(loc, "club.feed.newer"), callback_data=club_cb("fd", club_id, page - 1),
            ))
        count = payload.get("count") or 0
        if (page + 1) * 5 < count:
            nav.append(InlineKeyboardButton(
                t(loc, "club.feed.older"), callback_data=club_cb("fd", club_id, page + 1),
            ))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.back"), callback_data=club_cb("h", club_id),
        )])
        markup = InlineKeyboardMarkup(keyboard)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _toggle_mute(self, update, club_id, query):
        loc = self.locale(update)
        api = self._api(update)
        home = api.get_club(club_id)
        muted = not bool(home.get("feed_muted"))
        api.patch_club_membership(club_id, feed_muted=muted)
        key = "club.muted" if muted else "club.unmuted"
        from integrations.telegram_bot import _safe_edit
        unmute = t(loc, "club.home.unmute" if muted else "club.home.mute")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(unmute, callback_data=club_cb("u", club_id)),
            InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("h", club_id)),
        ]])
        await _safe_edit(
            query, t(loc, key, name=home.get("name") or "club"),
            reply_markup=keyboard,
        )

    async def _leave_confirm(self, update, club_id=None, query=None):
        loc = self.locale(update)
        club_id = club_id or await self._current_club_id(update)
        api = self._api(update)
        home = api.get_club(club_id) if club_id else {}
        name = home.get("name") or "this club"
        text = t(loc, "club.leave.ask", name=esc(name))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(loc, "club.leave.yes"), callback_data=club_cb("ly", club_id)),
            InlineKeyboardButton(t(loc, "club.leave.keep"), callback_data=club_cb("h", club_id)),
        ]])
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _leave(self, update, club_id, query):
        loc = self.locale(update)
        api = self._api(update)
        home = api.get_club(club_id)
        result = api.leave_club(club_id)
        from integrations.telegram_bot import _safe_edit
        if result.get("error") == "hand_over_required":
            members = api.club_members(club_id).get("members") or []
            rows = []
            for member in members:
                if member.get("role") == "owner":
                    continue
                rows.append([InlineKeyboardButton(
                    f"{t(loc, 'club.handover.btn')} {member.get('display_name')}",
                    callback_data=club_cb("ho", club_id, member["user_id"]),
                )])
            rows.append([InlineKeyboardButton(
                t(loc, "club.back"), callback_data=club_cb("h", club_id),
            )])
            await _safe_edit(
                query, t(loc, "club.handover.need"),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        await _safe_edit(query, t(loc, "club.left", name=home.get("name") or "club"))

    async def _settings(self, update, club_id, query):
        loc = self.locale(update)
        api = self._api(update)
        home = api.get_club(club_id)
        if home.get("kind") == "global":
            await self._show_home(update, club_id, query=query)
            return
        if home.get("role") != "owner":
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.forbidden"))
            return
        vis = home.get("cellar_visibility") or "full"
        badges = bool(home.get("badges_visible"))
        mark = lambda wanted, current: "\u2705 " if wanted == current else ""
        text = t(loc, "club.share.ask", name=esc(home.get("name") or "club"))
        keyboard = [
            [InlineKeyboardButton(
                f"{mark('full', vis)}{t(loc, 'club.share.full')}",
                callback_data=club_cb("sv", club_id, 2),
            )],
            [InlineKeyboardButton(
                f"{mark('summary', vis)}{t(loc, 'club.share.summary')}",
                callback_data=club_cb("sv", club_id, 1),
            )],
            [InlineKeyboardButton(
                f"{mark('none', vis)}{t(loc, 'club.share.none')}",
                callback_data=club_cb("sv", club_id, 0),
            )],
            [InlineKeyboardButton(
                t(loc, "club.share.badges_on" if badges else "club.share.badges_off"),
                callback_data=club_cb("sb", club_id, 0 if badges else 1),
            )],
        ]
        if (home.get("member_count") or 1) > 1:
            keyboard.append([InlineKeyboardButton(
                t(loc, "club.end.btn"), callback_data=club_cb("z", club_id),
            )])
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.back"), callback_data=club_cb("h", club_id),
        )])
        from integrations.telegram_bot import _safe_edit
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    async def open_invite(self, update, preview: dict):
        loc = self.locale(update)
        if not clubs_enabled(self.handler):
            await update.message.reply_text(t(loc, "club.off"))
            return
        if preview.get("dismantle_in_progress"):
            await update.message.reply_text(t(loc, "club.end.in_progress"))
            return
        club = preview.get("club") or {}
        if preview.get("expired") or preview.get("used"):
            # Their own join may be what used it up: say so, not "expired".
            if club.get("id") and self._already_in_club(update, club["id"]):
                await update.message.reply_text(
                    t(loc, "club.join.already", club=esc(club.get("name") or "club")),
                    parse_mode="HTML",
                )
                await self._show_home(update, club["id"])
                return
            await update.message.reply_text(t(loc, "club.fresh"))
            return
        chat_id = str(update.effective_chat.id)
        code = preview.get("code")
        self.remember(
            chat_id,
            step="join",
            code=code,
            club_name=club.get("name") or "club",
            cellar_visibility=preview.get("cellar_visibility") or "full",
            badges_visible=preview.get("badges_visible", True),
            inviter=preview.get("inviter_display_name") or "",
            expires_at=preview.get("expires_at"),
        )
        known = self.handler._is_known(update)
        if not known:
            self.remember(chat_id, step="compound_name")
            display = ""
            user = getattr(update, "effective_user", None)
            if user is not None:
                display = user.first_name or ""
            await update.message.reply_text(
                I1_COPY.format(name=esc(display or "there")),
                parse_mode="HTML",
                reply_markup=name_keyboard(),
            )
            return
        households = []
        try:
            households = self._api(update).list_households() or []
        except Exception:
            households = []
        if len(households) > 1:
            self.remember(chat_id, step="pick_hh")
            rows = [[InlineKeyboardButton(
                h.get("name") or "Cellar",
                callback_data=club_cb("ph", code, h["id"]),
            )] for h in households]
            await update.message.reply_text(
                t(loc, "club.join.pick_hh", club=esc(club.get("name") or "club")),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if households:
            self.remember(chat_id, household_id=households[0]["id"], household_name=households[0].get("name"))
        await self._join_card(update)

    def _already_in_club(self, update, club_id) -> bool:
        if not self.handler._is_known(update):
            return False
        try:
            payload = self._api(update).list_clubs() or {}
        except Exception as e:
            logger.info("[club] membership check failed: %s", e)
            return False
        clubs = payload.get("clubs") if isinstance(payload, dict) else payload
        return any(
            isinstance(c, dict) and c.get("id") == club_id for c in (clubs or [])
        )

    async def _join_card(self, update, query=None):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        vis = state.get("cellar_visibility") or "full"
        household = state.get("household_name") or "your cellar"
        text = t(
            loc, "club.join.known",
            inviter=esc(state.get("inviter") or "Someone"),
            club=esc(state.get("club_name") or "club"),
            policy_of=t(loc, policy_of_key(vis)),
            household=esc(household),
        )
        code = state.get("code") or ""
        join_label = t(loc, "club.join.btn", name=state.get("club_name") or "club")
        # The button carries the chosen cellar, so a tap after a restart (state
        # gone) still shares the cellar this card named, not the default one.
        hh_id = state.get("household_id")
        join_cb = club_cb("j", code, hh_id) if hh_id else club_cb("j", code)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(join_label, callback_data=join_cb),
            InlineKeyboardButton(t(loc, "club.join.not_now"), callback_data=club_cb("nn", code)),
        ]])
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _ask_which_cellar(self, update, query, code, households):
        """Re-ask the cellar from a Join tap. State may be gone (restart), so
        re-read the invite: the next card must name the club and its policy."""
        from integrations.telegram_bot import _safe_edit
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        if not state.get("club_name") or state.get("code") != code:
            if state.get("code") != code:
                # Another invite's details must never label this one.
                for stale in ("club_name", "cellar_visibility", "badges_visible", "inviter", "expires_at"):
                    state.pop(stale, None)
            try:
                preview = self._staff_api().preview_invite(code) or {}
            except Exception:
                preview = {}
            if preview.get("expired") or preview.get("used"):
                self.clear(chat_id)
                club_id = (preview.get("club") or {}).get("id")
                if club_id and self._already_in_club(update, club_id):
                    await self._show_home(update, club_id, query=query)
                else:
                    await _safe_edit(query, t(loc, "club.fresh"))
                return
            if preview.get("kind") == "club":
                self.remember(
                    chat_id,
                    club_name=(preview.get("club") or {}).get("name") or "club",
                    cellar_visibility=preview.get("cellar_visibility") or "full",
                    badges_visible=preview.get("badges_visible", True),
                    inviter=preview.get("inviter_display_name") or "",
                    expires_at=preview.get("expires_at"),
                )
                state = self.state_for(chat_id) or {}
        self.remember(chat_id, step="pick_hh", code=code, household_id=None)
        await _safe_edit(
            query,
            t(loc, "club.join.pick_hh", club=esc(state.get("club_name") or "the club")),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                h.get("name") or "Cellar",
                callback_data=club_cb("ph", code, h["id"]),
            )] for h in households]),
        )

    async def _do_join(self, update, query, code: str, household_id=None):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        api = self._staff_api()
        from integrations.telegram_bot import _safe_edit
        if api is None:
            await _safe_edit(query, t(loc, "club.unreachable"))
            return
        known = self.handler._is_known(update)
        # The button names the cellar its card showed; it beats older state.
        household_id = household_id or state.get("household_id")
        if known and household_id is None:
            # No cellar on the tap (old button, state lost): never let the
            # server fall back to a default cellar the person didn't pick.
            try:
                households = self._api(update).list_households() or []
            except Exception:
                households = []
            if len(households) > 1:
                await self._ask_which_cellar(update, query, code, households)
                return
            if households:
                household_id = households[0].get("id")
        display = ""
        user = getattr(update, "effective_user", None)
        if user is not None:
            display = user.full_name or user.first_name or "Someone"
        try:
            result = api.redeem_club_invite(
                code, chat_id, display,
                household_name=state.get("household_name"),
                household_id=household_id,
            )
        except Exception:
            logger.exception("[club] redeem failed")
            await _safe_edit(query, t(loc, "club.unreachable"))
            return
        status = result.get("status_code")
        if status == 400 and result.get("error") == "household_not_yours" and known:
            try:
                households = self._api(update).list_households() or []
            except Exception:
                households = []
            if households:
                await self._ask_which_cellar(update, query, code, households)
                return
        if status == 404 and result.get("error") == "not_enabled":
            await _safe_edit(query, t(loc, "club.off"))
            return
        if status in (404, 410) or result.get("expired") or result.get("used") and not result.get("token"):
            await _safe_edit(query, t(loc, "club.fresh"))
            return
        if status == 409 and result.get("error") == "dismantle_in_progress":
            await _safe_edit(query, t(loc, "club.end.in_progress"))
            return
        if status == 409 and result.get("suggested_name"):
            suggestion = str(result.get("suggested_name") or "")[:NAME_MAX]
            self.remember(
                chat_id, step="compound_name", suggested_name=suggestion,
            )
            await _safe_edit(
                query, t(loc, "club.hh.taken"),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(suggestion, callback_data=club_cb("sn")),
                ]]),
            )
            return
        if isinstance(status, int) and status >= 500:
            await _safe_edit(query, t(loc, "club.unreachable"))
            return
        if (
            status == 400 and not known
            and "household_name" in str(result.get("error") or "")
        ):
            # A stranger whose typed cellar name was lost (bot restart): the
            # link re-runs the naming step.
            await _safe_edit(query, t(loc, "club.join.lost_place"))
            return
        if result.get("error") and not result.get("token"):
            log = logger.error if status in (401, 403) else logger.warning
            log("[club] redeem refused status=%s error=%s", status, result.get("error"))
            await _safe_edit(query, t(loc, "club.join.failed"))
            return
        self.clear(chat_id)
        # Only a newcomer is wired up here. A known chat keeps its client,
        # active cellar and currency: joining a club must not switch them.
        if result.get("token") and not known:
            try:
                await self.handler._invite_flow()._hot_add(chat_id, result)
            except Exception:
                logger.exception("[club] hot-add failed")
            context = getattr(query, "get_bot", None)
            bot = context() if callable(context) else None
            nav = getattr(self.handler, "_nav", None)
            if bot is not None and callable(nav):
                try:
                    await nav().send_bar_intro(bot, chat_id)
                except Exception as e:
                    logger.info("[club] bar intro failed: %s", e)
        club = result.get("club") or {}
        club_id = club.get("id")
        if club_id:
            await self._show_home(update, club_id, query=query)
        else:
            await _safe_edit(query, t(loc, "club.created", name=club.get("name") or "club"))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = update.callback_query
        parsed = parse_club_cb(query.data or "")
        if parsed is None:
            return False
        action, rest = parsed
        if action == "tn":
            return await self._tasteoff_name_tap(update, rest)
        if action in ("rx", "wt"):
            # These answer the tap themselves, with a toast.
            return await self._reaction_callback(update, query, action, rest)
        try:
            await query.answer()
        except Exception as e:
            logger.info("[club] answer() failed: %s", e)
        loc = self.locale(update)
        if not clubs_enabled(self.handler) and action != "nn":
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.off"))
            return True
        if action in ("dc", "dj", "jj", "gp") and not global_clubs_enabled(self.handler):
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.global.off"))
            return True
        if action == "gp" and not _is_staff(self.handler, update):
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.global.staff_only"))
            return True
        vis_map = {"0": "none", "1": "summary", "2": "full"}
        if action == "f":
            await self._start_found(update, query=query)
        elif action == "v":
            chat_id = str(query.message.chat_id)
            vis = vis_map.get(rest[0] if rest else "2", "full")
            self.remember(chat_id, visibility=vis, step="share")
            await self._share_card(update, query=query)
        elif action == "b":
            chat_id = str(query.message.chat_id)
            self.remember(chat_id, badges=(rest[0] == "1") if rest else True, step="share")
            await self._share_card(update, query=query)
        elif action == "c":
            await self._create(update, query)
        elif action == "h" or action == "x":
            await self._show_home(update, int(rest[0]), query=query)
        elif action == "m":
            await self._show_members(update, int(rest[0]), query=query)
        elif action == "p":
            await self._show_peek(update, int(rest[0]), int(rest[1]), query)
        elif action == "g":
            await self._show_badges(update, int(rest[0]), int(rest[1]), query)
        elif action == "u":
            await self._toggle_mute(update, int(rest[0]), query)
        elif action == "l":
            await self._leave_confirm(update, int(rest[0]), query=query)
        elif action == "ly":
            await self._leave(update, int(rest[0]), query)
        elif action == "i":
            await self._invite(update, club_id=int(rest[0]), query=query)
        elif action == "s":
            await self._settings(update, int(rest[0]), query)
        elif action == "sv":
            api = self._api(update)
            vis = vis_map.get(rest[1] if len(rest) > 1 else "2", "full")
            api.patch_club(int(rest[0]), cellar_visibility=vis)
            await self._settings(update, int(rest[0]), query)
        elif action == "sb":
            api = self._api(update)
            api.patch_club(int(rest[0]), badges_visible=rest[1] == "1")
            await self._settings(update, int(rest[0]), query)
        elif action == "w":
            club_id = int(rest[0])
            if len(rest) > 1 and rest[1] == "leave":
                await self._leave(update, club_id, query)
            else:
                api = self._api(update)
                home = api.get_club(club_id)
                api.patch_club_membership(
                    club_id, accepted_sharing_version=home.get("sharing_version"),
                )
                await self._show_home(update, club_id, query=query)
        elif action == "fd":
            page = int(rest[1]) if len(rest) > 1 else 0
            await self._show_feed(update, int(rest[0]), page=page, query=query)
        elif action == "z":
            await self._dismantle_confirm(update, int(rest[0]), query)
        elif action == "za":
            await self._dismantle_ask(update, int(rest[0]), query)
        elif action == "dy":
            await self._ballot(update, int(rest[0]), "yes", query)
        elif action == "dn":
            await self._ballot(update, int(rest[0]), "no", query)
        elif action == "zc":
            await self._dismantle_cancel(update, int(rest[0]), query)
        elif action == "j":
            hh = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
            await self._do_join(update, query, rest[0] if rest else "", household_id=hh)
        elif action == "nn":
            chat_id = str(query.message.chat_id)
            state = self.state_for(chat_id) or {}
            when = format_until(state.get("expires_at")) or ""
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.not_now.ok", date=when))
            self.clear(chat_id)
        elif action == "ph":
            chat_id = str(query.message.chat_id)
            code, hh_id = rest[0], int(rest[1])
            api = self._api(update)
            households = api.list_households() or []
            hh = next((h for h in households if h.get("id") == hh_id), None)
            self.remember(
                chat_id, step="join", code=code,
                household_id=hh_id,
                household_name=(hh or {}).get("name") or "cellar",
            )
            await self._join_card(update, query=query)
        elif action == "hy":
            api = self._api(update)
            api.patch_club_membership(int(rest[0]), household_id=int(rest[1]))
            await self._show_home(update, int(rest[0]), query=query)
        elif action == "hk":
            await self._show_home(update, int(rest[0]), query=query)
        elif action == "ho":
            api = self._api(update)
            api.handover_club(int(rest[0]), int(rest[1]))
            await self._leave(update, int(rest[0]), query)
        elif action == "sn":
            chat_id = str(query.message.chat_id)
            state = self.state_for(chat_id) or {}
            name = state.get("suggested_name") or ""
            if not name:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, t(loc, "club.name.retry"))
            else:
                self.remember(chat_id, step="join", household_name=name)
                await self._join_card(update, query=query)
        elif action in ("cm", "rp"):
            await self._start_comment(update, int(rest[0]) if rest else 0, query)
        elif action in ("pt", "pn") and self.threads_on():
            await self._confirm_held(update, context, query, post=action == "pt")
        elif action == "mt" and self.threads_on():
            await self._toggle_thread_mute(update, int(rest[0]) if rest else 0, query)
        elif action == "th" and self.threads_on() and len(rest) >= 2:
            await self._show_thread(
                update, int(rest[0]), int(rest[1]),
                int(rest[2]) if len(rest) > 2 else 0, query,
            )
        elif action == "td":
            await self._event_to_list(update, int(rest[0]) if rest else 0, query)
        elif action == "li":
            await self._show_club_list(update, club_id=int(rest[0]), query=query)
        elif action == "la":
            await self._start_club_list_add(update, int(rest[0]), query)
        elif action == "ld":
            await self._delete_club_list_item(
                update, int(rest[0]), int(rest[1]), query,
            )
        elif action == "to":
            club_id = int(rest[0]) if rest else None
            await self._start_tasteoff(update, club_id=club_id, query=query)
        elif action == "tb":
            await self._prompt_tasteoff_label(update, int(rest[0]), query)
        elif action == "tl":
            await self._tasteoff_from_list(update, int(rest[0]), query)
        elif action == "ta":
            await self._tasteoff_add_from_list(
                update, int(rest[0]), int(rest[1]) if len(rest) > 1 else 0, query,
            )
        elif action == "tf":
            await self._tasteoff_from_cellar(update, int(rest[0]), query)
        elif action == "tm":
            await self._tasteoff_member_wines(
                update, int(rest[0]), int(rest[1]) if len(rest) > 1 else 0, query,
            )
        elif action == "tw":
            await self._tasteoff_add_wine(
                update, int(rest[0]), int(rest[1]) if len(rest) > 1 else 0, query,
            )
        elif action == "th":
            await self._show_history(update, int(rest[0]), query)
        elif action == "tv":
            await self._show_past(update, int(rest[0]), int(rest[1]), query)
        elif action == "tq":
            await self._start_tasteoff(
                update, club_id=int(rest[0]) if rest else None, query=query, force_new=True,
            )
        elif action == "tz":
            await self._close_tasteoff(update, int(rest[0]), query)
        elif action == "tr":
            await self._reopen_tasteoff(update, int(rest[0]), query)
        elif action == "op":
            await self._open_tasteoff(update, int(rest[0]), query)
        elif action == "ts":
            await self._score_tasteoff(
                update,
                int(rest[0]),
                int(rest[1]) if len(rest) > 1 else 0,
                int(rest[2]) if len(rest) > 2 else 0,
                query,
            )
        elif action == "tc":
            await self._close_confirm(update, int(rest[0]), query)
        elif action == "tx":
            await self._cancel_tasteoff_setup(update, int(rest[0]), query)
        elif action == "dc":
            await self._show_discover(update, query=query)
        elif action == "dj":
            await self._discover_consent(update, int(rest[0]) if rest else 0, query)
        elif action == "jj":
            await self._do_global_join(update, int(rest[0]) if rest else 0, query)
        elif action == "gp":
            await self._start_club_post(update, int(rest[0]) if rest else 0, query=query)
        else:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.err"))
            return True
        return True

    async def cmd_clubpost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loc = self.locale(update)
        if not _is_staff(self.handler, update):
            await update.message.reply_text(t(loc, "club.global.staff_only"))
            return
        if not global_clubs_enabled(self.handler):
            await update.message.reply_text(t(loc, "club.global.off"))
            return
        api = self._api(update)
        try:
            data = api.discover_clubs()
        except Exception as e:
            logger.warning("[club] discover for post failed: %s", e)
            await update.message.reply_text(t(loc, "club.err"))
            return
        if data.get("status_code") == 404:
            await update.message.reply_text(t(loc, "club.global.off"))
            return
        clubs = data.get("clubs") or []
        needle = " ".join(list(context.args or [])).strip().lower()
        if needle:
            match = next(
                (c for c in clubs if (c.get("name") or "").strip().lower() == needle),
                None,
            )
            if match:
                await self._start_club_post(update, match["id"])
                return
        if not clubs:
            await update.message.reply_text(t(loc, "club.discover.empty"))
            return
        rows = [[InlineKeyboardButton(
            f"\U0001F30D {c.get('name') or 'Club'}",
            callback_data=club_cb("gp", c["id"]),
        )] for c in clubs]
        await update.message.reply_text(
            t(loc, "club.discover.title"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _start_club_post(self, update, club_id, query=None):
        loc = self.locale(update)
        if not _is_staff(self.handler, update) or not global_clubs_enabled(self.handler):
            text = t(
                loc,
                "club.global.staff_only" if not _is_staff(self.handler, update)
                else "club.global.off",
            )
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        api = self._api(update)
        chat_id = str(update.effective_chat.id)
        name = "club"
        try:
            clubs = (api.discover_clubs() or {}).get("clubs") or []
            match = next((c for c in clubs if c.get("id") == club_id), None)
            if match:
                name = match.get("name") or name
            else:
                home = api.get_club(club_id) or {}
                name = home.get("name") or name
        except Exception:
            pass
        self.remember(chat_id, step="club_post", club_id=club_id, club_name=name)
        text = t(loc, "club.global.post.prompt", name=esc(name))
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    async def _submit_club_post(self, update, text: str) -> bool:
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        club_id = state.get("club_id")
        name = state.get("club_name") or "club"
        body = (text or "").strip()
        if not body:
            await update.message.reply_text(t(loc, "club.global.post.prompt", name=esc(name)))
            return True
        api = self._api(update)
        try:
            result = api.post_club_note(club_id, body)
        except Exception:
            logger.exception("[club] staff note failed")
            await update.message.reply_text(t(loc, "club.err"))
            return True
        self.clear(chat_id)
        if result.get("error") == "frozen" or result.get("status_code") == 409:
            await update.message.reply_text(t(loc, "club.global.frozen"))
            return True
        if result.get("error") or (
            isinstance(result.get("status_code"), int) and result["status_code"] >= 400
        ):
            await update.message.reply_text(t(loc, "club.err"))
            return True
        await update.message.reply_text(t(loc, "club.global.posted", name=esc(name)))
        return True

    async def _show_discover(self, update, query=None):
        loc = self.locale(update)
        if not global_clubs_enabled(self.handler):
            text = t(loc, "club.global.off")
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        api = self._api(update)
        try:
            data = api.discover_clubs()
        except Exception as e:
            logger.warning("[club] discover failed: %s", e)
            data = {"error": str(e)}
        if data.get("status_code") == 404:
            text = t(loc, "club.global.off")
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text)
            else:
                await update.message.reply_text(text)
            return
        clubs = data.get("clubs") or []
        lines = [f"\U0001F30D <b>{esc(t(loc, 'club.discover.title'))}</b>", ""]
        keyboard = []
        if not clubs:
            lines.append(t(loc, "club.discover.empty"))
        for club in clubs:
            name = club.get("name") or "Club"
            desc = (club.get("description") or "").strip()
            lines.append(f"\U0001F30D <b>{esc(name)}</b>")
            if desc:
                lines.append(esc(desc))
            if club.get("frozen"):
                lines.append(t(loc, "club.global.frozen"))
            lines.append("")
            if club.get("joined"):
                keyboard.append([InlineKeyboardButton(
                    name, callback_data=club_cb("x", club["id"]),
                )])
            elif not club.get("frozen"):
                keyboard.append([InlineKeyboardButton(
                    t(loc, "club.join.btn", name=name),
                    callback_data=club_cb("dj", club["id"]),
                )])
        text = "\n".join(lines).rstrip()
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _discover_consent(self, update, club_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not global_clubs_enabled(self.handler):
            await _safe_edit(query, t(loc, "club.global.off"))
            return
        api = self._api(update)
        try:
            data = api.discover_clubs()
        except Exception:
            data = {}
        club = next(
            (c for c in (data.get("clubs") or []) if c.get("id") == club_id),
            None,
        )
        if club is None:
            await _safe_edit(query, t(loc, "club.err"))
            return
        if club.get("joined"):
            await self._show_home(update, club_id, query=query)
            return
        if club.get("frozen"):
            await _safe_edit(query, t(loc, "club.global.frozen"))
            return
        name = club.get("name") or "Club"
        text = t(loc, "club.global.consent", name=esc(name))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                t(loc, "club.global.join"), callback_data=club_cb("jj", club_id),
            ),
            InlineKeyboardButton(
                t(loc, "club.global.not_now"), callback_data=club_cb("dc"),
            ),
        ]])
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)

    async def _do_global_join(self, update, club_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not global_clubs_enabled(self.handler):
            await _safe_edit(query, t(loc, "club.global.off"))
            return
        api = self._api(update)
        try:
            result = api.join_club(club_id)
        except Exception:
            logger.exception("[club] global join failed")
            await _safe_edit(query, t(loc, "club.err"))
            return
        status = result.get("status_code")
        if result.get("error") == "frozen" or status == 409:
            await _safe_edit(query, t(loc, "club.global.frozen"))
            return
        if result.get("error") == "invite_only" or status == 403:
            await _safe_edit(query, t(loc, "club.forbidden"))
            return
        if result.get("error") or (isinstance(status, int) and status >= 400):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await self._show_home(update, club_id, query=query)

    async def _dismantle_confirm(self, update, club_id, query):
        loc = self.locale(update)
        api = self._api(update)
        home = api.get_club(club_id)
        text = t(loc, "club.end.confirm", name=esc(home.get("name") or "club"))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(loc, "club.end.ask"), callback_data=club_cb("za", club_id)),
            InlineKeyboardButton(t(loc, "club.end.cancel"), callback_data=club_cb("s", club_id)),
        ]])
        from integrations.telegram_bot import _safe_edit
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)

    async def _dismantle_ask(self, update, club_id, query):
        loc = self.locale(update)
        api = self._api(update)
        result = api.start_dismantle(club_id)
        from integrations.telegram_bot import _safe_edit
        if result.get("error") == "sole_member":
            await self._leave(update, club_id, query)
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        waiting = ", ".join(result.get("waiting_names") or []) or "—"
        text = t(
            loc, "club.end.status",
            club=esc(api.get_club(club_id).get("name") or "club"),
            n=result.get("pending") or 0,
            yes_n=result.get("yes") or 0,
            no_n=result.get("no") or 0,
            waiting=esc(waiting),
            date=format_until(result.get("expires_at")) or "",
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                t(loc, "club.end.cancel"),
                callback_data=club_cb("zc", result["id"]),
            ),
        ]])
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)

    async def _ballot(self, update, vote_id, choice, query):
        loc = self.locale(update)
        api = self._api(update)
        clubs = (api.list_clubs() or {}).get("clubs") or []
        club_id = None
        for club in clubs:
            vote = api.get_dismantle(club["id"])
            if vote.get("id") == vote_id:
                club_id = club["id"]
                break
        from integrations.telegram_bot import _safe_edit
        if club_id is None:
            await _safe_edit(query, t(loc, "club.end.over"))
            return
        club_name = next((c.get("name") for c in clubs if c["id"] == club_id), "club")
        voter = ""
        user = getattr(update, "effective_user", None)
        if user is not None:
            voter = user.first_name or user.full_name or ""
        result = api.cast_dismantle_ballot(club_id, vote_id, choice)
        # A repeat tap the server already counted: show where the vote stands.
        already = result.get("error") == "already_voted"
        if not already and (
            result.get("error") in ("vote_closed",) or result.get("status_code") == 409
        ):
            await _safe_edit(query, t(loc, "club.end.over"))
            return
        prefix = f"\u2705 {t(loc, 'nav.already_done')}\n\n" if already else ""
        if result.get("status") == "passed":
            await _safe_edit(query, t(loc, "club.end.passed", club=esc(club_name)))
            return
        if result.get("status") == "rejected" or result.get("no"):
            await _safe_edit(query, t(
                loc, "club.end.rejected",
                name=esc(voter or "Someone"),
                club=esc(club_name),
            ))
            return
        await _safe_edit(query, prefix + t(loc, "club.end.status",
            club=esc(next((c.get("name") for c in clubs if c["id"] == club_id), "club")),
            n=result.get("pending") or 0,
            yes_n=result.get("yes") or 0,
            no_n=result.get("no") or 0,
            waiting=esc(", ".join(result.get("waiting_names") or [])),
            date="",
        ))

    async def _dismantle_cancel(self, update, vote_id, query):
        loc = self.locale(update)
        api = self._api(update)
        clubs = (api.list_clubs() or {}).get("clubs") or []
        from integrations.telegram_bot import _safe_edit
        for club in clubs:
            vote = api.get_dismantle(club["id"])
            if vote.get("id") == vote_id:
                api.cancel_dismantle(club["id"], vote_id)
                await _safe_edit(query, t(loc, "club.end.cancelled", club=esc(club.get("name") or "club")))
                return
        await _safe_edit(query, t(loc, "club.end.over"))

    def _flags(self):
        return {
            "feature_lists": self.handler.flag_on("feature_lists"),
            "feature_tasteoffs": self.handler.flag_on("feature_tasteoffs"),
            "feature_club_comments": self.handler.flag_on("feature_club_comments"),
            "feature_global_clubs": self.handler.flag_on("feature_global_clubs"),
        }

    def _default_tasteoff_title(self, club_name: str) -> str:
        from datetime import datetime, timezone as dt_timezone

        now = datetime.now(dt_timezone.utc)
        day = f"{now.day} {_MONTHS[now.month - 1]}"
        name = (club_name or "Tasting").strip() or "Tasting"
        return f"{name} · {day}"[:80]

    async def _club_id_for_event(self, update, event_id: int):
        api = self._api(update)
        try:
            clubs = (api.list_clubs() or {}).get("clubs") or []
        except Exception:
            return None
        last = next((c for c in clubs if c.get("is_last_used")), None)
        ordered = ([last] if last else []) + [
            c for c in clubs if last is None or c["id"] != last["id"]
        ]
        for club in ordered:
            try:
                payload = api.club_feed(club["id"], limit=1, event_id=event_id)
            except Exception:
                continue
            events = payload.get("events") or []
            match = next((e for e in events if e.get("id") == event_id), None)
            if match is not None:
                if len(self._events_seen) > 200:
                    self._events_seen.clear()
                self._events_seen[event_id] = match
                return club["id"]
        return last["id"] if last else None

    async def _club_id_for_tasteoff(self, update, tid: int):
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        if state.get("tasteoff_id") == tid and state.get("club_id"):
            return state["club_id"]
        api = self._api(update)
        try:
            clubs = (api.list_clubs() or {}).get("clubs") or []
        except Exception:
            return None
        last = next((c for c in clubs if c.get("is_last_used")), None)
        ordered = ([last] if last else []) + [
            c for c in clubs if last is None or c["id"] != last["id"]
        ]
        for club in ordered:
            try:
                body = api.get_tasteoff(club["id"], tid)
            except Exception:
                continue
            if body.get("id") == tid:
                return club["id"]
        return last["id"] if last else None

    async def _start_comment(self, update, event_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not self.handler.flag_on("feature_club_comments"):
            await _safe_edit(query, t(loc, "club.comment.off"))
            return
        club_id = await self._club_id_for_event(update, event_id)
        if not club_id:
            await _safe_edit(query, t(loc, "club.err"))
            return
        chat_id = str(update.effective_chat.id)
        nav = getattr(self.handler, "_nav", None)
        if callable(nav):
            nav().pending.cancel(chat_id)  # one "next message" slot per chat
        if self.threads_on():
            self.clear(chat_id)
            prompt = t(loc, "club.thread.prompt")
            event = self._events_seen.pop(event_id, None)
            wine = ""
            if event and (event.get("kind") or "opened") == "opened":
                prompt = f"\U0001F4AC {_wine_line(event, loc)}\n\n{prompt}"
                wine = f"{event.get('vintage') or ''} {event.get('wine_name') or ''}".strip()
            sent = await query.message.reply_text(
                prompt, parse_mode="HTML", reply_markup=_cancel_markup(),
            )
            self._prompt_put(
                chat_id, event_id=event_id, club_id=club_id,
                prompt_id=getattr(sent, "message_id", None), wine=wine,
            )
            return
        # Say what the note goes under: a home-card shortcut is not obviously
        # tied to one bottle.
        prompt = t(loc, "club.comment.prompt")
        event = self._events_seen.pop(event_id, None)
        if event and (event.get("kind") or "opened") == "opened":
            prompt = f"\U0001F4AC {_wine_line(event, loc)}\n\n{prompt}"
        sent = await query.message.reply_text(
            prompt, parse_mode="HTML", reply_markup=_cancel_markup(),
        )
        self.remember(
            chat_id,
            step="comment",
            event_id=event_id,
            club_id=club_id,
            comment_at=time.monotonic(),
            prompt_id=getattr(sent, "message_id", None),
        )

    async def _submit_comment(self, update, text: str) -> bool:
        """Only the next message is the comment. It is taken (and the prompt
        closed) whether or not it can be posted; a retry is one tap away."""
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        self.clear(chat_id)
        await self._close_comment_prompt(update, chat_id, state.get("prompt_id"))
        event_id = state.get("event_id")
        club_id = state.get("club_id")
        started = state.get("comment_at") or 0
        if time.monotonic() - started > COMMENT_TTL_SECONDS:
            # Expired: this message was never meant as the comment.
            return False
        note = (text or "").strip()
        if not note or len(note) > COMMENT_MAX:
            await update.message.reply_text(
                t(loc, "club.comment.too_long"),
                reply_markup=self._comment_again_markup(event_id),
            )
            return True
        api = self._api(update)
        result = api.add_club_comment(club_id, event_id, note)
        if result.get("error") == "retracted" or result.get("status_code") == 409:
            await update.message.reply_text(t(loc, "club.err"), reply_markup=_menu_markup())
            return True
        if result.get("error"):
            await update.message.reply_text(
                t(loc, "club.err"), reply_markup=self._comment_again_markup(event_id),
            )
            return True
        from integrations.telegram_nav import follow_up_rows
        await update.message.reply_text(
            t(loc, "club.comment.saved"),
            reply_markup=InlineKeyboardMarkup(follow_up_rows("club", {
                "flags": self._flags(), "club_id": club_id,
            })),
        )
        return True

    async def _post_thread_comment(
        self, update, event_id, club_id, text, message=None,
    ) -> bool:
        """Post a comment from the typed prompt or a swipe-reply. Never raises.
        ``message`` is where replies go (a callback has no update.message)."""
        loc = self.locale(update)
        message = message or update.message
        note = (text or "").strip()
        if not note or len(note) > COMMENT_MAX:
            await message.reply_text(
                t(loc, "club.comment.too_long"),
                reply_markup=self._comment_again_markup(event_id),
            )
            return True
        try:
            result = self._api(update).add_club_comment(club_id, event_id, note)
        except Exception as e:
            logger.info("[club] comment post failed: %s", e)
            result = {"error": "err"}
        if result.get("error") == "retracted" or result.get("status_code") == 409:
            await message.reply_text(t(loc, "club.err"), reply_markup=_menu_markup())
            return True
        if result.get("error"):
            await message.reply_text(
                t(loc, "club.err"), reply_markup=self._comment_again_markup(event_id),
            )
            return True
        from integrations.telegram_nav import follow_up_rows
        await message.reply_text(
            t(loc, "club.comment.saved"),
            reply_markup=InlineKeyboardMarkup(follow_up_rows("club", {
                "flags": self._flags(), "club_id": club_id,
            })),
        )
        return True

    async def _take_or_ask(self, update, context, chat_id, prompt) -> bool:
        """The Reply / Say something prompt lives 15 minutes, but only a message
        that is plainly the comment is posted without asking."""
        message = update.message
        if prompt.get("held") is not None:
            # They ignored the question and typed something else: it is theirs.
            self._prompt_drop(chat_id)
            await self._close_comment_prompt(update, chat_id, prompt.get("prompt_id"))
            return False
        fresh = time.time() - float(prompt.get("at") or 0) <= THREAD_DIRECT_SECONDS
        if fresh or await self._is_reply_to_thread(update, chat_id, prompt):
            self._prompt_drop(chat_id)
            await self._close_comment_prompt(update, chat_id, prompt.get("prompt_id"))
            return await self._post_thread_comment(
                update, prompt.get("event_id"), prompt.get("club_id"), message.text,
            )
        loc = self.locale(update)
        wine = prompt.get("wine") or t(loc, "club.thread.confirm_wine")
        self._prompt_hold(chat_id, prompt, message.text or "")
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                t("en", "club.thread.btn_post"), callback_data=club_cb("pt"),
            ),
            InlineKeyboardButton(
                t("en", "club.thread.btn_not_mine"), callback_data=club_cb("pn"),
            ),
        ]])
        await message.reply_text(
            t(loc, "club.thread.confirm", wine=esc(wine)),
            parse_mode="HTML", reply_markup=markup,
        )
        return True

    async def _is_reply_to_thread(self, update, chat_id, prompt) -> bool:
        reply = getattr(update.message, "reply_to_message", None)
        if reply is None:
            return False
        if prompt.get("prompt_id") and reply.message_id == prompt.get("prompt_id"):
            return True
        try:
            found = self._api(update).club_thread_resolve(chat_id, reply.message_id)
        except Exception:
            return False
        return bool(
            not found.get("error") and found.get("event_id") == prompt.get("event_id")
        )

    async def _confirm_held(self, update, context, query, post: bool) -> None:
        chat_id = str(update.effective_chat.id)
        loc = self.locale(update)
        prompt = self._prompt_get(chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        held = (prompt or {}).get("held")
        if not prompt or held is None:
            await query.message.reply_text(t(loc, "club.thread.gone"))
            return
        self._prompt_drop(chat_id)
        await self._close_comment_prompt(update, chat_id, prompt.get("prompt_id"))
        if post:
            await self._post_thread_comment(
                update, prompt.get("event_id"), prompt.get("club_id"), held,
                message=query.message,
            )
            return
        # Not a comment: answer it like any other message.
        shim = _HeldTextUpdate(update, query.message, held)
        await self.handler._unknown_text(shim, context)

    async def on_reply(self, update, context) -> bool:
        """A swipe-reply to a club message is a comment on that event.
        True when it was one; False leaves the message to the normal paths."""
        if not (clubs_enabled(self.handler) and self.threads_on()):
            return False
        message = getattr(update, "message", None)
        reply = getattr(message, "reply_to_message", None)
        if message is None or reply is None:
            return False
        sender = getattr(reply, "from_user", None)
        if sender is not None and not getattr(sender, "is_bot", True):
            return False
        chat_id = str(update.effective_chat.id)
        loc = self.locale(update)
        try:
            found = self._api(update).club_thread_resolve(chat_id, reply.message_id)
        except Exception as e:
            logger.info("[club] thread resolve failed: %s", e)
            found = {"error": "err"}
        if found.get("error") and found.get("status_code") != 404:
            # Network/API trouble is not "not a club message": say so and stop.
            await message.reply_text(
                t(loc, "club.thread.post_failed"), reply_markup=_menu_markup(),
            )
            return True
        if found.get("error") or not found.get("event_id"):
            return False
        self._prompt_drop(chat_id)
        if found.get("retracted"):
            await message.reply_text(
                t(loc, "club.thread.retracted"), reply_markup=_menu_markup(),
            )
            return True
        return await self._post_thread_comment(
            update, found["event_id"], found["club_id"], message.text,
        )

    async def _toggle_thread_mute(self, update, event_id, query):
        loc = self.locale(update)
        try:
            result = self._api(update).club_thread_mute(event_id)
        except Exception as e:
            logger.info("[club] thread mute failed: %s", e)
            result = {"error": "err"}
        if result.get("error"):
            await query.message.reply_text(t(loc, "club.thread.gone"))
            return
        muted = bool(result.get("muted"))
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                t(loc, "club.thread.btn_reply"), callback_data=club_cb("rp", event_id),
            ),
            InlineKeyboardButton(
                t(loc, "club.thread.btn_unmute" if muted else "club.thread.btn_mute"),
                callback_data=club_cb("mt", event_id),
            ),
        ]])
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except Exception:
            pass
        await query.message.reply_text(
            t(loc, "club.thread.muted" if muted else "club.thread.unmuted"),
        )

    @staticmethod
    def _thread_pages(head: str, comments: list) -> list[str]:
        """Split head + every comment into messages under THREAD_PAGE_CHARS."""
        pages, cur = [], head
        for c in comments:
            who = esc(c.get("display_name") or "")
            line = f"{who}: {esc(c.get('text') or '')}"
            if len(cur) + len(line) + 1 > THREAD_PAGE_CHARS and cur != head:
                pages.append(cur)
                cur = head
            cur = f"{cur}\n{line}"
        pages.append(cur)
        return pages

    async def _show_thread(self, update, club_id, event_id, page, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        api = self._api(update)
        try:
            feed = api.club_feed(club_id, limit=1, event_id=event_id)
            event = next(
                (e for e in feed.get("events") or [] if e.get("id") == event_id), None,
            )
            body = api.club_comments(club_id, event_id)
        except Exception:
            event, body = None, {"error": "err"}
        if event is None or body.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        comments = body.get("comments") or []
        head = f"\U0001F4AC {_wine_line(event, loc)}\n"
        if not comments:
            head = f"{head}\n{t(loc, 'club.thread.empty')}"
        pages = self._thread_pages(head, comments)
        page = max(0, min(page, len(pages) - 1))
        text = pages[page]
        if len(pages) > 1:
            text += "\n\n" + t(loc, "club.thread.page", page=page + 1, pages=len(pages))
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                t(loc, "club.feed.newer"), callback_data=club_cb("th", club_id, event_id, page - 1),
            ))
        if page + 1 < len(pages):
            nav.append(InlineKeyboardButton(
                t(loc, "club.feed.older"), callback_data=club_cb("th", club_id, event_id, page + 1),
            ))
        rows = [nav] if nav else []
        rows.append([
            InlineKeyboardButton(
                t(loc, "club.thread.btn_reply"), callback_data=club_cb("rp", event_id),
            ),
            InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("fd", club_id, 0)),
        ])
        await _safe_edit(
            query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows),
        )

    def _comment_again_markup(self, event_id):
        from integrations.telegram_nav import MENU, menu_cb
        row = []
        if event_id:
            row.append(InlineKeyboardButton(
                "\U0001F4AC Try again", callback_data=club_cb("cm", event_id),
            ))
        row.append(InlineKeyboardButton(MENU, callback_data=menu_cb("new")))
        return InlineKeyboardMarkup([row])

    async def _close_comment_prompt(self, update, chat_id, prompt_id) -> None:
        if not prompt_id:
            return
        bot = None
        for holder in (
            getattr(update, "message", None),
            getattr(getattr(update, "callback_query", None), "message", None),
        ):
            getter = getattr(holder, "get_bot", None)
            try:
                bot = getter() if callable(getter) else None
            except Exception:
                bot = None
            if bot is not None:
                break
        if bot is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(chat_id), message_id=prompt_id, reply_markup=None,
            )
        except Exception:
            pass

    # -- reactions and Want this (feature_club_reactions) -----------------
    def reactions_on(self) -> bool:
        return bool(
            clubs_enabled(self.handler)
            and self.handler.flag_on("feature_club_reactions")
        )

    @staticmethod
    async def _toast(query, text: str) -> None:
        try:
            await query.answer(text[:190])
        except Exception as e:
            logger.info("[club] toast failed: %s", e)

    @staticmethod
    def _with_counts(markup, counts, event_id):
        """The tapped message's keyboard, with only the emoji row refreshed."""
        rows = []
        replaced = False
        for row in getattr(markup, "inline_keyboard", None) or []:
            first = getattr(row[0], "callback_data", "") if row else ""
            if str(first).startswith("cl:rx:"):
                rows.append(reaction_buttons(event_id, counts))
                replaced = True
            else:
                rows.append(list(row))
        return InlineKeyboardMarkup(rows) if replaced else None

    async def _reaction_callback(self, update, query, action, rest) -> bool:
        loc = self.locale(update)
        if not self.reactions_on():
            if action == "wt" and clubs_enabled(self.handler):
                pass  # flag off: fall through to the plain To-drink path
            else:
                await self._toast(query, t(loc, "club.rx.off"))
                return True
        try:
            event_id = int(rest[0]) if rest else 0
        except (TypeError, ValueError):
            event_id = 0
        if action == "wt":
            if not self.reactions_on():
                try:
                    await query.answer()
                except Exception:
                    pass
                await self._event_to_list(update, event_id, query)
                return True
            await self._want_this(update, query, event_id)
            return True
        try:
            index = int(rest[1]) if len(rest) > 1 else -1
        except (TypeError, ValueError):
            index = -1
        if not event_id or not 0 <= index < len(PALETTE):
            await self._toast(query, t(loc, "club.err"))
            return True
        try:
            result = self._api(update).club_event_react(event_id, index)
        except Exception as e:
            logger.info("[club] react failed: %s", e)
            result = {"error": "err"}
        if result.get("status_code") == 409 or result.get("error") == "retracted":
            await self._toast(query, t(loc, "club.rx.retracted"))
            return True
        if result.get("error"):
            await self._toast(query, t(loc, "club.err"))
            return True
        emoji = PALETTE[index]
        await self._toast(
            query, t(loc, "club.rx.on" if result.get("on") else "club.rx.undone", emoji=emoji),
        )
        markup = self._with_counts(
            getattr(query.message, "reply_markup", None),
            result.get("counts") or [0] * len(PALETTE), event_id,
        )
        if markup is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception as e:
                logger.info("[club] reaction markup edit: %s", e)
        return True

    async def _want_this(self, update, query, event_id) -> None:
        loc = self.locale(update)
        if not self.handler.flag_on("feature_lists"):
            await self._toast(query, t(loc, "club.td.off"))
            return
        try:
            result = self._api(update).club_event_want(event_id)
        except Exception as e:
            logger.info("[club] want failed: %s", e)
            result = {"error": "err"}
        if result.get("status_code") == 409 or result.get("error") == "retracted":
            await self._toast(query, t(loc, "club.rx.retracted"))
        elif result.get("status_code") == 404:
            await self._toast(query, t(loc, "club.td.off"))
        elif result.get("error"):
            await self._toast(query, t(loc, "club.err"))
        else:
            await self._toast(query, t(loc, "club.td.added"))

    async def _event_to_list(self, update, event_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not self.handler.flag_on("feature_lists"):
            await _safe_edit(query, t(loc, "club.td.off"))
            return
        club_id = await self._club_id_for_event(update, event_id)
        if not club_id:
            await _safe_edit(query, t(loc, "club.err"))
            return
        api = self._api(update)
        result = api.event_to_list(club_id, event_id)
        if result.get("status_code") == 404:
            await _safe_edit(query, t(loc, "club.td.off"))
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await query.message.reply_text(t(loc, "club.td.added"))

    async def _show_club_list(self, update, club_id=None, query=None):
        loc = self.locale(update)
        if not self.handler.flag_on("feature_lists"):
            await self._reply(update, t(loc, "club.td.off"), query=query)
            return
        club_id = club_id or await self._current_club_id(update)
        if not club_id:
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        api = self._api(update)
        try:
            payload = api.club_list(club_id)
        except Exception:
            payload = {"error": "err"}
        if payload.get("error"):
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        items = payload.get("items") or []
        lines = [f"<b>{esc(t(loc, 'club.list.title'))}</b>", ""]
        keyboard = []
        if not items:
            lines.append(t(loc, "club.list.empty"))
        for item in items:
            vintage = item.get("vintage") or ""
            producer = item.get("producer") or ""
            who = item.get("added_by_name") or ""
            bit = f"{esc(item.get('name') or '')} {vintage}".strip()
            if producer:
                bit += f" · {esc(producer)}"
            if who:
                bit += f" · {esc(who)}"
            lines.append(bit)
            keyboard.append([InlineKeyboardButton(
                f"{t(loc, 'club.list.remove_btn')} {(item.get('name') or '')}"[:40],
                callback_data=club_cb("ld", club_id, item["id"]),
            )])
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.list.add_btn"), callback_data=club_cb("la", club_id),
        )])
        keyboard.append([InlineKeyboardButton(
            t(loc, "club.back"), callback_data=club_cb("h", club_id),
        )])
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _start_club_list_add(self, update, club_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not self.handler.flag_on("feature_lists"):
            await _safe_edit(query, t(loc, "club.td.off"))
            return
        chat_id = str(update.effective_chat.id)
        self.remember(chat_id, step="club_list_add", club_id=club_id)
        await _safe_edit(query, t(loc, "club.list.add_prompt"))

    async def _club_list_add_typed(self, update, text: str) -> bool:
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        if not self.handler.flag_on("feature_lists"):
            self.clear(chat_id)
            await update.message.reply_text(t(loc, "club.td.off"))
            return True
        state = self.state_for(chat_id) or {}
        name = (text or "").strip()
        if len(name) < 2:
            await update.message.reply_text(t(loc, "club.list.add_prompt"))
            return True
        vintage = None
        bits = name.rsplit(" ", 1)
        if len(bits) == 2 and bits[1].isdigit() and len(bits[1]) == 4:
            name, vintage = bits[0], int(bits[1])
        api = self._api(update)
        result = api.add_club_list_item(state.get("club_id"), name, vintage=vintage)
        if result.get("error"):
            await update.message.reply_text(t(loc, "club.err"))
            return True
        self.clear(chat_id)
        await update.message.reply_text(t(loc, "club.list.added"))
        await self._show_club_list(update, club_id=state.get("club_id"))
        return True

    async def _delete_club_list_item(self, update, club_id, item_id, query):
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        if not self.handler.flag_on("feature_lists"):
            await _safe_edit(query, t(loc, "club.td.off"))
            return
        api = self._api(update)
        result = api.delete_club_list_item(club_id, item_id)
        if result.get("status_code") == 403:
            await _safe_edit(query, t(loc, "club.forbidden"))
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await self._show_club_list(update, club_id=club_id, query=query)

    async def _start_tasteoff(self, update, club_id=None, query=None, force_new=False):
        loc = self.locale(update)
        if not self.handler.flag_on("feature_tasteoffs"):
            await self._reply(update, t(loc, "club.to.off"), query=query)
            return
        club_id = club_id or await self._current_club_id(update)
        if not club_id:
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        api = self._api(update)
        home = api.get_club(club_id) if club_id else {}
        existing = None
        rows = []
        try:
            rows = (api.list_tasteoffs(club_id) or {}).get("tasteoffs") or []
            existing = next(
                (r for r in rows if r.get("status") in ("setup", "scoring")),
                None,
            )
        except Exception:
            existing = None
        if existing and existing.get("status") == "scoring":
            await self._show_scoring(update, club_id, existing["id"], query=query)
            return
        if existing is None and not force_new:
            reopenable = next(
                (r for r in rows if r.get("status") == "closed" and r.get("can_reopen")),
                None,
            ) if rows else None
            if reopenable is not None:
                text = t(loc, "club.to.reopen_or_new", title=esc(reopenable.get("title") or ""))
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(loc, "club.to.reopen"), callback_data=club_cb("tr", reopenable["id"]))],
                    [InlineKeyboardButton(t(loc, "club.to.start_new"), callback_data=club_cb("tq", club_id))],
                ])
                if query is not None:
                    from integrations.telegram_bot import _safe_edit
                    await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
                else:
                    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
                return
        if existing and existing.get("status") == "setup":
            chat_id = str(update.effective_chat.id)
            self.remember(
                chat_id, step="tasteoff_setup", club_id=club_id,
                tasteoff_id=existing["id"], club_name=home.get("name"),
            )
            await self._show_tasteoff_setup(update, club_id, existing["id"], query=query)
            return
        chat_id = str(update.effective_chat.id)
        default = self._default_tasteoff_title(home.get("name") or "Tasting")
        self.remember(
            chat_id, step="tasteoff_title", club_id=club_id,
            club_name=home.get("name"), default_title=default,
        )
        prompt = t(loc, "club.to.title") + f"\n\n{default}"
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, prompt)
        else:
            await update.message.reply_text(prompt)

    async def _tasteoff_named(self, update, text: str) -> bool:
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        title = (text or "").strip()
        if not title or title in {".", "-"}:
            title = state.get("default_title") or self._default_tasteoff_title(
                state.get("club_name") or "Tasting",
            )
        if len(title) < 2:
            await update.message.reply_text(t(loc, "club.to.title"))
            return True
        api = self._api(update)
        result = api.create_tasteoff(state.get("club_id"), title)
        if result.get("error") == "already_open":
            tid = result.get("id")
            if tid:
                self.remember(
                    chat_id, step="tasteoff_setup", tasteoff_id=tid,
                    club_id=state.get("club_id"),
                )
                await update.message.reply_text(t(loc, "club.to.already"))
                await self._show_tasteoff_setup(update, state.get("club_id"), tid)
            else:
                await update.message.reply_text(t(loc, "club.to.already"))
            return True
        if result.get("error") or not result.get("id"):
            await update.message.reply_text(t(loc, "club.err"))
            return True
        self.remember(
            chat_id, step="tasteoff_setup", tasteoff_id=result["id"],
            club_id=state.get("club_id"), club_name=state.get("club_name"),
        )
        await self._show_tasteoff_setup(update, state.get("club_id"), result["id"])
        return True

    async def _show_tasteoff_setup(self, update, club_id, tid, query=None):
        loc = self.locale(update)
        api = self._api(update)
        body = api.get_tasteoff(club_id, tid)
        if body.get("error"):
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        if body.get("is_host") is False:
            # Only the host sets up and opens; everyone else just waits.
            self.clear(str(update.effective_chat.id))
            text = t(
                loc, "club.to.hosting",
                host=esc(body.get("host_name") or "Someone"),
                title=esc(body.get("title") or ""),
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                t(loc, "club.back"), callback_data=club_cb("h", club_id),
            )]])
            if query is not None:
                from integrations.telegram_bot import _safe_edit
                await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
            else:
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
            return
        bottles = body.get("bottles") or []
        lines = [
            f"<b>{esc(body.get('title') or '')}</b>",
            t(loc, "club.to.setup", n=len(bottles)),
            "",
        ]
        for bottle in bottles:
            lines.append(esc(bottle.get("label") or ""))
        keyboard = [
            [InlineKeyboardButton(t(loc, "club.to.type_btn"), callback_data=club_cb("tb", tid))],
            [InlineKeyboardButton(t(loc, "club.to.from_list"), callback_data=club_cb("tl", tid))],
            [InlineKeyboardButton(t(loc, "club.to.from_cellar"), callback_data=club_cb("tf", tid))],
            [InlineKeyboardButton(t(loc, "club.to.open"), callback_data=club_cb("op", tid))],
            [InlineKeyboardButton(t(loc, "club.to.cancel_setup"), callback_data=club_cb("tx", tid))],
            [InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("h", club_id))],
        ]
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _prompt_tasteoff_label(self, update, tid, query):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        club_id = await self._club_id_for_tasteoff(update, tid)
        self.remember(
            chat_id, step="tasteoff_label", tasteoff_id=tid, club_id=club_id,
        )
        from integrations.telegram_bot import _safe_edit
        await _safe_edit(query, t(loc, "club.to.type_btn"))

    async def _tasteoff_add_typed(self, update, text: str) -> bool:
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        label = (text or "").strip()
        if len(label) < 2:
            await update.message.reply_text(t(loc, "club.to.type_btn"))
            return True
        api = self._api(update)
        result = api.add_tasteoff_bottle(state.get("club_id"), state.get("tasteoff_id"), label)
        if result.get("error") == "forbidden":
            await self._show_tasteoff_setup(update, state.get("club_id"), state.get("tasteoff_id"))
            return True
        if result.get("error"):
            await update.message.reply_text(t(loc, "club.err"))
            return True
        self.remember(chat_id, step="tasteoff_setup")
        await update.message.reply_text(t(loc, "club.to.added_bottle", label=label))
        await self._show_tasteoff_setup(update, state.get("club_id"), state.get("tasteoff_id"))
        return True

    async def _tasteoff_from_list(self, update, tid, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        from integrations.telegram_bot import _safe_edit
        if not self.handler.flag_on("feature_lists"):
            await _safe_edit(query, t(loc, "club.td.off"))
            return
        payload = api.club_list(club_id)
        items = payload.get("items") or []
        if not items:
            await self._tasteoff_no_pick(update, tid, club_id, "club.list.empty", query)
            return
        rows = []
        for item in items[:8]:
            label = f"{item.get('name') or ''} {item.get('vintage') or ''}".strip()
            rows.append([InlineKeyboardButton(
                label[:40] or "wine",
                callback_data=club_cb("ta", tid, item["id"]),
            )])
        rows.append([InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("to", club_id))])
        await _safe_edit(
            query, t(loc, "club.to.from_list"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _tasteoff_add_from_list(self, update, tid, item_id, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        payload = api.club_list(club_id)
        item = next((i for i in (payload.get("items") or []) if i.get("id") == item_id), None)
        from integrations.telegram_bot import _safe_edit
        if item is None:
            await _safe_edit(query, t(loc, "club.err"))
            return
        label = f"{item.get('name') or ''} {item.get('vintage') or ''}".strip()
        result = api.add_tasteoff_bottle(club_id, tid, label)
        if result.get("error") == "forbidden":
            await self._show_tasteoff_setup(update, club_id, tid, query=query)
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await self._show_tasteoff_setup(update, club_id, tid, query=query)

    async def _tasteoff_no_pick(self, update, tid, club_id, key, query):
        """Nothing to pick here: say why and offer typing a name or going back."""
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        await _safe_edit(query, t(loc, key), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(loc, "club.to.type_btn"), callback_data=club_cb("tb", tid))],
            [InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("to", club_id))],
        ]))

    async def _tasteoff_from_cellar(self, update, tid, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        home = api.get_club(club_id)
        from integrations.telegram_bot import _safe_edit
        if (home.get("cellar_visibility") or "none") != "full":
            await self._tasteoff_no_pick(update, tid, club_id, "club.to.summary_only", query)
            return
        members = (api.club_members(club_id) or {}).get("members") or []
        rows = []
        for member in members:
            if (member.get("peek") or home.get("cellar_visibility")) != "full":
                continue
            rows.append([InlineKeyboardButton(
                member.get("display_name") or "Member",
                callback_data=club_cb("tm", tid, member["user_id"]),
            )])
        if not rows:
            await self._tasteoff_no_pick(update, tid, club_id, "club.to.summary_only", query)
            return
        rows.append([InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("to", club_id))])
        await _safe_edit(
            query, t(loc, "club.to.from_cellar"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _tasteoff_member_wines(self, update, tid, user_id, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        from integrations.telegram_bot import _safe_edit
        peek = api.club_peek(club_id, user_id)
        if peek.get("error") or peek.get("visibility") != "full":
            await self._tasteoff_no_pick(update, tid, club_id, "club.to.summary_only", query)
            return
        wines = peek.get("wines") or []
        if not wines:
            await self._tasteoff_no_pick(update, tid, club_id, "club.to.cellar_empty", query)
            return
        chat_id = str(update.effective_chat.id)
        self.remember(chat_id, tasteoff_id=tid, club_id=club_id, step="tasteoff_setup")
        rows = []
        for wine in wines[:8]:
            label = f"{wine.get('name') or ''} {wine.get('vintage') or ''}".strip()
            wine_id = wine.get("id")
            if not wine_id:
                continue
            rows.append([InlineKeyboardButton(
                label[:40] or "wine",
                callback_data=club_cb("tw", tid, int(wine_id)),
            )])
        if not rows:
            await self._tasteoff_no_pick(update, tid, club_id, "club.to.cellar_empty", query)
            return
        rows.append([InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("tf", tid))])
        await _safe_edit(
            query, t(loc, "club.to.from_cellar"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _tasteoff_add_wine(self, update, tid, wine_id, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        from integrations.telegram_bot import _safe_edit
        peek = None
        members = (api.club_members(club_id) or {}).get("members") or []
        wine = None
        for member in members:
            payload = api.club_peek(club_id, member.get("user_id"))
            if payload.get("visibility") != "full":
                continue
            wine = next(
                (w for w in (payload.get("wines") or []) if w.get("id") == wine_id),
                None,
            )
            if wine is not None:
                break
        if wine is None:
            await _safe_edit(query, t(loc, "club.to.summary_only"))
            return
        label = f"{wine.get('name') or ''} {wine.get('vintage') or ''}".strip()
        result = api.add_tasteoff_bottle(club_id, tid, label, wine_id=wine_id)
        if result.get("error") == "forbidden":
            await self._show_tasteoff_setup(update, club_id, tid, query=query)
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await self._show_tasteoff_setup(update, club_id, tid, query=query)

    async def _open_tasteoff(self, update, tid, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        result = api.open_tasteoff(club_id, tid)
        from integrations.telegram_bot import _safe_edit
        if result.get("error") == "forbidden":
            await self._show_tasteoff_setup(update, club_id, tid, query=query)
            return
        if result.get("error") in ("need_wine", "need_two"):
            await _safe_edit(query, t(loc, "club.to.need_wine"))
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        chat_id = str(update.effective_chat.id)
        self.clear(chat_id)
        # The host tastes too: their card has the score rows, plus Close.
        await self._show_scoring(update, club_id, tid, query=query)

    async def _score_tasteoff(self, update, tid, bottle_id, score, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        result = api.score_tasteoff(club_id, tid, bottle_id, score)
        from integrations.telegram_bot import _safe_edit
        if result.get("error") == "not_scoring":
            await _safe_edit(query, t(loc, "club.to.closed"))
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        await self._show_scoring(update, club_id, tid, query=query)

    async def _show_scoring(self, update, club_id, tid, query=None):
        loc = self.locale(update)
        body = self._api(update).get_tasteoff(club_id, tid)
        if body.get("error"):
            await self._reply(update, t(loc, "club.err"), query=query)
            return
        text, markup = build_scoring(body, loc, host=body.get("is_host") is True)
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _tasteoff_name_tap(self, update, rest) -> bool:
        """A wine's name button: show its full name, never score it."""
        query = update.callback_query
        label = ""
        try:
            tid, bid = int(rest[0]), int(rest[1])
            club_id = await self._club_id_for_tasteoff(update, tid)
            body = self._api(update).get_tasteoff(club_id, tid)
            bottle = next((b for b in body.get("bottles") or [] if b.get("id") == bid), None)
            label = (bottle or {}).get("label") or ""
        except Exception as e:
            logger.info("[club] name tap lookup failed: %s", e)
        text = t(self.locale(update), "club.to.name_tap", label=label or "this wine")
        try:
            await query.answer(text[:190], show_alert=len(label) > 40)
        except Exception as e:
            logger.info("[club] answer() failed: %s", e)
        return True

    async def _show_history(self, update, club_id, query):
        """Finished tastings in this club, newest first, with the winner."""
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        try:
            rows = (self._api(update).list_tasteoffs(club_id) or {}).get("tasteoffs") or []
        except Exception as e:
            logger.warning("[club] tasting history failed: %s", e)
            await _safe_edit(query, t(loc, "club.err"))
            return
        closed = [r for r in rows if r.get("status") == "closed"]
        back = [InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("h", club_id))]
        if not closed:
            await _safe_edit(
                query, t(loc, "club.to.history_empty"),
                reply_markup=InlineKeyboardMarkup([back]),
            )
            return
        lines = [f"<b>{t(loc, 'club.to.history')}</b>", ""]
        buttons = []
        for row in closed[:10]:
            when = _short_date(row.get("closed_at"))
            winner = _winner(row)
            title = row.get("title") or "Tasting"
            lines.append(
                f"\U0001F3C6 <b>{esc(title)}</b>" + (f" · {when}" if when else "")
                + (f"\n   {esc(winner)}" if winner else "")
            )
            buttons.append([InlineKeyboardButton(
                f"{title[:32]}" + (f" · {when}" if when else ""),
                callback_data=club_cb("tv", club_id, row["id"]),
            )])
        buttons.append(back)
        await _safe_edit(
            query, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _show_past(self, update, club_id, tid, query):
        """One finished tasting: the ranking and how each person scored."""
        loc = self.locale(update)
        from integrations.telegram_bot import _safe_edit
        body = self._api(update).get_tasteoff(club_id, tid) or {}
        if body.get("error") or body.get("status") != "closed":
            await _safe_edit(query, t(loc, "club.err"))
            return
        when = _short_date(body.get("closed_at"))
        lines = [
            f"\U0001F3C6 <b>{esc(body.get('title') or '')}</b>"
            + (f" · {when}" if when else ""),
        ]
        if body.get("host_name"):
            lines.append(t(loc, "club.to.hosted_by", host=esc(body["host_name"])))
        lines.append("")
        bottles = sorted(
            body.get("bottles") or [],
            key=lambda b: (b.get("average") is None, -(b.get("average") or 0),
                           (b.get("label") or "").casefold()),
        )
        for n, bottle in enumerate(bottles, start=1):
            avg = bottle.get("average")
            avg_s = f"{avg:.1f}" if isinstance(avg, (int, float)) else "\u2014"
            lines.append(f"{n}. <b>{esc(bottle.get('label') or '')}</b> — {avg_s}")
            votes = bottle.get("votes") or []
            if votes:
                lines.append("   " + " · ".join(
                    f"{esc(v.get('name') or '?')} {v.get('score')}" for v in votes
                ))
            else:
                lines.append("   " + t(loc, "club.to.no_votes"))
        rows = []
        if body.get("can_reopen"):
            rows.append([InlineKeyboardButton(t(loc, "club.to.reopen"), callback_data=club_cb("tr", tid))])
        rows.append([InlineKeyboardButton(t(loc, "club.back"), callback_data=club_cb("th", club_id))])
        await _safe_edit(
            query, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _close_confirm(self, update, tid, query):
        """Close at once when everyone has scored; otherwise say who we're
        still waiting on and ask first (closing sends the results)."""
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        try:
            body = self._api(update).get_tasteoff(club_id, tid) or {}
        except Exception:
            body = {}
        waiting = [s.get("name") or "?" for s in body.get("scorers") or [] if not s.get("done")]
        if not waiting or body.get("status") != "scoring":
            await self._close_tasteoff(update, tid, query)
            return
        from integrations.telegram_bot import _safe_edit
        await _safe_edit(
            query,
            t(loc, "club.to.close_ask", names=esc(", ".join(waiting))),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(loc, "club.to.close_anyway"), callback_data=club_cb("tz", tid))],
                [InlineKeyboardButton(t(loc, "club.to.keep_open"), callback_data=club_cb("to", club_id))],
            ]),
        )

    async def _reopen_tasteoff(self, update, tid, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        result = self._api(update).reopen_tasteoff(club_id, tid)
        from integrations.telegram_bot import _safe_edit
        error = result.get("error")
        if error == "too_late":
            await _safe_edit(query, t(loc, "club.to.reopen_late"))
            return
        if error == "another_open":
            await _safe_edit(query, t(loc, "club.to.reopen_busy"))
            return
        if error == "not_closed":
            await self._show_scoring(update, club_id, tid, query=query)
            return
        if error:
            await _safe_edit(query, t(loc, "club.err"))
            return
        # Everyone (the host included) gets fresh score buttons from the
        # server; this message just says so.
        await _safe_edit(query, t(loc, "club.to.reopened"))

    async def _close_tasteoff(self, update, tid, query):
        loc = self.locale(update)
        club_id = await self._club_id_for_tasteoff(update, tid)
        api = self._api(update)
        result = api.close_tasteoff(club_id, tid)
        from integrations.telegram_bot import _safe_edit
        if result.get("error") == "already_closed":
            await _safe_edit(query, t(loc, "club.to.closed"))
            return
        if result.get("error"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        rankings = (result.get("bottles") or [])
        # Prefer payload rankings from a follow-up feed event if present.
        lines = [t(loc, "club.to.closed"), ""]
        scored = []
        for bottle in rankings:
            if bottle.get("average") is not None:
                scored.append(bottle)
        scored.sort(key=lambda b: (-(b.get("average") or 0), b.get("label") or ""))
        for index, bottle in enumerate(scored, 1):
            avg = bottle.get("average")
            n = bottle.get("n") or 0
            lines.append(
                f"{index}. {esc(bottle.get('label') or '')} — {avg} "
                f"({t(loc, 'club.to.n_scores', n=n)})"
            )
        title = result.get("title") or ""
        if title:
            lines[0] = f"\U0001F3C6 {esc(title)}"
        markup = None
        if result.get("can_reopen"):
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                t(loc, "club.to.reopen"), callback_data=club_cb("tr", tid),
            )]])
        await _safe_edit(query, "\n".join(lines), parse_mode="HTML", reply_markup=markup)

    async def _cancel_tasteoff_setup(self, update, tid, query):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        club_id = await self._club_id_for_tasteoff(update, tid)
        self.clear(chat_id)
        from integrations.telegram_bot import _safe_edit
        if club_id:
            await self._show_home(update, club_id, query=query)
        else:
            await _safe_edit(query, t(loc, "club.end.cancelled", club=""))

    async def after_household_switch(self, update_or_query, chat_id, household_id, household_name):
        if not clubs_enabled(self.handler):
            return
        api = self.handler._chat_id_to_api.get(str(chat_id)) or self.handler._get_api(update_or_query)
        loc = self.locale(update=update_or_query, chat_id=chat_id)
        try:
            data = api.list_clubs()
        except Exception:
            return
        clubs = data.get("clubs") or []
        last = next((c for c in clubs if c.get("is_last_used")), None) or (clubs[0] if clubs else None)
        if last is None:
            return
        home = api.get_club(last["id"])
        current_hh = (home.get("household") or {}).get("name") or ""
        if (home.get("household") or {}).get("id") == household_id:
            return
        text = t(loc, "club.switch.ask", household=esc(household_name), club=esc(last.get("name") or "club"))
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(loc, "club.switch.yes"), callback_data=club_cb("hy", last["id"], household_id)),
            InlineKeyboardButton(
                t(loc, "club.switch.keep", household=current_hh or "cellar"),
                callback_data=club_cb("hk", last["id"]),
            ),
        ]])
        bot = getattr(getattr(self.handler, "_app", None), "bot", None)
        if bot is not None:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
