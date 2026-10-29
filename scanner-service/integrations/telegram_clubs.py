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
    remove_keyboard,
    reverse_geocode,
    share_keyboard,
    validate_typed_name,
)

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 30 * 60
VOTE_GLYPH = {"down": "\U0001F44E", "up": "\U0001F44D", "meh": ""}
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


def _wine_line(event: dict, loc: str) -> str:
    kind = event.get("kind")
    name = event.get("actor_name") or "Someone"
    wine = event.get("wine_name") or "wine"
    vintage = event.get("vintage") or ""
    if kind == "joined":
        return t(loc, "club.feed.joined", name=esc(name))
    if event.get("retracted"):
        body = t(loc, "club.feed.put_back", name=esc(name), vintage=vintage, wine=esc(wine))
        return " ".join(body.split())
    body = t(loc, "club.feed.opened", name=esc(name), vintage=vintage, wine=esc(wine))
    body = " ".join(body.split())
    glyph = VOTE_GLYPH.get(event.get("vote") or "", "")
    return f"{body} {glyph}".rstrip()


def build_home(payload: dict, loc: str) -> tuple[str, InlineKeyboardMarkup]:
    name = payload.get("name") or "Club"
    members = payload.get("member_count") or 1
    vis = payload.get("cellar_visibility") or "full"
    badges = payload.get("badges_visible")
    policy = _visibility_label(loc, vis)
    badge_bit = t(loc, "club.share.badges_on" if badges else "club.share.badges_off")
    lines = [
        f"\U0001F377 <b>{esc(name)}</b> · {t(loc, 'club.home.n_members', n=members)}",
        f"{esc(policy)} · {esc(badge_bit)}",
        "",
    ]
    events = [e for e in (payload.get("events") or []) if not e.get("retracted")]
    if not events:
        lines.append(t(loc, "club.feed.empty"))
    else:
        for event in events[:5]:
            lines.append(_wine_line(event, loc))
    keyboard = [
        [
            InlineKeyboardButton(t(loc, "club.home.invite"), callback_data=club_cb("i", payload["id"])),
            InlineKeyboardButton(t(loc, "club.home.members"), callback_data=club_cb("m", payload["id"])),
        ],
        [
            InlineKeyboardButton(t(loc, "club.home.feed"), callback_data=club_cb("fd", payload["id"], 0)),
            InlineKeyboardButton(
                t(loc, "club.home.unmute" if payload.get("feed_muted") else "club.home.mute"),
                callback_data=club_cb("u", payload["id"]),
            ),
        ],
        [
            InlineKeyboardButton(t(loc, "club.home.leave"), callback_data=club_cb("l", payload["id"])),
        ],
    ]
    if payload.get("role") == "owner":
        keyboard[-1].insert(
            0,
            InlineKeyboardButton(
                t(loc, "club.home.settings"), callback_data=club_cb("s", payload["id"]),
            ),
        )
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


class ClubFlow:
    def __init__(self, handler):
        self.handler = handler
        self._states: dict[str, dict] = {}

    def locale(self, update=None, chat_id=None) -> str:
        return locale_for(self.handler, update=update, chat_id=chat_id)

    def _api(self, update):
        return self.handler._get_api(update)

    def _staff_api(self):
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
        if not clubs:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(loc, "club.found.btn"), callback_data=club_cb("f")),
            ]])
            await update.message.reply_text(
                t(loc, "club.pitch"), reply_markup=keyboard,
            )
            return
        if len(clubs) == 1:
            await self._show_home(update, clubs[0]["id"])
            return
        rows = []
        for club in clubs:
            mark = "\u2705 " if club.get("is_last_used") else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{club.get('name') or 'Club'}",
                callback_data=club_cb("x", club["id"]),
            )])
        rows.append([InlineKeyboardButton(t(loc, "club.found.btn"), callback_data=club_cb("f"))])
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
        state = self.state_for(chat_id)
        if not state:
            return False
        step = state.get("step")
        loc = self.locale(update)
        text = (update.message.text or "").strip()
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
                t(loc, "club.named.ok"), reply_markup=remove_keyboard(),
            )
            await self._join_card(update, query=None)
            return True
        if step == "join" and text.casefold() == "skip":
            return True
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
            t(self.locale(update), "club.named.ok"), reply_markup=remove_keyboard(),
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
        text, markup = build_home(payload, loc)
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
            if hh_id:
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
        if payload.get("error"):
            text = t(loc, "club.err")
        else:
            events = payload.get("events") or []
            lines = [f"\U0001F377 <b>{esc(t(loc, 'club.home.feed'))}</b>", ""]
            if not events:
                lines.append(t(loc, "club.feed.empty"))
            for event in events:
                line = _wine_line(event, loc)
                if event.get("retracted"):
                    line = f"<i>{line}</i>"
                lines.append(line)
            text = "\n".join(lines)
        keyboard = []
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
        if preview.get("expired") or preview.get("used"):
            await update.message.reply_text(t(loc, "club.fresh"))
            return
        chat_id = str(update.effective_chat.id)
        code = preview.get("code")
        club = preview.get("club") or {}
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
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(join_label, callback_data=club_cb("j", code)),
            InlineKeyboardButton(t(loc, "club.join.not_now"), callback_data=club_cb("nn", code)),
        ]])
        if query is not None:
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _do_join(self, update, query, code: str):
        loc = self.locale(update)
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id) or {}
        api = self._staff_api()
        from integrations.telegram_bot import _safe_edit
        if api is None:
            await _safe_edit(query, t(loc, "club.unreachable"))
            return
        display = ""
        user = getattr(update, "effective_user", None)
        if user is not None:
            display = user.full_name or user.first_name or "Someone"
        try:
            result = api.redeem_club_invite(
                code, chat_id, display,
                household_name=state.get("household_name"),
                household_id=state.get("household_id"),
            )
        except Exception:
            logger.exception("[club] redeem failed")
            await _safe_edit(query, t(loc, "club.unreachable"))
            return
        status = result.get("status_code")
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
        if result.get("error") and not result.get("token"):
            await _safe_edit(query, t(loc, "club.err"))
            return
        self.clear(chat_id)
        if result.get("token"):
            try:
                await self.handler._invite_flow()._hot_add(chat_id, result)
            except Exception:
                logger.exception("[club] hot-add failed")
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
        try:
            await query.answer()
        except Exception as e:
            logger.info("[club] answer() failed: %s", e)
        loc = self.locale(update)
        if not clubs_enabled(self.handler) and action not in ("j", "nn", "ph"):
            from integrations.telegram_bot import _safe_edit
            await _safe_edit(query, t(loc, "club.off"))
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
            await self._do_join(update, query, rest[0] if rest else "")
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
        else:
            return True
        return True

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
        if result.get("error") in ("vote_closed",) or result.get("status_code") == 409:
            await _safe_edit(query, t(loc, "club.end.over"))
            return
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
        await _safe_edit(query, t(loc, "club.end.status",
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
