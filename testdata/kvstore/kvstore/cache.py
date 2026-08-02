"""In-memory cache in front of Storage.

Cache never expires anything on its own -- a separate background evictor
(evictor.py) is responsible for walking this cache's entries and removing
ones past their TTL. Cache.get() only ever consults its own dict first,
falling back to storage on a miss; it never re-checks storage for an
already-cached key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import ItemsView

from .storage import Storage


@dataclass
class Entry:
    value: str | None
    inserted_at_ms: float


class Cache:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._entries: dict[str, Entry] = {}

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is not None:
            return entry.value
        value = self._storage.get(key)
        self._entries[key] = Entry(value=value, inserted_at_ms=time.time() * 1000)
        return value

    def items(self) -> ItemsView[str, Entry]:
        """Every currently-cached entry, for the evictor to walk."""
        return self._entries.items()

    def evict(self, key: str) -> None:
        """Remove one entry if present. Called by the evictor on expiry, and
        available for a writer to call after updating storage -- nothing in
        this package currently calls it from a writer, which is the source of
        one of this fixture's intentional failure modes (documented in
        admin.py, written separately)."""
        self._entries.pop(key, None)
