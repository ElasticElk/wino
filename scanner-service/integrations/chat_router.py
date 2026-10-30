"""Plain text that used to become a search, once the early exits have passed.

A fast parse decides what the sentence is. Shelf questions are then answered
from the stock rows. A taste request is one reasoning call, with the
placeholder updated while it runs so the chat never looks stuck. Anything
we cannot understand falls back to today's search.
"""

from __future__ import annotations

import asyncio
import html
import logging
import statistics
import time
from types import SimpleNamespace

import requests
from telegram.error import BadRequest

from integrations.card_store import StoreError
from integrations.cellar_facts import answer as answer_fact
from integrations.cellar_facts import quick_pick
from integrations.chat_intent import (
    canonical_parse,
    gated_flag,
    needs_clarification,
    note_turn,
    slot_summary,
    turn_pacer,
)
from integrations.chat_models import (
    PARSE_MODEL,
    RECOMMEND_MODEL,
    ChatFailure,
    compact_cellar,
)
from integrations.copy import sniff_locale
from integrations.copy import t as copy_t

logger = logging.getLogger(__name__)

CHAT_ACTIONS = {
    "cf", "cc", "cq", "ck", "cn", "co", "cx", "cr", "cw", "cg", "cm", "ca",
    "ct", "cp", "cl",
}

# English fallbacks so existing tests that import PLACE_LINES keep working.
# User-facing replies go through copy_t with the turn locale.
CHAT_RESTING = copy_t("en", "chat.resting")
REC_SLOW = copy_t("en", "chat.rec_slow")
REC_QUICK = copy_t("en", "chat.rec_quick")
CELLAR_DOWN = copy_t("en", "chat.cellar_down")
WRITE_FAILED = copy_t("en", "chat.write_failed")
WINE_GONE = copy_t("en", "chat.wine_gone")
WRITE_REJECTED_FALLBACK = copy_t("en", "chat.write_rejected")
ASK_LOST = copy_t("en", "chat.ask_lost")
FOLLOWUP_FOOTER = copy_t("en", "chat.followup_footer")
FINISHED = copy_t("en", "chat.finished")
PLACE_ASK_LOCATION = copy_t("en", "chat.place.ask_location")
PLACES_DOWN = copy_t("en", "chat.place.down")
PLACES_NONE = copy_t("en", "chat.place.none")
HELP_COMMANDS = copy_t("en", "chat.help.commands")

PROGRESS_STEPS = (
    (1.5, "matching"),
    (4.0, "narrowing"),
    (8.0, "slow"),
)
TYPING_EVERY = 4.0
PLACE_LINES = (
    "🔎 Searching the web for places near you…",
    "🍷 Reading wine lists…",
    "⭐ Checking reviews…",
    "⚖️ Matching them to your taste…",
    "🗺️ Narrowing it down to the best few…",
    "⏳ Still searching — good wine lists take a moment…",
)
PLACE_EVERY = 3.5
# Rotate for longer than the search can take (PLACE_TIMEOUT).
PLACE_STEPS = tuple(
    (PLACE_EVERY * (i + 1), (i + 1) % len(PLACE_LINES)) for i in range(16)
)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _place_lines(locale: str) -> tuple[str, ...]:
    return tuple(
        copy_t(locale, key)
        for key in (
            "chat.place.searching", "chat.place.reading", "chat.place.reviews",
            "chat.place.matching", "chat.place.narrowing", "chat.place.still",
        )
    )


def _tools():
    from integrations.telegram_bot import (
        _cb,
        _glyph,
        _not_modified,
        _unconfigured_storage,
        empty_cellar,
    )
    return _cb, _glyph, _not_modified, _unconfigured_storage, empty_cellar


