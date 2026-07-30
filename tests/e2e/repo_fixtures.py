"""Shared repo/`.blare/` construction helpers for the T2.2 preflight refusal e2e
tests. Factored out because most of these tests need the same minimal, structurally
valid starting point (a one-commit repo, optionally with a fresh `.blare/`) before
introducing the one thing that should make preflight refuse.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ENTRY_FILENAMES = (
    "system-map.yaml",
    "failure-modes.yaml",
    "metrics.yaml",
    "metric-recommendations.yaml",
    "alert-recommendations.yaml",
    "coverage.yaml",
)


def init_repo(repo_dir: Path) -> None:
    """A git repo with one commit -- the minimum every preflight refusal test but
    R11's own clauses needs to get past step 1."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial commit"], cwd=repo_dir, check=True)


def head_sha(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit_file(repo_dir: Path, relative_path: str, content: str, message: str) -> str:
    """Write `content` to `relative_path` (outside `.blare/`) and commit it -- T2.5's
    re-analysis scenarios use this to give the second `blare analyze` invocation a
    genuinely new HEAD to record, the realistic re-analysis trigger (a code change
    between runs), distinct from the state file's own recorded SHA that `blare
    analyze` never consults (unlike `blare update`, it has no ancestry check)."""
    path = repo_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", relative_path], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def write_minimal_state(blare_root: Path, analyzed_sha: str, schema_version: int = 1) -> None:
    """A minimal, structurally valid `.blare/`: every entry file an empty list."""
    blare_root.mkdir(parents=True, exist_ok=True)
    (blare_root / "state.yaml").write_text(
        f'analyzed_sha: "{analyzed_sha}"\nschema_version: {schema_version}\n'
    )
    for filename in _ENTRY_FILENAMES:
        (blare_root / filename).write_text("[]\n")
