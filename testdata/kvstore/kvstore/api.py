"""The only client-facing operation this service exposes: get_value(key).

Instrumented with one metric: a request counter labeled by outcome. A
storage failure is at least visible as a spike in the "error" label --
alertable today with just a new alert rule, no new metric needed. Every
other failure mode in this package (stale reads after a write, unbounded
cache growth, key collisions in storage) has no instrumentation reaching
this far: a cache hit returns whatever the cache has, correct or stale,
and this counter cannot tell the difference.
"""

from __future__ import annotations

from prometheus_client import Counter

from .cache import Cache
from .storage import StorageError

GET_VALUE_REQUESTS = Counter(
    "kvstore_get_value_requests_total",
    "Total get_value calls, labeled by outcome.",
    ["outcome"],
)


def get_value(cache: Cache, key: str) -> str | None:
    try:
        value = cache.get(key)
    except StorageError:
        GET_VALUE_REQUESTS.labels(outcome="error").inc()
        raise
    GET_VALUE_REQUESTS.labels(outcome="success").inc()
    return value
