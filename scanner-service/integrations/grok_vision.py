"""Grok Vision API integration for wine label recognition and photo triage."""

import base64
import json
import logging
import re
import time
from datetime import datetime
from io import BytesIO

import requests

from integrations.chat_models import _responses_text, _trailing_json

logger = logging.getLogger(__name__)

GROK_MODEL = "grok-4.3"

# Italian and Australian labels are often set in capitals. A cellar entry is not.
NAME_STYLE = (
    "Write name and producer the way a merchant would, in ordinary title case. "
    "Never copy the label's ALL CAPS. The name is the cuvée "
    "(Bin 311 Chardonnay, Contino Reserva), not the grape or appellation alone "
    "unless that is genuinely all that is printed. Producer is a proper name "
    "(Penfolds, Terre da Vino), not PENFOLDS or TERREDAVINO."
)


def normalise_label_case(text: str) -> str:
    """If a field is mostly capitals, title-case it. Leave mixed case alone."""
    if not text or not isinstance(text, str):
        return text
    s = text.strip()
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3:
        return s
    if sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return s
    return s.title()


def polish_extracted_fields(data: dict | None) -> dict | None:
    if not data:
        return data
    for field in ("name", "producer"):
        value = data.get(field)
        if isinstance(value, str):
            data[field] = normalise_label_case(value)
    from integrations.barcodes import accepted_barcode
    digits = accepted_barcode(data.get("barcode"))
    if digits:
        data["barcode"] = digits
    elif "barcode" in data:
        data["barcode"] = None
    return data

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None
    ImageOps = None

try:
    import zxingcpp
    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False
    zxingcpp = None
    logger.info("zxing-cpp not available — barcode reading will try pyzbar")

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False
    pyzbar_decode = None
    logger.info("pyzbar not available — barcode reading will try zxing-cpp")


def _zxing_formats():
    fmt = zxingcpp.BarcodeFormat
    try:
        return (fmt.EAN13, fmt.EAN8, fmt.UPCA, fmt.UPCE, fmt.ITF)
    except Exception:
        return None


def _accept_digits(raw) -> str | None:
    from integrations.barcodes import accepted_barcode
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return accepted_barcode(str(raw).strip())


def _try_zxing(image) -> str | None:
    if not HAS_ZXING:
        return None
    try:
        src = image
        if image.mode not in ("RGB", "L"):
            src = image.convert("RGB")
        kwargs = {"try_rotate": True, "try_downscale": True}
        formats = _zxing_formats()
        if formats:
            kwargs["formats"] = formats
        results = zxingcpp.read_barcodes(src, **kwargs)
    except Exception as e:
        logger.warning("[PHOTO] zxing decode failed: %s", e)
        return None
    for result in results or []:
        digits = _accept_digits(getattr(result, "text", None))
        if digits:
            return digits
    return None


def _try_pyzbar(image) -> str | None:
    if not HAS_PYZBAR:
        return None
    try:
        results = pyzbar_decode(image)
    except Exception as e:
        logger.warning("[PHOTO] pyzbar decode failed: %s", e)
        return None
    for result in results or []:
        digits = _accept_digits(getattr(result, "data", None))
        if digits:
            return digits
    return None


