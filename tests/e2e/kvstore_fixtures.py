"""Reproduces specific real kvstore deltas locally for update-mode e2e tests
replaying T4.1's real, kvstore-derived fixtures.

`blare update`'s triage message carries the effective delta's real file list and
patch text (agent.md); a replayed real fixture's recorded triage event therefore
pins the *exact* file-content delta that produced it, not just "some change" -- an
e2e test's own ad hoc repo content no longer suffices once the fixture behind it
is a real, live capture (T4.1). This mirrors what `tests/release/kvstore_repo.py`
already does for the release suite's own live captures.

Necessarily duplicates the handful of "fixed" file contents `kvstore_repo.py`
already defines for these same named deltas (kept in sync manually): tests/release
depends on tests/e2e (`kvstore_repo.py` reuses this package's PTY harness), so the
reverse import isn't available, and `kvstore_repo.py` is out of scope for T4.1 to
modify. `genesis` itself needs no duplication -- it's `testdata/kvstore` verbatim,
reached here as a real Bazel data dependency (`testdata/kvstore/BUILD.bazel`)
rather than `kvstore_repo.py`'s own plain-filesystem-path shortcut (its capture
py_test targets run untagged "no-sandbox", against the real source checkout; this
suite's targets run sandboxed and need a declared, hermetic dependency instead).

`bootstrap_analyze_happy_path`/`inject_unmapped_failure_mode` similarly duplicate
`tests/release/capture.py`'s own equivalents (`_bootstrap_analyze`'s replay
mechanism and `inject_unmapped_failure_mode` respectively) for the same one-way
import reason: an e2e test needing a genuine prior `.blare/` replays the
already-captured `analyze-happy-path` fixture rather than seeding a hand-authored
one, so its IDs match what a real bootstrap now deterministically produces
(decisions.md, 2026-08-02: "Bootstrap via replaying analyze-happy-path, not a
fresh live call").
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from python.runfiles import Runfiles

from tests.e2e.pty_harness import PtyProcess, approve_all

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


def _kvstore_source_dir() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    located = runfiles.Rlocation("blare/testdata/kvstore/README.md")
    assert located is not None
    path = Path(located).parent
    assert path.is_dir()
    return path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def build_genesis(dest: Path) -> str:
    """Create a fresh kvstore repo at `dest` (must not already exist) from
    `testdata/kvstore` verbatim, with one commit; returns that commit's SHA."""
    dest.mkdir(parents=True)
    shutil.copytree(_kvstore_source_dir(), dest, dirs_exist_ok=True)
    _git(dest, "init", "--quiet")
    _git(dest, "config", "user.email", "test@example.com")
    _git(dest, "config", "user.name", "Test")
    return _commit_all(dest, "Add kvstore: a minimal key-value store service")


def commit_fix_evictor(repo: Path) -> str:
    """update-happy-path's real delta: fix the evictor's ms/seconds unit
    mismatch. Returns the new commit's SHA."""
    (repo / "kvstore" / "evictor.py").write_text(_FIXED_EVICTOR_PY)
    return _commit_all(repo, "Fix evictor: compare elapsed time in consistent units (ms)")


def commit_docs_update(repo: Path) -> str:
    """update-no-impact-redirect's and update-load-seeded-repair's real delta:
    docs-only. Returns the new commit's SHA."""
    with (repo / "README.md").open("a") as handle:
        handle.write(_README_METRICS_SECTION)
    return _commit_all(repo, "Document the existing get_value request metric in the README")


def commit_test_only_change(repo: Path) -> str:
    """update-no-impact's real delta: adds a tests/ dir, touches no production
    file. Returns the new commit's SHA."""
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_storage.py").write_text(_TEST_STORAGE_PY)
    return _commit_all(repo, "Add basic tests for Storage.get/put")


