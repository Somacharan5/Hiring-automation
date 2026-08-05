"""Rotating pool of API keys — ride several free tiers by switching keys as each
one hits its monthly quota.

The user supplies 8-10 keys per provider in .env as a comma-separated list
(HUNTER_API_KEYS=k1,k2,…). A pool picks a key that still has quota (checked via the
provider's *free* account endpoint, so the check itself costs nothing), and rotates
to the next on a quota/rate error. Nothing is persisted: the free account endpoint
reports quota afresh, so monthly resets are picked up automatically next run.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..db import _load_env


class KeyPool:
    def __init__(self, env_names: list[str],
                 has_quota: Callable[[str], bool] | None = None):
        """`env_names`: env vars to read keys from, in preference order. Each may hold
        one key or a comma-separated list. `has_quota(key)`: optional live quota check
        (its result is cached per key for the pool's lifetime)."""
        _load_env()
        keys: list[str] = []
        for name in env_names:
            for raw in (os.environ.get(name, "") or "").split(","):
                k = raw.strip()
                if k and k not in keys:
                    keys.append(k)
        self.keys = keys
        self._has_quota = has_quota
        self._spent: set[str] = set()          # marked exhausted this process
        self._quota_cache: dict[str, bool] = {}

    def __bool__(self) -> bool:
        return bool(self.keys)

    def _quota_ok(self, key: str) -> bool:
        if self._has_quota is None:
            return True
        if key not in self._quota_cache:
            try:
                self._quota_cache[key] = bool(self._has_quota(key))
            except Exception:                  # noqa: BLE001 — check failed; give the key a chance
                return True
        return self._quota_cache[key]

    def current(self) -> str | None:
        """First key that is neither spent nor out of quota, else None."""
        for k in self.keys:
            if k in self._spent:
                continue
            if self._quota_ok(k):
                return k
            self._spent.add(k)                 # no quota → don't try again
        return None

    def mark_spent(self, key: str) -> None:
        self._spent.add(key)
        self._quota_cache.pop(key, None)

    def run(self, fn: Callable[[str], object],
            is_quota_error: Callable[[Exception], bool]) -> object | None:
        """Call `fn(key)`; on a quota/rate error mark the key spent and try the next.
        Returns fn's result, or None when every key is exhausted."""
        while True:
            key = self.current()
            if key is None:
                return None
            try:
                return fn(key)
            except Exception as e:             # noqa: BLE001
                if is_quota_error(e):
                    self.mark_spent(key)
                    continue
                raise
