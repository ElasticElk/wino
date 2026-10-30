"""The chat calls. None of them is the label vision call.

parse() reads one sentence into a fixed shape, fast, with reasoning off.
recommend() chooses from a cellar it was given. wine_chat() answers a
general wine question. find_places() is the one live web search. All raise
ChatFailure instead of returning a guess, and a breaker stops us waiting on
an API that has already failed three times.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

import requests

from integrations.chat_intent import (
    INTENT_ALIASES,
    LIST_OPS,
    SCHEMA_INTENTS,
    SLOT_KEYS,
    UNKNOWN_KINDS,
    blank_slots,
    canonical_intent,
)

logger = logging.getLogger(__name__)

PARSE_MODEL = "grok-4.20-0309-non-reasoning"
# Every xAI reasoning model took 16-32s on a 31-wine cellar (2026-10-21), and
# the non-reasoning model matched its picks on the eval in about 1.4s.
RECOMMEND_MODEL = "grok-4.20-0309-non-reasoning"
# 4s timed out live on 2026-10-21, when parses ran 2-4s.
PARSE_TIMEOUT = 8
RECOMMEND_TIMEOUT = 12
BASE_URL = "https://api.x.ai/v1"
BREAKER_LIMIT = 3
BREAKER_COOLDOWN = 120
CELLAR_CAP = 300

COLOUR_TO_TYPE = {
    "red": "RE", "white": "WH", "rose": "RO", "sparkling": "SP",
    "dessert": "DE", "fortified": "FO", "orange": "OR",
}
TYPE_TO_WORD = {code: word for word, code in COLOUR_TO_TYPE.items()}

INTENTS = set(SCHEMA_INTENTS)
CLUB_KINDS = {
    "none", "members", "my_clubs", "cellar", "who_has", "oldest", "feed",
    "top_rated", "who_drinks", "avg_rating", "tasteoff", "badges",
    "badge_progress", "list", "comments",
}
HELP_TOPICS = {
    "invite", "new_cellar", "switch", "photo", "take_out", "commands", "other", "wine",
}
WINE_CHAT_TIMEOUT = 10
# Live web search. grok-4.3 found cited places in about 5s where the
# non-reasoning model took 9s (2026-10-21). web_search only works on /responses;
# /chat/completions rejects it with a 422.
PLACE_MODEL = "grok-4.3"
PLACE_TIMEOUT = 45  # a live search took 30s
FACT_KINDS = {"drawer", "storage", "country", "grape", "list_all", "none"}
COLOURS = set(COLOUR_TO_TYPE) | {"any"}
EVIDENCE = {"note", "attribute", "grape", "none"}

PARSE_PROMPT = """You classify one message sent to Whiney, a wine-cellar Telegram bot. Return JSON only. Do not answer the user. Do not invent bottles, people, clubs, stock, scores, or anything about their data — another step will look that up.
The message may be in any language (English and Afrikaans first, then any other). Classify by meaning, never by which language it is in.
intent (pick one):
- cellar_query: a question about bottles they already own ("what's in D3 of the small fridge", "list my wines", "anything from South Africa", "any shiraz?", "Watter wyne het ek van Frankryk?"). Only their own cellar. Set fact.kind to drawer, storage, country, grape, list_all, or none.
- club_query: a question about a wine club they belong to — other members' cellars, who is in a club, what clubs they are in, what the club opened, votes, badges, taste-offs, club comments, or a shared club list. Not bottles in their own fridge (that is cellar_query) and not a pick from their own cellar (that is recommend). Always classify club questions as club_query even if you are unsure the feature exists; never turn them into cellar_query, wine_knowledge, or smalltalk_help.
- drink_remove: they already drank, finished, or want a named bottle taken out of stock now ("I drank one of my barolos from the big fridge", "remove a bottle of Chenin", "ek het een van my barolos uit die yskas gedrink"). Not a question about what to open (recommend). Not how to take a bottle out (smalltalk_help). wine_ref is the wine words ("this" if they pointed at a card); fact.storage_text / slots.location is the fridge they named.
- add_wine: they want to add a bottle to the cellar ("add this", "put it in the fridge", "voeg by die kelder"). Not a to-drink list (list_action).
- search: a wine name, producer or appellation on its own, looking it up in their cellar.
- recommend: they want a bottle chosen from their own cellar to open ("what should I drink", "something leathery", "deep red with fennel").
- list_action: their personal To-drink list ("what's on my to-drink list", "add this to my list", "wat is op my lys?"). list_op is show, add, or remove. Another person's list is club_query (kind list).
- smalltalk_help: how to use Whiney itself ("how do I add someone", "can I start a new cellar", "how do I invite", "how do I switch", "how do I add a wine", "how do I take a bottle out", "what can you do"). Set help_topic to invite, new_cellar, switch, photo, take_out, commands, or other.
- wine_knowledge: a wine, drink or food-pairing question that is not about their cellar, their clubs, or their lists: what to order at a restaurant they are already at, a grape, region, vintage or producer in general, serving, storing, decanting ("I'm at a steakhouse, what should I order?", "is 2019 a good Barolo year?", "Watter wyn pas by bobotie?"). Never use this for questions about what they own or who they drink with.
- place: they want a restaurant, wine bar, wine farm or shop found for wine, near them or in a named area. Put a named town/suburb in area and slots.location; leave area empty for "near me".
- price: they give what they paid ("£18 on the Chenin", "it was 12.50").
- correct: they say a wine's details are wrong ("wrong wine, it's Aux Moines", "vintage is 2019").
- answer: only when context.open_question is set and the message answers it. Set answer_index if it matches one of context.options (0-based), else null.
- unknown: anything else, including off-topic ("how high do planes fly"). Set unknown_kind to offtopic or unclear. Never guess.

