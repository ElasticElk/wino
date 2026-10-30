"""Classify → execute → phrase helpers for typed Telegram messages.

The LLM may only classify (and, for a few paths, phrase facts it was given).
User data always comes from the cellar API. Flag checks live here so a
feature that is off never falls through to open-ended chat.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Public classifier intents. Operational extras (place, price, correct,
# answer, wine_knowledge) stay execute-in-code so we do not lose the
# location split, writes, or general wine questions that are not about
# the user's own data.
CLASSIFY_INTENTS = (
    "cellar_query",
    "club_query",
    "drink_remove",
    "add_wine",
    "search",
    "recommend",
    "list_action",
    "smalltalk_help",
    "unknown",
)
OPERATIONAL_INTENTS = (
    "place",
    "price",
    "correct",
    "answer",
    "wine_knowledge",
)
SCHEMA_INTENTS = CLASSIFY_INTENTS + OPERATIONAL_INTENTS

INTENT_ALIASES = {
    "fact": "cellar_query",
    "club": "club_query",
    "drink": "drink_remove",
    "help": "smalltalk_help",
    "unsure": "unknown",
    "offtopic": "unknown",
    "wine_chat": "wine_knowledge",
}

SLOT_KEYS = (
    "wine",
    "producer",
    "appellation",
    "vintage",
    "colour",
    "grape",
    "person",
    "club",
    "location",
    "quantity",
)

LIST_OPS = ("show", "add", "remove", "none")
UNKNOWN_KINDS = ("offtopic", "unclear", "none")

# Below this, we ask rather than execute. Structured high-signal intents
# still run when the slots they need are filled.
LOW_CONFIDENCE = 0.55
DEFAULT_MIN_REPLY_MS = 1000

OUTCOMES = ("answered", "clarified", "unknown", "flag_off")


def blank_slots() -> dict:
    return {
        "wine": "",
        "producer": "",
        "appellation": "",
        "vintage": None,
        "colour": "",
        "grape": "",
        "person": "",
        "club": "",
        "location": "",
        "quantity": None,
    }


def canonical_intent(raw) -> str:
    name = str(raw or "").strip()
    name = INTENT_ALIASES.get(name, name)
    if name not in SCHEMA_INTENTS:
        return "unknown"
    return name


def _as_confidence(value, intent: str) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        conf = 0.3 if intent == "unknown" else 1.0
    return max(0.0, min(1.0, conf))


def _as_slots(parsed: dict) -> dict:
    slots = blank_slots()
    incoming = parsed.get("slots") if isinstance(parsed.get("slots"), dict) else {}
    for key in SLOT_KEYS:
        if key not in incoming:
            continue
        value = incoming.get(key)
        if key in ("vintage", "quantity"):
            if value in (None, ""):
                slots[key] = None
            else:
                try:
                    slots[key] = int(value)
                except (TypeError, ValueError):
                    slots[key] = None
        else:
            slots[key] = str(value or "").strip()
    if not slots["wine"]:
        slots["wine"] = str(parsed.get("wine_ref") or "").strip()
    fact = parsed.get("fact") if isinstance(parsed.get("fact"), dict) else {}
    if not slots["location"]:
        slots["location"] = str(fact.get("storage_text") or parsed.get("area") or "").strip()
    if not slots["grape"]:
        slots["grape"] = str(fact.get("grape") or "").strip()
    if not slots["colour"]:
        taste = parsed.get("taste") if isinstance(parsed.get("taste"), dict) else {}
        colour = str(taste.get("colour") or "").strip()
        slots["colour"] = "" if colour == "any" else colour
    club = parsed.get("club") if isinstance(parsed.get("club"), dict) else {}
    if not slots["person"]:
        slots["person"] = str(club.get("member_name") or "").strip()
    if not slots["club"]:
        slots["club"] = str(club.get("club_name") or "").strip()
    if not slots["wine"] and club.get("wine_text"):
        slots["wine"] = str(club.get("wine_text") or "").strip()
    if slots["vintage"] is None and club.get("vintage") not in (None, ""):
        try:
            slots["vintage"] = int(club["vintage"])
        except (TypeError, ValueError):
            pass
    correction = parsed.get("correction") if isinstance(parsed.get("correction"), dict) else {}
    if not slots["producer"]:
        slots["producer"] = str(correction.get("producer") or "").strip()
    if not slots["appellation"]:
        slots["appellation"] = str(
            parsed.get("appellation") or correction.get("appellation") or ""
        ).strip()
    return slots


def canonical_parse(parsed: dict | None) -> dict:
    """Normalise a classifier payload (new schema or the previous aliases)."""
    data = dict(parsed) if isinstance(parsed, dict) else {}
    raw = data.get("raw_intent") or data.get("intent") or "unknown"
    intent = canonical_intent(raw)
    unknown_kind = data.get("unknown_kind") or "none"
    if str(raw) == "offtopic" or str(data.get("intent")) == "offtopic":
        unknown_kind = "offtopic"
    if unknown_kind not in UNKNOWN_KINDS:
        unknown_kind = "none"
    if intent == "unknown" and unknown_kind == "none" and str(raw) == "unsure":
        unknown_kind = "unclear"
    list_op = data.get("list_op") or "none"
    if list_op not in LIST_OPS:
        list_op = "none"
    data["intent"] = intent
    data["raw_intent"] = raw
    data["confidence"] = _as_confidence(data.get("confidence"), intent)
    data["slots"] = _as_slots(data)
    data["unknown_kind"] = unknown_kind
    data["list_op"] = list_op
    data.setdefault("wine_ref", data["slots"].get("wine") or "")
    return data


def slot_summary(slots: dict | None) -> str:
    """Names only, for INFO logs. Never the original message."""
    parts = []
    for key in SLOT_KEYS:
        value = (slots or {}).get(key)
        if value in (None, "", 0):
            continue
        parts.append(f"{key}={str(value)[:40]}")
    return ",".join(parts) or "-"


def gated_flag(parsed: dict, features: dict | None) -> str | None:
    """Return the ServiceConfig flag that blocks this intent, else None.

    The classifier always knows about clubs/lists/taste-offs so a flag-off
    question is classified honestly. The executor then refuses in code.
    """
    features = features or {}
    intent = parsed.get("intent") or ""
    if intent == "club_query":
        if not features.get("clubs"):
            return "feature_clubs"
        kind = ((parsed.get("club") or {}) if isinstance(parsed.get("club"), dict) else {}).get("kind")
        if kind == "tasteoff" and not features.get("tasteoffs"):
            return "feature_tasteoffs"
        if kind == "comments" and not features.get("comments"):
            return "feature_club_comments"
        if kind == "list" and not features.get("lists"):
            return "feature_lists"
        return None
    if intent == "list_action" and not features.get("lists"):
        return "feature_lists"
    return None


def needs_clarification(parsed: dict) -> bool:
    intent = parsed.get("intent") or "unknown"
    confidence = parsed.get("confidence") or 0.0
    if intent == "unknown":
        return True
    if confidence >= LOW_CONFIDENCE:
        return False
    slots = parsed.get("slots") or {}
    if intent == "drink_remove" and (slots.get("wine") or parsed.get("wine_ref")):
        return False
    if intent == "search" and slots.get("wine"):
        return False
    if intent in ("price", "correct", "answer"):
        return False
    return True


class ReplyPacer:
    """Hold the first text reply until min_ms after receipt. No extra wait after that."""

    def __init__(self, now: Callable[[], float], sleep, min_ms: int = 0):
        self._now = now
        self._sleep = sleep
        self.min_s = max(0, int(min_ms or 0)) / 1000.0
        self.started = now()
        self.released = False

    async def wait(self) -> float:
        if self.released or self.min_s <= 0:
            self.released = True
            return 0.0
        remain = self.min_s - (self._now() - self.started)
        if remain > 0:
            await self._sleep(remain)
        self.released = True
        return max(0.0, remain)


def attach_pacer(message, pacer: ReplyPacer | None) -> None:
    """Wrap reply_text so the first send waits. Skip when there is no delay
    so test mocks stay mocks.
    """
    if pacer is None or pacer.min_s <= 0 or message is None:
        return
    orig = getattr(message, "reply_text", None)
    if orig is None or getattr(orig, "_whiney_paced", False):
        return

    async def paced(*args, **kwargs):
        await pacer.wait()
        return await orig(*args, **kwargs)

    paced._whiney_paced = True
    paced._whiney_orig = orig
    message.reply_text = paced
