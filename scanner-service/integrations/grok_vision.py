"""Grok Vision API integration for wine label recognition and photo triage."""

import base64
import json
import logging
import re
from io import BytesIO

import requests

logger = logging.getLogger(__name__)

GROK_MODEL = "grok-4.3"

# pyzbar is optional — used for fast local barcode reading before falling back to Grok
try:
    from PIL import Image
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False
    logger.info("pyzbar not available — barcode reading will use Grok only")


def read_barcode_local(photo_bytes: bytes) -> str | None:
    """Try to read a barcode from photo bytes using pyzbar. Returns digits or None."""
    if not HAS_PYZBAR:
        return None
    try:
        image = Image.open(BytesIO(photo_bytes))
        results = pyzbar_decode(image)
        for result in results:
            digits = result.data.decode("utf-8").strip()
            if digits.isdigit() and 8 <= len(digits) <= 14:
                logger.info(f"pyzbar read barcode: {digits}")
                return digits
    except Exception as e:
        logger.warning(f"pyzbar failed: {e}")
    return None

REFERENCE_INSTRUCTIONS = """REFERENCE FIELDS — fill each INDEPENDENTLY. You may know one and not the others; that is fine.

For each of critic_score / vivino_rating / market_price, return three values:
  <field>             — the value, or null
  <field>_confidence  — "high", "medium", or "low"
  <field>_reasoning   — one short sentence justifying the value (REQUIRED whenever the value is not null)

Confidence rubric (apply per field, independently):
  high   — you can name a specific source (e.g. "Wine Spectator 92pts for the 2019", "Vivino avg 4.2 with ~3000 ratings", "Wine-Searcher avg £45 across major UK retailers")
  medium — you don't have an exact source but have strong contextual knowledge (producer, region, vintage quality, comparable bottlings) to give an educated estimate
  low    — you cannot anchor an estimate at all; in this case set the value to null

Examples of GOOD reasoning:
  "Wine Spectator 91pts for the 2018 vintage; James Suckling 92pts"
  "Producer's flagship cuvée in Stellenbosch; comparable Syrahs from same maker fetch ~R450 retail"
  "Vivino community averages 4.1 across most vintages of this label"

Examples of BAD reasoning (do NOT use these — return null instead):
  "based on the grape variety"
  "typical for this style"
  "estimated from general wine knowledge"
  "Syrah from a warm climate usually scores around 90"

For market_price_currency: the ISO code you used for market_price (normally {currency}).

ABSOLUTE RULES:
  - Never invent specific critic names or scores you don't actually know.
  - If your reasoning would be generic (any of the BAD examples above), the value MUST be null.
  - Each field is independent: returning null for critic_score does NOT mean the others must also be null."""


def _location_phrase(country: str) -> str:
    """Render an 'in {country}' clause from an ISO 2-letter code, or empty if unknown."""
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


def build_label_prompt(target_currency: str = "USD", target_country: str = "") -> str:
    location = _location_phrase(target_country)
    return f"""You are a sommelier and wine label expert. Analyse this wine bottle label photo and extract as much detail as possible.

Return a JSON object with these fields (use null for anything you can't determine):

{{
  "name": "The wine name as it appears on the label (required)",
  "producer": "Winery / producer / domaine name",
  "vintage": 2020,
  "wine_type": "RE/WH/RO/SP/DE/FO/OR",
  "category": "DR/SD/MS/SW",
  "country": "Two-letter ISO country code (e.g. CH, FR, IT, ZA, AR, US)",
  "region": "Wine region (e.g. Bordeaux, Stellenbosch, Valais, Mendoza)",
  "appellation": "Official appellation if known (e.g. AOC Margaux, DOC Barolo, WO Stellenbosch)",
  "grape_variety": "Primary grape variety (e.g. Merlot, Chardonnay, Pinot Noir)",
  "vineyard": "Vineyard / estate name if different from producer",
  "abv": 13.5,
  "size": 0.75,
  "drink_by": "YYYY-MM-DD or null — estimated peak drinking window end date based on wine style",
  "food_pairings": ["Grilled lamb", "Hard cheese", "Dark chocolate"],
  "attributes": ["Old vine", "Organic", "Barrel-aged", "Single vineyard"],
  "description": "Tasting notes — describe the expected flavour profile, aromas, body, and character of this wine based on the grape, region, and style. 2-3 sentences.",
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
}}

Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
Category codes: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet. Most wines are DR unless stated otherwise.

For the description field: even if tasting notes are not printed on the label, use your wine knowledge to write useful tasting notes based on the grape variety, region, and wine style.
For food_pairings: suggest 2-4 food pairings based on your wine knowledge.
For attributes: list notable wine attributes visible on the label or inferable from the wine style (e.g. organic, biodynamic, old vine, reserve, barrel-aged, single vineyard, natural, unfiltered).
For drink_by: estimate based on wine type, vintage, grape, and region. Simple whites/roses: 2-3 years. Quality reds: 5-15 years. Fine wines: 10-30 years.
For size: bottle size in litres (standard=0.75, magnum=1.5, half=0.375).
{REFERENCE_INSTRUCTIONS.format(currency=target_currency, location=location)}

Return ONLY the JSON object, no markdown, no explanation."""