club.kind when intent is club_query: members, my_clubs, cellar, who_has, oldest, feed, top_rated, who_drinks, avg_rating, badges, badge_progress, tasteoff, list, comments.
member_name / slots.person is the person they named. club_name / slots.club is the club they named.

slots: copy names they used (wine, producer, appellation, vintage, colour, grape, person, club, location, quantity). Empty string / null if they did not say it. location is a fridge/rack for cellar_query and drink_remove, or a town for place.
confidence: 0 to 1. Use below 0.55 when you are guessing or the sentence is ambiguous.

Rules:
- country_code is ISO 3166 alpha-2 ("South Africa", "the Cape" -> "ZA").
- drawer is the number in "D3" or "drawer 3".
- storage_text is the words they used for the fridge or rack, verbatim.
- wine_ref is "this" when they point at the wine being discussed, otherwise the wine name words they used.
- Copy taste words as written.
- Never guess a wine, person, or club they did not name."""

PHRASE_PROMPT = """You are Whiney. Turn the facts below into a short Telegram reply in the same language as the user message.
Use only these facts. If a needed fact is missing, say you do not know. Never invent bottles, people, clubs, scores, stock, or anything about the user's data.
Plain text, no markdown, at most 90 words."""

RECOMMEND_PROMPT = """You are Whiney, a sommelier choosing from this household's own cellar. Choose only from the wines listed, by their #id.
- Read every line. Weigh, in this order: what they asked for (colour, grape, notes, occasion, food); the tasting note and attributes as evidence; drink-by (sooner is better when it fits); their own rating and thumbs up/down (avoid thumbs-down bottles unless asked); critic and Vivino scores only to break near-ties; and not repeating a wine from the recently-drunk list.
- reason: one sentence of at most 25 words. It quotes the note or attribute words that matched when there are any. Never claim a note the wine's line does not contain. evidence says where the match came from, or "none".
- If they named a note no wine has (e.g. leather), still pick the closest, set evidence "none", and put the word in no_evidence_for.
- runner_up: only when a second bottle is genuinely as good. Otherwise id 0.
- alternatives: up to 4 more good fits, best first.
- question: only if the message gives nothing that would change your choice AND the cellar has clearly different directions (e.g. reds and whites). One short question, with 2-4 options drawn from this cellar. Otherwise leave it empty and pick."""


PLACE_PROMPT = """You are Whiney, a friendly sommelier. Search the web and suggest up to 3 real restaurants, wine bars, wine farms or shops that fit the request, {where}.
- Prefer places known for their wine list or wine range, and say in one short line why each suits this person's taste.
- Only name places you found in the search and that are still open. Never invent one.
- Write the "why" lines in the same language as the request.
End your answer with JSON only, nothing after it:
{{"places": [{{"name": "...", "area": "suburb or town", "why": "one line", "url": "a page about the place or empty"}}]}}
Return {{"places": []}} if you found nothing suitable."""

SUPPORT_PROMPT = """You are Whiney, answering a how-to about this Telegram bot. Use only the facts below. If the facts do not cover it, say you are not sure and tell them to send /help, or take a screenshot and send it to whoever set Whiney up.
Reply in the same language they asked in, at most 90 words, plain text, no markdown.
Facts:
- Whiney is a Telegram bot for a wine cellar. There is no WhatsApp, email or web app for this.
- Add a wine: send a photo of the label or barcode. No command. They tap to add it. They can type a correction if the read is wrong.
- Add a person to this cellar: /invite, share the link. They must message the bot once.
- Start a new cellar: staff send /newcellar. Anyone else asks whoever set Whiney up, or uses /invite for this cellar.
- Switch cellar: /switch. Who is in it: /household.
- Take a bottle out: scan it, or ask what to open then That's the one then Take it out now. /fridge also changes stock. After a shuffle, /fridge then Catch up walks a drawer: tap Drunk or Moved, then Apply. Empty here means they moved, not that they were drunk.
- Ask in plain language: what to open, what's in a fridge, wine questions, restaurants with good wine.
- Price or a wrong vintage: type it ("I paid R250", "it's the 2020").
- Wine list at a restaurant: send a photo of the list.
- /help lists commands. /search finds a name. /stock /recent /status /storages are browsing.
- Badges are cellar challenges; owners can turn them off with /badges.
- If something is stuck or odd: screenshot and send it to whoever set Whiney up.
{club_facts}{staff_line}"""


WINE_CHAT_PROMPT = """You are Whiney, a friendly sommelier in a Telegram chat. Answer a general wine, drink or food-pairing question in at most 90 words of plain text, no markdown or headings.
- Reply in the same language they wrote in.
- Be specific: name styles, grapes, regions or producers someone could actually find on a list or shelf.
- Use their taste profile when it helps ("you rate Syrah highly, so…").
- Never mention, invent, or guess bottles they own, who is in a club, what a club opened, lists, badges, or taste-offs. You have no access to their cellar. If the question is about their own stock or people they drink with, say you do not know and that they can ask about their cellar instead.
- If they're out (restaurant, bar, shop) and a photo of the list would help, end with one short line saying they can send a photo of the wine list.
- If the question isn't about wine, drink or food, say in one sentence that you only do wine."""


