"""kvstore: a minimal key-value store service.

The only client-facing operation is get_value(key) (api.py). Internally,
reads flow through an in-memory cache (cache.py) backed by a flat-file
store (storage.py); a background evictor (evictor.py) is meant to keep the
cache bounded; an internal, non-client-facing write path (admin.py) is
used by a periodic data-sync job.
"""
