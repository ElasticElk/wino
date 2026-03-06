import requests


class OpenFoodFactsAPI:
    BASE_URL = "https://world.openfoodfacts.org/api/v2/product"

    def lookup(self, barcode: str) -> dict | None:
        """Look up a barcode in Open Food Facts. Returns extracted wine data or None."""
        try:
            resp = requests.get(
                f"{self.BASE_URL}/{barcode}.json",
                timeout=10,
                headers={"User-Agent": "WineFridgeScanner/1.0"},
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            if data.get("status") != 1:
                return None

            product = data.get("product", {})
            return self._extract_wine_data(product, barcode)
        except (requests.RequestException, ValueError):
            return None

    def _extract_wine_data(self, product: dict, barcode: str) -> dict:
        """Extract wine-relevant fields from an OFF product."""
        return {
            "name": product.get("product_name", ""),
            "producer": product.get("brands", ""),
            "country": self._extract_country(product),
            "region": product.get("origins", "") or product.get("manufacturing_places", ""),
            "abv": self._extract_abv(product),
            "image_url": product.get("image_front_url", ""),
            "barcode": barcode,
        }

    # OFF country tag → ISO 2-letter code (common wine-producing countries)
    _COUNTRY_MAP = {
        "france": "FR", "italy": "IT", "spain": "ES", "germany": "DE",
        "portugal": "PT", "switzerland": "CH", "austria": "AT",
        "south-africa": "ZA", "argentina": "AR", "chile": "CL",
        "australia": "AU", "new-zealand": "NZ", "united-states": "US",
        "greece": "GR", "hungary": "HU", "romania": "RO", "georgia": "GE",
        "lebanon": "LB", "israel": "IL", "uruguay": "UY", "brazil": "BR",
        "canada": "CA", "china": "CN", "united-kingdom": "GB",
        "croatia": "HR", "slovenia": "SI", "moldova": "MD",
    }

    def _extract_country(self, product: dict) -> str:
        """Try to get a 2-letter ISO country code from OFF tags."""
        countries_tags = product.get("countries_tags", [])
        for tag in countries_tags:
            # Tags are like "en:france" or "en:south-africa"
            if ":" in tag:
                name = tag.split(":")[1]
                code = self._COUNTRY_MAP.get(name)
                if code:
                    return code
        return ""

    def _extract_abv(self, product: dict) -> float | None:
        """Extract alcohol percentage."""
        abv = product.get("nutriments", {}).get("alcohol_100g")
        if abv:
            try:
                return float(abv)
            except (ValueError, TypeError):
                pass
        return None
