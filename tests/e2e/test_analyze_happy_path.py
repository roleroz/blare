"""e2e: the full `blare analyze` happy path over the analyze-happy-path replay
fixture -- fresh analyze (R1), a failure-mode chain (R3), coverage and alert
recommendations (R4, R5), and the R13/R14 summary/transcript content.

T2.3's e2e scope (architecture.md): "fresh analyze (R1), ... chains (R3), coverage
and alerts (R4, R5), ... summaries (R13, R14)".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python.runfiles import Runfiles
from ruamel.yaml import YAML

from tests.e2e.pty_harness import PtyProcess
from tests.e2e.repo_fixtures import head_sha, init_repo

_CHECKPOINT_PROMPT = "$ approve · abort · anything else is chat"
_YAML = YAML(typ="safe")


def _load_yaml(path: Path) -> Any:
    # A YAML file's shape isn't statically known; this test's own assertions below
    # are the real type check.
    return _YAML.load(path.read_bytes())


def test_e2e_analyze_happy_path(tmp_path: Path) -> None:
    """Four checkpoints, each approved; the full artifact set lands on disk with a
    failure-mode chain, an excluded failure mode, a metric-gap recommendation, and
    alert recommendations linked through coverage; the summary states real entry
    and gap counts plus the transcript path."""
    runfiles = Runfiles.Create()
    assert runfiles is not None
    blare_bin = Path(runfiles.Rlocation("blare/src/blare/blare"))
    fixture_file = Path(
        runfiles.Rlocation("blare/tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl")
    )
    assert blare_bin.exists()
    assert fixture_file.exists()

    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    xdg_state = tmp_path / "xdg"

    process = PtyProcess(
        [str(blare_bin), "analyze"],
        cwd=repo_dir,
        env={
            "BLARE_SDK_FIXTURES": f"replay:{fixture_file.parent}",
            "XDG_STATE_HOME": str(xdg_state),
        },
    )
    for occurrence in (1, 2, 3, 4):
        output = process.read_until(_CHECKPOINT_PROMPT, occurrence=occurrence)
        # Each checkpoint names its own phase header, in order (R2: "presents that
        # phase's results").
        assert f"phase {occurrence} " in output
        process.send_line("approve")
    result = process.read_all_until_exit()

    assert result.exit_code == 0
    # R13: the summary states counts and the current gap count.
    assert "analysis complete" in result.output
    assert "11 added · 0 updated · 0 removed" in result.output
    assert "1 alertable · 1 metric-gap · 1 excluded" in result.output
    # R14: the transcript path is stated, and it exists.
    transcript_dir = xdg_state / "blare"
    [repo_id_dir] = list(transcript_dir.iterdir())
    [transcript_path] = list((repo_id_dir / "transcripts").glob("*.jsonl"))
    assert str(transcript_path) in result.output
    assert transcript_path.is_file()

    blare_root = repo_dir / ".blare"

    # R1: the full artifact set, a default config, every entry with a stable ID,
    # the state recording the analyzed SHA.
    state = _load_yaml(blare_root / "state.yaml")
    assert state["analyzed_sha"] == head_sha(repo_dir)
    config = _load_yaml(blare_root / "config.yaml")
    assert config["stack"] == "prometheus"

    failure_modes = {
        fm["id"]: fm for fm in _load_yaml(blare_root / "failure-modes.yaml")
    }
    assert set(failure_modes) == {"fm-timeout", "fm-503", "fm-slow"}

    # R3: fm-503 is user-visible and caused_by fm-timeout, itself a documented entry
    # with its own severity and visibility.
    assert failure_modes["fm-503"]["user_visible"] is True
    assert failure_modes["fm-503"]["caused_by"] == ["fm-timeout"]
    assert failure_modes["fm-timeout"]["user_visible"] is False
    assert failure_modes["fm-timeout"]["severity"] == "warning"

    # R4/R5: coverage status split, alert recommendations linked via coverage.
    assert failure_modes["fm-timeout"]["coverage_status"] == "excluded"
    assert failure_modes["fm-timeout"]["exclusion_reason"]
    assert failure_modes["fm-503"]["coverage_status"] == "alertable"
    assert failure_modes["fm-slow"]["coverage_status"] == "metric-gap"

    metric_recommendations = {
        mr["id"]: mr
        for mr in _load_yaml(blare_root / "metric-recommendations.yaml")
    }
    assert metric_recommendations["mr-latency"]["kind"] == "new"
    assert metric_recommendations["mr-latency"]["failure_mode_ids"] == ["fm-slow"]

    alerts = {
        ar["id"]: ar
        for ar in _load_yaml(blare_root / "alert-recommendations.yaml")
    }
    assert alerts["ar-503"]["failure_mode_ids"] == ["fm-503"]
    assert alerts["ar-503"]["severity"] == "critical"
    assert alerts["ar-slow"]["failure_mode_ids"] == ["fm-slow"]

    coverage = {
        c["failure_mode_id"]: c for c in _load_yaml(blare_root / "coverage.yaml")
    }
    assert coverage["fm-503"]["detecting_metric_ids"] == ["mx-errors"]
    assert coverage["fm-503"]["alert_ids"] == ["ar-503"]
    assert coverage["fm-timeout"]["detecting_metric_ids"] == []
    assert coverage["fm-timeout"]["alert_ids"] == []

    # R10: every derived doc carries the generated-file header.
    for doc_name in (
        "system-map.md",
        "failure-modes.md",
        "metrics.md",
        "metric-recommendations.md",
        "alert-recommendations.md",
        "coverage.md",
    ):
        doc_bytes = (blare_root / "docs" / doc_name).read_bytes()
        assert doc_bytes.startswith(b"<!-- Generated by blare. Do not edit")