class ChatFailure(Exception):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(detail or kind)


def _string():
    return {"type": "string"}


def _string_array():
    return {"type": "array", "items": {"type": "string"}}


def _nullable_int():
    return {"type": ["integer", "null"]}


def _nullable_number():
    return {"type": ["number", "null"]}


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def _enum(values: set[str]) -> dict:
    return {"type": "string", "enum": sorted(values)}


def parse_prompt(features: dict | None = None) -> str:
    """Classifier prompt. Feature flags are applied in code, not here, so a
    clubs-off question still classifies as club_query instead of inventing
    a cellar answer.
    """
    return PARSE_PROMPT


def _blank_club() -> dict:
    return {
        "kind": "none",
        "member_name": "",
        "club_name": "",
        "wine_text": "",
        "grape": "",
        "vintage": None,
        "period": "",
        "when": "",
    }


def club_support_facts(features: dict | None) -> str:
    features = features or {}
    if not features.get("clubs"):
        return ""
    lines = [
        "- Wine clubs: /club. They can also ask in plain language who is in a club, "
        "what the club opened, or what a member has if the club shares cellars.",
    ]
    if features.get("lists"):
        lines.append(
            "- To-drink lists: /list is personal. A club shared list is /club list. "
            "Another person's personal list stays private."
        )
    if features.get("tasteoffs"):
        lines.append("- Taste-offs: /club tasteoff. They can ask who won or when the next one is.")
    if features.get("comments"):
        lines.append("- Club comments: they can ask what people said about a bottle the club opened.")
    if features.get("global_clubs"):
        lines.append("- Clubs for everyone: /club discover. Those clubs never share cellars.")
    return "\n".join(lines) + "\n"