def _barcode_retry_images(image):
    """Grayscale + autocontrast 2x, plus centre and lower-half crops."""
    gray = ImageOps.grayscale(image)
    contrast = ImageOps.autocontrast(gray)
    w, h = contrast.size
    yield contrast.resize((max(1, w * 2), max(1, h * 2)), Image.Resampling.LANCZOS)
    yield contrast.crop((w // 4, h // 4, max(w // 4 + 1, (w * 3) // 4), max(h // 4 + 1, (h * 3) // 4)))
    yield contrast.crop((0, h // 2, w, h))


def read_barcode_local(photo_bytes: bytes) -> str | None:
    """Read an EAN/UPC from photo bytes. zxing-cpp, then pyzbar, then retries."""
    started = time.monotonic()
    engine = "none"
    width = height = 0
    found = None
    try:
        if not HAS_PIL:
            logger.warning("[PHOTO] barcode decode failed: Pillow is not installed")
        else:
            image = Image.open(BytesIO(photo_bytes))
            image.load()
            width, height = image.size
            found = _try_zxing(image)
            if found:
                engine = "zxing"
            else:
                found = _try_pyzbar(image)
                if found:
                    engine = "pyzbar"
                else:
                    for variant in _barcode_retry_images(image):
                        found = _try_zxing(variant)
                        if found:
                            engine = "zxing"
                            break
                        found = _try_pyzbar(variant)
                        if found:
                            engine = "pyzbar"
                            break
    except Exception as e:
        logger.warning("[PHOTO] barcode decode failed: %s", e)
        found = None
        engine = "none"
    ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "[PHOTO] barcode decode engine=%s size=%sx%s ms=%s",
        engine, width, height, ms,
    )
    return found

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


def build_label_prompt(target_currency: str = "USD", target_country: str = "",
                       hint: str = "") -> str:
    location = _location_phrase(target_country)
    extra = f"\n{hint}\n" if hint else ""
    return f"""You are a sommelier and wine label expert. Analyse this wine bottle label photo and extract as much detail as possible.
{extra}

Return a JSON object with these fields (use null for anything you can't determine):

{{
  "name": "Cuvée / wine name in title case (required)",
  "producer": "Winery / producer / domaine in title case",
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
  "market_price_reasoning": null,
  "list_price": null,
  "barcode": "EAN/UPC digits printed on the label, or null"
}}

Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
Category codes: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet. Most wines are DR unless stated otherwise.

For list_price: ONLY a number printed on a shelf sticker, neck tag or price gun label actually visible in the photo. If you cannot see a printed price, this MUST be null. Do not copy market_price into list_price. Do not invent a shop price.
For barcode: the EAN-8/EAN-13/UPC digits printed under the bars, digits only, or null if you cannot read them. Never guess a barcode.

For the description field: even if tasting notes are not printed on the label, use your wine knowledge to write useful tasting notes based on the grape variety, region, and wine style.
For food_pairings: suggest 2-4 food pairings based on your wine knowledge.
For attributes: list notable wine attributes visible on the label or inferable from the wine style (e.g. organic, biodynamic, old vine, reserve, barrel-aged, single vineyard, natural, unfiltered).
For drink_by: estimate based on wine type, vintage, grape, and region. Simple whites/roses: 2-3 years. Quality reds: 5-15 years. Fine wines: 10-30 years.
For size: bottle size in litres (standard=0.75, magnum=1.5, half=0.375).
{NAME_STYLE}
{REFERENCE_INSTRUCTIONS.format(currency=target_currency, location=location)}

Return ONLY the JSON object, no markdown, no explanation."""


def build_triage_prompt(target_currency: str = "USD", target_country: str = "") -> str:
    location = _location_phrase(target_country)
    return f"""Look at this photo and determine what it shows. Respond with a JSON object:

1. If it shows a BARCODE (black and white bars with numbers), return:
   {{"type": "barcode", "barcode": "the numeric EAN/UPC digits"}}

2. If it shows a WINE LABEL or wine bottle, return a full analysis:
   {{"type": "label", "name": "Cuvée in title case", "producer": "Winery in title case", "vintage": 2020, "wine_type": "RE/WH/RO/SP/DE/FO/OR", "category": "DR/SD/MS/SW", "country": "Two-letter ISO code", "region": "Wine region", "appellation": "Official appellation", "grape_variety": "Primary grape", "vineyard": "Estate/vineyard name", "abv": 13.5, "size": 0.75, "drink_by": "YYYY-MM-DD or null", "food_pairings": ["Food 1", "Food 2"], "attributes": ["Attribute 1"], "description": "Tasting notes. 2-3 sentences.", "critic_score": null, "critic_score_confidence": "low", "critic_score_reasoning": null, "vivino_rating": null, "vivino_rating_confidence": "low", "vivino_rating_reasoning": null, "market_price": null, "market_price_currency": "{target_currency}", "market_price_confidence": "low", "market_price_reasoning": null, "list_price": null, "barcode": "EAN/UPC digits or null"}}
   {NAME_STYLE}

   Wine type codes: RE=Red, WH=White, RO=Rose, SP=Sparkling, DE=Dessert, FO=Fortified, OR=Orange.
   Category: DR=Dry, SD=Semi-Dry, MS=Medium Sweet, SW=Sweet.
   For description: use wine knowledge for tasting notes. For food_pairings: 2-4 suggestions. For attributes: notable traits (organic, old vine, reserve, barrel-aged, etc). For drink_by: estimate peak drinking date. For size: litres (0.75 standard).
   For list_price: ONLY a number printed on a shelf sticker or price tag visible in the photo. Null if you cannot see one. Never copy market_price into list_price.
   For barcode: EAN/UPC digits printed on the label, or null. Never guess.
   {REFERENCE_INSTRUCTIONS.format(currency=target_currency, location=location)}
   Use null for fields you can't determine.

3. If it shows MORE THAN ONE WINE — a restaurant list, a shop shelf, a printed
   menu, or several bottles/labels photographed together — return:
   {{"type": "winelist"}}
   Do not try to read the wines here; a separate call handles that. This
   is the multi-wine path, not "it must be a priced wine list".

4. If it's NONE of these, return:
   {{"type": "other"}}

Return ONLY the JSON object, no markdown, no explanation."""


def build_winelist_prompt(cellar_summary: str, target_currency: str = "USD",
                          target_country: str = "") -> str:
    """Read every wine in a multi-wine photo and recommend from it.

    Covers restaurant lists, shop shelves, and several labels in one shot.
    Those are different scenes: only a menu or a shelf tag has an offered
    price. Inventing a "list price" for unlabeled bottles is the failure
    mode this prompt is built to prevent.

    Deliberately no per-wine web search: twenty wines with a live lookup each
    would take minutes, and this gets used while standing in a shop or holding
    a menu. Model knowledge gives serviceable one-liners and ballpark retail
    instantly; the accurate lookup stays reserved for wines actually added.
    """
    location = _location_phrase(target_country)
    cellar = cellar_summary or "(their cellar is empty — no preferences to go on yet)"
    return f"""You are a sommelier looking at a photo of wines for a friend.

Read every wine you can make out. For each:
  note   — one line, about 15–20 words (aromas and style). If there are
           many wines, keep this tighter rather than skipping wines.
  detail — two short sentences: grape/region, and how it drinks / who it's
           for. Omit detail on a very long list if you must; note is required.

First classify the SCENE — this is required:
  "menu"    — a restaurant wine list, printed menu, or screenshot of a list
              to order from
  "shelf"   — a shop shelf / fridge where bottles sit with price tags
  "bottles" — several bottle labels photographed together, with no menu and
              no price tags (a table, a tasting lineup, supermarket labels
              without readable prices). This is NOT a wine list.

Here is what they already drink at home:
{cellar}

Recommend ONE wine from the photo they would most enjoy, and say why in one
sentence that refers to their taste. Then name up to three others worth a look.
Flag any wine they already own.

Prices:
  list_price   — ONLY a number actually printed in the photo next to that
                 wine (menu price or shelf tag), exactly as printed — do not
                 convert it. If you cannot see a price in the photo, this
                 MUST be null. Do not estimate a restaurant markup. Do not
                 invent a list price for unlabeled bottles. Scene "bottles"
                 always has list_price null.
  currency     — ISO code of the prices printed in the photo (a Dutch menu
                 in euros is "EUR" even if the user's currency differs). If
                 no prices are visible, use "{target_currency}".
  retail_price — your ballpark typical shop price in {target_currency}{location},
                 or null if you cannot estimate. This may be estimated from
                 knowledge; list_price may not.

Return ONLY this JSON, no markdown:
{{
  "type": "winelist",
  "scene": "bottles",
  "wines": [
    {{
      "name": "Wine name as printed",
      "producer": "Producer if shown",
      "vintage": 2022,
      "wine_type": "RE/WH/RO/SP/DE/FO/OR",
      "note": "One line, 15–20 words",
      "detail": "Two short sentences.",
      "list_price": null,
      "retail_price": 16,
      "already_owned": false
    }}
  ],
  "recommendation": {{"name": "Exact name from the photo", "why": "One sentence referring to their taste"}},
  "alternates": ["Exact name", "Exact name"],
  "currency": "{target_currency}",
  "retail_currency": "{target_currency}"
}}

If the photo is too blurred or cropped to read any wine, return
{{"type": "winelist", "scene": "bottles", "wines": [], "recommendation": null, "alternates": []}}."""


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


def build_identify_hint_prompt(hint: str, wrong_name: str = "",
                               target_currency: str = "USD",
                               target_country: str = "",
                               context: dict | None = None) -> str:
    """Prompt: the previous read was wrong; look up the wine the user named."""
    location = _location_phrase(target_country)
    context = context or {}
    bits = []
    for key, label in (
        ("barcode", "barcode"),
        ("producer", "producer"),
        ("vintage", "vintage"),
        ("country", "country"),
        ("region", "region"),
        ("appellation", "appellation"),
        ("grape_variety", "grape"),
        ("wine_type", "type"),
    ):
        value = context.get(key)
        if value not in (None, ""):
            bits.append(f"{label} {value}")
    prior = "; ".join(bits)
    prior_line = (
        f"What we (wrongly) had: {prior}.\n" if prior else ""
    )
    return f"""The previous identification was WRONG. Do not reuse it unless the user
confirmed that field.

Wrong reading: {wrong_name or '(unknown)'}
{prior_line}The user says the wine is: {hint}

Look this bottle up — producer, exact cuvée name, vintage. Prefer live web
search (Wine-Searcher, producer site, merchant listings). If the user named a
vintage, that vintage is required.

Return a FULL wine record as JSON (label-card shape), plus a short note:
{{"type": "label", "name": "Wine name", "producer": "Winery", "vintage": 2022,
  "wine_type": "RE/WH/RO/SP/DE/FO/OR", "category": "DR/SD/MS/SW",
  "country": "Two-letter ISO", "region": "Region", "appellation": "Appellation",
  "grape_variety": "Primary grape", "vineyard": "Estate or null",
  "abv": 13.5, "size": 0.75, "description": "Tasting notes, 2-3 sentences.",
  "food_pairings": ["Food 1", "Food 2"], "attributes": ["Attribute"],
  "note": "One line, 15-20 words", "detail": "Two short sentences.",
  "list_price": null, "retail_price": 16,
  "market_price": null, "market_price_currency": "{target_currency}"}}

Wine types: RE=Red WH=White RO=Rose SP=Sparkling DE=Dessert FO=Fortified OR=Orange.
Prices in {target_currency}{location}. list_price must be null.
Return ONLY the JSON object."""


CURRENCY_SYMBOL_CODES = {"\u00a3": "GBP", "$": "USD", "\u20ac": "EUR", "R": "ZAR",
                         "A$": "AUD", "HK$": "HKD"}
CURRENCY_WORD_CODES = {"EURO": "EUR", "EUROS": "EUR", "POUND": "GBP",
                       "POUNDS": "GBP", "RAND": "ZAR", "DOLLAR": "USD",
                       "DOLLARS": "USD"}


def currency_code(value) -> str | None:
    """ISO code for a currency the model wrote as a code, symbol or word.

    Truncating to three characters turned "\u00a3" into "\u00a3" and "pounds"
    into "POU" — neither of which the cellar can convert.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw in CURRENCY_SYMBOL_CODES:
        return CURRENCY_SYMBOL_CODES[raw]
    word = raw.upper()
    if word in CURRENCY_WORD_CODES:
        return CURRENCY_WORD_CODES[word]
    if len(word) == 3 and word.isalpha():
        return word
    return None


class GrokVisionAPI:
    """Extracts wine data from label photos using Grok's vision model."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.x.ai/v1"

    def analyse_label(self, photo_bytes: bytes, target_currency: str = "USD", target_country: str = "",
                      hint: str = "") -> dict | None:
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
                            "text": build_label_prompt(target_currency, target_country, hint=hint),
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
            data = polish_extracted_fields(data)
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
                from integrations.barcodes import accepted_barcode
                digits = accepted_barcode(data.get("barcode"))
                if digits:
                    data["barcode"] = digits
                    return data
                logger.warning(f"Grok barcode result not valid: {data.get('barcode')!r}")
                return {"type": "other"}

            if photo_type == "label":
                data = polish_extracted_fields(data)
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
            scene = data.get("scene") or "menu"
            logger.info(
                f"Grok read {len(data.get('wines') or [])} wines "
                f"(scene={scene})"
            )
            return data
        except requests.RequestException as e:
            logger.error(f"Grok wine-list request failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse Grok wine-list response: {e}")
            return None

    def identify_wine_from_hint(self, hint: str, wrong_name: str = "",
                                target_currency: str = "USD",
                                target_country: str = "",
                                context: dict | None = None) -> dict | None:
        """Look up one wine from a name the user typed after a misread."""
        prompt = build_identify_hint_prompt(
            hint, wrong_name, target_currency, target_country, context,
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # web_search is accepted on /responses only; /chat/completions returns 422.
        payload = {
            "model": GROK_MODEL,
            "input": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": 1024,
            "temperature": 0,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
                timeout=90,
            )
            resp.raise_for_status()
            data = polish_extracted_fields(_trailing_json(_responses_text(resp.json())))
            logger.info(f"Grok re-identified from hint: {data.get('name', '?')}")
            return data
        except requests.RequestException as e:
            logger.error(f"Grok identify-from-hint failed: {e}")
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": GROK_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1024,
                        "temperature": 0.0,
                    },
                    timeout=90,
                )
                resp.raise_for_status()
                content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                return polish_extracted_fields(json.loads(content))
            except Exception as retry_err:
                logger.error(f"Grok identify-from-hint retry failed: {retry_err}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError) as e:
            logger.error(f"Failed to parse Grok identify-from-hint response: {e}")
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
        "name": normalise_label_case(pick("name", grok, off) or "Unknown Wine"),
        "producer": normalise_label_case(pick("producer", grok, off) or ""),
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
            code = currency_code(grok.get("market_price_currency"))
            if code:
                merged["market_price_currency"] = code
            persisted_confidences.append(grok.get("market_price_confidence", "").lower())
        except (ValueError, TypeError):
            pass

    try:
        tag = grok.get("list_price")
        if tag is not None and not isinstance(tag, bool):
            merged["list_price"] = float(tag)
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

    # Drink-by date — only ISO, or WineCreateSerializer 400s on add-to-cellar
    drink_by = pick("drink_by", grok)
    if drink_by:
        raw = str(drink_by).strip()
        try:
            datetime.strptime(raw[:10], "%Y-%m-%d")
            merged["drink_by"] = raw[:10]
        except ValueError:
            pass

    # List fields
    food_pairings = pick("food_pairings", grok)
    if food_pairings and isinstance(food_pairings, list):
        merged["food_pairings"] = food_pairings

    attributes = pick("attributes", grok)
    if attributes and isinstance(attributes, list):
        merged["attributes"] = attributes

    return merged
