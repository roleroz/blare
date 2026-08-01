"""e2e: R18's redirect at the no-impact confirmation (T3.2) -- triage
concludes `no_impact`; chat at that confirmation issues a bare
`affected_verdict` naming a phase, withdrawing the conclusion for good (no
amendment involved, so there is no reject/restore path back to it). The
withdrawn conclusion's own prompt is mooted (never re-offered), and the newly
opened phase gets its own ordinary checkpoint before the write.

T4.1: this fixture is a release-suite capture of the live Claude Agent SDK
against a real `~/external_git/miniflux_v2` delta (the single commit
`2717336d`, a man-page wording clarification for `POLLING_FREQUENCY` and
`TRUSTED_REVERSE_PROXY_NETWORKS`). The real triage concluded `no_impact`; a
directive chat redirect ("an operator could have misread the previously
undocumented unit -- open phase 2 for this") made the real model withdraw
that conclusion via a bare `affected_verdict` naming phase 2, where it added
`fm-polling-frequency-unit-misconfiguration` (excluded, not alertable, since
the misconfigured tick is indistinguishable at runtime from a deliberately
chosen one). `tests/e2e/testdata/update_no_impact_redirect/` holds byte-exact
copies of the real file's content at the delta's two endpoints so this
synthetic repo's own `git diff` reproduces the real capture's recorded
`patch_text` byte-for-byte, and the chat text sent below matches the real
capture's recorded outbound chat event byte-for-byte (the replaying client
compares every outbound message, not only triage's).

Traces `engineering/architecture.md`'s T3.2 scope: "a redirect path when chat
happens during the no-impact confirmation ... Traces: ... R18 (dynamic
clauses)".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import head_sha, init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")
_MINIFLUX1_PATH = "miniflux.1"
_REDIRECT_TEXT = (
    "wait -- if POLLING_FREQUENCY's unit was never documented before this commit, "
    "an operator could genuinely have configured it believing it meant seconds "
    "rather than minutes, causing far slower feed refreshes than intended. I think "
    "that's worth its own failure mode in phase 2 (operator misconfiguration due to "
    "the previously ambiguous unit) -- can you open phase 2 for this delta?"
)


def _load_yaml(path: Path) -> Any:
    # A YAML file's shape isn't statically known; this test's own assertions below
    # are the real type check.
    return _YAML.load(path.read_bytes())


def _blare_bin() -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation("blare/src/blare/blare"))
    assert path.exists()
    return path


def _fixture_dir(name: str) -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(runfiles.Rlocation(f"blare/tests/fixtures/claude-sdk/{name}/scenario.jsonl")).parent
    assert (path / "scenario.jsonl").exists()
    return path


def _testdata(name: str) -> Path:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = Path(
        runfiles.Rlocation(f"blare/tests/e2e/testdata/update_no_impact_redirect/{name}")
    )
    assert path.exists()
    return path


def _write_valid_update_state(blare_root: Path, analyzed_sha: str) -> None:
    """A structurally and semantically valid `.blare/`: one excluded failure
    mode, so step 7's semantic check seeds nothing -- only the redirect
    mechanism itself is under test here."""
    blare_root.mkdir(parents=True, exist_ok=True)
    (blare_root / "state.yaml").write_text(
        f'analyzed_sha: "{analyzed_sha}"\nschema_version: 1\n'
    )
    (blare_root / "config.yaml").write_text("stack: prometheus\n")
    (blare_root / "system-map.yaml").write_text("[]\n")
    (blare_root / "failure-modes.yaml").write_text(
        "- id: fm-timeout\n"
        "  title: upstream timeout\n"
        "  description: a call to an upstream service times out\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: excluded\n"
        "  exclusion_reason: not independently detectable\n"
    )
    (blare_root / "metrics.yaml").write_text("[]\n")
    (blare_root / "metric-recommendations.yaml").write_text("[]\n")
    (blare_root / "alert-recommendations.yaml").write_text("[]\n")
    (blare_root / "coverage.yaml").write_text(
        "- failure_mode_id: fm-timeout\n"
        "  detecting_metric_ids: []\n"
        "  metric_recommendation_ids: []\n"
        "  alert_ids: []\n"
    )


def _commit_miniflux1(repo_dir: Path, testdata_name: str, message: str) -> str:
    """Write the real file's exact byte content (copied from the real
    capture's two endpoint commits) at the same relative path, then commit --
    so this repo's own diff between the two commits is byte-identical to the
    real capture's recorded `patch_text`."""
    dest = repo_dir / _MINIFLUX1_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_testdata(testdata_name), dest)
    subprocess.run(["git", "add", _MINIFLUX1_PATH], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo_dir, check=True)
    return head_sha(repo_dir)


def test_e2e_update_no_impact_redirect_withdraws_conclusion(tmp_path: Path) -> None:
    """The no-impact screen is shown for a real docs-only delta, a directive
    chat redirect withdraws it into phase 2, and that phase's own ordinary
    checkpoint is what the run pauses at next -- the no-impact prompt itself
    never returns."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"
    # See test_update_happy_path.py's docstring: pins the abbreviated
    # object-hash length `git diff`'s "index a..b" line uses to match the real
    # miniflux_v2 checkout's (8 chars) rather than this tiny repo's default.
    subprocess.run(["git", "config", "core.abbrev", "8"], cwd=repo_dir, check=True)

    first_sha = _commit_miniflux1(
        repo_dir, "miniflux1_before.1", "add miniflux.1 (pre-delta content)"
    )
    _write_valid_update_state(blare_root, first_sha)

    second_sha = _commit_miniflux1(
        repo_dir,
        "miniflux1_after.1",
        "docs: Specify unit of time for POLLING_FREQUENCY and list-separation "
        "for TRUSTED_REVERSE_PROXY_NETWORKS",
    )
    assert second_sha != first_sha

    process = PtyProcess(
        [str(blare_bin), "update"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{_fixture_dir('update-no-impact-redirect')}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    # Occurrence 1: the no-impact confirmation itself.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    assert "no changes needed" in output
    assert _MINIFLUX1_PATH in output
    process.send_line(_REDIRECT_TEXT)
    # Occurrence 2: phase 2's own ordinary checkpoint -- the no-impact prompt
    # never comes back around.
    output = process.read_until(_CHECKPOINT_PROMPT, occurrence=2)
    assert "phase 2 " in output
    # "no changes needed" only ever appeared once (occurrence 1's own header),
    # never repeated for a second (stale) presentation of the conclusion.
    assert output.count("no changes needed") == 1
    process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0, result.output
    assert "update complete" in result.output
    # 2: the new failure_modes entry plus its mechanically-created coverage row.
    assert "2 added · 0 updated · 0 removed" in result.output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha

    failure_modes = {fm["id"]: fm for fm in _load_yaml(blare_root / "failure-modes.yaml")}
    assert failure_modes["fm-polling-frequency-unit-misconfiguration"]["coverage_status"] == (
        "excluded"
    )
    assert failure_modes["fm-polling-frequency-unit-misconfiguration"]["exclusion_reason"]