def parse_schema() -> dict:
    fact = _obj({
        "kind": _enum(FACT_KINDS),
        "storage_text": _string(),
        "drawer": _nullable_int(),
        "country_code": _string(),
        "grape": _string(),
    })
    taste = _obj({
        "colour": _enum(COLOURS),
        "words": _string_array(),
        "occasion": _string(),
        "food": _string(),
        "budget_max": _nullable_number(),
    })
    price = _obj({"amount": _nullable_number(), "currency": _string()})
    correction = _obj({
        "name": _string(),
        "producer": _string(),
        "vintage": _nullable_int(),
        "grape": _string(),
        "country_code": _string(),
    })
    club = _obj({
        "kind": _enum(CLUB_KINDS),
        "member_name": _string(),
        "club_name": _string(),
        "wine_text": _string(),
        "grape": _string(),
        "vintage": _nullable_int(),
        "period": _string(),
        "when": _string(),
    })
    slots = _obj({
        "wine": _string(),
        "producer": _string(),
        "appellation": _string(),
        "vintage": _nullable_int(),
        "colour": _string(),
        "grape": _string(),
        "person": _string(),
        "club": _string(),
        "location": _string(),
        "quantity": _nullable_int(),
    })
    return _obj({
        "intent": _enum(INTENTS),
        "confidence": {"type": "number"},
        "slots": slots,
        "fact": fact,
        "taste": taste,
        "price": price,
        "wine_ref": _string(),
        "correction": correction,
        "answer_index": _nullable_int(),
        "area": _string(),
        "help_topic": _enum(HELP_TOPICS),
        "club": club,
        "list_op": _enum(set(LIST_OPS)),
        "unknown_kind": _enum(set(UNKNOWN_KINDS)),
    })


def _pick_schema() -> dict:
    return _obj({
        "id": {"type": "integer"},
        "reason": _string(),
        "evidence": _enum(EVIDENCE),
    })


def recommend_schema(*, allow_question: bool) -> dict:
    properties = {
        "pick": _pick_schema(),
        "runner_up": _pick_schema(),
        "alternatives": {"type": "array", "items": _pick_schema()},
        "no_evidence_for": _string_array(),
    }
    if allow_question:
        properties = {
            "question": _string(),
            "options": _string_array(),
            **properties,
        }
    return _obj(properties)


def _blank_pick() -> dict:
    return {"id": 0, "reason": "", "evidence": "none"}


def check_recommendation(data: dict, valid_ids: set[int] | None) -> dict:
    """Drop bottles the cellar does not have. An unknown pick is a failure.

    A question is allowed to come back with pick id 0. That is the model
    saying it needs one more fact, not a bottle it invented.
    """
    if valid_ids is None:
        return data
    question = (data.get("question") or "").strip()
    alternatives = []
    for entry in data.get("alternatives") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in valid_ids and entry.get("id") != (data.get("pick") or {}).get("id"):
            alternatives.append(entry)
    data["alternatives"] = alternatives[:4]
    runner = data.get("runner_up") if isinstance(data.get("runner_up"), dict) else _blank_pick()
    pick_id = (data.get("pick") or {}).get("id")
    if runner.get("id") not in valid_ids or runner.get("id") in (0, pick_id):
        runner = _blank_pick()
    data["runner_up"] = runner
    if question:
        return data
    pick = data.get("pick") if isinstance(data.get("pick"), dict) else {}
    if pick.get("id") not in valid_ids:
        raise ChatFailure("bad_output", "unknown pick")
    data["pick"] = pick
    return data


def _fold(text: str) -> str:
    import unicodedata
    nfd = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn").casefold()


def _canonical(name: str) -> str:
    try:
        from integrations.cellar_facts import canonical_grape
    except ImportError:  # pragma: no cover - package layout in tests is integrations.*
        return _fold(name)
    return canonical_grape(name)


def _location(wine: dict) -> str:
    grouped: dict[tuple, int] = {}
    for item in wine.get("items") or []:
        key = (item.get("storage") or "", item.get("drawer"))
        grouped[key] = grouped.get(key, 0) + 1
    parts = []
    for (storage, drawer), count in grouped.items():
        bit = storage
        if drawer is not None:
            bit = f"{bit} D{drawer}".strip()
        if count > 1:
            bit = f"{bit} x{count}"
        if bit:
            parts.append(bit)
    return "; ".join(parts)


