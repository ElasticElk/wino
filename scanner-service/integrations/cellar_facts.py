"""Shelf questions, answered from the stock rows. No model, no network.

The parser decides what was asked. This module only counts bottles that
are actually in the payload, so a reply cannot name a wine that is not there.
"""

from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

import pycountry

# Same map as chat_models.COLOUR_TO_TYPE. Kept here so this module stays
# free of the chat client.
COLOUR_TO_TYPE = {
    "red": "RE", "white": "WH", "rose": "RO", "sparkling": "SP",
    "dessert": "DE", "fortified": "FO", "orange": "OR",
}

# Folded alias -> folded canonical. Both sides of a pair point at one name,
# so Shiraz and Syrah compare equal.
_ALIAS_GROUPS = (
    ("syrah", ("shiraz", "syrah")),
    ("grenache", ("grenache", "garnacha", "cannonau")),
    ("pinot gris", ("pinot gris", "pinot grigio")),
    ("zinfandel", ("zinfandel", "primitivo")),
    ("mourvedre", ("mourvedre", "monastrell", "mataro")),
    ("carignan", ("carignan", "carinena", "mazuelo")),
    ("tempranillo", ("tempranillo", "tinta roriz", "aragonez")),
    ("chenin blanc", ("chenin blanc", "steen")),
    ("sauvignon blanc", ("sauvignon blanc", "fume blanc")),
    ("pinot noir", ("pinot noir", "spatburgunder")),
    ("cabernet sauvignon", ("cabernet sauvignon", "cab sauv")),
)
GRAPE_ALIASES = {
    alias: canonical
    for canonical, aliases in _ALIAS_GROUPS
    for alias in aliases
}
_GENERIC_STORAGE = {"fridge", "rack", "cellar", "fridges", "racks"}
_STOP_WORDS = {"my", "the"}


@dataclass
class FactReply:
    text: str
    kind: str
    wine_ids: list[int] = field(default_factory=list)
    storage_choices: list[str] = field(default_factory=list)
    suggest_close: str = ""


