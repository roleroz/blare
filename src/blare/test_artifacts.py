"""Unit tests for blare.artifacts (read side T1.4, write side T1.5), per
engineering/modules/artifacts.md's test plan.

Two sets, per the global testing rules: `test_contract_*` covers the module's promised
behaviour with its dependency (a FakeStack substituted for the real stack registry
entry) behaving normally; `test_failure_*` covers what happens when the filesystem or
the stack misbehave.

Fixture convention: `_write_set` builds a minimal, fully valid `.blare/` tree -- one
`alertable` failure mode (`fm-timeout`) with full, consistent coverage, and one
`excluded` failure mode (`fm-accepted`) with empty coverage -- via keyword overrides.
Each test starts from that baseline and mutates exactly the file/field(s) its scenario
needs to break, so the resulting violation or refusal is attributable to one cause.
The write-side tests below (batch_check, apply, referencing_phases, render_docs,
raw_bytes_match, the write primitives) reuse this same baseline via `_load_default`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from ruamel.yaml import YAML

import blare.artifacts as artifacts_module
import blare.stack as stack_module
from blare.artifacts import (
    GENERATED_DOC_HEADER,
    ArtifactSet,
    ConfigError,
    CoverageEntry,
    GapSummary,
    PreexistingFilesError,
    SchemaVersionError,
    StateMissingError,
    StructuralValidationError,
    WriteError,
    apply,
    batch_check,
    empty_set,
    gap_counts,
    init_inspection,
    load,
    raw_bytes_match,
    referencing_phases,
    render_docs,
    semantic_violations,
    state_exists,
    write_docs,
    write_entries_and_config,
    write_state,
)
from blare.model import (
    BatchVerdict,
    Edit,
    EditBatch,
    EditOp,
    Phase,
    RunMode,
    Violation,
    ViolationKind,
)
from blare.stack import (
    AlertRuleInput,
    ExpressionVerdict,
    ObservabilityStack,
    UnsupportedStackError,
)

_DUMPER = YAML(typ="rt")


class FakeStack(ObservabilityStack):
    """A pure verdict table (artifacts.md's fake), installed by monkeypatching the stack
    registry -- `load`/`empty_set` resolve the stack internally, so there is no call site
    to patch directly."""

    name = "fake"

    def __init__(
        self,
        *,
        invalid_exprs: frozenset[str] = frozenset(),
        raise_on: frozenset[str] = frozenset(),
    ) -> None:
        self._invalid_exprs = invalid_exprs
        self._raise_on = raise_on

    def instrumentation_hints(self) -> str:
        return "fake instrumentation hints"

    def alerting_hints(self) -> str:
        return "fake alerting hints"

    def validate_expression(self, expr: str) -> ExpressionVerdict:
        if expr in self._raise_on:
            raise RuntimeError("fake stack blew up")
        if expr in self._invalid_exprs:
            return ExpressionVerdict(ok=False, message="fake: invalid expression")
        return ExpressionVerdict(ok=True, message=None)

    def validate_rule_fields(self, alert: AlertRuleInput) -> ExpressionVerdict:
        return ExpressionVerdict(ok=True, message=None)

    def alert_rule_shape(self, alert: AlertRuleInput) -> dict[str, object]:
        return {}


# --- Fixture baseline: a minimal, fully valid `.blare/` tree -------------------------


def _default_state() -> dict[str, object]:
    return {"analyzed_sha": "a" * 40, "schema_version": 1}


def _default_config() -> dict[str, object]:
    return {"stack": "prometheus"}


def _default_system_components() -> list[dict[str, object]]:
    return [
        {
            "id": "sm-web",
            "name": "Web API",
            "kind": "service",
            "description": "Serves HTTP requests.",
            "depends_on": [],
        }
    ]


def _default_failure_modes() -> list[dict[str, object]]:
    return [
        {
            "id": "fm-timeout",
            "title": "Request timeout",
            "description": "An upstream call times out.",
            "severity": "critical",
            "user_visible": True,
            "caused_by": [],
            "coverage_status": "alertable",
        },
        {
            "id": "fm-accepted",
            "title": "Accepted risk",
            "description": "A risk we knowingly accept.",
            "severity": "warning",
            "user_visible": False,
            "caused_by": [],
            "coverage_status": "excluded",
            "exclusion_reason": "Low impact, accepted.",
        },
    ]


def _default_metrics() -> list[dict[str, object]]:
    return [
        {
            "id": "mx-latency",
            "name": "request_latency_seconds",
            "type": "histogram",
            "labels": [],
            "emitted_at": ["app.py:10"],
            "description": "Request latency.",
        }
    ]


def _default_metric_recommendations() -> list[dict[str, object]]:
    return [
        {
            "id": "mr-add-latency",
            "kind": "new",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "Detects slow requests.",
            "details": "Add a histogram.",
        }
    ]


def _default_alert_recommendations() -> list[dict[str, object]]:
    return [
        {
            "id": "ar-timeout-critical",
            "name": "TimeoutCritical",
            "expr": "rate(errors_total[5m]) > 0.1",
            "for_duration": "5m",
            "severity": "critical",
            "failure_mode_ids": ["fm-timeout"],
            "annotations": {"summary": "Timeouts spiking", "description": "Investigate."},
        }
    ]


def _default_coverage() -> list[dict[str, object]]:
    return [
        {
            "failure_mode_id": "fm-timeout",
            "detecting_metric_ids": ["mx-latency"],
            "metric_recommendation_ids": ["mr-add-latency"],
            "alert_ids": ["ar-timeout-critical"],
        },
        {
            "failure_mode_id": "fm-accepted",
            "detecting_metric_ids": [],
            "metric_recommendation_ids": [],
            "alert_ids": [],
        },
    ]


_FILES: dict[str, str] = {
    "system_components": "system-map.yaml",
    "failure_modes": "failure-modes.yaml",
    "metrics": "metrics.yaml",
    "metric_recommendations": "metric-recommendations.yaml",
    "alert_recommendations": "alert-recommendations.yaml",
    "coverage": "coverage.yaml",
}

_DEFAULTS: dict[str, Callable[[], list[dict[str, object]]]] = {
    "system_components": _default_system_components,
    "failure_modes": _default_failure_modes,
    "metrics": _default_metrics,
    "metric_recommendations": _default_metric_recommendations,
    "alert_recommendations": _default_alert_recommendations,
    "coverage": _default_coverage,
}


def _dump(path: Path, data: object) -> None:
    with path.open("w") as handle:
        _DUMPER.dump(data, handle)


def _write_set(
    root: Path,
    *,
    state: dict[str, object] | bool | None = None,
    config: dict[str, object] | bool | None = None,
    docs: bool = True,
    **overrides: list[dict[str, object]],
) -> None:
    """Write a minimal, fully valid `.blare/` tree. A keyword in `overrides` (e.g.
    `failure_modes=[...]`) replaces that one file's entry list; `state`/`config` of
    `False` skips writing that file entirely, `None` (the default) writes the valid
    default, and an explicit dict writes exactly that content."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    for key, filename in _FILES.items():
        entries = overrides.get(key, _DEFAULTS[key]())
        _dump(root / filename, entries)
    if state is not False:
        _dump(root / "state.yaml", state if isinstance(state, dict) else _default_state())
    if config is not False:
        _dump(root / "config.yaml", config if isinstance(config, dict) else _default_config())
    if docs:
        for filename in _FILES.values():
            doc_path = root / "docs" / f"{Path(filename).stem}.md"
            doc_path.write_text(GENERATED_DOC_HEADER + "\n\n# Generated doc\n")


# --- state_exists / init_inspection ---------------------------------------------------


def test_contract_state_exists_both_ways(tmp_path: Path) -> None:
    """state_exists is False before state.yaml is written and True after."""
    root = tmp_path / ".blare"
    root.mkdir()

    assert state_exists(root) is False

    (root / "state.yaml").write_text("analyzed_sha: a\nschema_version: 1\n")

    assert state_exists(root) is True


def test_contract_init_inspection_passes_on_an_empty_root(tmp_path: Path) -> None:
    """init_inspection does not raise when nothing is at any canonical path."""
    root = tmp_path / ".blare"
    root.mkdir()

    init_inspection(root)


def test_contract_init_inspection_refuses_on_preexisting_entry_file(tmp_path: Path) -> None:
    """A pre-existing entry-based file with no state file refuses, naming the file."""
    root = tmp_path / ".blare"
    root.mkdir()
    (root / "failure-modes.yaml").write_text("[]\n")

    with pytest.raises(PreexistingFilesError) as exc_info:
        init_inspection(root)

    assert "failure-modes.yaml" in exc_info.value.cause


def test_contract_init_inspection_refuses_on_preexisting_derived_doc_file(
    tmp_path: Path,
) -> None:
    """A pre-existing file at a derived-doc path with no state file refuses, naming it."""
    root = tmp_path / ".blare"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "coverage.md").write_text(GENERATED_DOC_HEADER + "\n")

    with pytest.raises(PreexistingFilesError) as exc_info:
        init_inspection(root)

    assert str(root / "docs" / "coverage.md") in exc_info.value.cause