def _wine_line(wine: dict) -> str:
    grapes = [g for g in (wine.get("grapes") or []) if g]
    if len(grapes) > 1:
        grape_bit = "blend: " + ", ".join(grapes)
    elif grapes:
        grape_bit = grapes[0]
    else:
        grape_bit = ""
    country = wine.get("country") or ""
    region = wine.get("region") or ""
    place = f"{country}/{region}" if country and region else (country or region)
    votes = wine.get("votes") or {}
    parts = [
        f"#{wine.get('id')}",
        wine.get("name") or "",
        wine.get("producer") or "",
        TYPE_TO_WORD.get(wine.get("wine_type") or "", wine.get("wine_type") or ""),
        grape_bit,
        place,
        _location(wine),
        f"drink by {wine.get('drink_by') or '-'}",
        f"mine {wine.get('rating') if wine.get('rating') is not None else '-'}/10",
        "critic {c} vivino {v}".format(
            c=wine.get("critic_score") if wine.get("critic_score") is not None else "-",
            v=wine.get("vivino_rating") if wine.get("vivino_rating") is not None else "-",
        ),
        "votes +{up} ~{meh} -{down}".format(
            up=votes.get("up", 0), meh=votes.get("meh", 0), down=votes.get("down", 0),
        ),
    ]
    food = ", ".join(wine.get("food_pairings") or [])
    attrs = ", ".join(wine.get("attributes") or [])
    note = wine.get("comment") or ""
    if food:
        parts.append(f"pairs: {food}")
    if attrs:
        parts.append(f"attrs: {attrs}")
    if note:
        parts.append(f"note: {note}")
    return " | ".join(part for part in parts if part)


def _matches_cap(wine: dict, *, colour: str, grape: str, country: str, storage: str) -> bool:
    if colour and colour != "any":
        if wine.get("wine_type") != COLOUR_TO_TYPE.get(colour):
            return False
    if country and _fold(wine.get("country") or "") != _fold(country):
        return False
    if grape:
        wanted = _canonical(grape)
        names = wine.get("grapes") or []
        if not any(_canonical(name) == wanted for name in names):
            return False
    if storage:
        wanted_storage = _fold(storage)
        storages = [_fold(item.get("storage") or "") for item in wine.get("items") or []]
        if not any(wanted_storage in name for name in storages):
            return False
    return True


def _drink_by_key(wine: dict):
    value = wine.get("drink_by") or ""
    return (value == "", value)


def compact_cellar(wines: list[dict], *, colour: str = "", grape: str = "",
                   country: str = "", storage: str = "") -> tuple[str, dict[int, dict]]:
    """One stable line per wine, so the prefix can be prompt-cached.

    A cellar under the cap is sent whole. Taste words never drop a bottle;
    only an explicit colour, grape, country or storage does, and only then
    to keep a huge cellar inside the cap.
    """
    rows = [w for w in wines if w.get("id") is not None]
    rows.sort(key=lambda w: w["id"])
    if len(rows) > CELLAR_CAP:
        narrowed = [
            w for w in rows
            if _matches_cap(w, colour=colour, grape=grape, country=country, storage=storage)
        ]
        # An explicit filter that matches nothing would send an empty cellar
        # and force a made-up answer. Fall back to the full list and cap it.
        if narrowed:
            rows = narrowed
        if len(rows) > CELLAR_CAP:
            rows = sorted(rows, key=_drink_by_key)[:CELLAR_CAP]
            rows.sort(key=lambda w: w["id"])
    lookup = {w["id"]: w for w in rows}
    return "\n".join(_wine_line(w) for w in rows), lookup


