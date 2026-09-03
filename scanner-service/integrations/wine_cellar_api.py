import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class WineCellarAPI:
    def __init__(self, base_url: str, api_token: str, default_storage: str = "Wine Fridge",
                 web_url: str = "", household_id: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.web_url = (web_url or base_url).rstrip("/")
        self.default_storage = default_storage
        self.household_id = household_id
        self.session = requests.Session()
        headers = {
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        }
        if household_id is not None:
            headers["X-Household"] = str(household_id)
        self.session.headers.update(headers)
        # Retry on connection errors, not on 4xx/5xx
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def lookup_barcode(self, barcode: str) -> list[dict]:
        resp = self.session.get(
            f"{self.base_url}/api/wines/by-barcode/{barcode}/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["wines"]

    def add_stock(self, wine_id: int, price: float | None = None,
                  storage_name: str | None = None, row: int | None = None) -> dict:
        data = {"wine_id": wine_id, "storage_name": storage_name or self.default_storage}
        if price is not None:
            data["price"] = price
        if row is not None:
            data["row"] = row
        resp = self.session.post(
            f"{self.base_url}/api/stock/add/", json=data, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def set_stock(self, wine_id: int, quantity: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/set/",
            json={"wine_id": wine_id, "quantity": quantity},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def remove_stock(self, wine_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/remove/", json={"wine_id": wine_id}, timeout=10,
        )
        if resp.status_code == 409:
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def create_wine(self, wine_data: dict) -> dict:
        logger.info(f"[API] create_wine payload: {wine_data}")
        resp = self.session.post(
            f"{self.base_url}/api/wines/", json=wine_data, timeout=10,
        )
        if resp.status_code >= 400:
            logger.error(f"[API] create_wine failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def wines_in_stock(self, storage_name: str = "") -> list[dict]:
        params = {}
        if storage_name:
            params["storage"] = storage_name
        resp = self.session.get(
            f"{self.base_url}/api/wines/in-stock/", params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["wines"]

    def get_wine(self, wine_id: int) -> dict | None:
        resp = self.session.get(
            f"{self.base_url}/api/wines/{wine_id}/stock/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_wine_detail(self, wine_id: int) -> dict | None:
        """Get full wine details, including storage locations."""
        wines = self.list_all_wines()
        wine = next((w for w in wines if w["id"] == wine_id), None)
        if not wine:
            return None
        # Merge in location items from in-stock endpoint
        try:
            in_stock = self.wines_in_stock()
            stock_entry = next((w for w in in_stock if w["id"] == wine_id), None)
            if stock_entry:
                wine["items"] = stock_entry.get("items", [])
                wine["producer"] = stock_entry.get("producer", "")
        except Exception:
            pass
        return wine

    def refresh_references(self, wine_id: int) -> dict:
        """Re-ask Grok for critic_score / vivino_rating / market_price for this wine.

        Returns dict like {"updated": bool, "confidence": str, ...}
        """
        resp = self.session.post(
            f"{self.base_url}/api/wines/{wine_id}/refresh-references/",
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def search_wines(self, query: str) -> list[dict]:
        """Search wines by name within this household."""
        resp = self.session.get(
            f"{self.base_url}/api/wines/search/", params={"q": query}, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["wines"]

    def list_all_wines(self) -> list[dict]:
        resp = self.session.get(
            f"{self.base_url}/api/wines/all/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["wines"]

    def list_storages(self) -> list[dict]:
        resp = self.session.get(
            f"{self.base_url}/api/storages/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["storages"]

    def move_item(self, item_id: int, storage_name: str | None = None,
                  row: int | None = None) -> dict:
        data = {}
        if storage_name is not None:
            data["storage_name"] = storage_name
        if row is not None:
            data["row"] = row
        resp = self.session.patch(
            f"{self.base_url}/api/stock/{item_id}/move/", json=data, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def upload_wine_image(self, wine_id: int, photo_bytes: bytes,
                          image_type: str = "FR") -> dict:
        logger.info(f"[API] upload_wine_image({wine_id}) size={len(photo_bytes)} bytes, type={image_type}")
        # Multipart upload — must override Content-Type (let requests set boundary)
        resp = self.session.post(
            f"{self.base_url}/api/wines/{wine_id}/images/",
            files={"image": ("label.jpg", photo_bytes, "image/jpeg")},
            data={"image_type": image_type},
            headers={"Content-Type": None},
            timeout=15,
        )
        if resp.status_code >= 400:
            logger.error(f"[API] upload_wine_image failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        logger.info(f"[API] upload_wine_image success: {resp.json()}")
        return resp.json()

    def get_wine_images(self, wine_id: int) -> list[dict]:
        resp = self.session.get(
            f"{self.base_url}/api/wines/{wine_id}/images/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("images", [])

    def get_image_file(self, image_id: int) -> bytes:
        """Download the actual image bytes for a WineImage."""
        resp = self.session.get(
            f"{self.base_url}/api/images/{image_id}/file/", timeout=10,
        )
        resp.raise_for_status()
        return resp.content

    def update_wine(self, wine_id: int, wine_data: dict) -> dict:
        logger.info(f"[API] update_wine({wine_id}) payload: {wine_data}")
        resp = self.session.patch(
            f"{self.base_url}/api/wines/{wine_id}/", json=wine_data, timeout=10,
        )
        if resp.status_code >= 400:
            logger.error(f"[API] update_wine failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        return resp.json()

    # --- Household management ---

    def get_household(self) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/scanner/household/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def add_household_member(self, chat_id: str, display_name: str,
                             household_id: int | None = None) -> dict:
        data = {"chat_id": chat_id, "display_name": display_name}
        if household_id is not None:
            data["household_id"] = household_id
        resp = self.session.post(
            f"{self.base_url}/api/scanner/household/add-member/",
            json=data, timeout=10,
        )
        if resp.status_code >= 400:
            return resp.json()
        return resp.json()

    def remove_household_member(self, chat_id: str,
                                household_id: int | None = None) -> dict:
        data = {"chat_id": chat_id}
        if household_id is not None:
            data["household_id"] = household_id
        resp = self.session.post(
            f"{self.base_url}/api/scanner/household/remove-member/",
            json=data, timeout=10,
        )
        if resp.status_code >= 400:
            return resp.json()
        return resp.json()

    def list_households(self) -> list[dict]:
        """List all households (admin only)."""
        resp = self.session.get(
            f"{self.base_url}/api/scanner/households/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("households", [])