def build_triage_prompt(target_currency: str = "USD", target_country: str = "") -> str:
    location = _location_phrase(target_country)
    return f"""Look at this photo and determine what it shows. Respond with a JSON object:

1. If it shows a BARCODE (black and white bars with numbers), return:
   {{"type": "barcode", "barcode": "the numeric EAN/UPC digits"}}

2. If it shows a WINE LABEL or wine bottle, return a full analysis:
   {{"type": "label", "name": "Wine name", "producer": "Winery name", "vintage": 2020, "wine_type": "RE/WH/RO/SP/DE/FO/OR", "category": "DR/SD/MS/SW", "country": "Two-letter ISO code", "region": "Wine region", "appellation": "Official appellation", "grape_variety": "Primary grape", "vineyard": "Estate/vineyard name", "abv": 13.5, "size": 0.75, "drink_by": "YYYY-MM-DD or null", "food_pairings": ["Food 1", "Food 2"], "attributes": ["Attribute 1"], "description": "Tasting notes. 2-3 sentences.", "critic_score": null, "critic_score_confidence": "low", "critic_score_reasoning": null, "vivino_rating": null, "vivino_rating_confidence": "low", "vivino_rating_reasoning": null, "market_price": null, "market_price_currency": "{target_currency}", "market_price_confidence": "low", "market_price_reasoning": null}}

   Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
   Category: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet.
   For description: use wine knowledge for tasting notes. For food_pairings: 2-4 suggestions. For attributes: notable traits (organic, old vine, reserve, barrel-aged, etc). For drink_by: estimate peak drinking date. For size: litres (0.75 standard).
   {REFERENCE_INSTRUCTIONS.format(currency=target_currency, location=location)}
   Use null for fields you can't determine.

3. If it shows a WINE LIST — a restaurant list, a shelf display, a printed
   menu, several bottles photographed together — return:
   {{"type": "winelist"}}
   Do not try to read the wines here; a separate call handles that.

4. If it's NONE of these, return:
   {{"type": "other"}}

Return ONLY the JSON object, no markdown, no explanation."""


def build_winelist_prompt(cellar_summary: str, target_currency: str = "USD",
                          target_country: str = "") -> str:
    """Read a wine list and recommend from it, in one call.

    Deliberately no per-wine web search: twenty wines with a live lookup each
    would take minutes, and this gets used while standing in a shop or holding
    a menu. Model knowledge gives serviceable one-liners and ballpark prices
    instantly; the accurate lookup stays reserved for wines actually added.
    """
    location = _location_phrase(target_country)
    cellar = cellar_summary or "(their cellar is empty — no preferences to go on yet)"
    return f"""You are a sommelier reading a wine list for a friend.

Read every wine you can make out in this photo. For each, give a ONE LINE
tasting note (aromas and style, no more than about 12 words) and your best
estimate of what it costs.

Here is what they already drink at home:
{cellar}

Recommend ONE wine from the list they would most enjoy, and say why in one
sentence that refers to their taste. Then name up to three others worth a look.
Flag any wine on the list they already own — they can drink that at home.

Prices in {target_currency}, for a typical venue{location}. Give both the price
you would expect to see charged on this list, and the typical retail price, when
you can tell them apart. Use null when you genuinely cannot estimate.

Return ONLY this JSON, no markdown:
{{
  "type": "winelist",
  "wines": [
    {{
      "name": "Wine name as printed",
      "producer": "Producer if shown",
      "vintage": 2022,
      "wine_type": "RE/WH/RO/SP/DE/FO/OR",
      "note": "One line, twelve words or fewer",
      "list_price": 38,
      "retail_price": 16,
      "already_owned": false
    }}
  ],
  "recommendation": {{"name": "Exact name from the list", "why": "One sentence referring to their taste"}},
  "alternates": ["Exact name", "Exact name"],
  "currency": "{target_currency}"
}}

If the photo is too blurred or cropped to read any wine, return
{{"type": "winelist", "wines": [], "recommendation": null, "alternates": []}}."""


