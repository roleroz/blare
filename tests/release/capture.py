"""The release suite's scripted scenarios (T4.1): one function per entry on
`engineering/modules/agent.md`'s provisional-fixtures list, each driving a real
`blare` invocation against `~/external_git/miniflux_v2` with
`BLARE_SDK_FIXTURES=record:<dir>` and finalizing the capture into
`tests/fixtures/claude-sdk/<scenario>/scenario.jsonl`.

Every scenario's real code delta comes from checking out among miniflux's own
pre-existing commits (`miniflux_repo.checkout_commit`) -- this module never creates a
commit itself. Run directly (e.g. from a `python3 -c` snippet) during a release-suite
capture session; each captured scenario also gets a thin pytest wrapper under this
package that `bazel test --test_tag_filters=live //...` runs.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.release import miniflux_repo as mr
from tests.release.scenario_driver import (
    Capture,
    approve_to_exit,
    approve_until,
    chat_at_marker,
    finish,
    reply_at_marker,
    start_recording,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "claude-sdk"


def _locate_blare_bin() -> Path:
    """Prefer Bazel's runfiles resolution (hermetic under `bazel test`, matching
    every e2e test's own `_blare_bin()` helper); fall back to the fixed
    `bazel-bin` path this module's other absolute paths already assume, for a
    plain `python3 -c` capture session run outside Bazel."""
    try:
        from python.runfiles import Runfiles

        runfiles = Runfiles.Create()
        if runfiles is not None:
            located = runfiles.Rlocation("blare/src/blare/blare")
            if located is not None and Path(located).exists():
                return Path(located)
    except ImportError:
        pass
    return REPO_ROOT / "bazel-bin" / "src" / "blare" / "blare"


BLARE_BIN = _locate_blare_bin()

PHASE_HEADER = {1: "phase 1 —", 2: "phase 2 —", 3: "phase 3 —", 4: "phase 4 —"}


def scratch_paths(scratch_root: Path, name: str) -> tuple[Path, Path]:
    """(xdg_state, record_dir) for one scenario, both fresh."""
    base = scratch_root / name
    return base / "xdg", base / "record"


def _report(name: str, exit_code: int, output: str) -> None:
    print(f"=== {name}: exit_code={exit_code} ===")
    print(output[-3000:])
    if exit_code != 0:
        raise RuntimeError(f"{name} capture: expected exit 0, got {exit_code}")


def set_analyzed_sha(blare_root: Path, sha: str) -> None:
    """Hand-edit `state.yaml`'s `analyzed_sha` -- sanctioned by spec ("hand-editing
    the canonical YAML is supported... Blare validates the YAML on load and treats it
    as the current state"). Used to present a real, already-analyzed artifact set as
    if it had been recorded at a different real ancestor commit, so an update
    scenario's delta is whatever real range this module's caller wants to demonstrate
    -- without inventing any commit of its own."""
    state_path = blare_root / "state.yaml"
    text = state_path.read_text()
    if not re.search(r"analyzed_sha:\s*\"?[0-9a-f]+\"?", text):
        raise RuntimeError(f"analyzed_sha pattern not found in {state_path}: {text!r}")
    new_text = re.sub(r'analyzed_sha:\s*"?[0-9a-f]+"?', f"analyzed_sha: {sha}", text)
    state_path.write_text(new_text)


# ---- analyze-mode scenarios -------------------------------------------------------


def capture_analyze_happy_path(scratch_root: Path, base_sha: str) -> Capture:
    """Fresh `blare analyze` at `base_sha`: approve every real prompt to completion."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, "analyze-happy-path")
    with mr.on_commit(repo, base_sha):
        cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("analyze-happy-path", exit_code, output)
    return cap