def test_contract_init_inspection_ignores_a_lone_config_file(tmp_path: Path) -> None:
    """An existing config.yaml alone never triggers the R1 inverse refusal."""
    root = tmp_path / ".blare"
    root.mkdir()
    (root / "config.yaml").write_text("stack: prometheus\n")

    init_inspection(root)


# --- empty_set -------------------------------------------------------------------------


def test_contract_empty_set_is_semantically_empty_with_default_config(
    tmp_path: Path,
) -> None:
    """empty_set on a bare root yields every entry map empty, no recorded SHA, the
    default stack, and no semantic violations."""
    root = tmp_path / ".blare"
    root.mkdir()

    s = empty_set(root)

    assert s.system_components == {}
    assert s.failure_modes == {}
    assert s.metrics == {}
    assert s.metric_recommendations == {}
    assert s.alert_recommendations == {}
    assert s.coverage == {}
    assert s.analyzed_sha is None
    assert s.stack_name == "prometheus"
    assert s.config_existed is False
    assert s.raw_bytes == {}
    assert s.documents == {}
    assert semantic_violations(s) == []


def test_contract_empty_set_honors_an_existing_config(tmp_path: Path) -> None:
    """empty_set resolves and keeps a pre-existing, valid config.yaml."""
    root = tmp_path / ".blare"
    root.mkdir()
    (root / "config.yaml").write_text("stack: prometheus\n")

    s = empty_set(root)

    assert s.stack_name == "prometheus"
    assert s.config_existed is True


def test_contract_empty_set_refuses_an_unsupported_existing_config(tmp_path: Path) -> None:
    """empty_set still refuses an existing config naming an unsupported stack."""
    root = tmp_path / ".blare"
    root.mkdir()
    (root / "config.yaml").write_text("stack: datadog\n")

    with pytest.raises(UnsupportedStackError) as exc_info:
        empty_set(root)

    assert "datadog" in exc_info.value.cause
    assert "prometheus" in exc_info.value.cause


def test_contract_empty_set_defaults_when_config_absent(tmp_path: Path) -> None:
    """empty_set resolves the default stack in memory when config.yaml is absent."""
    root = tmp_path / ".blare"
    root.mkdir()

    s = empty_set(root)

    assert s.stack_name == "prometheus"
    assert s.config_existed is False


# --- load: the happy path ---------------------------------------------------------------


def test_contract_load_without_state_raises_naming_blare_analyze(tmp_path: Path) -> None:
    """load on a root with no state.yaml raises StateMissingError naming `blare analyze`."""
    root = tmp_path / ".blare"
    root.mkdir()

    with pytest.raises(StateMissingError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "blare analyze" in exc_info.value.next_action


def test_contract_load_round_trips_a_valid_set(tmp_path: Path) -> None:
    """load of a valid set round-trips every entry type, state, config, and raw bytes."""
    root = tmp_path / ".blare"
    _write_set(root)

    s = load(root, RunMode.UPDATE)

    assert set(s.system_components) == {"sm-web"}
    assert s.system_components["sm-web"].kind == "service"
    assert set(s.failure_modes) == {"fm-timeout", "fm-accepted"}
    assert s.failure_modes["fm-timeout"].severity == "critical"
    assert s.failure_modes["fm-accepted"].exclusion_reason == "Low impact, accepted."
    assert set(s.metrics) == {"mx-latency"}
    assert set(s.metric_recommendations) == {"mr-add-latency"}
    assert set(s.alert_recommendations) == {"ar-timeout-critical"}
    assert set(s.coverage) == {"fm-timeout", "fm-accepted"}
    assert s.analyzed_sha == "a" * 40
    assert s.schema_version == 1
    assert s.stack_name == "prometheus"
    assert s.config_existed is True

    expected_files = set(_FILES.values()) | {"state.yaml"}
    assert set(s.raw_bytes) == expected_files
    assert set(s.documents) == set(_FILES.values())
    for filename in _FILES.values():
        assert (root / filename).read_bytes() == s.raw_bytes[filename]
    assert (root / "state.yaml").read_bytes() == s.raw_bytes["state.yaml"]

    assert semantic_violations(s) == []


# --- load: R19 structural red cases, one per clause -------------------------------------


def test_contract_load_rejects_bad_enum(tmp_path: Path) -> None:
    """A failure mode with an out-of-range severity fails schema conformance."""
    root = tmp_path / ".blare"
    bad = _default_failure_modes()
    bad[0]["severity"] = "urgent"
    _write_set(root, failure_modes=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "failure-modes.yaml" in exc_info.value.cause


def test_contract_load_rejects_wrong_file_id_prefix(tmp_path: Path) -> None:
    """An id using another file's prefix fails schema conformance (also how a cross-file
    duplicate would manifest, since each file only accepts its own prefix)."""
    root = tmp_path / ".blare"
    bad = _default_system_components()
    bad[0]["id"] = "fm-web"
    _write_set(root, system_components=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "system-map.yaml" in exc_info.value.cause


def test_contract_load_accepts_change_metric_recommendation_with_valid_metric_id(
    tmp_path: Path,
) -> None:
    """A 'change' metric recommendation with a real, correctly-prefixed metric_id
    round-trips correctly (the happy path for the kind/metric_id schema rule). Reuses
    the default coverage entry's expected id (mr-add-latency) so this doesn't also
    need to override coverage to stay dangling-reference-free."""
    root = tmp_path / ".blare"
    mrs: list[dict[str, object]] = [
        {
            "id": "mr-add-latency",
            "kind": "change",
            "metric_id": "mx-latency",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "Add a label.",
            "details": "Add the route label.",
        }
    ]
    _write_set(root, metric_recommendations=mrs)

    s = load(root, RunMode.UPDATE)

    assert s.metric_recommendations["mr-add-latency"].kind == "change"
    assert s.metric_recommendations["mr-add-latency"].metric_id == "mx-latency"


def test_contract_load_rejects_change_metric_recommendation_without_metric_id(
    tmp_path: Path,
) -> None:
    """A 'change' metric recommendation missing metric_id fails schema conformance
    (metric_id is required iff kind == 'change')."""
    root = tmp_path / ".blare"
    mrs: list[dict[str, object]] = [
        {
            "id": "mr-add-label",
            "kind": "change",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "r",
            "details": "d",
        }
    ]
    _write_set(root, metric_recommendations=mrs)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "metric_id" in exc_info.value.cause


def test_contract_load_rejects_new_metric_recommendation_with_metric_id(
    tmp_path: Path,
) -> None:
    """A 'new' metric recommendation that also specifies metric_id fails schema
    conformance -- 'required iff change' is enforced in both directions."""
    root = tmp_path / ".blare"
    mrs: list[dict[str, object]] = [
        {
            "id": "mr-add-latency",
            "kind": "new",
            "metric_id": "mx-latency",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "r",
            "details": "d",
        }
    ]
    _write_set(root, metric_recommendations=mrs)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "metric_id" in exc_info.value.cause


def test_contract_load_rejects_metric_recommendation_metric_id_wrong_prefix(
    tmp_path: Path,
) -> None:
    """A 'change' metric recommendation whose metric_id doesn't use the 'mx-' prefix
    fails schema conformance."""
    root = tmp_path / ".blare"
    mrs: list[dict[str, object]] = [
        {
            "id": "mr-add-label",
            "kind": "change",
            "metric_id": "fm-timeout",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "r",
            "details": "d",
        }
    ]
    _write_set(root, metric_recommendations=mrs)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "mx-" in exc_info.value.cause


def test_contract_load_rejects_duplicate_id_within_a_file(tmp_path: Path) -> None:
    """Two entries in the same file sharing an id fail global uniqueness."""
    root = tmp_path / ".blare"
    dup = _default_system_components()
    dup.append(dict(dup[0]))
    _write_set(root, system_components=dup)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "duplicate id" in exc_info.value.cause
    assert "sm-web" in exc_info.value.cause


def test_contract_load_rejects_dangling_reference_from_non_user_visible_entry(
    tmp_path: Path,
) -> None:
    """A dangling reference is rejected from any entry, not only user-visible ones (R19
    broadens R3's narrower "user-visible" text to every reference field)."""
    root = tmp_path / ".blare"
    bad = _default_failure_modes()
    bad[1]["caused_by"] = ["fm-does-not-exist"]  # fm-accepted: user_visible is False
    _write_set(root, failure_modes=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "fm-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_reference_in_system_component_depends_on(
    tmp_path: Path,
) -> None:
    """A system component's depends_on referencing an unknown id is a dangling
    reference (R19, distinct from the failure-mode caused_by case above)."""
    root = tmp_path / ".blare"
    bad = _default_system_components()
    bad[0] = {**bad[0], "depends_on": ["sm-does-not-exist"]}
    _write_set(root, system_components=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "sm-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_metric_id_on_metric_recommendation(
    tmp_path: Path,
) -> None:
    """A 'change' metric recommendation whose metric_id doesn't exist is a dangling
    reference."""
    root = tmp_path / ".blare"
    mrs: list[dict[str, object]] = [
        {
            "id": "mr-add-latency",
            "kind": "change",
            "metric_id": "mx-does-not-exist",
            "failure_mode_ids": ["fm-timeout"],
            "rationale": "r",
            "details": "d",
        }
    ]
    _write_set(root, metric_recommendations=mrs)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "mx-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_failure_mode_id_on_metric_recommendation(
    tmp_path: Path,
) -> None:
    """A metric recommendation referencing an unknown failure_mode_id is a dangling
    reference."""
    root = tmp_path / ".blare"
    mrs = _default_metric_recommendations()
    mrs[0] = {**mrs[0], "failure_mode_ids": ["fm-does-not-exist"]}
    _write_set(root, metric_recommendations=mrs)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "fm-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_failure_mode_id_on_alert_recommendation(
    tmp_path: Path,
) -> None:
    """An alert recommendation referencing an unknown failure_mode_id is a dangling
    reference."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars[0] = {**ars[0], "failure_mode_ids": ["fm-does-not-exist"]}
    _write_set(root, alert_recommendations=ars)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "fm-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_failure_mode_id_on_coverage_entry(
    tmp_path: Path,
) -> None:
    """A coverage entry whose own failure_mode_id doesn't exist is a dangling
    reference."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov.append(
        {
            "failure_mode_id": "fm-does-not-exist",
            "detecting_metric_ids": [],
            "metric_recommendation_ids": [],
            "alert_ids": [],
        }
    )
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "fm-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_metric_id_on_coverage_entry(tmp_path: Path) -> None:
    """A coverage entry's detecting_metric_ids referencing an unknown metric id is a
    dangling reference."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[0] = {**cov[0], "detecting_metric_ids": ["mx-does-not-exist"]}
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "mx-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_metric_recommendation_id_on_coverage_entry(
    tmp_path: Path,
) -> None:
    """A coverage entry's metric_recommendation_ids referencing an unknown id is a
    dangling reference."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[0] = {**cov[0], "metric_recommendation_ids": ["mr-does-not-exist"]}
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "mr-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_dangling_alert_id_on_coverage_entry(tmp_path: Path) -> None:
    """A coverage entry's alert_ids referencing an unknown alert id is a dangling
    reference."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[0] = {**cov[0], "alert_ids": ["ar-does-not-exist"]}
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "ar-does-not-exist" in exc_info.value.cause


def test_contract_load_rejects_caused_by_cycle(tmp_path: Path) -> None:
    """A caused_by cycle between two failure modes fails acyclicity."""
    root = tmp_path / ".blare"
    cyclic = _default_failure_modes()
    cyclic[0]["caused_by"] = ["fm-accepted"]
    cyclic[1]["caused_by"] = ["fm-timeout"]
    _write_set(root, failure_modes=cyclic)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "cycle" in exc_info.value.cause


@pytest.mark.parametrize("field", ["severity", "user_visible", "coverage_status"])
def test_contract_load_rejects_failure_mode_missing_required_field(
    tmp_path: Path, field: str
) -> None:
    """A failure mode missing severity, user_visible, or coverage_status fails
    schema conformance, naming the file."""
    root = tmp_path / ".blare"
    bad = _default_failure_modes()
    del bad[0][field]
    _write_set(root, failure_modes=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "failure-modes.yaml" in exc_info.value.cause


def test_contract_load_rejects_excluded_without_reason(tmp_path: Path) -> None:
    """An excluded failure mode with no exclusion_reason fails schema conformance."""
    root = tmp_path / ".blare"
    bad = _default_failure_modes()
    del bad[1]["exclusion_reason"]
    _write_set(root, failure_modes=bad)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "exclusion_reason" in exc_info.value.cause
    assert "fm-accepted" in exc_info.value.cause


def test_contract_load_rejects_missing_coverage_entry_for_excluded_failure_mode(
    tmp_path: Path,
) -> None:
    """A failure mode (excluded or not) with no coverage entry at all is a structural
    failure."""
    root = tmp_path / ".blare"
    cov = [_default_coverage()[0]]  # drop fm-accepted's entry
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "fm-accepted" in exc_info.value.cause


def test_contract_load_rejects_duplicate_coverage_key(tmp_path: Path) -> None:
    """Two coverage entries for the same failure_mode_id fail structural validation."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov.append(dict(cov[0]))
    _write_set(root, coverage=cov)

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "duplicate coverage key" in exc_info.value.cause
    assert "fm-timeout" in exc_info.value.cause


