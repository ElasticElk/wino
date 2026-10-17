"""Pending cards and bottle details that survive a scanner restart.

Keyed strings, TTL 1 hour. Redis when REDIS_URL is set; otherwise memory
(tests, or Redis down at boot). Bytes (label JPEGs) are base64-encoded.

Redis is the source of truth while it's up — the process must not also keep
every JPEG. Memory is only the fallback, with the same TTL and a size cap.
A Redis drop after startup raises StoreError so the bot can tell the user
to resend the photo.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Iterator

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600
CLAIM_TTL = 120
PREFIX = "whiney:card:"
MEM_CAP = 64


class StoreError(Exception):
    """Redis (or the backing store) failed mid-request."""


class _Live(dict):
    """A card dict that writes back on top-level mutation (tests do this)."""

    def __init__(self, store: "CardStore", key: str, data: dict):
        super().__init__(data)
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_key", key)

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self._store._put(self._key, dict(self))

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._store._put(self._key, dict(self))

    def pop(self, k, *default):
        value = super().pop(k, *default) if default else super().pop(k)
        self._store._put(self._key, dict(self))
        return value


def _encode(obj):
    if isinstance(obj, bytes):
        return {"__bytes__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    return obj


def _decode(obj):
    if isinstance(obj, dict):
        if set(obj.keys()) == {"__bytes__"} and isinstance(obj.get("__bytes__"), str):
            return base64.b64decode(obj["__bytes__"])
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


class CardStore:
    def __init__(self, redis_client=None, ttl: int = DEFAULT_TTL, prefix: str = PREFIX):
        self._redis = redis_client
        self._ttl = ttl
        self._prefix = prefix
        self._mem: dict[str, dict] = {}
        self._mem_at: dict[str, float] = {}

    @classmethod
    def from_url(cls, url: str, ttl: int = DEFAULT_TTL, prefix: str = PREFIX) -> "CardStore":
        url = (url or "").strip()
        if not url:
            return cls(ttl=ttl, prefix=prefix)
        try:
            import redis

            client = redis.Redis.from_url(
                url, decode_responses=True, socket_connect_timeout=2,
            )
            client.ping()
            logger.info("CardStore using Redis prefix=%s ttl=%ss", prefix, ttl)
            return cls(redis_client=client, ttl=ttl, prefix=prefix)
        except Exception as e:
            logger.warning("CardStore Redis unavailable (%s); cards stay in memory", e)
            return cls(ttl=ttl, prefix=prefix)

    def _rkey(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _lock_key(self, key: str) -> str:
        return f"{key}:lock"

    def _redis_call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoreError:
            raise
        except Exception as e:
            logger.warning("CardStore Redis error: %s", e)
            raise StoreError("card store unavailable") from e

    def _expire_mem(self) -> None:
        if self._redis is not None:
            return
        now = time.time()
        dead = [k for k, t in self._mem_at.items() if now - t > self._ttl]
        for k in dead:
            self._mem.pop(k, None)
            self._mem_at.pop(k, None)
        if len(self._mem) <= MEM_CAP:
            return
        oldest = sorted(self._mem_at.items(), key=lambda kv: kv[1])
        for k, _ in oldest[: len(self._mem) - MEM_CAP]:
            self._mem.pop(k, None)
            self._mem_at.pop(k, None)

    def _load(self, key: str) -> dict | None:
        if self._redis is not None:
            raw = self._redis_call(self._redis.get, self._rkey(key))
            if raw is None:
                return None
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                return None
            data = _decode(data)
            return data if isinstance(data, dict) else None
        self._expire_mem()
        return self._mem.get(key)

    def _put(self, key: str, payload: dict) -> None:
        data = dict(payload)
        if self._redis is not None:
            self._redis_call(
                self._redis.set,
                self._rkey(key),
                json.dumps(_encode(data), default=str),
                ex=self._ttl,
            )
            return
        self._expire_mem()
        self._mem[key] = data
        self._mem_at[key] = time.time()
        self._expire_mem()

    def _delete(self, key: str) -> None:
        self._mem.pop(key, None)
        self._mem_at.pop(key, None)
        if self._redis is not None:
            self._redis_call(self._redis.delete, self._rkey(key))

    def try_claim(self, key: str, ttl: int = CLAIM_TTL) -> bool:
        """Atomic in-progress lock. False if another tap already holds it."""
        lock = self._lock_key(key)
        if self._redis is not None:
            got = self._redis_call(
                self._redis.set, self._rkey(lock), "1", nx=True, ex=ttl,
            )
            return bool(got)
        self._expire_mem()
        now = time.time()
        held = self._mem_at.get(lock)
        if held is not None and now - held < ttl:
            return False
        self._mem[lock] = {"claim": True}
        self._mem_at[lock] = now
        return True

    def release_claim(self, key: str) -> None:
        self._delete(self._lock_key(key))

    def get(self, key, default=None):
        data = self._load(key)
        if data is None:
            return default
        return _Live(self, key, data)

    def __getitem__(self, key: str) -> _Live:
        data = self._load(key)
        if data is None:
            raise KeyError(key)
        return _Live(self, key, data)

    def __setitem__(self, key: str, value) -> None:
        self._put(key, dict(value) if isinstance(value, dict) else value)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if self._redis is not None:
            return bool(self._redis_call(self._redis.exists, self._rkey(key)))
        self._expire_mem()
        return key in self._mem

    def _iter_keys(self) -> list[str]:
        if self._redis is None:
            self._expire_mem()
            return [k for k in self._mem if not k.endswith(":lock")]
        try:
            raw = list(self._redis.scan_iter(match=f"{self._prefix}*"))
        except Exception as e:
            logger.warning("CardStore Redis scan error: %s", e)
            raise StoreError("card store unavailable") from e
        suffix = len(self._prefix)
        keys = []
        for k in raw:
            name = k[suffix:]
            if name.endswith(":lock"):
                continue
            keys.append(name)
        return keys

    def __iter__(self) -> Iterator[str]:
        return iter(self._iter_keys())

    def __len__(self) -> int:
        return len(self._iter_keys())

    def items(self):
        for key in self._iter_keys():
            data = self._load(key)
            if data is not None:
                yield key, _Live(self, key, data)

    def pop(self, key, default=None):
        data = self._load(key)
        if data is None:
            return default
        self._delete(key)
        return data

    def clear(self) -> None:
        if self._redis is not None:
            for k in self._iter_keys():
                self._delete(k)
                self._delete(self._lock_key(k))
        self._mem.clear()
        self._mem_at.clear()
