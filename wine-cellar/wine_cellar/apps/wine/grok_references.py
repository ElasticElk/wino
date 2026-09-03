"""Text-only Grok client for refreshing wine reference data.

Per-field independent confidence — each of critic_score, vivino_rating,
market_price has its own confidence + reasoning. The server-side caller
(WineRefreshReferencesView) uses validate_reference_field() to decide which
fields to persist.
"""

import json
import logging

import requests

from wine_cellar.apps.scanner.models import ServiceConfig

logger = logging.getLogger(__name__)

GROK_MODEL = "grok-4.3"
GROK_BASE_URL = "https://api.x.ai/v1"

# Heuristics for rejecting hand-wavy reasoning that screams hallucination.
# Lowercase substring match. If any are found in reasoning, downgrade to "low".
GENERIC_REASONING_MARKERS = (
    "based on the grape",
    "based on grape",
    "typical for this style",
    "typical for the style",
    "general wine knowledge",
    "general knowledge",
    "estimated from general",
    "typically score",
    "usually scores",
    "i don't have specific",
    "no specific knowledge",
    "without specific",
)
MIN_REASONING_LEN = 15  # below this we treat reasoning as effectively empty


def is_valid_reasoning(text) -> bool:
    """Return True iff reasoning is non-empty, long enough, and not generic boilerplate."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < MIN_REASONING_LEN:
        return False
    lowered = t.lower()
    return not any(marker in lowered for marker in GENERIC_REASONING_MARKERS)


def validate_reference_field(value, confidence, reasoning) -> bool:
    """Decide whether a single reference field should be persisted.

    Persist iff value is not None, confidence is "medium" or "high",
    and reasoning passes is_valid_reasoning().
    """
    if value is None:
        return False
    conf = (confidence or "").lower()
    if conf not in ("medium", "high"):
        return False
    return is_valid_reasoning(reasoning)


def _location_phrase(country: str) -> str:
    if not country:
        return ""
    try:
        import pycountry
        c = pycountry.countries.get(alpha_2=country.upper())
        if c:
            return f" in {c.name}"
    except Exception:
        pass
    return f" in {country}"


def _build_prompt(name, producer, vintage, country, region, appellation, grape, vineyard, target_currency, target_country):
    parts = [name]
    if vintage:
        parts.append(str(vintage))
    descriptor = " ".join(parts)
    extras = []
    if producer:
        extras.append(f"producer: {producer}")
    if vineyard:
        extras.append(f"vineyard: {vineyard}")
    if grape:
        extras.append(f"grape: {grape}")
    if appellation:
        extras.append(f"appellation: {appellation}")
    if region:
        extras.append(f"region: {region}")
    if country:
        extras.append(f"country: {country}")
    extras_str = "\n".join(f"  - {e}" for e in extras) if extras else "  (no further details available)"
    location_phrase = _location_phrase(target_country)
    market_context = f"the typical retail price{location_phrase} expressed in {target_currency}"

    place = region or country or "unknown region"
    return f"""You are looking up reference data on a specific wine. Use live web search to find:
  1. The wine's Vivino page (vivino.com) — note the average rating and number of ratings.
  2. Critic reviews — search wine-critic sites and publications (Wine Spectator, Wine Advocate, Decanter, James Suckling, Vinous, Tim Atkin, Jancis Robinson, CellarTracker, Gilbert & Gaillard, Falstaff, regional press) for any score on this exact wine + vintage.
  3. UK / {target_currency} retail prices — search wine-searcher.com, The Wine Society, Berry Bros & Rudd, Lay & Wheeler, Lea & Sandeman, Majestic, Tesco, Waitrose, Sainsbury's, Ocado, and other current online listings. Take the spread of real listings and use the median; don't gravitate to premium-merchant pricing if the wine is widely sold cheaper at supermarkets/value retailers.

Wine: {descriptor}
Context:
{extras_str}

Step 1 — Briefly summarise what the searches actually returned. Quote real values from real pages: "Vivino: 4.1 (243 ratings)", "Tim Atkin SA Report 2022: 92pts", "The Wine Society £27.99, Lea & Sandeman £29, Majestic £24" etc. If you find data on adjacent vintages (e.g. you searched 2024 but only found 2022/2023 data), report that — adjacent vintages are usually a valid fallback.

Step 2 — Extract the values into JSON. For market_price, use the median of the real prices you found, not the maximum.

**A real price in another currency beats null.** If you find listings only in
EUR, USD, ZAR or similar, return that figure and set market_price_currency to
the currency you actually found — it gets converted afterwards. Returning null
because nothing was listed in {target_currency} throws away a price you have.
Only use null when you found no prices anywhere. **Use adjacent-vintage data when the exact vintage is missing — don't return null just because the exact vintage page wasn't indexed yet.** Most non-fine wines have stable pricing year to year; Vivino ratings carry across vintages on the same wine page; critic scores from the prior 1-2 vintages are a strong indicator. Apply confidence accordingly:
  • high   — exact wine + exact vintage found on a live page (Vivino, named critic, named retailer)
  • medium — exact wine but adjacent vintage (e.g. 2023 data when asked 2024), or same producer's comparable cuvée. Use the values you found, mark medium.
  • low    — searches returned nothing on this wine OR the producer's comparable wines; value = null

