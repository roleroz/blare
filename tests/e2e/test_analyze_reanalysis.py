"""e2e: `blare analyze` re-run over an existing state file (R16), and the byte
stability R9 permits it to rest on -- an unchanged conclusion keeps its entry's
exact ID and bytes; a changed one gets a new serialization while every untouched
entry (siblings in the same file, and every other canonical file and derived doc)
keeps its bytes verbatim; the recorded SHA advances to the new HEAD either way.

Traces: R16, R9. `blare analyze`'s `end_sha` is `repo.head_sha()` captured at run
start (orchestrator.md, step 1) -- unlike `blare update`'s step 5, analyze mode
never checks the recorded `analyzed_sha`'s ancestry, so a second `blare analyze`
run is valid with or without a new commit in between. Both scenarios below add one
anyway: a re-analysis is only a meaningful scenario once the codebase has moved,
and it lets these tests assert the SHA actually advancing to a genuinely different
value (artifacts.md's write-primitives test plan: "an empty edit set with a new
SHA changes exactly the state file") rather than a same-SHA no-op.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import commit_file, head_sha, init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")

_CANONICAL_ENTRY_FILES = (
    "system-map.yaml",
    "failure-modes.yaml",
    "metrics.yaml",
    "metric-recommendations.yaml",
    "alert-recommendations.yaml",
    "coverage.yaml",
)
_DOC_FILES = (
    "system-map.md",
    "failure-modes.md",
    "metrics.md",
    "metric-recommendations.md",
    "alert-recommendations.md",
    "coverage.md",
)

# ruamel's block-style rendering always starts a top-level list item with its
# first field at column 0 -- `- id: ...` for every entry type but coverage,
# which is keyed by `failure_mode_id` instead (artifacts.md: "CoverageEntry has
# no id") -- confirmed against `artifacts._dump_yaml_bytes`'s own output. This is
# the anchor this module splits an entry-based file on to assert byte stability
# *per entry*, which is what R16's "entries ... keep their IDs and bytes" claims,
# not merely per whole file.
_ENTRY_START = re.compile(r"^- (?:id|failure_mode_id): ", re.MULTILINE)


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


_PROMPT_PREFIX = "$ approve"


def _run_analyze(blare_bin: Path, fixture_dir: Path, repo_dir: Path, xdg: Path) -> str:
    """Drive one `blare analyze` run to completion, approving every reply-pending
    prompt (ordinary checkpoint or amendment re-presentation alike -- both share
    the `"$ approve"` prefix). The analyze-reanalysis-update fixture is a real
    capture whose session organically folded a self-correcting `amend_proposal`
    into its own run (T4.1), so a plain four-checkpoint loop can't replay it;
    approving along the shared prefix until the process exits handles both this
    fixture and the plain four-checkpoint ones the same way."""
    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={"BLARE_SDK_FIXTURES": f"replay:{fixture_dir}", "XDG_STATE_HOME": str(xdg)},
    )
    output = ""
    for _ in range(20):
        try:
            output = process.read_until(
                _PROMPT_PREFIX, occurrence=output.count(_PROMPT_PREFIX) + 1, timeout=10.0
            )
        except TimeoutError as exc:
            if "process exited: True" in str(exc):
                break
            raise
        process.send_line("approve")
    result = process.read_all_until_exit()
    assert result.exit_code == 0, result.output
    return result.output


def _snapshot(blare_root: Path) -> dict[str, bytes]:
    """Every canonical entry file's and every derived doc's raw bytes, keyed by a
    path relative to `.blare/` (`state.yaml`/`config.yaml` are handled separately
    by each test, since one of them is expected to change)."""
    snapshot: dict[str, bytes] = {}
    for filename in _CANONICAL_ENTRY_FILES:
        snapshot[filename] = (blare_root / filename).read_bytes()
    for filename in _DOC_FILES:
        snapshot[f"docs/{filename}"] = (blare_root / "docs" / filename).read_bytes()
    return snapshot


def _hand_annotate(path: Path, marker: str, comment: str) -> None:
    """Hand-edit one canonical YAML file, inserting `comment` immediately before the
    first line equal to `marker` -- hand-editing canonical YAML is spec-sanctioned
    (Artifacts: "Hand-editing the canonical YAML is supported"). This is what
    actually exercises the surgical-merge byte-preservation mechanism
    (architecture.md, Determinism: "including hand-edited formatting") at the e2e
    level -- without it, every entry in these tests' fixtures is machine-generated
    on both sides of the comparison, and a full re-serialization would happen to
    produce identical bytes anyway, silently passing even if surgical preservation
    were removed entirely."""
    original = path.read_text()
    annotated = original.replace(marker, f"{comment}{marker}", 1)
    assert annotated != original, f"expected marker {marker!r} not found in {path}"
    path.write_text(annotated)


def _hand_annotate_metrics(blare_root: Path) -> None:
    """Hand-edit `metrics.yaml` between the two runs -- neither re-analysis fixture
    touches phase 3's metrics, so the whole file is untouched by either run's edit
    batch (the file-level skip in `write_entries_and_config`)."""
    _hand_annotate(
        blare_root / "metrics.yaml",
        "  name: miniflux_archive_entries_duration\n",
        "  # hand-verified against the codebase\n",
    )


def _hand_annotate_fm_oom(blare_root: Path) -> None:
    """Hand-edit `failure-modes.yaml`'s fm-oom-condition entry -- a *sibling*,
    within the same file as the entries the reanalysis-update fixture adds. Unlike
    `_hand_annotate_metrics` (whose file is skipped wholesale), this specifically
    exercises `_merge_entry_file`'s per-entry preserve-vs-replace branch: new
    entries land in the same file while this one must be individually preserved
    from the deep-copied loaded sequence, not merely because its containing file
    was skipped."""
    _hand_annotate(
        blare_root / "failure-modes.yaml",
        "  title: Out-of-memory condition\n",
        "  # hand note: still tracking this one\n",
    )


def _entries_by_id(raw: bytes) -> dict[str, str]:
    """Split one entry-based YAML file's raw text into its top-level list items,
    keyed by id."""
    text = raw.decode()
    starts = [m.start() for m in _ENTRY_START.finditer(text)]
    assert starts, f"no top-level entries found in:\n{text}"
    bounds = [*starts, len(text)]
    chunks = [text[bounds[i] : bounds[i + 1]] for i in range(len(starts))]
    result: dict[str, str] = {}
    for chunk in chunks:
        first_line = chunk.splitlines()[0]
        _, _, entry_id = first_line.partition(": ")
        result[entry_id.strip()] = chunk
    return result


def test_e2e_reanalysis_unchanged_conclusions_preserve_ids_and_bytes(tmp_path: Path) -> None:
    """R16/R9: a second `blare analyze` run over an existing state file, whose
    replayed session reaches no different conclusions in any phase, leaves every
    canonical entry-based file, every derived doc, and `config.yaml` byte-
    identical; only the recorded SHA advances, to the delta's new HEAD."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    _run_analyze(blare_bin, _fixture_dir("analyze-happy-path"), repo_dir, xdg_state)
    first_sha = head_sha(repo_dir)
    _hand_annotate_metrics(blare_root)
    before = _snapshot(blare_root)
    before_config = (blare_root / "config.yaml").read_bytes()
    before_ids = {
        filename: set(_entries_by_id(before[filename])) for filename in _CANONICAL_ENTRY_FILES
    }

    second_sha = commit_file(
        repo_dir, "src/extra.py", "# a later, unrelated change\n", "a later commit"
    )
    assert second_sha != first_sha

    output = _run_analyze(blare_bin, _fixture_dir("analyze-reanalysis-noop"), repo_dir, xdg_state)
    assert "0 added · 0 updated · 0 removed" in output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha
    assert state["analyzed_sha"] != first_sha

    after = _snapshot(blare_root)
    for filename, before_bytes in before.items():
        assert after[filename] == before_bytes, f"{filename} changed bytes on a no-op re-analysis"
    for filename in _CANONICAL_ENTRY_FILES:
        assert set(_entries_by_id(after[filename])) == before_ids[filename]
    assert (blare_root / "config.yaml").read_bytes() == before_config
    assert b"# hand-verified against the codebase" in after["metrics.yaml"]