def test_contract_load_rejects_unparseable_entry_file(tmp_path: Path) -> None:
    """A syntactically unparseable entry file fails, naming the file."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "metrics.yaml").write_text("this: is: not: valid: yaml: [\n")

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "metrics.yaml" in exc_info.value.cause


def test_contract_load_rejects_unparseable_state(tmp_path: Path) -> None:
    """An unparseable state.yaml fails, naming the file."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "state.yaml").write_text("analyzed_sha: [broken\n")

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "state.yaml" in exc_info.value.cause


def test_contract_load_rejects_state_missing_a_field(tmp_path: Path) -> None:
    """A state.yaml missing schema_version fails, naming the missing field."""
    root = tmp_path / ".blare"
    _write_set(root, state={"analyzed_sha": "a" * 40})

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "schema_version" in exc_info.value.cause


def test_contract_load_rejects_state_missing_analyzed_sha(tmp_path: Path) -> None:
    """A state.yaml missing analyzed_sha fails, naming the missing field (the same code
    path as the missing-schema_version case, exercised for symmetry)."""
    root = tmp_path / ".blare"
    _write_set(root, state={"schema_version": 1})

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "analyzed_sha" in exc_info.value.cause


def test_contract_load_rejects_headerless_derived_doc_file(tmp_path: Path) -> None:
    """A file at a derived-doc path lacking the generated-file header fails, naming it --
    such a file is not Blare's to overwrite."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "docs" / "metrics.md").write_text("# Hand-written, not generated by blare\n")

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert str(root / "docs" / "metrics.md") in exc_info.value.cause


def test_contract_load_rejects_state_present_with_entry_file_missing(tmp_path: Path) -> None:
    """state.yaml present but an entry file missing fails, naming the missing file."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "metrics.yaml").unlink()

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "metrics.yaml" in exc_info.value.cause


# --- load: config and stack resolution (R23) ---------------------------------------------


def test_contract_load_resolves_a_valid_config(tmp_path: Path) -> None:
    """A valid config.yaml resolves its named stack."""
    root = tmp_path / ".blare"
    _write_set(root, config={"stack": "prometheus"})

    s = load(root, RunMode.UPDATE)

    assert s.stack_name == "prometheus"
    assert s.stack.name == "prometheus"
    assert s.config_existed is True


def test_contract_load_unparseable_config_raises_config_error(tmp_path: Path) -> None:
    """An unparseable config.yaml raises ConfigError naming the file and supported values."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "config.yaml").write_text("stack: [broken\n")

    with pytest.raises(ConfigError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "config.yaml" in exc_info.value.cause
    assert "prometheus" in exc_info.value.next_action


def test_contract_load_empty_config_document_raises_config_error(tmp_path: Path) -> None:
    """A config.yaml that parses to an empty (null) document is ConfigError (R23's
    "otherwise invalid" -- "an empty or null document included")."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "config.yaml").write_text("")

    with pytest.raises(ConfigError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "prometheus" in exc_info.value.next_action


def test_contract_load_config_mapping_without_stack_key_raises_config_error(
    tmp_path: Path,
) -> None:
    """A config.yaml that parses to a mapping with no 'stack' key at all is
    ConfigError -- distinct from the empty-document case above (a different branch of
    the same resolution logic: a real mapping, just missing the key)."""
    root = tmp_path / ".blare"
    _write_set(root, config={"other_setting": "value"})

    with pytest.raises(ConfigError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "prometheus" in exc_info.value.next_action


def test_contract_load_config_null_stack_value_raises_config_error(tmp_path: Path) -> None:
    """A config.yaml with an explicit `stack: null` is ConfigError, not a crash trying
    to treat None as a stack name."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "config.yaml").write_text("stack:\n")

    with pytest.raises(ConfigError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "prometheus" in exc_info.value.next_action


def test_contract_load_unsupported_stack_name_raises_listing_supported_values(
    tmp_path: Path,
) -> None:
    """An unsupported stack name propagates UnsupportedStackError listing supported
    values, rather than a ConfigError. R23 requires every config-path error to name the
    file too -- get_stack's own message already hardcodes the fixed config path, so
    artifacts propagates it unwrapped rather than re-attaching a computed one."""
    root = tmp_path / ".blare"
    _write_set(root, config={"stack": "datadog"})

    with pytest.raises(UnsupportedStackError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "datadog" in exc_info.value.cause
    assert "prometheus" in exc_info.value.cause
    assert ".blare/config.yaml" in exc_info.value.next_action


def test_contract_load_missing_config_in_update_mode_raises_config_error(
    tmp_path: Path,
) -> None:
    """A missing config.yaml at `blare update` time is ConfigError (R23's "same error"
    as an invalid config), naming the file and supported values."""
    root = tmp_path / ".blare"
    _write_set(root, config=False)

    with pytest.raises(ConfigError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "config.yaml" in exc_info.value.cause
    assert "prometheus" in exc_info.value.next_action


def test_contract_load_missing_config_in_analyze_mode_defaults_in_memory(
    tmp_path: Path,
) -> None:
    """A missing config.yaml at analyze time (R16 re-analysis included) resolves the
    default in memory rather than raising, flagging the set for the file's creation at
    write time."""
    root = tmp_path / ".blare"
    _write_set(root, config=False)

    s = load(root, RunMode.ANALYZE)

    assert s.stack_name == "prometheus"
    assert s.config_existed is False


# --- load: schema version (R24) -----------------------------------------------------------


def test_contract_load_schema_version_mismatch_names_both_versions(tmp_path: Path) -> None:
    """A schema_version mismatch raises SchemaVersionError naming both versions --
    distinct from a StructuralValidationError."""
    root = tmp_path / ".blare"
    _write_set(root, state={"analyzed_sha": "a" * 40, "schema_version": 99})

    with pytest.raises(SchemaVersionError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "99" in exc_info.value.cause
    assert "1" in exc_info.value.cause


# --- semantic_violations: R4-R5 plus the excluded-empty-sets property ----------------------


def test_contract_semantic_unmapped_non_excluded_failure_mode(tmp_path: Path) -> None:
    """A non-excluded failure mode with no recommended alert is UNMAPPED_FAILURE_MODE,
    with its fixed repair phase (4)."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[0] = {**cov[0], "alert_ids": []}
    _write_set(root, alert_recommendations=[], coverage=cov)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [Violation(ViolationKind.UNMAPPED_FAILURE_MODE, ("fm-timeout",))]
    assert violations[0].phase is Phase.ALERT_RECOMMENDATIONS


def test_contract_semantic_invalid_expression_via_fake_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alert whose expression the (fake) stack rejects is INVALID_EXPRESSION."""
    fake = FakeStack(invalid_exprs=frozenset({"rate(errors_total[5m]) > 0.1"}))
    monkeypatch.setitem(stack_module._REGISTRY, "prometheus", fake)
    root = tmp_path / ".blare"
    _write_set(root)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.INVALID_EXPRESSION, ("ar-timeout-critical",))
    ]


def test_contract_semantic_alert_severity_below_max(tmp_path: Path) -> None:
    """An alert with a lower severity than the max of its failure modes is
    ALERT_SEVERITY_BELOW_MAX."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars[0] = {**ars[0], "severity": "warning"}  # fm-timeout is critical
    _write_set(root, alert_recommendations=ars)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.ALERT_SEVERITY_BELOW_MAX, ("ar-timeout-critical",))
    ]


def test_contract_semantic_alert_coverage_linkage_disagreement(tmp_path: Path) -> None:
    """An alert whose own failure_mode_ids disagrees with which coverage entries list it
    is LINKAGE_INCONSISTENCY."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars.append(
        {
            "id": "ar-second",
            "name": "TimeoutSecond",
            "expr": "rate(errors_total[5m]) > 0.1",
            "for_duration": "5m",
            "severity": "critical",
            "failure_mode_ids": ["fm-timeout"],
            "annotations": {"summary": "s2", "description": "d2"},
        }
    )
    # No coverage entry lists "ar-second" in its alert_ids, so the two sides disagree.
    _write_set(root, alert_recommendations=ars)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [Violation(ViolationKind.LINKAGE_INCONSISTENCY, ("ar-second",))]


def test_contract_semantic_metric_recommendation_empty_linkage(tmp_path: Path) -> None:
    """A metric recommendation with empty failure_mode_ids is
    EMPTY_LINKAGE_METRIC_RECOMMENDATION, with its fixed repair phase (3)."""
    root = tmp_path / ".blare"
    mrs = _default_metric_recommendations()
    mrs[0] = {**mrs[0], "failure_mode_ids": []}
    _write_set(root, metric_recommendations=mrs)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.EMPTY_LINKAGE_METRIC_RECOMMENDATION, ("mr-add-latency",))
    ]
    assert violations[0].phase is Phase.METRIC_COVERAGE


def test_contract_semantic_alert_recommendation_empty_linkage(tmp_path: Path) -> None:
    """An alert recommendation with empty failure_mode_ids is
    EMPTY_LINKAGE_ALERT_RECOMMENDATION."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars.append(
        {
            "id": "ar-orphan",
            "name": "Orphan",
            "expr": "up",
            "for_duration": "5m",
            "severity": "warning",
            "failure_mode_ids": [],
            "annotations": {"summary": "s", "description": "d"},
        }
    )
    _write_set(root, alert_recommendations=ars)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.EMPTY_LINKAGE_ALERT_RECOMMENDATION, ("ar-orphan",))
    ]


