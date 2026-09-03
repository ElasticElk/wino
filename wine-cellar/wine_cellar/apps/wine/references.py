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


def perform_reference_refresh(wine_id, user_id=None) -> dict:
    """Look up references for one wine and persist whatever passes validation.

    Per-field preservation: each field keeps its prior value unless *this* run
    produces a valid replacement for that specific field, so a bad search day
    for one field doesn't destroy good prior data.
    """
    from django.contrib.auth import get_user_model

    try:
        wine = Wine.objects.get(id=wine_id)
    except Wine.DoesNotExist:
        return {"updated": False, "error": "Wine not found"}

    currency, country = "USD", ""
    if user_id is not None:
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is not None and hasattr(user, "user_settings"):
            currency = user.user_settings.currency or "USD"
            country = user.user_settings.country or ""

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

    persisted = []
    details = {"critic_score": None, "vivino_rating": None, "market_price": None}

    critic = data.get("critic_score")
    if validate_reference_field(
        critic, data.get("critic_score_confidence"), data.get("critic_score_reasoning"),
    ) and isinstance(critic, (int, float)) and 0 <= critic <= 100:
        wine.critic_score = int(critic)
        conf = (data.get("critic_score_confidence") or "").lower()
        persisted.append(conf)
        details["critic_score"] = {"value": int(critic), "confidence": conf}

    vivino = data.get("vivino_rating")
    if validate_reference_field(
        vivino, data.get("vivino_rating_confidence"), data.get("vivino_rating_reasoning"),
    ) and isinstance(vivino, (int, float)) and 0 <= vivino <= 5:
        wine.vivino_rating = round(float(vivino), 1)
        conf = (data.get("vivino_rating_confidence") or "").lower()
        persisted.append(conf)
        details["vivino_rating"] = {"value": round(float(vivino), 1), "confidence": conf}

    price = data.get("market_price")
    if validate_reference_field(
        price, data.get("market_price_confidence"), data.get("market_price_reasoning"),
    ) and isinstance(price, (int, float)) and price >= 0:
        wine.market_price = price
        returned = data.get("market_price_currency") or currency
        if isinstance(returned, str):
            wine.market_price_currency = returned.upper()[:3]
        conf = (data.get("market_price_confidence") or "").lower()
        persisted.append(conf)
        details["market_price"] = {
            "value": float(price),
            "currency": wine.market_price_currency,
            "confidence": conf,
        }

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

    return {
        "updated": True,
        "confidence": wine.reference_confidence,
        "critic_score": wine.critic_score,
        "vivino_rating": float(wine.vivino_rating) if wine.vivino_rating is not None else None,
        "category_rating": wine.category_rating,
        "market_price": float(wine.market_price) if wine.market_price is not None else None,
        "market_price_currency": wine.market_price_currency,
        "details": details,
    }
