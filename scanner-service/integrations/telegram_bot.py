"""Telegram bot for wine scanner notifications, disambiguation, and photo handling."""

import asyncio
import functools
import html
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from telegram import (
    Bot,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from integrations.card_store import CardStore, StoreError

logger = logging.getLogger(__name__)

PENDING_TIMEOUT_SECONDS = 600  # 10 minutes
TYPED_ADD_TTL = 3600  # typed "confirm"/"add" still finds the card for an hour
TYPED_ADD_RE = re.compile(
    r"^(confirm|add|add it|add to cellar|yes|ok|\u2705)\W*$",
    re.IGNORECASE,
)
TYPED_DRANK_THIS_RE = re.compile(
    r"^(?:(?:i|ek)\s+)?"
    r"(?:drank this|drank it|remove one|gedrink|het dit gedrink|verwyder een)"
    r"\W*$",
    re.IGNORECASE,
)
TYPED_DRANK_OF_RE = re.compile(
    r"^(?:(?:i|ek)\s+)?"
    r"(?:drank|removed?|verwyder|het)\s+"
    r"(?:one|een|a)\s+"
    r"(?:of\s+(?:my\s+)?|van\s+my\s+)?"
    r"(.+?)"
    r"(?:\s+gedrink)?"
    r"\W*$",
    re.IGNORECASE,
)
PHOTO_TRIAGE_FAILED = (
    "I couldn't read that photo in time. Send it again — "
    "a closer, brighter shot of the label usually works."
)
DRANK_THIS_LABEL = "\U0001F377 Drank this"
WINE_SITE_LINK = "Open in Whiney Wino"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def parse_typed_stock(text: str) -> tuple[str, str] | None:
    """Map a typed drink/remove line to ('this', '') or ('of', query)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if TYPED_DRANK_THIS_RE.match(raw):
        return "this", ""
    match = TYPED_DRANK_OF_RE.match(raw)
    if match:
        query = (match.group(1) or "").strip()
        if query:
            return "of", query
    return None


def match_in_stock_wines(wines: list[dict], query: str) -> list[dict]:
    """In-stock rows whose name/producer/grape mention the typed wine."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    stem = needle[:-1] if needle.endswith("s") and len(needle) > 4 else needle
    hits = []
    for wine in wines or []:
        blob = " ".join(
            str(wine.get(key) or "")
            for key in ("name", "producer", "grape", "region")
        ).lower()
        if needle in blob or stem in blob:
            hits.append(wine)
    return hits


def _confirm_drink_markup(wine_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "\U0001F377 Yes, drank it",
            callback_data=_cb("dy", wine_id=wine_id),
        )],
        [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
    ])


def _confirm_drink_text(wine: dict) -> str:
    name = html.escape(str(wine.get("name") or "that wine"), quote=False)
    stock = wine.get("stock_count")
    extra = f" ({stock} in stock)" if stock is not None else ""
    return f"Remove 1× <b>{name}</b>{extra} and mark it as drunk?"

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
DRANK_PAGE_SIZE = 5
BROWSE_SESSION_TTL = 600  # 10 minutes
WINELIST_TTL = 1800  # 30 minutes — you may sit with a menu a while
WINELIST_PER_PAGE = 5  # numbers on a row; Telegram squeezes more than this
MISREAD_TTL = 900  # 15 minutes to type the real name after a wrong read

# Telegram limits callback_data to 64 bytes. We use short keys to stay under.
# Format: "a:action,w:wine_id,b:barcode,o:original_action"
# Example: "a:ww,w:42,b:7798039590229,o:in" = 37 bytes


CALLBACK_DATA_MAX = 64


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
    data = ",".join(parts)
    size = len(data.encode("utf-8"))
    if size > CALLBACK_DATA_MAX:
        raise ValueError(
            f"callback_data is {size} bytes (Telegram limit {CALLBACK_DATA_MAX}): {data!r}"
        )
    return data


CARD_FINISHED = (
    "That card's finished — send the photo again if you still want to add it."
)
BARCODE_FINISHED = "This barcode was already handled or expired."
UNREADABLE_LABEL = "I couldn't read this label, try a clearer photo"
SIDE_PHOTO_GONE = (
    "That card is gone or already added — send a new photo if you still want "
    "to attach one."
)


def _finished_copy(barcode: str = "") -> str:
    """Label-only cards are not barcodes; don't say they expired as one."""
    if str(barcode).startswith("_"):
        return CARD_FINISHED
    return BARCODE_FINISHED


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


# Optional bottle vote: thumbs down, a dash (indifferent), thumbs up.
# A face-with-straight-mouth read as "disgusting", so the middle is just —.
VOTE_LABELS = (("down", "\U0001F44E"), ("meh", "\u2014"), ("up", "\U0001F44D"))
VOTE_PROMPT = "How was it? (optional)"
VOTE_THANKS = {
    "up": "\U0001F44D Got it.",
    "down": "\U0001F44E Got it.",
    "meh": "Got it.",
}
VOTE_REACT = {
    "up": "\U0001F44D",
    "down": "\U0001F44E",
    "meh": "\U0001F44C",  # OK hand — 😐 is not a reaction we want
}


def vote_keyboard_row(item_id: int, chosen: str | None = None) -> list:
    buttons = []
    for val, label in VOTE_LABELS:
        text = f"[{label}]" if chosen == val else label
        buttons.append(InlineKeyboardButton(
            text, callback_data=_cb("vt", item_id=item_id, original=val),
        ))
    return buttons


def apply_vote_thanks(text: str, chosen: str) -> str:
    """Swap the vote prompt for a one-line thanks. Keeps the rest of the card."""
    thanks = VOTE_THANKS.get(chosen, "Got it.")
    if not text:
        return thanks
    if VOTE_PROMPT in text:
        return text.replace(VOTE_PROMPT, thanks)
    for prior in VOTE_THANKS.values():
        if prior in text:
            return text.replace(prior, thanks)
    return text.rstrip() + "\n\n" + thanks


def apply_vote_choice(markup, chosen: str) -> InlineKeyboardMarkup:
    """Keep every other button; mark the chosen vote so they can change it later."""
    labels = dict(VOTE_LABELS)
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            data = _parse_cb(btn.callback_data or "")
            if data.get("action") == "vt":
                val = data.get("original") or ""
                emoji = labels.get(val, val)
                text = f"[{emoji}]" if val == chosen else emoji
                new_row.append(InlineKeyboardButton(text, callback_data=btn.callback_data))
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    return InlineKeyboardMarkup(new_rows)


def build_drank_view(
    wine_name: str, stock_count: int, item_id: int, chosen: str | None = None,
    wine_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"\U0001F377 <b>Drank:</b> {wine_name}\n"
        f"\U0001F4CA In stock: {stock_count}\n\n"
        f"{VOTE_PROMPT}"
    )
    rows = []
    if item_id and wine_id:
        rows.append([InlineKeyboardButton(
            "\u21a9\ufe0f Undo",
            callback_data=_cb("ul", wine_id=wine_id, item_id=item_id),
        )])
    rows.append(vote_keyboard_row(item_id, chosen))
    return text, InlineKeyboardMarkup(rows)


def format_drank_at(value) -> str:
    """Relative time for a drank/removed timestamp from the API."""
    if not value:
        return ""
    if isinstance(value, str):
        raw = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return value[:10]
    else:
        dt = value
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return dt.strftime("%d %b %Y")
    if seconds < 3600:
        return "just now"
    hours = seconds // 3600
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    return dt.strftime("%d %b %Y")


def build_drank_history_view(payload: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """A page of recently drank/removed bottles with notes and label buttons."""
    items = payload.get("items") or []
    total = int(payload.get("count") or 0)
    offset = int(payload.get("offset") or 0)
    limit = int(payload.get("limit") or DRANK_PAGE_SIZE) or DRANK_PAGE_SIZE
    page = offset // limit

    if not items and offset == 0:
        text = (
            "\U0001F377 <b>Recent bottles</b>\n\n"
            "Nothing here yet.\n\n"
            "When you finish a bottle, tap <b>Drank this</b> on the label "
            "card \u2014 it will show up here with its notes and photo."
        )
        return text, None

    start = offset + 1
    end = offset + len(items)
    header = f"\U0001F377 <b>Recent bottles</b>  <i>{start}\u2013{end} of {total}</i>\n"
    lines = [header]
    keyboard = []
    vote_emoji = dict(VOTE_LABELS)

    for i, item in enumerate(items, start=start):
        wine_id = item.get("wine_id")
        name = item.get("wine_name") or "Wine"
        emoji = WINE_TYPE_EMOJI.get(item.get("wine_type") or "", "\U0001F377")
        vote = vote_emoji.get(item.get("vote") or "", "")
        title = f"{emoji} <b>{name}</b>"
        if vote:
            title += f"  {vote}"
        lines.append(f"\n{i}. {title}")

        meta = []
        when = format_drank_at(item.get("drank_at"))
        if when:
            meta.append(when)
        producer = (item.get("producer") or "").strip()
        if producer:
            meta.append(producer)
        if meta:
            lines.append("   " + " \u00b7 ".join(meta))

        comment = (item.get("comment") or "").strip()
        if comment:
            preview = comment if len(comment) <= 140 else comment[:137] + "..."
            lines.append(f"   <i>{preview}</i>")

        row = [InlineKeyboardButton(
            f"\U0001F4DD {i}. Notes",
            callback_data=_cb("dn", wine_id=wine_id, page=page),
        )]
        if item.get("has_image"):
            row.append(InlineKeyboardButton(
                "\U0001F4F8 Label",
                callback_data=_cb("di", wine_id=wine_id, page=page),
            ))
        keyboard.append(row)

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            "\u2b05\ufe0f Newer 5",
            callback_data=_cb("dh", page=max(0, page - 1)),
        ))
    if offset + len(items) < total:
        nav.append(InlineKeyboardButton(
            "Older 5 \u27a1\ufe0f",
            callback_data=_cb("dh", page=page + 1),
        ))
    if nav:
        keyboard.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_drank_notes_view(wine: dict, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    name = wine.get("name") or "Wine"
    text = f"\U0001F4DD <b>{name}</b>\n\n"
    producer = (wine.get("producer") or "").strip()
    if producer:
        text += f"\U0001F464 {producer}\n\n"
    comment = (wine.get("comment") or "").strip()
    text += f"{comment}\n" if comment else "<i>No tasting notes yet.</i>\n"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "\u2b05\ufe0f Back", callback_data=_cb("dh", page=page),
        ),
    ]])
    return text, keyboard


def build_stock_update_view(
    action: str,
    wine_name: str,
    stock_count: int,
    *,
    wine_id: int,
    barcode: str = "",
    storage: str = "",
    item_id: int | None = None,
    drawers: int = 0,
    row: int | None = None,
    storage_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Fridge-scan card: default location already applied; buttons fix it.

    Mass IN/OUT stays beep-and-next. This card is the after-the-fact editor.
    """
    if action == "in":
        emoji, verb = "\U0001F4E5", "Added"
    else:
        emoji, verb = "\U0001F4E4", "Removed"
    lines = [f"{emoji} <b>{verb}:</b> {wine_name}"]
    if storage:
        place = storage
        if action == "in" and row:
            place = f"{storage} · drawer {row}"
        prefix = "" if action == "in" else "from "
        lines.append(f"\U0001F4CD {prefix}{place}")
    lines.append(f"\U0001F4CA In stock: {stock_count}")
    if stock_count == 0:
        lines.append("")
        lines.append("\u26A0\ufe0f <b>Last bottle removed!</b>")
    if action == "in":
        undo_cb = _cb("ua", wine_id=wine_id)
    elif item_id:
        undo_cb = _cb("ul", wine_id=wine_id, item_id=item_id)
    else:
        undo_cb = _cb("ur", wine_id=wine_id)
    keyboard = [[
        InlineKeyboardButton("\u21a9\ufe0f Undo", callback_data=undo_cb),
        InlineKeyboardButton("\U0001F195 Wrong wine", callback_data=_cb(
            "ww", wine_id=wine_id, barcode=barcode, original=action,
        )),
    ]]
    extra = []
    if action == "in" and item_id:
        extra.append(InlineKeyboardButton(
            "\U0001F4E6 Move", callback_data=_cb("sp", wine_id=wine_id, item_id=item_id, barcode=barcode),
        ))
        if drawers and drawers > 0:
            extra.append(InlineKeyboardButton(
                "\U0001F5C4 Drawer", callback_data=_cb("dp", wine_id=wine_id, item_id=item_id, barcode=barcode),
            ))
    elif action == "out":
        extra.append(InlineKeyboardButton(
            "From another fridge",
            callback_data=_cb("rf", wine_id=wine_id, barcode=barcode, page=storage_id or 0),
        ))
    if extra:
        keyboard.append(extra)
    if action == "out" and item_id:
        lines.append("")
        lines.append(VOTE_PROMPT)
        keyboard.append(vote_keyboard_row(item_id))
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


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


class NotProvisionedError(Exception):
    """Raised when a chat_id has no API client mapped to it.

    Never fall back to an arbitrary household here — that would silently hand
    one user another household's cellar.
    """

    def __init__(self):
        super().__init__(
            "You're not set up for a household yet. Ask an owner to run "
            "/adduser with your chat ID (send /myid to get it)."
        )


def pending_key(household_id, barcode: str) -> str:
    """Namespace a pending barcode to its household.

    The store is process-global and shared by every household on the bot. Keying
    on the bare barcode meant one household's photo could be matched against
    another's in-flight scan, and /pending and /flush spanned all tenants.

    Internal keys (_label_N, _edit_<wine_id>) are already globally unique — a
    counter and a database PK — and some call sites write them directly, so they
    must never be prefixed or the reads and writes won't agree.
    """
    if barcode.startswith("_"):
        return barcode
    return f"{household_id}:{barcode}" if household_id is not None else barcode


def _active_pendings(pending_store: dict, household_id=None) -> dict:
    """Return non-stale, non-resolved pending barcodes as {barcode: data}.

    Filters out internal temp keys (_label_, _edit_). When household_id is
    given, only that household's entries are returned. Keys are unwrapped back
    to bare barcodes so callers and callback_data stay unchanged.
    """
    now = time.time()
    active = {}
    for key, data in pending_store.items():
        if key.startswith("_") or data.get("resolved"):
            continue
        if (now - data.get("time", 0)) >= PENDING_TIMEOUT_SECONDS:
            continue
        if household_id is not None and data.get("household_id") != household_id:
            continue
        active[data.get("barcode") or key] = data
    return active


def _sweep_pendings(pending_store: dict) -> int:
    """Drop every entry past its TTL, including internal _label_/_edit_ keys.

    Entries hold the full label JPEG in "photo_data". Nothing else evicts them:
    _active_pendings only filters what it *lists*, and /flush only pops the
    active ones — so without this the store grows for the life of the process.
    """
    now = time.time()
    stale = [
        key for key, data in pending_store.items()
        if (now - data.get("time", 0)) >= PENDING_TIMEOUT_SECONDS
    ]
    for key in stale:
        pending_store.pop(key, None)
    return len(stale)


def _parse_drink_by(val) -> str | None:
    """Keep only an ISO date. Anything else 400s WineCreateSerializer."""
    if val is None or val == "":
        return None
    if hasattr(val, "isoformat"):
        try:
            return val.isoformat()[:10]
        except Exception:
            return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "unknown", "n/a"):
        return None
    try:
        datetime.strptime(s[:10], "%Y-%m-%d")
        return s[:10]
    except ValueError:
        return None


def _as_money(value) -> float | None:
    """A number we can store as Wine/StorageItem.price (max_digits=6)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").replace(" ", ""))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (int, float)):
        return None
    n = float(value)
    if n < 0:
        return None
    return round(min(n, 9999.99), 2)


def _paid_price(data: dict | None, scene: str | None = None) -> float | None:
    """What they paid — a typed price or a shelf tag, never a Grok estimate.

    Restaurant list prices are markups, so a menu scene does not count as paid.
    A shelf tag or a sticker on a label photo does.
    """
    if not data:
        return None
    paid = _as_money(data.get("price"))
    if paid is not None:
        return paid
    if scene == "menu":
        return None
    return _as_money(data.get("list_price"))


def _clean_wine_data(grok_data: dict) -> dict:
    """Strip non-wine fields and enforce API field limits."""
    clean = {k: v for k, v in grok_data.items()
             if k not in ("type", "barcode", "reference_lookup") and v is not None}
    # Remove empty strings — avoids serializer blank validation errors
    clean = {k: v for k, v in clean.items() if v != ""}
    if "description" in clean and len(clean["description"]) > 450:
        clean["description"] = clean["description"][:447] + "..."
    if "name" in clean:
        from integrations.grok_vision import normalise_label_case
        clean["name"] = normalise_label_case(str(clean["name"]))
        if len(clean["name"]) > 100:
            clean["name"] = clean["name"][:97] + "..."
    if "producer" in clean:
        from integrations.grok_vision import normalise_label_case
        clean["producer"] = normalise_label_case(str(clean["producer"]))
    if "country" in clean:
        clean["country"] = str(clean["country"])[:2]
    if "wine_type" in clean and clean["wine_type"] not in WINE_TYPES:
        clean["wine_type"] = "RE"
    if "vintage" in clean:
        try:
            clean["vintage"] = int(float(clean["vintage"]))
        except (TypeError, ValueError):
            clean.pop("vintage")
    if "abv" in clean:
        try:
            clean["abv"] = float(str(clean["abv"]).replace("%", "").strip())
        except (TypeError, ValueError):
            clean.pop("abv")
    if "drink_by" in clean:
        parsed = _parse_drink_by(clean["drink_by"])
        if parsed:
            clean["drink_by"] = parsed
        else:
            clean.pop("drink_by")
    for field, limit in (
        ("producer", 100), ("grape_variety", 100), ("region", 100),
        ("appellation", 100), ("vineyard", 100),
    ):
        if field in clean:
            clean[field] = str(clean[field])[:limit]
    # Ensure list fields are actually lists
    for field in ("food_pairings", "attributes"):
        if field in clean and not isinstance(clean[field], list):
            clean.pop(field)
    # Filter empty strings from lists
    for field in ("food_pairings", "attributes"):
        if field in clean:
            clean[field] = [str(x)[:100] for x in clean[field] if x]
    # Shelf tag / sticker → paid price. Market/retail estimates stay off this field.
    paid = _paid_price(grok_data)
    if paid is not None:
        clean["price"] = paid
    for extra in ("list_price", "retail_price"):
        clean.pop(extra, None)
    return clean


BADGE_GIF_DIR = Path(__file__).resolve().parent.parent / "assets" / "badges"
BADGE_FAMILIES = (
    "bottles", "country", "explorer", "type", "grape",
    "producer", "alphabet", "decade",
)


def _badge_family(slug: str) -> str:
    base = (slug or "").split(":")[0]
    if base == "grape-explorer":
        return "grape"
    return base if base in BADGE_FAMILIES else "bottles"


def badge_gif_path(achievements: list | None) -> Path | None:
    """Looping GIF for the first unlock's family. None if the file isn't there."""
    if not achievements:
        return None
    slug = (achievements[0] or {}).get("slug") or "bottles"
    path = BADGE_GIF_DIR / f"{_badge_family(slug)}.gif"
    return path if path.is_file() else None


def _taste_wine(data: dict | None) -> dict:
    """Compact wine fields for a Just-looking intent. No ratings."""
    data = data or {}
    grape = data.get("grape") or data.get("grape_variety") or ""
    if isinstance(grape, list):
        grape = ", ".join(str(x) for x in grape if x)
    vintage = data.get("vintage")
    return {
        "name": str(data.get("name") or "")[:120],
        "producer": str(data.get("producer") or "")[:120],
        "vintage": vintage if isinstance(vintage, int) else None,
        "wine_type": str(data.get("wine_type") or "")[:8],
        "country": str(data.get("country") or "")[:8],
        "grape": str(grape)[:120],
        "region": str(data.get("region") or "")[:120],
    }


def cellar_match_for_hint(hits: list[dict], identified: dict | None) -> tuple[str, list]:
    """How a typed correction should land in the cellar.

    Returns (action, wines):
      stock — add a bottle to this existing row (same vintage, or only hit)
      create — new row (named vintage is not in the cellar)
      pick — show these hits, don't guess
    """
    identified = identified or {}
    vintage = identified.get("vintage")
    if vintage is not None:
        try:
            vintage = int(vintage)
        except (TypeError, ValueError):
            vintage = None
    if not hits:
        return "create", []
    if vintage is not None:
        same_year = [w for w in hits if w.get("vintage") == vintage]
        if len(same_year) == 1:
            return "stock", same_year
        if len(same_year) > 1:
            return "pick", same_year
        return "create", []
    if len(hits) == 1:
        return "stock", hits
    return "pick", hits[:8]


def format_achievements(achievements: list | None) -> str:
    """Telegram card for newly unlocked cellar challenges. Empty string if none."""
    if not achievements:
        return ""
    lines = ["\U0001F3C6 <b>Cellar challenge</b>\n"]
    for a in achievements:
        emoji = a.get("emoji") or "\U0001F377"
        lines.append(f"{emoji} <b>{a.get('title', '')}</b>")
        if a.get("detail"):
            lines.append(a["detail"])
    return "\n".join(lines)


def format_competition(this_month) -> list[str]:
    """Household month race for /badges. Always shown when the payload exists."""
    if not this_month:
        return []
    if isinstance(this_month, list):
        standings = this_month
        label, days_left = "This month", None
    else:
        standings = this_month.get("standings") or []
        label = this_month.get("label") or "This month"
        days_left = this_month.get("days_left")
    race = len(standings) > 1
    heading = f"\U0001F3C1 <b>{label} race</b>" if race else f"\U0001F3C1 <b>{label} race</b>"
    if days_left is not None:
        if days_left == 0:
            heading += " \u2014 last day"
        elif days_left == 1:
            heading += " \u2014 1 day left"
        else:
            heading += f" \u2014 {days_left} days left"
    lines = [heading]
    if not standings:
        lines.append("Nobody has logged a bottle this month yet \u2014 first one starts the race.")
        lines.append("")
        return lines
    for row in standings:
        rank = row.get("rank")
        name = row.get("name") or "Someone"
        bottles = row.get("bottles") or 0
        prefix = f"{rank}. " if rank else "  "
        line = f"{prefix}{name} \u2014 {bottles}"
        if row.get("leading") and race:
            line += " \U0001F451"
        lines.append(line)
    lines.append("")
    return lines


def format_progress(progress: dict | None) -> list[str]:
    """Unlocked-adjacent lines for /badges: next milestones, then missing types."""
    if not progress:
        return []
    lines: list[str] = []
    close = list(progress.get("close") or [])
    tracks = list(progress.get("tracks") or [])
    missing = list(progress.get("missing") or [])
    if tracks or close:
        lines.append("<b>On the way</b>")
        for row in tracks + close:
            have, nxt = row.get("have"), row.get("next")
            emoji = row.get("emoji") or "\U0001F377"
            title = row.get("title") or ""
            if have is not None and nxt:
                lines.append(f"{emoji} {title} \u2014 {have}/{nxt}")
            else:
                lines.append(f"{emoji} {title}")
        lines.append("")
    if missing:
        lines.append("<b>Not yet</b>")
        for row in missing:
            emoji = row.get("emoji") or "\U0001F377"
            lines.append(f"{emoji} {row.get('title', '')}")
    return lines


async def send_achievement_card(bot, chat_id, achievements: list | None) -> None:
    """GIF + caption when a file exists; otherwise the same text as a message."""
    text = format_achievements(achievements)
    if not text:
        return
    gif = badge_gif_path(achievements)
    if gif:
        try:
            await bot.send_animation(
                chat_id=chat_id,
                animation=InputFile(BytesIO(gif.read_bytes()), filename=gif.name),
                caption=text,
                parse_mode="HTML",
            )
            return
        except Exception as e:
            logger.warning(f"Could not send achievement gif: {e}")
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


LABEL_HEADER = "\U0001F50D <b>Label analysis complete!</b>\n\n"
REFS_PENDING_NOTE = "\n\u23F3 <i>Checking ratings and price\u2026</i>"
JUST_LOOKING_NOTE = "\n\n\U0001F440 <i>Just looking \u2014 not added to your cellar.</i>"
BARCODE_TIP = (
    "\n\n\U0001F4F8 <i>Send the barcode side too so scans find it next time.</i>"
)
WHO_MAKES_THIS = (
    "Who makes this?\n\n"
    "Type the producer, or add without one."
)
ADD_WITHOUT_PRODUCER = "\u23ed Add without producer"
ADD_TO_CELLAR = "\u2705 Add to cellar"
ADD_ANOTHER_PHOTO = "\U0001F4F8 Add another photo (front/back)"
ADD_PHOTO_INFO = "\U0001F4F8 Add photo/info"
VIEW_WINE = "\U0001F377 View wine"


def _callback_status(query):
    """Message-like object whose edit_text is the callback's edit_message_text."""
    message = getattr(query, "message", None)

    class _Status:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "message_id", None)

        async def edit_text(self, *args, **kwargs):
            return await _safe_edit(query, *args, **kwargs)

    return _Status()


def _public_web_base(api) -> str:
    """Public site URL for Telegram links; empty when unset or loopback.

    WineCellarAPI falls back to the internal API base_url when web_url is
    missing, so a phone would otherwise see http://127.0.0.1:8000/wine/5.
    """
    raw = getattr(api, "web_url", None)
    if raw is None:
        return ""
    base = str(raw).strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base if "://" in base else f"https://{base}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host in _LOOPBACK_HOSTS:
        return ""
    return base


def _wine_url(api, wine_id) -> str:
    base = _public_web_base(api)
    if not base or not wine_id:
        return ""
    return f"{base}/wine/{wine_id}"


def _wine_followup_rows(wine_id) -> list:
    return [[
        InlineKeyboardButton(ADD_PHOTO_INFO, callback_data=_cb("ai", wine_id=wine_id)),
        InlineKeyboardButton(VIEW_WINE, callback_data=_cb("det", wine_id=wine_id)),
    ]]


def _append_wine_url(text: str, api, wine_id) -> str:
    url = _wine_url(api, wine_id)
    if not url:
        return text
    href = html.escape(url, quote=True)
    link = f'<a href="{href}">{html.escape(WINE_SITE_LINK)}</a>'
    if link in (text or "") or url in (text or ""):
        return text
    return f"{text.rstrip()}\n\n{link}"

REFERENCE_FIELDS = (
    "critic_score", "vivino_rating", "category_rating",
    "market_price", "market_price_currency",
)


def _needs_references(data: dict) -> bool:
    """True when the ratings are worth a live search.

    Both ratings present means the vision call already produced something for
    each; a missing one is what the web search is for. An empty name cannot
    be looked up — POST /api/references/lookup/ is a 400 without one.
    """
    if not str(data.get("name") or "").strip():
        return False
    return data.get("critic_score") is None or data.get("vivino_rating") is None


def _merge_stored_references(data: dict, wine: dict) -> None:
    """Copy a cellar wine's stored references onto freshly-read label data.

    What we already looked up and kept beats what the vision call guessed off
    the label, and it is free.
    """
    for field in REFERENCE_FIELDS:
        value = wine.get(field)
        if value is not None:
            data[field] = value


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
    tag = _as_money(data.get("list_price"))
    if tag is not None:
        text += f"🏷 Shelf: {tag:g}\n"
    text += _references_block(data)
    return text


def _category_label(score) -> str:
    """Word label for a category_rating 0-100.

    Bands recalibrated for the 70/30 critic+Vivino composite — Vivino rarely
    exceeds 4.3-4.5 so the composite sits ~5pts below a pure critic score.
    """
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ""
    if s >= 95:
        return "Outstanding"
    if s >= 85:
        return "Excellent"
    if s >= 80:
        return "Very Good"
    if s >= 75:
        return "Good"
    return "Average"


def _references_block(data: dict) -> str:
    """Render critic / Vivino / category / market price lines if any are present."""
    critic = data.get("critic_score")
    vivino = data.get("vivino_rating")
    market = data.get("market_price")
    currency = data.get("market_price_currency") or ""
    confidence = data.get("reference_confidence") or ""
    category = data.get("category_rating")
    # Fallback compute: blend if both, else use whichever is available
    if category is None:
        try:
            if critic is not None and vivino is not None:
                category = round(float(critic) * 0.7 + float(vivino) * 20.0 * 0.3)
            elif critic is not None:
                category = int(critic)
            elif vivino is not None:
                category = round(float(vivino) * 20.0)
        except (TypeError, ValueError):
            category = None

    if not any(v is not None for v in (critic, vivino, market, category)):
        return ""

    lines = ["\n📊 <b>References</b>"]
    if critic is not None:
        lines.append(f"  ⭐ Critic: {critic}/100")
    if vivino is not None:
        lines.append(f"  🍇 Vivino: {vivino}/5")
    if category is not None:
        label = _category_label(category)
        lines.append(f"  🏷 Category: {label}" if label else f"  🏷 Category: {category}/100")
    if market is not None:
        try:
            lines.append(f"  💰 Market: ~{float(market):,.0f} {currency}".rstrip())
        except (TypeError, ValueError):
            pass
    if confidence:
        lines.append(f"  <i>(confidence: {confidence})</i>")
    return "\n".join(lines) + "\n"


def empty_cellar(
    context_line: str = "",
    *,
    unconfigured: bool = False,
    storage_name: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """The one empty-state card, used everywhere a cellar or fridge is empty.

    The empty state is the highest-intent screen a new member sees, and it was
    nine separate dead ends — one of which told people to "scan a bottle first",
    pointing at hardware most of them don't have. Every one now says the same
    thing: send a photo.
    """
    text = (
        f"\U0001F377 <b>{context_line or 'Your cellar is empty'}</b>\n\n"
        "\U0001F4F8 <b>Send me a photo of a bottle</b> and I'll add it.\n\n"
        "Point your camera at the front label — I'll read the producer, grape, "
        "region and vintage, write tasting notes, and look up ratings and price."
    )
    if unconfigured:
        where = storage_name or "This cellar"
        text += (
            f"\n\n<i>{where} has no shelves yet. When you've got a fridge or "
            "rack, add it from the website and say how many drawers it has — "
            "or just send bottles and I'll keep them here until you do.</i>"
        )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2139\uFE0F How it works", callback_data=_cb("howto")),
    ]])
    return text, keyboard


