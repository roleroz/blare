# kvstore

A minimal key-value store service. The only client-facing operation is
`get_value(key)`.

## Components

- `api.py` — the public entrypoint, `get_value(cache, key)`. Instrumented
  with one metric: `kvstore_get_value_requests_total{outcome}`.
- `cache.py` — an in-memory cache in front of storage. Never expires
  anything on its own.
- `storage.py` — the backing flat-file store.
- `evictor.py` — a background pass that's supposed to walk the cache and
  remove entries past their TTL, keeping it bounded.
- `admin.py` — an internal, non-client-facing write path (e.g. used by a
  periodic data-sync job). Not reachable from external clients.

## Purpose

This is a deliberately small, fixture codebase — a target for testing an
observability-analysis tool, not a real service. It contains a handful of
realistic, intentional issues, each several hops upstream of where a user
would actually notice something wrong, rather than one bug with one
obvious symptom.
