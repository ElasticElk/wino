"""Reference refresh: ask Grok for critic score, Vivino rating and market price.

Lives outside the view because the work takes far longer than a web request
should. Grok's live search budgets up to 120s per attempt and retries once, so
a synchronous call could run for four minutes — while gunicorn's default
timeout is 30s and its default worker count is one, meaning a single refresh
both died and blocked every other request (including all bot traffic) until it
did. This function is called from a Celery task instead.

Written as a plain function so it can be tested without a broker.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from wine_cellar.apps.wine.grok_references import fetch_references, validate_reference_field
from wine_cellar.apps.wine.models import Wine

logger = logging.getLogger(__name__)

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _producer_hint(wine) -> str:
    """Producer, falling back to the first vineyard when it isn't set."""
    if wine.producer:
        return wine.producer
    if wine.vineyard.exists():
        return wine.vineyard.first().name
    return ""


def _currency_code(returned, fallback: str) -> str:
    """An ISO code for whatever Grok put in market_price_currency.

    It sometimes answers with a symbol ("£") or a word ("euro"); truncating
    those gave codes like "EUR" by luck and "£" or "POU" otherwise, which
    then never convert.
    """
    if not isinstance(returned, str) or not returned.strip():
        return fallback
    raw = returned.strip()
    by_symbol = {sym: code for code, sym in settings.CURRENCY_SYMBOLS.items()}
    if raw in by_symbol:
        return by_symbol[raw]
    word = raw.upper()
    aliases = {"EURO": "EUR", "EUROS": "EUR", "POUND": "GBP", "POUNDS": "GBP",
               "RAND": "ZAR", "DOLLAR": "USD", "DOLLARS": "USD"}
    if word in aliases:
        return aliases[word]
    if len(word) == 3 and word.isalpha():
        return word
    return fallback


def validated_fields(data: dict, fallback_currency: str) -> tuple[dict, list, dict]:
    """Pull the reference fields of a Grok response that are good enough to keep.

    Returns (values, confidences, details): `values` maps model field names to
    the values that passed validation, `confidences` lists their confidence
    words, `details` is the per-field breakdown reported back to the caller.
    Shared by the cellar refresh and the wine-less lookup so a label read in a
    shop is held to exactly the same standard as one already on the shelf.
    """
    values: dict = {}
    confidences: list = []
    details = {"critic_score": None, "vivino_rating": None, "market_price": None}

    critic = data.get("critic_score")
    if validate_reference_field(
        critic, data.get("critic_score_confidence"), data.get("critic_score_reasoning"),
    ) and isinstance(critic, (int, float)) and 0 <= critic <= 100:
        conf = (data.get("critic_score_confidence") or "").lower()
        values["critic_score"] = int(critic)
        confidences.append(conf)
        details["critic_score"] = {"value": int(critic), "confidence": conf}

    vivino = data.get("vivino_rating")
    if validate_reference_field(
        vivino, data.get("vivino_rating_confidence"), data.get("vivino_rating_reasoning"),
    ) and isinstance(vivino, (int, float)) and 0 <= vivino <= 5:
        conf = (data.get("vivino_rating_confidence") or "").lower()
        values["vivino_rating"] = round(float(vivino), 1)
        confidences.append(conf)
        details["vivino_rating"] = {"value": round(float(vivino), 1), "confidence": conf}

    price = data.get("market_price")
    if validate_reference_field(
        price, data.get("market_price_confidence"), data.get("market_price_reasoning"),
    ) and isinstance(price, (int, float)) and price >= 0:
        conf = (data.get("market_price_confidence") or "").lower()
        values["market_price"] = price
        values["market_price_currency"] = _currency_code(
            data.get("market_price_currency"), fallback_currency,
        )
        confidences.append(conf)
        details["market_price"] = {
            "value": float(price),
            "currency": values.get("market_price_currency"),
            "confidence": conf,
        }

    return values, confidences, details


def blended_category(critic, vivino):
    """The 70/30 critic+Vivino composite, mirroring Wine.save()."""
    if critic is not None and vivino is not None:
        return round(critic * 0.7 + float(vivino) * 20.0 * 0.3)
    if critic is not None:
        return int(critic)
    if vivino is not None:
        return round(float(vivino) * 20.0)
    return None


def _currency_country_for(user_id):
    """The user's preferred currency and country, for price grounding."""
    from django.contrib.auth import get_user_model

    if user_id is None:
        return "EUR", ""
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is not None and hasattr(user, "user_settings"):
        return user.user_settings.currency or "EUR", user.user_settings.country or ""
    return "EUR", ""


