"""One-time household invites and the /start onboarding they open.

State lives in memory, keyed by chat id — same idea as a wine-list correction.
Nothing is provisioned until Join is tapped, or a new cellar has a name.
Buttons carry the invite code, so a restart only loses the typed-name step.
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def mask_invite_code(code: str) -> str:
    """Last two chars only — enough to match a log line, not enough to redeem."""
    code = code or ""
    if len(code) < 2:
        return "…"
    return f"…{code[-2:]}"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_UA = "WhineyWineCellar/1.0 (household invite; contact: cellar)"
NAME_MIN = 2
NAME_MAX = 40
# Long enough to name a cellar; short enough that a later search isn't swallowed.
STATE_TTL_SECONDS = 30 * 60
CALLBACK_BUDGET = 64

JOIN_COPY = (
    "Join <b>{name}</b> on Whiney.\n\n"
    "Tap this, then Start:\n"
    "{link}\n\n"
    "Forward this, or tap Share.\n\n"
    "{expiry}"
)
NEW_COPY = (
    "You're invited to start a cellar on Whiney.\n\n"
    "Tap this, then Start:\n"
    "{link}\n\n"
    "Send a photo of a bottle. That's the whole app.\n\n"
    "Forward this, or tap Share.\n\n"
    "{expiry}"
)
SHARE_TEXT_JOIN = "Join {name} on Whiney. Tap the link, then Start."
SHARE_TEXT_NEW = "Start a cellar on Whiney. Tap the link, then Start."
EXPIRY_FALLBACK = "One use, lasts 7 days."
JOIN_CARD = (
    "{who} invited you to <b>{household}</b>.\n\n"
    "Whiney keeps a cellar: photo a label and it tracks the bottle."
)
NOT_NOW = "No problem. That link still works until {when}. Tap it again whenever you're ready."
NOT_NOW_UNKNOWN = "No problem. That link still works. Tap it again whenever you're ready."
STRANGER_COPY = (
    "Hi <b>{name}</b>.\n\n"
    "Ask a friend already using Whiney to send you an invite.\n"
    "If they ask for a chat ID, /myid shows it."
)
KNOWN_START = (
    "Hi <b>{name}</b>.\n\n"
    "You're in <b>{household}</b>.\n\n"
    "Send a photo of a bottle and I'll add it. A wine list works too.\n\n"
    "/help if you want the rest."
)
KNOWN_START_NO_HH = (
    "Hi <b>{name}</b>.\n\n"
    "Send a photo of a bottle and I'll add it. A wine list works too.\n\n"
    "/help if you want the rest."
)
I1_COPY = (
    "You're starting your own cellar, {name}.\n\n"
    "What should we call it? A house name, a nickname, your street — "
    "whatever you'll recognise in a list.\n\n"
    "Or share your area and I'll suggest one. I only use the pin for a name. "
    "I don't keep it."
)
I2_COPY = (
    "{name} is live. You're the owner.\n\n"
    "Grab the nearest bottle and send a photo of the front label.\n\n"
    "I'll read producer, grape, region, vintage, notes, ratings and price.\n\n"
    "Wine list on a table? Photo of that works too — I'll tell you what to order."
)
NAME_FIRST = "Name the cellar first — type something, share your suburb, or tap Skip."
TAKEN_COPY = "That name's taken — pick another"
PHOTO_RESEND = "That name's taken — pick another, then send the photo again."
PHOTO_NAME_FIRST = "Name the cellar first, then send that photo again."
CALL_IT = "Call it {name}?"
KEEP_SEARCHING = "Type a wine name, or send a photo."
FRESH_LINK = "That invite has expired or was already used. Ask them for a fresh link."
UNREACHABLE = "Couldn't reach the cellar just now — tap the link again in a minute."
TAP_AGAIN = "Tap your invite link again and I'll pick this up."
CANCEL_JOIN = "Cancelled the invite to {name} ({code})."
CANCEL_NEW = "Cancelled the new-cellar invite ({code})."
CANCEL_NONE = "You don't have an unused invite to cancel."

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def esc(text: str) -> str:
    """For user text inside parse_mode=HTML. Plain-text sends must not use it."""
    return html.escape(text or "", quote=False)


def invite_link(username: str, code: str) -> str:
    return f"https://t.me/{username}?start={code}"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_until(expires_at) -> str | None:
    """'19 Oct', or '2 Jan 2027' outside the current year. None if unparseable."""
    dt = _parse_dt(expires_at)
    if dt is None:
        return None
    day = f"{dt.day} {_MONTHS[dt.month - 1]}"
    if dt.year != datetime.now(timezone.utc).year:
        return f"{day} {dt.year}"
    return day


def format_expiry(expires_at) -> str:
    when = format_until(expires_at)
    if when is None:
        return EXPIRY_FALLBACK
    return f"Works until {when}, one use."


def join_share_text(household_name: str, link: str, expires_at=None) -> str:
    return JOIN_COPY.format(
        name=esc(household_name), link=link, expiry=format_expiry(expires_at),
    )


def new_share_text(link: str, expires_at=None) -> str:
    return NEW_COPY.format(link=link, expiry=format_expiry(expires_at))


def share_keyboard(link: str, text: str) -> InlineKeyboardMarkup:
    url = "https://t.me/share/url?" + urllib.parse.urlencode(
        {"url": link, "text": text}, quote_via=urllib.parse.quote,
    )
    return InlineKeyboardMarkup([[InlineKeyboardButton("Share invite", url=url)]])


def invite_callback(action: str, code: str = "", extra: str = "") -> str:
    """inv:<action>[:<code>[:<extra>]], with extra trimmed to fit 64 bytes."""
    head = f"inv:{action}" + (f":{code}" if code else "")
    if not extra:
        return head
    room = CALLBACK_BUDGET - len(head.encode("utf-8")) - 1
    raw = extra.encode("utf-8")[: max(room, 0)]
    while raw and (raw[-1] & 0xC0) == 0x80:
        raw = raw[:-1]
    return f"{head}:{raw.decode('utf-8', errors='ignore')}"


def parse_invite_callback(data: str) -> tuple[str, str, str]:
    """(action, code, extra). Pre-code buttons like inv:join parse with code=''."""
    action, _, rest = (data or "")[4:].partition(":")
    code, _, extra = rest.partition(":")
    return action, code, extra


def _button(label_prefix: str, action: str, code: str, name: str) -> InlineKeyboardButton:
    # The label is rebuilt from what survives in callback_data, so a trimmed
    # name can never show one thing and commit another.
    data = invite_callback(action, code, name)
    shown = parse_invite_callback(data)[2]
    return InlineKeyboardButton(f"{label_prefix}{shown}", callback_data=data)


def default_cellar_name(display_name: str) -> str:
    first = (display_name or "").strip().split()[0] if display_name else ""
    if not first:
        return "My cellar"
    return f"{first}'s cellar"


def local_alternative(taken: str, display: str) -> str:
    """Fallback when the API gave no suggestion. Never the taken name itself."""
    first = (display or "").strip().split()[:1]
    first = first[0] if first else ""
    if first:
        cand = f"{taken} ({first})".strip()[:NAME_MAX].rstrip()
        if cand.casefold() != (taken or "").casefold():
            return cand
    return f"{taken} 2".strip()[:NAME_MAX].rstrip()


def validate_typed_name(text: str) -> str | None:
    """Return an error string, or None if the name can be committed."""
    name = (text or "").strip()
    if name.startswith("/"):
        return NAME_FIRST
    if len(name) < NAME_MIN or len(name) > NAME_MAX:
        return f"Keep it between {NAME_MIN} and {NAME_MAX} characters."
    return None


def suburb_from_address(address: dict | None) -> str | None:
    """Suburb / neighbourhood / village / town / city. Never a street."""
    if not address:
        return None
    for key in ("suburb", "neighbourhood", "village", "town", "city"):
        value = (address.get(key) or "").strip()
        if value and not re.search(r"\d", value):
            return value
    return None


def reverse_geocode(lat: float, lon: float, timeout: float = 3) -> str | None:
    """Nominatim suburb. Failure is a fall-through, not an error to show."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": NOMINATIM_UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        return suburb_from_address((resp.json() or {}).get("address"))
    except Exception as e:
        logger.info(f"[invite] reverse geocode failed: {e}")
        return None


