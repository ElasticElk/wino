import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class WineCellarAPI:
    def __init__(self, base_url: str, api_token: str, default_storage: str = "Wine Fridge",
                 web_url: str = ""):
        self.base_url = base_url.rstrip("/")
        self.web_url = (web_url or base_url).rstrip("/")
        self.default_storage = default_storage
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        })
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
        resp = self.session.post(
            f"{self.base_url}/api/wines/", json=wine_data, timeout=10,
        )
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

    def update_wine(self, wine_id: int, wine_data: dict) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/api/wines/{wine_id}/", json=wine_data, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