def _markup(pairs: list[tuple[str, str]]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if not pairs:
        return None
    rows = []
    row = []
    for label, data in pairs:
        row.append(InlineKeyboardButton(label[:30], callback_data=data))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


class ProgressTicker:
    """Edits one placeholder on a timer. Stops the moment the answer arrives."""

    def __init__(self, message, lines: dict[str, str], typing=None, sleep=None, steps=PROGRESS_STEPS):
        self._message = message
        self._lines = lines
        self._steps = steps
        self._typing = typing
        self._sleep = sleep or asyncio.sleep
        self._closed = False
        self._task = None
        self._typed_at = 0.0

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return False

    async def _run(self):
        elapsed = 0.0
        for delay, key in self._steps:
            await self._sleep(max(0.0, delay - elapsed))
            elapsed = delay
            if self._closed:
                return
            try:
                await self._edit(self._lines[key])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("[chat] progress edit skipped detail=%s", exc)
            if self._typing is not None and elapsed - self._typed_at >= TYPING_EVERY:
                self._typed_at = elapsed
                try:
                    await self._typing()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.info("[chat] typing indicator failed")

    async def _edit(self, text: str):
        if self._closed:
            return
        _cb, _glyph, not_modified, _storage, _empty = _tools()
        try:
            await self._message.edit_text(text, parse_mode="HTML")
        except BadRequest as exc:
            if not not_modified(exc):
                raise


class ChatRouter:
    def __init__(self, handler, chat, sleep=None, now=None):
        self.handler = handler
        self.chat = chat
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._staff_alert_at: dict[str, float] = {}

    def status_line(self) -> str:
        if self.chat is None:
            return ""
        ready = self.chat.healthy(PARSE_MODEL) and self.chat.healthy(RECOMMEND_MODEL)
        if ready:
            return "\n💬 Chat: ready"
        return "\n💬 Chat: resting (model unavailable)"

    def _key(self, chat_id) -> str:
        return f"ask:{chat_id}"

    def _load(self, chat_id) -> dict:
        data = self.handler._asks.get(self._key(str(chat_id)))
        return dict(data) if isinstance(data, dict) else {}

    def _save(self, chat_id, data: dict) -> bool:
        try:
            self.handler._asks[self._key(str(chat_id))] = dict(data)
            return True
        except StoreError:
            logger.warning("[chat] kind=store chat=%s", chat_id)
            return False

    def _clear(self, chat_id) -> None:
        try:
            self.handler._asks.pop(self._key(str(chat_id)), None)
        except StoreError:
            logger.warning("[chat] kind=store chat=%s", chat_id)

    async def abandon(self, chat_id) -> None:
        self._drop_place(chat_id)
        ask = self._load(chat_id)
        intent_id = ask.get("intent_id")
        if intent_id and ask.get("stage"):
            api = self._api_for(chat_id)
            if api is not None:
                await self._call(api.update_intent, intent_id, {"outcome": "abandoned"})
        self._clear(chat_id)

    def _api_for(self, chat_id):
        try:
            return self.handler._get_api(str(chat_id))
        except Exception:
            return getattr(self.handler, "api", None)

    def _note(self, **kwargs) -> None:
        meta = note_turn(**kwargs)
        if meta is not None:
            handler_meta = getattr(self.handler, "_text_meta", None)
            if isinstance(handler_meta, dict):
                handler_meta.update(kwargs)
            return
        meta = getattr(self.handler, "_text_meta", None)
        if not isinstance(meta, dict):
            meta = {}
            self.handler._text_meta = meta
        meta.update(kwargs)

    def _min_reply_ms(self) -> int:
        fn = getattr(self.handler, "min_reply_ms", None)
        if callable(fn):
            try:
                return max(0, int(fn()))
            except (TypeError, ValueError):
                return 0
        return int(getattr(self.handler, "_min_reply_ms", 0) or 0)

    async def _wait_pace(self) -> None:
        pacer = turn_pacer()
        if pacer is None:
            pacer = getattr(self.handler, "_text_pacer", None)
        if pacer is not None:
            await pacer.wait()

    def _reply_locale(self, update, chat_id, parsed=None, text: str = "") -> str:
        lang = (parsed or {}).get("language") or ""
        if lang in ("en", "af"):
            return lang
        if not text:
            message = getattr(update, "message", None)
            text = getattr(message, "text", "") or ""
        return sniff_locale(text, self._locale(update, chat_id))

    def _c(self, locale: str, key: str, **kwargs) -> str:
        return copy_t(locale or "en", key, **kwargs)

    async def handle_text(self, update, context, text: str) -> bool:
        box: dict = {}
        try:
            await self._handle_text(update, context, text, box)
        except Exception:
            logger.exception("[chat] kind=unexpected chat=%s", getattr(update.effective_chat, "id", ""))
            self._note(outcome="unknown")
            message = box.get("placeholder")
            if asyncio.isfuture(message) or isinstance(message, asyncio.Task):
                try:
                    message = await message
                except Exception:
                    message = None
            if message is not None:
                chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
                locale = box.get("locale") or self._reply_locale(update, chat_id, text=text)
                await self._edit(message, self._c(locale, "chat.resting"), update)
                return True
            return False
        return bool(box.get("handled"))

    async def _handle_text(self, update, context, text: str, box: dict) -> None:
        api = self.handler._get_api(update)
        chat_id = str(update.effective_chat.id)
        loop = asyncio.get_running_loop()
        wines = []
        cellar_down = False
        try:
            wines = await loop.run_in_executor(None, api.wines_in_stock)
        except Exception as exc:
            cellar_down = True
            logger.warning("[chat] kind=cellar_down model=cellar chat=%s detail=%s", chat_id, exc)
            if not self._clubs_on():
                await self._say(update, self._c(self._reply_locale(update, chat_id, text=text), "chat.cellar_down"), self._retry_button())
                self._save(chat_id, {"retry_text": text})
                box["handled"] = True
                return
        waiting = self._pending_place(chat_id)
        if waiting:
            self._drop_place(chat_id)
            if _looks_like_area(text):
                await self._search_places(
                    update, context, api, chat_id, waiting["query"], area=text.strip(), clear_keyboard=True,
                )
                box["handled"] = True
                return
        if _whole_word_in_names(text, wines):
            box["handled"] = False
            return
        ask = self._load(chat_id)
        ctx = {
            "open_question": ask.get("question") if ask.get("stage") == "question" else None,
            "options": ask.get("options") or [],
            "shown_wine": _shown_name(ask, wines),
            "label_card_open": bool(self._open_cards(api)),
            "features": self._chat_features(),
        }
        started_classify = self._now()

        async def _parsed():
            try:
                return await loop.run_in_executor(None, lambda: self.chat.parse(text, ctx))
            except ChatFailure as exc:
                return exc

        async def _storages():
            try:
                return await loop.run_in_executor(None, api.list_storages)
            except Exception:
                if self._clubs_on():
                    return []
                raise

        try:
            parsed, storages = await asyncio.gather(_parsed(), _storages())
        except Exception as exc:
            logger.warning("[chat] kind=cellar_down model=cellar chat=%s detail=%s", chat_id, exc)
            locale = self._reply_locale(update, chat_id, text=text)
            await self._say(update, self._c(locale, "chat.cellar_down"), self._retry_button())
            self._save(chat_id, {"retry_text": text})
            box["handled"] = True
            return
        classify_ms = _ms(self._now() - started_classify) if started_classify else 0
        if isinstance(parsed, ChatFailure):
            await self._alert_staff(context, parsed, PARSE_MODEL)
            self._note(intent="unknown", confidence="", slots="-", outcome="unknown",
                       classify_ms=classify_ms)
            if _short_phrase(text):
                box["handled"] = False
                return
            logger.warning("[chat] kind=%s model=%s chat=%s", parsed.kind, PARSE_MODEL, chat_id)
            await self._say(update, self._c(self._reply_locale(update, chat_id, text=text), "chat.resting"))
            box["handled"] = True
            return
        parsed = canonical_parse(parsed)
        intent = parsed["intent"]
        confidence = parsed["confidence"]
        slots = parsed["slots"]
        locale = self._reply_locale(update, chat_id, parsed, text)
        box["locale"] = locale
        self._note(
            intent=intent, confidence=f"{confidence:.2f}", slots=slot_summary(slots),
            classify_ms=classify_ms,
        )
        logger.info(
            "[chat] parse intent=%s confidence=%.2f slots=%s chat=%s text=%r",
            intent, confidence, slot_summary(slots), chat_id, text[:60],
        )
        execute_at = self._now()
        features = self._chat_features()
        blocked = gated_flag(parsed, features)
        if blocked:
            await self._flag_off(update, chat_id, locale)
            self._note(outcome="flag_off", execute_ms=_ms(self._now() - execute_at))
            box["handled"] = True
            return
        cellar_ok_intents = {
            "club_query", "smalltalk_help", "unknown", "wine_knowledge", "place",
            "list_action", "add_wine",
        }
        if cellar_down and intent not in cellar_ok_intents:
            await self._say(update, self._c(locale, "chat.cellar_down"), self._retry_button())
            self._save(chat_id, {"retry_text": text})
            self._note(outcome="unknown", execute_ms=_ms(self._now() - execute_at))
            box["handled"] = True
            return
        if intent == "search":
            box["handled"] = False
            self._note(outcome="answered")
            return
        if intent == "unknown" and parsed.get("unknown_kind") != "offtopic" and _short_phrase(text):
            box["handled"] = False
            self._note(outcome="answered")
            return
        if intent == "unknown" and parsed.get("unknown_kind") == "offtopic":
            await self._say(update, f"🍷 {self._c(locale, 'chat.offtopic')}", self._pipeline_markup())
            self._note(outcome="unknown", execute_ms=_ms(self._now() - execute_at))
            box["handled"] = True
            return
        if needs_clarification(parsed):
            await self._clarify(update, chat_id, parsed, locale)
            self._note(outcome="clarified", execute_ms=_ms(self._now() - execute_at))
            box["handled"] = True
            return
        handled = await self._execute(
            update, context, api, chat_id, text, parsed, wines, storages, ask, box,
            locale=locale,
        )
        self._note(
            outcome=self._outcome_of(box, handled),
            execute_ms=_ms(self._now() - execute_at),
        )
        box["handled"] = handled

    def _pending_place(self, chat_id) -> dict | None:
        try:
            data = self.handler._asks.get(f"place:{chat_id}")
        except StoreError:
            return None
        return dict(data) if isinstance(data, dict) and data.get("query") else None

    def _drop_place(self, chat_id) -> None:
        try:
            self.handler._asks.pop(f"place:{chat_id}", None)
        except StoreError:
            logger.warning("[chat] kind=store chat=%s", chat_id)

    async def _place(self, update, context, api, chat_id, text, area) -> None:
        if area:
            await self._search_places(update, context, api, chat_id, text, area=area)
            return
        try:
            self.handler._asks[f"place:{chat_id}"] = {"query": text}
        except StoreError:
            logger.warning("[chat] kind=store chat=%s", chat_id)
        from telegram import KeyboardButton, ReplyKeyboardMarkup
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Send my location", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        locale = self._reply_locale(update, chat_id, text=text)
        await update.message.reply_text(
            self._c(locale, "chat.place.ask_location"), reply_markup=keyboard,
        )

    async def _search_places(self, update, context, api, chat_id, query, *, area="", lat=None, lon=None,
                             clear_keyboard=False):
        loop = asyncio.get_running_loop()
        locale = self._reply_locale(update, chat_id, text=query)
        lines = _place_lines(locale)
        if clear_keyboard:
            # Telegram won't edit a message that carries a reply-keyboard removal,
            # so it goes on its own message and the placeholder stays editable.
            from telegram import ReplyKeyboardRemove
            got = (
                self._c(locale, "chat.place.got_location")
                if lat is not None
                else self._c(locale, "chat.place.got_area", area=area)
            )
            await update.message.reply_text(got, reply_markup=ReplyKeyboardRemove())
        placeholder = await update.message.reply_text(lines[0])
        try:
            taste = await loop.run_in_executor(None, api.get_taste)
            profile = (taste or {}).get("summary") or ""
        except Exception as exc:
            logger.warning("[chat] kind=taste chat=%s detail=%s", chat_id, exc)
            profile = ""
        country = (getattr(self.handler, "_chat_id_to_country", {}) or {}).get(str(chat_id), "")
        started = self._now()
        try:
            async with ProgressTicker(
                placeholder, dict(enumerate(lines)), typing=lambda: self._typing(context, chat_id),
                sleep=self._sleep, steps=PLACE_STEPS,
            ):
                places = await loop.run_in_executor(
                    None,
                    lambda: self.chat.find_places(
                        query, profile, area=area, lat=lat, lon=lon, country=country,
                    ),
                )
        except ChatFailure as exc:
            await self._alert_staff(context, exc, "places")
            logger.warning("[chat] kind=%s model=places chat=%s", exc.kind, chat_id)
            await self._edit(placeholder, self._c(locale, "chat.place.down"), update)
            return
        logger.info("[chat] places n=%s ms=%s chat=%s", len(places), _ms(self._now() - started), chat_id)
        if not places:
            await self._edit(placeholder, self._c(locale, "chat.place.none"), update)
            return
        await self._edit(placeholder, _render_places(places, area, locale), update)

    def _staff(self, chat_id) -> bool:
        return str(chat_id) in getattr(self.handler, "admin_chat_ids", [])

    def _clubs_on(self) -> bool:
        flag_on = getattr(self.handler, "flag_on", None)
        return bool(flag_on("feature_clubs")) if callable(flag_on) else False

    def _chat_features(self) -> dict:
        flag_on = getattr(self.handler, "flag_on", None)

        def on(key):
            return bool(flag_on(key)) if callable(flag_on) else False

        return {
            "clubs": on("feature_clubs"),
            "lists": on("feature_lists"),
            "comments": on("feature_club_comments"),
            "tasteoffs": on("feature_tasteoffs"),
            "global_clubs": on("feature_global_clubs"),
        }

    def _locale(self, update, chat_id) -> str:
        fn = getattr(self.handler, "locale_for", None)
        if callable(fn):
            return fn(update=update, chat_id=chat_id)
        return "en"

    def _outcome_of(self, box: dict, handled: bool) -> str:
        if box.get("outcome"):
            return box["outcome"]
        if handled:
            return "answered"
        return "answered"

    def _pipeline_markup(self):
        from integrations.copy import t as copy_t
        cb, *_rest = _tools()
        return _markup([
            (copy_t("en", "chat.btn.stock"), cb("ct")),
            (copy_t("en", "chat.btn.drink"), cb("cp")),
            (copy_t("en", "chat.btn.help"), cb("cl")),
        ])

    async def _flag_off(self, update, chat_id, locale: str | None = None) -> None:
        locale = locale or self._reply_locale(update, chat_id)
        await self._say(update, f"🍷 {self._c(locale, 'chat.flag_off')}", self._pipeline_markup())

    async def _clarify(self, update, chat_id, parsed, locale: str | None = None) -> None:
        locale = locale or self._reply_locale(update, chat_id, parsed)
        kind = parsed.get("unknown_kind") or "unclear"
        key = "chat.unknown" if kind == "unclear" or parsed.get("intent") == "unknown" else "chat.clarify"
        await self._say(update, f"🤔 {self._c(locale, key)}", self._pipeline_markup())

    async def _execute(self, update, context, api, chat_id, text, parsed, wines, storages, ask, box,
                       locale: str = "en") -> bool:
        intent = parsed.get("intent")
        if intent == "club_query":
            return await self._club(update, context, api, chat_id, parsed, text, locale)
        if intent == "smalltalk_help":
            await self._help(update, context, api, chat_id, text, parsed, locale)
            return True
        if intent == "drink_remove":
            wine_ref = (parsed.get("wine_ref") or parsed.get("slots", {}).get("wine") or "").strip()
            fact = parsed.get("fact") if isinstance(parsed.get("fact"), dict) else {}
            location = (fact.get("storage_text") or parsed.get("slots", {}).get("location") or "").strip()
            if not wine_ref or wine_ref.casefold() in {
                "this", "it", "that", "dit", "hierdie",
            }:
                return await self.handler._maybe_typed_stock(update, context, "Drank this")
            return await self.handler._typed_drink_named(
                update, context, api, None, wine_ref, location,
            )
        if intent == "wine_knowledge":
            await self._wine_chat(update, context, api, chat_id, text, locale)
            return True
        if intent == "place":
            area = (parsed.get("area") or parsed.get("slots", {}).get("location") or "").strip()
            await self._place(update, context, api, chat_id, text, area)
            return True
        if intent == "cellar_query":
            return await self._fact(update, context, api, chat_id, parsed, wines, storages, locale)
        if intent in ("recommend", "answer"):
            if not wines:
                await self._empty(update, api)
                return True
            placeholder = asyncio.create_task(self._placeholder(update, wines, locale))
            box["placeholder"] = placeholder
            if intent == "answer" and ask.get("stage") == "question":
                answer = _answer_text(parsed, text, ask)
                sentence = ask.get("sentence") or text
                taste = ask.get("taste") or parsed.get("taste") or {}
                await self._recommend(
                    chat_id, api, sentence, taste, placeholder, False, update, context,
                    wines, answer=answer, ask=ask, locale=locale,
                )
            else:
                await self._recommend(
                    chat_id, api, text, parsed.get("taste") or {}, placeholder, True,
                    update, context, wines, locale=locale,
                )
            return True
        if intent in ("price", "correct"):
            await self._write(update, context, api, chat_id, parsed, wines, ask, locale)
            return True
        if intent == "add_wine":
            return await self._add_wine(update, context, api, parsed, locale)
        if intent == "list_action":
            return await self._list_action(update, context, api, chat_id, parsed, locale)
        return False

    async def _add_wine(self, update, context, api, parsed, locale: str = "en") -> bool:
        from integrations.telegram_bot import TYPED_ADD_TTL
        wine = ((parsed.get("slots") or {}).get("wine") or "").strip()
        if not wine or wine.casefold() in {"this", "it", "that", "dit", "hierdie"}:
            try:
                cards = self.handler._open_label_cards(
                    api, max_age=TYPED_ADD_TTL, include_complete=True,
                )
            except Exception:
                cards = []
            if cards and await self.handler._maybe_typed_add(update, context, "add"):
                return True
        await self._say(update, f"📷 {self._c(locale, 'chat.add_photo')}")
        return True

    async def _list_action(self, update, context, api, chat_id, parsed, locale: str = "en") -> bool:
        op = parsed.get("list_op") or "show"
        slots = parsed.get("slots") or {}
        wine = (slots.get("wine") or "").strip()
        if op == "add" and not wine:
            await self._say(update, f"📝 {self._c(locale, 'chat.list_add_need_wine')}")
            return True
        if op == "add" and wine:
            flow = self.handler._list_flow()
            list_id = flow._personal_list_id(api)
            if not list_id:
                await self._say(update, self._c(locale, "list.err"))
                return True
            payload = {
                "name": wine[:100],
                "producer": (slots.get("producer") or "")[:100],
                "source": "typed",
            }
            if slots.get("vintage"):
                payload["vintage"] = slots["vintage"]
            if slots.get("grape"):
                payload["grape"] = slots["grape"]
            try:
                result = api.add_list_item(list_id, payload)
            except Exception as exc:
                logger.warning("[chat] list add failed chat=%s detail=%s", chat_id, exc)
                await self._say(update, self._c(locale, "list.err"))
                return True
            if not isinstance(result, dict) or result.get("error"):
                await self._say(update, self._c(locale, "list.err"))
                return True
            key = "chat.list.already_named" if result.get("already") else "chat.list.added_named"
            await self._say(update, self._c(locale, key, name=_esc(wine)))
            return True
        await self.handler._list_flow().cmd_list(update, context)
        return True

    async def _club(self, update, context, api, chat_id, parsed, text, locale: str | None = None) -> bool:
        from integrations.club_chat import render_club

        locale = locale or self._reply_locale(update, chat_id, parsed, text)
        payload = dict(parsed.get("club") or {})
        slots = parsed.get("slots") or {}
        if not payload.get("wine_text") and slots.get("wine"):
            payload["wine_text"] = slots["wine"]
        if not payload.get("grape") and slots.get("grape"):
            payload["grape"] = slots["grape"]
        if payload.get("vintage") in (None, "") and slots.get("vintage"):
            payload["vintage"] = slots["vintage"]
        if not payload.get("member_name") and slots.get("person"):
            payload["member_name"] = slots["person"]
        if not payload.get("club_name") and slots.get("club"):
            payload["club_name"] = slots["club"]
        kind = payload.get("kind") or "none"
        if kind in ("none", ""):
            payload["kind"] = "members"
        remembered = self._load(chat_id)
        pending = remembered.get("club_ask") or {}
        if pending.get("club_id") and not payload.get("club_id"):
            payload["club_id"] = pending.get("club_id")
        if pending.get("member_id") and not payload.get("member_id"):
            payload["member_id"] = pending.get("member_id")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: api.club_ask(payload))
        except Exception as exc:
            logger.warning("[chat] club status=down chat=%s detail=%s", chat_id, exc)
            await self._say(update, self._c(locale, "chat.club.down"))
            return True
        if not isinstance(result, dict):
            result = {}
        status = result.get("status") or result.get("error") or ""
        logger.info("[chat] club kind=%s status=%s chat=%s", payload.get("kind"), status, chat_id)
        if result.get("error") == "not_enabled" or status in ("fallback", "not_enabled"):
            await self._flag_off(update, chat_id, locale)
            return True
        text_out = render_club(result, locale)
        self._save(chat_id, {
            **remembered,
            "club_ask": payload,
            "club_kind": payload.get("kind"),
        })
        await self._say(update, text_out, self._club_markup(result))
        return True

    def _club_markup(self, result: dict):
        from integrations.club_chat import club_pick_buttons

        cb, *_rest = _tools()
        pairs = []
        for label, action, wine_id, page in club_pick_buttons(result):
            kwargs = {}
            if wine_id is not None:
                kwargs["wine_id"] = wine_id
            if page is not None:
                kwargs["page"] = page
            if action == "ca":
                if page == 1:
                    kwargs["original"] = "list"
                elif wine_id:
                    kwargs["original"] = "members"
                else:
                    kwargs["original"] = "clubs"
            pairs.append((label, cb(action, **kwargs)))
        return _markup(pairs) if pairs else None

    async def _club_from_pick(self, update, context, api, chat_id, **fields) -> None:
        ask = self._load(chat_id)
        payload = dict(ask.get("club_ask") or {})
        payload.update(fields)
        parsed = {"club": payload, "intent": "club_query"}
        await self._club(update, context, api, chat_id, canonical_parse(parsed), "")

    async def _help(self, update, context, api, chat_id, text, parsed, locale: str = "en") -> None:
        # Known topics still have a canned line if the model is down.
        topic = parsed.get("help_topic") or "other"
        fallback = self._c(locale, "chat.help.commands")
        if topic == "invite":
            fallback = self._c(locale, "chat.help.invite")
        elif topic == "new_cellar":
            key = "chat.help.new_cellar_staff" if self._staff(chat_id) else "chat.help.new_cellar"
            fallback = self._c(locale, key)
        elif topic == "switch":
            fallback = self._c(locale, "chat.help.switch")
        elif topic == "photo":
            fallback = self._c(locale, "chat.help.photo")
        elif topic == "take_out":
            fallback = self._c(locale, "chat.help.take_out")
        markup = None
        if topic == "take_out":
            markup = _markup([("Catch up", "a:cu,o:go")])
        loop = asyncio.get_running_loop()
        started = self._now()
        try:
            reply = await loop.run_in_executor(
                None,
                lambda: self.chat.support(
                    text, staff=self._staff(chat_id),
                    household_id=getattr(api, "household_id", None),
                    features=self._chat_features(),
                ),
            )
        except ChatFailure as exc:
            await self._alert_staff(context, exc, PARSE_MODEL)
            logger.warning("[chat] kind=%s model=support chat=%s", exc.kind, chat_id)
            await self._say(update, f"🍷 {fallback}", markup)
            return
        logger.info("[chat] support topic=%s ms=%s chat=%s", topic, _ms(self._now() - started), chat_id)
        await self._say(update, f"🍷 {_esc(reply)}", markup)

    async def _wine_chat(self, update, context, api, chat_id, text, locale: str = "en") -> None:
        loop = asyncio.get_running_loop()
        try:
            taste = await loop.run_in_executor(None, api.get_taste)
            profile = (taste or {}).get("summary") or ""
        except Exception as exc:
            logger.warning("[chat] kind=taste chat=%s detail=%s", chat_id, exc)
            profile = ""
        try:
            await self._typing(context, chat_id)
        except Exception:
            pass
        started = self._now()
        try:
            reply = await loop.run_in_executor(
                None,
                lambda: self.chat.wine_chat(
                    text, profile, household_id=getattr(api, "household_id", None),
                ),
            )
        except ChatFailure as exc:
            await self._alert_staff(context, exc, RECOMMEND_MODEL)
            logger.warning("[chat] kind=%s model=%s chat=%s", exc.kind, RECOMMEND_MODEL, chat_id)
            await self._say(update, self._c(locale, "chat.resting"))
            return
        logger.info("[chat] wine_chat ms=%s chat=%s", _ms(self._now() - started), chat_id)
        await self._say(update, f"🍷 {_esc(reply)}")

    async def _fact(self, update, context, api, chat_id, parsed, wines, storages,
                    locale: str | None = None) -> bool:
        locale = locale or self._reply_locale(update, chat_id, parsed)
        if not wines:
            await self._empty(update, api)
            return True
        fact = parsed.get("fact") or {}
        # Storages the payload doesn't mention still count when the user names one.
        known = []
        for wine in wines:
            for item in wine.get("items") or []:
                name = item.get("storage") or ""
                if name and name not in known:
                    known.append(name)
        for storage in storages or []:
            name = storage.get("name") if isinstance(storage, dict) else ""
            if name and name not in known:
                known.append(name)
        reply = answer_fact(fact, wines, locale=locale)
        if reply is None:
            reply = _slot_fact(parsed, wines, locale=locale)
        if reply is None:
            await self._say(
                update,
                f"🍷 {self._c(locale, 'chat.dont_know')}",
                self._pipeline_markup(),
            )
            return True
        if reply.kind == "list_all":
            await self.handler._cmd_stock(update, context)
            return True
        buttons = []
        cb, *_rest = _tools()
        extra = {}
        if reply.kind == "choose_storage":
            for index, name in enumerate(reply.storage_choices):
                buttons.append((name, cb("cf", page=index)))
            extra = {"fact": fact, "choices": list(reply.storage_choices)}
        elif reply.suggest_close:
            buttons.append(("Suggest something close", cb("cc")))
            extra = {"close": reply.suggest_close, "fact": fact}
        if extra:
            merged = self._load(chat_id)
            merged.update(extra)
            self._save(chat_id, merged)
        await self._say(update, reply.text, _markup(buttons) if buttons else None)
        return True

    async def _recommend(self, chat_id, api, sentence, taste, placeholder, allow_question,
                         update, context, wines, answer=None, ask=None, retried=False,
                         locale: str = "en"):
        loop = asyncio.get_running_loop()
        taste_summary = ""
        history = []

        async def _taste_history():
            try:
                taste_payload, history_payload = await asyncio.gather(
                    loop.run_in_executor(None, api.get_taste),
                    loop.run_in_executor(None, lambda: api.stock_history(limit=5)),
                )
                return (taste_payload or {}).get("summary") or "", (history_payload or {}).get("items") or []
            except Exception as exc:
                logger.warning("[chat] kind=taste chat=%s detail=%s", chat_id, exc)
                return "", []

        if asyncio.isfuture(placeholder) or isinstance(placeholder, asyncio.Task):
            (taste_summary, history), placeholder = await asyncio.gather(
                _taste_history(), placeholder,
            )
        else:
            taste_summary, history = await _taste_history()
        colour = (taste or {}).get("colour") or ""
        # Taste words stay in the prompt. They must not drop a bottle here.
        block, lookup = compact_cellar(wines, colour=colour)
        # Taste words must not filter the cellar. Colour, when explicit, may,
        # and only inside compact_cellar once the list is past the cap.
        lines = _progress_lines(taste, sum(w.get("stock_count") or 0 for w in wines), locale)
        started = self._now()
        try:
            async with ProgressTicker(placeholder, lines, typing=lambda: self._typing(context, chat_id), sleep=self._sleep):
                result = await loop.run_in_executor(
                    None,
                    lambda: self.chat.recommend(
                        sentence, taste or {}, block, taste_summary, history,
                        allow_question=allow_question,
                        household_id=getattr(api, "household_id", None),
                        answer=answer,
                        valid_ids=set(lookup),
                        locale=locale,
                    ),
                )
        except ChatFailure as exc:
            await self._alert_staff(context, exc, RECOMMEND_MODEL)
            logger.warning("[chat] kind=%s model=%s chat=%s", exc.kind, RECOMMEND_MODEL, chat_id)
            self._save(chat_id, {
                **(ask or {}),
                "sentence": sentence, "taste": taste or {}, "allow_question": allow_question,
                "answer": answer,
            })
            if exc.kind == "bad_output" and not retried and self._now() - started < 10:
                await self._recommend(
                    chat_id, api, sentence, taste, placeholder, allow_question,
                    update, context, wines, answer=answer, ask=ask, retried=True,
                    locale=locale,
                )
                return
            await self._quick(
                chat_id, api, sentence, taste, placeholder, wines, history, update,
                meta={"model": "quick", "latency_ms": _ms(self._now() - started)},
                ask=ask, locale=locale,
            )
            return
        meta = {"model": RECOMMEND_MODEL, "latency_ms": _ms(self._now() - started)}
        logger.info("[chat] recommend model=%s ms=%s chat=%s", meta["model"], meta["latency_ms"], chat_id)
        if (result.get("question") or "").strip() and allow_question:
            await self._ask_question(chat_id, api, sentence, taste, placeholder, result, wines, meta=meta)
            return
        await self._show_pick(
            chat_id, api, sentence, taste, placeholder, result, lookup, wines, update,
            answer=answer, ask=ask, meta=meta, locale=locale,
        )

    async def _ask_question(self, chat_id, api, sentence, taste, placeholder, result, wines, meta=None):
        cb, *_ = _tools()
        options = [str(o) for o in (result.get("options") or []) if str(o).strip()][:4]
        question = result.get("question") or ""
        buttons = [(opt, cb("cq", page=i)) for i, opt in enumerate(options)]
        remembered = self._save(chat_id, {
            "stage": "question", "sentence": sentence, "taste": taste,
            "question": question, "options": options,
            "message_id": getattr(placeholder, "message_id", None),
        })
        await self._edit(placeholder, f"🤔 {_esc(question)}", None, _markup(buttons))
        payload = _ask_payload(sentence, taste, question, options, wines, result)
        payload.update(meta or {})
        intent_id = await self._record(api, payload)
        if intent_id and remembered:
            current = self._load(chat_id)
            current["intent_id"] = intent_id
            self._save(chat_id, current)

    async def _show_pick(self, chat_id, api, sentence, taste, placeholder, result, lookup,
                         wines, update, answer=None, fallback=False, ask=None, meta=None,
                         locale: str = "en"):
        cb, *_ = _tools()
        pick = result.get("pick") or {}
        wine = lookup.get(pick.get("id")) or _by_id(wines, pick.get("id"))
        if wine is None:
            await self._edit(placeholder, self._c(locale, "chat.rec_slow"), update, self._retry_button())
            return
        runner = result.get("runner_up") or {}
        runner_wine = lookup.get(runner.get("id")) if runner.get("id") else None
        queue = []
        if runner_wine is not None:
            queue.append({"id": runner["id"], "reason": runner.get("reason") or "", "evidence": runner.get("evidence") or "none"})
        for alt in result.get("alternatives") or []:
            if lookup.get(alt.get("id")) or _by_id(wines, alt.get("id")):
                queue.append({"id": alt.get("id"), "reason": alt.get("reason") or "", "evidence": alt.get("evidence") or "none"})
        text = _render_pick(
            wine, pick.get("reason") or "", runner_wine, runner.get("reason") or "",
            result.get("no_evidence_for") or [], locale=locale,
        )
        if fallback:
            text = self._c(locale, "chat.rec_quick") + "\n" + text
        buttons = _pick_buttons(cb, wine, runner_wine, fallback=fallback)
        previous = (ask or {}).get("intent_id")
        remembered = True
        state = {
            "stage": "shown", "sentence": sentence, "taste": taste, "answer": answer,
            "shown": wine.get("id"), "queue": queue, "rejected": list((ask or {}).get("rejected") or []),
            "message_id": getattr(placeholder, "message_id", None),
            "fallback": fallback, "intent_id": previous,
        }
        if not self._save(chat_id, state):
            text += f"\n<i>{self._c(locale, 'chat.followup_footer')}</i>"
            buttons = [b for b in buttons if "Something else" not in b[0]]
            remembered = False
        await self._edit(placeholder, text, update, _markup(buttons))
        ids = [wine.get("id")] + [item["id"] for item in queue]
        payload = _ask_payload(sentence, taste, "", [], wines, result, shown=wine.get("id"), answer=answer)
        payload["price_band"] = _price_band(wines, ids, self._currency(chat_id))
        payload["fallback"] = fallback
        meta = {key: value for key, value in (meta or {}).items() if key in ("model", "latency_ms")}
        payload.update(meta)
        if previous:
            await self._call(api.update_intent, previous, {
                "shown": wine.get("id"), "outcome": "pending", "fallback": fallback, **meta,
            })
        else:
            intent_id = await self._record(api, payload)
            if intent_id and remembered:
                current = self._load(chat_id)
                current["intent_id"] = intent_id
                self._save(chat_id, current)

    async def _quick(self, chat_id, api, sentence, taste, placeholder, wines, history, update,
                     meta=None, ask=None, locale: str = "en"):
        picked = quick_pick(taste or {}, wines, history, locale=locale)
        if not picked:
            await self._edit(placeholder, self._c(locale, "chat.resting"), update)
            return
        first, *rest = picked
        lookup = {row["id"]: row["wine"] for row in picked}
        result = {
            "pick": {"id": first["id"], "reason": first["reason"], "evidence": first["evidence"]},
            "runner_up": {"id": 0, "reason": "", "evidence": "none"},
            "alternatives": [
                {"id": row["id"], "reason": row["reason"], "evidence": row["evidence"]}
                for row in rest
            ],
            "no_evidence_for": [],
        }
        await self._show_pick(
            chat_id, api, sentence, taste, placeholder, result, lookup, wines, update,
            fallback=True, ask=ask, meta=meta, locale=locale,
        )

    async def _write(self, update, context, api, chat_id, parsed, wines, ask, locale: str = "en"):
        intent = parsed.get("intent")
        if intent == "price":
            amount = (parsed.get("price") or {}).get("amount")
            if amount is None:
                await self._say(update, self._c(locale, "chat.write.no_price"))
                return
            target = await self._price_target(api, parsed, wines, ask, locale)
            payload = {"price": amount}
            currency = (parsed.get("price") or {}).get("currency") or ""
        else:
            target = await self._correct_target(api, parsed, wines, locale)
            payload = _correction_payload(parsed.get("correction") or {})
            currency = ""
            if target is not None and target.get("card"):
                await self._correct_card(api, target, payload, update, locale)
                return
        if target is None:
            return
        if target.get("down"):
            await self._say(update, self._c(locale, "chat.cellar_down"), self._retry_button())
            self._save(chat_id, {**self._load(chat_id), "retry_text": parsed.get("wine_ref") or ""})
            return
        if target.get("missing"):
            await self._say(update, target.get("prompt") or self._c(locale, "chat.write.which_mean"))
            return
        if target.get("card") and intent == "price":
            await self._price_card(api, target, amount, currency, update, chat_id, locale)
            return
        if target.get("choose"):
            await self._choose_wine(update, chat_id, target["choose"], intent, payload, currency, parsed, locale)
            return
        await self._apply_write(update, api, chat_id, target["wine"], intent, payload, currency, parsed, locale)

    async def _price_card(self, api, target, amount, currency, update, chat_id, locale: str = "en"):
        barcode, data = target["card"]
        merged = dict(data.get("grok_merged") or {})
        merged["price"] = amount
        self.handler._update_pending(api, barcode, grok_merged=merged)
        _cb, glyph, *_rest = _tools()
        shown = f"{glyph(currency)}{float(amount):.2f}" if currency else f"{float(amount):.2f}"
        name = _esc(merged.get("name") or "that wine")
        await self._say(update, self._c(locale, "chat.write.set_price", name=name, shown=shown, note=""))

    async def _apply_write(self, update, api, chat_id, wine, intent, payload, currency, parsed,
                           locale: str = "en"):
        if not payload:
            await self._say(update, self._c(locale, "chat.write.no_change"))
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: api.update_wine(wine["id"], payload))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 500
            if status == 404:
                await self._say(update, self._c(locale, "chat.wine_gone"))
                return
            if status == 400:
                detail = _first_error(exc.response) or self._c(locale, "chat.write_rejected")
                await self._say(update, _esc(detail))
                return
            self._remember_write(chat_id, intent, payload, currency, wine["id"], parsed)
            await self._say(update, self._c(locale, "chat.write_failed"), self._retry_button())
            return
        except Exception as exc:
            logger.warning("[chat] kind=write chat=%s detail=%s", chat_id, exc)
            self._remember_write(chat_id, intent, payload, currency, wine["id"], parsed)
            await self._say(update, self._c(locale, "chat.write_failed"), self._retry_button())
            return
        name = _esc(wine.get("name") or "that wine")
        if intent == "price":
            _cb, glyph, *_rest = _tools()
            shown = f"{glyph(currency)}{float(payload['price']):.2f}" if currency else f"{float(payload['price']):.2f}"
            household = self._currency(chat_id)
            note = ""
            if currency and household and currency.upper() != household.upper():
                note = self._c(locale, "chat.write.currency_note", currency=household)
            await self._say(update, self._c(locale, "chat.write.set_price", name=name, shown=shown, note=note))
            return
        bits = [f"{key} {_esc(value)}" for key, value in payload.items()]
        await self._say(update, self._c(locale, "chat.write.updated", name=name, bits=", ".join(bits)))

    def _remember_write(self, chat_id, intent, payload, currency, wine_id, parsed):
        merged = self._load(chat_id)
        merged["write"] = {
            "intent": intent, "payload": payload, "currency": currency,
            "wine_id": wine_id, "parsed": parsed,
        }
        self._save(chat_id, merged)

    async def _choose_wine(self, update, chat_id, wines, intent, payload, currency, parsed,
                           locale: str = "en"):
        cb, *_ = _tools()
        buttons = [(w.get("name") or "wine", cb("cw", wine_id=w["id"])) for w in wines[:4]]
        merged = self._load(chat_id)
        merged["write"] = {
            "intent": intent, "payload": payload, "currency": currency, "parsed": parsed,
            "choices": {str(w["id"]): w for w in wines[:4]},
        }
        self._save(chat_id, merged)
        await self._say(update, self._c(locale, "chat.write.which"), _markup(buttons))

    async def _price_target(self, api, parsed, wines, ask, locale: str = "en"):
        ref = (parsed.get("wine_ref") or "").strip().lower()
        if ref and ref != "this":
            return await self._named(api, parsed, wines, locale)
        if ask.get("shown"):
            wine = _by_id(wines, ask.get("shown"))
            if wine:
                return {"wine": wine}
        card = self._first_card(api)
        if card and not card[1].get("wine_id"):
            return {"card": card}
        return await self._named(api, parsed, wines, locale)

    async def _correct_target(self, api, parsed, wines, locale: str = "en"):
        card = self._first_card(api)
        if card and not card[1].get("wine_id"):
            return {"card": card}
        return await self._named(api, parsed, wines, locale)

    async def _named(self, api, parsed, wines, locale: str = "en"):
        ref = (parsed.get("wine_ref") or "").strip()
        if not ref or ref.lower() == "this":
            # The caller sends the prompt. Signal with a wine-less result.
            return {"missing": True, "prompt": self._c(locale, "chat.write.which_mean")}
        loop = asyncio.get_running_loop()
        try:
            found = await loop.run_in_executor(None, lambda: api.search_wines(ref))
        except Exception as exc:
            logger.warning("[chat] kind=cellar_down model=cellar detail=%s", exc)
            return {"down": True}
        found = found or []
        if len(found) >= 2:
            return {"choose": found}
        if len(found) == 1:
            return {"wine": found[0]}
        return {"missing": True, "prompt": self._c(locale, "chat.write.not_found", name=_esc(ref))}

    async def _correct_card(self, api, target, payload, update, locale: str = "en"):
        barcode, data = target["card"]
        merged = dict(data.get("grok_merged") or {})
        # grape_variety is a saved-wine field. On a card the key the add flow
        # reads is grape.
        if "grape_variety" in payload:
            merged["grape"] = payload["grape_variety"]
        for key, value in payload.items():
            if key == "grape_variety":
                continue
            merged[key] = value
        self.handler._update_pending(api, barcode, grok_merged=merged)
        bits = [f"{key} {_esc(value)}" for key, value in payload.items()]
        name = _esc(merged.get("name") or "that wine")
        await self._say(
            update,
            self._c(locale, "chat.write.updated_card", name=name, bits=", ".join(bits)),
        )

    def _first_card(self, api):
        cards = self._open_cards(api)
        return cards[0] if cards else None

    def _open_cards(self, api):
        try:
            return self.handler._open_label_cards(api)
        except Exception:
            return []

    async def on_callback(self, query, data, context) -> None:
        try:
            await self._on_callback(query, data, context)
        except StoreError:
            logger.warning("[chat] kind=store")
            try:
                chat_id = str(getattr(query.message, "chat_id", "") or "")
                locale = self._reply_locale(
                    SimpleNamespace(message=query.message, effective_chat=SimpleNamespace(id=chat_id)),
                    chat_id,
                )
                await query.message.reply_text(self._c(locale, "chat.ask_lost"))
            except Exception:
                logger.exception("[chat] kind=unexpected")

    async def _on_callback(self, query, data, context) -> None:
        action = data.get("action")
        message = query.message
        chat_id = str(getattr(message, "chat_id", None) or message.chat.id)
        ask = self._load(chat_id)
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id), edited_message=None)
        api = self._api_for(chat_id)
        locale = self._reply_locale(update, chat_id)
        if action == "cf":
            choices = ask.get("choices") or []
            page = data.get("page") or 0
            fact = dict(ask.get("fact") or {})
            if page >= len(choices) or not fact:
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            fact["storage_text"] = choices[page]
            wines = await self._wines_or_down(api, update, chat_id)
            if wines is None:
                return
            await self._fact(update, context, api, chat_id, {"fact": fact}, wines, [], locale)
            return
        if action == "cc":
            grape = ask.get("close") or ""
            if not grape:
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            wines = await self._wines_or_down(api, update, chat_id)
            if wines is None:
                return
            placeholder = await message.reply_text(_looking(wines, locale), parse_mode="HTML")
            await self._recommend(
                chat_id, api, f"something close to {grape}",
                {"colour": "any", "words": [grape]}, placeholder, False, update, context, wines,
                locale=locale,
            )
            return
        if action == "cq":
            options = ask.get("options") or []
            page = data.get("page") or 0
            if page >= len(options):
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            wines = await self._wines_or_down(api, update, chat_id)
            if wines is None:
                return
            placeholder = await message.reply_text(_looking(wines, locale), parse_mode="HTML")
            await self._recommend(
                chat_id, api, ask.get("sentence") or "", ask.get("taste") or {},
                placeholder, False, update, context, wines, answer=options[page], ask=ask,
                locale=locale,
            )
            return
        if action == "cn":
            if ask.get("stage") != "shown":
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            await self._next(chat_id, ask, message, update, api)
            return
        if action == "ck":
            wine_id = data.get("wine_id") or ask.get("shown")
            await self._keep(chat_id, ask, message, update, api, wine_id)
            return
        if action == "co":
            await self._checkout(message, update, api, data)
            return
        if action == "cx":
            if ask.get("intent_id"):
                await self._call(api.update_intent, ask["intent_id"], {"outcome": "abandoned"})
            self._clear(chat_id)
            await self._edit(message, self._c(locale, "chat.no_problem"), update)
            return
        if action == "cr":
            await self._retry(chat_id, ask, update, context, api)
            return
        if action == "cw":
            write = ask.get("write") or {}
            choices = write.get("choices") or {}
            wine = choices.get(str(data.get("wine_id")))
            if not wine:
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            await self._apply_write(
                update, api, chat_id, wine, write.get("intent"), write.get("payload") or {},
                write.get("currency") or "", write.get("parsed") or {}, locale=locale,
            )
            return
        if action == "cg":
            club_id = data.get("wine_id")
            if not club_id:
                await self._edit(message, self._c(locale, "chat.finished"), update)
                return
            await self._club_from_pick(
                update, context, api, chat_id, club_id=club_id, club_name="",
            )
            return
        if action == "cm":
            member_id = data.get("wine_id")
            club_id = data.get("page")
            extra = {"member_id": member_id, "member_name": ""}
            if club_id:
                extra["club_id"] = club_id
            await self._club_from_pick(update, context, api, chat_id, **extra)
            return
        if action == "ca":
            which = data.get("original") or "members"
            club_id = data.get("wine_id")
            if which == "clubs":
                await self._club_from_pick(
                    update, context, api, chat_id, kind="my_clubs",
                    club_id=None, club_name="", member_id=None, member_name="",
                )
                return
            extra = {
                "kind": "list" if which == "list" else "members",
                "member_id": None, "member_name": "",
            }
            if club_id:
                extra["club_id"] = club_id
            await self._club_from_pick(update, context, api, chat_id, **extra)
            return
        if action == "ct":
            await self.handler._cmd_stock(update, context)
            return
        if action == "cp":
            wines = await self._wines_or_down(api, update, chat_id)
            if wines is None:
                return
            if not wines:
                await self._empty(update, api)
                return
            placeholder = await message.reply_text(_looking(wines, locale), parse_mode="HTML")
            await self._recommend(
                chat_id, api, "what should I drink",
                {"colour": "any", "words": []}, placeholder, True, update, context, wines,
                locale=locale,
            )
            return
        if action == "cl":
            await self._say(update, f"🍷 {self._c(locale, 'chat.help.commands')}")
            return

    async def _next(self, chat_id, ask, message, update, api):
        queue = list(ask.get("queue") or [])
        if not queue:
            if ask.get("intent_id"):
                await self._call(api.update_intent, ask["intent_id"], {"outcome": "rejected"})
            self._clear(chat_id)
            locale = self._reply_locale(update, chat_id)
            await self._edit(message, self._c(locale, "chat.pick.exhausted"), update)
            return
        nxt = queue.pop(0)
        rejected = list(ask.get("rejected") or [])
        if ask.get("shown"):
            rejected.append(ask["shown"])
        wines = await self._wines_or_down(api, update, chat_id)
        if wines is None:
            return
        wine = _by_id(wines, nxt.get("id"))
        if wine is None:
            await self._edit(message, self._c(self._reply_locale(update, chat_id), "chat.finished"), update)
            return
        ask.update({"queue": queue, "shown": wine.get("id"), "rejected": rejected, "stage": "shown"})
        self._save(chat_id, ask)
        if ask.get("intent_id"):
            await self._call(api.update_intent, ask["intent_id"], {
                "shown": wine.get("id"), "rejected": rejected,
            })
        cb, *_ = _tools()
        locale = self._reply_locale(update, chat_id)
        text = _render_pick(wine, nxt.get("reason") or "", None, "", [], locale=locale)
        await self._edit(message, text, update, _markup(_pick_buttons(cb, wine, None)))

    async def _keep(self, chat_id, ask, message, update, api, wine_id):
        loop = asyncio.get_running_loop()
        wines, storages = [], []
        try:
            wines, storages = await asyncio.gather(
                loop.run_in_executor(None, api.wines_in_stock),
                loop.run_in_executor(None, api.list_storages),
            )
        except Exception:
            logger.warning("[chat] kind=cellar_down model=cellar chat=%s", chat_id)
        wine = _by_id(wines, wine_id) or {"id": wine_id, "name": "that bottle", "items": []}
        if ask.get("intent_id"):
            await self._call(api.update_intent, ask["intent_id"], {
                "outcome": "accepted", "shown": wine_id,
            })
        self._clear(chat_id)
        cb, *_ = _tools()
        locale = self._reply_locale(update, chat_id)
        await self._edit(message, _enjoy(wine, locale), update, _markup(_checkout_buttons(cb, wine, storages)))

    async def _checkout(self, message, update, api, data):
        """Take the chosen bottle out of stock so it needn't be scanned out."""
        wine_id = data.get("wine_id")
        storage_id = data.get("page") or 0
        loop = asyncio.get_running_loop()
        try:
            storage_name = None
            if storage_id:
                storages = await loop.run_in_executor(None, api.list_storages)
                storage_name = next(
                    (s.get("name") for s in storages or [] if s.get("id") == storage_id), None,
                )
            result = await loop.run_in_executor(
                None, lambda: api.remove_stock(wine_id, storage_name=storage_name),
            )
        except Exception as exc:
            logger.warning("[chat] kind=checkout wine=%s detail=%s", wine_id, exc)
            chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
            locale = self._reply_locale(update, chat_id)
            await self._say(update, self._c(locale, "chat.checkout.fail"))
            return
        if not isinstance(result, dict) or result.get("error"):
            if isinstance(result, dict) and result.get("error"):
                logger.info("[chat] kind=checkout wine=%s error=%s", wine_id, result.get("error"))
            chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
            locale = self._reply_locale(update, chat_id)
            await self._edit(message, self._c(locale, "chat.checkout.gone"), update)
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from integrations.telegram_bot import VOTE_PROMPT, vote_keyboard_row
        cb, *_ = _tools()
        chat_id = str(getattr(getattr(update, "effective_chat", None), "id", "") or "")
        locale = self._reply_locale(update, chat_id)
        item_id = result.get("item_id")
        undo = cb("ul", wine_id=wine_id, item_id=item_id) if item_id else cb("ur", wine_id=wine_id)
        name = _esc(result.get("wine_name") or "that bottle")
        lines = [self._c(locale, "chat.checkout.taken", name=name)]
        if result.get("storage") or storage_name:
            lines.append(self._c(locale, "chat.checkout.from", storage=_esc(result.get("storage") or storage_name)))
        lines.append(self._c(locale, "chat.checkout.stock", n=result.get("stock_count", 0)))
        keyboard = [[InlineKeyboardButton("↩️ Undo", callback_data=undo)]]
        if item_id:
            lines += ["", VOTE_PROMPT]
            keyboard.append(vote_keyboard_row(item_id))
        await self._edit(message, "\n".join(lines), update, InlineKeyboardMarkup(keyboard))

    async def _retry(self, chat_id, ask, update, context, api):
        write = ask.get("write")
        if write and write.get("wine_id"):
            wine = {"id": write["wine_id"], "name": "that wine"}
            locale = self._reply_locale(update, chat_id)
            await self._apply_write(
                update, api, chat_id, wine, write.get("intent"), write.get("payload") or {},
                write.get("currency") or "", write.get("parsed") or {}, locale=locale,
            )
            return
        text = ask.get("retry_text")
        if text and not ask.get("sentence"):
            await self._handle_text(update, context, text, {})
            return
        locale = self._reply_locale(update, chat_id)
        if not ask.get("sentence"):
            await self._edit(update.message, self._c(locale, "chat.finished"), update)
            return
        wines = await self._wines_or_down(api, update, chat_id)
        if wines is None:
            return
        placeholder = await update.message.reply_text(_looking(wines, locale), parse_mode="HTML")
        await self._recommend(
            chat_id, api, ask.get("sentence"), ask.get("taste") or {}, placeholder,
            bool(ask.get("allow_question", True)), update, context, wines,
            answer=ask.get("answer"), ask=ask, locale=locale,
        )

    async def on_location(self, update, context=None) -> bool:
        if getattr(update, "edited_message", None):
            return False
        message = getattr(update, "message", None)
        loc = getattr(message, "location", None) if message is not None else None
        if loc is None:
            return False
        chat_id = str(update.effective_chat.id)
        waiting = self._pending_place(chat_id)
        if waiting:
            # Used for this one search and not stored anywhere.
            self._drop_place(chat_id)
            await self._search_places(
                update, context, self._api_for(chat_id), chat_id, waiting["query"],
                lat=loc.latitude, lon=loc.longitude, clear_keyboard=True,
            )
            return True
        ask = self._load(chat_id)
        if not ask.get("intent_id") or ask.get("place"):
            return False
        place = {
            "lat": loc.latitude,
            "lon": loc.longitude,
            "accuracy_m": getattr(loc, "horizontal_accuracy", None),
            "fix": (
                "live_start" if getattr(loc, "live_period", None)
                else ("gps" if getattr(loc, "horizontal_accuracy", None) else "map_pin")
            ),
            "purpose": "weather",
            "place_kind": None,
            "at": update.message.date.isoformat() if getattr(update.message, "date", None) else "",
        }
        api = self._api_for(chat_id)
        await self._call(api.update_intent, ask["intent_id"], {"place": place})
        ask["place"] = place
        self._save(chat_id, ask)
        await message.reply_text(self._c(self._reply_locale(update, chat_id), "chat.place.thanks"))
        return True

    async def _wines_or_down(self, api, update, chat_id):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, api.wines_in_stock)
        except Exception as exc:
            logger.warning("[chat] kind=cellar_down model=cellar chat=%s detail=%s", chat_id, exc)
            await self._say(update, self._c(self._reply_locale(update, chat_id), "chat.cellar_down"), self._retry_button())
            return None

    async def _record(self, api, payload) -> int | None:
        result = await self._call(api.record_intent, "ask", payload=payload)
        if isinstance(result, dict):
            return result.get("intent_id")
        return None

    async def _call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as exc:
            logger.warning("[chat] kind=record detail=%s", exc)
            return {}

    async def _alert_staff(self, context, failure: ChatFailure, model: str):
        if failure.kind != "auth":
            return
        now = self._now()
        last = self._staff_alert_at.get("auth")
        if last is not None and now - last < 3600:
            return
        self._staff_alert_at["auth"] = now
        text = f"⚠️ Whiney chat: xAI returned {failure.detail} for {model}. Check the API key / credit."
        bot = getattr(context, "bot", None)
        if bot is None:
            return
        for admin in getattr(self.handler, "admin_chat_ids", []) or []:
            try:
                await bot.send_message(int(admin), text)
            except Exception:
                logger.warning("[chat] kind=staff chat=%s", admin)

    def _retry_button(self):
        cb, *_ = _tools()
        return _markup([("Try again", cb("cr"))])

    def _currency(self, chat_id) -> str:
        return (getattr(self.handler, "_chat_id_to_currency", {}) or {}).get(str(chat_id), "")

    async def _empty(self, update, api):
        _cb, _glyph, _nm, unconfigured_storage, empty = _tools()
        try:
            unconfigured, storage_name = unconfigured_storage(api)
        except Exception:
            unconfigured, storage_name = False, ""
        text, keyboard = empty(unconfigured=unconfigured, storage_name=storage_name)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

    async def _placeholder(self, update, wines, locale: str = "en"):
        await self._wait_pace()
        return await update.message.reply_text(_looking(wines, locale), parse_mode="HTML")

    async def _say(self, update, text, markup=None):
        await self._wait_pace()
        kwargs = {"parse_mode": "HTML"}
        if markup is not None:
            kwargs["reply_markup"] = markup
        await update.message.reply_text(text, **kwargs)

    async def _edit(self, message, text, update, markup=None):
        _cb, _glyph, not_modified, _s, _e = _tools()
        kwargs = {"parse_mode": "HTML"}
        if markup is not None:
            kwargs["reply_markup"] = markup
        try:
            await message.edit_text(text, **kwargs)
        except BadRequest as exc:
            if not_modified(exc):
                return
            reply = getattr(message, "reply_text", None) or (update.message.reply_text if update else None)
            if reply is not None:
                await reply(text, **kwargs)
        except Exception:
            reply = getattr(update, "message", None)
            if reply is not None:
                await reply.reply_text(text, **kwargs)

    async def _typing(self, context, chat_id):
        bot = getattr(context, "bot", None)
        if bot is None:
            return
        await bot.send_chat_action(chat_id=int(chat_id), action="typing")