def classify_invite_result(result, *, exc: bool = False, api_missing: bool = False) -> str | None:
    """Copy to show, or None if the payload is usable. 409 is handled by callers."""
    if api_missing or exc or not isinstance(result, dict):
        return UNREACHABLE
    status = result.get("status_code")
    if status in (404, 410) or result.get("used") or result.get("expired"):
        return FRESH_LINK
    if status in (401, 403) or (isinstance(status, int) and status >= 500):
        return UNREACHABLE
    if result.get("error") and not result.get("token"):
        if status is None:
            return UNREACHABLE
        return result.get("error") or FRESH_LINK
    return None


def name_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("\U0001F4CD Use my suburb", request_location=True)],
            [KeyboardButton("Skip")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def join_card(inviter: str, household: str, code: str = "") -> tuple[str, InlineKeyboardMarkup]:
    text = JOIN_CARD.format(who=esc(inviter or "Someone"), household=esc(household))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Join", callback_data=invite_callback("join", code)),
        InlineKeyboardButton("Not now", callback_data=invite_callback("no", code)),
    ]])
    return text, keyboard


def taken_keyboard(code: str, suggested: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("", "tn", code, suggested)],
        [InlineKeyboardButton("Type another", callback_data=invite_callback("type", code))],
    ])


def suburb_keyboard(code: str, suburb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Use ", "use", code, suburb)],
        [InlineKeyboardButton("Type another", callback_data=invite_callback("type", code))],
        [InlineKeyboardButton("Skip", callback_data=invite_callback("skip", code))],
    ])


