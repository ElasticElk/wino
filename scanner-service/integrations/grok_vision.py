"""Grok Vision API integration for wine label recognition and photo triage."""

import base64
import json
import logging
import re
from io import BytesIO

import requests

logger = logging.getLogger(__name__)

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

LABEL_PROMPT = """You are a sommelier and wine label expert. Analyse this wine bottle label photo and extract as much detail as possible.

Return a JSON object with these fields (use null for anything you can't determine):

{
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
  "price": null,
  "rating": null,
  "drink_by": "YYYY-MM-DD or null — estimated peak drinking window end date based on wine style",
  "food_pairings": ["Grilled lamb", "Hard cheese", "Dark chocolate"],
  "attributes": ["Old vine", "Organic", "Barrel-aged", "Single vineyard"],
  "description": "Tasting notes — describe the expected flavour profile, aromas, body, and character of this wine based on the grape, region, and style. 2-3 sentences."
}

Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
Category codes: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet. Most wines are DR unless stated otherwise.

For the description field: even if tasting notes are not printed on the label, use your wine knowledge to write useful tasting notes based on the grape variety, region, and wine style.
For food_pairings: suggest 2-4 food pairings based on your wine knowledge.
For attributes: list notable wine attributes visible on the label or inferable from the wine style (e.g. organic, biodynamic, old vine, reserve, barrel-aged, single vineyard, natural, unfiltered).
For drink_by: estimate based on wine type, vintage, grape, and region. Simple whites/roses: 2-3 years. Quality reds: 5-15 years. Fine wines: 10-30 years.
For size: bottle size in litres (standard=0.75, magnum=1.5, half=0.375).

Return ONLY the JSON object, no markdown, no explanation."""


TRIAGE_PROMPT = """Look at this photo and determine what it shows. Respond with a JSON object:

1. If it shows a BARCODE (black and white bars with numbers), return:
   {"type": "barcode", "barcode": "the numeric EAN/UPC digits"}

2. If it shows a WINE LABEL or wine bottle, return a full analysis:
   {"type": "label", "name": "Wine name", "producer": "Winery name", "vintage": 2020, "wine_type": "RE/WH/RO/SP/DE/FO/OR", "category": "DR/SD/MS/SW", "country": "Two-letter ISO code", "region": "Wine region", "appellation": "Official appellation", "grape_variety": "Primary grape", "vineyard": "Estate/vineyard name", "abv": 13.5, "size": 0.75, "price": null, "rating": null, "drink_by": "YYYY-MM-DD or null", "food_pairings": ["Food 1", "Food 2"], "attributes": ["Attribute 1"], "description": "Tasting notes. 2-3 sentences."}

   Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
   Category: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet.
   For description: use wine knowledge for tasting notes. For food_pairings: 2-4 suggestions. For attributes: notable traits (organic, old vine, reserve, barrel-aged, etc). For drink_by: estimate peak drinking date. For size: litres (0.75 standard).
   Use null for fields you can't determine.

3. If it's NEITHER a barcode nor a wine label, return:
   {"type": "other"}

Return ONLY the JSON object, no markdown, no explanation."""


class GrokVisionAPI:
    """Extracts wine data from label photos using Grok's vision model."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.x.ai/v1"

    def analyse_label(self, photo_bytes: bytes) -> dict | None:
        """Send a label photo to Grok vision and extract wine data.

        Returns a dict of wine fields, or None on failure.
        """
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")

        payload = {
            "model": "grok-4-1-fast-non-reasoning",
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
                            "text": LABEL_PROMPT,
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
            "temperature": 0.1,
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


    def triage_photo(self, photo_bytes: bytes) -> dict | None:
        """Classify a photo and extract data in one call.

        Returns a dict with "type" key:
        - {"type": "barcode", "barcode": "1234567890"}
        - {"type": "label", "name": ..., "producer": ..., ...}  (full wine analysis)
        - {"type": "other"}
        - None on API failure
        """
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")

        payload = {
            "model": "grok-4-1-fast-non-reasoning",
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
                            "text": TRIAGE_PROMPT,
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
            "temperature": 0.1,
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


VALID_WINE_TYPES = {"RE", "WH", "RO", "SP", "DE", "FO", "OR"}
VALID_CATEGORIES = {"DR", "SD", "MS", "SW"}


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

    # Price and rating: only if Grok found them on the label (rare)
    price = pick("price", grok)
    if price:
        try:
            merged["price"] = float(price)
        except (ValueError, TypeError):
            pass

    rating = pick("rating", grok)
    if rating:
        try:
            merged["rating"] = int(rating)
        except (ValueError, TypeError):
            pass

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
