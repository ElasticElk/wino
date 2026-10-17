"""CardStore: Redis is source of truth; memory is a capped TTL fallback."""

import time

import pytest

from integrations.card_store import MEM_CAP, CardStore, StoreError


class FakeRedis:
    def __init__(self):
        self.data = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0

    def exists(self, key):
        return 1 if key in self.data else 0

    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


def test_redis_put_does_not_copy_into_memory():
    redis = FakeRedis()
    store = CardStore(redis_client=redis)
    store["card"] = {"photo": b"x" * 120_000, "wine_name": "Chenin"}
    assert store._mem == {}
    assert store.get("card")["photo"] == b"x" * 120_000
    assert any(redis.data.values())


def test_memory_fallback_expires():
    store = CardStore(ttl=1)
    store["a"] = {"n": 1}
    store._mem_at["a"] = time.time() - 2
    assert store.get("a") is None


def test_memory_fallback_caps_size():
    store = CardStore(ttl=3600)
    now = time.time()
    for i in range(MEM_CAP + 8):
        store[str(i)] = {"n": i}
        store._mem_at[str(i)] = now - (MEM_CAP + 8 - i)
    store._expire_mem()
    assert len(store) <= MEM_CAP
    assert "0" not in store


def test_try_claim_is_exclusive_in_memory():
    store = CardStore()
    assert store.try_claim("k") is True
    assert store.try_claim("k") is False
    store.release_claim("k")
    assert store.try_claim("k") is True


def test_try_claim_uses_redis_set_nx():
    redis = FakeRedis()
    store = CardStore(redis_client=redis)
    assert store.try_claim("k") is True
    assert store.try_claim("k") is False
    store.release_claim("k")
    assert store.try_claim("k") is True


def test_scan_iter_errors_become_store_error():
    class Boom:
        def scan_iter(self, match=None):
            raise RuntimeError("down")

    store = CardStore(redis_client=Boom())
    with pytest.raises(StoreError):
        list(store)


def test_lock_keys_are_not_listed():
    store = CardStore()
    store["card"] = {"n": 1}
    assert store.try_claim("card") is True
    assert list(store) == ["card"]
