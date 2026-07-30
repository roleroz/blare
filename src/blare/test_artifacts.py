"""Unit tests for blare.artifacts (read side, T1.4), per
engineering/modules/artifacts.md's test plan.

Two sets, per the global testing rules: `test_contract_*` covers the module's promised
read-side behaviour with its dependency (a FakeStack substituted for the real stack
registry entry) behaving normally; `test_failure_*` covers what happens when the
filesystem or the stack misbehave.

Fixture convention: `_write_set` builds a minimal, fully valid `.blare/` tree -- one
`alertable` failure mode (`fm-timeout`) with full, consistent coverage, and one
`excluded` failure mode (`fm-accepted`) with empty coverage -- via keyword overrides.
Each test starts from that baseline and mutates exactly the file/field(s) its scenario
needs to break, so the resulting violation or refusal is attributable to one cause.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from ruamel.yaml import YAML

import blare.stack as stack_module
from blare.artifacts import (
    GENERATED_DOC_HEADER,
    ConfigError,
    GapSummary,
    PreexistingFilesError,
    SchemaVersionError,
    StateMissingError,
    StructuralValidationError,
    empty_set,
    gap_counts,
    init_inspection,
    load,
    semantic_violations,
    state_exists,
)
from blare.model import Phase, RunMode, Violation, ViolationKind
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