def fold(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return stripped.casefold().strip()


def canonical_grape(name: str) -> str:
    folded = fold(name)
    return GRAPE_ALIASES.get(folded, folded)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _label(grape: str) -> str:
    text = (grape or "").strip()
    if not text:
        return text
    return text[:1].upper() + text[1:]


def resolve_storage(text: str, storages: list[str]) -> list[str]:
    words = [w for w in fold(text).split() if w and w not in _STOP_WORDS]
    if not words:
        return []
    hits = [
        name for name in storages
        if all(word in fold(name) for word in words)
    ]
    return hits


def _in_stock(wines: list[dict]) -> list[dict]:
    return [w for w in wines if (w.get("stock_count") or 0) > 0]


def _storages_of(wines: list[dict]) -> list[str]:
    seen = []
    for wine in wines:
        for item in wine.get("items") or []:
            name = item.get("storage") or ""
            if name and name not in seen:
                seen.append(name)
    return seen


def _where(items: list[dict]) -> str:
    bits = []
    for item in items:
        storage = item.get("storage") or ""
        drawer = item.get("drawer")
        if drawer is None:
            bits.append(storage)
        else:
            bits.append(f"{storage}, D{drawer}" if storage else f"D{drawer}")
    return "; ".join(bit for bit in bits if bit)


def _lines(rows: list[tuple[dict, list[dict], str]]) -> tuple[str, list[int]]:
    lines = []
    ids = []
    for wine, items, extra in rows:
        ids.append(wine["id"])
        name = _esc(wine.get("name") or "a bottle")
        where = _esc(_where(items))
        count = len(items)
        suffix = f" ×{count}" if count > 1 else ""
        extra_bit = f" {extra}" if extra else ""
        place = f" — {where}" if where else ""
        lines.append(f"• <b>{name}</b>{extra_bit}{place}{suffix}")
    return "\n".join(lines), ids


def _items_in(wine: dict, storage: str | None, drawer: int | None) -> list[dict]:
    found = []
    for item in wine.get("items") or []:
        if storage is not None and fold(item.get("storage") or "") != fold(storage):
            continue
        if drawer is not None and item.get("drawer") != drawer:
            continue
        found.append(item)
    return found


def _choose(choices: list[str]) -> FactReply:
    names = ", ".join(_esc(name) for name in choices)
    return FactReply(
        text=f"Which one? {names}",
        kind="choose_storage",
        storage_choices=list(choices),
    )


def _missing_storage(text: str, storages: list[str]) -> FactReply:
    names = ", ".join(_esc(name) for name in storages) or "none yet"
    return FactReply(
        text=f"I can't find a storage called '{_esc(text)}'. You have: {names}.",
        kind="storage",
    )


def _country_name(code: str) -> str:
    country = pycountry.countries.get(alpha_2=(code or "").upper())
    return country.name if country is not None else code


def answer(fact: dict, wines: list[dict]) -> FactReply | None:
    kind = (fact or {}).get("kind") or "none"
    if kind in ("none", ""):
        return None
    stock = _in_stock(wines)
    storages = _storages_of(stock)
    storage_text = (fact.get("storage_text") or "").strip()

    if kind == "list_all":
        return FactReply(text="", kind="list_all")

    if kind == "drawer":
        drawer = fact.get("drawer")
        if not isinstance(drawer, int):
            return None
        if storage_text:
            hits = resolve_storage(storage_text, storages)
            if len(hits) >= 2:
                return _choose(hits)
            if not hits:
                return _missing_storage(storage_text, storages)
            storage = hits[0]
        elif len(storages) > 1:
            return _choose(storages)
        elif len(storages) == 1:
            storage = storages[0]
        else:
            storage = ""
        rows = []
        for wine in stock:
            items = _items_in(wine, storage or None, drawer)
            if items:
                rows.append((wine, items, ""))
        title = f"D{drawer} — {_esc(storage)}" if storage else f"D{drawer}"
        if not rows:
            where = f" in the {_esc(storage)}" if storage else ""
            return FactReply(text=f"D{drawer}{where} is empty.", kind="empty")
        body, ids = _lines(rows)
        return FactReply(text=f"{title}\n{body}", kind="drawer", wine_ids=ids)

    if kind == "storage":
        if not storage_text:
            return None
        hits = resolve_storage(storage_text, storages)
        if len(hits) >= 2:
            return _choose(hits)
        if not hits:
            return _missing_storage(storage_text, storages)
        storage = hits[0]
        by_drawer: dict[int | None, list] = {}
        ids = []
        for wine in stock:
            items = _items_in(wine, storage, None)
            if not items:
                continue
            ids.append(wine["id"])
            for item in items:
                by_drawer.setdefault(item.get("drawer"), []).append((wine, [item], ""))
        if not ids:
            return FactReply(
                text=f"{_esc(storage)} is empty.", kind="empty",
            )
        chunks = [f"<b>{_esc(storage)}</b>"]
        for drawer in sorted(by_drawer, key=lambda d: (d is None, d or 0)):
            if drawer is not None:
                chunks.append(f"D{drawer}")
            body, _ids = _lines(by_drawer[drawer])
            chunks.append(body)
        return FactReply(text="\n".join(chunks), kind="storage", wine_ids=ids)

    if kind == "country":
        code = (fact.get("country_code") or "").strip()
        if not code:
            return None
        rows = []
        for wine in stock:
            if fold(wine.get("country") or "") != fold(code):
                continue
            rows.append((wine, wine.get("items") or [], ""))
        name = _country_name(code)
        if not rows:
            return FactReply(
                text=f"Nothing from {_esc(name)} in the cellar.", kind="country",
            )
        body, ids = _lines(rows)
        return FactReply(text=f"From {_esc(name)}\n{body}", kind="country", wine_ids=ids)

    if kind == "grape":
        asked = (fact.get("grape") or "").strip()
        if not asked:
            return None
        wanted = canonical_grape(asked)
        rows = []
        literal = False
        for wine in stock:
            grapes = [g for g in (wine.get("grapes") or []) if g]
            matched = [g for g in grapes if canonical_grape(g) == wanted]
            if not matched:
                continue
            if any(fold(g) == fold(asked) for g in matched):
                literal = True
            extra = ""
            if len(grapes) > 1:
                extra = f"(blend with {_esc(matched[0])})"
            rows.append((wine, wine.get("items") or [], extra))
        label = _label(asked)
        if not rows:
            return FactReply(
                text=f"You don't have a {_esc(label)}.",
                kind="grape",
                suggest_close=label,
            )
        body, ids = _lines(rows)
        if literal:
            title = f"{_esc(label)} in your cellar:"
        else:
            # The name printed on the bottle, not the word they typed.
            shown = _esc(next(
                g for wine, _items, _extra in rows
                for g in (wine.get("grapes") or [])
                if canonical_grape(g) == wanted
            ))
            title = f"No bottle labelled {_esc(label)}, but these are the same grape ({shown}):"
        return FactReply(text=f"{title}\n{body}", kind="grape", wine_ids=ids)

    return None


def _parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def quick_pick(taste: dict, wines: list[dict], history: list[dict],
               today: date | None = None) -> list[dict]:
    """A bottle chosen in code, for when the sommelier model is down.

    Best first, at most five. Empty when the colour filter or the
    thumbs-down filter leaves nothing.
    """
    today = today or date.today()
    colour = (taste or {}).get("colour") or ""
    words = [fold(w) for w in (taste or {}).get("words") or [] if fold(w)]
    recent = set()
    for item in (history or [])[:5]:
        if isinstance(item, dict) and item.get("wine_id") is not None:
            recent.add(item["wine_id"])
        elif isinstance(item, dict) and item.get("id") is not None:
            recent.add(item["id"])
    scored = []
    for wine in _in_stock(wines):
        if colour and colour != "any":
            if wine.get("wine_type") != COLOUR_TO_TYPE.get(colour):
                continue
        votes = wine.get("votes") or {}
        if (votes.get("down") or 0) > 0:
            continue
        note = fold(wine.get("comment") or "")
        attrs = fold(" ".join(wine.get("attributes") or []))
        grapes = fold(" ".join(wine.get("grapes") or []))
        evidence = "none"
        score = 0
        for word in words:
            if word and word in note:
                score += 3
                evidence = "note"
            elif word and word in attrs:
                score += 3
                if evidence == "none":
                    evidence = "attribute"
            elif word and word in grapes:
                score += 3
                if evidence == "none":
                    evidence = "grape"
        drink_by = _parse_day(wine.get("drink_by"))
        if drink_by is not None and drink_by <= today + timedelta(days=365):
            score += 2
        score += int(votes.get("up") or 0)
        if wine.get("id") in recent:
            score -= 2
        scored.append((score, wine.get("id") or 0, wine, evidence))
    scored.sort(key=lambda row: (-row[0], row[1]))
    picked = []
    for _score, _wine_id, wine, evidence in scored[:5]:
        if evidence == "note":
            reason = "The tasting note matches what you asked for."
        elif evidence == "attribute":
            reason = "It's marked with what you asked for."
        elif evidence == "grape":
            reason = "The grape matches what you asked for."
        else:
            reason = "A solid bottle from what's open in the cellar."
        picked.append({
            "id": wine.get("id"),
            "reason": reason,
            "evidence": evidence,
            "wine": wine,
        })
    return picked