def _whole_word_in_names(text: str, wines: list[dict]) -> bool:
    if " " in text.strip():
        return False
    word = text.strip().casefold()
    if not word:
        return False
    for wine in wines:
        words = (wine.get("name") or "").casefold().split()
        if word in words:
            return True
    return False


def _short_phrase(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith("?"):
        return False
    return len(stripped.split()) <= 2


def _slot_fact(parsed: dict, wines: list[dict], locale: str = "en"):
    """Match a named wine/producer/appellation against stock when fact.kind
    did not already answer. Empty is a fact, not a guess. Try every slot
    before saying nothing matched.
    """
    from integrations.cellar_facts import FactReply
    from integrations.telegram_bot import match_in_stock_wines

    slots = parsed.get("slots") or {}
    stock = [w for w in wines if (w.get("stock_count") or 0) > 0]
    last_empty = None
    for key in ("appellation", "wine", "producer"):
        query = str(slots.get(key) or "").strip()
        if not query:
            continue
        hits = match_in_stock_wines(stock, query)
        if hits:
            lines = []
            ids = []
            for wine in hits:
                ids.append(wine.get("id"))
                name = _esc(wine.get("name") or "a bottle")
                lines.append(f"• <b>{name}</b>")
            return FactReply(
                text=f"{_esc(query)}\n" + "\n".join(lines),
                kind="name",
                wine_ids=[i for i in ids if i is not None],
            )
        last_empty = FactReply(
            text=copy_t(locale, "chat.fact.none_matching", query=_esc(query)),
            kind="empty",
        )
    grape = str(slots.get("grape") or "").strip()
    if grape:
        return answer_fact(
            {
                "kind": "grape", "grape": grape, "storage_text": "",
                "drawer": None, "country_code": "",
            },
            wines,
            locale=locale,
        )
    return last_empty


def _shown_name(ask: dict, wines: list[dict]) -> str | None:
    wine = _by_id(wines, ask.get("shown"))
    if wine:
        return wine.get("name")
    return None


def _by_id(wines, wine_id):
    for wine in wines or []:
        if wine.get("id") == wine_id:
            return wine
    return None


def _answer_text(parsed: dict, text: str, ask: dict) -> str:
    index = parsed.get("answer_index")
    options = ask.get("options") or []
    if isinstance(index, int) and 0 <= index < len(options):
        return options[index]
    return text


def _looking(wines: list[dict], locale: str = "en") -> str:
    return copy_t(locale, "chat.looking")


def _progress_lines(taste: dict, bottles: int, locale: str = "en") -> dict[str, str]:
    colour = (taste or {}).get("colour") or ""
    words = [w for w in (taste or {}).get("words") or [] if w]
    bits = []
    if colour and colour != "any":
        bits.append(colour)
    bits.extend(words)
    phrase = ", ".join(bits)[:40]
    if phrase:
        matching = copy_t(locale, "chat.progress.matching_phrase", phrase=_esc(phrase))
    else:
        matching = copy_t(locale, "chat.progress.matching")
    return {
        "matching": matching,
        "narrowing": copy_t(locale, "chat.progress.narrowing"),
        "slow": copy_t(locale, "chat.progress.slow"),
    }


def _render_pick(wine, reason, runner, runner_reason, no_evidence, locale: str = "en") -> str:
    evidence = ""
    if no_evidence:
        words = ", ".join(_esc(w) for w in no_evidence)
        evidence = copy_t(locale, "chat.pick.none_notes", words=words)
    if runner is not None:
        return evidence + copy_t(
            locale, "chat.pick.tie",
            first=_bullet(wine, reason),
            second=_bullet(runner, runner_reason),
        )
    return copy_t(
        locale, "chat.pick.ask",
        evidence=evidence, bottle=_bottle(wine), reason=_esc(reason),
    )


def _bottle(wine) -> str:
    name = _esc(wine.get("name") or "a bottle")
    items = wine.get("items") or []
    item = items[0] if items else {}
    storage = _esc(item.get("storage") or "")
    drawer = item.get("drawer")
    if storage and drawer is not None:
        place = f"{storage}, D{drawer}"
    else:
        place = storage or (f"D{drawer}" if drawer is not None else "")
    loc = f" — {place}" if place else ""
    return f"🍷 <b>{name}</b>{loc}"


def _bullet(wine, reason) -> str:
    name = _esc(wine.get("name") or "a bottle")
    items = wine.get("items") or []
    item = items[0] if items else {}
    storage = _esc(item.get("storage") or "")
    drawer = item.get("drawer")
    place = storage
    if drawer is not None and storage:
        place = f"{storage}, D{drawer}"
    return f"• <b>{name}</b> — {place}: {_esc(reason)}"


def _enjoy(wine, locale: str = "en") -> str:
    name = _esc(wine.get("name") or "that bottle")
    items = wine.get("items") or []
    item = items[0] if items else {}
    storage = _esc(item.get("storage") or "")
    drawer = item.get("drawer")
    place = storage
    if drawer is not None and place:
        place = f"{place}, D{drawer}"
    loc = f" — {place}" if place else ""
    return copy_t(locale, "chat.enjoy", name=name, loc=loc)


def _looks_like_area(text: str) -> bool:
    """A typed suburb in reply to the location prompt, not a new question or command."""
    words = text.strip().split()
    return 0 < len(words) <= 4 and "?" not in text and not text.startswith("/")


def _render_places(places: list[dict], area: str, locale: str = "en") -> str:
    if area:
        header = copy_t(locale, "chat.place.header_area", area=_esc(area))
    else:
        header = copy_t(locale, "chat.place.header")
    lines = [header, ""]
    for index, place in enumerate(places, 1):
        name = _esc(place["name"])
        if place.get("url"):
            name = f'<a href="{html.escape(place["url"], quote=True)}">{name}</a>'
        head = f"{index}. <b>{name}</b>"
        if place.get("area"):
            head += f" — {_esc(place['area'])}"
        lines.append(head)
        if place.get("why"):
            lines.append(f"   {_esc(place['why'])}")
    lines += ["", copy_t(locale, "chat.place.footnote")]
    return "\n".join(lines)


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _checkout_buttons(cb, wine, storages) -> list[tuple[str, str]]:
    """One button per fridge holding the bottle; none if we can't tell where it is."""
    ids = {s.get("name"): s.get("id") for s in storages or [] if isinstance(s, dict)}
    places = []
    for item in wine.get("items") or []:
        name = item.get("storage") or ""
        if name and name not in places:
            places.append(name)
    if not places:
        return []
    if len(places) == 1:
        return [("📤 Take it out now", cb("co", wine_id=wine.get("id"), page=ids.get(places[0]) or 0))]
    return [
        (f"📤 From {name}", cb("co", wine_id=wine.get("id"), page=ids.get(name) or 0))
        for name in places[:3]
    ]


def _pick_buttons(cb, wine, runner, fallback=False):
    if runner is not None:
        buttons = [
            ((wine.get("name") or "First")[:30], cb("ck", wine_id=wine.get("id"))),
            ((runner.get("name") or "Second")[:30], cb("ck", wine_id=runner.get("id"))),
        ]
    else:
        buttons = [("That's the one", cb("ck", wine_id=wine.get("id")))]
    buttons.append(("Something else", cb("cn")))
    if fallback:
        buttons.append(("Try properly", cb("cr")))
    else:
        buttons.append(("Not now", cb("cx")))
    return buttons


def _ask_payload(sentence, taste, question, options, wines, result, shown=None, answer=None):
    return {
        "sentence": sentence,
        "taste": taste or {},
        "question": question,
        "options": options,
        "answer": answer,
        "pick": result.get("pick"),
        "runner_up": result.get("runner_up"),
        "alternatives": result.get("alternatives") or [],
        "shown": shown,
        "rejected": [],
        "outcome": "pending",
        "offer": None,
        "place": None,
    }


def _price_band(wines, ids, currency) -> dict:
    prices = []
    by_id = {w.get("id"): w for w in wines}
    for wine_id in ids:
        wine = by_id.get(wine_id) or {}
        item_prices = [
            item.get("price") for item in wine.get("items") or []
            if item.get("price") is not None
        ]
        if item_prices:
            prices.extend(item_prices)
        elif wine.get("price") is not None:
            prices.append(wine["price"])
    if not prices:
        return {"currency": currency, "median": None, "n": 0}
    return {"currency": currency, "median": statistics.median(prices), "n": len(prices)}


def _correction_payload(correction: dict) -> dict:
    payload = {}
    for src, dest in (
        ("name", "name"),
        ("producer", "producer"),
        ("vintage", "vintage"),
        ("grape", "grape_variety"),
        ("country_code", "country"),
    ):
        value = correction.get(src)
        if value not in (None, ""):
            payload[dest] = value
    return payload


def _first_error(response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str) and error:
            return error
        for value in data.values():
            if isinstance(value, list) and value:
                return str(value[0])
            if isinstance(value, str) and value:
                return value
    return ""

