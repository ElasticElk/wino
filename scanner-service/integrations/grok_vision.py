"""Grok Vision API integration for wine label recognition."""

import base64
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

LABEL_PROMPT = """You are a sommelier and wine label expert. Analyse this wine bottle label photo and extract as much detail as possible.

Return a JSON object with these fields (use null for anything you can't determine):

{
  "name": "The wine name as it appears on the label (required)",
  "producer": "Winery / producer / domaine name",
  "vintage": 2020,
  "wine_type": "RE/WH/RO/SP/DE/FO/OR",
  "country": "Two-letter ISO country code (e.g. CH, FR, IT, ZA, AR, US)",
  "region": "Wine region (e.g. Bordeaux, Stellenbosch, Valais, Mendoza)",
  "grape_variety": "Primary grape variety (e.g. Merlot, Chardonnay, Pinot Noir)",
  "abv": 13.5,
  "price": null,
  "rating": null,
  "description": "Tasting notes — describe the expected flavour profile, aromas, body, and character of this wine based on the grape, region, and style. 1-2 sentences max."
}

Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.

For the description field: even if tasting notes are not printed on the label, use your wine knowledge to write brief but useful tasting notes based on the grape variety, region, and wine style.

Return ONLY the JSON object, no markdown, no explanation."""


BARCODE_PROMPT = """Look at this photo of a barcode on a product. Read the barcode number (EAN/UPC) printed below or above the barcode bars.

Return ONLY the numeric barcode string (e.g. "7798039590229"). No explanation, no formatting, just the digits."""


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
            "model": "grok-2-vision-latest",
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


    def read_barcode(self, photo_bytes: bytes) -> str | None:
        """Read a barcode number from a photo. Returns the digits or None."""
        b64_image = base64.b64encode(photo_bytes).decode("utf-8")

        payload = {
            "model": "grok-2-vision-latest",
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
                            "text": BARCODE_PROMPT,
                        },
                    ],
                }
            ],
            "max_tokens": 64,
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

            content = result["choices"][0]["message"]["content"].strip()
            # Extract just the digits
            digits = re.sub(r"\D", "", content)
            if 8 <= len(digits) <= 14:
                logger.info(f"Grok read barcode: {digits}")
                return digits
            logger.warning(f"Grok barcode result not valid: {content!r}")
            return None

        except requests.RequestException as e:
            logger.error(f"Grok barcode read failed: {e}")
            return None


VALID_WINE_TYPES = {"RE", "WH", "RO", "SP", "DE", "FO", "OR"}


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
    }

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

    return merged