def perform_reference_lookup(
    name,
    producer="",
    vintage=None,
    user_id=None,
    country="",
    region="",
    appellation="",
    grape="",
    vineyard="",
) -> dict:
    """Look up references for a wine that has no row in the cellar.

    Same live search as perform_reference_refresh, minus the Wine: a label
    photographed in a shop is exactly where the critic score, the Vivino rating
    and the going price matter most, and there is nothing yet to persist them
    to. Nothing is written; the caller just renders what comes back.
    """
    if not name:
        return {"updated": False, "error": "Name is required"}

    currency, target_country = _currency_country_for(user_id)

    data = fetch_references(
        name=name,
        producer=producer or "",
        vintage=vintage,
        target_currency=currency,
        target_country=target_country,
        country=country or "",
        region=region or "",
        appellation=appellation or "",
        grape=grape or "",
        vineyard=vineyard or "",
    )
    if data is None:
        return {"updated": False, "error": "Grok request failed"}

    values, found, details = validated_fields(data, currency)
    if not found:
        return {
            "updated": False,
            "confidence": "low",
            "message": (
                "Grok's live search did not find usable references for this wine."
            ),
            "details": details,
        }

    critic = values.get("critic_score")
    vivino = values.get("vivino_rating")
    price = values.get("market_price")
    return {
        "updated": True,
        "confidence": max(found, key=lambda c: CONFIDENCE_RANK.get(c, 0)),
        "critic_score": critic,
        "vivino_rating": float(vivino) if vivino is not None else None,
        "category_rating": blended_category(critic, vivino),
        "market_price": float(price) if price is not None else None,
        "market_price_currency": values.get("market_price_currency") or currency,
        "details": details,
    }


def _stored_references(wine) -> dict:
    return {
        "confidence": wine.reference_confidence,
        "critic_score": wine.critic_score,
        "vivino_rating": (
            float(wine.vivino_rating) if wine.vivino_rating is not None else None
        ),
        "category_rating": wine.category_rating,
        "market_price": (
            float(wine.market_price) if wine.market_price is not None else None
        ),
        "market_price_currency": wine.market_price_currency,
    }


def perform_reference_refresh(wine_id, user_id=None, force=False) -> dict:
    """Look up references for one wine and persist whatever passes validation.

    Per-field preservation: each field keeps its prior value unless *this* run
    produces a valid replacement for that specific field, so a bad search day
    for one field doesn't destroy good prior data.

    A lookup that landed in the last week is kept unless the caller forces a
    new search — the label card already paid for one.
    """
    try:
        wine = Wine.objects.get(id=wine_id)
    except Wine.DoesNotExist:
        return {"updated": False, "error": "Wine not found"}

    if (
        not force
        and wine.reference_updated is not None
        and timezone.now() - wine.reference_updated < timedelta(days=7)
    ):
        return {"updated": False, "skipped": True, **_stored_references(wine)}

    currency, country = _currency_country_for(user_id)

    data = fetch_references(
        name=wine.name,
        producer=_producer_hint(wine),
        vintage=wine.vintage,
        target_currency=currency,
        target_country=country,
        country=wine.country or "",
        region=wine.region.name if wine.region else "",
        appellation=wine.appellation.name if wine.appellation else "",
        grape=", ".join(g.name for g in wine.grapes.all()),
        vineyard=", ".join(v.name for v in wine.vineyard.all()),
    )
    if data is None:
        return {"updated": False, "error": "Grok request failed"}

    values, persisted, details = validated_fields(data, currency)
    for field, value in values.items():
        setattr(wine, field, value)

    if not persisted:
        kept_any = any(
            getattr(wine, f) is not None
            for f in ("critic_score", "vivino_rating", "market_price")
        )
        return {
            "updated": False,
            "confidence": "low",
            "message": (
                "Grok's live search did not find usable references for this "
                "wine. Existing values (if any) were kept."
            ),
            "details": details,
            "kept_prior": kept_any,
        }

    # Aggregate confidence: highest of this run's new fields and any prior
    # confidence that still applies to kept fields, so a high-confidence field
    # already in place doesn't get regressed by a medium one landing now.
    candidates = list(persisted)
    if wine.reference_confidence:
        candidates.append(wine.reference_confidence.lower())
    wine.reference_confidence = max(candidates, key=lambda c: CONFIDENCE_RANK.get(c, 0))
    wine.reference_updated = timezone.now()
    wine.save()

    try:
        from wine_cellar.apps.wine.taste import rebuild_snapshot
        rebuild_snapshot(wine.household)
    except Exception:
        pass

    return {
        "updated": True,
        **_stored_references(wine),
        "details": details,
    }