def test_e2e_reanalysis_changed_conclusion_adds_new_entries_only(tmp_path: Path) -> None:
    """R16/R9: a second `blare analyze` run whose replayed session finds four new
    failure modes (plus their metric recommendations, alerts, and coverage
    entries) adds exactly those -- every entry from the first run keeps its exact
    ID and bytes, siblings included -- while the recorded SHA still advances to
    the delta's new HEAD."""
    blare_bin = _blare_bin()
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"
    blare_root = repo_dir / ".blare"

    _run_analyze(blare_bin, _fixture_dir("analyze-happy-path"), repo_dir, xdg_state)
    first_sha = head_sha(repo_dir)
    _hand_annotate_metrics(blare_root)
    _hand_annotate_fm_oom(blare_root)
    before = _snapshot(blare_root)
    before_config = (blare_root / "config.yaml").read_bytes()
    before_fm_entries = _entries_by_id(before["failure-modes.yaml"])
    assert "# hand note: still tracking this one" in before_fm_entries["fm-oom-condition"]

    second_sha = commit_file(
        repo_dir, "src/extra.py", "# a later, unrelated change\n", "a later commit"
    )
    assert second_sha != first_sha

    output = _run_analyze(
        blare_bin, _fixture_dir("analyze-reanalysis-update"), repo_dir, xdg_state
    )
    assert "12 added · 0 updated · 0 removed" in output

    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == second_sha
    assert state["analyzed_sha"] != first_sha

    after = _snapshot(blare_root)

    # system-map.yaml and metrics.yaml are untouched by this delta (the file-level
    # skip); every other canonical file gains new entries but keeps every prior
    # entry byte-stable.
    assert after["system-map.yaml"] == before["system-map.yaml"]
    assert after["metrics.yaml"] == before["metrics.yaml"]
    assert b"# hand-verified against the codebase" in after["metrics.yaml"]

    after_fm_entries = _entries_by_id(after["failure-modes.yaml"])
    new_failure_modes = {
        "fm-oauth2-provider-init-failure",
        "fm-maintenance-mode-stuck-enabled",
        "fm-auth-proxy-trust-misconfiguration",
        "fm-premature-session-expiry",
    }
    assert set(after_fm_entries) == set(before_fm_entries) | new_failure_modes
    for entry_id, entry_bytes in before_fm_entries.items():
        assert after_fm_entries[entry_id] == entry_bytes, f"{entry_id} changed unexpectedly"
    assert "# hand note: still tracking this one" in after_fm_entries["fm-oom-condition"]

    after_mr = _entries_by_id(after["metric-recommendations.yaml"])
    before_mr = _entries_by_id(before["metric-recommendations.yaml"])
    assert set(after_mr) == set(before_mr) | {
        "mr-oauth2-provider-init-status-gauge",
        "mr-maintenance-mode-gauge",
    }
    for entry_id, entry_bytes in before_mr.items():
        assert after_mr[entry_id] == entry_bytes, f"{entry_id} changed unexpectedly"

    after_ar = _entries_by_id(after["alert-recommendations.yaml"])
    before_ar = _entries_by_id(before["alert-recommendations.yaml"])
    assert set(after_ar) == set(before_ar) | {
        "ar-oauth2-provider-not-ready",
        "ar-maintenance-mode-stuck",
    }
    for entry_id, entry_bytes in before_ar.items():
        assert after_ar[entry_id] == entry_bytes, f"{entry_id} changed unexpectedly"

    after_cov = _entries_by_id(after["coverage.yaml"])
    before_cov = _entries_by_id(before["coverage.yaml"])
    assert set(after_cov) == set(before_cov) | new_failure_modes
    for entry_id, entry_bytes in before_cov.items():
        assert after_cov[entry_id] == entry_bytes, f"{entry_id} changed unexpectedly"

    assert (blare_root / "config.yaml").read_bytes() == before_config

    # failure-modes.md, metric-recommendations.md, alert-recommendations.md, and
    # coverage.md are the only derived docs whose bytes changed (R9/R10: unchanged
    # YAML renders byte-identically even though every doc is rewritten).
    changed_docs = {
        "failure-modes.md",
        "metric-recommendations.md",
        "alert-recommendations.md",
        "coverage.md",
    }
    for doc_filename in _DOC_FILES:
        key = f"docs/{doc_filename}"
        if doc_filename in changed_docs:
            assert after[key] != before[key]
        else:
            assert after[key] == before[key], f"{key} changed unexpectedly"
