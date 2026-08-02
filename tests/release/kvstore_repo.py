"""Builds a fresh, self-contained kvstore git repository with real commit
history -- one new copy per capture, at whatever destination the caller
gives. Retires `miniflux_repo`'s model of navigating a single, real,
external checkout shared across every scenario: since kvstore is a small
fixture this project owns outright (`testdata/kvstore`), each capture can
have its own instance instead of coordinating over one physical directory.
This removes the need for the `exclusive` bazel tag the miniflux-era capture
tests carried -- confirmed empirically (2026-08-01) that bazel gives every
test action, even concurrent instances of the identical target, an isolated
TEST_TMPDIR, so building the repo inside the test's own `tmp_path` is enough
for genuine parallel execution.

`genesis` is `testdata/kvstore` verbatim (the shipped fixture, bugs and all)
-- every other named commit is a real, single-purpose fix or addition
authored only here, on its own branch off `genesis`, never folded back into
`testdata/kvstore` itself (that stays the canonical "current, buggy" snapshot
the README describes). `build()` returns every named commit's SHA; a caller
picks whichever pair it needs as a baseline/target range. As with
miniflux_repo, this module never leaves the built repo on a stray detached
HEAD outside the `on_commit` block, though since the repo is throwaway per
capture this is hygiene rather than a shared-state requirement.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

_TESTDATA_ROOT = Path(__file__).resolve().parents[2] / "testdata" / "kvstore"

_FIXED_STORAGE_PY = '''"""Backing key-value storage: a small flat-file store.

Storage.get/put persist to a single file, one JSON object per line -- this
avoids the delimiter-collision bug of the original comma-joined format (a
key or value containing a comma no longer corrupts a neighboring entry).
"""

from __future__ import annotations

import json
from pathlib import Path


class StorageError(Exception):
    """Raised when the backing file can't be read or written."""


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, key: str) -> str | None:
        try:
            if not self._path.exists():
                return None
            for line in self._path.read_text().splitlines():
                entry = json.loads(line)
                if entry["key"] == key:
                    return entry["value"]
            return None
        except OSError as exc:
            raise StorageError(f"failed to read {self._path}: {exc}") from exc

    def put(self, key: str, value: str) -> None:
        try:
            existing: dict[str, str] = {}
            if self._path.exists():
                for line in self._path.read_text().splitlines():
                    entry = json.loads(line)
                    existing[entry["key"]] = entry["value"]
            existing[key] = value
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                "\\n".join(
                    json.dumps({"key": k, "value": v}) for k, v in existing.items()
                )
                + "\\n"
            )
        except OSError as exc:
            raise StorageError(f"failed to write {self._path}: {exc}") from exc
'''

_FIXED_EVICTOR_PY = '''"""Background eviction: periodically walks Cache entries and removes ones
past TTL_SECONDS, so the cache doesn't grow without bound.
"""

from __future__ import annotations

import time

from .cache import Cache

TTL_SECONDS = 300.0


def run_eviction_pass(cache: Cache) -> int:
    """Remove every entry older than TTL_SECONDS; returns how many were
    removed, so a caller (e.g. a scheduler loop) can log/observe progress."""
    now_ms = time.time() * 1000
    expired_keys = [
        key
        for key, entry in cache.items()
        if now_ms - entry.inserted_at_ms > TTL_SECONDS * 1000
    ]
    for key in expired_keys:
        cache.evict(key)
    return len(expired_keys)
'''

_FIXED_ADMIN_PY = '''"""Internal, non-client-facing write path -- e.g. used by a periodic
data-sync job. Nothing here is reachable from external clients; the public
API surface is exactly get_value (api.py).
"""

from __future__ import annotations

from .cache import Cache
from .storage import Storage


def update_value(storage: Storage, cache: Cache, key: str, value: str) -> None:
    """Write a new value for key, then evict it from the cache so the next
    read goes to storage and picks up the new value immediately."""
    storage.put(key, value)
    cache.evict(key)
'''

_README_METRICS_SECTION = """
## Metrics

`kvstore_get_value_requests_total{outcome}` (api.py) counts every
`get_value` call, labeled `success` or `error`. It's enough to alert on a
spike in the `error` label; it says nothing about staleness, unbounded
cache growth, or storage collisions, since a cache hit looks the same
whether or not the value is correct.
"""

_TEST_STORAGE_PY = '''"""Basic tests for Storage -- exercises the happy path only; doesn't cover
the comma-collision bug (see kvstore's README)."""

from __future__ import annotations

from pathlib import Path

from kvstore.storage import Storage