def test_contract_semantic_excluded_failure_mode_with_metric_side_coverage(
    tmp_path: Path,
) -> None:
    """An excluded failure mode whose coverage entry has non-empty detecting_metric_ids
    is EXCLUDED_WITH_METRIC_COVERAGE, with its fixed repair phase (3)."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[1] = {**cov[1], "detecting_metric_ids": ["mx-latency"]}
    _write_set(root, coverage=cov)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.EXCLUDED_WITH_METRIC_COVERAGE, ("fm-accepted",))
    ]
    assert violations[0].phase is Phase.METRIC_COVERAGE


def test_contract_semantic_excluded_failure_mode_referenced_by_metric_recommendation(
    tmp_path: Path,
) -> None:
    """An excluded failure mode referenced directly by a metric recommendation's
    failure_mode_ids is metric-side dirty even when coverage stays empty."""
    root = tmp_path / ".blare"
    mrs = _default_metric_recommendations()
    mrs.append(
        {
            "id": "mr-for-excluded",
            "kind": "new",
            "failure_mode_ids": ["fm-accepted"],
            "rationale": "r",
            "details": "d",
        }
    )
    _write_set(root, metric_recommendations=mrs)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.EXCLUDED_WITH_METRIC_COVERAGE, ("fm-accepted",))
    ]


def test_contract_semantic_excluded_failure_mode_with_alert_side_coverage(
    tmp_path: Path,
) -> None:
    """An excluded failure mode with a (linkage-consistent) recommended alert is
    EXCLUDED_WITH_ALERT_COVERAGE -- the spec's excluded-empty-sets property, with its
    fixed repair phase (4)."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars.append(
        {
            "id": "ar-for-excluded",
            "name": "ForExcluded",
            "expr": "up",
            "for_duration": "5m",
            "severity": "warning",
            "failure_mode_ids": ["fm-accepted"],
            "annotations": {"summary": "s", "description": "d"},
        }
    )
    cov = _default_coverage()
    cov[1] = {**cov[1], "alert_ids": ["ar-for-excluded"]}
    _write_set(root, alert_recommendations=ars, coverage=cov)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert violations == [
        Violation(ViolationKind.EXCLUDED_WITH_ALERT_COVERAGE, ("fm-accepted",))
    ]
    assert violations[0].phase is Phase.ALERT_RECOMMENDATIONS


def test_contract_semantic_excluded_failure_mode_referenced_by_alert_recommendation_directly(
    tmp_path: Path,
) -> None:
    """An excluded failure mode referenced directly by an alert recommendation's
    failure_mode_ids is alert-side dirty even when no coverage entry's alert_ids lists
    it. Unlike the metric-side direct-reference case above, this also independently
    triggers LINKAGE_INCONSISTENCY for the same alert: the alert side has a
    linkage-consistency invariant the metric side doesn't, so an alert whose own
    failure_mode_ids disagrees with every coverage entry's alert_ids always trips it
    too -- both violations are expected here, not a sign either check is wrong."""
    root = tmp_path / ".blare"
    ars = _default_alert_recommendations()
    ars.append(
        {
            "id": "ar-for-excluded-direct",
            "name": "ForExcludedDirect",
            "expr": "up",
            "for_duration": "5m",
            "severity": "warning",
            "failure_mode_ids": ["fm-accepted"],
            "annotations": {"summary": "s", "description": "d"},
        }
    )
    _write_set(root, alert_recommendations=ars)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)

    assert Violation(ViolationKind.EXCLUDED_WITH_ALERT_COVERAGE, ("fm-accepted",)) in violations
    assert (
        Violation(ViolationKind.LINKAGE_INCONSISTENCY, ("ar-for-excluded-direct",))
        in violations
    )
    assert len(violations) == 2


def test_contract_semantic_violating_set_still_loads(tmp_path: Path) -> None:
    """semantic_violations never raises: a violating set still loads and reports its
    violations rather than being rejected by `load`."""
    root = tmp_path / ".blare"
    cov = _default_coverage()
    cov[0] = {**cov[0], "alert_ids": []}
    _write_set(root, alert_recommendations=[], coverage=cov)

    s = load(root, RunMode.UPDATE)  # must not raise despite the violation

    assert semantic_violations(s) != []