def confirm_name_keyboard(code: str, name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("Use ", "ok", code, name)],
        [InlineKeyboardButton("Keep searching", callback_data=invite_callback("drop"))],
    ])


def already_in_keyboard(code: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Stay", callback_data=invite_callback("stay"))],
        [InlineKeyboardButton("Start a new cellar", callback_data=invite_callback("start", code))],
    ])


def household_picker(households: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for h in households:
        rows.append([InlineKeyboardButton(
            h.get("name") or "Cellar",
            callback_data=f"inv:hh:{h.get('id')}",
        )])
    return InlineKeyboardMarkup(rows)


def welcome_join(household: str, bottles: int) -> str:
    name = esc(household)
    if bottles:
        noun = "bottle" if bottles == 1 else "bottles"
        pitch = (
            f"{name} already has {bottles} {noun}. "
            "Send a photo of a bottle to add one."
        )
    else:
        pitch = (
            f"{name} is empty. Send a photo of a bottle and I'll add it."
        )
    return f"{pitch}\n\nAdd someone with /invite"


def welcome_member(display_name: str, household_name: str) -> str:
    """Shorter /adduser welcome. No command dump."""
    name = esc(display_name or "there")
    return (
        f"You're in, <b>{name}</b>.\n\n"
        f"You've been added to <b>{esc(household_name)}</b>.\n\n"
        "Send a photo of a bottle to add one.\n\n"
        "Add someone with /invite"
    )


def first_bottle_text(household: str, wine_line: str) -> str:
    bits = [f"{household} has its first bottle."]
    if wine_line:
        bits.append(wine_line)
    bits.append("Send another, or a wine list next time you're out. Fridges and extra people can wait.")
    return "\n\n".join(bits)


def i2_text(household: str) -> str:
    return I2_COPY.format(name=household)


COUNTRY_CHOICES = (
    ("AU", "Australia"),
    ("NZ", "New Zealand"),
    ("ZA", "South Africa"),
    ("GB", "United Kingdom"),
    ("US", "United States"),
    ("FR", "France"),
    ("IT", "Italy"),
    ("ES", "Spain"),
    ("DE", "Germany"),
    ("PT", "Portugal"),
)


def country_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in COUNTRY_CHOICES:
        row.append(InlineKeyboardButton(
            label, callback_data=invite_callback("cy", code),
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        "Skip", callback_data=invite_callback("cy", "skip"),
    )])
    return InlineKeyboardMarkup(rows)