def last_bottle_removed_view(
    wine_id: int, name: str, item_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Shown when fridge minus takes the last bottle — empty_cellar has no undo."""
    safe = html.escape(str(name or "that wine"), quote=False)
    text = (
        f"\U0001F4E4 Removed the last bottle of <b>{safe}</b>.\n\n"
        "Tap Undo if that was a mistake."
    )
    rows = []
    if item_id:
        rows.append([InlineKeyboardButton(
            "\u21a9\ufe0f Undo",
            callback_data=_cb("ul", wine_id=wine_id, item_id=item_id),
        )])
    return text, InlineKeyboardMarkup(rows)


def _unconfigured_storage(api) -> tuple[bool, str]:
    """True only for the auto-created empty Cellar in a household with no bottles."""
    try:
        storages = api.list_storages() or []
    except Exception:
        return False, ""
    if len(storages) != 1:
        return False, ""
    s = storages[0]
    if (s.get("name") or "") != "Cellar":
        return False, ""
    if s.get("drawers") or s.get("capacity") or s.get("bottles"):
        return False, ""
    return True, "Cellar"


RESEND_PHOTO = "Something went wrong, please resend the photo."


def _stock_failed(result) -> str | None:
    """Non-2xx / 409 body from add_stock. Callers must not say Added."""
    if not isinstance(result, dict):
        return "Couldn't add that."
    err = result.get("error")
    if not err:
        return None
    if err == "recent_duplicate":
        return "Already logged a moment ago."
    return str(err)


BUSY_TAP = "Still working on that — try again in a moment."

_MATCH_SKIP = {
    "wine", "the", "de", "du", "le", "la", "les", "di", "del", "das", "der",
    "reserve", "reserva", "red", "white", "brut", "extra", "dry",
    "grand", "cru", "appellation", "dezaley", "calamin", "lavaux",
    "epesses", "saphorin", "saint",
}
FRONT_WINS = ("name", "producer", "vintage")
BACK_WINS = ("barcode", "abv")
CONFIRM_OVERWRITE_FIELDS = {"producer", "vintage", "barcode", "name"}


def _fold_accents(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def _name_words(text: str) -> set[str]:
    folded = _fold_accents(text).lower()
    words = {
        w for w in re.findall(r"[a-z0-9]+", folded)
        if w not in _MATCH_SKIP and len(w) > 2
    }
    for init in re.findall(r"\b[a-z](?:\.[a-z0-9]+)+\.?", folded):
        collapsed = init.replace(".", "")
        if len(collapsed) >= 2:
            words.add(collapsed)
    return words


_INITIALS_TOKEN = re.compile(r"^[A-Za-z](?:\.[A-Za-z0-9]+){1,}\.?$")
_GRAPE_WORDS = {
    "grenache", "noir", "blanc", "chardonnay", "cabernet", "sauvignon",
    "merlot", "syrah", "shiraz", "pinot", "grigio", "gris", "chenin",
    "chasselas", "cinsault", "cinsaut", "malbec", "riesling", "viognier",
    "tempranillo", "sangiovese", "nebbiolo", "barbera", "zinfandel",
    "mourvedre", "carignan", "colombard", "semillon", "muscat",
    "gewurztraminer", "albarino", "vermentino", "red", "white", "rose",
    "blend",
}


def _suggested_producer(name: str) -> str:
    """Guess a producer from a Grok name like 'G.W.M Grenache Noir'."""
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return ""
    first = parts[0]
    if _INITIALS_TOKEN.match(first) and len(parts) >= 2:
        return first
    grape_tail = 0
    for word in reversed(parts):
        folded = _fold_accents(word).lower().strip(".,")
        if folded in _GRAPE_WORDS or folded in _MATCH_SKIP:
            grape_tail += 1
        else:
            break
    if grape_tail and len(parts) > grape_tail:
        producer = " ".join(parts[:-grape_tail]).strip()
        if 1 <= len(producer.split()) <= 4:
            return producer
    return ""


def _plausible_producer(text: str) -> bool:
    """Reject questions and long sentences so they fall through to search."""
    raw = (text or "").strip()
    if len(raw) < 2 or raw.startswith("/") or "?" in raw:
        return False
    if TYPED_ADD_RE.match(raw) or parse_typed_stock(raw):
        return False
    return len(raw.split()) < 5


def _who_makes_keyboard(barcode: str, guess: str = "") -> InlineKeyboardMarkup:
    rows = []
    if guess:
        rows.append([InlineKeyboardButton(
            f"Producer: {guess} \u2705",
            callback_data=_cb("gp", barcode=barcode),
        )])
    rows.append([InlineKeyboardButton(
        ADD_WITHOUT_PRODUCER, callback_data=_cb("np", barcode=barcode),
    )])
    rows.append([InlineKeyboardButton("\u274c Cancel", callback_data=_cb("x"))])
    return InlineKeyboardMarkup(rows)


PRODUCER_HINT = (
    "\n\n<b>Who makes this?</b> Type the producer, or add without one."
)


def _guess_producer_row(barcode: str, name: str) -> list:
    guess = _suggested_producer(name or "")
    if not guess:
        return []
    return [[InlineKeyboardButton(
        f"Producer: {guess} \u2705",
        callback_data=_cb("gp", barcode=barcode),
    )]]


DIFFERENT_BOTTLE = (
    "This looks like a different bottle — new wine or attach anyway?"
)


def _different_bottle_keyboard(barcode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "New wine", callback_data=_cb("nd", barcode=barcode),
        )],
        [InlineKeyboardButton(
            "Attach anyway", callback_data=_cb("yb", barcode=barcode),
        )],
    ])


def _clearly_different_label(first: dict, second: dict) -> bool:
    """True when two Grok reads name two different wines, not a back-label."""
    a = _name_words(first.get("name") or "")
    b = _name_words(second.get("name") or "")
    if not a or not b or (a & b):
        return False
    return True


def _as_edit_query(update):
    """Let typed flows reuse _safe_edit / _prompt_where_to_store."""
    class _Q:
        def __init__(self):
            self.message = update.message

        async def answer(self):
            return None

        async def edit_message_text(self, *args, **kwargs):
            return await update.message.reply_text(*args, **kwargs)

    return _Q()


def _is_classification_name(name: str) -> bool:
    """True when Grok only read a style word (Reserva, Red, Brut…)."""
    raw = (name or "").strip()
    if not raw:
        return True
    return not _name_words(raw)


def _region_of(wine: dict) -> str:
    return _fold_accents(
        str(wine.get("region_name") or wine.get("region") or "")
    ).lower().strip()


def _country_of(value) -> str:
    return _fold_accents(str(value or "")).lower().strip()


def _attrs_match(query: dict, wine: dict) -> int | None:
    """Score a vintage/country/region/type match. None if they disagree."""
    score = 0
    q_vin, w_vin = query.get("vintage"), wine.get("vintage")
    if q_vin not in (None, "") and w_vin not in (None, ""):
        if str(q_vin) != str(w_vin):
            return None
        score += 2
    q_cty, w_cty = _country_of(query.get("country")), _country_of(wine.get("country"))
    if q_cty and w_cty:
        if q_cty != w_cty and q_cty not in w_cty and w_cty not in q_cty:
            return None
        score += 1
    q_reg, w_reg = _region_of(query), _region_of(wine)
    if q_reg and w_reg:
        if q_reg not in w_reg and w_reg not in q_reg:
            return None
        score += 2
    elif q_reg and not w_reg:
        return None
    q_type = str(query.get("wine_type") or "").upper()
    w_type = str(wine.get("wine_type") or "").upper()
    if q_type and w_type:
        if q_type != w_type:
            return None
        score += 1
    if score < 2:
        return None
    if q_vin not in (None, "") and str(q_vin) == str(w_vin):
        return score
    if q_reg and w_reg:
        return score
    return None


def match_barcode_less_wines(query: dict, wines: list[dict], limit: int = 3) -> list[dict]:
    """Fuzzy-match name+producer against household wines that have no barcode.

    Producer must agree when both sides have one. At least two meaningful
    words must overlap, and at least one of those must come from the wine
    name (not only the producer), so a whole range from one estate is not
    offered. Accents are folded so ñ matches n.

    When the scanned name is only a classification word (Reserva) or a
    producer is missing, fall back to vintage / country / region / type.
    """
    q_name = (query.get("name") or "").strip()
    q_name_words = _name_words(q_name)
    q_prod = _name_words(query.get("producer") or "")
    q_words = q_name_words | q_prod
    scored = []
    for wine in wines:
        if str(wine.get("barcode") or "").strip():
            continue
        w_name = (wine.get("name") or "").strip()
        if not w_name:
            continue
        w_name_words = _name_words(w_name)
        w_prod = _name_words(wine.get("producer") or "")
        if q_prod and w_prod and not (q_prod & w_prod):
            continue
        if q_name_words and (q_name_words & w_name_words):
            overlap = q_words & (w_name_words | w_prod)
            if len(overlap) >= 2:
                scored.append((100 + len(overlap), wine))
                continue
        if not q_name_words or not q_prod or not w_prod:
            attr = _attrs_match(query, wine)
            if attr:
                scored.append((attr, wine))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [wine for _, wine in scored[:limit]]


def match_barcode_less_wine(query: dict, wines: list[dict]) -> dict | None:
    hits = match_barcode_less_wines(query, wines, limit=1)
    return hits[0] if hits else None


def _filled(value) -> bool:
    return value not in (None, "", [], {}, 0)


def _producer_agrees(query: dict, wine: dict) -> bool:
    q, w = _name_words(query.get("producer") or ""), _name_words(wine.get("producer") or "")
    if q and w:
        return bool(q & w)
    return True


def _vintage_agrees(query: dict, wine: dict) -> bool:
    q, w = query.get("vintage"), wine.get("vintage")
    if q not in (None, "") and w not in (None, ""):
        return str(q) == str(w)
    return True


def _can_update_wine(query: dict, wine: dict) -> bool:
    """Update when producer agrees, and vintage agrees if both sides have one."""
    q_prod, w_prod = _name_words(query.get("producer") or ""), _name_words(
        wine.get("producer") or "",
    )
    q_vin, w_vin = query.get("vintage"), wine.get("vintage")
    if not (q_prod and w_prod and (q_prod & w_prod)):
        return False
    if q_vin not in (None, "") and w_vin not in (None, "") and str(q_vin) != str(w_vin):
        return False
    q_name, w_name = _name_words(query.get("name") or ""), _name_words(
        wine.get("name") or "",
    )
    if q_name and w_name and not (q_name & w_name):
        return False
    return True


def find_existing_wines(query: dict, wines: list[dict], limit: int = 3) -> list[dict]:
    """Household wines that may already be this bottle.

    Producer must agree when both sides have one, vintage when both have one.
    Appellation words (Dezaley, Grand Cru, …) do not count toward name overlap.
    """
    q_name_words = _name_words(query.get("name") or "")
    q_prod = _name_words(query.get("producer") or "")
    scored = []
    for wine in wines:
        w_name_words = _name_words(wine.get("name") or "")
        w_prod = _name_words(wine.get("producer") or "")
        if not _producer_agrees(query, wine) or not _vintage_agrees(query, wine):
            continue
        name_overlap = q_name_words & w_name_words
        prod_overlap = q_prod & w_prod
        if not name_overlap and len(prod_overlap) < 2:
            continue
        score = len(name_overlap) * 2 + len(prod_overlap)
        if _can_update_wine(query, wine):
            score += 10
        scored.append((score, wine))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [wine for _, wine in scored[:limit]]


def wines_to_link(query: dict, wines: list[dict], limit: int = 3) -> list[dict]:
    """Barcode-less cellar wines this scan may already be.

    Same matcher as shop-label photos. Wines that already have a barcode are
    skipped — linking another code onto them is refused later.
    """
    barcode_less = [
        wine for wine in wines
        if not str(wine.get("barcode") or "").strip()
    ]
    return find_existing_wines(query, barcode_less, limit=limit)


def _wine_choice_label(wine: dict, limit: int = 50) -> str:
    name = str(wine.get("name") or "that wine")
    producer = str(wine.get("producer") or "")
    vintage = str(wine.get("vintage") or "")
    tail = " · ".join(part for part in (producer, vintage) if part)
    if not tail:
        return name[:limit]
    if len(name) + 3 + len(tail) <= limit:
        return f"{name} · {tail}"
    room = max(8, limit - len(tail) - 3)
    return f"{name[:room].rstrip()} · {tail}"[:limit]


def merge_front_back(front: dict | None, back: dict | None) -> dict:
    """Combine two label reads. Front wins name/producer/vintage; back wins barcode/ABV."""
    front = dict(front or {})
    back = dict(back or {})
    merged = {}
    for key in list(dict.fromkeys([*front, *back])):
        fv, bv = front.get(key), back.get(key)
        if key in FRONT_WINS:
            merged[key] = fv if _filled(fv) else bv
        elif key in BACK_WINS:
            merged[key] = bv if _filled(bv) else fv
        else:
            merged[key] = fv if _filled(fv) else bv
    return {k: v for k, v in merged.items() if _filled(v) or k in ("type",)}


def proposed_wine_update(current: dict, incoming: dict) -> tuple[dict, list[tuple[str, object, object]]]:
    """Split a Grok patch into blank-fills vs overwrites of filled fields."""
    fills: dict = {}
    overwrites: list[tuple[str, object, object]] = []
    current = current or {}
    for field, new in (incoming or {}).items():
        if new in (None, "", [], {}):
            continue
        old = current.get("comment") if field == "description" else current.get(field)
        if not _filled(old):
            fills[field] = new
            continue
        if str(old).strip() == str(new).strip():
            continue
        if field in CONFIRM_OVERWRITE_FIELDS:
            overwrites.append((field, old, new))
    return fills, overwrites


def _another_photo_row(barcode: str, pending: dict | None = None) -> list:
    if pending and pending.get("photo_back"):
        return []
    return [[InlineKeyboardButton(
        ADD_ANOTHER_PHOTO, callback_data=_cb("ap", barcode=barcode),
    )]]


def _is_this_keyboard(barcode: str, wines: list[dict], *, new_action: str = "grok"):
    rows = []
    label_card = str(barcode).startswith("_")
    verb = "Yes, that's" if label_card else "Yes, link"
    for wine in wines[:3]:
        name = (wine.get("name") or "that wine")[:32]
        rows.append([InlineKeyboardButton(
            f"{verb} {name}",
            callback_data=_cb("lnk", wine_id=wine["id"], barcode=barcode),
        )])
    if new_action == "nn":
        rows.append([InlineKeyboardButton(
            "No, new wine", callback_data=_cb("nn", barcode=barcode),
        )])
    else:
        rows.append([InlineKeyboardButton(
            "No, new wine",
            callback_data=_cb("grok", barcode=barcode, original="new"),
        )])
    return InlineKeyboardMarkup(rows)


def _is_this_text(barcode: str, wines: list[dict], extra: str = "") -> str:
    if len(wines) == 1:
        name = html.escape(str(wines[0].get("name") or "that wine"), quote=False)
        body = f"Is this <b>{name}</b>?"
    else:
        body = "Is this one of these?"
    if str(barcode).startswith("_"):
        return f"{body}{extra}"
    code = html.escape(barcode, quote=False)
    return f"❓ Unknown barcode <code>{code}</code>\n\n{body}{extra}"


def unknown_barcode_prompt(barcode: str, off_data: dict | None = None):
    """Text + buttons for an unknown scan when we are not offering a link."""
    if off_data and off_data.get("name"):
        text = (
            f"🍷 <b>New wine scanned!</b>\n\n"
            f"OFF found: <b>{html.escape(str(off_data['name']), quote=False)}</b>\n"
            f"Producer: {html.escape(str(off_data.get('producer', 'Unknown')), quote=False)}\n"
            f"Country: {html.escape(str(off_data.get('country', 'Unknown')), quote=False)}\n"
            f"ABV: {html.escape(str(off_data.get('abv', 'Unknown')), quote=False)}\n"
            f"Barcode: <code>{html.escape(barcode, quote=False)}</code>\n\n"
            f"📸 Send a label photo to enrich with tasting notes, "
            f"pairings and more — or add with OFF data below.\n"
            f"Or type the producer, name and vintage and I'll look it up."
        )
        keyboard = [
            [InlineKeyboardButton("📸 Send label photo", callback_data=_cb("retry", barcode=barcode))],
            [InlineKeyboardButton("✅ Add with OFF data", callback_data=_cb("off", barcode=barcode))],
            [InlineKeyboardButton("🔗 Link to existing wine", callback_data=_cb("link", barcode=barcode))],
            [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
            [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
        ]
    else:
        text = (
            f"❓ <b>Unknown barcode: <code>{html.escape(barcode, quote=False)}</code></b>\n\n"
            f"Not found in Open Food Facts.\n\n"
            f"📸 Send a label photo and I'll extract the details + tasting notes.\n"
            f"Or type the producer, name and vintage and I'll look it up."
        )
        keyboard = [
            [InlineKeyboardButton("📸 Send label photo", callback_data=_cb("retry", barcode=barcode))],
            [InlineKeyboardButton("🔗 Link to existing wine", callback_data=_cb("link", barcode=barcode))],
            [InlineKeyboardButton("✏️ Add manually", callback_data=_cb("man", barcode=barcode))],
            [InlineKeyboardButton("❌ Skip", callback_data=_cb("skip", barcode=barcode))],
        ]
    return text, InlineKeyboardMarkup(keyboard)


def _not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


async def _safe_edit(query, *args, **kwargs):
    """Redraw a callback message. A double-tap sends the same content again,
    and Telegram rejects that with BadRequest 'Message is not modified'.
    Ignore that, and always answer so the client spinner cannot stick.
    """
    try:
        await query.answer()
    except Exception:
        pass
    try:
        return await query.edit_message_text(*args, **kwargs)
    except BadRequest as e:
        if _not_modified(e):
            logger.info("[tap] edit unchanged")
            return None
        raise


def _retry_add_markup(wine_id, *, drank: bool = False, barcode: str = "") -> InlineKeyboardMarkup:
    if drank:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Try again", callback_data=_cb("dk", wine_id=wine_id, barcode=barcode),
            ),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Try adding again", callback_data=_cb("in", wine_id=wine_id),
        ),
    ]])


def _duplicate_guard_markup(wine_id, item_id, *, drank: bool) -> InlineKeyboardMarkup:
    add_action = "la" if drank else "aa"
    label = "Log another bottle?" if drank else "Add another bottle?"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            label, callback_data=_cb(add_action, wine_id=wine_id),
        )],
        [InlineKeyboardButton(
            "That's the one",
            callback_data=_cb("ok", wine_id=wine_id, item_id=item_id),
        )],
    ])


def build_cellar_summary(wines: list[dict], limit: int = 40) -> str:
    """A compact description of someone's taste, for the wine-list prompt.

    Counts rather than a wine list: what they actually buy says more about what
    they'd enjoy than any single bottle, and it keeps the prompt short.
    """
    if not wines:
        return ""

    def tally(field):
        counts = {}
        for w in wines:
            value = (w.get(field) or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    lines = []

    types = tally("wine_type")
    if types:
        named = [f"{WINE_TYPES.get(code, code)} ({n})" for code, n in types[:5]]
        lines.append(f"Styles: {', '.join(named)}")

    for field, label in (("grape", "Grapes"), ("country", "Countries"),
                         ("producer", "Producers")):
        counts = tally(field)
        if counts:
            named = [f"{value} ({n})" if n > 1 else value for value, n in counts[:6]]
            lines.append(f"{label}: {', '.join(named)}")

    # Naming actual bottles lets the model spot a wine they already own.
    names = [w.get("name", "") for w in wines[:limit] if w.get("name")]
    if names:
        lines.append(f"Bottles at home: {'; '.join(names)}")

    return "\n".join(lines)


def _wine_label(w: dict) -> str:
    """Name plus vintage, without doubling it up.

    Wine-list entries are usually printed "Producer Cuvee 2022", so appending
    the parsed vintage again gives "... 2022 2022".
    """
    name = (w.get("name") or "").strip()
    vintage = w.get("vintage")
    if vintage and str(vintage) not in name:
        return f"{name} {vintage}"
    return name


# Grape and style words describe a wine rather than identify it. Left in, any
# two South African Chenins matched each other, because "chenin" + "blanc" is
# two shared words on its own.
_DESCRIPTIVE_WORDS = {
    "chenin", "blanc", "blancs", "noir", "noirs", "gris", "grigio",
    "cabernet", "sauvignon", "merlot", "shiraz", "syrah", "pinot",
    "chardonnay", "riesling", "malbec", "tempranillo", "sangiovese",
    "nebbiolo", "zinfandel", "viognier", "semillon", "mourvedre",
    "grenache", "carignan", "cinsault", "pinotage", "verdelho", "muscat",
    "rose", "rosé", "brut", "blend", "cuvee", "cuvée", "red", "white",
    "sparkling", "dry", "sweet", "organic", "old", "vine", "vines",
}


def _norm_words(text: str) -> set:
    """Significant lowercase words, for loose title matching."""
    skip = {"the", "de", "du", "le", "la", "les", "di", "del", "das", "der",
            "wine", "wines", "estate", "reserve", "reserva", "vineyards",
            "cellar", "cellars", "domaine", "chateau", "vintage", "nv"}
    skip |= _DESCRIPTIVE_WORDS
    return {
        w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split()
        if w not in skip and len(w) > 2 and not w.isdigit()
    }


def mark_owned_wines(data: dict, cellar: list[dict]) -> int:
    """Flag list entries already in the cellar, locally rather than via the model.

    Asking the model to spot these meant relying on whichever slice of the
    cellar fitted in the prompt — and it missed a bottle that was plainly
    there. Matching here is deterministic and sees everything.
    """
    if not cellar:
        return 0

    owned = []
    for w in cellar:
        words = _norm_words(f"{w.get('name', '')} {w.get('producer', '')}")
        if words:
            owned.append(words)

    hits = 0
    for entry in data.get("wines") or []:
        entry_words = _norm_words(f"{entry.get('name', '')} {entry.get('producer', '')}")
        if not entry_words:
            continue
        # Two shared significant words is a confident match ("ken forrester",
        # "alheit cartology") without firing on a lone shared grape name.
        if any(len(entry_words & mine) >= 2 for mine in owned):
            entry["already_owned"] = True
            hits += 1
        else:
            entry.setdefault("already_owned", False)
    return hits


WINELIST_SCENES = ("menu", "shelf", "bottles")


def _scene(data: dict) -> str:
    """menu / shelf / bottles. Unknown values fall back to menu."""
    scene = (data.get("scene") or "menu").lower()
    return scene if scene in WINELIST_SCENES else "menu"


def _price_str(list_price, retail_price, currency: str, scene: str = "menu",
               retail_currency: str | None = None) -> str:
    """Render prices that belong to this scene.

    list_price is an offered price printed in the photo (menu or shelf tag),
    in the photo's own currency. retail_price is an estimate in the user's
    currency, or a stored market price in whatever it was found in — so it
    carries its own glyph rather than borrowing the menu's.

    Bottle-group photos have no such price, so we never label one "on the
    list" even if the model still emits a number.
    """
    parts = []
    if scene in ("menu", "shelf") and isinstance(list_price, (int, float)):
        label = "on the shelf" if scene == "shelf" else "on the list"
        parts.append(f"~{currency}{list_price:,.0f} {label}")
    if isinstance(retail_price, (int, float)):
        glyph = currency if retail_currency is None else retail_currency
        parts.append(f"~{glyph}{retail_price:,.0f} retail")
    return " · ".join(parts)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "\u2026"


def _winelist_title(n: int, scene: str) -> str:
    plural = "s" if n != 1 else ""
    if scene == "shelf":
        return f"\U0001F6D2 <b>Spotted {n} wine{plural} on the shelf</b>"
    if scene == "bottles":
        return f"\U0001F377 <b>Spotted {n} wine{plural}</b>"
    return f"\U0001F4CB <b>Read {n} wine{plural}</b>"


def _winelist_index(wines: list, wine: dict | None) -> int | None:
    if not wine:
        return None
    name = (wine.get("name") or "").lower()
    for i, other in enumerate(wines):
        if (other.get("name") or "").lower() == name:
            return i
    return None


def _is_pick(w: dict, pick: dict | None) -> bool:
    if not pick:
        return False
    return (w.get("name") or "").lower() == (pick.get("name") or "").lower()


def _wine_action_row(idx: int, w: dict, *, pick: dict | None = None,
                     from_pick: bool = False) -> list:
    """One wine, one row: the name opens more, Wrong sits beside it.

    Telegram cannot put buttons between lines of text — they always hang
    under the whole card. A named row is the useful version of 'inline'.
    """
    star = "\u2B50 " if _is_pick(w, pick) else ""
    owned = " \U0001F3E0" if w.get("already_owned") else ""
    more = f"{star}{idx}. {_clip(_wine_label(w), 28)}{owned}"
    origin = "p" if from_pick else "l"
    return [
        InlineKeyboardButton(
            more, callback_data=_cb("wli", page=idx - 1, original=origin),
        ),
        InlineKeyboardButton(
            "Wrong", callback_data=_cb("wlf", page=idx - 1),
        ),
    ]


CURRENCY_GLYPH = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac", "ZAR": "R",
                  "AUD": "A$", "HKD": "HK$"}


def _glyph(code) -> str:
    if not code:
        return ""
    return CURRENCY_GLYPH.get(code, f"{code} ")


def estimated_cellar_value(wines) -> str:
    """Market-price estimate. Paid shelf prices are a different number."""
    totals: dict[str, float] = {}
    for wine in wines:
        price = wine.get("market_price")
        currency = wine.get("market_price_currency")
        count = wine.get("stock_count") or 0
        if price is None or not currency or not count:
            continue
        totals[currency] = totals.get(currency, 0) + float(price) * count
    if not totals:
        return ""
    parts = []
    for code, amount in totals.items():
        shown = str(int(amount)) if float(amount).is_integer() else f"{amount:.2f}"
        parts.append(f"{_glyph(code)}{shown}")
    joined = " · ".join(parts)
    return f"Estimated cellar value: {joined} (market prices, not what you paid)"


def _retail_glyph(data: dict, wine: dict) -> str | None:
    """The row's own retail currency, else the list's; None = the list glyph."""
    code = wine.get("retail_currency") or data.get("retail_currency")
    return _glyph(code) if code else None


def build_winelist_view(data: dict, page: int = 0,
                        per_page: int = WINELIST_PER_PAGE,
                        show_all: bool = False,
                        detail_index: int | None = None,
                        from_pick: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Render a multi-wine photo: the pick in prose, the wines as named rows.

    Each wine is a keyboard row — name opens more, Wrong sits beside it —
    because Telegram cannot place buttons between lines of text.
    """
    wines = data.get("wines") or []
    currency = CURRENCY_GLYPH.get(data.get("currency", ""), data.get("currency", ""))
    scene = _scene(data)

    if not wines:
        return (
            "\U0001F4CB I couldn't read any wines off that.\n\n"
            "Try a straighter shot with the names in focus — or send one bottle "
            "at a time and I'll go into more detail.",
            InlineKeyboardMarkup([]),
        )

    if detail_index is not None:
        return _build_winelist_detail(
            data, wines, detail_index, currency, scene,
            per_page=per_page, from_pick=from_pick,
        )

    by_name = {(w.get("name") or "").lower(): w for w in wines}
    pick = data.get("recommendation") or {}
    chosen = by_name.get((pick.get("name") or "").lower())
    compact = len(wines) <= per_page

    if not show_all:
        return _build_winelist_pick(
            data, wines, chosen, pick, currency, scene,
            per_page=per_page, compact=compact,
        )

    return _build_winelist_page(
        data, wines, chosen, currency, scene, page, per_page,
    )


def _build_winelist_pick(data, wines, chosen, pick, currency, scene,
                         per_page, compact) -> tuple[str, InlineKeyboardMarkup]:
    lines = [_winelist_title(len(wines), scene)]
    if chosen:
        emoji = WINE_TYPE_EMOJI.get(chosen.get("wine_type", ""), "\U0001F377")
        lines.append(f"\n\u2B50 <b>I'd go for: {_wine_label(chosen)}</b>")
        if pick.get("why"):
            lines.append(f"   <i>{pick['why']}</i>")
        blurb = (chosen.get("detail") or chosen.get("note") or "").strip()
        if blurb:
            lines.append(f"   {emoji} {_clip(blurb, 220)}")
        price = _price_str(
            chosen.get("list_price"), chosen.get("retail_price"), currency, scene,
            _retail_glyph(data, chosen),
        )
        if price:
            lines.append(f"   \U0001F4B7 {price}")

    alternates = [
        next((w for w in wines if (w.get("name") or "").lower() == (n or "").lower()), None)
        for n in (data.get("alternates") or [])
    ]
    alternates = [w for w in alternates if w and w is not chosen][:3]
    if alternates:
        lines.append("\n<b>Also worth it:</b>")
        for w in alternates:
            emoji = WINE_TYPE_EMOJI.get(w.get("wine_type", ""), "\U0001F377")
            price = _price_str(
                w.get("list_price"), w.get("retail_price"), currency, scene,
                _retail_glyph(data, w),
            )
            tail = f" · {price}" if price else ""
            note = _clip(w.get("note") or "", 90)
            dash = f" — {note}" if note else ""
            lines.append(f"{emoji} <b>{_wine_label(w)}</b>{dash}{tail}")

    owned = [w.get("name") for w in wines if w.get("already_owned")]
    if owned:
        lines.append(
            f"\n\U0001F3E0 You already own {', '.join(owned[:3])} — "
            f"maybe branch out."
        )

    if compact:
        lines.append("")
        for i, w in enumerate(wines, start=1):
            star = " \u2B50" if _is_pick(w, chosen) else ""
            lines.append(f"{i}. {_wine_label(w)}{star}")

    keyboard = []
    if compact:
        keyboard.extend(
            _wine_action_row(i, w, pick=chosen, from_pick=True)
            for i, w in enumerate(wines, start=1)
        )
    else:
        pick_idx = _winelist_index(wines, chosen)
        if pick_idx is not None:
            keyboard.append(_wine_action_row(
                pick_idx + 1, chosen, pick=chosen, from_pick=True,
            ))
        keyboard.append([InlineKeyboardButton(
            f"\U0001F4CB Show all {len(wines)}", callback_data=_cb("wl", page=0),
        )])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _build_winelist_page(data, wines, chosen, currency, scene, page,
                         per_page) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, (len(wines) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    slice_wines = wines[start:start + per_page]
    lines = [_winelist_title(len(wines), scene)]

    keyboard = [
        _wine_action_row(start + i + 1, w, pick=chosen, from_pick=False)
        for i, w in enumerate(slice_wines)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u2B05\uFE0F", callback_data=_cb("wl", page=page - 1)))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("x")))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("\u27A1\uFE0F", callback_data=_cb("wl", page=page + 1)))
    if len(nav) > 1:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "\u2B50 Back to the pick", callback_data=_cb("wlp"),
    )])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _build_winelist_detail(data, wines, index, currency, scene,
                           per_page, from_pick) -> tuple[str, InlineKeyboardMarkup]:
    if not (0 <= index < len(wines)):
        return build_winelist_view(data, show_all=not from_pick, from_pick=from_pick)
    w = wines[index]
    emoji = WINE_TYPE_EMOJI.get(w.get("wine_type", ""), "\U0001F377")
    lines = [
        _winelist_title(len(wines), scene),
        f"\n{emoji} <b>{index + 1}. {_wine_label(w)}</b>",
    ]
    bits = []
    producer = (w.get("producer") or "").strip()
    if producer and producer.lower() not in (_wine_label(w) or "").lower():
        bits.append(producer)
    wt = WINE_TYPES.get(w.get("wine_type", ""), "")
    if wt:
        bits.append(wt)
    if bits:
        lines.append(" · ".join(bits))
    body = (w.get("detail") or w.get("note") or "").strip()
    if body:
        lines.append(f"\n{body}")
    rec = data.get("recommendation") or {}
    if rec.get("why") and (rec.get("name") or "").lower() == (w.get("name") or "").lower():
        lines.append(f"\n\u2B50 <i>{rec['why']}</i>")
    price = _price_str(
        w.get("list_price"), w.get("retail_price"), currency, scene,
        _retail_glyph(data, w),
    )
    if price:
        lines.append(f"\n\U0001F4B7 {price}")
    if w.get("already_owned"):
        lines.append("\n\U0001F3E0 Already in the cellar.")
    lines.append("\n<i>Wrong bottle? Tell me what it actually is.</i>")

    back = (
        _cb("wlp") if from_pick
        else _cb("wl", page=index // per_page)
    )
    keyboard = [[
        InlineKeyboardButton("Wrong", callback_data=_cb("wlf", page=index)),
        InlineKeyboardButton("\u2190 Back", callback_data=back),
    ]]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def list_entry_from_label(data: dict, currency: str = "") -> dict:
    """Turn a single-label Grok result into a wine-list row.

    `currency` is the user's — what an estimated retail_price is quoted in.
    A market_price keeps the currency it was found in.
    """
    raw = (data.get("description") or data.get("detail") or data.get("note") or "").strip()
    note = (data.get("note") or "").strip() or (raw.split(". ")[0] if raw else "")
    detail = (data.get("detail") or raw or note)
    retail = data.get("market_price")
    retail_currency = data.get("market_price_currency") or currency
    if not isinstance(retail, (int, float)):
        retail = data.get("retail_price")
        retail_currency = currency
    return {
        "name": data.get("name") or "Unknown",
        "producer": data.get("producer") or "",
        "vintage": data.get("vintage"),
        "wine_type": data.get("wine_type") or "",
        "note": _clip(note, 110),
        "detail": _clip(detail, 400),
        "list_price": _as_money(data.get("list_price")),
        "retail_price": retail if isinstance(retail, (int, float)) else None,
        "retail_currency": retail_currency or None,
        "already_owned": False,
    }


def list_entry_from_cellar(wine: dict) -> dict:
    raw = (wine.get("comment") or "").strip()
    note = raw.split(". ")[0] if raw else ""
    return {
        "name": wine.get("name") or "Unknown",
        "producer": wine.get("producer") or "",
        "vintage": wine.get("vintage"),
        "wine_type": wine.get("wine_type") or "",
        "note": _clip(note, 110),
        "detail": _clip(raw, 400),
        "list_price": None,
        "retail_price": wine.get("market_price") if isinstance(wine.get("market_price"), (int, float)) else None,
        "retail_currency": wine.get("market_price_currency") or None,
        "already_owned": True,
    }


def _names_match_pick(wine: dict, rec_name: str, extra: str = "") -> bool:
    """True if the recommendation is pointing at this bottle.

    Grok's pick name and the list label often disagree about vintage, so
    matching only the correction's display label misses the retarget.
    """
    rec_name = (rec_name or "").strip().lower()
    if not rec_name:
        return False
    aliases = {
        (wine.get("name") or "").strip().lower(),
        _wine_label(wine).strip().lower(),
        (extra or "").strip().lower(),
    }
    aliases.discard("")
    if rec_name in aliases:
        return True
    return any(rec_name in alias or alias in rec_name for alias in aliases if len(alias) >= 8)


def apply_winelist_correction(data: dict, index: int, entry: dict, old_name: str = "") -> bool:
    """Replace wines[index] and retarget the recommendation if it was that bottle."""
    wines = data.get("wines") or []
    if not (0 <= index < len(wines)):
        return False
    old = wines[index]
    rec = data.get("recommendation") if isinstance(data.get("recommendation"), dict) else None
    was_pick = bool(rec) and _names_match_pick(old, rec.get("name") or "", old_name)
    wines[index] = entry
    if was_pick and rec is not None:
        rec["name"] = entry.get("name") or rec.get("name")
    return True


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
        self.last_removed: dict | None = None
        # Group-by values for the current grouped view. Buttons carry an index
        # into this list so a long producer/grape never lands in callback_data.
        self.group_values: list[tuple[str, str]] = []

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
    session.group_values = []

    for idx, (val, display, count) in enumerate(groups):
        bottles = sum(w.get("stock_count", 0) for w in wines
                      if (w.get({"type": "wine_type"}.get(dim, dim), "") or "Unknown") == val)
        lines.append(f"  {display} \u2014 {count} wines, {bottles} bottles")
        session.group_values.append((dim, val))
        keyboard.append([InlineKeyboardButton(
            f"{display} ({count})",
            callback_data=_cb("sd2", original=session_key, page=idx),
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
        callback_data=_cb("bl", original=session_key),
    )])
    if session.mode == "fridge":
        keyboard.append([InlineKeyboardButton(
            "Catch up", callback_data=_cb("cu", original="go"),
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
    total_pages = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    if session.page >= total_pages:
        session.page = total_pages - 1
    if session.page < 0:
        session.page = 0
    page = session.page
    start = page * BROWSE_PAGE_SIZE
    end = min(start + BROWSE_PAGE_SIZE, total)
    page_wines = wines[start:end]

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

    removed = getattr(session, "last_removed", None)
    if removed and removed.get("wine_id") and removed.get("item_id"):
        name = str(removed.get("name") or "last bottle")
        label = f"\u21a9\ufe0f Undo {name}"
        if len(label) > 34:
            label = "\u21a9\ufe0f Undo last bottle"
        keyboard.append([InlineKeyboardButton(
            label,
            callback_data=_cb(
                "ul", wine_id=removed["wine_id"], item_id=removed["item_id"],
            ),
        )])

    # Pagination
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev", callback_data=_cb("bp", original=f"{session_key}:{page - 1}")))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("x")))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next \u27a1\ufe0f", callback_data=_cb("bp", original=f"{session_key}:{page + 1}")))
        keyboard.append(nav)

    # Back button
    if session.filters:
        keyboard.append([InlineKeyboardButton(
            "\u2b05\ufe0f Back",
            callback_data=_cb("sb", original=session_key),
        )])
    if session.mode == "fridge":
        keyboard.append([InlineKeyboardButton(
            "Catch up", callback_data=_cb("cu", original="go"),
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
        self.chat_ids = [str(c) for c in chat_ids]
        # Dedicated event loop + thread for sending, avoids conflicts with uvicorn's loop.
        # The loop is closed on this same thread after run_forever returns.
        self._loop = asyncio.new_event_loop()
        loop = self._loop

        def _run_forever():
            asyncio.set_event_loop(loop)
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=_run_forever, daemon=True, name="telegram-notifier")
        self._thread.start()

    def close(self) -> None:
        """Stop the dedicated send loop so this instance cannot leak a thread."""
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        self._loop = None
        self._thread = None
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _run(self, coro):
        loop = self._loop
        if loop is None:
            raise RuntimeError("TelegramNotifier is closed")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
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
                                       barcode: str, action: str, stock_count: int,
                                       storage: str = "", item_id: int | None = None,
                                       drawers: int = 0, row: int | None = None,
                                       storage_id: int | None = None):
        """Stock ping after a fridge scan. Location is the default; buttons fix it."""
        text, reply_markup = build_stock_update_view(
            action, wine_name, stock_count,
            wine_id=wine_id, barcode=barcode, storage=storage,
            item_id=item_id, drawers=drawers, row=row, storage_id=storage_id,
        )
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

    def send_new_wine_prompt(self, barcode: str, off_data: dict | None = None,
                             suggested=None):
        """Prompt user about a new unknown barcode — always ask for a label photo."""
        hits = suggested if isinstance(suggested, list) else (
            [suggested] if isinstance(suggested, dict) and suggested.get("id") else []
        )
        hits = [w for w in hits if isinstance(w, dict) and w.get("id")][:3]
        if hits:
            extra = ""
            if off_data and off_data.get("name"):
                extra = (
                    f"\nOFF read it as {html.escape(str(off_data['name']), quote=False)}."
                )
            text = _is_this_text(barcode, hits, extra)
            reply_markup = _is_this_keyboard(barcode, hits, new_action="nn")
        else:
            text, reply_markup = unknown_barcode_prompt(barcode, off_data)
        self._send_to_all(lambda cid: self.bot.send_message(
            chat_id=cid, text=text, reply_markup=reply_markup, parse_mode="HTML",
        ))

    def send_error(self, message: str):
        self.send(f"⚠️ {message}")

    def send_achievements(self, achievements: list | None):
        if not format_achievements(achievements):
            return
        self._send_to_all(
            lambda cid: send_achievement_card(self.bot, cid, achievements)
        )


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
        admin_chat_ids: list[str] | None = None,
        admin_api=None,
        card_store=None,
        ask_store=None,
    ):
        self.bot_token = bot_token
        self.chat_ids = [str(cid) for cid in chat_ids]
        self.admin_chat_ids = [str(cid) for cid in (admin_chat_ids or [])]
        self.api = wine_cellar_api  # fallback/default API client
        self.admin_api = admin_api  # staff token — invite preview and redeem only
        self._chat_id_to_api = chat_id_to_api or {}  # per-user household routing
        self.pending_store = pending_store
        self._lock = pending_lock or threading.Lock()
        self._app = None
        self._user_filter = None
        self._admin_filter = None
        self._photo_callback = None
        self._api_factory = None
        self._triage_callback = None
        self._winelist_callback = None
        self._identify_callback = None
        self._label_analyse = None
        self._local_barcode_reader = None
        self._browse_sessions: dict[str, BrowseSession] = {}
        self._chat_id_to_currency: dict[str, str] = {}
        self._chat_id_to_country: dict[str, str] = {}
        self._chat_id_to_language: dict[str, str] = {}
        self.flags: dict[str, bool] = {}
        self._enriching_wine_ids: set[int] = set()
        self._label_seq = 0
        self._winelists: dict[str, dict] = {}
        self._winelist_corrections: dict[str, dict] = {}
        # Label cards awaiting (or done with) a background reference lookup,
        # keyed chat:message. Redis-backed when REDIS_URL is set so a restart
        # does not kill open cards.
        self._label_cards = card_store if card_store is not None else CardStore()
        self._asks = ask_store if ask_store is not None else CardStore(ttl=1800, prefix="whiney:ask:")
        self._chat = None
        self._router = None
        # Shelf-tag paid prices waiting for add_stock (create then storage picker).
        self._pending_paid: dict[int, float] = {}
        self._misread_corrections: dict[str, dict] = {}
        self.bot_username: str | None = None
        self._first_cellars: set[str] = set()
        self._country_asked: set[str] = set()
        self._await_producer: dict[str, dict] = {}
        self._await_side_photo: dict[str, dict] = {}
        self._pending_side_bytes: dict[str, bytes] = {}
        self._invites = None

    def set_chat_models(self, chat):
        """Turn plain text into cellar answers and recommendations.

        Left unset in tests and when there is no Grok key, so a typed wine
        name keeps going straight to search.
        """
        from integrations.chat_router import ChatRouter
        self._chat = chat
        self._router = ChatRouter(self, chat)

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

        api = self._chat_id_to_api.get(chat_id)
        if api is not None:
            return api
        # No mapping. self.api is just "whichever client was built first", so
        # falling back to it would drop this user into an arbitrary household.
        # Only safe when there is exactly one client to begin with.
        if len(self._chat_id_to_api) <= 1 and self.api is not None:
            return self.api
        logger.warning(f"[auth] no API client mapped for chat_id={chat_id}; refusing")
        raise NotProvisionedError()

    def set_photo_callback(self, callback: Callable):
        """Set callback for when a label photo is received. Called with (barcode, photo_bytes).

        Should return a dict of extracted wine data (from Grok), or None on failure.
        """
        self._photo_callback = callback

    def set_winelist_callback(self, callback: Callable):
        """Set the callback that reads a wine list photo.
        Called with (photo_bytes, cellar_summary, currency, country)."""
        self._winelist_callback = callback

    def set_identify_callback(self, callback: Callable):
        """Set the callback that identifies one wine from a typed name.
        Called with (hint, wrong_name, currency, country)."""
        self._identify_callback = callback

    def set_label_analyse_callback(self, callback: Callable):
        """Re-read a photo that only yielded a classification word."""
        self._label_analyse = callback

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

    def set_bot_username(self, username: str | None):
        """Telegram username from getMe(). Used to build t.me invite links."""
        if username:
            self.bot_username = username.lstrip("@")

    def _invite_flow(self):
        flow = getattr(self, "_invites", None)
        if flow is None:
            from integrations.telegram_invites import InviteFlow
            flow = InviteFlow(self)
            self._invites = flow
        return flow

    def _catchup_flow(self):
        flow = getattr(self, "_catchups", None)
        if flow is None:
            from integrations.telegram_catchup import CatchUpFlow
            flow = CatchUpFlow(self)
            self._catchups = flow
        return flow

    def _list_flow(self):
        flow = getattr(self, "_lists", None)
        if flow is None:
            from integrations.telegram_lists import ListFlow
            flow = ListFlow(self)
            self._lists = flow
        return flow

    def _club_flow(self):
        flow = getattr(self, "_clubs", None)
        if flow is None:
            from integrations.telegram_clubs import ClubFlow
            flow = ClubFlow(self)
            self._clubs = flow
        return flow

    def set_flags(self, config: dict | None):
        flags = {}
        for key, value in (config or {}).items():
            if str(key).startswith("feature_"):
                flags[str(key)] = str(value or "").strip().lower() in {
                    "on", "true", "1", "yes",
                }
        self.flags = flags

    def flag_on(self, key: str) -> bool:
        return bool((getattr(self, "flags", None) or {}).get(key))

    def set_language(self, chat_id: str, language: str):
        if chat_id:
            self._chat_id_to_language[str(chat_id)] = language or ""

    def locale_for(self, update=None, chat_id=None, language_code=None) -> str:
        from integrations.copy import locale_from_telegram, normalize_locale

        cid = str(chat_id or "")
        if update is not None:
            chat = getattr(update, "effective_chat", None)
            if not cid:
                cid = str(getattr(chat, "id", "") or "")
            user = getattr(update, "effective_user", None)
            if language_code is None:
                language_code = (
                    getattr(user, "language_code", None) if user is not None else None
                )
        stored = (self._chat_id_to_language or {}).get(cid)
        if stored:
            return normalize_locale(stored)
        return locale_from_telegram(language_code)

    def set_api_factory(self, factory: Callable):
        """Set a factory function to build new WineCellarAPI clients.
        Called with (api_token, household_id) -> WineCellarAPI.
        """
        self._api_factory = factory

    def set_currency(self, chat_id: str, currency: str):
        """Register a chat_id's preferred currency (used for Grok price estimates)."""
        if chat_id and currency:
            self._chat_id_to_currency[str(chat_id)] = currency

    def set_country(self, chat_id: str, country: str, prompted: bool | None = None):
        """Register a chat_id's country (used for Grok price-context grounding)."""
        if chat_id:
            self._chat_id_to_country[str(chat_id)] = country or ""
            if prompted or country:
                self._country_asked.add(str(chat_id))

    async def _enrich_references_async(self, wine_id: int, chat_id: str, api, wine_name: str = ""):
        """Background task: run live-search refresh on a freshly-created wine.

        Fires after wine creation so the user gets immediate feedback on "Wine added"
        and a follow-up message a moment later with whatever Grok's web search found.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, functools.partial(api.refresh_references, wine_id),
            )
        except Exception as e:
            logger.warning(f"[enrich] refresh_references failed for wine #{wine_id}: {e}")
            self._enriching_wine_ids.discard(wine_id)
            return
        finally:
            # Whether or not we end up sending a follow-up, the enrich is done.
            self._enriching_wine_ids.discard(wine_id)

        if not result.get("updated"):
            # Nothing usable — silent. The user already saw "added" and the wine
            # detail card will just have no references; they can manually refresh later.
            logger.info(f"[enrich] wine #{wine_id}: live search found nothing usable")
            return

        # Build a short summary line and post it to the user
        critic = result.get("critic_score")
        vivino = result.get("vivino_rating")
        market = result.get("market_price")
        currency = result.get("market_price_currency", "")
        confidence = result.get("confidence", "")
        parts = []
        if critic is not None:
            parts.append(f"⭐ {critic}/100")
        if vivino is not None:
            parts.append(f"🍇 {vivino}/5")
        if market is not None:
            parts.append(f"💰 ~{market:.0f} {currency}".rstrip())
        if not parts:
            return
        label = wine_name or f"wine #{wine_id}"
        msg = f"📊 <b>Ratings &amp; price for {label}</b>\n  " + " · ".join(parts)
        if confidence:
            msg += f"\n  <i>(confidence: {confidence})</i>"
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"[enrich] failed to send refs message: {e}")

    def _kick_enrich(self, wine_id: int, chat_id, api, wine_name: str = ""):
        """Fire-and-forget wrapper to schedule _enrich_references_async."""
        try:
            self._enriching_wine_ids.add(wine_id)
            asyncio.create_task(
                self._enrich_references_async(wine_id, str(chat_id), api, wine_name),
            )
        except Exception as e:
            logger.warning(f"[enrich] could not schedule enrichment: {e}")
            self._enriching_wine_ids.discard(wine_id)

    def _reuse_label_lookup(self, wine_id, merged, message, api, chat_id, wine_name=""):
        """Persist the card's live search, or wait for it. Never search twice.

        Add-to-cellar almost always lands while the first lookup is still
        running. The old path kicked a second paid search and then threw the
        first result away when it arrived.
        """
        lookup = (merged or {}).get("reference_lookup") or {}
        if lookup.get("updated"):
            try:
                api.apply_references(wine_id, lookup)
            except Exception as e:
                logger.warning(
                    f"[enrich] apply_references failed for wine #{wine_id}: {e}"
                )
                self._kick_enrich(wine_id, chat_id, api, wine_name)
            return
        key = self._card_key(message) if message else ""
        card = self._label_cards.get(key) if key else None
        if card is not None and card.get("pending_lookup"):
            self._touch_label_card(message, wine_id=wine_id)
            logger.info("[enrich] wine #%s waiting for in-flight label lookup", wine_id)
            return
        self._kick_enrich(wine_id, chat_id, api, wine_name)

    def _currency_for(self, chat_id) -> str:
        """Return the currency for a given chat_id, defaulting to EUR."""
        return self._chat_id_to_currency.get(str(chat_id), "EUR")

    def _country_for(self, chat_id) -> str:
        """Return the country code for a given chat_id, defaulting to empty (no grounding)."""
        return self._chat_id_to_country.get(str(chat_id), "")

    async def _celebrate(self, context, chat_id, result: dict | None):
        """Follow-up ping when a bottle unlocked a challenge. No-op if the flag is off."""
        try:
            await send_achievement_card(
                context.bot, chat_id, (result or {}).get("achievements"),
            )
        except Exception as e:
            logger.warning(f"Could not send achievement ping: {e}")

    def add_chat_id(self, chat_id: str, api_client=None):
        """Hot-add a chat_id to the bot's allowed list (no restart needed)."""
        chat_id = str(chat_id)
        if chat_id not in self.chat_ids:
            self.chat_ids.append(chat_id)
        if self._user_filter is not None:
            self._user_filter.add_chat_ids(int(chat_id))
        if api_client is not None:
            self._chat_id_to_api[chat_id] = api_client

    def add_admin_chat_id(self, chat_id: str):
        """Hot-add a staff chat so they keep seeing the admin command set."""
        chat_id = str(chat_id)
        if chat_id not in self.admin_chat_ids:
            self.admin_chat_ids.append(chat_id)
        if getattr(self, "_admin_filter", None) is not None:
            self._admin_filter.add_chat_ids(int(chat_id))

    def remove_chat_id(self, chat_id: str):
        """Hot-remove a chat_id from the bot's allowed list."""
        chat_id = str(chat_id)
        if chat_id in self.chat_ids:
            self.chat_ids.remove(chat_id)
        if self._user_filter is not None:
            self._user_filter.remove_chat_ids(int(chat_id))
        self._chat_id_to_api.pop(chat_id, None)

    def _build_app(self) -> Application:
        admin_ids = list(self.admin_chat_ids)

        async def _post_init(application: Application) -> None:
            from integrations.telegram_admin import push_command_menus
            await push_command_menus(
                application.bot, admin_ids, flags=self.flags,
            )

        app = (
            Application.builder()
            .token(self.bot_token)
            .post_init(_post_init)
            .build()
        )

        self._user_filter = filters.Chat(chat_id=[int(cid) for cid in self.chat_ids])
        user_filter = self._user_filter

        app.add_handler(CommandHandler("status", self._cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("stock", self._cmd_stock, filters=user_filter))
        app.add_handler(CommandHandler("fridge", self._cmd_fridge, filters=user_filter))
        app.add_handler(CommandHandler("search", self._cmd_search, filters=user_filter))
        app.add_handler(CommandHandler("recent", self._cmd_recent, filters=user_filter))
        app.add_handler(CommandHandler("list", self._cmd_list, filters=user_filter))
        app.add_handler(CommandHandler("club", self._cmd_club, filters=user_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=user_filter))
        app.add_handler(CommandHandler("pending", self._cmd_pending, filters=user_filter))
        app.add_handler(CommandHandler("flush", self._cmd_flush, filters=user_filter))
        app.add_handler(CommandHandler("storages", self._cmd_storages, filters=user_filter))
        app.add_handler(CommandHandler("household", self._cmd_household, filters=user_filter))
        app.add_handler(CommandHandler("switch", self._cmd_switch, filters=user_filter))
        app.add_handler(CommandHandler("badges", self._cmd_badges, filters=user_filter))
        app.add_handler(CommandHandler("taste", self._cmd_taste, filters=user_filter))
        from integrations.telegram_admin import register_admin_handlers
        register_admin_handlers(app, self)
        # Owners add/remove in *their* cellar. Staff chats hit the admin
        # handlers above instead, which require a household name.
        owner_member_filter = user_filter
        if getattr(self, "_admin_filter", None) is not None:
            owner_member_filter = user_filter & ~self._admin_filter
        app.add_handler(CommandHandler("adduser", self._cmd_adduser, filters=owner_member_filter))
        app.add_handler(CommandHandler("removeuser", self._cmd_removeuser, filters=owner_member_filter))
        # /invite is for anyone already in a cellar, including a brand-new owner.
        # Staff /invite new is the same handler — it branches on the argument.
        app.add_handler(CommandHandler("invite", self._cmd_invite, filters=user_filter))
        # /new is the old name, kept working but out of the menu.
        app.add_handler(CommandHandler(["newcellar", "new"], self._cmd_invite_new, filters=user_filter))
        app.add_handler(
            MessageHandler(filters.COMMAND, self._abandon_invite_if_command), group=-1,
        )
        # Unfiltered: the START button sends /start, with or without an invite code.
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("myid", self._cmd_myid))  # no filter — anyone can get their ID
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        # Photos and locations from strangers are needed during naming, before
        # the chat id is on the allow list. Provisioned users still land here.
        app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo_routed))
        app.add_handler(MessageHandler(filters.LOCATION, self._handle_location))
        # Catch-all for unknown commands and plain text from authorised users
        app.add_handler(MessageHandler(filters.COMMAND & user_filter, self._unknown_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, self._unknown_text))
        # Fallbacks for unprovisioned chats. Registered last so provisioned users
        # always match one of the handlers above; only strangers reach these.
        app.add_handler(MessageHandler(filters.COMMAND, self._stranger_fallback))
        app.add_handler(MessageHandler(filters.TEXT, self._stranger_fallback))
        app.add_error_handler(self._on_error)

        return app

    async def _reject_inaccessible(self, query, context) -> bool:
        """True if this button is too old to act on, after telling the user so.

        Telegram stops sending the message body for callbacks older than ~48h
        and hands us an InaccessibleMessage instead. It is *truthy*, so every
        `if query.message` guard below sails straight past it, and the first
        attribute we touch — chat_id, text_html, caption, reply_markup — raises
        AttributeError into the error handler. That is the generic "something
        went wrong" reply users see after the bot has been up for two days.

        chat and message_id are the only attributes that survive, which is just
        enough to answer in the right chat.
        """
        msg = getattr(query, "message", None)
        if msg is not None and not isinstance(msg, InaccessibleMessage):
            return False

        try:
            await query.answer()
        except Exception:
            pass  # expected: Telegram rejects answers to callbacks this old

        chat = getattr(msg, "chat", None)
        if chat is None:
            logger.info("[callback] inaccessible message with no chat; dropping")
            return True

        logger.info(
            f"[callback] button too old to act on (chat={chat.id}, data={query.data!r})"
        )
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "\U0001F570\uFE0F That button is more than two days old, so "
                    "Telegram no longer lets me touch the message it came from.\n"
                    "Send the command again and the new buttons will work."
                ),
            )
        except Exception as e:
            logger.warning(f"[callback] could not explain stale button to {chat.id}: {e}")
        return True

    async def _abandon_invite_if_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A provisioned user running any command has left the naming prompt.

        Runs in group -1 and never stops propagation. /start <code> is exempt —
        it is the deep link resuming its own state. Strangers keep their state
        so a stray /fridge during naming still gets "Name the cellar first".
        """
        if not update.message or not self._is_known(update):
            return
        parts = (update.message.text or "").split()
        if parts and parts[0].split("@")[0] == "/start" and len(parts) > 1:
            return
        flow = self._invite_flow()
        chat_id = str(update.effective_chat.id)
        state = flow.state_for(chat_id)
        if state and state.get("step") in ("name", "confirm"):
            flow.clear(chat_id)
        club = self._club_flow()
        club_state = club.state_for(chat_id)
        if club_state and club_state.get("step") in (
            "name", "share", "compound_name", "compound_consent", "pick_hh", "join",
            "comment", "tasteoff_title", "tasteoff_label", "club_list_add",
            "tasteoff_setup",
        ):
            club.clear(chat_id)
        router = getattr(self, "_router", None)
        if router is not None:
            await router.abandon(chat_id)

    async def _stranger_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Anything at all from an unprovisioned chat gets the onboarding reply."""
        if self._is_known(update):
            return  # provisioned users are handled above; nothing to do here
        if update.message and await self._club_flow().on_text(update, context):
            return
        if update.message and await self._invite_flow().on_text(update, context):
            return
        await self._greet_stranger(update)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Last-resort handler so a failure is never silent.

        Without this, an exception anywhere in a handler is logged and the user
        sees nothing at all — the bot simply appears to ignore them. That is the
        single most common way this bot "breaks" from the outside.
        """
        err = context.error
        logger.exception(f"Unhandled error while processing update: {err}")

        if isinstance(err, NotProvisionedError):
            text = f"\U0001F6AB {err}"
        else:
            text = (
                "⚠️ Something went wrong handling that.\n"
                "If it was a button from an older message, the session has "
                "likely expired — send the command again."
            )

        chat_id = None
        if isinstance(update, Update):
            if update.effective_chat:
                chat_id = update.effective_chat.id
            if update.callback_query:
                # Clear the button's spinner even when the branch below fails.
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
        if chat_id is None:
            return
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"[error-handler] could not notify chat {chat_id}: {e}")

    # --- Commands ---

    def _is_known(self, update) -> bool:
        """True if this chat is provisioned. Cheap; safe to call before any API use."""
        chat = getattr(update, "effective_chat", None)
        return chat is not None and str(chat.id) in self.chat_ids

    async def _greet_stranger(self, update: Update):
        """Reply to someone who isn't provisioned yet.

        Telegram shows a START button on first open, which sends /start. If that
        goes unanswered the bot looks broken, and there is no way for the person
        to discover /myid. This is the entire top of the funnel, so it must
        always answer — and it must hand over the one thing an owner needs.

        It also matters that they message us first: a bot cannot open a
        conversation with a user, so the welcome in _cmd_adduser can only be
        delivered to someone who has already sent something.
        """
        await self._invite_flow()._stranger(update)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._invite_flow().start(update, context):
            return
        from integrations.telegram_invites import KNOWN_START, KNOWN_START_NO_HH, esc

        api = self._get_api(update)
        household_name = ""
        try:
            hh = api.get_household()
            if hh and hh.get("name"):
                household_name = hh["name"]
        except Exception:
            pass
        name = esc(update.effective_user.first_name or "there")
        if household_name:
            text = KNOWN_START.format(name=name, household=esc(household_name))
        else:
            text = KNOWN_START_NO_HH.format(name=name)
        await update.message.reply_text(text, parse_mode="HTML")
        cid = str(update.effective_chat.id)
        if not self._country_for(cid) and cid not in self._country_asked:
            self._country_asked.add(cid)
            await self._invite_flow()._ask_country(context, cid)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        is_staff = str(update.effective_chat.id) in getattr(self, "admin_chat_ids", [])
        text = (
            "🍷 <b>Whiney</b>\n\n"
            "/fridge \u2014 What's in the fridge (Catch up walks a drawer)\n"
            "/status \u2014 Inventory summary\n"
            "/search \u2014 Find a wine by name\n"
            "/stock \u2014 Browse wines by type\n"
            "/storages \u2014 View storage locations\n"
            "/recent \u2014 Recent bottles\n"
        )
        if self.flag_on("feature_lists"):
            text += "/list \u2014 Things to drink or buy\n"
        if self.flag_on("feature_clubs"):
            text += "/club \u2014 Wine clubs\n"
            if self.flag_on("feature_lists"):
                text += "/club list \u2014 Shared tasting list\n"
            if self.flag_on("feature_tasteoffs"):
                text += "/club tasteoff \u2014 Score a flight\n"
            if self.flag_on("feature_club_comments"):
                text += "Feed cards: Say something\n"
        text += (
            "/pending \u2014 Show pending barcodes\n"
            "/flush \u2014 Clear all pending barcodes\n"
            "/household \u2014 View household & members\n"
            "/switch \u2014 Switch active household\n"
            "/invite \u2014 Invite someone to your cellar\n"
            "/badges \u2014 Cellar challenges (on for new cellars; owners can turn them off)\n"
            "/taste \u2014 What this cellar drinks, and what you've looked at\n"
        )
        if is_staff:
            text += (
                "/adduser \u2014 Add a member to a named household\n"
                "/removeuser \u2014 Remove a member from a named household\n"
            )
        else:
            text += (
                "/adduser \u2014 Add a member to your household\n"
                "/removeuser \u2014 Remove a member from your household\n"
            )
        text += (
            "/myid \u2014 Show your Telegram chat ID\n"
            "/help \u2014 This message\n"
        )
        if is_staff:
            text += (
                "\n\U0001F6E0 <b>Staff</b> — /admin for tools that only you see "
                "in the / menu. Add and remove always take a household name.\n"
            )
        text += (
            "\n"
            "📸 <b>Reply to a barcode message</b> with a label photo to add a new wine.\n"
            "📸 <b>Reply to a wine edit message</b> with a label photo to update details.\n"
            "📷 Send a <b>barcode photo</b> (no reply) to scan without the hardware scanner."
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._club_flow().on_text(update, context):
            return
        if await self._invite_flow().on_text(update, context):
            return
        cmd = update.message.text.split()[0] if update.message.text else "that"
        await update.message.reply_text(
            f"🤔 I don't recognise <b>{cmd}</b>. Try /help for a list of commands.",
            parse_mode="HTML",
        )

    async def _unknown_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Plain text from a provisioned user is treated as a search.

        Typing a wine name is the most natural thing to do in a chat window, and
        it used to spend the turn explaining that only commands work.
        """
        text = (update.message.text or "").strip()

        if await self._club_flow().on_text(update, context):
            return

        if await self._invite_flow().on_text(update, context):
            return

        # A pasted link is not a wine name. Searching the cellar for a URL
        # returns nothing and tells the sender nothing about why.
        if re.search(r"https?://|www\.", text, re.IGNORECASE):
            await update.message.reply_text(
                "\U0001F517 I can't open links yet.\n\n"
                "If that's a wine list, take a <b>screenshot</b> or photo of it "
                "and send me that — I'll read it and tell you what to order.",
                parse_mode="HTML",
            )
            return

        # Drink/remove-one-of-my-X must beat country/region browse and the
        # wine-detail lookup — those used to list every Italian wine or open
        # a card for "I drank one of my barolos" / "Remove one of my barolos".
        if await self._maybe_typed_stock(update, context, text):
            return

        if await self._maybe_correct_winelist_text(update, text):
            return

        if await self._maybe_typed_add(update, context, text):
            return

        if await self._maybe_take_producer(update, text, context):
            return

        if await self._maybe_identify_misread_text(update, text):
            return

        if len(text) >= 3:
            router = getattr(self, "_router", None)
            if router is not None and await router.handle_text(update, context, text):
                return
            await self._do_search(update, text)
            return
        await update.message.reply_text(
            "\U0001F4A1 Type a wine name to search, send a <b>photo</b> of a bottle to add one, "
            "or try /help.",
            parse_mode="HTML",
        )

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

            if not wines:
                unconfigured, storage_name = _unconfigured_storage(api)
                text, kb = empty_cellar(
                    unconfigured=unconfigured, storage_name=storage_name,
                )
                await update.message.reply_text(
                    text, reply_markup=kb, parse_mode="HTML",
                )
                return

            text = f"📊 <b>Wine Cellar</b>\n\n"
            text += f"<b>{len(wines)}</b> wines  ·  <b>{total_bottles}</b> bottles\n"
            estimate = estimated_cellar_value(wines)
            if estimate:
                text += estimate + "\n"

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

            router = getattr(self, "_router", None)
            if router is not None:
                text += router.status_line()
            from integrations.copy import t as copy_t
            loc = self.locale_for(update)
            if self.flag_on("feature_lists"):
                text += "\n" + copy_t(loc, "list.status.ready")
            else:
                text += "\n" + copy_t(loc, "list.status.resting")
            if self.flag_on("feature_clubs"):
                text += "\n" + copy_t(loc, "club.status.ready")
            else:
                text += "\n" + copy_t(loc, "club.status.resting")
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = update.effective_user
        name = html.escape(user.full_name if user else "Unknown", quote=False)
        await update.message.reply_text(
            f"👤 <b>{name}</b>\n"
            f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
            f"Send this to whoever is adding you.",
            parse_mode="HTML",
        )

    async def _cmd_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Smart cascading stock browser."""
        api = self._get_api(update)
        try:
            wines = api.wines_in_stock()
            if not wines:
                unconfigured, storage_name = _unconfigured_storage(api)
                text, kb = empty_cellar(
                    unconfigured=unconfigured, storage_name=storage_name,
                )
                await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
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

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/search <name> — find a wine by name. Also backs plain-text search."""
        query = " ".join(context.args).strip() if context.args else ""
        if not query:
            await update.message.reply_text(
                "\U0001F50D Search for a wine by name:\n"
                "<code>/search chenin</code>\n\n"
                "Or just type the name — no command needed.",
                parse_mode="HTML",
            )
            return
        await self._do_search(update, query)

    async def _do_search(self, update: Update, query: str):
        if parse_typed_stock(query):
            if await self._maybe_typed_stock(update, None, query):
                return
        api = self._get_api(update)
        try:
            wines = api.search_wines(query)
        except Exception as e:
            await update.message.reply_text(f"Error searching: {e}")
            return

        if not wines:
            await update.message.reply_text(
                f"\U0001F50D Nothing matching <b>{query}</b> in your cellar.\n\n"
                f"\U0001F4F8 If you've just bought it, send me a photo of the label.",
                parse_mode="HTML",
            )
            return

        lines = [f"\U0001F50D <b>{len(wines)} match{'es' if len(wines) != 1 else ''} for \"{query}\"</b>\n"]
        keyboard = []
        for w in wines[:8]:
            emoji = WINE_TYPE_EMOJI.get(w.get("wine_type", ""), "\U0001F377")
            stock = w.get("stock_count", 0)
            stock_str = f"{stock}x" if stock else "none left"
            lines.append(f"{emoji} <b>{w['name']}</b> — {stock_str}")
            keyboard.append([InlineKeyboardButton(
                f"\U0001F4CB {w['name'][:40]}", callback_data=_cb("det", wine_id=w["id"]),
            )])
        if len(wines) > 8:
            lines.append(f"\n<i>... and {len(wines) - 8} more — try a narrower search.</i>")

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        api = self._get_api(update)
        try:
            payload = api.stock_history(offset=0, limit=DRANK_PAGE_SIZE)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return
        text, markup = build_drank_history_view(payload)
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=markup,
        )

    async def _cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._list_flow().cmd_list(update, context)

    async def _cmd_club(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._club_flow().cmd_club(update, context)

    async def _show_drank_page(self, query, api, page: int):
        try:
            payload = api.stock_history(
                offset=max(0, page) * DRANK_PAGE_SIZE,
                limit=DRANK_PAGE_SIZE,
            )
        except Exception as e:
            await _safe_edit(query,f"Error: {e}")
            return
        text, markup = build_drank_history_view(payload)
        await _safe_edit(query,
            text, parse_mode="HTML", reply_markup=markup,
        )

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._sweep()
        api = self._get_api(update)
        with self._lock:
            active = _active_pendings(self.pending_store, self._hid(api))
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
        api = self._get_api(update)
        hid = self._hid(api)
        with self._lock:
            active = _active_pendings(self.pending_store, hid)
            count = len(active)
            for bc in list(active.keys()):
                self.pending_store.pop(pending_key(hid, bc), None)
            # Also drop expired and internal (_label_/_edit_) leftovers, which
            # never appear in _active_pendings but do hold photo bytes.
            swept = _sweep_pendings(self.pending_store)
        if not count and not swept:
            await update.message.reply_text("Nothing to flush.")
            return
        msg = f"🗑 Flushed {count} pending barcode(s)."
        if swept:
            msg += f"\nAlso cleared {swept} expired entr{'y' if swept == 1 else 'ies'}."
        await update.message.reply_text(msg)

    async def _cmd_storages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        api = self._get_api(update)
        try:
            storages = api.list_storages()
            if not storages:
                text, kb = empty_cellar(
                    "No storage locations yet", unconfigured=True,
                )
                await update.message.reply_text(
                    text, reply_markup=kb, parse_mode="HTML",
                )
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
            self._catchup_flow().drop(chat_id)
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
            await _safe_edit(update_or_query, text, parse_mode="HTML", reply_markup=buttons)
        elif hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=buttons)
        if new_api:
            try:
                await self._club_flow().after_household_switch(
                    update_or_query, chat_id, household_id, household_name,
                )
            except Exception:
                logger.exception("[club] household switch prompt failed")

    async def _cmd_household(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show household info and members."""
        api = self._get_api(update)
        try:
            data = api.get_household()
            text = f"\U0001F3E0 <b>{data['name']}</b>\n"
            text += f"Your role: {data['role']}\n"
            if data.get("gamification_enabled"):
                text += "Cellar challenges: on \u2014 /badges\n"
            text += "\n<b>Members:</b>\n"
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

    async def _cmd_badges(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Opt in/out of cellar challenges, or list what's been unlocked."""
        api = self._get_api(update)
        arg = ((context.args or [None])[0] or "").strip().lower()
        if arg in ("on", "off"):
            try:
                household = api.get_household()
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
                return
            role = household.get("role")
            if role not in ("owner", "staff"):
                await update.message.reply_text(
                    "Only a household owner can turn challenges on or off."
                )
                return
            try:
                result = api.set_gamification(arg == "on")
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
                return
            if "error" in result:
                await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
                return
            if arg == "on":
                filled = result.get("backfilled") or 0
                extra = (
                    f" You've already earned {filled} from what's in the cellar \u2014 "
                    f"/badges to see them."
                    if filled else
                    " Add a bottle and I'll ping you the firsts."
                )
                await update.message.reply_text(
                    "\U0001F3C6 Cellar challenges are <b>on</b> for "
                    f"<b>{result.get('name') or 'this cellar'}</b>.{extra}",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    "Cellar challenges are off. Existing badges are kept; "
                    "nothing new will ping. /badges on to turn them back."
                )
            return

        try:
            data = api.list_achievements()
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return
        if not data.get("enabled"):
            await update.message.reply_text(
                "Cellar challenges are <b>off</b> for this household \u2014 "
                "nobody gets pings, nothing shows on the dashboard.\n\n"
                "Owners can turn them on with <code>/badges on</code>. "
                "You'll get a note the first time you add a wine from Croatia, "
                "your 10th bottle, your 5th Sauvignon, that kind of thing. "
                "There's a monthly tally too, if you ever want a friendly race.",
                parse_mode="HTML",
            )
            return
        unlocks = data.get("achievements") or []
        lines = ["\U0001F3C6 <b>Cellar challenges</b>\n"]
        lines.extend(format_competition(data.get("this_month")))
        lines.extend(format_progress(data.get("progress")))
        if unlocks:
            lines.append("<b>Latest unlocks</b>")
            for a in unlocks[:8]:
                emoji = a.get("emoji") or "\U0001F377"
                lines.append(f"{emoji} {a.get('title', '')}")
        else:
            lines.append("Nothing unlocked yet \u2014 add a bottle.")
        await update.message.reply_text("\n".join(lines).rstrip(), parse_mode="HTML")

    async def _cmd_taste(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """What this cellar drinks — inferred, not a scoreboard."""
        api = self._get_api(update)
        try:
            data = api.get_taste()
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return
        text = (data or {}).get("card") or "No taste snapshot yet \u2014 add a bottle or send a label photo."
        await update.message.reply_text(text, parse_mode="HTML")

    def _first_bottle_line(self, chat_id, wine_line: str, stock_count) -> str | None:
        from integrations.telegram_invites import maybe_first_bottle
        household = ""
        try:
            data = self._get_api(chat_id).get_household()
            household = (data or {}).get("name") or ""
        except Exception:
            pass
        return maybe_first_bottle(self, chat_id, household, wine_line, stock_count)

    async def _cmd_invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._invite_flow().cmd_invite(update, context)

    async def _cmd_invite_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Staff /newcellar. Same mint as /invite new; hidden from non-staff."""
        context.args = ["new"]
        await self._invite_flow().cmd_invite(update, context)

    async def _handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._club_flow().on_location(update, context):
            return
        if await self._invite_flow().on_location(update, context):
            return
        if not self._is_known(update):
            await self._greet_stranger(update)
            return
        router = getattr(self, "_router", None)
        if router is not None:
            await router.on_location(update, context)

    async def _handle_photo_routed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._invite_flow().on_photo(update, context):
            return
        if not self._is_known(update):
            await self._greet_stranger(update)
            return
        await self._handle_photo(update, context)

    async def _cmd_adduser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a user to a household: /adduser <chat_id> <name> [household]"""
        api = self._get_api(update)
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /adduser &lt;chat_id&gt; &lt;display_name&gt;\n\n"
                "Adds them to <b>your</b> household. If you belong to more than "
                "one, pass the household name at the end.\n\n"
                "They should message this bot first (/myid) so we can welcome them.",
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

            household_label = result.get("household", "your cellar")
            delivered = await self._welcome_new_member(
                context, chat_id, display_name, household_label,
            )

            if status_text == "created":
                text = (
                    f"\u2705 <b>New user created:</b> {display_name}\n"
                    f"Added to <b>{household_label}</b>\n"
                    f"Username: <code>{result['username']}</code>"
                )
            else:
                text = (
                    f"\u2705 <b>{result['display_name']}</b> added to <b>{household_label}</b>"
                )
            if delivered:
                text += "\n\n\U0001F4EC I've welcomed them and they can start adding wine now."
            else:
                # A bot cannot open a conversation with a user — they have to
                # message it first. Say so, or the add looks done when it isn't.
                text += (
                    "\n\n\u26A0\uFE0F I couldn't message them — they haven't opened "
                    "the bot yet. Ask them to search for this bot and tap "
                    "<b>Start</b>, then they're good to go."
                )
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _welcome_new_member(self, context, chat_id, display_name, household_name) -> bool:
        """Message a freshly added member. Returns False if we couldn't reach them.

        This is the moment they're most likely to act, and until now nothing was
        sent here at all — the "they can use it immediately" confirmation went to
        the adder, so the new person never learned anything had happened.
        """
        from integrations.telegram_invites import welcome_member
        text = welcome_member(display_name, household_name)
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            return True
        except Exception as e:
            logger.info(f"[adduser] could not welcome {chat_id}: {e}")
            return False

    async def _cmd_removeuser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a user from a household: /removeuser <chat_id> [household]"""
        api = self._get_api(update)
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /removeuser &lt;chat_id&gt;\n\n"
                "Removes them from <b>your</b> household. If you belong to more "
                "than one, pass the household name at the end.\n\n"
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
            text, kb = empty_cellar(unconfigured=True)
            await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
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
                await _safe_edit(update_or_query, text)
            else:
                await update_or_query.message.reply_text(text)
            return

        if not wines:
            unconfigured, hint_name = _unconfigured_storage(api)
            text, kb = empty_cellar(
                f"{storage_name} is empty" if storage_name else "Your cellar is empty",
                unconfigured=unconfigured,
                storage_name=storage_name or hint_name,
            )
            if hasattr(update_or_query, "edit_message_text"):
                await _safe_edit(update_or_query, text, reply_markup=kb, parse_mode="HTML")
            else:
                await update_or_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
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
            await _safe_edit(update_or_query, text, reply_markup=keyboard, parse_mode="HTML")
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
            removed = getattr(session, "last_removed", None)
            if removed and removed.get("wine_id"):
                text, kb = last_bottle_removed_view(
                    removed["wine_id"], removed.get("name") or "that wine",
                    item_id=removed.get("item_id"),
                )
                await _safe_edit(
                    query,
                    text, reply_markup=kb, parse_mode="HTML",
                )
                return
            del self._browse_sessions[session_key]
            await _safe_edit(
                query,
                empty_cellar()[0], reply_markup=empty_cellar()[1], parse_mode="HTML",
            )
            return
        # If currently showing a flat list, stay on the same page (clamped)
        filtered = session.filtered_wines
        total_pages = max(1, (len(filtered) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
        session.page = min(session.page, total_pages - 1)
        text, keyboard = _build_browse_list(session, session_key, filtered)
        await _safe_edit(query, text, reply_markup=keyboard, parse_mode="HTML")

    def _build_detail_card(self, wine: dict, api=None) -> tuple[str, InlineKeyboardMarkup]:
        """Build the wine detail card text and keyboard."""
        wine_id = wine["id"]
        text = f"\U0001F377 <b>{wine['name']}</b>\n\n"
        producer = wine.get("producer", "")
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

        text += _references_block(wine)

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
        text = _append_wine_url(text, api or self.api, wine_id)
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
            [
                InlineKeyboardButton("\U0001F504 Refresh refs", callback_data=_cb("rr", wine_id=wine_id)),
                InlineKeyboardButton("\u270f\ufe0f Update from label", callback_data=_cb("ed", wine_id=wine_id)),
            ],
            [
                InlineKeyboardButton(ADD_PHOTO_INFO, callback_data=_cb("ai", wine_id=wine_id)),
            ],
        ])
        return text, keyboard

    def _build_drawer_view(self, wine_id: int, api=None):
        """Build the drawer assignment view for a specific wine.

        Returns (text, InlineKeyboardMarkup) or (None, None) if wine not found.
        """
        api = api or self.api
        wine = api.get_wine_detail(wine_id)
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

    @staticmethod
    def _hid(api):
        """Household id for an API client, or None in single-tenant fallback mode."""
        return getattr(api, "household_id", None)

    def _pkey(self, api, barcode: str) -> str:
        """Household-scoped key for a barcode in the shared pending store."""
        return pending_key(self._hid(api), barcode)

    def _get_pending(self, api, barcode: str) -> dict:
        """Read a pending entry for this household. Returns {} when absent."""
        with self._lock:
            return dict(self.pending_store.get(self._pkey(api, barcode), {}))

    def _upload_pending_photos(self, api, pending_entry: dict, wine_id: int) -> None:
        front = pending_entry.get("photo_front") or pending_entry.get("photo_data")
        back = pending_entry.get("photo_back")
        if front:
            itype = "LF" if back else "FR"
            try:
                api.upload_wine_image(wine_id, front, image_type=itype)
            except Exception as e:
                logger.warning(f"[PHOTO] Failed to upload front image: {e}")
        if back:
            try:
                api.upload_wine_image(wine_id, back, image_type="LB")
            except Exception as e:
                logger.warning(f"[PHOTO] Failed to upload back image: {e}")

    def _open_label_cards(self, api, *, max_age: float | None = None,
                          include_complete: bool = False) -> list[tuple[str, dict]]:
        """Uncommitted label cards, newest first, including `_label_` keys."""
        now = time.time()
        hid = self._hid(api)
        timeout = PENDING_TIMEOUT_SECONDS if max_age is None else max_age
        found = []
        with self._lock:
            for key, data in self.pending_store.items():
                if not isinstance(data, dict) or data.get("resolved") or data.get("wine_id"):
                    continue
                if key.startswith("_edit_"):
                    continue
                if (now - data.get("time", 0)) >= timeout:
                    continue
                if hid is not None and data.get("household_id") not in (None, hid):
                    continue
                if data.get("photo_back") and not include_complete:
                    continue
                if not (data.get("grok_merged") or data.get("photo_data") or data.get("photo_front")):
                    continue
                barcode = key if str(key).startswith("_") else (data.get("barcode") or key)
                found.append((barcode, data))
        found.sort(key=lambda row: row[1].get("time", 0), reverse=True)
        return found

    def _is_pending(self, api, barcode: str) -> bool:
        with self._lock:
            data = self.pending_store.get(self._pkey(api, barcode))
            if not data or data.get("resolved"):
                return False
            return (time.time() - data.get("time", 0)) < PENDING_TIMEOUT_SECONDS

    def _resolve_pending(self, api, barcode: str):
        """Drop a pending entry once it has been acted on.

        Popping rather than flagging matters: the entry holds the label photo
        bytes, and a flagged entry is filtered out of every listing while
        staying in memory forever. Callers read what they need before calling
        this, and a second tap on a stale button still reads as "already
        handled" because _is_pending() returns False either way.
        """
        with self._lock:
            self.pending_store.pop(self._pkey(api, barcode), None)

    def _next_label_key(self) -> str:
        """Unique key for a label analysed without a barcode.

        Previously keyed on id(grok_data) — a memory address, which CPython
        reuses after GC, so two concurrent label flows could collide.
        """
        with self._lock:
            self._label_seq += 1
            return f"_label_{self._label_seq}"

    def _sweep(self):
        """Evict expired pendings. Cheap; call on any user-initiated entry point."""
        with self._lock:
            dropped = _sweep_pendings(self.pending_store)
        if dropped:
            logger.info(f"[pending] swept {dropped} expired entr{'y' if dropped == 1 else 'ies'}")

    def _create_pending(self, api, barcode: str, off_data=None):
        """Create a new pending entry for a barcode (e.g. when user says 'new wine')."""
        with self._lock:
            self.pending_store[self._pkey(api, barcode)] = {
                "off_data": off_data,
                "time": time.time(),
                "barcode": barcode,
                "household_id": self._hid(api),
            }

    def _update_pending(self, api, barcode: str, **fields):
        """Merge fields into an existing pending entry, if it's still there."""
        with self._lock:
            entry = self.pending_store.get(self._pkey(api, barcode))
            if entry is not None:
                entry.update(fields)

    def _claim_pending(self, api, barcode: str) -> tuple[str, int | None]:
        """Take a pending card so a double-tap cannot create the wine twice.

        Returns (status, wine_id): claimed, busy, created, or gone.
        """
        with self._lock:
            key = self._pkey(api, barcode)
            entry = self.pending_store.get(key)
            if not entry:
                return "gone", None
            if (time.time() - entry.get("time", 0)) >= PENDING_TIMEOUT_SECONDS:
                return "gone", None
            wine_id = entry.get("wine_id")
            if wine_id:
                return "created", wine_id
            try_claim = getattr(self.pending_store, "try_claim", None)
            if callable(try_claim):
                # SET NX 120s is the lock. Ignore a leftover 1h in_progress
                # flag so a restart or a failed Drank cannot mute the card.
                if not try_claim(key):
                    return "busy", None
                return "claimed", None
            if entry.get("in_progress"):
                return "busy", None
            entry["in_progress"] = True
            return "claimed", None

    def _unclaim_pending(self, api, barcode: str):
        key = self._pkey(api, barcode)
        with self._lock:
            entry = self.pending_store.get(key)
            if entry:
                entry.pop("in_progress", None)
        release = getattr(self.pending_store, "release_claim", None)
        if not callable(release):
            return
        try:
            release(key)
        except StoreError:
            pass

    def _claim_card(self, message) -> bool:
        key = self._card_key(message) if message else ""
        if not key:
            return True
        try:
            return self._label_cards.try_claim(key)
        except StoreError:
            raise

    def _release_card(self, message) -> None:
        key = self._card_key(message) if message else ""
        if not key:
            return
        try:
            self._release_card_key(key)
        except StoreError:
            pass

    def _release_card_key(self, key: str) -> None:
        self._label_cards.release_claim(key)
        entry = dict(self._label_cards.get(key) or {})
        if entry.get("in_progress"):
            entry["in_progress"] = False
            self._label_cards[key] = entry

    def _touch_label_card(self, message, **fields) -> dict | None:
        if not message:
            return None
        key = self._card_key(message)
        entry = dict(self._label_cards.get(key) or {})
        entry.update(fields)
        self._label_cards[key] = entry
        return entry

    def _remember_paid(self, message, wine_id, paid) -> None:
        if paid is None or not wine_id:
            return
        self._pending_paid[wine_id] = paid
        if message:
            self._touch_label_card(message, paid=paid, wine_id=wine_id)

    def _paid_for(self, message, wine_id):
        card = self._card_logged_item(message)
        if card.get("paid") is not None:
            return card.get("paid")
        return self._pending_paid.get(wine_id)

    async def _tap_busy(self, query) -> None:
        try:
            await query.answer(BUSY_TAP, show_alert=True)
            return
        except Exception:
            pass
        try:
            await query.message.reply_text(BUSY_TAP)
        except Exception:
            pass

    async def _resend_photo(self, query):
        try:
            await _safe_edit(query,RESEND_PHOTO)
        except Exception:
            try:
                await query.answer(RESEND_PHOTO, show_alert=True)
            except Exception:
                pass

    def _card_logged_item(self, message) -> dict:
        if not message:
            return {}
        return dict(self._label_cards.get(self._card_key(message)) or {})

    async def _reshow_drank(self, query, card: dict):
        name = card.get("wine_name") or "that wine"
        item_id = card.get("item_id")
        stock = card.get("stock_count") or 0
        if item_id:
            text, markup = build_drank_view(
                name, stock, item_id, wine_id=card.get("wine_id"),
            )
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
            return
        await _safe_edit(
            query,
            f"\U0001F377 <b>Drank:</b> {name}\n"
            f"\U0001F4CA In stock: {stock}",
            parse_mode="HTML",
        )

    async def _prompt_where_to_store(
        self, query, api, context, *, wine_id, name, paid=None, created=True,
        ask_barcode=False,
    ):
        """Fridge picker after creating a wine, when the household has several storages."""
        card = self._card_logged_item(query.message)
        if card.get("item_id") and not card.get("drunk") and not card.get("guard"):
            await _safe_edit(
                query,
                f"📥 <b>Added:</b> {card.get('wine_name') or name}\n"
                f"📊 In stock: {card.get('stock_count') or 0}",
                parse_mode="HTML",
            )
            return
        if paid is not None:
            self._remember_paid(query.message, wine_id, paid)
        paid = self._paid_for(query.message, wine_id)
        storages = api.list_storages()
        if len(storages) <= 1:
            storage = storages[0] if storages else None
            storage_name = storage["name"] if storage else api.default_storage
            drawers = storage.get("drawers", 0) if storage else 0
            stock = api.add_stock(
                wine_id, storage_name=storage_name, price=paid,
                check_duplicate=True,
            )
            if stock.get("error") == "recent_duplicate":
                self._touch_label_card(
                    query.message, guard=True, wine_id=wine_id,
                    wine_name=stock.get("wine_name") or name, in_progress=False,
                )
                await _safe_edit(
                    query,
                    f"Already added <b>{stock.get('wine_name') or name}</b> a moment ago."
                    "\n\nAdd another bottle?",
                    parse_mode="HTML",
                    reply_markup=_duplicate_guard_markup(
                        wine_id, stock.get("item_id"), drank=False,
                    ),
                )
                return
            failed = _stock_failed(stock)
            if failed:
                await _safe_edit(
                    query,
                    f"Saved <b>{name}</b> but couldn't add a bottle.\n\n⚠️ {failed}",
                    parse_mode="HTML",
                    reply_markup=_retry_add_markup(wine_id),
                )
                return
            self._touch_label_card(
                query.message, item_id=stock.get("item_id"),
                wine_name=stock.get("wine_name") or name,
                stock_count=stock.get("stock_count", 0),
                committed=True, drunk=False, in_progress=False,
                wine_id=wine_id,
            )
            item_id = stock.get("item_id")
            await self._celebrate(context, query.message.chat_id, stock)
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
                tip = BARCODE_TIP if ask_barcode else ""
                await _safe_edit(
                    query,
                    f"🍷 <b>Wine created:</b> {name}\n"
                    f"📍 {storage_name}{tip}\n\n"
                    f"🗄 <b>Which drawer?</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                first = self._first_bottle_line(
                    query.message.chat_id, name, stock.get("stock_count"),
                )
                if first:
                    await _safe_edit(
                        query,
                        _append_wine_url(first, api, wine_id),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(_wine_followup_rows(wine_id)),
                    )
                else:
                    tip = BARCODE_TIP if ask_barcode else ""
                    text = (
                        f"🍷 <b>Wine added:</b> {name}\n"
                        f"📍 {storage_name}{tip}\n"
                        f"📊 In stock: {stock['stock_count']}\n\n"
                        f"⏳ <i>Looking up ratings and price — these will arrive in a follow-up message in a few seconds.</i>"
                    )
                    await _safe_edit(
                        query,
                        _append_wine_url(text, api, wine_id),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(_wine_followup_rows(wine_id)),
                    )
            return
        keyboard = []
        for s in storages:
            label = s["name"]
            if s.get("bottles"):
                label += f" ({s['bottles']})"
            keyboard.append([InlineKeyboardButton(
                label,
                callback_data=_cb("si", wine_id=wine_id, page=s["id"]),
            )])
        heading = "Wine created" if created else "Wine added"
        tip = BARCODE_TIP if ask_barcode else ""
        await _safe_edit(
            query,
            f"🍷 <b>{heading}:</b> {name}{tip}\n\n"
            f"📍 <b>Where are you storing this?</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _add_pending_wine(
        self, query, api, context, barcode, *,
        skip_producer=False, producer_override=None, original="",
        tap_action="grok",
    ):
        """Claim a pending label card and create the wine (grok / np / gp / typed)."""
        tap_chat = None
        if query.message:
            tap_chat = getattr(query.message, "chat_id", None) or getattr(
                getattr(query.message, "chat", None), "id", None,
            )
        self._touch_label_card(query.message, committed=True)
        status, existing_id = self._claim_pending(api, barcode)
        logger.info(
            "[tap] %s claim=%s barcode=%s existing=%s",
            tap_action, status, barcode or "-", existing_id,
        )
        if status == "gone":
            self._log_tap_early(tap_action, "gone", barcode, tap_chat)
            card = self._label_cards.get(
                self._card_key(query.message) if query.message else "",
            ) or {}
            existing_id = card.get("wine_id")
            if existing_id:
                status = "created"
            elif barcode and not str(barcode).startswith("_"):
                try:
                    hits = api.lookup_barcode(barcode)
                except Exception:
                    hits = []
                if hits:
                    name = hits[0].get("name") or "that wine"
                    await _safe_edit(
                        query,
                        f"That's already in as <b>{name}</b>. "
                        "Scan it on the gun to add a bottle.",
                        parse_mode="HTML",
                    )
                    return
                await _safe_edit(query, _finished_copy(barcode))
                return
            else:
                await _safe_edit(query, _finished_copy(barcode))
                return
        if status == "busy":
            self._log_tap_early(tap_action, "busy", barcode, tap_chat)
            await self._tap_busy(query)
            return
        if status == "created":
            card = self._label_cards.get(
                self._card_key(query.message) if query.message else "",
            ) or {}
            name = card.get("wine_name") or ""
            stock = 0
            try:
                detail = api.get_wine_detail(existing_id)
                if isinstance(detail, dict):
                    raw = detail.get("stock_count")
                    if isinstance(raw, int):
                        stock = raw
                    if not name:
                        name = detail.get("name") or ""
            except Exception:
                pass
            if stock > 0:
                self._log_tap_early(tap_action, "already_in", barcode, tap_chat)
                await _safe_edit(
                    query,
                    f"That's already in as <b>{name or 'that wine'}</b> "
                    f"({stock} in stock).",
                    parse_mode="HTML",
                )
                return
            await self._prompt_where_to_store(
                query, api, context,
                wine_id=existing_id, name=name or "that wine",
                paid=self._paid_for(query.message, existing_id),
            )
            return

        try:
            merged = dict(self._get_pending(api, barcode).get("grok_merged") or {})
            if not merged:
                self._log_tap_early(
                    tap_action, "no_extracted_data", barcode, tap_chat,
                )
                await _safe_edit(
                    query,
                    "I lost the reading for that bottle. Send the label photo again?",
                )
                return
            if producer_override is not None:
                merged["producer"] = producer_override
                self._update_pending(api, barcode, grok_merged=merged)
            elif skip_producer:
                merged["producer"] = str(merged.get("producer") or "").strip()
                self._update_pending(api, barcode, grok_merged=merged)
            elif not str(merged.get("producer") or "").strip():
                self._log_tap_early(tap_action, "no_producer", barcode, tap_chat)
                guess = _suggested_producer(merged.get("name") or "")
                chat_id = str(tap_chat or "")
                if chat_id:
                    self._await_producer[chat_id] = {
                        "barcode": barcode,
                        "time": time.time(),
                        "card_key": self._card_key(query.message) if query.message else "",
                    }
                await _safe_edit(
                    query,
                    WHO_MAKES_THIS,
                    parse_mode="HTML",
                    reply_markup=_who_makes_keyboard(barcode, guess),
                )
                return

            from integrations.barcodes import accepted_barcode, normalise_barcode
            wine_data = _clean_wine_data(merged)
            if str(barcode).startswith("_"):
                grok_bc = accepted_barcode(merged.get("barcode"))
                if grok_bc:
                    wine_data["barcode"] = grok_bc
            if original != "new":
                try:
                    cellar = api.list_all_wines()
                    if str(barcode).startswith("_"):
                        hits = find_existing_wines(merged, cellar)
                    else:
                        hits = wines_to_link(merged, cellar)
                except Exception:
                    hits = []
                if hits:
                    self._log_tap_early(
                        tap_action, "is_this", barcode, tap_chat,
                    )
                    await _safe_edit(
                        query,
                        _is_this_text(barcode, hits),
                        parse_mode="HTML",
                        reply_markup=_is_this_keyboard(barcode, hits),
                    )
                    return
            if not str(barcode).startswith("_"):
                wine_data["barcode"] = normalise_barcode(barcode) or barcode
            result = api.create_wine(wine_data)
            wine_id = result.get("id")
            if not wine_id:
                await _safe_edit(
                    query,
                    f"Error creating wine: {result.get('error') or result}",
                )
                return
            paid = wine_data.get("price")
            self._remember_paid(query.message, wine_id, paid)
            self._update_pending(api, barcode, wine_id=wine_id)
            self._touch_label_card(
                query.message, committed=True,
                wine_id=wine_id, wine_name=result.get("name"),
            )

            pending_entry = self._get_pending(api, barcode)
            self._upload_pending_photos(api, pending_entry, wine_id)
            if not (pending_entry.get("photo_data") or pending_entry.get("photo_front")):
                logger.warning(
                    f"[PHOTO] No photo_data found in pending_store for barcode={barcode}"
                )

            chat_id = tap_chat
            if chat_id is None and getattr(query, "message", None):
                chat_id = query.message.chat_id
            self._reuse_label_lookup(
                wine_id, merged, query.message, api,
                chat_id, result.get("name", ""),
            )

            self._resolve_pending(api, barcode)
            await self._prompt_where_to_store(
                query, api, context,
                wine_id=wine_id, name=result.get("name") or "",
                paid=paid,
                ask_barcode=str(barcode).startswith("_"),
            )
        except StoreError:
            raise
        except Exception as e:
            logger.exception("%s confirm failed after create", tap_action)
            await _safe_edit(query, f"Error creating wine: {e}")
        finally:
            self._unclaim_pending(api, barcode)

    def _log_tap_early(self, action, reason, barcode, chat):
        logger.info(
            "[tap] %s early-return reason=%s barcode=%s chat=%s",
            action, reason, barcode or "-", chat,
        )

    async def _redraw_scan_card(
        self, query, api, *, action, wine_id, barcode="",
        item_id=None, storage_id=None,
    ):
        """Re-render the fridge-scan card after Move / Drawer / Back."""
        if not wine_id:
            await _safe_edit(query, "That card's out of date — send /stock again.")
            return
        wine = api.get_wine_detail(wine_id) or {}
        name = wine.get("name") or "Wine"
        vintage = wine.get("vintage")
        if vintage and str(vintage) not in str(name):
            name = f"{name} {vintage}".strip()
        stock = wine.get("stock_count")
        if stock is None:
            stock = len(wine.get("items") or [])
        barcode = barcode or wine.get("barcode") or ""
        storage, row, drawers, sid = "", None, 0, storage_id
        storages = api.list_storages()
        if action == "in" and item_id:
            item = next(
                (i for i in wine.get("items") or [] if i.get("item_id") == item_id),
                None,
            )
            if item:
                storage = item.get("storage") or ""
                row = item.get("drawer")
                match = next((s for s in storages if s["name"] == storage), None)
                if match:
                    drawers = match.get("drawers") or 0
                    sid = match.get("id")
        elif action == "out" and storage_id:
            match = next((s for s in storages if s["id"] == storage_id), None)
            if match:
                storage = match["name"]
                sid = match["id"]
        text, markup = build_stock_update_view(
            action, name, stock,
            wine_id=wine_id, barcode=barcode, storage=storage,
            item_id=item_id, drawers=drawers, row=row, storage_id=sid,
        )
        await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)

    # --- Callback handler ---

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            return await self._dispatch_callback(update, context)
        except StoreError:
            logger.exception("[tap] store")
            await self._resend_photo(query)
        except BadRequest as e:
            if not _not_modified(e):
                raise
            logger.info("[tap] message unchanged")
            try:
                await query.answer()
            except Exception:
                pass

    async def _dispatch_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query

        if await self._reject_inaccessible(query, context):
            return

        if await self._invite_flow().on_callback(update, context):
            return

        if await self._catchup_flow().on_callback(update, context):
            return

        if await self._list_flow().on_callback(update, context):
            return

        if await self._club_flow().on_callback(update, context):
            return

        data = _parse_cb(query.data)
        action = data.get("action", "")
        msg = getattr(query, "message", None)
        tap_chat = getattr(msg, "chat_id", None) or getattr(getattr(msg, "chat", None), "id", None)
        logger.info(
            "[tap] chat=%s action=%s barcode=%s wine_id=%s item_id=%s data=%s",
            tap_chat, action or "-", data.get("barcode") or "-",
            data.get("wine_id") or "-", data.get("item_id") or "-",
            (query.data or "")[:64],
        )

        # Fridge +/- use custom answer toasts; all others answer immediately.
        # answer() fails on callbacks older than ~48h; that must not abort the
        # handler, or every button in the user's scrollback becomes a no-op.
        if action not in ("+", "-", "mv", "ss", "vt", "ul", "ap"):
            try:
                await query.answer()
            except Exception as e:
                logger.info(f"[callback] answer() failed (likely a stale button): {e}")

        if action in ("cf", "cc", "cq", "ck", "cn", "co", "cx", "cr", "cw"):
            router = getattr(self, "_router", None)
            if router is None:
                await query.message.reply_text("That's finished — just type again.")
                return
            await router.on_callback(query, data, context)
            return

        api = self._get_api(query)

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
                await _safe_edit(query,"\U0001f9ca Loading fridge...")
                await self._cmd_fridge(update, context)
            elif cmd == "status":
                await _safe_edit(query,"\U0001f4ca Loading status...")
                await self._cmd_status(update, context)
            return

        if action in ("wl", "wlp", "wli"):
            key = f"{query.message.chat_id}:{query.message.message_id}"
            entry = self._winelists.get(key)
            if not entry:
                await _safe_edit(query,
                    "\U0001F4CB That list has expired — send the photo again."
                )
                return
            text, keyboard = build_winelist_view(
                entry["data"],
                page=data.get("page", 0),
                show_all=(action == "wl"),
                detail_index=data.get("page") if action == "wli" else None,
                from_pick=data.get("original") == "p",
            )
            await _safe_edit(query,text, parse_mode="HTML", reply_markup=keyboard)
            return

        if action == "wlf":
            await self._start_winelist_correction(query, data.get("page", 0))
            return

        if action == "howto":
            await _safe_edit(query,
                "\U0001F4F8 <b>Adding a wine</b>\n\n"
                "1. Take a photo of the <b>front label</b> — straight on, whole "
                "label in frame, no flash glare.\n"
                "2. Send it to me here. No caption needed.\n"
                "3. I'll show you what I read off it — tap <b>Add to cellar</b>.\n"
                "4. Pick where you're putting the bottle.\n\n"
                "That's it. Ratings and a price estimate arrive a few seconds later.\n\n"
                "\U0001F4E6 A photo of a <b>barcode</b> works too — I'll look it up "
                "and ask for a label photo if I don't recognise it.",
                parse_mode="HTML",
            )
            return

        if action == "dh":
            await self._show_drank_page(query, api, data.get("page", 0))
            return

        if action == "dn":
            wine_id = data.get("wine_id")
            page = data.get("page", 0)
            try:
                wine = api.get_wine_detail(wine_id) if wine_id else None
                if not wine:
                    await _safe_edit(query,"Wine not found.")
                    return
                text, markup = build_drank_notes_view(wine, page=page)
                await _safe_edit(query,
                    text, parse_mode="HTML", reply_markup=markup,
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")
            return

        if action == "di":
            wine_id = data.get("wine_id")
            page = data.get("page", 0)
            try:
                wine = api.get_wine_detail(wine_id) if wine_id else None
                wine_name = wine["name"] if wine else f"Wine #{wine_id}"
                images = api.get_wine_images(wine_id) if wine_id else []
                if images:
                    image_id = images[0].get("id")
                    if image_id:
                        photo_bytes = api.get_image_file(image_id)
                        orig_msg_id = query.message.message_id
                        keyboard = [[
                            InlineKeyboardButton(
                                "\u2b05\ufe0f Back to list",
                                callback_data=_cb(
                                    "dib", wine_id=wine_id, page=orig_msg_id,
                                    original=str(page),
                                ),
                            ),
                        ]]
                        await query.message.reply_photo(
                            photo=photo_bytes,
                            caption=f"\U0001F4F8 <b>{wine_name}</b>",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
                        await _safe_edit(query,
                            f"\U0001F4F8 {wine_name} \u2014 photo below \u2b07\ufe0f"
                        )
                        return
                keyboard = [[
                    InlineKeyboardButton(
                        "\u2b05\ufe0f Back",
                        callback_data=_cb("dh", page=page),
                    ),
                ]]
                await _safe_edit(query,
                    f"\U0001F4F8 <b>{wine_name}</b>\n\nNo label photo attached.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")
            return

        if action == "dib":
            orig_msg_id = data.get("page")
            try:
                hist_page = int(data.get("original") or 0)
            except (TypeError, ValueError):
                hist_page = 0
            chat_id = query.message.chat_id
            try:
                await query.message.delete()
            except Exception as e:
                logger.info(f"[dib] could not delete photo: {e}")
            try:
                payload = api.stock_history(
                    offset=hist_page * DRANK_PAGE_SIZE,
                    limit=DRANK_PAGE_SIZE,
                )
                text, markup = build_drank_history_view(payload)
                if orig_msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=orig_msg_id,
                        text=text, parse_mode="HTML",
                        reply_markup=markup,
                    )
            except Exception as e:
                logger.warning(f"[dib] Error returning to drank list: {e}")
            return

        if action == "vt":
            item_id = data.get("item_id")
            chosen = data.get("original") or ""
            if not item_id or chosen not in dict(VOTE_LABELS):
                try:
                    await query.answer("Couldn't save that.")
                except Exception:
                    pass
                return
            try:
                api.vote_item(item_id, chosen)
            except Exception as e:
                logger.warning(f"[vote] item {item_id}: {e}")
                try:
                    await query.answer("Couldn't save that.")
                except Exception:
                    pass
                return
            try:
                await query.answer("Got it")
            except Exception:
                pass
            markup = query.message.reply_markup if query.message else None
            raw = ""
            if query.message:
                html_text = getattr(query.message, "text_html", None)
                plain = getattr(query.message, "text", None)
                if isinstance(html_text, str) and html_text:
                    raw = html_text
                elif isinstance(plain, str) and plain:
                    raw = plain
            new_markup = apply_vote_choice(markup, chosen) if markup else None
            try:
                if raw:
                    await _safe_edit(query,
                        apply_vote_thanks(raw, chosen),
                        parse_mode="HTML",
                        reply_markup=new_markup,
                    )
                elif new_markup:
                    await query.edit_message_reply_markup(reply_markup=new_markup)
            except Exception as e:
                logger.info(f"[vote] card edit failed: {e}")
            react = VOTE_REACT.get(chosen)
            if react and query.message:
                try:
                    await query.message.set_reaction(react)
                except Exception as e:
                    logger.info(f"[vote] reaction failed: {e}")
            logger.info("[tap] vote item_id=%s chosen=%s", item_id, chosen)
            return

        if action in ("dx", "dy", "ds"):
            wine_id = data.get("wine_id")
            storage_name = None
            if action == "ds":
                storage_id = data.get("page") or 0
                if storage_id:
                    try:
                        storages = api.list_storages()
                    except Exception:
                        storages = []
                    storage_name = next(
                        (s.get("name") for s in storages or [] if s.get("id") == storage_id),
                        None,
                    )
            if not wine_id:
                barcode = data.get("barcode", "")
                merged = (self._get_pending(api, barcode) or {}).get("grok_merged") or {}
                try:
                    hits = find_existing_wines(merged, api.list_all_wines())
                except Exception:
                    hits = []
                if not hits:
                    data["action"] = "dk"
                    action = "dk"
                elif len(hits) > 1:
                    await self._offer_drink_picks(query, hits)
                    return
                else:
                    wine_id = hits[0]["id"]
            if action != "dk" and wine_id:
                await self._drink_existing(
                    query, api, context, wine_id, storage_name=storage_name,
                )
                return

        if action == "dc":
            wine_id = data.get("wine_id")
            if not wine_id:
                await _safe_edit(query, "Couldn't find that wine.")
                return
            wine = {"id": wine_id, "name": f"Wine #{wine_id}"}
            try:
                detail = api.get_wine_detail(wine_id) or {}
            except Exception:
                detail = {}
            if detail:
                wine = detail
            await _safe_edit(
                query,
                _confirm_drink_text(wine),
                parse_mode="HTML",
                reply_markup=_confirm_drink_markup(wine_id),
            )
            return

        if action == "dk":
            key = self._card_key(query.message) if query.message else ""
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            created_name = ""
            card = self._card_logged_item(query.message)
            if card.get("item_id") and card.get("drunk"):
                await self._reshow_drank(query, card)
                return
            if not self._claim_card(query.message):
                await self._tap_busy(query)
                return
            pending_claimed = False
            try:
                self._touch_label_card(query.message, drunk=True, in_progress=True)
                card = self._label_cards.get(key) if key else card
                if not wine_id:
                    status, existing_id = self._claim_pending(api, barcode)
                    logger.info(
                        "[tap] drank claim=%s barcode=%s existing=%s",
                        status, barcode or "-", existing_id,
                    )
                    if status == "busy":
                        logger.info(
                            "[tap] drank early-return reason=busy barcode=%s chat=%s",
                            barcode or "-", tap_chat,
                        )
                        await self._tap_busy(query)
                        return
                    if status == "claimed":
                        pending_claimed = True
                        merged = self._get_pending(api, barcode).get("grok_merged")
                        if not merged:
                            logger.info(
                                "[tap] drank early-return reason=no_extracted_data barcode=%s chat=%s",
                                barcode or "-", tap_chat,
                            )
                            await _safe_edit(
                                query,
                                "I lost the reading for that bottle. Send the label photo again?",
                            )
                            return
                        wine_data = _clean_wine_data(merged)
                        if not str(barcode).startswith("_"):
                            wine_data["barcode"] = barcode
                        created = api.create_wine(wine_data)
                        wine_id = created["id"]
                        created_name = created.get("name") or ""
                        self._touch_label_card(
                            query.message, wine_id=wine_id, wine_name=created_name,
                        )
                        card = self._label_cards.get(key) if key else card
                        paid = wine_data.get("price")
                        self._remember_paid(query.message, wine_id, paid)
                        self._upload_pending_photos(
                            api, self._get_pending(api, barcode), wine_id,
                        )
                        self._reuse_label_lookup(
                            wine_id, merged, query.message, api,
                            update.effective_chat.id, created_name,
                        )
                        self._resolve_pending(api, barcode)
                    elif status == "created":
                        wine_id = existing_id
                    else:
                        wine_id = (card or {}).get("wine_id")
                        if not wine_id:
                            logger.info(
                                "[tap] drank early-return reason=gone barcode=%s chat=%s",
                                barcode or "-", tap_chat,
                            )
                            await _safe_edit(query,_finished_copy(barcode))
                            return
                        logger.info("[tap] drank duplicate chat=%s wine_id=%s", tap_chat, wine_id)
                        detail = {}
                        try:
                            detail = api.get_wine_detail(wine_id) or {}
                        except Exception:
                            detail = {}
                        name = (
                            detail.get("name")
                            or (card or {}).get("wine_name")
                            or "that wine"
                        )
                        stock = detail.get("stock_count")
                        if stock is None:
                            stock = 0
                        await _safe_edit(query,
                            f"\U0001F377 <b>Drank:</b> {name}\n"
                            f"\U0001F4CA In stock: {stock}",
                            parse_mode="HTML",
                        )
                        return
                paid = self._paid_for(query.message, wine_id)
                result = api.add_stock(
                    wine_id, drank=True, price=paid, check_duplicate=True,
                )
                name = created_name or result.get("wine_name") or ""
                if result.get("error") == "recent_duplicate":
                    item_id = result.get("item_id")
                    self._touch_label_card(
                        query.message, guard=True, in_progress=False,
                        wine_id=wine_id, wine_name=name,
                    )
                    await _safe_edit(query,
                        f"Already logged <b>{name or 'that wine'}</b> a moment ago."
                        "\n\nLog another bottle?",
                        parse_mode="HTML",
                        reply_markup=_duplicate_guard_markup(
                            wine_id, item_id, drank=True,
                        ),
                    )
                    return
                failed = _stock_failed(result)
                if failed:
                    await _safe_edit(query,
                        f"⚠️ {failed}"
                        + (f"\n\nSaved <b>{name}</b> but didn't log the bottle." if wine_id else ""),
                        parse_mode="HTML",
                        reply_markup=_retry_add_markup(
                            wine_id, drank=True, barcode=barcode,
                        ) if wine_id else None,
                    )
                    return
                item_id = result.get("item_id")
                self._touch_label_card(
                    query.message, item_id=item_id, wine_name=name,
                    stock_count=result.get("stock_count", 0),
                    drunk=True, in_progress=False, guard=False,
                    wine_id=wine_id,
                )
                if not item_id:
                    await _safe_edit(query,
                        f"\U0001F377 <b>Drank:</b> {name}\n"
                        f"\U0001F4CA In stock: {result.get('stock_count', 0)}",
                        parse_mode="HTML",
                    )
                    await self._celebrate(context, query.message.chat_id, result)
                    return
                text, markup = build_drank_view(
                    name, result.get("stock_count", 0), item_id, wine_id=wine_id,
                )
                await _safe_edit(query,
                    text, parse_mode="HTML", reply_markup=markup,
                )
                await self._celebrate(context, query.message.chat_id, result)
            except StoreError:
                logger.exception("[tap] drank store")
                await self._resend_photo(query)
            except Exception as e:
                logger.exception("[tap] drank failed")
                await _safe_edit(query,f"Error: {e}")
            finally:
                self._release_card(query.message)
                if pending_claimed and barcode:
                    self._unclaim_pending(api, barcode)
            return

        if action == "la" or action == "aa":
            wine_id = data.get("wine_id")
            drank = action == "la"
            if not wine_id:
                await _safe_edit(query,"Couldn't log that.")
                return
            card = self._card_logged_item(query.message)
            if not card.get("guard"):
                await _safe_edit(query,
                    "That card's finished — send the photo again if you still want to add it."
                )
                return
            if card.get("item_id") and card.get("logged_another"):
                if drank:
                    await self._reshow_drank(query, card)
                else:
                    await _safe_edit(query,
                        f"📥 <b>Added:</b> {card.get('wine_name') or 'that wine'}\n"
                        f"📊 In stock: {card.get('stock_count') or 0}",
                        parse_mode="HTML",
                    )
                return
            if not self._claim_card(query.message):
                await self._tap_busy(query)
                return
            try:
                self._touch_label_card(query.message, in_progress=True)
                paid = self._paid_for(query.message, wine_id)
                result = api.add_stock(
                    wine_id, drank=drank, price=paid, confirm_duplicate=True,
                )
                failed = _stock_failed(result)
                if failed:
                    await _safe_edit(query,failed)
                    return
                name = result.get("wine_name") or ""
                item_id = result.get("item_id")
                self._touch_label_card(
                    query.message, item_id=item_id, wine_name=name,
                    stock_count=result.get("stock_count", 0),
                    drunk=drank, in_progress=False, guard=False,
                    logged_another=True, wine_id=wine_id,
                )
                if not item_id:
                    await _safe_edit(query,
                        f"\U0001F377 <b>Drank:</b> {name}\n"
                        f"\U0001F4CA In stock: {result.get('stock_count', 0)}"
                        if drank else f"📥 <b>Added:</b> {name}",
                        parse_mode="HTML",
                    )
                    await self._celebrate(context, query.message.chat_id, result)
                    return
                if drank:
                    text, markup = build_drank_view(
                        name, result.get("stock_count", 0), item_id, wine_id=wine_id,
                    )
                    await _safe_edit(query,
                        text, parse_mode="HTML", reply_markup=markup,
                    )
                else:
                    await _safe_edit(query,
                        f"📥 <b>Added:</b> {name}\n"
                        f"📊 In stock: {result.get('stock_count', 0)}",
                        parse_mode="HTML",
                    )
                await self._celebrate(context, query.message.chat_id, result)
            except StoreError:
                logger.exception("[tap] log-another store")
                await self._resend_photo(query)
            except Exception as e:
                logger.exception("[tap] log-another failed")
                await _safe_edit(query,f"Error: {e}")
            finally:
                self._release_card(query.message)
            return

        if action == "ok":
            wine_id = data.get("wine_id")
            item_id = data.get("item_id")
            name = "that wine"
            stock = 0
            if wine_id:
                try:
                    detail = api.get_wine_detail(wine_id) or {}
                    name = detail.get("name") or name
                    stock = detail.get("stock_count") or 0
                except Exception:
                    pass
            if item_id:
                text, markup = build_drank_view(
                    name, stock, item_id, wine_id=wine_id,
                )
                await _safe_edit(query,
                    text, parse_mode="HTML", reply_markup=markup,
                )
            else:
                await _safe_edit(query,
                    f"\U0001F377 <b>Drank:</b> {name}\n"
                    f"\U0001F4CA In stock: {stock}",
                    parse_mode="HTML",
                )
            return

        if action == "look":
            # "Just looking" means the bottle isn't going in the cellar — not
            # that the user is done reading what we found. Keep the card, drop
            # the buttons. If a reference lookup is still running it will
            # re-render this same text with the ratings added.
            key = self._card_key(query.message) if query.message else ""
            entry = self._touch_label_card(query.message, looking=True)
            wine = (entry or {}).get("wine") or {}
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    functools.partial(api.record_intent, "look", wine=wine),
                )
            except Exception as e:
                logger.warning(f"[taste] look intent failed: {e}")
            text = (query.message.text_html if query.message else "") or ""
            text = text.replace(REFS_PENDING_NOTE.strip(), "").rstrip()
            try:
                await _safe_edit(query,
                    text + JUST_LOOKING_NOTE, parse_mode="HTML",
                )
            except Exception as e:
                logger.info(f"[callback] just-looking edit failed: {e}")
            return

        if action == "wr":
            # Photo/barcode read was the wrong wine — accept a typed name next.
            barcode = data.get("barcode", "")
            grok = {}
            pending = {}
            if barcode:
                pending = self._get_pending(api, barcode) or {}
                grok = pending.get("grok_merged") or {}
            wrong = grok.get("name") or ""
            real_barcode = "" if str(barcode).startswith("_") else barcode
            self._start_misread(
                update.effective_chat.id,
                barcode=real_barcode or barcode,
                wrong_name=wrong,
                context={**grok, "barcode": real_barcode or barcode},
                photo_data=pending.get("photo_front") or pending.get("photo_data") or b"",
            )
            had = f"I had it as <b>{wrong}</b>" if wrong else "That reading was wrong"
            await _safe_edit(query,
                f"\u2717 {had}.\n\n"
                f"Type the producer, name and vintage and I'll look it up — "
                f"or send a closer label photo.",
                parse_mode="HTML",
            )
            return

        if action == "x":
            barcode = data.get("barcode", "")
            if barcode:
                self._resolve_pending(api, barcode)
            await _safe_edit(query,"Cancelled.")
            return

        if action == "skip":
            barcode = data.get("barcode", "")
            self._resolve_pending(api, barcode)
            await _safe_edit(query,"Skipped.")
            return

        # --- Stock add: show storage picker ---
        if action == "in":
            wine_id = data["wine_id"]
            if not self._claim_card(query.message):
                await self._tap_busy(query)
                return
            try:
                card = self._card_logged_item(query.message)
                if card.get("item_id") and not card.get("drunk") and not card.get("guard"):
                    await _safe_edit(query,
                        f"📥 <b>Added:</b> {card.get('wine_name') or 'that wine'}\n"
                        f"📊 In stock: {card.get('stock_count') or 0}",
                        parse_mode="HTML",
                    )
                    return
                storages = api.list_storages()
                if len(storages) <= 1:
                    # Only one storage — skip picker, add directly
                    storage = storages[0] if storages else None
                    storage_name = storage["name"] if storage else api.default_storage
                    drawers = storage.get("drawers", 0) if storage else 0
                    result = api.add_stock(
                        wine_id, storage_name=storage_name,
                        price=self._paid_for(query.message, wine_id),
                        check_duplicate=bool(self._card_logged_item(query.message)),
                    )
                    if result.get("error") == "recent_duplicate":
                        self._touch_label_card(
                            query.message, guard=True, wine_id=wine_id,
                            wine_name=result.get("wine_name"), in_progress=False,
                        )
                        await _safe_edit(query,
                            f"Already added <b>{result.get('wine_name') or 'that wine'}</b>"
                            " a moment ago.\n\nAdd another bottle?",
                            parse_mode="HTML",
                            reply_markup=_duplicate_guard_markup(
                                wine_id, result.get("item_id"), drank=False,
                            ),
                        )
                        return
                    if "error" in result:
                        await _safe_edit(query,f"⚠️ {result['error']}")
                    else:
                        self._touch_label_card(
                            query.message, item_id=result.get("item_id"),
                            wine_name=result.get("wine_name"),
                            stock_count=result.get("stock_count", 0),
                            committed=True, drunk=False, in_progress=False,
                            wine_id=wine_id,
                        )
                        await self._celebrate(context, update.effective_chat.id, result)
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
                            await _safe_edit(query,
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
                            await _safe_edit(query,
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
                await _safe_edit(query,
                    "📍 <b>Where are you storing this?</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except StoreError:
                raise
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")
            finally:
                self._release_card(query.message)

        # --- Stock add with chosen storage ---
        elif action == "si":
            wine_id = data["wine_id"]
            storage_id = data.get("page", 0)  # reusing page field for storage_id
            if not self._claim_card(query.message):
                await self._tap_busy(query)
                return
            try:
                card = self._card_logged_item(query.message)
                if card.get("item_id") and not card.get("drunk") and not card.get("guard"):
                    await _safe_edit(query,
                        f"📥 <b>Added:</b> {card.get('wine_name') or 'that wine'}\n"
                        f"📊 In stock: {card.get('stock_count') or 0}",
                        parse_mode="HTML",
                    )
                    return
                # Look up storage name from id
                storages = api.list_storages()
                storage = next((s for s in storages if s["id"] == storage_id), None)
                storage_name = storage["name"] if storage else api.default_storage
                drawers = storage.get("drawers", 0) if storage else 0

                result = api.add_stock(
                    wine_id, storage_name=storage_name,
                    price=self._paid_for(query.message, wine_id),
                    check_duplicate=bool(self._card_logged_item(query.message)),
                )
                if result.get("error") == "recent_duplicate":
                    self._touch_label_card(
                        query.message, guard=True, wine_id=wine_id,
                        wine_name=result.get("wine_name"), in_progress=False,
                    )
                    await _safe_edit(query,
                        f"Already added <b>{result.get('wine_name') or 'that wine'}</b>"
                        " a moment ago.\n\nAdd another bottle?",
                        parse_mode="HTML",
                        reply_markup=_duplicate_guard_markup(
                            wine_id, result.get("item_id"), drank=False,
                        ),
                    )
                    return
                if "error" in result:
                    await _safe_edit(query,f"⚠️ {result['error']}")
                else:
                    self._touch_label_card(
                        query.message, item_id=result.get("item_id"),
                        wine_name=result.get("wine_name"),
                        stock_count=result.get("stock_count", 0),
                        committed=True, drunk=False, in_progress=False,
                        wine_id=wine_id,
                    )
                    await self._celebrate(context, update.effective_chat.id, result)
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
                        await _safe_edit(query,
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
                        keyboard.extend(_wine_followup_rows(wine_id))
                        await _safe_edit(query,
                            _append_wine_url(
                                f"📥 <b>Added:</b> {result['wine_name']}\n"
                                f"📍 {storage_name}\n"
                                f"📊 In stock: {result['stock_count']}",
                                api, wine_id,
                            ),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
            except StoreError:
                raise
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")
            finally:
                self._release_card(query.message)

        # --- Set drawer after stock-in ---
        elif action == "sd":
            item_id = data.get("item_id")
            row = data.get("row")
            wine_id = data.get("wine_id")
            try:
                # Only show the "looking up" tail if there's actually an enrich
                # in flight for this wine (i.e. it was just created from a scan,
                # not when adding stock to a wine that already exists).
                tail = ""
                if wine_id and wine_id in self._enriching_wine_ids:
                    tail = (
                        "\n\n⏳ <i>Looking up ratings and price — these will arrive "
                        "in a follow-up message in a few seconds.</i>"
                    )
                if row and row > 0 and item_id:
                    api.move_item(item_id, row=row)
                    placed = f"✅ Placed in drawer {row}.{tail}"
                else:
                    placed = f"✅ Added (no drawer assigned).{tail}"
                if data.get("original") == "in":
                    await self._redraw_scan_card(
                        query, api, action="in", wine_id=wine_id,
                        barcode=data.get("barcode", ""), item_id=item_id,
                    )
                else:
                    markup = InlineKeyboardMarkup(_wine_followup_rows(wine_id)) if wine_id else None
                    await _safe_edit(query,
                        _append_wine_url(placed, api, wine_id) if wine_id else placed,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
            except Exception as e:
                await _safe_edit(query,f"Error setting drawer: {e}")

        # --- Fridge-scan card: pick a different storage for this bottle ---
        elif action == "sp":
            item_id = data.get("item_id")
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            if not wine_id:
                await _safe_edit(query,"That card's out of date — send /stock again.")
                return
            try:
                storages = api.list_storages()
                keyboard = []
                for s in storages:
                    keyboard.append([InlineKeyboardButton(
                        s["name"],
                        callback_data=_cb("sl", item_id=item_id, page=s["id"], wine_id=wine_id, barcode=barcode),
                    )])
                keyboard.append([InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=_cb("sc", item_id=item_id, wine_id=wine_id, barcode=barcode, original="in"),
                )])
                await _safe_edit(query,
                    "\U0001F4E6 <b>Move this bottle to:</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        elif action == "sl":
            item_id = data.get("item_id")
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            storage_id = data.get("page", 0)
            if not wine_id:
                await _safe_edit(query,"That card's out of date — send /stock again.")
                return
            try:
                storages = api.list_storages()
                storage = next((s for s in storages if s["id"] == storage_id), None)
                if storage and item_id:
                    api.move_item(item_id, storage_name=storage["name"])
                await self._redraw_scan_card(
                    query, api, action="in", wine_id=wine_id,
                    barcode=barcode, item_id=item_id,
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        elif action == "dp":
            item_id = data.get("item_id")
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            try:
                storages = api.list_storages()
                wine = api.get_wine_detail(wine_id) or {}
                item = next((i for i in wine.get("items") or [] if i.get("item_id") == item_id), None)
                storage_name = (item or {}).get("storage") or ""
                match = next((s for s in storages if s["name"] == storage_name), None)
                drawers = (match or {}).get("drawers") or 0
                keyboard = []
                row_buttons = []
                for d in range(1, drawers + 1):
                    row_buttons.append(InlineKeyboardButton(
                        str(d),
                        callback_data=_cb("sd", item_id=item_id, row=d, wine_id=wine_id, original="in"),
                    ))
                    if len(row_buttons) == 4:
                        keyboard.append(row_buttons)
                        row_buttons = []
                if row_buttons:
                    keyboard.append(row_buttons)
                keyboard.append([InlineKeyboardButton(
                    "Skip",
                    callback_data=_cb("sd", item_id=item_id, row=-1, wine_id=wine_id, original="in"),
                )])
                keyboard.append([InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=_cb("sc", item_id=item_id, wine_id=wine_id, barcode=barcode, original="in"),
                )])
                await _safe_edit(query,
                    "\U0001F5C4 <b>Which drawer?</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        elif action == "sc":
            await self._redraw_scan_card(
                query, api,
                action=data.get("original") or "in",
                wine_id=data.get("wine_id"),
                barcode=data.get("barcode", ""),
                item_id=data.get("item_id"),
                storage_id=data.get("page"),
            )

        elif action == "rf":
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            from_id = data.get("page") or 0
            try:
                storages = api.list_storages()
                keyboard = []
                for s in storages:
                    if s["id"] == from_id:
                        continue
                    keyboard.append([InlineKeyboardButton(
                        s["name"],
                        callback_data=_cb("ro", wine_id=wine_id, page=s["id"], original=str(from_id), barcode=barcode),
                    )])
                keyboard.append([InlineKeyboardButton(
                    "\u2b05\ufe0f Back",
                    callback_data=_cb("sc", wine_id=wine_id, barcode=barcode, original="out", page=from_id),
                )])
                if len(keyboard) == 1:
                    await _safe_edit(query,
                        "There's only one fridge in this cellar.",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                else:
                    await _safe_edit(query,
                        "\U0001F4E4 <b>That bottle actually left:</b>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        elif action == "ro":
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            to_id = data.get("page", 0)
            from_id = int(data.get("original") or 0)
            try:
                storages = api.list_storages()
                dest = next((s for s in storages if s["id"] == to_id), None)
                src = next((s for s in storages if s["id"] == from_id), None)
                if not dest:
                    await _safe_edit(query,"Couldn't find that fridge.")
                    return
                # Take from the chosen fridge first. Put the default-fridge
                # bottle back only after that succeeds, so a miss doesn't
                # invent stock in the fridge the gun just emptied.
                result = api.remove_stock(
                    wine_id, storage_name=(dest["name"] if dest else None),
                )
                if "error" in result:
                    await _safe_edit(query,f"\u26a0\ufe0f {result['error']}")
                else:
                    if src:
                        put_back = api.add_stock(wine_id, storage_name=src["name"])
                        failed = _stock_failed(put_back)
                        if failed:
                            rollback = api.add_stock(
                                wine_id, storage_name=dest["name"],
                            )
                            if _stock_failed(rollback):
                                await _safe_edit(query,
                                    f"Couldn't finish the move. The bottle left "
                                    f"<b>{dest['name']}</b> and isn't back yet — "
                                    f"add it in {dest['name']} or {src['name']}.",
                                    parse_mode="HTML",
                                )
                            else:
                                await _safe_edit(query,
                                    f"Couldn't put that back in <b>{src['name']}</b>, "
                                    f"so it's still in {dest['name']}.",
                                    parse_mode="HTML",
                                )
                            return
                        stock_count = put_back.get("stock_count", result.get("stock_count"))
                    else:
                        stock_count = result.get("stock_count") or 0
                    text, markup = build_stock_update_view(
                        "out", result.get("wine_name") or "",
                        stock_count,
                        wine_id=wine_id, barcode=barcode,
                        storage=result.get("storage") or "",
                        storage_id=result.get("storage_id"),
                        item_id=result.get("item_id"),
                        row=result.get("row"),
                    )
                    await _safe_edit(query,
                        text, parse_mode="HTML", reply_markup=markup,
                    )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Stock remove ---
        elif action == "out":
            wine_id = data["wine_id"]
            try:
                result = api.remove_stock(wine_id)
                if "error" in result:
                    await _safe_edit(query,f"⚠️ {result['error']}")
                else:
                    item_id = result.get("item_id")
                    if item_id:
                        undo_cb = _cb("ul", wine_id=wine_id, item_id=item_id)
                    else:
                        undo_cb = _cb("ur", wine_id=wine_id)
                    keyboard = [[InlineKeyboardButton(
                        "↩️ Undo", callback_data=undo_cb,
                    )]]
                    text = (
                        f"📤 <b>Removed:</b> {result['wine_name']}\n"
                        f"📊 In stock: {result['stock_count']}"
                    )
                    if item_id:
                        text += f"\n\n{VOTE_PROMPT}"
                        keyboard.append(vote_keyboard_row(item_id))
                    await _safe_edit(query,
                        text,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- "It's a new/different wine" with a duplicate barcode ---
        elif action == "nw":
            barcode = data["barcode"]
            self._create_pending(api, barcode)
            self._start_misread(
                update.effective_chat.id,
                barcode=barcode,
                context={"barcode": barcode},
            )
            await _safe_edit(query,
                f"🆕 <b>New wine with barcode <code>{barcode}</code></b>\n\n"
                f"📸 <b>Reply to this message</b> with a label photo, "
                f"or type the producer, name and vintage and I'll look it up.\n\n"
                f"A separate wine entry will be created.",
                parse_mode="HTML",
            )

        # --- Undo add (reverse = remove) ---
        elif action == "ua":
            wine_id = data["wine_id"]
            try:
                result = api.remove_stock(wine_id)
                if "error" in result:
                    await _safe_edit(query,f"⚠️ Could not undo: {result['error']}")
                else:
                    await _safe_edit(query,
                        f"↩️ <b>Undone.</b> {result['wine_name']}\n"
                        f"📊 In stock: {result['stock_count']}",
                        parse_mode="HTML",
                    )
            except Exception as e:
                await _safe_edit(query,f"Error undoing: {e}")

        # --- Undo remove (reverse = add) ---
        elif action == "ur":
            wine_id = data["wine_id"]
            try:
                result = api.add_stock(wine_id)
                failed = _stock_failed(result)
                if failed:
                    await _safe_edit(query,f"⚠️ Could not undo: {failed}")
                    return
                await _safe_edit(query,
                    f"↩️ <b>Undone.</b> {result['wine_name']}\n"
                    f"📊 In stock: {result['stock_count']}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await _safe_edit(query,f"Error undoing: {e}")

        # --- Wrong wine: undo the stock change AND start new-wine photo flow ---
        elif action == "ww":
            wine_id = data["wine_id"]
            barcode = data.get("barcode", "")
            original = data.get("original", "in")
            undo_ok = True
            try:
                if original == "in":
                    undone = api.remove_stock(wine_id)
                    if _stock_failed(undone):
                        undo_ok = False
                else:
                    undone = api.add_stock(wine_id)
                    if _stock_failed(undone):
                        undo_ok = False
            except Exception as e:
                undo_ok = False
                logger.warning(f"Failed to undo stock change for wine {wine_id}: {e}")

            undo_line = "↩️ Stock change undone.\n\n" if undo_ok else "⚠️ Could not undo the stock change — please fix manually.\n\n"
            self._create_pending(api, barcode)
            wrong_name = ""
            try:
                detail = api.get_wine_detail(wine_id) if hasattr(api, "get_wine_detail") else None
                if detail:
                    wrong_name = detail.get("name") or ""
            except Exception:
                pass
            self._start_misread(
                update.effective_chat.id,
                barcode=barcode,
                wrong_name=wrong_name,
                context={"barcode": barcode},
            )
            await _safe_edit(query,
                f"{undo_line}"
                f"🆕 <b>New wine with barcode <code>{barcode}</code></b>\n\n"
                f"Type the producer, name and vintage and I'll look it up — "
                f"or <b>reply to this message</b> with a label photo.",
                parse_mode="HTML",
            )

        # --- Add with Open Food Facts data ---
        elif action == "off":
            barcode = data["barcode"]
            if not self._is_pending(api, barcode):
                logger.info("[tap] off gone barcode=%s", barcode)
                await _safe_edit(query,_finished_copy(barcode))
                return

            off_data = self._get_pending(api, barcode).get("off_data", {})
            if not off_data:
                await _safe_edit(query,"No OFF data found for this barcode.")
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
                failed = _stock_failed(stock)
                if failed:
                    self._resolve_pending(api, barcode)
                    await _safe_edit(query,
                        f"Saved <b>{result.get('name') or 'that wine'}</b> "
                        f"but couldn't add a bottle.\n\n⚠️ {failed}",
                        parse_mode="HTML",
                        reply_markup=_retry_add_markup(result["id"]),
                    )
                    return
                self._resolve_pending(api, barcode)
                await _safe_edit(query,
                    f"🍷 <b>Wine added:</b> {result['name']}\n"
                    f"📊 In stock: {stock['stock_count']}\n\n"
                    f"⏳ <i>Looking up ratings and price — these will arrive in a follow-up message in a few seconds.</i>",
                    parse_mode="HTML",
                )
                await self._celebrate(context, update.effective_chat.id, stock)
                # Background live-search enrichment — sends a follow-up message when done
                self._kick_enrich(result["id"], update.effective_chat.id, api, result.get("name", ""))
            except StoreError:
                raise
            except Exception as e:
                await _safe_edit(query,f"Error creating wine: {e}")

        # --- Add manually via web UI ---
        elif action == "man":
            barcode = data["barcode"]
            if not self._is_pending(api, barcode):
                logger.info("[tap] man gone barcode=%s", barcode)
                await _safe_edit(query,_finished_copy(barcode))
                return
            self._resolve_pending(api, barcode)
            await _safe_edit(query,
                f"Add manually in Wine Cellar:\n"
                f"🔗 {api.web_url}/wine/add/{barcode}",
                parse_mode="HTML",
            )

        # --- Confirm Grok extraction and create wine ---
        elif action in ("grok", "np", "gp"):
            barcode = data["barcode"]
            producer_override = None
            if action == "gp":
                pending = self._get_pending(api, barcode)
                producer_override = _suggested_producer(
                    (pending.get("grok_merged") or {}).get("name") or "",
                ) or None
            await self._add_pending_wine(
                query, api, context, barcode,
                skip_producer=(action == "np"),
                producer_override=producer_override,
                original=data.get("original") or "",
                tap_action=action,
            )

        elif action == "ap":
            barcode = data.get("barcode") or ""
            chat_id = str(query.message.chat_id)
            self._await_side_photo[chat_id] = {
                "barcode": barcode, "time": time.time(),
            }
            await query.answer("Send the other side of the bottle — I'll attach it.")

        elif action == "yb":
            barcode = data.get("barcode") or ""
            chat_id = str(query.message.chat_id)
            photo = getattr(self, "_pending_side_bytes", {}).pop(chat_id, b"")
            getattr(self, "_await_side_photo", {}).pop(chat_id, None)
            if not photo:
                await _safe_edit(query,"Send the photo again and I'll attach it.")
                return
            await _safe_edit(query,"Attaching the other side\u2026")
            detected = await self._decode_photo_barcode(photo)
            await self._attach_side_photo(
                _callback_status(query), barcode, photo, update,
                force=True, detected=detected,
            )

        elif action == "nd":
            chat_id = str(query.message.chat_id)
            photo = getattr(self, "_pending_side_bytes", {}).pop(chat_id, b"")
            getattr(self, "_await_side_photo", {}).pop(chat_id, None)
            if not photo:
                await _safe_edit(query,"Send a photo of the other bottle.")
                return
            await _safe_edit(query,"📷 Analysing photo...")
            grok_data = await self._analyse_photo_bytes(photo, update) or {}
            grok_data.setdefault("type", "label")
            await self._handle_triage_label(
                _callback_status(query), grok_data, photo_data=photo, update=update,
            )

        # --- Retry with a new photo ---
        elif action == "retry":
            barcode = data["barcode"]
            if not self._is_pending(api, barcode):
                logger.info("[tap] retry gone barcode=%s", barcode)
                await _safe_edit(query,_finished_copy(barcode))
                return
            await _safe_edit(query,
                f"📸 Send another photo — <b>reply to this message</b> "
                f"with a label photo.\n\nBarcode: <code>{barcode}</code>",
                parse_mode="HTML",
            )

        # --- Browse: drill into a group ---
        elif action == "sd2":
            session_key = data.get("original", "")
            session = self._browse_sessions.get(session_key)
            if not session:
                await _safe_edit(query,"Session expired. Use /stock again.")
                return
            idx = data.get("page")
            groups = getattr(session, "group_values", None) or []
            if idx is None or idx < 0 or idx >= len(groups):
                await _safe_edit(query,"That group is out of date — send /stock again.")
                return
            dim, value = groups[idx]
            session.add_filter(dim, value)
            try:
                text, keyboard = _build_browse_view(session, session_key)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Browse: regroup by a different dimension ---
        elif action == "sg":
            parts = data.get("original", "").split(":", 2)
            if len(parts) < 3:
                await _safe_edit(query,"Invalid browse action.")
                return
            session_key = f"{parts[0]}:{parts[1]}"
            dim = parts[2]
            session = self._browse_sessions.get(session_key)
            if not session:
                await _safe_edit(query,"Session expired. Use /stock again.")
                return
            # Rebuild the grouped view with the chosen dimension
            wines = session.filtered_wines
            total_bottles = sum(w.get("stock_count", 0) for w in wines)
            try:
                text, keyboard = _build_browse_groups(session, session_key, wines, dim, total_bottles)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Browse: list all (flat, paginated) ---
        elif action == "bl":
            session_key = data.get("original", "")
            session = self._browse_sessions.get(session_key)
            if not session:
                await _safe_edit(query,"Session expired. Use /stock again.")
                return
            session.page = 0
            try:
                wines = session.filtered_wines
                text, keyboard = _build_browse_list(session, session_key, wines)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Browse: page navigation ---
        elif action == "bp":
            parts = data.get("original", "").rsplit(":", 1)
            if len(parts) < 2:
                await _safe_edit(query,"Invalid page action.")
                return
            session_key = parts[0]
            page = int(parts[1])
            session = self._browse_sessions.get(session_key)
            if not session:
                await _safe_edit(query,"Session expired. Use /stock again.")
                return
            session.page = page
            try:
                wines = session.filtered_wines
                text, keyboard = _build_browse_list(session, session_key, wines)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Browse: back one level ---
        elif action == "sb":
            session_key = data.get("original", "")
            session = self._browse_sessions.get(session_key)
            if not session:
                await _safe_edit(query,"Session expired. Use /stock again.")
                return
            session.remove_last_filter()
            try:
                text, keyboard = _build_browse_view(session, session_key)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

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
                    await _safe_edit(query,f"Error: {e}")
            else:
                try:
                    wines = api.wines_in_stock()
                except Exception as e:
                    await _safe_edit(query,f"Error loading fridge: {e}")
                    return
                if not wines:
                    await _safe_edit(query,
                        empty_cellar()[0], reply_markup=empty_cellar()[1], parse_mode="HTML",
                    )
                    return
                text, keyboard = self._build_fridge_page(wines, page=page)
                await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")

        # --- Fridge: +1 stock ---
        elif action == "+":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            session_key, session = self._get_browse_session(query)
            try:
                result = None
                last = getattr(session, "last_removed", None) if session else None
                item_id = None
                if last and last.get("wine_id") == wine_id:
                    item_id = last.get("item_id")
                if item_id:
                    result = api.restore_stock(item_id)
                if not result or result.get("error"):
                    if hasattr(api, "restore_latest"):
                        result = api.restore_latest(wine_id)
                if result and result.get("action") == "restored":
                    if session:
                        session.last_removed = None
                else:
                    storage_name = None
                    if session and session.storage_name not in (None, "", "All storages"):
                        storage_name = session.storage_name
                    elif last and last.get("storage"):
                        storage_name = last.get("storage")
                    result = api.add_stock(wine_id, storage_name=storage_name)
                failed = _stock_failed(result)
                if failed:
                    await query.answer(failed, show_alert=True)
                    return
                verb = "Put back" if result.get("action") == "restored" else "Added"
                await query.answer(f"{verb}: {result['wine_name']} ({result['stock_count']}x)")
                await self._celebrate(context, update.effective_chat.id, result)
                if session:
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
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
                    session.last_removed = {
                        "wine_id": wine_id,
                        "name": result.get("wine_name") or "that wine",
                        "item_id": result.get("item_id"),
                        "storage": result.get("storage"),
                    }
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    wines = api.wines_in_stock()
                    if not wines:
                        text, kb = last_bottle_removed_view(
                            wine_id, result.get("wine_name") or "that wine",
                            item_id=result.get("item_id"),
                        )
                        await _safe_edit(query,
                            text, reply_markup=kb, parse_mode="HTML",
                        )
                        return
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Undo a recent remove/drink by restoring the same StorageItem ---
        elif action == "ul":
            wine_id = data.get("wine_id")
            item_id = data.get("item_id")
            session_key, session = self._get_browse_session(query)
            if not item_id:
                if session:
                    await query.answer(
                        "Too late to undo, use + to add it back", show_alert=True,
                    )
                else:
                    await _safe_edit(query,
                        "Too late to undo, use + to add it back",
                    )
                return
            if not self._claim_card(query.message):
                await self._tap_busy(query)
                return
            await asyncio.sleep(0)
            try:
                result = api.restore_stock(item_id)
                err = result.get("error") if isinstance(result, dict) else None
                if err == "already_restored":
                    if session:
                        session.last_removed = None
                        try:
                            await self._refresh_browse_session(
                                query, session_key, session,
                            )
                        except Exception:
                            pass
                        await query.answer("Already put back")
                    else:
                        await _safe_edit(query,"Already put back")
                    return
                if err == "too_late":
                    msg = "Too late to undo, use + to add it back"
                    if session:
                        await query.answer(msg, show_alert=True)
                    else:
                        await _safe_edit(query,msg)
                    return
                if err:
                    failed = err if isinstance(err, str) else "Could not undo"
                    if session:
                        await query.answer(failed, show_alert=True)
                    else:
                        await _safe_edit(query,f"⚠️ Could not undo: {failed}")
                    return
                name = result.get("wine_name") or "that wine"
                stock = result.get("stock_count")
                if session:
                    session.last_removed = None
                    await query.answer(f"Put back: {name} ({stock}x)")
                    await self._refresh_browse_session(query, session_key, session)
                else:
                    await _safe_edit(query,
                        f"↩️ <b>Put back:</b> {name}\n"
                        f"📊 In stock: {stock}",
                        parse_mode="HTML",
                    )
            except Exception as e:
                if session:
                    await query.answer(f"Error: {e}", show_alert=True)
                else:
                    await _safe_edit(query,f"Error undoing: {e}")
            finally:
                self._release_card(query.message)

        # --- Fridge: show drawer assignment for a wine ---
        elif action == "dw":
            wine_id = data["wine_id"]
            try:
                text, keyboard = self._build_drawer_view(wine_id, api=api)
                if text:
                    await _safe_edit(query,
                        text, reply_markup=keyboard, parse_mode="HTML",
                    )
                else:
                    await _safe_edit(query,"Wine not found in stock.")
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Wine details (compact card) ---
        elif action == "det":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await _safe_edit(query,"Wine not found.")
                    return
                text, keyboard = self._build_detail_card(wine, api=api)
                await _safe_edit(query,
                    text, parse_mode="HTML", reply_markup=keyboard,
                )
            except Exception as e:
                try:
                    await _safe_edit(query,f"Error: {e}")
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
                    await _safe_edit(query,"Wine not found.")
                    return

                text = f"📝 <b>{wine['name']}</b>\n\n"

                producer = wine.get("producer", "")
                if producer:
                    text += f"👤 {producer}\n\n"
                comment = wine.get("comment", "")
                text += f"{comment}\n" if comment else "<i>No tasting notes yet.</i>\n"

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
                await _safe_edit(query,
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Food pairings ---
        elif action == "pr":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await _safe_edit(query,"Wine not found.")
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
                await _safe_edit(query,
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Wine photo ---
        elif action == "ph":
            wine_id = data["wine_id"]
            try:
                wine = api.get_wine_detail(wine_id)
                wine_name = wine["name"] if wine else f"Wine #{wine_id}"

                images = []
                fetch_error = None
                try:
                    images = api.get_wine_images(wine_id)
                    logger.info(f"[PHOTO] get_wine_images({wine_id}) returned {len(images)} image(s)")
                except Exception as e:
                    logger.error(f"[PHOTO] Failed to fetch image list for wine #{wine_id}: {e}")
                    fetch_error = e

                if images:
                    image_id = images[0].get("id")
                    if image_id:
                        try:
                            photo_bytes = api.get_image_file(image_id)
                            logger.info(f"[PHOTO] Downloaded image #{image_id}: {len(photo_bytes)} bytes")
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
                            await _safe_edit(query,
                                f"\U0001F4F8 {wine_name} \u2014 photo below \u2b07\ufe0f"
                            )
                            return
                        except Exception as e:
                            logger.error(f"[PHOTO] Failed to send wine image #{image_id}: {e}")

                keyboard = [
                    [InlineKeyboardButton("📸 Upload label photo", callback_data=_cb("ed", wine_id=wine_id))],
                    [InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id))],
                ]
                if fetch_error:
                    msg = f"📸 <b>{wine_name}</b>\n\nCould not load photo — try again later."
                else:
                    msg = f"📸 <b>{wine_name}</b>\n\nNo photo yet."
                await _safe_edit(query,
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

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
                    text, keyboard = self._build_detail_card(wine, api=api)
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
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await _safe_edit(query,"Wine not found in stock.")
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

                await _safe_edit(query,
                    text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

        # --- Move all items to chosen storage ---
        elif action == "ma":
            wine_id = data["wine_id"]
            storage_id = data.get("page", 0)
            try:
                storages = api.list_storages()
                storage = next((s for s in storages if s["id"] == storage_id), None)
                storage_name = storage["name"] if storage else api.default_storage

                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await _safe_edit(query,"Wine not found in stock.")
                    return

                moved = 0
                for item in wine.get("items", []):
                    if item.get("storage") != storage_name:
                        api.move_item(item["item_id"], storage_name=storage_name)
                        moved += 1

                await _safe_edit(query,
                    f"📦 <b>Moved:</b> {wine['name']}\n"
                    f"📍 → {storage_name}\n"
                    f"({moved} bottle(s) moved)",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Back", callback_data=_cb("det", wine_id=wine_id)),
                    ]]),
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

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
                        await _safe_edit(query,
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
                    await _safe_edit(query,text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Fridge: set quantity (show picker) ---
        elif action == "sq":
            wine_id = data["wine_id"]
            page = data.get("page", 0)
            try:
                wine = api.get_wine_detail(wine_id)
                if not wine:
                    await _safe_edit(query,"Wine not found in stock.")
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
                await _safe_edit(query,
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
            except Exception as e:
                await _safe_edit(query,f"Error: {e}")

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
                        await _safe_edit(query,
                            empty_cellar()[0], reply_markup=empty_cellar()[1], parse_mode="HTML",
                        )
                        return
                    total_pages = max(1, (len(wines) + self.FRIDGE_PAGE_SIZE - 1) // self.FRIDGE_PAGE_SIZE)
                    page = min(page, total_pages - 1)
                    text, keyboard = self._build_fridge_page(wines, page=page)
                    await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        # --- Refresh references (re-ask Grok for critic / Vivino / market price) ---
        elif action == "rr":
            wine_id = data["wine_id"]
            await query.answer()  # acknowledge the tap; further feedback comes via message edits

            # Show in-flight status by editing the existing card message
            original_text = query.message.text_html if query.message else ""
            original_markup = query.message.reply_markup if query.message else None
            try:
                await _safe_edit(query,
                    f"🔄 <i>Asking Grok for references…</i>\n\n{original_text}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    None, functools.partial(api.refresh_references, wine_id),
                )
            except Exception as e:
                logger.error(f"[rr] refresh_references failed: {e}")
                # Restore the card and show the error as a popup-style status line
                try:
                    await _safe_edit(query,
                        f"⚠️ <b>Error refreshing references:</b> {e}\n\n{original_text}",
                        reply_markup=original_markup,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            if not result.get("updated"):
                msg = result.get("message") or "Grok couldn't confidently identify the wine."
                confidence = result.get("confidence", "low")
                # Restore the card with a status banner so the user sees what happened
                try:
                    await _safe_edit(query,
                        f"⚠️ <b>No update</b> (confidence: {confidence})\n<i>{msg}</i>\n\n{original_text}",
                        reply_markup=original_markup,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            # Updated — reload the wine detail card with the fresh data
            try:
                wine = api.get_wine_detail(wine_id)
                if wine:
                    text, keyboard = self._build_detail_card(wine, api=api)
                    confidence = result.get("confidence", "")
                    text = f"✅ <b>References updated</b> (confidence: {confidence})\n\n{text}"
                    await _safe_edit(query,text, reply_markup=keyboard, parse_mode="HTML")
                    return
            except Exception as e:
                logger.warning(f"[rr] reload failed: {e}")

            # Fallback if reload failed — show a one-liner with the new values
            confidence = result.get("confidence", "")
            critic = result.get("critic_score")
            vivino = result.get("vivino_rating")
            market = result.get("market_price")
            currency = result.get("market_price_currency", "")
            parts = []
            if critic is not None:
                parts.append(f"⭐ {critic}/100")
            if vivino is not None:
                parts.append(f"🍇 {vivino}/5")
            if market is not None:
                parts.append(f"💰 ~{market:.0f} {currency}")
            summary = " · ".join(parts) if parts else "(no values returned)"
            try:
                await _safe_edit(query,
                    f"✅ <b>References updated</b> ({confidence}): {summary}\n\n{original_text}",
                    reply_markup=original_markup,
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # --- Fridge: edit wine details / add photo after create ---
        elif action in ("ed", "ai"):
            await self._start_edit_photo_flow(query, api, data["wine_id"])

        # --- Confirm wine update from Grok ---
        elif action == "edc":
            wine_id = data["wine_id"]
            edit_key = f"_edit_{wine_id}"
            with self._lock:
                edit_data = dict(self.pending_store.get(edit_key, {}))
                grok_data = edit_data.get("grok_merged")
            if not grok_data:
                await _safe_edit(query,"Error: no extracted data found.")
                return
            try:
                current = {}
                try:
                    current = api.get_wine_detail(wine_id) or {}
                except Exception:
                    current = {}
                wine_fields = _clean_wine_data(grok_data)
                grok_bc = grok_data.get("barcode")
                if grok_bc:
                    wine_fields["barcode"] = grok_bc
                fills, overwrites = proposed_wine_update(current, wine_fields)
                apply = dict(fills)
                if data.get("original") == "all":
                    for field, _old, new in overwrites:
                        apply[field] = new
                elif overwrites and data.get("original") != "fill":
                    lines = ["This would change filled fields:"]
                    for field, old, new in overwrites:
                        lines.append(f"• {field}: {old} → {new}")
                    lines.append("\nI won't overwrite those without a tap.")
                    await _safe_edit(query,
                        "\n".join(lines),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "Yes, apply those changes",
                                callback_data=_cb("edc", wine_id=wine_id, original="all"),
                            )],
                            [InlineKeyboardButton(
                                "Only fill blanks",
                                callback_data=_cb("edc", wine_id=wine_id, original="fill"),
                            )],
                            [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
                        ]),
                    )
                    return
                result = api.update_wine(wine_id, apply) if apply else current

                photo = edit_data.get("photo_data")
                if photo:
                    try:
                        api.upload_wine_image(wine_id, photo)
                    except Exception as e:
                        logger.warning(f"[PHOTO] Failed to upload image: {e}")

                with self._lock:
                    self.pending_store.pop(edit_key, None)
                await _safe_edit(query,
                    f"✅ <b>Updated:</b> {result.get('name', 'Wine')}\n"
                    f"📊 In stock: {result.get('stock_count', '?')}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await _safe_edit(query,f"Error updating wine: {e}")

        elif action == "link":
            # Show list of existing wines to link a barcode to
            barcode = data.get("barcode", "")
            page = data.get("page", 0)
            if not barcode:
                await _safe_edit(query,"Error: no barcode to link.")
                return
            try:
                wines = api.wines_in_stock()
            except Exception as e:
                await _safe_edit(query,f"Error loading wines: {e}")
                return
            if not wines:
                await _safe_edit(query,
                    "No wines in your cellar yet — nothing to link to.\n"
                    "Add a wine first, then scan the barcode.",
                )
                return

            # Check OFF data for a suggested match
            suggested_id = None
            off_data = self._get_pending(api, barcode).get("off_data")
            if off_data and off_data.get("name"):
                off_name = off_data["name"].lower().strip()
                skip_words = {"wine", "the", "de", "du", "le", "la", "les", "di", "del", "das", "der"}
                off_words = {w for w in off_name.split() if w not in skip_words and len(w) > 2}
                for wine in wines:
                    existing = wine["name"].lower().strip()
                    if off_name in existing or existing in off_name:
                        suggested_id = wine["id"]
                        break
                if not suggested_id and off_words:
                    best_score = 0
                    for wine in wines:
                        existing_words = {w for w in wine["name"].lower().split() if w not in skip_words and len(w) > 2}
                        overlap = len(off_words & existing_words)
                        if overlap > best_score and overlap >= 2:
                            best_score = overlap
                            suggested_id = wine["id"]

            # Build paginated list
            page_size = 6
            start = page * page_size
            page_wines = wines[start:start + page_size]

            keyboard = []
            # Show suggested match first (on page 0)
            if page == 0 and suggested_id:
                suggested = next((w for w in wines if w["id"] == suggested_id), None)
                if suggested:
                    label = f"⭐ {suggested['name']} ({suggested['stock_count']}x)"
                    keyboard.append([InlineKeyboardButton(
                        label, callback_data=_cb("lnk", wine_id=suggested["id"], barcode=barcode),
                    )])

            for wine in page_wines:
                if wine["id"] == suggested_id and page == 0:
                    continue  # already shown as suggestion
                vintage = wine.get("vintage", "")
                label = f"{wine['name']} {vintage} ({wine['stock_count']}x)".strip()
                keyboard.append([InlineKeyboardButton(
                    label, callback_data=_cb("lnk", wine_id=wine["id"], barcode=barcode),
                )])

            # Pagination buttons
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=_cb("link", barcode=barcode, page=page - 1)))
            if start + page_size < len(wines):
                nav.append(InlineKeyboardButton("Next ➡️", callback_data=_cb("link", barcode=barcode, page=page + 1)))
            if nav:
                keyboard.append(nav)

            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("skip", barcode=barcode))])

            off_hint = ""
            if page == 0 and suggested_id and off_data:
                off_hint = f"\n⭐ Suggested match from OFF: <b>{off_data['name']}</b>\n"

            await _safe_edit(query,
                f"🔗 <b>Link barcode <code>{barcode}</code> to which wine?</b>\n"
                f"{off_hint}"
                f"\nPage {page + 1}/{(len(wines) + page_size - 1) // page_size}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif action == "nn":
            barcode = data.get("barcode", "")
            off_data = self._get_pending(api, barcode).get("off_data")
            text, markup = unknown_barcode_prompt(barcode, off_data)
            await _safe_edit(query,text, parse_mode="HTML", reply_markup=markup)

        elif action == "lnk":
            wine_id = data.get("wine_id")
            barcode = data.get("barcode", "")
            if not wine_id or not barcode:
                await _safe_edit(query,"Error: missing wine or barcode.")
                return
            from integrations.barcodes import normalise_barcode
            label_card = str(barcode).startswith("_")
            stored = "" if label_card else (normalise_barcode(barcode) or barcode)
            card = self._card_logged_item(query.message)
            if card.get("linked") and card.get("wine_id") == wine_id:
                await _safe_edit(query,"Already linked.")
                return
            status, existing_id = self._claim_pending(api, barcode)
            if status == "busy":
                logger.info(
                    "[tap] lnk early-return reason=busy barcode=%s chat=%s",
                    barcode or "-", tap_chat,
                )
                await self._tap_busy(query)
                return
            if status == "created":
                if existing_id and existing_id != wine_id:
                    added = ""
                    try:
                        prev = api.get_wine_detail(existing_id) or {}
                        added = prev.get("name") or ""
                    except Exception:
                        added = ""
                    if not added:
                        added = (
                            self._card_logged_item(query.message).get("wine_name")
                            or f"Wine #{existing_id}"
                        )
                    await _safe_edit(query,
                        f"This barcode was already added as "
                        f"{html.escape(str(added), quote=False)}"
                    )
                    return
                await _safe_edit(query,"Already linked.")
                return
            if status == "gone":
                logger.info(
                    "[tap] lnk early-return reason=gone barcode=%s chat=%s",
                    barcode or "-", tap_chat,
                )
                await _safe_edit(query,_finished_copy(barcode))
                return
            try:
                detail = {}
                try:
                    detail = api.get_wine_detail(wine_id) or {}
                except Exception:
                    detail = {}
                if not detail:
                    await _safe_edit(query,"Wine not found.")
                    return
                name = detail.get("name") or f"Wine #{wine_id}"
                current = normalise_barcode(detail.get("barcode") or "") or (
                    str(detail.get("barcode") or "").strip()
                )
                if not label_card:
                    if current and current != stored:
                        await _safe_edit(query,
                            f"{html.escape(str(name), quote=False)} already has a different barcode"
                        )
                        return
                    try:
                        holders = api.lookup_barcode(stored)
                    except Exception:
                        holders = []
                    if not isinstance(holders, list):
                        holders = []
                    other = next(
                        (
                            w for w in holders
                            if isinstance(w, dict) and w.get("id") not in (None, wine_id)
                        ),
                        None,
                    )
                    if other:
                        other_name = other.get("name") or f"Wine #{other.get('id')}"
                        await _safe_edit(query,
                            f"That barcode is already on {html.escape(str(other_name), quote=False)}."
                        )
                        return
                    if current != stored:
                        result = api.update_wine(wine_id, {"barcode": stored})
                        name = result.get("name") or name
                pending = self._get_pending(api, barcode)
                photo = pending.get("photo_data")
                if photo:
                    try:
                        api.upload_wine_image(wine_id, photo)
                    except Exception as e:
                        logger.warning(f"[PHOTO] Failed to upload image: {e}")
                merged = pending.get("grok_merged") or pending.get("off_data") or {}
                paid = _paid_price(merged)
                if paid is not None:
                    self._remember_paid(query.message, wine_id, paid)
                if pending.get("grok_merged"):
                    self._reuse_label_lookup(
                        wine_id, pending.get("grok_merged"), query.message, api,
                        update.effective_chat.id, name,
                    )
                self._update_pending(api, barcode, wine_id=wine_id)
                self._touch_label_card(
                    query.message, wine_id=wine_id, wine_name=name,
                    linked=True, committed=True,
                )
                self._resolve_pending(api, barcode)
                if stored != barcode:
                    self._resolve_pending(api, stored)
                await self._prompt_where_to_store(
                    query, api, context,
                    wine_id=wine_id, name=name, paid=paid, created=False,
                )
            except StoreError:
                raise
            except Exception as e:
                await _safe_edit(query,f"Error linking barcode: {e}")
            finally:
                self._unclaim_pending(api, barcode)

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
        try:
            return await self._handle_photo_body(update, context)
        except StoreError:
            logger.exception("[PHOTO] store")
            try:
                await update.message.reply_text(RESEND_PHOTO)
            except Exception:
                pass

    async def _handle_photo_body(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a photo sent by the user.

        Three modes:
        1. Reply to an edit message -> update existing wine details
        2. Reply to a barcode message / single pending → new wine label flow
        3. No reply, no pending -> try to read barcode from the photo
        """
        api = self._get_api(update)
        self._sweep()  # photos are the biggest entries; evict before adding more
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
        loop = asyncio.get_running_loop()

        # --- Mode 1: Edit wine details (reply to edit message) ---
        edit_wine_id = self._detect_edit_reply(reply)
        if edit_wine_id is not None:
            logger.info(f"[PHOTO] Mode 1: edit wine #{edit_wine_id}")
            await self._handle_edit_photo(update, context, edit_wine_id, photo_data)
            return

        waiting = getattr(self, "_await_side_photo", {}).get(str(update.effective_chat.id))
        if waiting and not waiting.get("offer"):
            if time.time() - waiting.get("time", 0) < PENDING_TIMEOUT_SECONDS:
                pending_key = waiting["barcode"]
                detected = await self._decode_photo_barcode(photo_data)
                if detected and await self._side_barcode_conflicts(
                    api, pending_key, detected, photo_data, update,
                ):
                    return
                status_msg = await update.message.reply_text("Attaching the other side\u2026")
                await self._attach_side_photo(
                    status_msg, pending_key, photo_data, update, detected=detected,
                )
                return

        # An explicit reply to a barcode message still wins over pyzbar.
        barcode = None
        if reply:
            barcode = _extract_barcode_from_message(reply)
            if barcode and not self._is_pending(api, barcode):
                await update.message.reply_text(
                    f"Barcode <code>{barcode}</code> was already handled or expired.\n"
                    f"Scan a new barcode to try again.",
                    parse_mode="HTML",
                )
                return

        # A barcode on this photo is this bottle — don't drop it onto another
        # pending card, and don't abort just because one is already open.
        detected = None
        if not barcode:
            logger.info("[PHOTO] trying local barcode reader...")
            detected = await self._decode_photo_barcode(photo_data)
            if detected:
                logger.info(f"[PHOTO] local barcode SUCCESS: barcode={detected}")
            else:
                logger.info("[PHOTO] local barcode: no barcode found")

        if detected:
            pending = self._get_pending(api, detected)
            if self._is_pending(api, detected) and not pending.get("wine_id"):
                # Kitchen Pi (or an earlier photo) already opened this barcode —
                # analyse the label onto that card, don't re-triage as a new scan.
                logger.info(
                    "[PHOTO] pyzbar barcode=%s already pending — label flow",
                    detected,
                )
                barcode = detected
            else:
                try:
                    found = api.lookup_barcode(detected)
                except Exception as e:
                    logger.warning(f"[PHOTO] barcode lookup failed: {e}")
                    found = []
                if found:
                    status_msg = await update.message.reply_text(
                        f"📷 Barcode detected: <code>{detected}</code>\nLooking up...",
                        parse_mode="HTML",
                    )
                    await self._handle_barcode_result(status_msg, detected, update=update)
                    return
                if not self._is_pending(api, detected):
                    off_data = None
                    try:
                        from integrations.open_food_facts import OpenFoodFactsAPI
                        off_data = OpenFoodFactsAPI().lookup(detected)
                    except Exception as e:
                        logger.warning(f"[PHOTO] OFF lookup failed: {e}")
                    self._create_pending(api, detected, off_data=off_data)
                    logger.info(
                        "[PHOTO] Mode 3: unknown pyzbar barcode=%s — analysing this label",
                        detected,
                    )
        elif not barcode:
            # --- Mode 2: label photo for a pending scan, only if pyzbar found nothing ---
            with self._lock:
                active = _active_pendings(self.pending_store, self._hid(api))
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

        # If we have a barcode from an explicit reply / leftover pending, analyse it.
        if barcode:
            logger.info(f"[PHOTO] Mode 2: label photo for barcode={barcode}")
            await self._handle_label_photo(update, context, barcode, photo_data)
            return

        if await self._maybe_correct_winelist_photo(update, photo_data):
            return

        if not reply and await self._maybe_handle_extra_label_photo(update, photo_data):
            return

        # --- Mode 3: No leftover barcode context — triage the photo ---
        logger.info(f"[PHOTO] Mode 3: no barcode context, photo size={len(photo_data)} bytes")

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
        currency = self._currency_for(update.effective_chat.id)
        country = self._country_for(update.effective_chat.id)
        try:
            result = await loop.run_in_executor(
                None, functools.partial(self._triage_callback, photo_data, currency, country),
            )
        except Exception as e:
            logger.error(f"[PHOTO] Grok triage exception: {e}")
            await status_msg.edit_text(f"Error analysing photo: {e}")
            return

        if not result:
            logger.error("[PHOTO] Grok triage returned None")
            await status_msg.edit_text(PHOTO_TRIAGE_FAILED)
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
                await self._handle_barcode_result(status_msg, barcode_digits, update=update)
                return
            logger.warning("[PHOTO] Grok detected barcode but digits invalid")
            await status_msg.edit_text("Detected a barcode but couldn't read the number. Try a clearer photo.")
            return

        # --- Grok says it's a wine label ---
        if photo_type == "label":
            logger.info(f"[PHOTO] Grok label analysis: {result.get('name', '?')}")
            await self._handle_triage_label(
                status_msg, result, photo_data, update=update,
                barcode=detected or "",
            )
            return

        # --- Grok says it's a wine list ---
        if photo_type == "winelist":
            if not self._winelist_callback:
                await status_msg.edit_text(
                    "\U0001F4CB That looks like a wine list, but list reading "
                    "isn't configured here."
                )
                return
            logger.info("[PHOTO] Grok says: multi-wine photo")
            await self._handle_wine_list(status_msg, photo_data, update=update)
            return

        # --- Neither ---
        logger.info(f"[PHOTO] Grok says: not a barcode or label (type={photo_type})")
        await status_msg.edit_text(
            "That doesn't look like a wine to me.\n\n"
            "Send a <b>bottle</b> or its <b>barcode</b> to add it, or a "
            "<b>wine list</b> and I'll tell you what to order.",
            parse_mode="HTML",
        )

    def _misread_for(self, chat_id) -> dict | None:
        cid = str(chat_id)
        pending = getattr(self, "_misread_corrections", {}).get(cid)
        if not pending:
            return None
        if time.time() - pending.get("time", 0) > MISREAD_TTL:
            self._misread_corrections.pop(cid, None)
            return None
        return pending

    def _start_misread(self, chat_id, *, barcode: str = "", wrong_name: str = "",
                       context: dict | None = None, photo_data: bytes = b"") -> None:
        cid = str(chat_id)
        store = getattr(self, "_misread_corrections", None)
        if store is None:
            self._misread_corrections = {}
        self._misread_corrections[cid] = {
            "barcode": barcode or "",
            "wrong_name": wrong_name or "",
            "context": dict(context or {}),
            "photo_data": photo_data or b"",
            "time": time.time(),
        }

    def _correction_for(self, chat_id) -> dict | None:
        cid = str(chat_id)
        store = getattr(self, "_winelist_corrections", None)
        if store is None:
            self._winelist_corrections = {}
            return None
        pending = store.get(cid)
        if not pending:
            return None
        if time.time() - pending.get("time", 0) > WINELIST_TTL:
            self._winelist_corrections.pop(cid, None)
            return None
        return pending

    async def _maybe_typed_add(self, update, context, text: str) -> bool:
        """Typed Confirm / Add on an open label card runs the same path as the tap."""
        if not TYPED_ADD_RE.match(text or ""):
            return False
        try:
            api = self._get_api(update)
        except Exception:
            return False
        cards = self._open_label_cards(
            api, max_age=TYPED_ADD_TTL, include_complete=True,
        )
        if not cards:
            await update.message.reply_text(
                "Nothing waiting to add, send me a photo of the label."
            )
            return True
        barcode, _ = cards[0]
        query = _as_edit_query(update)
        await self._add_pending_wine(
            query, api, context, barcode, tap_action="grok",
        )
        return True

    async def _maybe_typed_stock(self, update, context, text: str) -> bool:
        """Typed 'drank this' / 'drank one of my X' after a card, or by name."""
        parsed = parse_typed_stock(text)
        if not parsed:
            return False
        kind, query_name = parsed
        try:
            api = self._get_api(update)
        except Exception:
            return False
        query = _as_edit_query(update)
        if kind == "this":
            cards = self._open_label_cards(
                api, max_age=TYPED_ADD_TTL, include_complete=True,
            )
            if not cards:
                await update.message.reply_text(
                    "Nothing waiting to mark as drunk — send a photo or name the wine."
                )
                return True
            barcode, data = cards[0]
            merged = data.get("grok_merged") or {}
            hits = []
            try:
                hits = find_existing_wines(merged, api.list_all_wines())
            except Exception:
                hits = []
            if hits:
                await self._start_drink_from_hits(query, api, context, hits)
                return True
            fake = update
            fake.callback_query = query
            query.data = _cb("dk", barcode=barcode)
            await self._dispatch_callback(fake, context)
            return True
        return await self._typed_drink_named(update, context, api, query, query_name)

    async def _typed_drink_named(self, update, context, api, query, query_name: str) -> bool:
        try:
            wines = api.wines_in_stock()
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return True
        hits = match_in_stock_wines(wines, query_name)
        if not hits:
            await update.message.reply_text(
                f"Nothing matching <b>{html.escape(query_name, quote=False)}</b> "
                f"in stock.\n\nSend a label photo if you've just finished a bottle "
                f"that isn't on the shelf list.",
                parse_mode="HTML",
            )
            return True
        if len(hits) == 1:
            await _safe_edit(
                query,
                _confirm_drink_text(hits[0]),
                parse_mode="HTML",
                reply_markup=_confirm_drink_markup(hits[0]["id"]),
            )
            return True
        await self._offer_drink_picks(query, hits, confirm=True)
        return True

    async def _offer_drink_picks(self, query, wines: list[dict], *, confirm: bool = False):
        keyboard = []
        action = "dc" if confirm else "dx"
        for wine in wines[:8]:
            keyboard.append([InlineKeyboardButton(
                _wine_choice_label(wine),
                callback_data=_cb(action, wine_id=wine["id"]),
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))])
        await _safe_edit(
            query,
            "Which one did you drink?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _start_drink_from_hits(self, query, api, context, hits: list[dict]):
        if len(hits) == 1:
            await self._drink_existing(query, api, context, hits[0]["id"])
            return
        await self._offer_drink_picks(query, hits)

    async def _drink_existing(
        self, query, api, context, wine_id: int, *, storage_name: str | None = None,
    ):
        """Remove one in-stock bottle and show the usual drank card."""
        if storage_name is None:
            try:
                detail = api.get_wine_detail(wine_id) or {}
            except Exception:
                detail = {}
            places = []
            for item in detail.get("items") or []:
                name = item.get("storage") or ""
                if name and name not in places:
                    places.append(name)
            if len(places) > 1:
                try:
                    storages = api.list_storages()
                except Exception:
                    storages = []
                ids = {
                    s.get("name"): s.get("id")
                    for s in storages or [] if isinstance(s, dict)
                }
                keyboard = [[InlineKeyboardButton(
                    name,
                    callback_data=_cb("ds", wine_id=wine_id, page=ids.get(name) or 0),
                )] for name in places]
                keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))])
                await _safe_edit(
                    query,
                    "Which fridge did you take it from?",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return
            if places:
                storage_name = places[0]
        try:
            result = api.remove_stock(wine_id, storage_name=storage_name)
        except Exception as e:
            await _safe_edit(query, f"Error: {e}")
            return
        if not isinstance(result, dict) or result.get("error"):
            err = (result or {}).get("error") if isinstance(result, dict) else None
            if err and "no stock" in str(err).lower():
                try:
                    result = api.add_stock(wine_id, drank=True)
                except Exception as e:
                    await _safe_edit(query, f"Error: {e}")
                    return
                if isinstance(result, dict) and not result.get("error"):
                    await self._show_drank_result(query, context, result, wine_id)
                    return
            await _safe_edit(query, f"⚠️ {err or 'Could not take that bottle out.'}")
            return
        await self._show_drank_result(query, context, result, wine_id)

    async def _show_drank_result(self, query, context, result: dict, wine_id: int):
        name = result.get("wine_name") or "that wine"
        item_id = result.get("item_id")
        stock = result.get("stock_count", 0)
        self._touch_label_card(
            query.message, item_id=item_id, wine_name=name,
            stock_count=stock, drunk=True, in_progress=False, guard=False,
            wine_id=wine_id,
        )
        if item_id:
            text, markup = build_drank_view(name, stock, item_id, wine_id=wine_id)
            await _safe_edit(query, text, parse_mode="HTML", reply_markup=markup)
        else:
            await _safe_edit(
                query,
                f"\U0001F377 <b>Drank:</b> {name}\n"
                f"\U0001F4CA In stock: {stock}",
                parse_mode="HTML",
            )
        try:
            await self._celebrate(context, query.message.chat_id, result)
        except Exception:
            pass

    async def _maybe_take_producer(self, update, text: str, context=None) -> bool:
        """Fill a missing producer after 'Who makes this?' and create the wine."""
        chat_id = str(update.effective_chat.id)
        pending = getattr(self, "_await_producer", {}).get(chat_id)
        if not pending:
            return False
        if time.time() - pending.get("time", 0) > 30 * 60:
            self._await_producer.pop(chat_id, None)
            return False
        if not _plausible_producer(text):
            return False
        try:
            api = self._get_api(update)
        except Exception:
            return False
        producer = (text or "").strip()
        barcode = pending.get("barcode") or ""
        entry = self._get_pending(api, barcode) if barcode else {}
        merged = dict(entry.get("grok_merged") or {})
        merged["producer"] = producer
        if barcode:
            self._update_pending(api, barcode, grok_merged=merged)
        self._await_producer.pop(chat_id, None)
        query = _as_edit_query(update)
        await self._add_pending_wine(
            query, api, context, barcode,
            producer_override=producer, tap_action="grok",
        )
        return True

    async def _analyse_photo_bytes(self, photo_data: bytes, update) -> dict | None:
        chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
        currency = self._chat_id_to_currency.get(chat_id, "USD")
        country = self._chat_id_to_country.get(chat_id, "")
        loop = asyncio.get_running_loop()
        cb = self._label_analyse or self._triage_callback
        if not cb:
            return None
        try:
            result = await loop.run_in_executor(
                None, functools.partial(cb, photo_data, currency, country),
            )
        except Exception as e:
            logger.warning("[PHOTO] side-label analyse failed: %s", e)
            return None
        if isinstance(result, dict) and result.get("type") in (None, "label", "barcode"):
            return result
        return result if isinstance(result, dict) else None

    async def _decode_photo_barcode(self, photo_data: bytes) -> str | None:
        reader = self._local_barcode_reader
        if not reader:
            from integrations.grok_vision import read_barcode_local
            reader = read_barcode_local
        loop = asyncio.get_running_loop()
        try:
            detected = await loop.run_in_executor(
                None, functools.partial(reader, photo_data),
            )
        except Exception as e:
            logger.warning("[PHOTO] barcode decode failed: %s", e)
            return None
        from integrations.barcodes import accepted_barcode
        return accepted_barcode(detected) if detected else None

    def _store_side_barcode(self, api, pending_key: str, digits: str) -> None:
        entry = self._get_pending(api, pending_key)
        merged = dict(entry.get("grok_merged") or {})
        if str(merged.get("barcode") or "").strip():
            return
        merged["barcode"] = digits
        self._update_pending(api, pending_key, grok_merged=merged)
        wine_id = entry.get("wine_id")
        if wine_id:
            try:
                api.update_wine(wine_id, {"barcode": digits})
            except Exception as e:
                logger.warning("[PHOTO] could not save barcode on wine #%s: %s", wine_id, e)

    async def _side_barcode_conflicts(
        self, api, pending_key, detected, photo_data, update,
    ) -> bool:
        """True if we asked the user instead of attaching."""
        try:
            hits = api.lookup_barcode(detected) or []
        except Exception as e:
            logger.warning("[PHOTO] barcode lookup failed: %s", e)
            hits = []
        if not isinstance(hits, list):
            hits = []
        entry = self._get_pending(api, pending_key)
        wine_id = entry.get("wine_id")
        other = next(
            (w for w in hits if isinstance(w, dict) and w.get("id") not in (None, wine_id)),
            None,
        )
        if other is None:
            return False
        chat_id = str(update.effective_chat.id)
        self._pending_side_bytes[chat_id] = photo_data
        self._await_side_photo[chat_id] = {
            "barcode": pending_key, "time": time.time(), "offer": True,
        }
        await update.message.reply_text(
            DIFFERENT_BOTTLE,
            reply_markup=_different_bottle_keyboard(pending_key),
        )
        return True

    async def _ask_different_bottle(self, status_msg, pending_key, photo_data, update):
        chat_id = str(update.effective_chat.id)
        self._pending_side_bytes[chat_id] = photo_data
        self._await_side_photo[chat_id] = {
            "barcode": pending_key, "time": time.time(), "offer": True,
        }
        edit = getattr(status_msg, "edit_text", None)
        if callable(edit):
            await edit(DIFFERENT_BOTTLE, reply_markup=_different_bottle_keyboard(pending_key))
        else:
            await update.message.reply_text(
                DIFFERENT_BOTTLE,
                reply_markup=_different_bottle_keyboard(pending_key),
            )

    async def _attach_side_photo(self, status_msg, pending_key: str, photo_data: bytes, update,
                                 force: bool = False, detected: str | None = None) -> None:
        api = self._get_api(update)
        entry = self._get_pending(api, pending_key)
        chat_id = str(update.effective_chat.id)
        if entry.get("wine_id") or not self._is_pending(api, pending_key):
            getattr(self, "_await_side_photo", {}).pop(chat_id, None)
            getattr(self, "_pending_side_bytes", {}).pop(chat_id, None)
            edit = getattr(status_msg, "edit_text", None)
            if callable(edit):
                await edit(SIDE_PHOTO_GONE)
            else:
                await update.message.reply_text(SIDE_PHOTO_GONE)
            return
        first = dict(entry.get("grok_merged") or {})
        first_bytes = entry.get("photo_front") or entry.get("photo_data") or b""
        second = await self._analyse_photo_bytes(photo_data, update) or {}
        if not force and _clearly_different_label(first, second):
            await self._ask_different_bottle(status_msg, pending_key, photo_data, update)
            return
        from integrations.barcodes import accepted_barcode
        digits = detected or accepted_barcode(second.get("barcode"))
        if digits:
            second["barcode"] = second.get("barcode") or digits
            self._store_side_barcode(api, pending_key, digits)
            first = dict(self._get_pending(api, pending_key).get("grok_merged") or first)
        first_is_back = _is_classification_name(first.get("name")) or _filled(first.get("barcode"))
        second_is_front = not _is_classification_name(second.get("name"))
        if first_is_back and second_is_front:
            front, back = second, first
            front_bytes, back_bytes = photo_data, first_bytes
        else:
            front, back = first, second
            front_bytes, back_bytes = first_bytes, photo_data
        merged = merge_front_back(front, back)
        self._update_pending(
            api, pending_key,
            grok_merged=merged, photo_front=front_bytes, photo_back=back_bytes,
            photo_data=front_bytes or back_bytes,
        )
        getattr(self, "_await_side_photo", {}).pop(chat_id, None)
        getattr(self, "_pending_side_bytes", {}).pop(chat_id, None)
        if str(pending_key).startswith("_"):
            await self._handle_triage_label(
                status_msg, merged,
                photo_data=front_bytes or back_bytes,
                update=update, reuse_key=pending_key,
            )
        else:
            await self._show_barcode_label_confirm(status_msg, merged, pending_key, api)

    async def _maybe_handle_extra_label_photo(self, update, photo_data: bytes) -> bool:
        try:
            api = self._get_api(update)
        except Exception:
            return False
        cards = self._open_label_cards(api)
        if len(cards) != 1:
            return False
        barcode, data = cards[0]
        name = (data.get("grok_merged") or {}).get("name") or "that wine"
        chat_id = str(update.effective_chat.id)
        self._pending_side_bytes[chat_id] = photo_data
        self._await_side_photo[chat_id] = {
            "barcode": barcode, "time": time.time(), "offer": True,
        }
        await update.message.reply_text(
            f"Is this the back of <b>{html.escape(str(name), quote=False)}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Yes, that's the back",
                    callback_data=_cb("yb", barcode=barcode),
                )],
                [InlineKeyboardButton(
                    "No, it's a different wine",
                    callback_data=_cb("nd", barcode=barcode),
                )],
            ]),
        )
        return True

    async def _start_edit_photo_flow(self, query, api, wine_id: int) -> None:
        try:
            result = api.update_wine(wine_id, {})
        except Exception:
            result = {"name": f"Wine #{wine_id}"}
        with self._lock:
            self.pending_store[f"_edit_{wine_id}"] = {
                "wine_id": wine_id,
                "time": time.time(),
            }
        await _safe_edit(query,
            f"✏️ <b>Edit: {result.get('name', f'Wine #{wine_id}')}</b>\n\n"
            f"📸 <b>Reply to this message</b> with a label photo "
            f"to update this wine's details.\n\n"
            f"Wine ID: <code>edit:{wine_id}</code>",
            parse_mode="HTML",
        )

    async def _maybe_identify_misread_text(self, update, text: str) -> bool:
        """Typed correction after a wrong barcode or photo read.

        Wine-list Wrong is handled separately. This covers gun/label mistakes:
        search the cellar first, then ask Grok with everything we already had.
        """
        if len(text) < 3:
            return False
        try:
            api = self._get_api(update)
        except NotProvisionedError:
            return False
        chat_id = update.effective_chat.id
        if getattr(self, "_await_producer", {}).get(str(chat_id)):
            return False
        pending = self._misread_for(chat_id)
        barcode = (pending or {}).get("barcode") or ""
        wrong_name = (pending or {}).get("wrong_name") or ""
        context = dict((pending or {}).get("context") or {})
        photo = (pending or {}).get("photo_data") or b""
        if pending is None:
            # Only an explicit "That's the wrong wine" tap enters this flow.
            # A lone _label_/_edit_ card with grok_merged is waiting for Add.
            active = _active_pendings(self.pending_store, self._hid(api))
            if len(active) != 1:
                return False
            barcode = next(iter(active))
            entry = active[barcode] or {}
            key = str(barcode)
            stored_key = str(entry.get("barcode") or "")
            is_label_card = (
                key.startswith("_label_") or key.startswith("_edit_")
                or stored_key.startswith("_label_") or stored_key.startswith("_edit_")
            )
            if entry.get("grok_merged") and not (
                entry.get("misread") or entry.get("wrong")
            ):
                return False
            if is_label_card:
                return False
            grok = entry.get("grok_merged") or {}
            off = entry.get("off_data") or {}
            wrong_name = grok.get("name") or (off.get("name") if off else "") or ""
            context = {**(off or {}), **(grok or {}), "barcode": barcode}
            photo = entry.get("photo_front") or entry.get("photo_data") or b""
        real_barcode = "" if str(barcode).startswith("_") else barcode
        if real_barcode:
            context.setdefault("barcode", real_barcode)

        status = await update.message.reply_text("Looking that up\u2026")
        try:
            hits = api.search_wines(text)
        except Exception as e:
            logger.warning(f"[misread] cellar search failed: {e}")
            hits = []

        identified = {"name": text}
        # Pull a vintage out of the typed hint if present.
        year = re.search(r"\b((?:19|20)\d{2})\b", text)
        if year:
            identified["vintage"] = int(year.group(1))

        action, wines = cellar_match_for_hint(hits, identified)
        if action == "stock" and wines:
            wine = wines[0]
            self._misread_corrections.pop(str(chat_id), None)
            try:
                if real_barcode:
                    api.update_wine(wine["id"], {"barcode": real_barcode})
                result = api.add_stock(wine["id"])
                failed = _stock_failed(result)
                if failed:
                    await status.edit_text(failed)
                    return True
                if real_barcode:
                    self._resolve_pending(api, real_barcode)
            except Exception as e:
                await status.edit_text(f"Error adding: {e}")
                return True
            await status.edit_text(
                f"\U0001F4E5 <b>Added:</b> {result.get('wine_name') or wine.get('name')}\n"
                f"\U0001F4CA In stock: {result.get('stock_count', '?')}",
                parse_mode="HTML",
            )
            return True
        if action == "pick" and wines:
            lines = [
                "A few wines in the cellar match that — which one?",
            ]
            keyboard = []
            for w in wines:
                vintage = w.get("vintage") or ""
                label = f"{w.get('name', '')} {vintage}".strip()
                keyboard.append([InlineKeyboardButton(
                    label[:40], callback_data=_cb("in", wine_id=w["id"]),
                )])
            keyboard.append([InlineKeyboardButton(
                "None of these — look it up", callback_data=_cb("wr", barcode=barcode),
            )])
            await status.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return True

        data = None
        if self._identify_callback:
            currency = self._currency_for(chat_id)
            country = self._country_for(chat_id)
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._identify_callback, text, wrong_name,
                        currency, country, context,
                    ),
                )
            except TypeError:
                # Older callback signature without context.
                try:
                    data = await loop.run_in_executor(
                        None,
                        functools.partial(
                            self._identify_callback, text, wrong_name,
                            currency, country,
                        ),
                    )
                except Exception as e:
                    logger.error(f"[misread] identify-from-hint failed: {e}")
            except Exception as e:
                logger.error(f"[misread] identify-from-hint failed: {e}")
        if not data or not data.get("name"):
            await status.edit_text(
                "I couldn't place that name. Try a closer label photo, "
                "or a more specific producer + vintage."
            )
            return True

        try:
            hits = api.search_wines(str(data.get("name") or text))
        except Exception:
            hits = hits or []
        action, wines = cellar_match_for_hint(hits, data)
        if action == "stock" and wines:
            wine = wines[0]
            self._misread_corrections.pop(str(chat_id), None)
            try:
                if real_barcode:
                    api.update_wine(wine["id"], {"barcode": real_barcode})
                result = api.add_stock(wine["id"])
                failed = _stock_failed(result)
                if failed:
                    await status.edit_text(failed)
                    return True
                if real_barcode:
                    self._resolve_pending(api, real_barcode)
            except Exception as e:
                await status.edit_text(f"Error adding: {e}")
                return True
            await status.edit_text(
                f"\U0001F4E5 <b>Added:</b> {result.get('wine_name') or wine.get('name')}\n"
                f"That's <b>{data.get('name')}</b> {data.get('vintage') or ''} "
                f"already in the cellar.",
                parse_mode="HTML",
            )
            return True

        self._misread_corrections.pop(str(chat_id), None)
        if real_barcode:
            if not self._is_pending(api, real_barcode):
                self._create_pending(api, real_barcode)
            self._update_pending(api, real_barcode, grok_merged=data)
            if photo:
                self._update_pending(api, real_barcode, photo_data=photo, photo_front=photo)
        await status.edit_text("Found it — checking the cellar\u2026")
        await self._handle_triage_label(status, data, photo_data=photo, update=update)
        return True

    async def _start_winelist_correction(self, query, index: int):
        key = f"{query.message.chat_id}:{query.message.message_id}"
        entry = self._winelists.get(key)
        if not entry:
            await _safe_edit(query,
                "\U0001F4CB That list has expired — send the photo again."
            )
            return
        wines = entry["data"].get("wines") or []
        if not (0 <= index < len(wines)):
            await query.answer("That wine is not on this page.")
            return
        wrong = wines[index]
        wrong_name = _wine_label(wrong)
        self._winelist_corrections[str(query.message.chat_id)] = {
            "list_key": key,
            "index": index,
            "wrong_name": wrong_name,
            "time": time.time(),
        }
        await query.message.reply_text(
            f"\u2717 I had <b>#{index + 1}</b> as <b>{wrong_name}</b> — that's wrong.\n\n"
            "Send a closer photo of that bottle, or type the producer and name, "
            "and I'll look it up properly.",
            parse_mode="HTML",
        )

    async def _finish_winelist_correction(self, update, entry: dict, old_name: str):
        pending = self._winelist_corrections.pop(str(update.effective_chat.id), None)
        if not pending:
            return
        list_entry = self._winelists.get(pending["list_key"])
        if not list_entry:
            await update.message.reply_text(
                "That list expired while I was looking — send the photo again."
            )
            return
        apply_winelist_correction(
            list_entry["data"], pending["index"], entry, old_name,
        )
        api = self._get_api(update)
        try:
            cellar = api.list_all_wines()
        except Exception:
            cellar = []
        mark_owned_wines(list_entry["data"], cellar)
        photo = list_entry.get("photo")
        wines = list_entry["data"].get("wines") or []
        new_label = _wine_label(entry)
        logger.info(
            f"[winelist] corrected #{pending['index'] + 1} "
            f"{pending.get('wrong_name')!r} → {new_label!r}"
        )
        banner = f"Updated #{pending['index'] + 1} to <b>{new_label}</b>."
        bot = update.get_bot()
        chat_id, _, _msg_id = pending["list_key"].partition(":")
        await self._retire_winelist_message(bot, pending["list_key"])
        compact = len(wines) <= WINELIST_PER_PAGE
        await self._send_winelist(
            bot, chat_id, list_entry["data"], photo=photo, banner=banner,
            show_all=not compact,
            page=pending["index"] // WINELIST_PER_PAGE,
        )

    async def _maybe_correct_winelist_photo(self, update, photo_data: bytes) -> bool:
        pending = self._correction_for(update.effective_chat.id)
        if not pending:
            return False
        if not self._triage_callback:
            await update.message.reply_text(
                "I can't re-read a bottle without vision configured. Type the name instead."
            )
            return True
        status = await update.message.reply_text("Looking at that bottle again\u2026")
        currency = self._currency_for(update.effective_chat.id)
        country = self._country_for(update.effective_chat.id)
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None,
                functools.partial(self._triage_callback, photo_data, currency, country),
            )
        except Exception as e:
            logger.error(f"[winelist] correction photo failed: {e}")
            data = None
        if not data or data.get("type") != "label" or not data.get("name"):
            await status.edit_text(
                "I need a closer shot of that one bottle — or type the producer and name."
            )
            return True
        await status.delete()
        await self._finish_winelist_correction(
            update, list_entry_from_label(data, currency), pending["wrong_name"],
        )
        return True

    async def _maybe_correct_winelist_text(self, update, text: str) -> bool:
        pending = self._correction_for(update.effective_chat.id)
        if not pending:
            return False
        if len(text) < 3:
            await update.message.reply_text(
                "Give me a bit more — producer and name, or a closer photo."
            )
            return True
        api = self._get_api(update)
        status = await update.message.reply_text("Looking that up\u2026")
        try:
            hits = api.search_wines(text)
        except Exception as e:
            logger.warning(f"[winelist] cellar search for correction failed: {e}")
            hits = []
        if len(hits) == 1:
            await status.delete()
            await self._finish_winelist_correction(
                update, list_entry_from_cellar(hits[0]), pending["wrong_name"],
            )
            return True
        if len(hits) > 1:
            await status.edit_text(
                "A few wines in the cellar match that. Send a closer photo "
                "of the bottle, or type a more specific name."
            )
            return True
        currency = self._currency_for(update.effective_chat.id)
        country = self._country_for(update.effective_chat.id)
        data = None
        if self._identify_callback:
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._identify_callback, text, pending["wrong_name"],
                        currency, country,
                    ),
                )
            except Exception as e:
                logger.error(f"[winelist] identify-from-hint failed: {e}")
        if not data or not data.get("name"):
            await status.edit_text(
                "I couldn't place that name. Try a closer photo of the bottle."
            )
            return True
        await status.delete()
        await self._finish_winelist_correction(
            update, list_entry_from_label(data, currency), pending["wrong_name"],
        )
        return True

    async def _retire_winelist_message(self, bot, list_key: str):
        """Strip the old list card so its buttons die, then drop the cache key."""
        self._winelists.pop(list_key, None)
        chat_id, _, msg_id = list_key.partition(":")
        if not chat_id or not msg_id:
            return
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                text="Updated \u2014 latest reading is below.",
            )
        except Exception as e:
            logger.warning(f"[winelist] could not retire list message: {e}")

    async def _send_winelist(self, bot, chat_id, data: dict, *, photo=None,
                             banner: str = "", page: int = 0, show_all: bool = False):
        """Post a fresh list card at the bottom of the chat and cache it."""
        text, keyboard = build_winelist_view(data, page=page, show_all=show_all)
        if banner:
            text = f"{banner}\n\n{text}"
        msg = await bot.send_message(
            chat_id=int(chat_id),
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        self._winelists[f"{chat_id}:{msg.message_id}"] = {
            "data": data, "time": time.time(), "photo": photo,
        }
        self._sweep_winelists()
        return msg

    async def _handle_wine_list(self, status_msg, photo_data: bytes, update=None):
        """Read a multi-wine photo and recommend from it against their cellar."""
        api = self._get_api(update)
        chat_id = update.effective_chat.id if update else None
        currency = self._currency_for(chat_id)
        country = self._country_for(chat_id)

        await status_msg.edit_text(
            "\U0001F4CB Reading the wines\u2026 this one takes a few seconds."
        )

        loop = asyncio.get_running_loop()
        try:
            wines = await loop.run_in_executor(None, api.list_all_wines)
        except Exception as e:
            logger.warning(f"[winelist] could not load cellar for context: {e}")
            wines = []
        summary = build_cellar_summary(wines)
        try:
            taste = await loop.run_in_executor(None, api.get_taste)
            if taste and taste.get("summary"):
                summary = taste["summary"]
        except Exception as e:
            logger.warning(f"[taste] could not load snapshot for winelist: {e}")

        try:
            data = await loop.run_in_executor(
                None,
                functools.partial(
                    self._winelist_callback, photo_data, summary, currency, country,
                ),
            )
        except Exception as e:
            logger.error(f"[winelist] analysis failed: {e}")
            await status_msg.edit_text(f"Error reading the wines: {e}")
            return

        if not data:
            await status_msg.edit_text(
                "\U0001F4CB I couldn't read those wines. Try a straighter shot "
                "with the names in focus."
            )
            return

        owned_hits = mark_owned_wines(data, wines)
        if owned_hits:
            logger.info(f"[winelist] {owned_hits} wine(s) already owned")
        logger.info(f"[winelist] scene={_scene(data)}")

        rec = data.get("recommendation") if isinstance(data.get("recommendation"), dict) else None
        wines_payload = []
        for w in (data.get("wines") or [])[:20]:
            wines_payload.append({
                "name": (w.get("name") or "")[:120],
                "producer": (w.get("producer") or "")[:80],
                "country": (w.get("country") or "")[:8],
                "grape": (w.get("grape") or w.get("grape_variety") or "")[:80],
            })
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    api.record_intent, "winelist",
                    wines=wines_payload,
                    scene=_scene(data),
                    recommendation=rec,
                ),
            )
        except Exception as e:
            logger.warning(f"[taste] winelist intent failed: {e}")

        key = f"{status_msg.chat_id}:{status_msg.message_id}"
        self._winelists[key] = {
            "data": data, "time": time.time(), "photo": photo_data,
        }
        self._sweep_winelists()

        text, keyboard = build_winelist_view(data)
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    def _sweep_winelists(self):
        """Drop read lists past their TTL — they hold a full parsed menu."""
        now = time.time()
        for key in [k for k, v in self._winelists.items()
                    if now - v.get("time", 0) > WINELIST_TTL]:
            self._winelists.pop(key, None)

    async def _handle_barcode_result(self, status_msg, barcode: str, update=None):
        """Handle a barcode that was read from a photo (pyzbar or Grok).
        Looks up in DB and shows appropriate response."""
        api = self._get_api(update)
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
            self._create_pending(api, barcode, off_data=off_data)
            suggested = []
            if off_data and off_data.get("name"):
                try:
                    suggested = wines_to_link(off_data, api.list_all_wines())
                except Exception:
                    suggested = []
            if suggested:
                await status_msg.edit_text(
                    _is_this_text(barcode, suggested),
                    parse_mode="HTML",
                    reply_markup=_is_this_keyboard(
                        barcode, suggested, new_action="nn",
                    ),
                )
                return

            text = f"❓ <b>Unknown barcode: <code>{html.escape(barcode, quote=False)}</code></b>\n\n"
            if off_data and off_data.get("name"):
                text += (
                    f"OFF found: <b>{html.escape(str(off_data['name']), quote=False)}</b>\n"
                    f"Producer: {html.escape(str(off_data.get('producer', 'Unknown')), quote=False)}\n\n"
                )
            text += (
                "📸 <b>Reply to this message</b> with a label photo "
                "and I'll extract the full details + tasting notes."
            )
            keyboard = [
                [InlineKeyboardButton("🔗 Link to existing wine", callback_data=_cb("link", barcode=barcode))],
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
            ], [
                InlineKeyboardButton(
                    "\U0001F377 Drank this",
                    callback_data=_cb("dk", wine_id=wine["id"]),
                ),
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

    def _find_matching_wine(self, grok_data: dict, update=None) -> dict | None:
        """Best existing-wine match, or None. Producer/vintage must agree when present."""
        api = self._get_api(update)
        try:
            wines = api.list_all_wines()
        except Exception:
            try:
                wines = api.wines_in_stock()
            except Exception:
                return None
        hits = find_existing_wines(grok_data, wines, limit=1)
        return hits[0] if hits else None

    async def _reread_if_classification(self, grok_data: dict, photo_data: bytes, update=None) -> dict:
        """Back labels often come back as 'Reserva' — ask Grok for the brand."""
        if not _is_classification_name(grok_data.get("name")):
            return grok_data
        if not photo_data or not self._label_analyse:
            return grok_data
        chat_id = ""
        if update is not None:
            chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
        currency = self._chat_id_to_currency.get(chat_id, "USD")
        country = self._chat_id_to_country.get(chat_id, "")
        try:
            again = await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(
                    self._label_analyse, photo_data, currency, country,
                    "This may be a back label. Read the brand / producer, "
                    "not only Reserva, Crianza or the appellation.",
                ),
            )
        except Exception as e:
            logger.info("[MATCH] back-label reread failed: %s", e)
            return grok_data
        if not isinstance(again, dict):
            return grok_data
        merged = dict(grok_data)
        for key, value in again.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return merged

    async def _handle_triage_label(self, status_msg, grok_data: dict, photo_data: bytes = b"", update=None, barcode: str = "", reuse_key: str = ""):
        """Handle a photo that Grok identified as a wine label.
        Grok already did the full analysis in the triage call.
        Checks for existing wines before creating a new one."""
        if not reuse_key:
            grok_data = await self._reread_if_classification(grok_data, photo_data, update)
        api = self._get_api(update)
        # A pyzbar barcode from this photo wins even if another card is pending.
        # Otherwise attach only when this household has exactly one pending scan.
        if reuse_key:
            barcode = ""
        elif not barcode:
            with self._lock:
                active = _active_pendings(self.pending_store, self._hid(api))
                if len(active) == 1:
                    barcode = next(iter(active))

        if barcode:
            pending = self._get_pending(api, barcode)
            if photo_data and (pending.get("photo_data") or pending.get("photo_front")):
                await self._attach_side_photo(status_msg, barcode, photo_data, update)
                return

        if not reuse_key and not str(grok_data.get("name") or "").strip():
            await status_msg.edit_text(UNREADABLE_LABEL)
            return

        if barcode:
            fields = {"grok_merged": grok_data}
            if photo_data:
                fields["photo_data"] = photo_data
                fields["photo_front"] = photo_data
            self._update_pending(api, barcode, **fields)
            await self._show_barcode_label_confirm(status_msg, grok_data, barcode, api)
            return

        # No pending barcode — check if this wine already exists
        hits = []
        try:
            hits = find_existing_wines(grok_data, api.list_all_wines())
        except Exception:
            hits = []
        updatable = [h for h in hits if _can_update_wine(grok_data, h)]
        existing = updatable[0] if len(updatable) == 1 else None
        tag = _paid_price(grok_data)
        if existing is not None and tag is not None:
            self._pending_paid[existing["id"]] = tag

        # Store Grok data under a temp key for the confirm buttons
        if reuse_key:
            temp_key = reuse_key
            with self._lock:
                entry = self.pending_store.get(temp_key) or {
                    "household_id": self._hid(api),
                    "barcode": temp_key,
                }
                entry["grok_merged"] = grok_data
                entry["time"] = time.time()
                self.pending_store[temp_key] = entry
        else:
            temp_key = self._next_label_key()
            with self._lock:
                entry = {
                    "grok_merged": grok_data,
                    "time": time.time(),
                    "household_id": self._hid(api),
                    "barcode": temp_key,
                }
                if photo_data:
                    entry["photo_data"] = photo_data
                    entry["photo_front"] = photo_data
                self.pending_store[temp_key] = entry

        suffix = ""
        keyboard = []

        if hits:
            if existing:
                _merge_stored_references(grok_data, existing)
            if _is_classification_name(grok_data.get("name")):
                suffix = "\n\nWe couldn't read the name, is this one of these?"
            elif len(hits) == 1 and existing:
                suffix = (
                    f"\n🍷 This looks like <b>{html.escape(str(existing.get('name') or 'that wine'), quote=False)}</b>"
                    f" already in your cellar ({existing.get('stock_count', 0)}x)."
                )
            else:
                suffix = "\n\nIs this one of these?"
            for wine in hits:
                label = _wine_choice_label(wine)
                if _can_update_wine(grok_data, wine):
                    edit_key = f"_edit_{wine['id']}"
                    with self._lock:
                        self.pending_store[edit_key] = {
                            "wine_id": wine["id"],
                            "grok_merged": grok_data,
                            "time": time.time(),
                            "photo_data": photo_data,
                        }
                    keyboard.append([InlineKeyboardButton(
                        f"✏️ Update {label}",
                        callback_data=_cb("edc", wine_id=wine["id"]),
                    )])
                else:
                    keyboard.append([InlineKeyboardButton(
                        label, callback_data=_cb("in", wine_id=wine["id"]),
                    )])
            drink_id = existing["id"] if existing else (
                hits[0]["id"] if len(hits) == 1 else None
            )
            if drink_id:
                keyboard.append([InlineKeyboardButton(
                    DRANK_THIS_LABEL,
                    callback_data=_cb("dx", wine_id=drink_id),
                )])
            else:
                keyboard.append([InlineKeyboardButton(
                    DRANK_THIS_LABEL,
                    callback_data=_cb("dx", barcode=temp_key),
                )])
            keyboard.append([InlineKeyboardButton(
                "🆕 No, it's a different wine",
                callback_data=_cb("grok", barcode=temp_key, original="new"),
            )])
            keyboard.append([InlineKeyboardButton(
                "That's the wrong wine", callback_data=_cb("wr", barcode=temp_key),
            )])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))])
        else:
            if _is_classification_name(grok_data.get("name")):
                suffix = "\n\nWe couldn't read the name."
            keyboard = [
                [InlineKeyboardButton(ADD_TO_CELLAR, callback_data=_cb("grok", barcode=temp_key))],
                [InlineKeyboardButton(
                    "\U0001F377 Drank this",
                    callback_data=_cb("dk", barcode=temp_key),
                )],
                [InlineKeyboardButton("👀 Just looking", callback_data=_cb("look"))],
                [InlineKeyboardButton("That's the wrong wine", callback_data=_cb("wr", barcode=temp_key))],
            ]
        pending = self._get_pending(api, temp_key)
        keyboard.extend(_another_photo_row(temp_key, pending))
        if not str(grok_data.get("producer") or "").strip():
            suffix += PRODUCER_HINT
            extra = _guess_producer_row(temp_key, grok_data.get("name") or "")
            keyboard = keyboard[:1] + extra + keyboard[1:] if keyboard else extra
        from integrations.telegram_lists import insert_to_drink, to_drink_button
        td_wine_id = None
        if hits:
            td_wine_id = existing["id"] if existing else (
                hits[0]["id"] if len(hits) == 1 else None
            )
        keyboard = insert_to_drink(
            keyboard,
            to_drink_button(
                self, getattr(status_msg, "chat_id", None),
                barcode=temp_key, wine_id=td_wine_id,
            ),
        )

        markup = InlineKeyboardMarkup(keyboard)
        # The triage vision call answers from the model's own memory and, held
        # to the reference rubric, nearly always returns nulls — the ratings
        # only exist after a live web search, which takes far longer than
        # anyone will stare at a spinner. So send the card now and grow the
        # numbers into it a moment later.
        wants_refs = _needs_references(grok_data) and hasattr(api, "lookup_references")
        pending_note = REFS_PENDING_NOTE if wants_refs else ""
        logger.info(
            "[PHOTO] label card pending=%s existing=%s wants_refs=%s",
            barcode or temp_key, existing["id"] if existing else None, wants_refs,
        )

        text = LABEL_HEADER + _grok_summary(grok_data) + suffix + pending_note
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)

        if not existing:
            key = self._card_key(status_msg)
            if len(self._label_cards) > 200:
                self._label_cards.pop(next(iter(self._label_cards)), None)
            self._label_cards[key] = {
                "suffix": suffix,
                "looking": False,
                "drunk": False,
                "wine": _taste_wine(grok_data),
            }

        if wants_refs and not reuse_key:
            self._kick_label_refs(
                status_msg, grok_data, api, suffix, markup, pending_key=temp_key,
            )

    def _card_key(self, message) -> str:
        return f"{message.chat_id}:{message.message_id}"

    async def _show_barcode_label_confirm(self, status_msg, grok_data: dict,
                                          barcode: str, api) -> None:
        """Confirm card for a gun/unknown barcode + label photo.

        Same live ratings lookup as a shop photo — the vision call almost
        never fills critic/Vivino/price, so we search in the background.
        """
        if not str(grok_data.get("name") or "").strip():
            await status_msg.edit_text(UNREADABLE_LABEL)
            return
        suffix = ""
        if barcode and not str(barcode).startswith("_"):
            suffix = f"\nBarcode: <code>{html.escape(barcode, quote=False)}</code>"
        hits = []
        try:
            hits = wines_to_link(grok_data, api.list_all_wines())
        except Exception:
            hits = []
        if hits:
            if _is_classification_name(grok_data.get("name")):
                suffix += "\n\nWe couldn't read the name, is this one of these?"
            elif len(hits) == 1:
                name = html.escape(str(hits[0].get("name") or "that wine"), quote=False)
                suffix += f"\n\nIs this <b>{name}</b>?"
            else:
                suffix += "\n\nIs this one of these?"
            keyboard = list(_is_this_keyboard(barcode, hits).inline_keyboard)
            keyboard.append([InlineKeyboardButton(
                "That's the wrong wine", callback_data=_cb("wr", barcode=barcode),
            )])
        else:
            if _is_classification_name(grok_data.get("name")):
                suffix += "\n\nWe couldn't read the name."
            keyboard = [
                [InlineKeyboardButton(ADD_TO_CELLAR, callback_data=_cb("grok", barcode=barcode))],
                [InlineKeyboardButton(
                    "\U0001F377 Drank this",
                    callback_data=_cb("dk", barcode=barcode),
                )],
                [InlineKeyboardButton("That's the wrong wine", callback_data=_cb("wr", barcode=barcode))],
                [InlineKeyboardButton("📸 Try another photo", callback_data=_cb("retry", barcode=barcode))],
                [InlineKeyboardButton("✏️ Add manually instead", callback_data=_cb("man", barcode=barcode))],
            ]
        pending = self._get_pending(api, barcode)
        keyboard.extend(_another_photo_row(barcode, pending))
        if not str(grok_data.get("producer") or "").strip():
            suffix += PRODUCER_HINT
            extra = _guess_producer_row(barcode, grok_data.get("name") or "")
            keyboard = keyboard[:1] + extra + keyboard[1:] if keyboard else extra
        from integrations.telegram_lists import insert_to_drink, to_drink_button
        td_wine_id = hits[0]["id"] if len(hits) == 1 else None
        keyboard = insert_to_drink(
            keyboard,
            to_drink_button(
                self, getattr(status_msg, "chat_id", None),
                barcode=barcode, wine_id=td_wine_id,
            ),
        )
        markup = InlineKeyboardMarkup(keyboard)
        wants_refs = _needs_references(grok_data) and hasattr(api, "lookup_references")
        pending_note = REFS_PENDING_NOTE if wants_refs else ""
        await status_msg.edit_text(
            LABEL_HEADER + _grok_summary(grok_data) + suffix + pending_note,
            parse_mode="HTML",
            reply_markup=markup,
        )
        if wants_refs:
            self._kick_label_refs(
                status_msg, grok_data, api, suffix, markup, pending_key=barcode,
            )

    def _kick_label_refs(self, status_msg, grok_data: dict, api, suffix: str, markup,
                         pending_key: str = ""):
        """Fire-and-forget wrapper to schedule _enrich_label_card_async."""
        key = self._card_key(status_msg)
        if len(self._label_cards) > 200:
            self._label_cards.pop(next(iter(self._label_cards)), None)
        card = self._label_cards.get(key) or {"looking": False, "drunk": False}
        card["suffix"] = suffix
        card["pending_lookup"] = True
        if pending_key:
            card["pending_key"] = pending_key
            with self._lock:
                card["had_pending"] = pending_key in self.pending_store
        card.setdefault("wine", _taste_wine(grok_data))
        paid = _paid_price(grok_data)
        if paid is not None:
            card["paid"] = paid
        self._label_cards[key] = card
        try:
            asyncio.create_task(
                self._enrich_label_card_async(status_msg, grok_data, api, markup),
            )
        except Exception as e:
            logger.warning(f"[label-refs] could not schedule lookup: {e}")
            self._label_cards.pop(key, None)

    async def _enrich_label_card_async(self, status_msg, grok_data: dict, api, markup):
        """Background: live-search the ratings for a label and edit them in.

        This is the shop case — a bottle in your hand that isn't in the cellar,
        so there's no Wine row to refresh and nothing is persisted. The card is
        already on screen; it simply gains the critic score, the Vivino rating
        and a price when they arrive.
        """
        key = self._card_key(status_msg)
        loop = asyncio.get_running_loop()
        name = str(grok_data.get("name") or "").strip()
        try:
            if not name:
                result = {"updated": False}
            else:
                result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        api.lookup_references,
                        name,
                        grok_data.get("producer") or "",
                        grok_data.get("vintage"),
                        country=grok_data.get("country") or "",
                        region=grok_data.get("region") or "",
                        appellation=grok_data.get("appellation") or "",
                        grape=grok_data.get("grape_variety") or "",
                        vineyard=grok_data.get("vineyard") or "",
                    ),
                )
        except Exception as e:
            logger.warning(f"[label-refs] lookup failed: {e}")
            result = {"updated": False}

        if result.get("updated"):
            for field in ("critic_score", "vivino_rating", "category_rating",
                          "market_price", "market_price_currency"):
                if result.get(field) is not None:
                    grok_data[field] = result[field]
            grok_data["reference_confidence"] = result.get("confidence", "")
            grok_data["reference_lookup"] = result
        else:
            logger.info(f"[label-refs] nothing usable for {grok_data.get('name', '?')!r}")

        # Re-render without the pending note either way, so the card doesn't
        # sit there promising numbers that are never coming.
        entry = dict(self._label_cards.get(key) or {
            "suffix": "", "looking": False, "drunk": False, "committed": False,
        })
        entry["pending_lookup"] = False
        self._label_cards[key] = entry
        pending_gone = False
        stored = entry.get("pending_key")
        if entry.get("had_pending") and stored:
            with self._lock:
                pending_gone = stored not in self.pending_store
        keep_card = entry.get("looking") or entry.get("listed")
        if entry.get("drunk") or entry.get("committed") or (pending_gone and not keep_card):
            # User already tapped Add / Drank this. Do not resurrect those
            # buttons over the storage picker — apply the search we already paid for.
            logger.info(
                "[label-refs] skip restore key=%s drunk=%s committed=%s "
                "pending_gone=%s wine_id=%s",
                key, entry.get("drunk"), entry.get("committed"), pending_gone,
                entry.get("wine_id"),
            )
            wine_id = entry.get("wine_id")
            if wine_id and result.get("updated"):
                try:
                    await loop.run_in_executor(
                        None,
                        functools.partial(api.apply_references, wine_id, result),
                    )
                    logger.info("[enrich] applied in-flight lookup to wine #%s", wine_id)
                except Exception as e:
                    logger.warning(
                        f"[enrich] apply_references failed for wine #{wine_id}: {e}"
                    )
                    self._kick_enrich(
                        wine_id, status_msg.chat_id, api, grok_data.get("name", ""),
                    )
            return
        self._label_cards.pop(key, None)
        text = LABEL_HEADER + _grok_summary(grok_data) + entry["suffix"]
        if entry.get("listed"):
            from integrations.telegram_lists import listed_note
            text += listed_note(self, getattr(status_msg, "chat_id", None))
            markup = None
        elif entry.get("looking"):
            text += JUST_LOOKING_NOTE
            markup = None
        try:
            await status_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            logger.info(f"[label-refs] card edit failed: {e}")

    async def _handle_label_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   barcode: str, photo_data: bytes):
        """Process a label photo for a new wine (reply to barcode message)."""
        api = self._get_api(update)
        status_msg = await update.message.reply_text(
            f"📸 Analysing label for <code>{barcode}</code>...",
            parse_mode="HTML",
        )

        loop = asyncio.get_running_loop()
        currency = self._currency_for(update.effective_chat.id)
        country = self._country_for(update.effective_chat.id)
        try:
            grok_data = await loop.run_in_executor(
                None,
                functools.partial(
                    self._photo_callback, barcode, photo_data, currency, country,
                    self._get_pending(api, barcode).get("off_data"),
                ),
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
            off_data = self._get_pending(api, barcode).get("off_data")
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

        pending = self._get_pending(api, barcode)
        if pending.get("photo_data") or pending.get("photo_front"):
            await self._attach_side_photo(status_msg, barcode, photo_data, update)
            return
        self._update_pending(
            api, barcode, grok_merged=grok_data, photo_data=photo_data, photo_front=photo_data,
        )
        await self._show_barcode_label_confirm(status_msg, grok_data, barcode, api)

    async def _handle_edit_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  wine_id: int, photo_data: bytes):
        """Process a label photo to update an existing wine's details."""
        api = self._get_api(update)
        status_msg = await update.message.reply_text(
            f"📸 Analysing label to update wine #{wine_id}...",
            parse_mode="HTML",
        )

        loop = asyncio.get_running_loop()
        currency = self._currency_for(update.effective_chat.id)
        country = self._country_for(update.effective_chat.id)
        try:
            # Use the photo callback with a dummy barcode — we just need Grok's analysis
            grok_data = await loop.run_in_executor(
                None,
                functools.partial(self._photo_callback, "", photo_data, currency, country, None),
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

        # Store for confirmation — keep the photo so a typed hint can't drop it.
        edit_key = f"_edit_{wine_id}"
        with self._lock:
            if edit_key not in self.pending_store:
                self.pending_store[edit_key] = {"wine_id": wine_id, "time": time.time()}
            self.pending_store[edit_key]["grok_merged"] = grok_data
            self.pending_store[edit_key]["photo_data"] = photo_data

        current = {}
        try:
            current = api.get_wine_detail(wine_id) or {}
        except Exception:
            current = {}
        incoming = _clean_wine_data(grok_data)
        grok_bc = grok_data.get("barcode")
        if grok_bc:
            incoming["barcode"] = grok_bc
        fills, overwrites = proposed_wine_update(current, incoming)
        text = "🔍 <b>Update details:</b>\n\n"
        text += _grok_summary(grok_data)
        if overwrites:
            text += "\n\nWould change: " + ", ".join(
                f"{field} {old} → {new}" for field, old, new in overwrites
            )

        if overwrites:
            keyboard = [
                [InlineKeyboardButton(
                    "Yes, apply those changes",
                    callback_data=_cb("edc", wine_id=wine_id, original="all"),
                )],
                [InlineKeyboardButton(
                    "Only fill blanks",
                    callback_data=_cb("edc", wine_id=wine_id, original="fill"),
                )],
                [InlineKeyboardButton("❌ Cancel", callback_data=_cb("x"))],
            ]
        else:
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
            try:
                me = await self._app.bot.get_me()
                self.set_bot_username(getattr(me, "username", None))
                logger.info(f"[invite] bot username @{self.bot_username}")
            except Exception as e:
                logger.warning(f"[invite] getMe failed; invite links need a restart: {e}")
            # PTB 22: initialize() does not run post_init — only run_polling does.
            from integrations.telegram_admin import push_command_menus
            await push_command_menus(self._app.bot, self.admin_chat_ids)

        loop.run_until_complete(_start())
        loop.run_forever()

    def run_in_thread(self):
        """Start the bot in a background thread."""
        thread = threading.Thread(target=self._run_in_own_loop, daemon=True)
        thread.start()
        return thread