# --- gap_counts --------------------------------------------------------------------------


def test_contract_gap_counts_matches_coverage_status_split(tmp_path: Path) -> None:
    """gap_counts splits non-excluded failure modes by coverage status."""
    root = tmp_path / ".blare"
    _write_set(root)
    s = load(root, RunMode.UPDATE)

    assert gap_counts(s) == GapSummary(alertable=1, metric_gap=0, excluded=1)


def test_contract_gap_counts_counts_metric_gap_failure_modes(tmp_path: Path) -> None:
    """gap_counts counts a metric-gap failure mode under metric_gap, not alertable."""
    root = tmp_path / ".blare"
    fms = _default_failure_modes()
    fms[0] = {**fms[0], "coverage_status": "metric-gap"}
    _write_set(root, failure_modes=fms)
    s = load(root, RunMode.UPDATE)

    assert gap_counts(s) == GapSummary(alertable=0, metric_gap=1, excluded=1)


# --- Failure-mode tests: filesystem --------------------------------------------------------


def test_failure_filesystem_unreadable_artifact_file(tmp_path: Path) -> None:
    """A permission-denied entry file raises a structural error naming it."""
    root = tmp_path / ".blare"
    _write_set(root)
    target = root / "metrics.yaml"
    target.chmod(0o000)
    try:
        with pytest.raises(StructuralValidationError) as exc_info:
            load(root, RunMode.UPDATE)
        assert "metrics.yaml" in exc_info.value.cause
    finally:
        target.chmod(0o644)  # restore so tmp_path's own cleanup can remove it


def test_failure_filesystem_unreadable_config_file_raises_config_error(
    tmp_path: Path,
) -> None:
    """A permission-denied config.yaml raises ConfigError (R23's own next_action), not
    the generic StructuralValidationError every other canonical file's read failure
    raises -- config.yaml is outside R19's scope (spec, Artifacts)."""
    root = tmp_path / ".blare"
    _write_set(root)
    target = root / "config.yaml"
    target.chmod(0o000)
    try:
        with pytest.raises(ConfigError) as exc_info:
            load(root, RunMode.UPDATE)
        assert "config.yaml" in exc_info.value.cause
        assert "prometheus" in exc_info.value.next_action
    finally:
        target.chmod(0o644)  # restore so tmp_path's own cleanup can remove it


# --- Failure-mode tests: the stack dependency ------------------------------------------------


def test_failure_stack_validate_expression_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack whose validate_expression raises is treated as an invalid-expression
    violation, never a crash (FakeStack armed to raise)."""
    fake = FakeStack(raise_on=frozenset({"rate(errors_total[5m]) > 0.1"}))
    monkeypatch.setitem(stack_module._REGISTRY, "prometheus", fake)
    root = tmp_path / ".blare"
    _write_set(root)
    s = load(root, RunMode.UPDATE)

    violations = semantic_violations(s)  # must not raise despite the stack raising

    assert violations == [
        Violation(ViolationKind.INVALID_EXPRESSION, ("ar-timeout-critical",))
    ]


# --- Failure-mode tests: ruamel round-trip ----------------------------------------------------


def test_failure_ruamel_round_trip_rejects_a_construct_it_cannot_preserve(
    tmp_path: Path,
) -> None:
    """A YAML construct round-trip mode cannot preserve (a duplicate mapping key) raises
    a load error naming the file, rather than silently reformatting or picking a value."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "metrics.yaml").write_text(
        "- id: mx-latency\n"
        "  name: request_latency_seconds\n"
        "  name: duplicate_key_name\n"
        "  type: histogram\n"
        "  labels: []\n"
        "  emitted_at: [app.py:10]\n"
        "  description: Request latency.\n"
    )

    with pytest.raises(StructuralValidationError) as exc_info:
        load(root, RunMode.UPDATE)

    assert "metrics.yaml" in exc_info.value.cause


# =========================================================================================
# Write side (T1.5): batch_check, apply, referencing_phases, render_docs, raw_bytes_match,
# the write primitives.
# =========================================================================================


def _load_default(root: Path) -> ArtifactSet:
    """The baseline write-side tests build on: `_write_set`'s default tree, loaded."""
    _write_set(root)
    return load(root, RunMode.UPDATE)


# --- batch_check: phase consistency and coverage op restrictions -------------------------


def test_contract_batch_check_accepts_a_clean_batch(tmp_path: Path) -> None:
    """A well-formed, correctly-tagged add is accepted."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-cache",
                    "name": "Cache",
                    "kind": "datastore",
                    "description": "In-memory cache.",
                    "depends_on": ["sm-web"],
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict == BatchVerdict(ok=True)


def test_contract_batch_check_rejects_unknown_entry_type(tmp_path: Path) -> None:
    """An edit naming an entry_type outside the registry is rejected with a message
    that lists the valid entry types, so a caller guessing a name doesn't have to
    guess blindly a second time."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (Edit(EditOp.ADD, "linkage", {"id": "sm-cache"}),),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False
    assert verdict.message is not None
    assert "'linkage'" in verdict.message
    for valid_type in artifacts_module._TYPE_SPECS:
        assert valid_type in verdict.message