def capture_analyze_reanalysis_noop(scratch_root: Path) -> Capture:
    """`blare analyze` again with no code change since the last real analysis --
    R16 re-analysis expected to conclude no changes needed.

    A first attempt at this scenario (no hint) found the model has no way to know
    a prior analysis exists unless it checks `.blare/` on its own initiative --
    the phase prompts never mention it -- and it produced a real but noisy
    duplicate-then-reconcile run instead of a clean no-op (kept as the real
    analyze-reanalysis-update capture). The hint below is exactly what that run's
    own model concluded it should have done, not a scripted outcome.
    """
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, "analyze-reanalysis-noop")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    chat_at_marker(
        cap,
        PHASE_HEADER[1],
        "before proposing anything, please check .blare/ in this repo for the "
        "existing prior analysis, and only propose add/update/remove edits where "
        "your own conclusions genuinely differ from what is already recorded there",
    )
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("analyze-reanalysis-noop", exit_code, output)
    return cap


def capture_analyze_reanalysis_update(scratch_root: Path, new_sha: str) -> Capture:
    """`blare analyze` again after checking out `new_sha` (real commits ahead) --
    R16 re-analysis expected to change at least one entry."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, "analyze-reanalysis-update")
    with mr.on_commit(repo, new_sha):
        cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("analyze-reanalysis-update", exit_code, output)
    return cap


def capture_analyze_checkpoint_chat(scratch_root: Path) -> Capture:
    """R2: chat right at phase 1's own checkpoint (the first prompt of the run, so
    no organic amendment can have preceded it), then approve through the rest."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, "analyze-checkpoint-chat")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    chat_at_marker(cap, PHASE_HEADER[1], "what about the auth service?")
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("analyze-checkpoint-chat", exit_code, output)
    return cap


def capture_amendment_agent(scratch_root: Path, name: str, *, approve: bool) -> Capture:
    """Approve along until phase 4's own checkpoint, then chat there to propose an
    amendment to an earlier phase; `approve` picks the approved/rejected variant at
    the amendment's re-presentation."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, name)
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    chat_at_marker(
        cap,
        PHASE_HEADER[4],
        "before we wrap up -- can you revise the system map now that we've seen the "
        "rest of the analysis?",
    )
    # The very next reply-pending prompt is the amendment's own re-presentation
    # (rejectable, agent-origin) -- approve_until's stop marker is the origin line
    # cli.py renders for it.
    reply_at_marker(cap, "amendment · proposed by agent", "approve" if approve else "reject")
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report(name, exit_code, output)
    return cap


def capture_amendment_cascade(scratch_root: Path, name: str, *, approve: bool) -> Capture:
    """Chat at phase 4's checkpoint proposes renaming a failure mode, cascading
    (via `referencing_phases`) into phase 3's metric coverage."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, name)
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    chat_at_marker(
        cap,
        PHASE_HEADER[4],
        "one of the failure mode titles from phase 2 is unclear -- can you rename it "
        "to something clearer, and update anything that references it?",
    )
    reply_at_marker(cap, "amendment · proposed by agent", "approve" if approve else "reject")
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report(name, exit_code, output)
    return cap


def capture_amendment_system(scratch_root: Path) -> Capture:
    """A re-analysis run over a `.blare/` whose loaded set already violates R4
    (hand-edited beforehand, per the caller) -- expects the *approval gate* (not
    preflight) to open a system-originated unit once all four phases freeze."""
    repo = mr.MINIFLUX_ROOT
    xdg, record = scratch_paths(scratch_root, "amendment-system")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    approve_until(cap, "amendment · invariant repair")
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("amendment-system", exit_code, output)
    return cap


# ---- update-mode scenarios ---------------------------------------------------------


def capture_update(scratch_root: Path, name: str, baseline_sha: str, target_sha: str) -> Capture:
    """Generic `blare update` capture: hand-set the loaded state's `analyzed_sha` to
    `baseline_sha`, check out `target_sha` as the new HEAD, then approve everything."""
    repo = mr.MINIFLUX_ROOT
    blare_root = mr.blare_root(repo)
    set_analyzed_sha(blare_root, baseline_sha)
    xdg, record = scratch_paths(scratch_root, name)
    with mr.on_commit(repo, target_sha):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report(name, exit_code, output)
    return cap