def commit_multi_commit_range(repo: Path) -> str:
    """update-multi-commit's real delta (R8): three real commits across three
    concerns, as one effective delta. Returns the range's end SHA."""
    (repo / "kvstore" / "storage.py").write_text(_FIXED_STORAGE_PY)
    _commit_all(repo, "Fix storage: switch to JSON-lines format, avoid comma-collision bug")
    (repo / "kvstore" / "admin.py").write_text(_FIXED_ADMIN_PY)
    _commit_all(repo, "Fix admin: evict cache entry after writing, avoid stale reads")
    with (repo / "README.md").open("a") as handle:
        handle.write(_README_METRICS_SECTION)
    return _commit_all(repo, "Document the existing get_value request metric in the README")


def commit_dynamic_expansion_delta(repo: Path) -> str:
    """update-dynamic-expansion's real delta: the storage and admin fixes above,
    but as one commit spanning two distinct failure domains. Returns the new
    commit's SHA."""
    (repo / "kvstore" / "storage.py").write_text(_FIXED_STORAGE_PY)
    (repo / "kvstore" / "admin.py").write_text(_FIXED_ADMIN_PY)
    return _commit_all(repo, "Fix storage collision bug and admin's stale-cache bug together")


def _analyze_happy_path_fixture_dir() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    located = runfiles.Rlocation(
        "blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl"
    )
    assert located is not None
    path = Path(located).parent
    assert (path / "scenario.jsonl").exists()
    return path


def bootstrap_analyze_happy_path(blare_bin: Path, repo_dir: Path, xdg_state: Path) -> None:
    """Build a genuine, deterministic prior `.blare/` for a test that needs one,
    by replaying the already-captured, real `analyze-happy-path` fixture against
    `repo_dir` -- mirrors `tests/release/capture.py`'s `_bootstrap_analyze`,
    which does the equivalent for the release suite's own live captures via the
    same mechanism (decisions.md, 2026-08-02: "Bootstrap via replaying
    analyze-happy-path, not a fresh live call"). Deterministic and free of live-
    API cost: the resulting `.blare/` always carries `analyze-happy-path`'s own
    fixed, already-verified IDs, unlike the old bootstrap-via-fresh-live-call
    model whose IDs differed every run. Drives with `approve_all`, not a fixed
    occurrence count, because the real `analyze-happy-path` capture may fold an
    organic, model-initiated amendment into any phase's own turn (`tests/
    release/scenario_driver.py`'s own docstring), whose rejectable prompt text
    does not match a plain checkpoint's."""
    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_analyze_happy_path_fixture_dir()}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    result = approve_all(process)
    assert result.exit_code == 0, result.output


_ORPHAN_ID = "fm-orphan-injected"


def inject_unmapped_failure_mode(
    blare_root: Path, fm_id: str = _ORPHAN_ID, origin_note: str = "update-load-seeded-repair"
) -> None:
    """Hand-append a failure mode with `coverage_status: alertable` but no alert
    coverage -- duplicates `tests/release/capture.py`'s function of the same
    name (kept in sync manually, same one-way-import reason as this module's
    other duplicated content) so an e2e test can reproduce the exact same
    injected violation, under the exact same ID, that the real
    `update-load-seeded-repair` capture was taken against."""
    fm_path = blare_root / "failure-modes.yaml"
    if fm_id not in fm_path.read_text():
        with fm_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- id: {fm_id}\n"
                f"  title: hand-injected unmapped failure mode (T4.1 {origin_note} capture)\n"
                "  description: Deliberately hand-added with coverage_status alertable but"
                " no alert\n"
                "    coverage, to seed a semantic violation for a real release-suite\n"
                "    capture of a repair path.\n"
                "  severity: warning\n"
                "  user_visible: false\n"
                "  caused_by: []\n"
                "  coverage_status: alertable\n"
            )
    cov_path = blare_root / "coverage.yaml"
    if fm_id not in cov_path.read_text():
        with cov_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- failure_mode_id: {fm_id}\n"
                "  detecting_metric_ids: []\n"
                "  metric_recommendation_ids: []\n"
                "  alert_ids: []\n"
            )


__all__ = [
    "build_genesis",
    "commit_fix_evictor",
    "commit_docs_update",
    "commit_test_only_change",
    "commit_multi_commit_range",
    "commit_dynamic_expansion_delta",
    "bootstrap_analyze_happy_path",
    "inject_unmapped_failure_mode",
]
