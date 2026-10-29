import logging
import time

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
        from integrations.barcodes import normalise_barcode
        ean = normalise_barcode(barcode) or barcode
        resp = self.session.get(
            f"{self.base_url}/api/wines/by-barcode/{ean}/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["wines"]

    def add_stock(self, wine_id: int, price: float | None = None,
                  storage_name: str | None = None, row: int | None = None,
                  drank: bool = False, confirm_duplicate: bool = False,
                  check_duplicate: bool = False) -> dict:
        data = {"wine_id": wine_id, "storage_name": storage_name or self.default_storage}
        if price is not None:
            data["price"] = price
        if row is not None:
            data["row"] = row
        if drank:
            data["drank"] = True
        if confirm_duplicate:
            data["confirm_duplicate"] = True
        if check_duplicate:
            data["check_duplicate"] = True
        resp = self.session.post(
            f"{self.base_url}/api/stock/add/", json=data, timeout=10,
        )
        if resp.status_code >= 400:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
            body["status_code"] = resp.status_code
            return body
        return resp.json()

    def set_stock(self, wine_id: int, quantity: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/set/",
            json={"wine_id": wine_id, "quantity": quantity},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def remove_stock(self, wine_id: int | None = None, storage_name: str | None = None,
                     item_id: int | None = None) -> dict:
        data = {}
        if wine_id is not None:
            data["wine_id"] = wine_id
        if item_id is not None:
            data["item_id"] = item_id
        if storage_name:
            data["storage_name"] = storage_name
        resp = self.session.post(
            f"{self.base_url}/api/stock/remove/", json=data, timeout=10,
        )
        if resp.status_code == 409:
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def bulk_stock(self, ops: list[dict]) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/bulk/", json={"ops": ops}, timeout=20,
        )
        if resp.status_code >= 400:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
            body["status_code"] = resp.status_code
            return body
        return resp.json()

    def bulk_stock_undo(self, batch_id: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/bulk/{batch_id}/undo/", timeout=20,
        )
        if resp.status_code >= 400:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
            body["status_code"] = resp.status_code
            return body
        return resp.json()

    def restore_latest(self, wine_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/restore-latest/",
            json={"wine_id": wine_id},
            timeout=10,
        )
        if resp.status_code >= 400:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
            body["status_code"] = resp.status_code
            return body
        return resp.json()

    def restore_stock(self, item_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/stock/{item_id}/restore/", timeout=10,
        )
        if resp.status_code >= 400:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
            body["status_code"] = resp.status_code
            return body
        return resp.json()

    def create_wine(self, wine_data: dict) -> dict:
        logger.info(f"[API] create_wine payload: {wine_data}")
        resp = self.session.post(
            f"{self.base_url}/api/wines/", json=wine_data, timeout=10,
        )
        if resp.status_code == 409:
            body = {}
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("id"):
                body.setdefault("error", "duplicate_wine")
                logger.info("[API] create_wine duplicate id=%s", body.get("id"))
                return body
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
        """Full details for one wine, including its stock locations.

        One request. This used to fetch every wine in the cellar *and* every
        in-stock wine, then merge the two client-side, to render a single card.
        """
        resp = self.session.get(
            f"{self.base_url}/api/wines/{wine_id}/detail/", timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def apply_references(self, wine_id: int, payload: dict) -> dict:
        """Persist a lookup the label card already ran. No second web search."""
        resp = self.session.post(
            f"{self.base_url}/api/wines/{wine_id}/apply-references/",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def refresh_references(self, wine_id: int, wait_seconds: int = 240,
                           poll_seconds: int = 5) -> dict:
        """Re-ask Grok for critic_score / vivino_rating / market_price.

        The server enqueues the lookup and returns 202 immediately — a live web
        search takes far longer than a request should hold a worker open. We
        then poll from this thread, which is already a background executor on
        the bot side, so waiting here costs nothing that matters.

        Returns the usual {"updated": bool, "confidence": str, ...}. On timeout
        returns updated=False with a message; the task keeps running and the
        values will simply be there next time the card is opened.
        """
        resp = self.session.post(
            f"{self.base_url}/api/wines/{wine_id}/refresh-references/", timeout=15,
        )
        resp.raise_for_status()
        queued = resp.json()

        task_id = queued.get("task_id")
        if not task_id:
            # Older server, or it answered synchronously — take it at face value.
            return queued

        deadline = time.time() + wait_seconds
        status_url = f"{self.base_url}/api/wines/{wine_id}/refresh-references/"
        while time.time() < deadline:
            time.sleep(poll_seconds)
            try:
                poll = self.session.get(
                    status_url, params={"task_id": task_id}, timeout=15,
                )
                poll.raise_for_status()
                body = poll.json()
            except requests.RequestException as e:
                logger.warning(f"[API] refresh_references poll failed: {e}")
                continue

            state = body.get("state")
            if state == "SUCCESS":
                return body.get("result") or {"updated": False}
            if state == "FAILURE":
                return body.get("result") or {"updated": False, "error": "Lookup failed"}

        logger.info(f"[API] refresh_references still running for wine #{wine_id}")
        return {
            "updated": False,
            "message": "Still looking — the values will appear on the wine shortly.",
        }

    def lookup_references(self, name: str, producer: str = "", vintage=None,
                          wait_seconds: int = 120, poll_seconds: int = 5,
                          **hints) -> dict:
        """Ask Grok for critic / Vivino / market price for a wine we don't own.

        Same queue-and-poll shape as refresh_references, against an endpoint
        that takes a name instead of a wine id — a label photographed in a shop
        has no row to refresh, and that is precisely when the numbers decide
        whether the bottle comes home. Nothing is persisted.

        The wait is shorter than refresh_references': this one is edited into a
        card the user is looking at right now, so a lookup that drags on is
        better abandoned than waited out.
        """
        payload = {"name": name, "producer": producer or "", "vintage": vintage}
        payload.update({k: v for k, v in hints.items() if v})

        url = f"{self.base_url}/api/references/lookup/"
        resp = self.session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        queued = resp.json()

        task_id = queued.get("task_id")
        if not task_id:
            return queued

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(poll_seconds)
            try:
                poll = self.session.get(url, params={"task_id": task_id}, timeout=15)
                poll.raise_for_status()
                body = poll.json()
            except requests.RequestException as e:
                logger.warning(f"[API] lookup_references poll failed: {e}")
                continue

            state = body.get("state")
            if state == "SUCCESS":
                return body.get("result") or {"updated": False}
            if state == "FAILURE":
                return body.get("result") or {"updated": False, "error": "Lookup failed"}

        logger.info(f"[API] lookup_references timed out for {name!r}")
        return {"updated": False, "message": "Lookup timed out"}

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

    def stock_history(self, offset: int = 0, limit: int = 5) -> dict:
        """Recently drank or removed bottles, newest first."""
        resp = self.session.get(
            f"{self.base_url}/api/stock/history/",
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def vote_item(self, item_id: int, vote: str) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/api/stock/{item_id}/vote/",
            json={"vote": vote},
            timeout=10,
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

    def get_household(self, household_id: int | None = None) -> dict:
        params = {}
        if household_id is not None:
            params["household_id"] = household_id
        resp = self.session.get(
            f"{self.base_url}/api/scanner/household/",
            params=params, timeout=10,
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

    def set_gamification(self, enabled: bool, household_id: int | None = None) -> dict:
        data = {"gamification_enabled": bool(enabled)}
        if household_id is not None:
            data["household_id"] = household_id
        resp = self.session.patch(
            f"{self.base_url}/api/scanner/household/", json=data, timeout=10,
        )
        if resp.status_code >= 400:
            try:
                return resp.json()
            except ValueError:
                return {"error": f"HTTP {resp.status_code}"}
        return resp.json()

    def list_achievements(self) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/scanner/achievements/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_taste(self) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/scanner/taste/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def record_intent(self, kind: str, wine: dict | None = None,
                      wines: list | None = None, scene: str = "",
                      recommendation=None, payload: dict | None = None) -> dict:
        body = {"kind": kind}
        if wine:
            body["wine"] = wine
        if wines is not None:
            body["wines"] = wines
            body["scene"] = scene
            body["recommendation"] = recommendation
        if payload is not None:
            body["payload"] = payload
        try:
            resp = self.session.post(
                f"{self.base_url}/api/scanner/taste/", json=body, timeout=8,
            )
            if resp.status_code >= 400:
                logger.warning(f"record_intent failed: {resp.status_code} {resp.text}")
                return {}
            return resp.json()
        except Exception as e:
            logger.warning(f"record_intent failed: {e}")
            return {}

    def update_intent(self, intent_id: int, changes: dict) -> dict:
        """Merge outcome, shown, rejected or place into one ask. Never raises."""
        try:
            resp = self.session.patch(
                f"{self.base_url}/api/scanner/taste/{intent_id}/",
                json=changes, timeout=8,
            )
            if resp.status_code >= 400:
                logger.warning(f"update_intent failed: {resp.status_code} {resp.text}")
                return {}
            return resp.json()
        except Exception as e:
            logger.warning(f"update_intent failed: {e}")
            return {}

    def create_household(self, name: str, chat_id: str, display_name: str) -> dict:
        """Staff-only: create a household and provision its owner on Telegram."""
        resp = self.session.post(
            f"{self.base_url}/api/scanner/households/",
            json={
                "name": name,
                "chat_id": chat_id,
                "display_name": display_name,
            },
            timeout=15,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text or f"HTTP {resp.status_code}"}
        if resp.status_code >= 400:
            if "error" not in body:
                body["error"] = body.get("detail") or f"HTTP {resp.status_code}"
            return body
        return body

    def _invite_body(self, resp) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text or f"HTTP {resp.status_code}"}
        if resp.status_code >= 400:
            if "error" not in body:
                body["error"] = body.get("detail") or f"HTTP {resp.status_code}"
            body["status_code"] = resp.status_code
            return body
        return body

    def mint_invite(self, kind: str, household_id: int | None = None) -> dict:
        data = {"kind": kind}
        if household_id is not None:
            data["household_id"] = household_id
        resp = self.session.post(
            f"{self.base_url}/api/scanner/invites/", json=data, timeout=15,
        )
        return self._invite_body(resp)

    def preview_invite(self, code: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/scanner/invites/preview/",
            json={"code": code}, timeout=10,
        )
        return self._invite_body(resp)

    def update_settings(self, country: str = "", currency: str = "") -> dict:
        data = {"country": country}
        if currency:
            data["currency"] = currency
        resp = self.session.post(
            f"{self.base_url}/api/scanner/settings/", json=data, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def redeem_invite(self, code: str, chat_id: str, display_name: str,
                      household_name: str | None = None) -> dict:
        data = {
            "code": code,
            "chat_id": str(chat_id),
            "display_name": display_name,
        }
        if household_name:
            data["household_name"] = household_name
        resp = self.session.post(
            f"{self.base_url}/api/scanner/invites/redeem/", json=data, timeout=15,
        )
        return self._invite_body(resp)

    def cancel_invite(self, household_id: int | None = None) -> dict:
        data = {} if household_id is None else {"household_id": household_id}
        resp = self.session.post(
            f"{self.base_url}/api/scanner/invites/cancel/", json=data, timeout=10,
        )
        return self._invite_body(resp)

    def whois(self, query: str) -> dict:
        """Staff-only: look up a Telegram user by chat ID or name."""
        resp = self.session.get(
            f"{self.base_url}/api/scanner/whois/",
            params={"q": query},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def chat_stats(self, days: int = 30) -> dict:
        """Staff-only: how chat recommendations landed across every household."""
        resp = self.session.get(
            f"{self.base_url}/api/scanner/chat-stats/", params={"days": days}, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def stats(self) -> dict:
        """Staff-only: counts across every household."""
        resp = self.session.get(
            f"{self.base_url}/api/scanner/stats/", timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _list_response(self, resp) -> dict:
        body = {}
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        body.setdefault("error", body.get("detail") or f"HTTP {resp.status_code}")
        body["status_code"] = resp.status_code
        return body

    def list_lists(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/lists/", timeout=10)
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def get_list(self, list_id: int, limit: int = 5, offset: int = 0) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/lists/{list_id}/",
            params={"limit": limit, "offset": offset},
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def add_list_item(self, list_id: int, payload: dict) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/lists/{list_id}/items/",
            json=payload,
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def delete_list_item(self, list_id: int, item_id: int) -> dict:
        resp = self.session.delete(
            f"{self.base_url}/api/lists/{list_id}/items/{item_id}/",
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def move_list_item_to_cellar(
        self,
        list_id: int,
        item_id: int,
        storage_name: str | None = None,
        row: int | None = None,
        price: float | None = None,
        confirm_duplicate: bool = False,
        ack_duplicate: bool = False,
        item_id_ack: int | None = None,
    ) -> dict:
        data = {}
        if storage_name:
            data["storage_name"] = storage_name
        if row is not None:
            data["row"] = row
        if price is not None:
            data["price"] = price
        if confirm_duplicate:
            data["confirm_duplicate"] = True
        if ack_duplicate:
            data["ack_duplicate"] = True
            if item_id_ack is not None:
                data["item_id"] = item_id_ack
        resp = self.session.post(
            f"{self.base_url}/api/lists/{list_id}/items/{item_id}/to-cellar/",
            json=data,
            timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def list_clubs(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/clubs/", timeout=10)
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def create_club(self, name: str, cellar_visibility="full", badges_visible=True) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/",
            json={
                "name": name,
                "cellar_visibility": cellar_visibility,
                "badges_visible": badges_visible,
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def get_club(self, club_id: int) -> dict:
        resp = self.session.get(f"{self.base_url}/api/clubs/{club_id}/", timeout=10)
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def patch_club(self, club_id: int, **fields) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/api/clubs/{club_id}/", json=fields, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def patch_club_membership(self, club_id: int, **fields) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/api/clubs/{club_id}/membership/", json=fields, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def leave_club(self, club_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/leave/", json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {"status": "left"}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def handover_club(self, club_id: int, user_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/handover/",
            json={"user_id": user_id}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def club_feed(
        self, club_id: int, limit: int = 5, offset: int = 0, event_id: int | None = None,
    ) -> dict:
        params = {"limit": limit, "offset": offset}
        if event_id is not None:
            params["event_id"] = event_id
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/feed/",
            params=params, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def club_members(self, club_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/members/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def club_peek(self, club_id: int, user_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/members/{user_id}/cellar/",
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def club_badges(self, club_id: int, user_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/members/{user_id}/badges/",
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def mint_club_invite(self, club_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/invites/", json={}, timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def cancel_club_invite(self, club_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/invites/cancel/", json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def redeem_club_invite(
        self, code: str, chat_id: str, display_name: str,
        household_name: str | None = None, household_id: int | None = None,
    ) -> dict:
        data = {
            "code": code,
            "chat_id": str(chat_id),
            "display_name": display_name,
        }
        if household_name:
            data["household_name"] = household_name
        if household_id is not None:
            data["household_id"] = household_id
        resp = self.session.post(
            f"{self.base_url}/api/scanner/club-invites/redeem/", json=data, timeout=15,
        )
        return self._invite_body(resp)

    def start_dismantle(self, club_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/dismantle/", json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def get_dismantle(self, club_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/dismantle/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def cast_dismantle_ballot(self, club_id: int, vote_id: int, choice: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/dismantle/{vote_id}/ballot/",
            json={"choice": choice}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def cancel_dismantle(self, club_id: int, vote_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/dismantle/{vote_id}/cancel/",
            json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def club_comments(self, club_id: int, event_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/events/{event_id}/comments/",
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def add_club_comment(self, club_id: int, event_id: int, text: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/events/{event_id}/comments/",
            json={"text": text}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def event_to_list(self, club_id: int, event_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/events/{event_id}/to-list/",
            json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def club_list(self, club_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/list/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def add_club_list_item(self, club_id: int, name: str, vintage=None, producer="", wine_id=None) -> dict:
        data = {"name": name, "producer": producer or ""}
        if vintage is not None:
            data["vintage"] = vintage
        if wine_id is not None:
            data["wine_id"] = wine_id
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/list/items/", json=data, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def delete_club_list_item(self, club_id: int, item_id: int) -> dict:
        resp = self.session.delete(
            f"{self.base_url}/api/clubs/{club_id}/list/items/{item_id}/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {"status": "deleted"}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def list_tasteoffs(self, club_id: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def create_tasteoff(self, club_id: int, title: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/",
            json={"title": title}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def get_tasteoff(self, club_id: int, tid: int) -> dict:
        resp = self.session.get(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/", timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def add_tasteoff_bottle(self, club_id: int, tid: int, label: str, wine_id=None) -> dict:
        data = {"label": label}
        if wine_id is not None:
            data["wine_id"] = wine_id
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/bottles/",
            json=data, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def delete_tasteoff_bottle(self, club_id: int, tid: int, bid: int) -> dict:
        resp = self.session.delete(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/bottles/{bid}/",
            timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {"status": "deleted"}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def open_tasteoff(self, club_id: int, tid: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/open/",
            json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def score_tasteoff(self, club_id: int, tid: int, bottle_id: int, score: int) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/scores/",
            json={"bottle_id": bottle_id, "score": score}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def close_tasteoff(self, club_id: int, tid: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/tasteoffs/{tid}/close/",
            json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def discover_clubs(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/clubs/discover/", timeout=10)
        if resp.status_code >= 400:
            return self._list_response(resp)
        return resp.json()

    def join_club(self, club_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/join/", json={}, timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def create_global_club(self, name: str, description: str = "") -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/global/",
            json={"name": name, "description": description},
            timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def post_club_note(self, club_id: int, text: str) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/notes/",
            json={"text": text}, timeout=15,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json()
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body

    def kick_club_member(self, club_id: int, user_id: int) -> dict:
        resp = self.session.post(
            f"{self.base_url}/api/clubs/{club_id}/members/{user_id}/kick/",
            json={}, timeout=10,
        )
        if resp.status_code >= 400:
            return self._list_response(resp)
        body = resp.json() if resp.content else {"status": "kicked"}
        if isinstance(body, dict):
            body["status_code"] = resp.status_code
        return body