def test_put_then_get_returns_value(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data.txt")
    storage.put("a", "1")
    assert storage.get("a") == "1"


def test_get_missing_key_returns_none(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data.txt")
    assert storage.get("missing") is None
'''


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def build(dest: Path) -> dict[str, str]:
    """Create a fresh kvstore repo at `dest` with real commit history;
    returns every named commit's SHA. `dest` must not already exist."""
    dest.mkdir(parents=True)
    shutil.copytree(_TESTDATA_ROOT, dest, dirs_exist_ok=True)
    _git(dest, "init", "--quiet")
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "Test")
    shas: dict[str, str] = {}
    shas["genesis"] = _commit_all(
        dest, "Add kvstore: a minimal key-value store service"
    )
    main_branch = _git(dest, "branch", "--show-current")

    def _branch_from_genesis(branch_name: str) -> None:
        _git(dest, "checkout", "--quiet", "-b", branch_name, shas["genesis"])

    # fix_evictor: single-file defensive fix (update-happy-path).
    _branch_from_genesis("fix-evictor")
    (dest / "kvstore" / "evictor.py").write_text(_FIXED_EVICTOR_PY)
    shas["fix_evictor"] = _commit_all(
        dest, "Fix evictor: compare elapsed time in consistent units (ms)"
    )

    # docs_update: docs-only (update-no-impact-redirect).
    _git(dest, "checkout", "--quiet", shas["genesis"])
    _branch_from_genesis("docs-update")
    with (dest / "README.md").open("a") as handle:
        handle.write(_README_METRICS_SECTION)
    shas["docs_update"] = _commit_all(
        dest, "Document the existing get_value request metric in the README"
    )

    # test_only_change: adds a tests/ dir, touches no production file
    # (update-no-impact).
    _git(dest, "checkout", "--quiet", shas["genesis"])
    _branch_from_genesis("test-only-change")
    tests_dir = dest / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_storage.py").write_text(_TEST_STORAGE_PY)
    shas["test_only_change"] = _commit_all(dest, "Add basic tests for Storage.get/put")

    # multi_commit_range: three real commits across three concerns
    # (update-multi-commit, R8).
    _git(dest, "checkout", "--quiet", shas["genesis"])
    _branch_from_genesis("multi-commit-range")
    (dest / "kvstore" / "storage.py").write_text(_FIXED_STORAGE_PY)
    _commit_all(dest, "Fix storage: switch to JSON-lines format, avoid comma-collision bug")
    (dest / "kvstore" / "admin.py").write_text(_FIXED_ADMIN_PY)
    _commit_all(dest, "Fix admin: evict cache entry after writing, avoid stale reads")
    with (dest / "README.md").open("a") as handle:
        handle.write(_README_METRICS_SECTION)
    shas["multi_commit_range_end"] = _commit_all(
        dest, "Document the existing get_value request metric in the README"
    )

    # dynamic_expansion_delta: the storage and admin fixes above, but as one
    # commit spanning two distinct failure domains (update-dynamic-expansion).
    _git(dest, "checkout", "--quiet", shas["genesis"])
    _branch_from_genesis("dynamic-expansion-delta")
    (dest / "kvstore" / "storage.py").write_text(_FIXED_STORAGE_PY)
    (dest / "kvstore" / "admin.py").write_text(_FIXED_ADMIN_PY)
    shas["dynamic_expansion_delta"] = _commit_all(
        dest, "Fix storage collision bug and admin's stale-cache bug together"
    )

    _git(dest, "checkout", "--quiet", main_branch)
    return shas


class DirtyRepositoryError(RuntimeError):
    """The built repo has changes outside `.blare/` -- refuse to touch it."""


def assert_clean_outside_blare(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain")
    offending = [
        line for line in status.splitlines() if not line[3:].startswith(".blare/")
    ]
    if offending:
        raise DirtyRepositoryError(
            f"{repo} has changes outside .blare/, refusing to drive a scenario there: "
            f"{offending}"
        )


def current_branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current")


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def checkout_commit(repo: Path, sha: str) -> None:
    assert_clean_outside_blare(repo)
    subprocess.run(["git", "checkout", "--quiet", "--detach", sha], cwd=repo, check=True)


def restore(repo: Path, ref: str) -> None:
    subprocess.run(["git", "checkout", "--quiet", ref], cwd=repo, check=True)


@contextlib.contextmanager
def on_commit(repo: Path, sha: str) -> Iterator[None]:
    """Checkout `sha` for the duration of the block, then restore whatever ref
    was checked out beforehand."""
    original = current_branch(repo) or head_sha(repo)
    checkout_commit(repo, sha)
    try:
        yield
    finally:
        restore(repo, original)


def blare_root(repo: Path) -> Path:
    return repo / ".blare"


__all__ = [
    "build",
    "DirtyRepositoryError",
    "assert_clean_outside_blare",
    "current_branch",
    "head_sha",
    "checkout_commit",
    "restore",
    "on_commit",
    "blare_root",
]