Confidence describes how well you know the *price*, not whether it was quoted in
the requested currency. Real EUR listings for an Italian wine are high or medium
confidence, not low.

The reasoning field should be one short sentence quoting or citing what you found ("Vivino 4.0 from ~150 ratings on the wine page covering all vintages", "2023 listed at £9.99-£12.25 across Frontier/Winedirect/Ministry of Drinks; assuming similar for 2024", "Tim Atkin SA Report 2022 gave the 2020 vintage 92pts").

End your response with ONLY this JSON object (no markdown fences, nothing after it):
{{
  "critic_score": null,
  "critic_score_confidence": "low",
  "critic_score_reasoning": null,
  "vivino_rating": null,
  "vivino_rating_confidence": "low",
  "vivino_rating_reasoning": null,
  "market_price": null,
  "market_price_currency": "{target_currency}",
  "market_price_confidence": "low",
  "market_price_reasoning": null
}}"""


def _has_any_value(data: dict | None) -> bool:
    """True if the response contains at least one non-null reference value."""
    if not data:
        return False
    return any(
        data.get(f) is not None
        for f in ("critic_score", "vivino_rating", "market_price")
    )


def fetch_references(
    name,
    producer="",
    vintage=None,
    target_currency="USD",
    target_country="",
    country="",
    region="",
    appellation="",
    grape="",
    vineyard="",
    max_attempts: int = 2,
):
    """Call Grok and return reference data for a wine, or None on failure.

    Retries up to `max_attempts` times if the first attempt returns all-null
    values — Grok's web search is non-deterministic and a retry often surfaces
    pages the first attempt missed.
    """
    try:
        api_key = ServiceConfig.objects.get(key="grok_api_key").value
    except ServiceConfig.DoesNotExist:
        logger.error("ServiceConfig 'grok_api_key' not set")
        return None
    if not api_key:
        logger.error("Grok API key is empty")
        return None

    prompt = _build_prompt(
        name, producer, vintage, country, region, appellation, grape, vineyard,
        target_currency, target_country,
    )
    payload = {
        "model": GROK_MODEL,
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search"}],
        "temperature": 0.0,
    }

    last_data = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                f"{GROK_BASE_URL}/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,  # web search adds latency
            )
            resp.raise_for_status()
            body = resp.json()
            content = _extract_responses_text(body)
            if content is None:
                logger.error(f"Grok responses returned no text on attempt {attempt}: {body!r}")
                if attempt < max_attempts:
                    continue
                return None
            data = _extract_trailing_json(content)
        except requests.RequestException as e:
            logger.error(f"Grok references request failed on attempt {attempt}: {e}")
            if attempt < max_attempts:
                continue
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok references response on attempt {attempt}: {e}")
            if attempt < max_attempts:
                continue
            return None

        last_data = data
        if _has_any_value(data):
            if attempt > 1:
                logger.info(f"Grok references found on retry attempt {attempt}")
            return data
        # All-null result — retry to compensate for web-search variance
        logger.info(f"Grok references all-null on attempt {attempt}; retrying" if attempt < max_attempts else f"Grok references still all-null after {max_attempts} attempts")

    return last_data


def _extract_responses_text(body: dict) -> str | None:
    """Pull the assistant text out of a /v1/responses payload.

    Shape: {"output": [{"type": "message", "content": [{"type": "output_text", "text": "..."}]}, ...]}
    """
    if not isinstance(body, dict):
        return None
    # New unified shape: top-level `output_text` if the SDK rolled it up
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    output = body.get("output") or []
    chunks = []
    for item in output:
        if not isinstance(item, dict):
            continue
        # message-type items hold the assistant text
        for c in item.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                chunks.append(c["text"])
    return "\n".join(chunks) if chunks else None


def _extract_trailing_json(text: str):
    """Extract the JSON object at the end of a free-form response.

    The prompt has Grok write prose first, then close with a JSON object.
    We find the last "{...}" block and parse it, tolerating optional code fences.
    """
    s = text.strip()
    # Strip a final code fence if present
    if s.endswith("```"):
        # Find the matching opening fence
        start = s.rfind("```", 0, -3)
        if start != -1:
            inner = s[start + 3:-3]
            # Drop a leading "json\n" if Grok wrote ```json
            if inner.startswith("json\n"):
                inner = inner[5:]
            elif inner.startswith("\n"):
                inner = inner[1:]
            try:
                return json.loads(inner.strip())
            except json.JSONDecodeError:
                pass
    # Otherwise find the last balanced { ... } at the end
    if not s.endswith("}"):
        # Trim any trailing prose after the JSON close
        last_close = s.rfind("}")
        if last_close == -1:
            raise json.JSONDecodeError("no closing brace", s, 0)
        s = s[:last_close + 1]
    # Walk backwards to the matching opening brace
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        if s[i] == "}":
            depth += 1
        elif s[i] == "{":
            depth -= 1
            if depth == 0:
                return json.loads(s[i:])
    raise json.JSONDecodeError("unbalanced braces", text, 0)