class ChatModels:
    def __init__(self, api_key: str, post: Callable = requests.post, now: Callable = time.monotonic,
                 sleep: Callable = time.sleep):
        self.api_key = api_key
        self._post = post
        self._now = now
        self._sleep = sleep
        self._fail_streak = {PARSE_MODEL: 0, RECOMMEND_MODEL: 0}
        self._open_until = {PARSE_MODEL: 0.0, RECOMMEND_MODEL: 0.0}

    def healthy(self, model: str) -> bool:
        return self._now() >= self._open_until.get(model, 0.0)

    def _fail(self, model: str, kind: str, *, probing: bool) -> None:
        if kind == "bad_output":
            return
        if probing:
            self._open_until[model] = self._now() + BREAKER_COOLDOWN
            self._fail_streak[model] = 0
            return
        self._fail_streak[model] = self._fail_streak.get(model, 0) + 1
        if self._fail_streak[model] >= BREAKER_LIMIT:
            self._open_until[model] = self._now() + BREAKER_COOLDOWN
            self._fail_streak[model] = 0

    def _ok(self, model: str) -> None:
        self._fail_streak[model] = 0
        self._open_until[model] = 0.0

    def _call(self, model: str, messages: list[dict], schema_name: str, schema: dict,
              timeout: int, household_id: int | None = None) -> dict:
        until = self._open_until.get(model, 0.0)
        now = self._now()
        if now < until:
            raise ChatFailure("unavailable", "circuit open")
        probing = until != 0.0
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if household_id is not None:
            headers["x-grok-conv-id"] = f"hh-{household_id}"
        payload = {
            "model": model,
            "temperature": 0,
            "messages": messages,
            "max_tokens": 800 if model == RECOMMEND_MODEL else 400,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        retried = False
        while True:
            try:
                resp = self._post(
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
            except requests.Timeout as exc:
                self._fail(model, "timeout", probing=probing)
                logger.warning("[chat] %s failed kind=timeout detail=%s", schema_name, exc)
                raise ChatFailure("timeout", str(exc)) from exc
            except requests.ConnectionError as exc:
                self._fail(model, "unavailable", probing=probing)
                logger.warning("[chat] %s failed kind=unavailable detail=%s", schema_name, exc)
                raise ChatFailure("unavailable", str(exc)) from exc
            status = getattr(resp, "status_code", 200)
            if status == 429 and not retried:
                wait = _retry_after_seconds(resp)
                if wait is not None and wait <= 2:
                    retried = True
                    self._sleep(wait)
                    continue
            if status >= 400:
                kind = _http_kind(status)
                self._fail(model, kind, probing=probing)
                logger.warning("[chat] %s failed kind=%s detail=%s", schema_name, kind, status)
                raise ChatFailure(kind, str(status))
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                data = _load_json(content)
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._fail(model, "bad_output", probing=probing)
                logger.warning("[chat] %s failed kind=bad_output detail=%s", schema_name, exc)
                raise ChatFailure("bad_output", str(exc)) from exc
            self._ok(model)
            return data

    def parse(self, text: str, context: dict) -> dict:
        features = (context or {}).get("features") if isinstance(context, dict) else {}
        data = self._call(
            PARSE_MODEL,
            [
                {"role": "system", "content": parse_prompt(features or {})},
                {"role": "user", "content": json.dumps({"text": text, "context": context}, default=str)},
            ],
            "whiney_parse",
            parse_schema(),
            PARSE_TIMEOUT,
        )
        return _validate_parse(data)

    def recommend(self, sentence: str, taste: dict, cellar_block: str,
                  profile: str, history: list[dict], *, allow_question: bool,
                  household_id: int | None = None, answer: str | None = None,
                  valid_ids: set[int] | None = None) -> dict:
        history_names = ", ".join(
            item.get("wine_name") or item.get("name") or ""
            for item in history
            if isinstance(item, dict)
        )
        tail = (
            f"Profile: {profile}\n"
            f"Recently drunk: {history_names}\n"
            f"They asked: \"{sentence}\"\n"
            f"Parsed: {json.dumps(taste, default=str)}"
        )
        if answer:
            tail += f"\nThey answered: \"{answer}\""
        data = self._call(
            RECOMMEND_MODEL,
            [
                {"role": "system", "content": RECOMMEND_PROMPT},
                {"role": "user", "content": cellar_block},
                {"role": "user", "content": tail},
            ],
            "whiney_recommend",
            recommend_schema(allow_question=allow_question),
            RECOMMEND_TIMEOUT,
            household_id=household_id,
        )
        checked = _validate_recommend(data, allow_question=allow_question)
        try:
            return check_recommendation(checked, valid_ids)
        except ChatFailure as exc:
            # A bad pick is the model's fault, not the network's, so it must
            # not open the breaker. _call already counted this response as ok.
            logger.warning("[chat] recommend failed kind=%s detail=%s", exc.kind, exc.detail)
            raise


    def wine_chat(self, sentence: str, profile: str, *, household_id: int | None = None) -> str:
        data = self._call(
            RECOMMEND_MODEL,
            [
                {"role": "system", "content": WINE_CHAT_PROMPT},
                {"role": "user", "content": f"Taste profile: {profile or 'not known yet'}\nThey asked: \"{sentence}\""},
            ],
            "whiney_wine_chat",
            _obj({"reply": _string()}),
            WINE_CHAT_TIMEOUT,
            household_id=household_id,
        )
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise ChatFailure("bad_output", "empty reply")
        return reply[:1200]

    def support(self, sentence: str, *, staff: bool = False, household_id: int | None = None,
                features: dict | None = None) -> str:
        staff_line = (
            "- This person is staff: they have /newcellar, /chatstats and /admin."
            if staff else
            "- This person is not staff: do not tell them to use staff-only commands except to say /newcellar appears only for staff."
        )
        data = self._call(
            PARSE_MODEL,
            [
                {"role": "system", "content": SUPPORT_PROMPT.format(
                    staff_line=staff_line, club_facts=club_support_facts(features),
                )},
                {"role": "user", "content": sentence},
            ],
            "whiney_support",
            _obj({"reply": _string()}),
            WINE_CHAT_TIMEOUT,
            household_id=household_id,
        )
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise ChatFailure("bad_output", "empty reply")
        return reply[:1200]

    def phrase(self, user_text: str, facts: dict, *, locale: str = "en",
               household_id: int | None = None) -> str:
        """Phrase API facts in the user's language. Never a source of facts."""
        data = self._call(
            PARSE_MODEL,
            [
                {"role": "system", "content": PHRASE_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"language": locale, "message": user_text, "facts": facts},
                    default=str,
                )},
            ],
            "whiney_phrase",
            _obj({"reply": _string()}),
            WINE_CHAT_TIMEOUT,
            household_id=household_id,
        )
        reply = (data.get("reply") or "").strip()
        if not reply:
            raise ChatFailure("bad_output", "empty reply")
        return reply[:1200]


    def find_places(self, request: str, profile: str, *, area: str = "",
                    lat: float | None = None, lon: float | None = None,
                    country: str = "") -> list[dict]:
        """Live web search for wine-friendly places. Slow (about 5s), so callers show progress."""
        if lat is not None and lon is not None:
            where = f"within about 5 km of latitude {lat:.4f}, longitude {lon:.4f}"
        elif area:
            where = f"in or near {area}" + (f" ({country})" if country else "")
        else:
            raise ChatFailure("bad_output", "no place to search")
        until = self._open_until.get(PLACE_MODEL, 0.0)
        if self._now() < until:
            raise ChatFailure("unavailable", "circuit open")
        probing = until != 0.0
        payload = {
            "model": PLACE_MODEL,
            "input": [
                {"role": "system", "content": PLACE_PROMPT.format(where=where)},
                {"role": "user", "content": f"Their taste: {profile or 'not known yet'}\nThey asked: \"{request}\""},
            ],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": 900,
            "temperature": 0,
        }
        try:
            resp = self._post(
                f"{BASE_URL}/responses",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=PLACE_TIMEOUT,
            )
        except requests.Timeout as exc:
            self._fail(PLACE_MODEL, "timeout", probing=probing)
            raise ChatFailure("timeout", str(exc)) from exc
        except requests.ConnectionError as exc:
            self._fail(PLACE_MODEL, "unavailable", probing=probing)
            raise ChatFailure("unavailable", str(exc)) from exc
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            kind = _http_kind(status)
            self._fail(PLACE_MODEL, kind, probing=probing)
            logger.warning("[chat] places failed kind=%s detail=%s", kind, status)
            raise ChatFailure(kind, str(status))
        try:
            text = _responses_text(resp.json())
            data = _trailing_json(text)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("[chat] places failed kind=bad_output detail=%s", exc)
            raise ChatFailure("bad_output", str(exc)) from exc
        self._ok(PLACE_MODEL)
        places = []
        for row in (data or {}).get("places") or []:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                continue
            url = str(row.get("url") or "").strip()
            places.append({
                "name": str(row["name"]).strip()[:80],
                "area": str(row.get("area") or "").strip()[:60],
                "why": _CITATION.sub("", str(row.get("why") or "")).strip()[:200],
                "url": url if url.startswith(("https://", "http://")) else "",
            })
        return places[:3]