def capture_update_no_impact_redirect(
    scratch_root: Path, baseline_sha: str, target_sha: str, redirect_text: str
) -> Capture:
    """The no-impact confirmation, redirected via chat into an affected phase."""
    repo = mr.MINIFLUX_ROOT
    blare_root = mr.blare_root(repo)
    set_analyzed_sha(blare_root, baseline_sha)
    xdg, record = scratch_paths(scratch_root, "update-no-impact-redirect")
    with mr.on_commit(repo, target_sha):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        chat_at_marker(cap, "no changes needed", redirect_text)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("update-no-impact-redirect", exit_code, output)
    return cap


_ORPHAN_ID = "fm-orphan-injected"


def inject_unmapped_failure_mode(blare_root: Path) -> None:
    """Hand-append a failure mode with `coverage_status: alertable` but no alert
    coverage -- sanctioned by spec ("hand-editing the canonical YAML is
    supported"), seeding R18's load-time `unmapped_failure_mode` violation for a
    real capture of the proactive repair path. Idempotent per file (each of the
    two appends below is independently guarded), so a re-run after a prior run
    was interrupted between the two writes still completes the missing one
    rather than silently leaving `coverage.yaml` without its matching entry."""
    fm_path = blare_root / "failure-modes.yaml"
    if _ORPHAN_ID not in fm_path.read_text():
        with fm_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- id: {_ORPHAN_ID}\n"
                "  title: hand-injected unmapped failure mode (T4.1"
                " update-load-seeded-repair capture)\n"
                "  description: Deliberately hand-added with coverage_status alertable but"
                " no alert\n"
                "    coverage, to seed R18's load-time semantic violation for a real"
                " release-suite\n"
                "    capture of the proactive repair path.\n"
                "  severity: warning\n"
                "  user_visible: false\n"
                "  caused_by: []\n"
                "  coverage_status: alertable\n"
            )
    cov_path = blare_root / "coverage.yaml"
    if _ORPHAN_ID not in cov_path.read_text():
        with cov_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- failure_mode_id: {_ORPHAN_ID}\n"
                "  detecting_metric_ids: []\n"
                "  metric_recommendation_ids: []\n"
                "  alert_ids: []\n"
            )


def capture_update_load_seeded_repair(
    scratch_root: Path, baseline_sha: str, target_sha: str
) -> Capture:
    """The loaded state already violates R4 (hand-injected before this runs); the
    proactive post-triage repair should present before any ordinary checkpoint."""
    repo = mr.MINIFLUX_ROOT
    blare_root = mr.blare_root(repo)
    inject_unmapped_failure_mode(blare_root)
    set_analyzed_sha(blare_root, baseline_sha)
    xdg, record = scratch_paths(scratch_root, "update-load-seeded-repair")
    with mr.on_commit(repo, target_sha):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("update-load-seeded-repair", exit_code, output)
    return cap


# ---- auth-required (no miniflux involved) -----------------------------------------


def _init_scratch_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("scratch repo for the auth-required capture\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial commit"], cwd=repo, check=True)


def capture_auth_required(scratch_root: Path) -> Capture:
    """R12: a scripted scratch-`HOME` run with no Claude Code login -- the real
    handshake reports `auth_required`, exit 1. Runs against a throwaway scratch repo,
    never miniflux (this scenario needs no real codebase, only the handshake shape)."""
    scratch_repo = scratch_root / "auth-required" / "repo"
    _init_scratch_repo(scratch_repo)
    home = scratch_root / "auth-required" / "home"
    home.mkdir(parents=True, exist_ok=True)
    xdg, record = scratch_paths(scratch_root, "auth-required")
    cap = start_recording(BLARE_BIN, ["analyze"], scratch_repo, record, xdg, home=home)
    exit_code, output = finish(cap, timeout=60.0)
    print(f"=== auth-required: exit_code={exit_code} ===")
    print(output)
    if exit_code != 1:
        raise RuntimeError(f"auth-required capture: expected exit 1, got {exit_code!r}")
    return cap
