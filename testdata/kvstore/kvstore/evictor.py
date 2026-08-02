"""Background eviction: periodically walks Cache entries and removes ones
past TTL_SECONDS, so the cache doesn't grow without bound.

Known issue: Cache stores each entry's insertion time in milliseconds
(Entry.inserted_at_ms), but `now` below is computed in seconds -- so
`now - entry.inserted_at_ms` is always a large negative number, never
greater than TTL_SECONDS. The "expired" condition is therefore never true,
no entry is ever evicted, and the cache grows without bound under
sustained unique-key traffic.
"""

from __future__ import annotations

import time

from .cache import Cache

TTL_SECONDS = 300.0


def run_eviction_pass(cache: Cache) -> int:
    """Remove every entry older than TTL_SECONDS; returns how many were
    removed, so a caller (e.g. a scheduler loop) can log/observe progress."""
    now = time.time()
    expired_keys = [
        key
        for key, entry in cache.items()
        if now - entry.inserted_at_ms > TTL_SECONDS
    ]
    for key in expired_keys:
        cache.evict(key)
    return len(expired_keys)