def build_text_reference_prompt(name: str, producer: str, vintage, target_currency: str, target_country: str = "") -> str:
    """Prompt for refreshing reference data on a known wine — no image."""
    vintage_str = f" {vintage}" if vintage else ""
    producer_str = f" by {producer}" if producer else ""
    location = _location_phrase(target_country)
    return f"""You are a sommelier. For the following wine, return a JSON object with reference data:

Wine: {name}{producer_str}{vintage_str}

Return ONLY this JSON, no markdown:
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
}}

{REFERENCE_INSTRUCTIONS.format(currency=target_currency, location=location)}"""


class GrokVisionAPI:
    """Extracts wine data from label photos using Grok's vision model."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.x.ai/v1"

    def analyse_label(self, photo_bytes: bytes, target_currency: str = "USD", target_country: str = "") -> dict | None:
        """Send a label photo to Grok vision and extract wine data.

        Returns a dict of wine fields, or None on failure.
        """
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")

        payload = {
            "model": GROK_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                            },
                        },
                        {
                            "type": "text",
                            "text": build_label_prompt(target_currency, target_country),
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
            "temperature": 0.0,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"]
            # Strip markdown fences if Grok wraps the JSON
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0]
                content = content.strip()

            data = json.loads(content)
            logger.info(f"Grok extracted: {data.get('name', 'Unknown')}")
            return data

        except requests.RequestException as e:
            logger.error(f"Grok API request failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok response: {e}")
            return None


    def triage_photo(self, photo_bytes: bytes, target_currency: str = "USD", target_country: str = "") -> dict | None:
        """Classify a photo and extract data in one call.

        Returns a dict with "type" key:
        - {"type": "barcode", "barcode": "1234567890"}
        - {"type": "label", "name": ..., "producer": ..., ...}  (full wine analysis)
        - {"type": "other"}
        - None on API failure
        """
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")

        payload = {
            "model": GROK_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                            },
                        },
                        {
                            "type": "text",
                            "text": build_triage_prompt(target_currency, target_country),
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
            "temperature": 0.0,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0]
                content = content.strip()

            data = json.loads(content)
            photo_type = data.get("type", "other")
            logger.info(f"Grok triage: type={photo_type}")

            if photo_type == "barcode":
                digits = re.sub(r"\D", "", data.get("barcode", ""))
                if 8 <= len(digits) <= 14:
                    data["barcode"] = digits
                    return data
                logger.warning(f"Grok barcode result not valid: {data.get('barcode')!r}")
                return {"type": "other"}

            return data

        except requests.RequestException as e:
            logger.error(f"Grok triage failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok triage response: {e}")
            return None


    def analyse_wine_list(self, photo_bytes: bytes, cellar_summary: str = "",
                          target_currency: str = "USD",
                          target_country: str = "") -> dict | None:
        """Read a wine list photo and recommend from it against their cellar."""
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")
        payload = {
            "model": GROK_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    {"type": "text",
                     "text": build_winelist_prompt(
                         cellar_summary, target_currency, target_country,
                     )},
                ],
            }],
            # A long list needs room; a bottle label does not.
            "max_tokens": 4096,
            "temperature": 0.0,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=90,  # reading 20 wines takes longer than one label
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(content)
            logger.info(f"Grok read {len(data.get('wines') or [])} wines from a list")
            return data
        except requests.RequestException as e:
            logger.error(f"Grok wine-list request failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok wine-list response: {e}")
            return None

    def fetch_references(
        self,
        name: str,
        producer: str = "",
        vintage=None,
        target_currency: str = "USD",
        target_country: str = "",
    ) -> dict | None:
        """Text-only call: fetch critic_score, vivino_rating, market_price for a known wine.

        Returns dict with keys critic_score, vivino_rating, market_price,
        market_price_currency, reference_confidence; or None on failure.
        """
        prompt = build_text_reference_prompt(name, producer, vintage, target_currency, target_country)
        payload = {
            "model": GROK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.0,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(content)
            logger.info(f"Grok references for {name}: confidence={data.get('reference_confidence')}")
            return data
        except requests.RequestException as e:
            logger.error(f"Grok references request failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok references response: {e}")
            return None


VALID_WINE_TYPES = {"RE", "WH", "RO", "SP", "DE", "FO", "OR"}
VALID_CATEGORIES = {"DR", "SD", "MS", "SW"}

GENERIC_REASONING_MARKERS = (
    "based on the grape", "based on grape",
    "typical for this style", "typical for the style",
    "general wine knowledge", "general knowledge",
    "estimated from general",
    "typically score", "usually scores",
    "i don't have specific", "no specific knowledge",
    "without specific",
)


def _is_valid_reasoning(text) -> bool:
    """True iff reasoning is non-empty, long enough, and not generic boilerplate."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 15:
        return False
    lowered = t.lower()
    return not any(marker in lowered for marker in GENERIC_REASONING_MARKERS)