class InviteFlow:
    """In-memory onboarding. The handler owns persistence and the API."""

    def __init__(self, handler):
        self.handler = handler
        self._states: dict[str, dict] = {}

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

    def bot_username(self) -> str:
        cached = getattr(self.handler, "bot_username", None)
        if cached:
            return cached
        return "bot"

    def _api(self, update):
        try:
            return self.handler._get_api(update)
        except Exception:
            return self.handler.api

    def _staff_api(self):
        """Preview and redeem are staff-only. The clicker usually has no token."""
        api = getattr(self.handler, "admin_api", None)
        if api is not None:
            return api
        logger.warning("invite staff client missing; falling back to handler.api")
        return self.handler.api

    def _display_name(self, update) -> str:
        user = getattr(update, "effective_user", None)
        if user and user.first_name:
            return user.first_name
        return "there"

    async def cmd_invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = [a.lower() for a in (context.args or [])]
        chat_id = str(update.effective_chat.id)
        is_staff = chat_id in getattr(self.handler, "admin_chat_ids", [])
        api = self._api(update)

        if args and args[0] == "new":
            if not is_staff:
                await update.message.reply_text(
                    "That's a staff invite. /invite sends a link to <b>your</b> cellar.",
                    parse_mode="HTML",
                )
                return
            await self._mint_new(update, api)
            return

        if args and args[0] == "cancel":
            await self._cancel(update, api)
            return

        await self._mint_join(update, api)

    async def _mint_new(self, update, api) -> None:
        try:
            result = api.mint_invite("new")
        except Exception as e:
            await update.message.reply_text(f"Couldn't make that invite: {e}")
            return
        if result.get("error"):
            await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
            return
        # One forwardable message. No private follow-up — they will forward the wrong one.
        link = invite_link(self.bot_username(), result["code"])
        await update.message.reply_text(
            new_share_text(link, result.get("expires_at")),
            reply_markup=share_keyboard(link, SHARE_TEXT_NEW),
        )

    async def _cancel(self, update, api) -> None:
        household_id = None
        try:
            household_id = (api.get_household() or {}).get("id")
        except Exception:
            household_id = None
        try:
            result = api.cancel_invite(household_id=household_id)
        except Exception as e:
            await update.message.reply_text(f"Couldn't cancel: {e}")
            return
        if result.get("error"):
            await update.message.reply_text(CANCEL_NONE)
            return
        code = result.get("code") or ""
        if result.get("household_name"):
            text = CANCEL_JOIN.format(name=result["household_name"], code=code)
        else:
            text = CANCEL_NEW.format(code=code)
        await update.message.reply_text(text)

    async def _mint_join(self, update, api, household_id=None) -> None:
        households = []
        try:
            households = api.list_households() or []
        except Exception:
            households = []
        if household_id is None and len(households) > 1:
            await update.message.reply_text(
                "Which cellar is this invite for?",
                reply_markup=household_picker(households),
            )
            return
        if household_id is None and len(households) == 1:
            # Staff must name the household even when there is only one.
            household_id = households[0].get("id")
        try:
            result = api.mint_invite("join", household_id=household_id)
        except Exception as e:
            await update.message.reply_text(f"Couldn't make that invite: {e}")
            return
        if result.get("error"):
            await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
            return
        text, keyboard = self._join_share(result)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    def _join_share(self, result: dict) -> tuple[str, InlineKeyboardMarkup]:
        name = (result.get("household") or {}).get("name") or "your cellar"
        link = invite_link(self.bot_username(), result["code"])
        return (
            join_share_text(name, link, result.get("expires_at")),
            share_keyboard(link, SHARE_TEXT_JOIN.format(name=name)),
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle /start. Returns True if this module answered."""
        code = (context.args or [None])[0]
        if not code:
            if self.handler._is_known(update):
                return False
            await self._stranger(update)
            return True
        await self._open_code(update, code)
        return True

    async def _stranger(self, update) -> None:
        await update.message.reply_text(
            STRANGER_COPY.format(name=esc(self._display_name(update))),
            parse_mode="HTML",
        )

    async def _open_code(self, update, code: str) -> None:
        api = self._staff_api()
        if api is None:
            await update.message.reply_text(UNREACHABLE)
            return
        try:
            preview = api.preview_invite(code)
        except Exception:
            logger.exception("[invite] preview failed")
            await update.message.reply_text(UNREACHABLE)
            return
        error = classify_invite_result(preview)
        if error == UNREACHABLE:
            await update.message.reply_text(UNREACHABLE)
            return

        chat_id = str(update.effective_chat.id)
        display = self._display_name(update)
        kind = preview.get("kind")
        household_id = (preview.get("household") or {}).get("id")
        if preview.get("used"):
            if self._already_there(update, household_id):
                household = preview.get("household_name") or "that cellar"
                await update.message.reply_text(
                    f"You're already in {household}. Send a photo of a bottle."
                )
                return
            await self._resume_used(update, code, display)
            return
        if error:
            await self._dead_code(update, preview)
            return
        if kind == "join":
            household = preview.get("household_name") or "that cellar"
            if self._already_there(update, household_id):
                await update.message.reply_text(
                    f"You're already in {household}. Send a photo of a bottle."
                )
                return
            inviter = preview.get("inviter_display_name") or ""
            self.remember(
                chat_id, step="join", code=code, kind="join",
                household=household, household_id=household_id,
                inviter=inviter, expires_at=preview.get("expires_at"),
            )
            text, keyboard = join_card(inviter, household, code)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
            return

        # New cellar. Same code resumes I1 — do not create a second house.
        existing = self._current_household_name(update)
        self.remember(
            chat_id, step="confirm" if existing else "name",
            code=code, kind="new", display_name=display,
            existing_household=existing, expires_at=preview.get("expires_at"),
        )
        if existing:
            await update.message.reply_text(
                f"You're already in {existing}. This link starts a separate cellar.",
                reply_markup=already_in_keyboard(code),
            )
            return
        await self._show_i1(update, display)

    def _current_household_name(self, update) -> str:
        if not self.handler._is_known(update):
            return ""
        try:
            data = self.handler._get_api(update).get_household()
        except Exception:
            return ""
        return (data or {}).get("name") or ""

    def _membership_ids(self, update) -> set[int]:
        """Every household this chat belongs to. Staff list_households is global, so use whois."""
        if not self.handler._is_known(update):
            return set()
        chat_id = str(update.effective_chat.id)
        ids: set[int] = set()
        try:
            if chat_id in (getattr(self.handler, "admin_chat_ids", []) or []):
                data = self.handler.api.whois(chat_id) or {}
                for row in data.get("results", []) or []:
                    if str(row.get("chat_id")) != chat_id:
                        continue
                    for h in row.get("households", []) or []:
                        ids.add(int(h["id"]))
            else:
                for h in self.handler._get_api(update).list_households() or []:
                    ids.add(int(h["id"]))
        except Exception:
            ids = set()
        if ids:
            return ids
        try:
            data = self.handler._get_api(update).get_household() or {}
            return {int(data["id"])}
        except Exception:
            return set()

    def _already_there(self, update, household_id) -> bool:
        try:
            hid = int(household_id)
        except (TypeError, ValueError):
            return False
        return hid in self._membership_ids(update)

    async def _resume_used(self, update, code: str, display: str) -> None:
        """Same person tapped the link again after a lost welcome. Redeem is
        idempotent for them and returns the token the bot needs to hot-add."""
        api = self._staff_api()
        if api is None:
            await update.message.reply_text(UNREACHABLE)
            return
        chat_id = str(update.effective_chat.id)
        try:
            result = api.redeem_invite(code, chat_id, display)
        except Exception:
            logger.exception("[invite] resume redeem failed")
            await update.message.reply_text(UNREACHABLE)
            return
        error = classify_invite_result(result)
        if error:
            await update.message.reply_text(error)
            return
        await self._hot_add(chat_id, result)
        household = result.get("household_name") or "the cellar"
        await update.message.reply_text(
            f"You're already in {household}. Send a photo of a bottle."
        )

    async def _dead_code(self, update, preview: dict) -> None:
        household = preview.get("household_name") or ""
        household_id = (preview.get("household") or {}).get("id")
        if household and self._already_there(update, household_id):
            await update.message.reply_text(
                f"You're already in {household}. Send a photo of a bottle."
            )
            return
        await update.message.reply_text(FRESH_LINK)

    async def _show_i1(self, update, display: str) -> None:
        await update.message.reply_text(
            I1_COPY.format(name=display),
            reply_markup=name_keyboard(),
        )

    async def _rehydrate(self, chat_id: str, code: str, display: str) -> tuple[dict | None, str | None]:
        """New-cellar state for this code, re-previewing if memory was lost."""
        state = self.state_for(chat_id)
        if state and state.get("code") == code:
            return state, None
        if not code:
            return None, TAP_AGAIN
        api = self._staff_api()
        if api is None:
            return None, UNREACHABLE
        try:
            preview = api.preview_invite(code)
        except Exception:
            logger.exception("[invite] rehydrate preview failed")
            return None, UNREACHABLE
        error = classify_invite_result(preview)
        if error:
            return None, error
        if preview.get("kind") != "new":
            # A join code must never be redeemed with a household_name.
            return None, TAP_AGAIN
        state = self.remember(
            chat_id, step="name", code=code, kind="new", display_name=display,
            existing_household="", expires_at=preview.get("expires_at"),
        )
        return state, None

    async def on_callback(self, update, context) -> bool:
        query = update.callback_query
        data = query.data or ""
        if not data.startswith("inv:"):
            return False
        try:
            await query.answer()
        except Exception:
            pass
        chat_id = str(query.message.chat_id)
        action, code, extra = parse_invite_callback(data)
        logger.info(
            "[tap] chat=%s invite action=%s code=%s extra=%s",
            chat_id, action, mask_invite_code(code) if code else "-", extra or "-",
        )
        if action == "hh":
            try:
                hid = int(code)
            except ValueError:
                return True
            api = self._api(update)
            await self._mint_join_edit(query, api, hid)
            return True
        if action == "cy":
            await self._set_country(query, code)
            return True

        state = self.state_for(chat_id)
        code = code or (state or {}).get("code") or ""
        user = getattr(query, "from_user", None)
        display = (
            (user.first_name if user and user.first_name else "")
            or (state or {}).get("display_name")
            or "there"
        )

        if action == "no":
            when = format_until((state or {}).get("expires_at"))
            self.clear(chat_id)
            await query.edit_message_text(
                NOT_NOW.format(when=when) if when else NOT_NOW_UNKNOWN,
            )
            return True
        if action == "join":
            if not code:
                await query.edit_message_text(TAP_AGAIN)
                return True
            await self._redeem_join(query, context, code, display)
            return True
        if action == "stay":
            self.clear(chat_id)
            name = (state or {}).get("existing_household") or "your cellar"
            await query.edit_message_text(f"Staying in {name}.")
            return True
        if action == "drop":
            self.clear(chat_id)
            await query.edit_message_text(KEEP_SEARCHING)
            return True
        if action == "start":
            state, error = await self._rehydrate(chat_id, code, display)
            if error:
                await query.edit_message_text(error)
                return True
            state["step"] = "name"
            name = state.get("display_name") or display
            await query.edit_message_text("Alright — a new cellar.")
            await context.bot.send_message(
                chat_id=chat_id,
                text=I1_COPY.format(name=name),
                reply_markup=name_keyboard(),
            )
            return True
        if action == "type":
            state, error = await self._rehydrate(chat_id, code, display)
            if error:
                await query.edit_message_text(error)
                return True
            state["step"] = "name"
            await query.edit_message_text("Type a name, or tap Skip.")
            return True
        if action == "skip":
            name_for = (state or {}).get("display_name") or display
            await self._commit_name(query, context, code, display, default_cellar_name(name_for))
            return True
        if action in ("use", "tn", "ok"):
            name = extra or (state or {}).get("suggested") or ""
            if not name:
                await query.edit_message_text(TAP_AGAIN)
                return True
            await self._commit_name(query, context, code, display, name)
            return True
        if action == "taken":
            if not state:
                await query.edit_message_text(TAP_AGAIN)
                return True
            name = state.get("suggested_name") or local_alternative(
                state.get("taken_name") or "", state.get("display_name") or display,
            )
            await self._commit_name(query, context, code, display, name)
            return True
        return True

    async def _mint_join_edit(self, query, api, household_id: int) -> None:
        try:
            result = api.mint_invite("join", household_id=household_id)
        except Exception as e:
            await query.edit_message_text(f"Couldn't make that invite: {e}")
            return
        if result.get("error"):
            await query.edit_message_text(result["error"])
            return
        text, keyboard = self._join_share(result)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _redeem_join(self, query, context, code: str, display: str) -> None:
        chat_id = str(query.message.chat_id)
        api = self._staff_api()
        if api is None:
            await query.edit_message_text(UNREACHABLE)
            return
        try:
            result = api.redeem_invite(code, chat_id, display)
        except Exception:
            logger.exception("[invite] join redeem failed")
            await query.edit_message_text(UNREACHABLE)
            return
        error = classify_invite_result(result)
        if error:
            if error != UNREACHABLE:
                self.clear(chat_id)
            await query.edit_message_text(error)
            return
        await self._finish_provision(query, context, result, kind="join")

    def _suggestion(self, result: dict, sent: str, display: str) -> str:
        suggested = (result.get("suggested_name") or "").strip()
        if suggested and suggested.casefold() != (sent or "").casefold():
            return suggested
        return local_alternative(sent, display)

    async def on_text(self, update, context) -> bool:
        """True if this message belongs to naming and must not run as a command."""
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id)
        if not state or state.get("step") not in ("name", "confirm"):
            return False
        known = self.handler._is_known(update)
        if state.get("step") == "confirm":
            # They already have a cellar and didn't pick Start: let this text search.
            self.clear(chat_id)
            return False
        text = (update.message.text or "").strip()
        if text.startswith("/"):
            if text.split()[0].split("@")[0] == "/start":
                return False  # /start <code> resumes via the command handler
            if known:
                self.clear(chat_id)
                return False
            await update.message.reply_text(NAME_FIRST)
            return True
        if text.lower() == "skip":
            await self._provision_named(
                update, context, state,
                default_cellar_name(state.get("display_name") or self._display_name(update)),
            )
            return True
        error = validate_typed_name(text)
        if error:
            await update.message.reply_text(error)
            return True
        if known:
            # A known user typing is usually searching. Make the name a proposal.
            state["pending_name"] = text
            await update.message.reply_text(
                CALL_IT.format(name=text),
                reply_markup=confirm_name_keyboard(state.get("code") or "", text),
            )
            return True
        await self._provision_named(update, context, state, text)
        return True

    async def on_location(self, update, context) -> bool:
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id)
        if not state or state.get("step") != "name":
            return False
        loc = update.message.location
        suburb = None
        if loc is not None:
            suburb = reverse_geocode(loc.latitude, loc.longitude)
        # Do not store lat/lng. Deny/fail falls through to typing, no guilt.
        if not suburb:
            await update.message.reply_text(
                "Type a name, or tap Skip.",
                reply_markup=name_keyboard(),
            )
            return True
        state["suggested"] = suburb
        await update.message.reply_text(
            CALL_IT.format(name=suburb),
            reply_markup=suburb_keyboard(state.get("code") or "", suburb),
        )
        return True

    async def on_photo(self, update, context) -> bool:
        """Stranger photo during naming auto-names, provisions, then runs the photo handler."""
        chat_id = str(update.effective_chat.id)
        state = self.state_for(chat_id)
        if not state or state.get("kind") != "new" or state.get("step") not in ("name", "confirm"):
            return False
        if state.get("step") == "confirm":
            # Not consent to a second cellar — file it in the one they have.
            self.clear(chat_id)
            return False
        if self.handler._is_known(update):
            await update.message.reply_text(PHOTO_NAME_FIRST)
            return True
        display = state.get("display_name") or self._display_name(update)
        name = default_cellar_name(display)
        ok = await self._provision_named(
            update, context, state, name, photo=True,
        )
        if not ok:
            return True
        await self.handler._handle_photo(update, context)
        return True

    async def _commit_name(self, query, context, code: str, display: str, name: str) -> None:
        chat_id = str(query.message.chat_id)
        state, error = await self._rehydrate(chat_id, code, display)
        if error:
            await query.edit_message_text(error)
            return
        display = state.get("display_name") or display
        api = self._staff_api()
        if api is None:
            await query.edit_message_text(UNREACHABLE)
            return
        try:
            result = api.redeem_invite(code, chat_id, display, household_name=name)
        except Exception:
            logger.exception("[invite] name-button redeem failed")
            await query.edit_message_text(UNREACHABLE)
            return
        if result.get("status_code") == 409:
            suggestion = self._suggestion(result, name, display)
            state.update(taken_name=name, suggested_name=suggestion, step="name")
            await query.edit_message_text(
                TAKEN_COPY, reply_markup=taken_keyboard(code, suggestion),
            )
            return
        error = classify_invite_result(result)
        if error:
            if error != UNREACHABLE:
                self.clear(chat_id)
            await query.edit_message_text(error)
            return
        self.clear(chat_id)
        await self._hot_add(chat_id, result)
        await query.edit_message_text(i2_text(result.get("household_name") or name))
        if result.get("first_cellar"):
            await self._ask_country(context, chat_id)
        await self._ping_created(context, result, display)

    async def _provision_named(self, update, context, state, name: str, photo: bool = False) -> bool:
        chat_id = str(update.effective_chat.id)
        api = self._staff_api()
        code = state.get("code") or ""
        display = state.get("display_name") or self._display_name(update)
        if api is None:
            await update.message.reply_text(UNREACHABLE, reply_markup=remove_keyboard())
            return False
        try:
            result = api.redeem_invite(code, chat_id, display, household_name=name)
        except Exception:
            logger.exception("[invite] typed-name redeem failed")
            await update.message.reply_text(UNREACHABLE, reply_markup=remove_keyboard())
            return False
        if result.get("status_code") == 409:
            suggestion = self._suggestion(result, name, display)
            state.update(taken_name=name, suggested_name=suggestion)
            await update.message.reply_text(
                PHOTO_RESEND if photo else TAKEN_COPY,
                reply_markup=taken_keyboard(code, suggestion),
            )
            return False
        error = classify_invite_result(result)
        if error:
            if error != UNREACHABLE:
                self.clear(chat_id)
            await update.message.reply_text(error, reply_markup=remove_keyboard())
            return False
        self.clear(chat_id)
        await self._hot_add(chat_id, result)
        household = result.get("household_name") or name
        if photo:
            await update.message.reply_text(
                f"{household} it is. Reading the label\u2026",
                reply_markup=remove_keyboard(),
            )
        else:
            await update.message.reply_text(
                i2_text(household), reply_markup=remove_keyboard(),
            )
        await self._ping_created(context, result, display)
        if result.get("first_cellar"):
            await self._ask_country(context, chat_id)
        return True

    async def _finish_provision(self, query, context, result, kind: str) -> None:
        chat_id = str(query.message.chat_id)
        self.clear(chat_id)
        household = result.get("household_name") or "the cellar"
        if result.get("already_member"):
            await query.edit_message_text(
                f"You're already in {household}. Send a photo of a bottle."
            )
            await self._hot_add(chat_id, result)
            return
        await self._hot_add(chat_id, result)
        bottles = result.get("bottle_count") or 0
        await query.edit_message_text(welcome_join(household, bottles), parse_mode="HTML")
        inviter = result.get("created_by_chat_id")
        display = result.get("display_name") or "Someone"
        if inviter:
            try:
                await context.bot.send_message(
                    chat_id=inviter,
                    text=f"{display} joined {household}.",
                )
            except Exception as e:
                logger.info(f"[invite] could not ping inviter {inviter}: {e}")

    async def _hot_add(self, chat_id: str, result: dict) -> None:
        household_id = result.get("household_id") or (result.get("household") or {}).get("id")
        token = result.get("token")
        new_api = None
        # Prefer the token redeem just minted — config refetch can lag or fail.
        # admin_api has the same base_url if the primary client isn't set yet.
        base = getattr(self.handler, "api", None) or getattr(self.handler, "admin_api", None)
        if token and base is not None:
            try:
                from integrations.wine_cellar_api import WineCellarAPI
                storage = "Cellar" if result.get("first_cellar") else getattr(
                    base, "default_storage", "Cellar",
                )
                new_api = WineCellarAPI(
                    base_url=base.base_url,
                    api_token=token,
                    default_storage=storage,
                    web_url=getattr(base, "web_url", ""),
                    household_id=household_id,
                )
            except Exception:
                logger.exception("[invite] could not build client from redeem token")
                new_api = None
        factory = getattr(self.handler, "_api_factory", None)
        if new_api is None and factory and household_id:
            try:
                new_api = factory(chat_id, household_id)
            except Exception:
                logger.exception("[invite] api factory failed after redeem")
                new_api = None
        self.handler.add_chat_id(chat_id, api_client=new_api)
        set_ccy = getattr(self.handler, "set_currency", None)
        if callable(set_ccy):
            set_ccy(chat_id, result.get("currency") or "EUR")
        set_cty = getattr(self.handler, "set_country", None)
        if callable(set_cty):
            set_cty(chat_id, result.get("country") or "")
        if new_api is None:
            logger.error("[invite] provisioned %s with no API client", chat_id)
        else:
            logger.info(
                "[invite] provisioned chat_id=%s household_id=%s",
                chat_id, household_id,
            )
        if result.get("first_cellar"):
            pending = getattr(self.handler, "_first_cellars", None)
            if pending is None:
                self.handler._first_cellars = set()
            self.handler._first_cellars.add(str(chat_id))

    async def _ask_country(self, context, chat_id: str) -> None:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id) if str(chat_id).isdigit() else chat_id,
                text="Where should I price wines?",
                reply_markup=country_keyboard(),
            )
        except Exception as e:
            logger.info("[invite] country prompt failed: %s", e)

    async def _set_country(self, query, country: str) -> None:
        chat_id = str(query.message.chat_id)
        country = (country or "").upper()
        if country == "SKIP":
            country = ""
        api = self.handler._get_api(chat_id)
        try:
            result = api.update_settings(country=country)
        except Exception as e:
            logger.warning("[invite] settings: %s", e)
            await query.edit_message_text(
                "Couldn't save that.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Try again", callback_data=invite_callback("cy", country or "skip"),
                    ),
                ]]),
            )
            return
        ccy = (result or {}).get("currency") or "EUR"
        set_ccy = getattr(self.handler, "set_currency", None)
        if callable(set_ccy):
            set_ccy(chat_id, ccy)
        set_cty = getattr(self.handler, "set_country", None)
        if callable(set_cty):
            set_cty(chat_id, (result or {}).get("country") or "")
        names = dict(COUNTRY_CHOICES)
        asked = getattr(self.handler, "_country_asked", None)
        if asked is None:
            self.handler._country_asked = set()
        self.handler._country_asked.add(chat_id)
        if country and country in names:
            await query.edit_message_text(
                f"{names[country]}. Send a photo of a bottle.",
            )
        else:
            await query.edit_message_text(
                "Euro prices it is. Send a photo of a bottle.",
            )

    async def _ping_created(self, context, result: dict, display: str) -> None:
        household = result.get("household_name") or "a cellar"
        targets = list(getattr(self.handler, "admin_chat_ids", []) or [])
        inviter = result.get("created_by_chat_id")
        if inviter and inviter not in targets:
            targets.append(inviter)
        text = f"{display} created {household}."
        for cid in targets:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception as e:
                logger.info(f"[invite] could not ping {cid}: {e}")


def maybe_first_bottle(handler, chat_id, household_name: str, wine_line: str, stock_count) -> str | None:
    """Return the first-bottle card once, or None to keep the normal add card."""
    pending = getattr(handler, "_first_cellars", None) or set()
    if str(chat_id) not in pending:
        return None
    try:
        count = int(stock_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count != 1:
        return None
    pending.discard(str(chat_id))
    return first_bottle_text(household_name or "Your cellar", wine_line)