_CITATION = re.compile(r"\[\[\d+\]\]\([^)]*\)|\[\d+\]")


def _responses_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    chunks = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                chunks.append(part.get("text") or "")
    if not chunks:
        raise ValueError("no text in response")
    return "\n".join(chunks)


def _trailing_json(text: str) -> dict:
    """The prompt asks for prose-free JSON last; take the last balanced object."""
    s = text.strip()
    if s.endswith("```"):
        s = s[: s.rfind("```")].rstrip()
        fence = s.rfind("```")
        if fence != -1:
            s = s[fence + 3:].lstrip()
            if s.startswith("json"):
                s = s[4:]
    end = s.rfind("}")
    if end == -1:
        raise ValueError("no JSON object")
    depth = 0
    for start in range(end, -1, -1):
        if s[start] == "}":
            depth += 1
        elif s[start] == "{":
            depth -= 1
            if depth == 0:
                data = json.loads(s[start:end + 1])
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                return data
    raise ValueError("unbalanced JSON")


def _retry_after_seconds(resp) -> float | None:
    raw = ""
    headers = getattr(resp, "headers", None) or {}
    try:
        raw = headers.get("Retry-After", "")
    except Exception:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _http_kind(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    return "unavailable"


def _load_json(content) -> dict:
    if isinstance(content, dict):
        return content
    text = str(content).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _validate_parse(data: dict) -> dict:
    raw = data.get("intent")
    if raw not in INTENTS and raw not in INTENT_ALIASES:
        raise ChatFailure("bad_output", "intent")
    data["raw_intent"] = raw
    if str(raw) == "offtopic":
        data["unknown_kind"] = "offtopic"
    elif str(raw) == "unsure" and (data.get("unknown_kind") or "none") in ("none", "", None):
        data["unknown_kind"] = "unclear"
    data["intent"] = canonical_intent(raw)
    for key in ("fact", "taste", "price", "correction"):
        if not isinstance(data.get(key), dict):
            raise ChatFailure("bad_output", f"missing {key}")
    if "wine_ref" not in data or "answer_index" not in data:
        raise ChatFailure("bad_output", "missing field")
    fact = data["fact"]
    if fact.get("kind") not in FACT_KINDS:
        raise ChatFailure("bad_output", "fact.kind")
    if data["taste"].get("colour") not in COLOURS:
        raise ChatFailure("bad_output", "colour")
    data.setdefault("area", "")
    topic = data.get("help_topic") or "other"
    data["help_topic"] = topic if topic in HELP_TOPICS else "other"
    club = data.get("club") if isinstance(data.get("club"), dict) else _blank_club()
    blank = _blank_club()
    blank.update({k: club.get(k, blank[k]) for k in blank})
    kind = blank.get("kind") or "none"
    blank["kind"] = kind if kind in CLUB_KINDS else "none"
    data["club"] = blank
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        conf = 1.0 if data["intent"] != "unknown" else 0.3
    data["confidence"] = max(0.0, min(1.0, conf))
    slots = blank_slots()
    incoming = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    for key in SLOT_KEYS:
        if key in incoming:
            slots[key] = incoming[key]
    data["slots"] = slots
    list_op = data.get("list_op") or "none"
    data["list_op"] = list_op if list_op in LIST_OPS else "none"
    unknown_kind = data.get("unknown_kind") or "none"
    data["unknown_kind"] = unknown_kind if unknown_kind in UNKNOWN_KINDS else "none"
    return data


def _validate_recommend(data: dict, *, allow_question: bool) -> dict:
    if not isinstance(data.get("pick"), dict):
        raise ChatFailure("bad_output", "pick")
    evidence = data["pick"].get("evidence")
    if evidence not in EVIDENCE:
        raise ChatFailure("bad_output", "evidence")
    if not allow_question:
        data.pop("question", None)
        data.pop("options", None)
    else:
        data.setdefault("question", "")
        data.setdefault("options", [])
    data.setdefault("runner_up", _blank_pick())
    data.setdefault("alternatives", [])
    data.setdefault("no_evidence_for", [])
    if not isinstance(data["alternatives"], list) or not isinstance(data["no_evidence_for"], list):
        raise ChatFailure("bad_output", "lists")
    return data