def _validate_ref(value, confidence, reasoning) -> bool:
    """Per-field guard: persist only when value is set, confidence >= medium, reasoning passes."""
    if value is None:
        return False
    if (confidence or "").lower() not in ("medium", "high"):
        return False
    return _is_valid_reasoning(reasoning)


def merge_wine_data(off_data: dict | None, grok_data: dict | None) -> dict:
    """Merge Open Food Facts and Grok vision data, preferring Grok for most fields.

    Returns a unified dict ready for wine creation.
    """
    off = off_data or {}
    grok = grok_data or {}

    def pick(field, *sources):
        """Return the first truthy value from sources."""
        for src in sources:
            val = src.get(field)
            if val:
                return val
        return None

    # Grok is generally more accurate for wine-specific fields
    merged = {
        "name": pick("name", grok, off) or "Unknown Wine",
        "producer": pick("producer", grok, off) or "",
        "wine_type": pick("wine_type", grok) if pick("wine_type", grok) in VALID_WINE_TYPES else "RE",
        "country": (pick("country", grok, off) or "CH")[:2],
        "region": pick("region", grok, off) or "",
        "abv": pick("abv", grok, off),
        "description": pick("description", grok) or "",
        "grape_variety": pick("grape_variety", grok) or "",
        "appellation": pick("appellation", grok) or "",
        "vineyard": pick("vineyard", grok) or "",
    }

    # Category
    category = pick("category", grok)
    if category and category in VALID_CATEGORIES:
        merged["category"] = category

    # Vintage: Grok extracts from the label, OFF rarely has it
    vintage = pick("vintage", grok)
    if vintage:
        try:
            merged["vintage"] = int(vintage)
        except (ValueError, TypeError):
            pass

    # Per-field reference data: each field is validated and persisted independently.
    # critic_score / vivino_rating / market_price each have their own _confidence
    # and _reasoning. Aggregate confidence = highest of any persisted field.
    rank = {"high": 3, "medium": 2, "low": 1}
    persisted_confidences = []

    if _validate_ref(
        grok.get("critic_score"),
        grok.get("critic_score_confidence"),
        grok.get("critic_score_reasoning"),
    ):
        try:
            score = int(grok["critic_score"])
            if 0 <= score <= 100:
                merged["critic_score"] = score
                persisted_confidences.append(grok.get("critic_score_confidence", "").lower())
        except (ValueError, TypeError):
            pass

    if _validate_ref(
        grok.get("vivino_rating"),
        grok.get("vivino_rating_confidence"),
        grok.get("vivino_rating_reasoning"),
    ):
        try:
            rating = float(grok["vivino_rating"])
            if 0.0 <= rating <= 5.0:
                merged["vivino_rating"] = round(rating, 1)
                persisted_confidences.append(grok.get("vivino_rating_confidence", "").lower())
        except (ValueError, TypeError):
            pass

    if _validate_ref(
        grok.get("market_price"),
        grok.get("market_price_confidence"),
        grok.get("market_price_reasoning"),
    ):
        try:
            merged["market_price"] = float(grok["market_price"])
            currency_field = grok.get("market_price_currency")
            if isinstance(currency_field, str):
                merged["market_price_currency"] = currency_field.upper()[:3]
            persisted_confidences.append(grok.get("market_price_confidence", "").lower())
        except (ValueError, TypeError):
            pass

    if persisted_confidences:
        merged["reference_confidence"] = max(persisted_confidences, key=lambda c: rank.get(c, 0))

    # Size in litres
    size = pick("size", grok)
    if size:
        try:
            merged["size"] = float(size)
        except (ValueError, TypeError):
            pass

    # Drink-by date
    drink_by = pick("drink_by", grok)
    if drink_by:
        merged["drink_by"] = drink_by

    # List fields
    food_pairings = pick("food_pairings", grok)
    if food_pairings and isinstance(food_pairings, list):
        merged["food_pairings"] = food_pairings

    attributes = pick("attributes", grok)
    if attributes and isinstance(attributes, list):
        merged["attributes"] = attributes

    return merged
