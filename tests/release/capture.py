"""The release suite's scripted scenarios (T4.1): one function per entry on
`engineering/modules/agent.md`'s provisional-fixtures list, each driving a real
`blare` invocation against a fresh `testdata/kvstore` repo (`tests/release/
kvstore_repo.py`) with `BLARE_SDK_FIXTURES=record:<dir>` and finalizing the
capture into `tests/fixtures/claude-sdk/<scenario>/scenario.jsonl`.

Every capture function builds its own fresh kvstore repo at the start of the
call (`kvstore_repo.build`), inside its caller's own `scratch_root` (always a
test's own `tmp_path`, isolated per Bazel test action -- confirmed empirically,
decisions.md 2026-08-01). Every scenario that needs a prior analyzed state
before doing whatever it actually demonstrates now bootstraps that state by
*replaying* the already-captured `analyze-happy-path` fixture at the repo's
`genesis` commit (`_bootstrap_analyze`) rather than making a fresh live `blare
analyze` call -- deterministic, free of live-API cost, and the resulting
`.blare/` always carries `analyze-happy-path`'s own fixed, already-verified
IDs instead of whatever a fresh live session happened to invent this time
(decisions.md, 2026-08-02: "Bootstrap via replaying analyze-happy-path, not a
fresh live call"). This replaces the old model of navigating a single,
shared, external checkout (`miniflux_repo.py`) where every non-fresh scenario
depended on `test_capture_analyze_happy_path` having already run, in order, in
the same release-suite session; that implicit run-order requirement, and the
`exclusive` bazel tag it required, are both gone now that every capture is
fully self-contained (architecture.md, Test strategy).

Run directly (e.g. from a `python3 -c` snippet) during a release-suite capture
session; each captured scenario also gets a thin pytest wrapper under this
package that `bazel test --test_tag_filters=live //...` runs.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from tests.release import kvstore_repo
from tests.release.scenario_driver import (
    PROMPT_PREFIX,
    Capture,
    approve_to_exit,
    approve_until,
    chat_at_marker,
    finish,
    reply_at_marker,
    start_recording,
    start_replaying,
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


def _bootstrap_analyze(scratch_root: Path, repo: Path) -> None:
    """Reconstruct a genuine prior `.blare/` for a scenario to build on, by
    replaying the already-captured, real `analyze-happy-path` fixture at
    `repo`'s current commit (always `genesis`, since callers run this
    immediately after `kvstore_repo.build`) -- instead of making a fresh live
    `blare analyze` call. No recording is produced or discarded here (unlike
    the old live-bootstrap model): replay mode writes nothing, and the
    resulting `.blare/` always carries `analyze-happy-path`'s own fixed,
    already-verified IDs rather than whatever a fresh live session happened to
    invent this time (decisions.md, 2026-08-02: "Bootstrap via replaying
    analyze-happy-path, not a fresh live call")."""
    xdg, scratch = scratch_paths(scratch_root, "bootstrap")
    cap = start_replaying(
        BLARE_BIN, ["analyze"], repo, FIXTURES_ROOT / "analyze-happy-path", scratch, xdg
    )
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("bootstrap-analyze", exit_code, output)


# ---- analyze-mode scenarios -------------------------------------------------------


def capture_analyze_happy_path(scratch_root: Path) -> Capture:
    """Fresh `blare analyze` over a newly built kvstore repo at its `genesis`
    commit: approve every real prompt to completion. No bootstrap needed --
    this run itself establishes the first-ever `.blare/`."""
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
    xdg, record = scratch_paths(scratch_root, "analyze-happy-path")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("analyze-happy-path", exit_code, output)
    return cap


def capture_analyze_reanalysis_noop(scratch_root: Path) -> Capture:
    """Bootstrap a real analysis at `genesis`, then run `blare analyze` again
    with no code change since -- R16 re-analysis expected to conclude no
    changes needed.

    A first attempt at this scenario (no hint) found the model has no way to
    know a prior analysis exists unless it checks `.blare/` on its own
    initiative -- the phase prompts never mention it -- and it produced a real
    but noisy duplicate-then-reconcile run instead of a clean no-op (kept as
    the real analyze-reanalysis-update capture). The hint below is exactly
    what that run's own model concluded it should have done, not a scripted
    outcome.
    """
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
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


def capture_analyze_reanalysis_update(scratch_root: Path, new_sha_name: str) -> Capture:
    """Bootstrap a real analysis at `genesis`, check out `shas[new_sha_name]`
    (real commits ahead, e.g. `"fix_evictor"`), then run `blare analyze` again
    -- R16 re-analysis expected to change at least one entry."""
    repo = scratch_root / "repo"
    shas = kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    xdg, record = scratch_paths(scratch_root, "analyze-reanalysis-update")
    with kvstore_repo.on_commit(repo, shas[new_sha_name]):
        cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("analyze-reanalysis-update", exit_code, output)
    return cap


def capture_analyze_checkpoint_chat(scratch_root: Path) -> Capture:
    """R2: fresh `blare analyze` over a newly built kvstore repo at `genesis`;
    chat right at phase 1's own checkpoint (the first prompt of the run, so no
    organic amendment can have preceded it), then approve through the rest."""
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
    xdg, record = scratch_paths(scratch_root, "analyze-checkpoint-chat")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    chat_at_marker(
        cap,
        PHASE_HEADER[1],
        "what about the admin write path in admin.py -- it's not reachable from "
        "api.py's public surface at all, does that matter here?",
    )
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("analyze-checkpoint-chat", exit_code, output)
    return cap


def capture_amendment_agent(scratch_root: Path, name: str, *, approve: bool) -> Capture:
    """Fresh `blare analyze` over a newly built kvstore repo at `genesis`;
    approve along until phase 4's own checkpoint, then chat there to propose an
    amendment to an earlier phase; `approve` picks the approved/rejected
    variant at the amendment's re-presentation."""
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
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
    """Fresh `blare analyze` over a newly built kvstore repo at `genesis`; chat
    at phase 4's checkpoint proposes renaming a failure mode, cascading (via
    `referencing_phases`) into phase 3's metric coverage."""
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
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
    """Build a fresh kvstore repo and bootstrap a real analysis at `genesis` to
    get a genuine `.blare/`, hand-inject an unmapped failure mode into it (R4
    violation), then run `blare analyze` again (a real re-analysis) -- expects
    the *approval gate* (not preflight) to open a system-originated unit once
    all four phases freeze.

    Each capture now gets its own private repo/`.blare/`, so unlike the
    miniflux-era version there is no longer a need for a distinctly-named
    injected ID to avoid colliding with another scenario's own injection into
    a shared checkout -- the default `fm_id` is fine here."""
    repo = scratch_root / "repo"
    kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    blare_root = kvstore_repo.blare_root(repo)
    inject_unmapped_failure_mode(blare_root)
    xdg, record = scratch_paths(scratch_root, "amendment-system")
    cap = start_recording(BLARE_BIN, ["analyze"], repo, record, xdg)
    approve_until(cap, "amendment · invariant repair")
    approve_to_exit(cap)
    exit_code, output = finish(cap)
    _report("amendment-system", exit_code, output)
    return cap


# ---- update-mode scenarios ---------------------------------------------------------


def capture_update(scratch_root: Path, name: str, target_name: str) -> Capture:
    """Generic `blare update` capture: build a fresh kvstore repo, bootstrap a
    real analysis at `genesis`, check out `shas[target_name]` as the new HEAD,
    then approve everything. Baseline is always implicitly `genesis` -- the
    bootstrap analysis establishes it for real, so there is no separate
    baseline parameter or hand-edited `analyzed_sha` left to pass."""
    repo = scratch_root / "repo"
    shas = kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    xdg, record = scratch_paths(scratch_root, name)
    with kvstore_repo.on_commit(repo, shas[target_name]):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report(name, exit_code, output)
    return replace(cap, target_sha=shas[target_name])


def capture_update_dynamic_expansion(scratch_root: Path, target_name: str) -> Capture:
    """`blare update` with a chat nudge at the very first checkpoint (whichever
    phase triage actually names), asking the model to reconsider whether the
    delta also touches other phases -- R18's dynamic expansion (a revised
    `affected_verdict`, no amendment) is not otherwise scriptable from outside
    a phase's own turn, the same reasoning `capture_amendment_agent`'s chat
    nudge already relies on for organic, model-initiated mechanisms. Bootstraps
    a real analysis at `genesis` first, then checks out `shas[target_name]`
    (the caller passes `"dynamic_expansion_delta"`, kvstore's candidate for
    this scenario: a storage-collision fix and a stale-cache fix bundled into
    one commit, spanning two distinct failure domains)."""
    repo = scratch_root / "repo"
    shas = kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    xdg, record = scratch_paths(scratch_root, "update-dynamic-expansion")
    with kvstore_repo.on_commit(repo, shas[target_name]):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        chat_at_marker(
            cap,
            PROMPT_PREFIX,
            "before you approve -- given everything you've seen while working on "
            "this delta, please double check whether it also requires revisiting "
            "any other phase (system map, failure modes, metric coverage, or "
            "alert recommendations) beyond what triage originally named; if so, "
            "call run_control with a bare affected_verdict naming it now, before "
            "finishing this phase",
        )
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("update-dynamic-expansion", exit_code, output)
    return replace(cap, target_sha=shas[target_name])


def capture_update_no_impact_redirect(
    scratch_root: Path, target_name: str, redirect_text: str
) -> Capture:
    """The no-impact confirmation, redirected via chat into an affected phase.
    Bootstraps a real analysis at `genesis` first, then checks out
    `shas[target_name]` (the caller passes `"docs_update"`)."""
    repo = scratch_root / "repo"
    shas = kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    xdg, record = scratch_paths(scratch_root, "update-no-impact-redirect")
    with kvstore_repo.on_commit(repo, shas[target_name]):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        chat_at_marker(cap, "no changes needed", redirect_text)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("update-no-impact-redirect", exit_code, output)
    return replace(cap, target_sha=shas[target_name])


_ORPHAN_ID = "fm-orphan-injected"


def inject_unmapped_failure_mode(
    blare_root: Path, fm_id: str = _ORPHAN_ID, origin_note: str = "update-load-seeded-repair"
) -> None:
    """Hand-append a failure mode with `coverage_status: alertable` but no alert
    coverage -- sanctioned by spec ("hand-editing the canonical YAML is
    supported"), seeding a semantic violation for a real capture of a repair
    path. `fm_id`/`origin_note` let distinct scenarios inject their own
    independently-idempotent entry into a `.blare/` without colliding or
    re-triggering each other's already-resolved injection (kept for scenarios
    that might inject more than once into the same `.blare/`, though each
    capture now builds its own private repo, so cross-scenario collision is no
    longer the reason this matters -- idempotency within a single scenario
    still is). Idempotent per file (each of the two appends below is
    independently guarded), so a re-run after a prior run was interrupted
    between the two writes still completes the missing one rather than
    silently leaving `coverage.yaml` without its matching entry."""
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


def capture_update_load_seeded_repair(scratch_root: Path, target_name: str) -> Capture:
    """The loaded state already violates R4 (hand-injected right after the
    bootstrap analysis, before the delta is checked out); the proactive
    post-triage repair should present before any ordinary checkpoint. The
    delta's own content doesn't matter -- the violation is hand-seeded and the
    repair fires regardless of what triage concludes, R18 -- caller passes
    `"docs_update"`."""
    repo = scratch_root / "repo"
    shas = kvstore_repo.build(repo)
    _bootstrap_analyze(scratch_root, repo)
    blare_root = kvstore_repo.blare_root(repo)
    inject_unmapped_failure_mode(blare_root)
    xdg, record = scratch_paths(scratch_root, "update-load-seeded-repair")
    with kvstore_repo.on_commit(repo, shas[target_name]):
        cap = start_recording(BLARE_BIN, ["update"], repo, record, xdg)
        approve_to_exit(cap)
        exit_code, output = finish(cap)
    _report("update-load-seeded-repair", exit_code, output)
    return replace(cap, target_sha=shas[target_name])


# ---- auth-required (no target codebase involved) ----------------------------------


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
    handshake reports `auth_required`, exit 1. Runs against a throwaway scratch
    repo, never kvstore (this scenario needs no real codebase, only the
    handshake shape)."""
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
