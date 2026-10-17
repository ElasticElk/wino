"""Household taste — inferred from the cellar and from shop/list looking.

Not ratings. The snapshot is counts (styles, grapes, countries, decades,
price bands). Intents are wines they photographed and didn't add, plus
wine lists. Recommendations later read this; /taste shows it in Telegram.
"""

from __future__ import annotations

import html
import math
from collections import Counter

import pycountry
from django.conf import settings

from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.models import (
    HouseholdTasteSnapshot,
    TasteIntent,
    WineType,
)
from wine_cellar.apps.wine.utils import convert_price

# Band edges in USD, rounded to tidy figures in the household's own currency
# (so a GBP cellar reads "under £15", a ZAR one "under R350"). Bottles are
# converted before banding — a R420 bottle is an everyday wine, not a £420 one.
PRICE_BAND_EDGES_USD = (20, 50)
DEFAULT_CURRENCY = "EUR"  # UserSettings.currency default, which the web shows


def _tidy(amount: float) -> int:
    """Round to a figure a person would write: 15.7 -> 15, 370 -> 350, 926 -> 950."""
    if amount <= 0:
        return 0
    magnitude = 10 ** math.floor(math.log10(amount))
    return int(round(amount / magnitude * 2) / 2 * magnitude) or 1


def _band_edges(currency: str) -> tuple[int, ...]:
    return tuple(
        _tidy(float(convert_price(edge, "USD", currency) or edge))
        for edge in PRICE_BAND_EDGES_USD
    )


def _band_labels(currency: str) -> list[str]:
    sym = settings.CURRENCY_SYMBOLS.get(currency, currency + " ")
    low, high = _band_edges(currency)
    return [f"under {sym}{low}", f"{sym}{low}-{high}", f"{sym}{high}+"]


def _price_band(price, currency: str = DEFAULT_CURRENCY) -> str | None:
    """`price` must already be in `currency`."""
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    labels = _band_labels(currency)
    for edge, label in zip(_band_edges(currency), labels):
        if p < edge:
            return label
    return labels[-1]


def _user_currency(user) -> str | None:
    us = getattr(user, "user_settings", None) if user is not None else None
    return getattr(us, "currency", None) or None


def household_currency(household) -> str:
    """The owner's currency — what the household's taste is described in."""
    membership = (
        household.memberships.filter(role="owner")
        .select_related("user__user_settings")
        .order_by("created")
        .first()
    ) or household.memberships.select_related("user__user_settings").order_by("created").first()
    return (_user_currency(membership.user) if membership else None) or DEFAULT_CURRENCY


def _bottle_price(item) -> tuple[object, str | None]:
    """(price, currency) — paid price if we have one, else the shop estimate.

    StorageItem.price / Wine.price is what they paid, in the currency of
    whoever recorded it. Wine.market_price is Grok's later lookup, in
    whatever currency it was found in — used only when nothing was paid.
    """
    if item.price is not None:
        return item.price, _user_currency(item.user)
    wine = item.wine
    if wine is None:
        return None, None
    if wine.price is not None:
        return wine.price, _user_currency(wine.user)
    return wine.market_price, wine.market_price_currency


def _country_label(code: str) -> str:
    if not code:
        return code
    try:
        c = pycountry.countries.get(alpha_2=code)
        return c.name if c else code
    except Exception:
        return code


def _type_label(code: str) -> str:
    try:
        return str(WineType(code).label)
    except ValueError:
        return code


def _top(counter: Counter, n: int = 6) -> list[list]:
    return [[k, v] for k, v in counter.most_common(n) if k]


