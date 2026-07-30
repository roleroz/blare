"""e2e: R10 -- a derived doc edited *during a checkpoint pause of the same run* (the
single-run construction that dodges R1's inverse refusal, since nothing exists at
that path when preflight's `init_inspection` runs) is restored to the canonical form
of the final candidate at final confirmation; the abort variant of the same setup
shows no restoration, since R20 guarantees nothing is written before that point.
"""

from __future__ import annotations

from pathlib import Path

from python.runfiles import Runfiles

from blare.artifacts import GENERATED_DOC_HEADER
from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_HAND_EDIT = f"{GENERATED_DOC_HEADER}\nhand-edited during the checkpoint pause\n"


def _start_process(blare_bin: Path, fixture_dir: Path, repo_dir: Path, xdg: Path) -> PtyProcess:
    return PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={"BLARE_SDK_FIXTURES": f"replay:{fixture_dir}", "XDG_STATE_HOME": str(xdg)},
    )


def test_e2e_derived_doc_restored_at_final_confirmation(tmp_path: Path) -> None:
    """Hand-editing a derived doc mid-run, then approving through to final
    confirmation, restores it to the canonical form of the final candidate (R10);
    the manual edit does not survive."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_dir = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl")
    ).parent
    assert blare_bin.exists()

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    doc_path = repo_dir / ".blare" / "docs" / "system-map.md"

    process = _start_process(blare_bin, fixture_dir, repo_dir, tmp_path / "xdg")
    process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    # Nothing existed at this path before the run started (a fresh analyze, no
    # state file) -- this simulates a prior manual edit within the same run,
    # exactly the construction R10's e2e criterion calls for.
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(_HAND_EDIT)
    for occurrence in (1, 2, 3, 4):
        process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    restored = doc_path.read_bytes()
    assert restored.startswith(GENERATED_DOC_HEADER.encode())
    assert b"hand-edited during the checkpoint pause" not in restored
    assert b"sm-web" in restored


def test_e2e_derived_doc_not_restored_on_abort(tmp_path: Path) -> None:
    """The abort variant of the same setup: nothing is written (R20), so the
    hand-edited derived doc survives exactly as edited."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_dir = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl")
    ).parent
    assert blare_bin.exists()

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    doc_path = repo_dir / ".blare" / "docs" / "system-map.md"

    process = _start_process(blare_bin, fixture_dir, repo_dir, tmp_path / "xdg")
    process.read_until(_CHECKPOINT_PROMPT, occurrence=1)
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(_HAND_EDIT)
    process.send_line("abort")
    result = process.read_all_until_exit()

    assert result.exit_code == 3
    assert doc_path.read_text() == _HAND_EDIT
    assert not (repo_dir / ".blare" / "state.yaml").exists()
