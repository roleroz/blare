"""e2e: R21 -- two Blare processes started by the same user cannot run against the
same repo concurrently: the second invocation exits non-zero naming the running
one.

Simulates contention by pre-placing a lock file owned by this test process's own
PID (guaranteed alive for the test's duration) at the exact path the orchestrator
computes (`$XDG_STATE_HOME/blare/<repo-id>/lock`), rather than actually racing two
`blare` processes.

Traces `engineering/architecture.md`'s T2.2 scope: "Traces: ... R21, R22, ...".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from python.runfiles import Runfiles

from blare.gitrepo import GitRepo
from tests.e2e.pty_harness import run_blare_noninteractive
from tests.e2e.repo_fixtures import init_repo


def test_e2e_refuses_when_lock_held(tmp_path: Path) -> None:
    """A pre-existing lock owned by a live PID makes `blare analyze` exit 1, naming
    that PID."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert blare_bin.exists(), f"blare binary not found via Rlocation at {blare_bin}"

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    state_home = tmp_path / "xdg"

    repo_id = GitRepo.discover(repo_dir).repo_id()
    lock_dir = state_home / "blare" / repo_id
    lock_dir.mkdir(parents=True)
    own_pid = os.getpid()
    (lock_dir / "lock").write_text(json.dumps({"pid": own_pid, "started_at": "test"}))

    result = run_blare_noninteractive(
        blare_bin, ["analyze"], cwd=repo_dir, env={"XDG_STATE_HOME": str(state_home)}
    )

    assert result.exit_code == 1
    assert str(own_pid) in result.output