def rebuild_snapshot(household) -> HouseholdTasteSnapshot | None:
    if household is None:
        return None
    items = list(
        StorageItem.objects.filter(wine__household=household)
        .select_related("wine", "user__user_settings", "wine__user__user_settings")
        .prefetch_related("wine__grapes")
    )
    currency = household_currency(household)
    types, countries, grapes, producers, decades, bands = (
        Counter(), Counter(), Counter(), Counter(), Counter(), Counter(),
    )
    in_stock_ids = set()
    drunk = 0
    for item in items:
        wine = item.wine
        if wine is None:
            continue
        if item.deleted:
            drunk += 1
        else:
            in_stock_ids.add(wine.id)
        if wine.wine_type:
            types[wine.wine_type] += 1
        if wine.country:
            countries[wine.country] += 1
        producer = (wine.producer or "").strip()
        if producer:
            producers[producer] += 1
        if wine.vintage:
            decades[str((wine.vintage // 10) * 10)] += 1
        price, price_currency = _bottle_price(item)
        if price is not None and price_currency and price_currency != currency:
            # No rate for that currency -> leave it out rather than guess.
            price = convert_price(price, price_currency, currency)
        band = _price_band(price, currency)
        if band:
            bands[band] += 1
        for grape in wine.grapes.all():
            if grape.name:
                grapes[grape.name] += 1

    looks = TasteIntent.objects.filter(
        household=household, kind=TasteIntent.LOOK,
    ).count()
    lists = TasteIntent.objects.filter(
        household=household, kind=TasteIntent.WINELIST,
    ).count()

    counts = {
        "in_stock_wines": len(in_stock_ids),
        "bought_bottles": len(items),
        "drunk_bottles": drunk,
        "looks": looks,
        "winelists": lists,
        "types": _top(types),
        "countries": _top(countries),
        "grapes": _top(grapes),
        "producers": _top(producers, 8),
        "decades": _top(decades),
        "price_bands": _top(bands),
    }
    summary = _summary_from_counts(household, counts)
    obj, _ = HouseholdTasteSnapshot.objects.update_or_create(
        household=household,
        defaults={"counts": counts, "summary": summary},
    )
    return obj


def _fmt_pairs(pairs, label_fn=None) -> str:
    bits = []
    for key, n in pairs or []:
        name = str(label_fn(key) if label_fn else key)
        bits.append(f"{name} ({n})" if n > 1 else name)
    return ", ".join(bits)


def _summary_from_counts(household, counts: dict) -> str:
    lines = []
    types = _fmt_pairs(counts.get("types"), _type_label)
    if types:
        lines.append(f"Styles: {types}")
    grapes = _fmt_pairs(counts.get("grapes"))
    if grapes:
        lines.append(f"Grapes: {grapes}")
    countries = _fmt_pairs(counts.get("countries"), _country_label)
    if countries:
        lines.append(f"Countries: {countries}")
    producers = _fmt_pairs(counts.get("producers"))
    if producers:
        lines.append(f"Producers: {producers}")
    decades = _fmt_pairs(
        counts.get("decades"),
        lambda d: f"{d}s",
    )
    if decades:
        lines.append(f"Decades: {decades}")
    bands = _fmt_pairs(counts.get("price_bands"))
    if bands:
        lines.append(f"Price bands: {bands}")

    recent_looks = list(
        TasteIntent.objects.filter(household=household, kind=TasteIntent.LOOK)
        .order_by("-created")[:5]
    )
    names = []
    for intent in recent_looks:
        name = (intent.payload or {}).get("name") or ""
        if name:
            names.append(name)
    if names:
        lines.append(f"Recently looked at (not bought): {'; '.join(names)}")
    return "\n".join(lines)


def record_intent(household, user, kind: str, payload: dict | None) -> TasteIntent | None:
    if household is None or kind not in (TasteIntent.LOOK, TasteIntent.WINELIST):
        return None
    intent = TasteIntent.objects.create(
        household=household,
        user=user,
        kind=kind,
        payload=payload or {},
    )
    rebuild_snapshot(household)
    return intent


def serialize_snapshot(household) -> dict:
    snap = getattr(household, "taste_snapshot", None)
    if snap is None:
        snap = rebuild_snapshot(household)
    if snap is None:
        return {
            "counts": {}, "summary": "", "updated_at": None,
            "recent_looks": [],
            "card": format_taste_card(None),
        }
    looks = TasteIntent.objects.filter(
        household=household, kind=TasteIntent.LOOK,
    )[:8]
    data = {
        "counts": snap.counts or {},
        "summary": snap.summary or "",
        "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        "recent_looks": [
            {
                "name": (i.payload or {}).get("name") or "",
                "producer": (i.payload or {}).get("producer") or "",
                "country": (i.payload or {}).get("country") or "",
                "created": i.created.isoformat() if i.created else None,
            }
            for i in looks
        ],
    }
    data["card"] = format_taste_card(data)
    return data


def format_taste_card(data: dict | None) -> str:
    """Telegram text for /taste."""
    if not data:
        return "No taste snapshot yet \u2014 add a bottle or send a label photo."
    counts = data.get("counts") or {}
    lines = ["\U0001F377 <b>What this cellar drinks</b>\n"]
    n = counts.get("in_stock_wines") or 0
    bought = counts.get("bought_bottles") or 0
    if bought:
        line = f"{bought} bottle(s) ever added to the cellar"
        if n:
            line += f", {n} in stock now."
        else:
            line += ", none in stock now."
        lines.append(line)
    elif n:
        lines.append(f"{n} wine(s) in stock right now.")
    summary = (data.get("summary") or "").strip()
    # Drop the "recently looked" line from summary; listed separately below.
    summary_lines = [
        ln for ln in summary.split("\n")
        if ln and not ln.startswith("Recently looked")
    ]
    if summary_lines:
        lines.append("")
        # Summary and names are plain text (the summary also feeds a prompt);
        # escape here so a band like "<150" isn't read as an HTML tag.
        lines.extend(html.escape(ln, quote=False) for ln in summary_lines)
    looks = data.get("recent_looks") or []
    named = [x.get("name") for x in looks if x.get("name")]
    if named:
        lines.append("")
        lines.append("<b>Looked at, not added</b>")
        for name in named[:6]:
            lines.append(f"  {html.escape(name, quote=False)}")
    look_n = counts.get("looks") or 0
    list_n = counts.get("winelists") or 0
    if look_n or list_n:
        lines.append("")
        bits = []
        if look_n:
            bits.append(f"{look_n} look(s)")
        if list_n:
            bits.append(f"{list_n} wine list(s)")
        lines.append(" · ".join(bits) + " remembered.")
    if len(lines) == 1:
        lines.append("Nothing logged yet. A label photo still counts as a look.")
    return "\n".join(lines)
