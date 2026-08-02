"""Internal, non-client-facing write path -- e.g. used by a periodic
data-sync job. Nothing here is reachable from external clients; the public
API surface is exactly get_value (api.py).
"""

from __future__ import annotations

from .cache import Cache
from .storage import Storage


def update_value(storage: Storage, cache: Cache, key: str, value: str) -> None:
    """Write a new value for key. Note: this only updates the backing
    store -- a reader whose key is already cached keeps seeing the old
    value until the cache entry happens to expire (see evictor.py)."""
    storage.put(key, value)