def test_contract_batch_check_rejects_mistagged_phase_edit(tmp_path: Path) -> None:
    """An edit whose entry_type belongs to a different phase than the batch's is
    rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "failure_modes",
                {
                    "id": "fm-new",
                    "title": "New",
                    "description": "d",
                    "severity": "warning",
                    "user_visible": False,
                    "caused_by": [],
                    "coverage_status": "alertable",
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_add_op_on_coverage_entry(tmp_path: Path) -> None:
    """Coverage entries reject add ops -- their keys are mechanical."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.METRIC_COVERAGE,
        (Edit(EditOp.ADD, "coverage", {"failure_mode_id": "fm-timeout"}),),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_remove_op_on_coverage_entry(tmp_path: Path) -> None:
    """Coverage entries reject remove ops -- their keys are mechanical."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.METRIC_COVERAGE, (Edit(EditOp.REMOVE, "coverage", "fm-timeout"),))

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_phase_4_coverage_edit_touching_metric_side(
    tmp_path: Path,
) -> None:
    """A coverage update tagged phase 4 that touches a metric-side field is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (
            Edit(
                EditOp.UPDATE,
                "coverage",
                {"failure_mode_id": "fm-timeout", "detecting_metric_ids": ["mx-latency"]},
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_phase_3_coverage_edit_touching_alert_side(
    tmp_path: Path,
) -> None:
    """Symmetric to the phase-4 case above: a coverage update tagged phase 3 that
    touches the alert-side field is rejected too -- the side restriction runs both
    ways."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.METRIC_COVERAGE,
        (
            Edit(
                EditOp.UPDATE,
                "coverage",
                {"failure_mode_id": "fm-timeout", "alert_ids": ["ar-timeout-critical"]},
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_accepts_coverage_update_scoped_to_owned_side(
    tmp_path: Path,
) -> None:
    """A coverage update tagged phase 3, touching only metric-side fields, is
    accepted."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.METRIC_COVERAGE,
        (
            Edit(
                EditOp.UPDATE,
                "coverage",
                {"failure_mode_id": "fm-timeout", "detecting_metric_ids": ["mx-latency"]},
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict == BatchVerdict(ok=True)


def test_contract_batch_check_rejects_coverage_update_for_unknown_failure_mode(
    tmp_path: Path,
) -> None:
    """A coverage update naming a failure_mode_id that doesn't exist is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.METRIC_COVERAGE,
        (
            Edit(
                EditOp.UPDATE,
                "coverage",
                {"failure_mode_id": "fm-does-not-exist", "detecting_metric_ids": []},
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


# --- batch_check: edit payload schema -----------------------------------------------------


def test_contract_batch_check_rejects_malformed_edit_payload(tmp_path: Path) -> None:
    """An add whose payload isn't a mapping is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.SYSTEM_MAP, (Edit(EditOp.ADD, "system_components", "not-a-dict"),))

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_edit_missing_required_field(tmp_path: Path) -> None:
    """An add missing a required field (here: failure mode without 'title') is
    rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.FAILURE_MODES,
        (
            Edit(
                EditOp.ADD,
                "failure_modes",
                {
                    "id": "fm-new",
                    "description": "d",
                    "severity": "warning",
                    "user_visible": False,
                    "caused_by": [],
                    "coverage_status": "alertable",
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_wrong_prefix_id(tmp_path: Path) -> None:
    """An add whose id uses another type's prefix is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.FAILURE_MODES,
        (
            Edit(
                EditOp.ADD,
                "failure_modes",
                {
                    "id": "sm-new",
                    "title": "New",
                    "description": "d",
                    "severity": "warning",
                    "user_visible": False,
                    "caused_by": [],
                    "coverage_status": "alertable",
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_bad_expression(tmp_path: Path) -> None:
    """An alert add whose expr is syntactically invalid (per the set's real stack) is
    rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (
            Edit(
                EditOp.ADD,
                "alert_recommendations",
                {
                    "id": "ar-new",
                    "name": "New",
                    "expr": "(((",
                    "for_duration": "5m",
                    "severity": "critical",
                    "failure_mode_ids": ["fm-timeout"],
                    "annotations": {"summary": "s", "description": "d"},
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_bad_rule_fields(tmp_path: Path) -> None:
    """An alert add with an invalid for_duration (via validate_rule_fields) is
    rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (
            Edit(
                EditOp.ADD,
                "alert_recommendations",
                {
                    "id": "ar-new",
                    "name": "New",
                    "expr": "up == 0",
                    "for_duration": "5 minutes",
                    "severity": "critical",
                    "failure_mode_ids": ["fm-timeout"],
                    "annotations": {"summary": "s", "description": "d"},
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


# --- batch_check: R19 structural rules on the resulting candidate ------------------------


def test_contract_batch_check_rejects_duplicate_id_add(tmp_path: Path) -> None:
    """An add reusing an id that already exists is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-web",
                    "name": "Web API (dup)",
                    "kind": "service",
                    "description": "d",
                    "depends_on": [],
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_duplicate_id_add_within_same_batch(
    tmp_path: Path,
) -> None:
    """Two adds of the same *new* id within one batch are rejected too -- distinct from
    reusing a pre-existing id above, since this duplicate only exists mid-batch."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    payload = {
        "id": "sm-cache",
        "name": "Cache",
        "kind": "datastore",
        "description": "d",
        "depends_on": [],
    }
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(EditOp.ADD, "system_components", dict(payload)),
            Edit(EditOp.ADD, "system_components", dict(payload)),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_accepts_add_then_update_of_same_id_within_batch(
    tmp_path: Path,
) -> None:
    """An add followed by an update of that same new id, in the same batch, is
    accepted -- validation follows the batch's running effect, not a static pre-batch
    snapshot of what ids exist."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-cache",
                    "name": "Cache",
                    "kind": "datastore",
                    "description": "d",
                    "depends_on": [],
                },
            ),
            Edit(
                EditOp.UPDATE,
                "system_components",
                {
                    "id": "sm-cache",
                    "name": "Cache (renamed)",
                    "kind": "datastore",
                    "description": "d",
                    "depends_on": [],
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict == BatchVerdict(ok=True)


def test_contract_batch_check_accepts_add_then_remove_of_same_id_within_batch(
    tmp_path: Path,
) -> None:
    """An add followed by a remove of that same new id, in the same batch, is
    accepted -- same running-effect reasoning as the add-then-update case above."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-cache",
                    "name": "Cache",
                    "kind": "datastore",
                    "description": "d",
                    "depends_on": [],
                },
            ),
            Edit(EditOp.REMOVE, "system_components", "sm-cache"),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict == BatchVerdict(ok=True)


def test_contract_batch_check_rejects_update_targeting_unknown_id(tmp_path: Path) -> None:
    """An update naming an id that doesn't exist is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.UPDATE,
                "system_components",
                {
                    "id": "sm-does-not-exist",
                    "name": "x",
                    "kind": "service",
                    "description": "d",
                    "depends_on": [],
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_remove_targeting_unknown_id(tmp_path: Path) -> None:
    """A remove naming an id that doesn't exist is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.METRIC_COVERAGE, (Edit(EditOp.REMOVE, "metrics", "mx-does-not-exist"),))

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_remove_targeting_id_of_wrong_entry_type(
    tmp_path: Path,
) -> None:
    """A remove tagged for one entry_type but naming a real id belonging to a
    *different* entry type is rejected -- otherwise it would silently no-op in apply()
    (popping the id from the wrong map never raises) and falsely report success."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.SYSTEM_MAP, (Edit(EditOp.REMOVE, "system_components", "mx-latency"),))

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_removal_that_dangles_a_reference(tmp_path: Path) -> None:
    """Removing a metric still referenced by a coverage entry's detecting_metric_ids is
    rejected -- the resulting candidate would dangle."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.METRIC_COVERAGE, (Edit(EditOp.REMOVE, "metrics", "mx-latency"),))

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_contract_batch_check_rejects_cycle_introduction(tmp_path: Path) -> None:
    """A batch of updates that would introduce a caused_by cycle is rejected."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.FAILURE_MODES,
        (
            Edit(
                EditOp.UPDATE,
                "failure_modes",
                {
                    "id": "fm-timeout",
                    "title": "Request timeout",
                    "description": "An upstream call times out.",
                    "severity": "critical",
                    "user_visible": True,
                    "caused_by": ["fm-accepted"],
                    "coverage_status": "alertable",
                },
            ),
            Edit(
                EditOp.UPDATE,
                "failure_modes",
                {
                    "id": "fm-accepted",
                    "title": "Accepted risk",
                    "description": "A risk we knowingly accept.",
                    "severity": "warning",
                    "user_visible": False,
                    "caused_by": ["fm-timeout"],
                    "coverage_status": "excluded",
                    "exclusion_reason": "Low impact, accepted.",
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


# --- apply: purity and mechanical coverage completeness -----------------------------------


def test_contract_apply_is_pure_and_reflects_edits(tmp_path: Path) -> None:
    """apply never mutates its input; the returned candidate reflects the edit."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    original_components = dict(s.system_components)
    b = EditBatch(
        Phase.SYSTEM_MAP,
        (
            Edit(
                EditOp.ADD,
                "system_components",
                {
                    "id": "sm-cache",
                    "name": "Cache",
                    "kind": "datastore",
                    "description": "d",
                    "depends_on": [],
                },
            ),
        ),
    )

    candidate = apply(s, b)

    assert s.system_components == original_components
    assert "sm-cache" not in s.system_components
    assert "sm-cache" in candidate.system_components
    assert candidate.system_components["sm-cache"].kind == "datastore"


def test_contract_write_entries_and_config_never_mutates_shared_documents(
    tmp_path: Path,
) -> None:
    """Two sibling candidates derived from the same loaded set share `.documents`
    (`apply` passes it through by reference, never copying it) -- writing one must not
    mutate that shared `CommentedSeq`, or a still-held sibling candidate's own view of
    its (never written) entries would corrupt in place."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    metric = s.metrics["mx-latency"]

    def _update_description(new_description: str) -> ArtifactSet:
        payload: dict[str, object] = {
            "id": metric.id,
            "name": metric.name,
            "type": metric.type,
            "labels": list(metric.labels),
            "emitted_at": list(metric.emitted_at),
            "description": new_description,
        }
        b = EditBatch(Phase.METRIC_COVERAGE, (Edit(EditOp.UPDATE, "metrics", payload),))
        return apply(s, b)

    candidate_a = _update_description("Candidate A's description.")
    candidate_b = _update_description("Candidate B's description.")
    assert candidate_a.documents["metrics.yaml"] is candidate_b.documents["metrics.yaml"]

    write_entries_and_config(root, candidate_a)

    # candidate_b was never written; its shared `.documents` baseline must be unaffected
    # by candidate_a's write, and re-parsing it must still show the original loaded
    # value, not candidate_a's.
    raw_item = candidate_b.documents["metrics.yaml"][0]
    assert raw_item["description"] == metric.description
    assert candidate_b.metrics["mx-latency"].description == "Candidate B's description."


def test_contract_apply_adds_mechanical_coverage_entry_for_new_failure_mode(
    tmp_path: Path,
) -> None:
    """Adding a failure mode yields its empty coverage entry in the candidate."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.FAILURE_MODES,
        (
            Edit(
                EditOp.ADD,
                "failure_modes",
                {
                    "id": "fm-new",
                    "title": "New",
                    "description": "d",
                    "severity": "warning",
                    "user_visible": False,
                    "caused_by": [],
                    "coverage_status": "alertable",
                },
            ),
        ),
    )

    candidate = apply(s, b)

    assert candidate.coverage["fm-new"] == CoverageEntry("fm-new", (), (), ())


def test_contract_apply_removes_mechanical_coverage_entry_for_removed_failure_mode(
    tmp_path: Path,
) -> None:
    """Removing a failure mode drops its coverage entry from the candidate."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(Phase.FAILURE_MODES, (Edit(EditOp.REMOVE, "failure_modes", "fm-accepted"),))

    candidate = apply(s, b)

    assert "fm-accepted" not in candidate.coverage
    assert "fm-accepted" not in candidate.failure_modes


def test_contract_apply_coverage_update_merges_metric_side_only(tmp_path: Path) -> None:
    """A phase-3 coverage update merges the metric side and leaves the alert side
    untouched."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.METRIC_COVERAGE,
        (
            Edit(
                EditOp.UPDATE,
                "coverage",
                {"failure_mode_id": "fm-timeout", "detecting_metric_ids": []},
            ),
        ),
    )

    candidate = apply(s, b)

    assert candidate.coverage["fm-timeout"].detecting_metric_ids == ()
    assert candidate.coverage["fm-timeout"].alert_ids == s.coverage["fm-timeout"].alert_ids


def test_contract_apply_coverage_update_merges_alert_side_only(tmp_path: Path) -> None:
    """A phase-4 coverage update merges the alert side and leaves the metric side
    untouched."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (Edit(EditOp.UPDATE, "coverage", {"failure_mode_id": "fm-timeout", "alert_ids": []}),),
    )

    candidate = apply(s, b)

    assert candidate.coverage["fm-timeout"].alert_ids == ()
    assert (
        candidate.coverage["fm-timeout"].detecting_metric_ids
        == s.coverage["fm-timeout"].detecting_metric_ids
    )
    assert (
        candidate.coverage["fm-timeout"].metric_recommendation_ids
        == s.coverage["fm-timeout"].metric_recommendation_ids
    )


# --- referencing_phases -------------------------------------------------------------------


def test_contract_referencing_phases_attributes_coverage_metric_side_to_phase_3(
    tmp_path: Path,
) -> None:
    """A changed metric id referenced by a coverage entry's detecting_metric_ids
    attributes to phase 3."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert referencing_phases(s, {"mx-latency"}) == {Phase.METRIC_COVERAGE}


def test_contract_referencing_phases_attributes_coverage_alert_side_to_phase_4(
    tmp_path: Path,
) -> None:
    """A changed alert id referenced by a coverage entry's alert_ids attributes to
    phase 4."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert referencing_phases(s, {"ar-timeout-critical"}) == {Phase.ALERT_RECOMMENDATIONS}


def test_contract_referencing_phases_finds_failure_mode_referenced_by_recommendations(
    tmp_path: Path,
) -> None:
    """A changed failure-mode id referenced by both a metric recommendation and an
    alert recommendation returns both owning phases."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert referencing_phases(s, {"fm-timeout"}) == {
        Phase.METRIC_COVERAGE,
        Phase.ALERT_RECOMMENDATIONS,
    }


def test_contract_referencing_phases_finds_system_component_depends_on(tmp_path: Path) -> None:
    """A changed system-component id referenced by another component's depends_on
    returns exactly phase 1."""
    root = tmp_path / ".blare"
    components = _default_system_components()
    components.append(
        {
            "id": "sm-cache",
            "name": "Cache",
            "kind": "datastore",
            "description": "d",
            "depends_on": ["sm-web"],
        }
    )
    _write_set(root, system_components=components)
    s = load(root, RunMode.UPDATE)

    assert referencing_phases(s, {"sm-web"}) == {Phase.SYSTEM_MAP}


def test_contract_referencing_phases_finds_failure_mode_caused_by(tmp_path: Path) -> None:
    """A changed failure-mode id referenced only by another failure mode's caused_by
    returns exactly phase 2."""
    root = tmp_path / ".blare"
    fms = _default_failure_modes()
    fms.append(
        {
            "id": "fm-root",
            "title": "Root cause",
            "description": "d",
            "severity": "warning",
            "user_visible": False,
            "caused_by": [],
            "coverage_status": "excluded",
            "exclusion_reason": "Not independently alertable.",
        }
    )
    fms[0] = {**fms[0], "caused_by": ["fm-root"]}
    cov = _default_coverage()
    cov.append(
        {
            "failure_mode_id": "fm-root",
            "detecting_metric_ids": [],
            "metric_recommendation_ids": [],
            "alert_ids": [],
        }
    )
    _write_set(root, failure_modes=fms, coverage=cov)
    s = load(root, RunMode.UPDATE)

    assert referencing_phases(s, {"fm-root"}) == {Phase.FAILURE_MODES}


def test_contract_referencing_phases_returns_empty_set_when_nothing_references(
    tmp_path: Path,
) -> None:
    """An id nothing references returns an empty set."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert referencing_phases(s, {"fm-does-not-exist"}) == set()


# --- render_docs / rendering ---------------------------------------------------------------


def test_contract_render_docs_header_is_first_line_of_every_doc(tmp_path: Path) -> None:
    """Every rendered doc starts with the generated-file header."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    docs = render_docs(s)

    assert len(docs) == 6
    for content in docs.values():
        assert content.split(b"\n", 1)[0] == GENERATED_DOC_HEADER.encode()


def test_contract_render_docs_entries_sorted_by_id(tmp_path: Path) -> None:
    """Entries within a rendered doc appear sorted by id."""
    root = tmp_path / ".blare"
    components: list[dict[str, object]] = [
        {
            "id": "sm-zeta",
            "name": "Zeta",
            "kind": "service",
            "description": "d",
            "depends_on": [],
        },
        {
            "id": "sm-alpha",
            "name": "Alpha",
            "kind": "service",
            "description": "d",
            "depends_on": [],
        },
    ]
    _write_set(root, system_components=components)
    s = load(root, RunMode.UPDATE)

    content = render_docs(s)[Path("docs") / "system-map.md"].decode()

    assert content.index("sm-alpha") < content.index("sm-zeta")


def test_contract_render_docs_coverage_contains_gap_report(tmp_path: Path) -> None:
    """coverage.md contains the gap report (the coverage-status split)."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    gaps = gap_counts(s)

    content = render_docs(s)[Path("docs") / "coverage.md"].decode()

    assert f"alertable: {gaps.alertable}" in content
    assert f"metric-gap: {gaps.metric_gap}" in content
    assert f"excluded: {gaps.excluded}" in content


def test_contract_render_docs_deterministic_across_calls(tmp_path: Path) -> None:
    """Rendering the same (unchanged) YAML twice produces byte-identical docs (R9)."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert render_docs(s) == render_docs(s)


# --- raw_bytes_match ------------------------------------------------------------------------


def test_contract_raw_bytes_match_true_on_untouched_disk(tmp_path: Path) -> None:
    """True immediately after load, before anything on disk changes."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    assert raw_bytes_match(root, s) is True


def test_contract_raw_bytes_match_false_after_hand_edit(tmp_path: Path) -> None:
    """False after a canonical file is hand-edited mid-run."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    with (root / "failure-modes.yaml").open("a") as handle:
        handle.write("# a stray trailing comment\n")

    assert raw_bytes_match(root, s) is False


def test_contract_raw_bytes_match_false_after_file_deleted(tmp_path: Path) -> None:
    """False after a canonical file is deleted mid-run."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    (root / "metrics.yaml").unlink()

    assert raw_bytes_match(root, s) is False


def test_contract_raw_bytes_match_false_after_new_file_over_fresh_baseline(
    tmp_path: Path,
) -> None:
    """False when a canonical-path file appears mid-run over the empty fresh-run
    baseline."""
    root = tmp_path / ".blare"
    root.mkdir()
    s = empty_set(root)
    assert raw_bytes_match(root, s) is True

    (root / "state.yaml").write_text("analyzed_sha: a\nschema_version: 1\n")

    assert raw_bytes_match(root, s) is False


def test_contract_raw_bytes_match_unaffected_by_derived_doc_edit(tmp_path: Path) -> None:
    """A derived-doc edit never affects the canonical-YAML comparison."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    (root / "docs" / "coverage.md").write_text(GENERATED_DOC_HEADER + "\nhand-edited\n")

    assert raw_bytes_match(root, s) is True


def test_contract_raw_bytes_match_unaffected_by_stray_file(tmp_path: Path) -> None:
    """A stray file at a non-canonical path never affects the comparison."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    (root / "stray.txt").write_text("not blare's")

    assert raw_bytes_match(root, s) is True


def test_contract_raw_bytes_match_unaffected_by_config_edit(tmp_path: Path) -> None:
    """config.yaml is outside the R20 comparison (artifacts.md, stated twice): editing
    it mid-run never affects raw_bytes_match, unlike every canonical-YAML file above."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    (root / "config.yaml").write_text("stack: prometheus  # hand-edited mid-run\n")

    assert raw_bytes_match(root, s) is True


# --- write primitives -----------------------------------------------------------------------


def test_contract_write_entries_and_config_fresh_creates_every_canonical_file(
    tmp_path: Path,
) -> None:
    """A fresh set's first write creates every canonical entry file, zero-entry files
    included, plus the default config."""
    root = tmp_path / ".blare"
    root.mkdir()
    s = empty_set(root)

    report = write_entries_and_config(root, s)

    for filename in _FILES.values():
        path = root / filename
        assert path.is_file()
        assert _DUMPER.load(path.read_bytes()) in (None, [])
    assert (root / "config.yaml").is_file()
    expected = {root / f for f in _FILES.values()} | {root / "config.yaml"}
    assert set(report.written) == expected
    assert report.skipped == ()


def test_contract_write_entries_and_config_creates_default_config_when_absent(
    tmp_path: Path,
) -> None:
    """A missing config is created with the default stack at write time."""
    root = tmp_path / ".blare"
    _write_set(root, config=False)
    s = load(root, RunMode.ANALYZE)

    report = write_entries_and_config(root, s)

    assert (root / "config.yaml").is_file()
    assert _DUMPER.load((root / "config.yaml").read_bytes()) == {"stack": "prometheus"}
    assert (root / "config.yaml") in report.written


def test_contract_write_entries_and_config_preserves_existing_config_byte_identically(
    tmp_path: Path,
) -> None:
    """An existing config is never rewritten, byte for byte."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "config.yaml").write_text("stack: prometheus  # hand comment\n")
    before = (root / "config.yaml").read_bytes()
    s = load(root, RunMode.UPDATE)

    report = write_entries_and_config(root, s)

    assert (root / "config.yaml").read_bytes() == before
    assert (root / "config.yaml") in report.skipped
    assert (root / "config.yaml") not in report.written


def test_contract_write_entries_and_config_report_lists_exactly_its_own_files(
    tmp_path: Path,
) -> None:
    """The report's written+skipped set is exactly the six entry files plus config; only
    the one file with an actual content change is written."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    metric = s.metrics["mx-latency"]
    payload: dict[str, object] = {
        "id": metric.id,
        "name": metric.name,
        "type": metric.type,
        "labels": list(metric.labels),
        "emitted_at": list(metric.emitted_at),
        "description": "Updated description.",
    }
    b = EditBatch(Phase.METRIC_COVERAGE, (Edit(EditOp.UPDATE, "metrics", payload),))
    candidate = apply(s, b)

    report = write_entries_and_config(root, candidate)

    assert set(report.written) == {root / "metrics.yaml"}
    expected_skipped = {root / f for f in _FILES.values() if f != "metrics.yaml"} | {
        root / "config.yaml"
    }
    assert set(report.skipped) == expected_skipped


def test_contract_write_docs_report_lists_exactly_its_own_files(tmp_path: Path) -> None:
    """write_docs's report lists exactly the six doc files, always written."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    report = write_docs(root, s)

    expected = {root / "docs" / f"{Path(f).stem}.md" for f in _FILES.values()}
    assert set(report.written) == expected
    assert report.skipped == ()


def test_contract_write_state_report_lists_exactly_its_own_file(tmp_path: Path) -> None:
    """write_state's report lists exactly state.yaml -- written on a SHA change,
    skipped when unchanged."""
    root = tmp_path / ".blare"
    s = _load_default(root)

    changed = write_state(root, s, "b" * 40)
    assert changed.written == (root / "state.yaml",)
    assert changed.skipped == ()

    s2 = load(root, RunMode.UPDATE)
    unchanged = write_state(root, s2, s2.analyzed_sha or "")
    assert unchanged.written == ()
    assert unchanged.skipped == (root / "state.yaml",)


def test_contract_surgical_write_preserves_untouched_entry_bytes(tmp_path: Path) -> None:
    """An entry an edit never touches keeps its exact hand-formatted bytes; only the
    entry an edit changed is rewritten."""
    root = tmp_path / ".blare"
    _write_set(root)
    (root / "failure-modes.yaml").write_text(
        "- id: fm-timeout\n"
        "  title: Request timeout\n"
        "  description: An upstream call times out.\n"
        "  severity: critical\n"
        "  user_visible: true\n"
        "  caused_by: []\n"
        "  coverage_status: alertable\n"
        "- id: fm-accepted  # hand comment preserved\n"
        "  title: 'Accepted risk'\n"
        "  description: A risk we knowingly accept.\n"
        "  severity: warning\n"
        "  user_visible: false\n"
        "  caused_by: []\n"
        "  coverage_status: excluded\n"
        "  exclusion_reason: Low impact, accepted.\n"
    )
    s = load(root, RunMode.UPDATE)
    b = EditBatch(
        Phase.FAILURE_MODES,
        (
            Edit(
                EditOp.UPDATE,
                "failure_modes",
                {
                    "id": "fm-timeout",
                    "title": "Request timeout (renamed)",
                    "description": "An upstream call times out.",
                    "severity": "critical",
                    "user_visible": True,
                    "caused_by": [],
                    "coverage_status": "alertable",
                },
            ),
        ),
    )
    candidate = apply(s, b)

    write_entries_and_config(root, candidate)

    new_content = (root / "failure-modes.yaml").read_text()
    assert "# hand comment preserved" in new_content
    assert "'Accepted risk'" in new_content
    assert "Request timeout (renamed)" in new_content


def test_contract_write_empty_edit_set_unchanged_sha_zero_byte_changes(tmp_path: Path) -> None:
    """A full write cycle with an empty edit set and an unchanged SHA changes no file's
    bytes anywhere under .blare/ (R9's zero diff)."""
    root = tmp_path / ".blare"
    s1 = _load_default(root)
    write_entries_and_config(root, s1)
    write_docs(root, s1)
    write_state(root, s1, s1.analyzed_sha or "")

    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    s2 = load(root, RunMode.UPDATE)
    candidate = apply(s2, EditBatch(Phase.SYSTEM_MAP, ()))
    write_entries_and_config(root, candidate)
    write_docs(root, candidate)
    write_state(root, candidate, s2.analyzed_sha or "")

    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_contract_write_empty_edit_set_new_sha_changes_exactly_state_file(
    tmp_path: Path,
) -> None:
    """With an empty edit set but a new SHA, only state.yaml's bytes change."""
    root = tmp_path / ".blare"
    s1 = _load_default(root)
    write_entries_and_config(root, s1)
    write_docs(root, s1)
    write_state(root, s1, s1.analyzed_sha or "")

    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    s2 = load(root, RunMode.UPDATE)
    candidate = apply(s2, EditBatch(Phase.SYSTEM_MAP, ()))
    write_entries_and_config(root, candidate)
    write_docs(root, candidate)
    write_state(root, candidate, "b" * 40)

    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    changed_paths = {p for p in before if before[p] != after.get(p)}
    assert changed_paths == {root / "state.yaml"}


def test_contract_write_docs_restores_hand_edited_derived_doc(tmp_path: Path) -> None:
    """A manually edited derived doc is restored to canonical form (R10)."""
    root = tmp_path / ".blare"
    s = _load_default(root)
    write_docs(root, s)
    (root / "docs" / "coverage.md").write_text("hand-edited garbage, no header at all\n")

    write_docs(root, s)

    content = (root / "docs" / "coverage.md").read_text()
    assert content.startswith(GENERATED_DOC_HEADER)
    assert "garbage" not in content


# --- Failure-mode tests: filesystem (write side) -------------------------------------------


def test_failure_filesystem_write_entries_and_config_mid_write_names_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure injected mid-write_entries_and_config raises WriteError naming the
    failing file; files already written land on disk, and state.yaml (written by a
    later, never-reached primitive) is untouched."""
    root = tmp_path / ".blare"
    root.mkdir()
    s = empty_set(root)

    real_write_file = artifacts_module._write_file

    def fake_write_file(path: Path, data: bytes) -> None:
        if path.name == "metric-recommendations.yaml":
            raise WriteError(cause=f"{path} could not be written (disk full)", next_action="x")
        real_write_file(path, data)

    monkeypatch.setattr(artifacts_module, "_write_file", fake_write_file)

    with pytest.raises(WriteError) as exc_info:
        write_entries_and_config(root, s)

    assert "metric-recommendations.yaml" in exc_info.value.cause
    assert (root / "system-map.yaml").is_file()
    assert (root / "failure-modes.yaml").is_file()
    assert (root / "metrics.yaml").is_file()
    assert not (root / "metric-recommendations.yaml").exists()
    assert not (root / "alert-recommendations.yaml").exists()
    assert not (root / "state.yaml").exists()


# --- Failure-mode tests: the stack dependency (batch_check) --------------------------------


def test_failure_stack_validate_expression_raises_in_batch_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack whose validate_expression raises is a rejecting verdict in batch_check
    too, never a crash."""
    fake = FakeStack(raise_on=frozenset({"up == 0"}))
    monkeypatch.setitem(stack_module._REGISTRY, "prometheus", fake)
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (
            Edit(
                EditOp.ADD,
                "alert_recommendations",
                {
                    "id": "ar-new",
                    "name": "New",
                    "expr": "up == 0",
                    "for_duration": "5m",
                    "severity": "critical",
                    "failure_mode_ids": ["fm-timeout"],
                    "annotations": {"summary": "s", "description": "d"},
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False


def test_failure_stack_validate_rule_fields_raises_in_batch_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack whose validate_rule_fields raises is a rejecting verdict, never a
    crash."""

    class RaisingRuleFieldsStack(FakeStack):
        def validate_rule_fields(self, alert: AlertRuleInput) -> ExpressionVerdict:
            raise RuntimeError("fake stack blew up validating rule fields")

    monkeypatch.setitem(stack_module._REGISTRY, "prometheus", RaisingRuleFieldsStack())
    root = tmp_path / ".blare"
    s = _load_default(root)
    b = EditBatch(
        Phase.ALERT_RECOMMENDATIONS,
        (
            Edit(
                EditOp.ADD,
                "alert_recommendations",
                {
                    "id": "ar-new",
                    "name": "New",
                    "expr": "up == 0",
                    "for_duration": "5m",
                    "severity": "critical",
                    "failure_mode_ids": ["fm-timeout"],
                    "annotations": {"summary": "s", "description": "d"},
                },
            ),
        ),
    )

    verdict = batch_check(s, b)

    assert verdict.ok is False
