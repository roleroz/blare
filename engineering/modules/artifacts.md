# Module design — artifacts

## Decisions needed from you

This section contains only open items. **No open items** — schema field names and the ID
format below are implementation detail, changeable freely until first release.

## Responsibility

Everything under `.blare/` (architecture: no other module touches it): schemas, the three
validation contents, surgical edit application, deterministic rendering, ordered writing,
state and config.

## Data structures

Entry types, one YAML file each (layout per the architecture). Entries carry `id` —
`<prefix>-<kebab-slug>`, agent-proposed, **unique across the whole artifact set** (R19's
"every ID unique"); a prefix in the wrong file is a schema-conformance failure, which is
what makes per-file checks equal global uniqueness. Prefixes: `sm-` system map, `fm-`
failure modes, `mx-` metrics, `mr-` metric recommendations, `ar-` alert recommendations.
The one exception: **CoverageEntry has no `id`** — it is keyed by `failure_mode_id`, exactly
one entry per failure mode (excluded included, with empty sets); a duplicate key is reported
as a duplicate-ID violation, and a missing entry for any failure mode is a structural
failure. Completeness is maintained mechanically, never by the agent: `apply` creates an
empty coverage entry whenever a batch adds a failure mode and deletes it when one is
removed, so no reachable candidate can violate the one-entry-per-failure-mode rule — this
is part of what keeps "an accepted set always passes its next load" true.

- **SystemComponent** (`system-map.yaml`): id, name, kind (`service` | `worker` | `job` |
  `external-dependency` | `datastore` | `entrypoint`), description, depends_on (sm ids).
- **FailureMode** (`failure-modes.yaml`): id, title, description, severity
  (`critical`|`warning`), user_visible (bool), caused_by (fm ids), coverage_status
  (`alertable`|`metric-gap`|`excluded`), exclusion_reason (required iff excluded).
- **Metric** (`metrics.yaml`): id, name, type (`counter`|`gauge`|`histogram`|`summary`),
  labels, emitted_at (list of `path:line`), description.
- **MetricRecommendation** (`metric-recommendations.yaml`): id, kind (`new`|`change`),
  metric_id (required iff change), failure_mode_ids (non-emptiness is a semantic
  invariant, R5 — not schema), rationale, details.
- **AlertRecommendation** (`alert-recommendations.yaml`): id, name, expr, for_duration,
  severity, failure_mode_ids (non-emptiness semantic, as above), annotations (summary,
  description).
- **CoverageEntry** (`coverage.yaml`): failure_mode_id, detecting_metric_ids,
  metric_recommendation_ids, alert_ids.
- **state.yaml**: analyzed_sha, schema_version (current: 1).
- **config.yaml**: stack (MVP default: `prometheus`).

Linkage consistency rule: an alert's `failure_mode_ids` and the coverage entries listing
that alert in `alert_ids` must agree exactly; the severity invariant ("max of its failure
modes") is defined over the alert's own `failure_mode_ids`. Disagreement is a semantic
violation.

`ArtifactSet` is the in-memory whole: typed entry maps, the state fields, the config, the
stack handle received at load (what `semantic_violations` and `batch_check` consult), and
the loaded raw bytes per file (the surgical-write baseline and the R20 comparison baseline).
`EditBatch(phase, edits)`; `Edit(op: add|update|remove, entry_type, payload_or_id)` — for
coverage edits the key is the `failure_mode_id`, and the update payload carries only the
tagged phase's side: a phase-3 payload holds `detecting_metric_ids` and
`metric_recommendation_ids`, a phase-4 payload holds `alert_ids`; `apply` merges the owned
side and leaves the other untouched, which is also what makes the side-consistency check
a payload-schema check rather than a diff.

## Interface

```python
def state_exists(root: Path) -> bool                       # R1/R16/R17 mode dispatch
def init_inspection(root: Path) -> None                    # R1 inverse refusal
def empty_set(root: Path) -> ArtifactSet                   # fresh R1 baseline; resolves stack
def load(root: Path, mode: RunMode) -> ArtifactSet         # resolves stack; R19/R23/R24
def semantic_violations(s: ArtifactSet) -> list[Violation]
def batch_check(s: ArtifactSet, b: EditBatch) -> BatchVerdict
def apply(s: ArtifactSet, b: EditBatch) -> ArtifactSet     # pure candidate
def referencing_phases(s: ArtifactSet, changed_ids: set[str]) -> set[Phase]
def render_docs(s: ArtifactSet) -> dict[Path, bytes]
def raw_bytes_match(root: Path, s: ArtifactSet) -> bool    # R20 re-check, byte compare only
def write_entries_and_config(root: Path, s: ArtifactSet) -> WriteReport
def write_docs(root: Path, s: ArtifactSet) -> WriteReport
def write_state(root: Path, s: ArtifactSet, analyzed_sha: str) -> WriteReport
def gap_counts(s: ArtifactSet) -> GapSummary
```

- **Mode dispatch**: `state_exists` answers R1-vs-R16-vs-R17; `load` on a missing state file
  raises `StateMissingError` (the R17 identity — its message names `blare analyze`); the
  fresh R1 path never calls `load`, it starts from `empty_set`.
- **Config and stack resolution (R23)**: this module resolves the stack itself — the
  architecture's graph has artifacts→stack, not orchestrator→stack. `load` and `empty_set`
  read `config.yaml` when present and resolve its name through the stack registry: a parse
  failure is `ConfigError`, an unsupported name propagates the registry's
  `UnsupportedStackError` (carrying the supported values); a file that parses but carries
  no `stack` key — an empty or null document included — is "otherwise invalid" (R23), a
  `ConfigError` in both modes, never the missing-file default. Every config-path error —
  `ConfigError` included — names the file and the supported values (via the registry's
  `supported_stacks()`), because R23's message contract covers the invalid and
  missing-at-update cases alike. A missing config splits by
  `mode`, per R23: in update it is `ConfigError` ("the same error"); in analyze — the R16
  re-analysis path included, which is why `load` takes the mode — the default is resolved
  in memory and the file is created at the write, exactly as on the fresh branch.
  `empty_set` with no config resolves the default in memory; the config file is only ever
  first created by the write primitives (R20 forbids earlier creation), and an existing
  config is preserved byte-identically. The resolved
  handle rides in the set (`s.stack`), which is how the agent receives it — the
  orchestrator passes a value, it never consults the stack module.
- **Structural validation (load)** enforces R19's list: schema conformance (types, enums,
  ID-prefix-matches-file), global ID uniqueness, no dangling references from any entry,
  acyclic `caused_by`, required failure-mode fields, exclusion reasons, exactly one coverage
  entry per failure mode, parseable state with both fields, no headerless file at a
  derived-doc path, entry files present when state exists.
- **Semantic check** covers R4–R5 in full plus the excluded-empty-sets property — R3's
  content (reference integrity, acyclicity, required fields) is structural and lives in
  R19's tier: every non-excluded failure mode has ≥1 alert
  through coverage; alert↔coverage linkage consistency; alert severity is the max over its
  `failure_mode_ids`; recommendation linkage non-empty; expression validity via the set's
  stack (R4's language clause); and an excluded failure mode's coverage entry must have
  empty sets with no alert listing that failure mode (the spec's excluded-empty-sets
  property). `Violation(kind, entry_ids, phase)` — the phase is the **repair phase**, fixed
  per kind, not the phase owning the named entries, because it is what seeds the affected
  queue (R18) and what a system amendment opens: unmapped failure mode → 4; linkage
  inconsistency → 4; invalid expression → 4; alert severity below max → 4; excluded failure
  mode with alert-side coverage → 4, with metric-side coverage → 3; empty
  `failure_mode_ids` on a metric recommendation → 3, on an alert recommendation → 4. This
  list and the check list above are the same enumeration — every kind has a phase, every
  phase claim has a kind.
- **Amendment blast radius**: `referencing_phases` returns the phases owning entries that
  reference any changed ID — for coverage entries attributed by side (metric side → phase
  3, alert side → phase 4). It is the reference half of the amendment recompute; the
  invariant half is `semantic_violations` over the candidate, whose repair phases the
  orchestrator unions in.
- **Batch check** (content half; phase state is the orchestrator's): edit payload schema,
  expression syntax and stack rule-field validation for alert edits (`validate_expression`
  and `validate_rule_fields`), phase consistency (edit targets the tagged phase's
  artifacts; for coverage entries, the tagged phase's side), and R19's structural rules on
  the applied candidate — uniqueness, reference integrity including removals, acyclicity —
  so an accepted set always passes its next load. Coverage entries accept only `update`
  ops: their keys are mechanical (created and deleted by `apply` alongside their failure
  modes), so explicit `add`/`remove` ops on coverage are rejected — the other half of the
  completeness guarantee.
- **Surgical writing**: ruamel.yaml round-trip mode; entries untouched by edits keep their
  exact bytes, hand-formatting included (the R9/R16 mechanism); a file with no changed
  entries is not rewritten — except that a fresh run creates every canonical file,
  zero-entry files included, so the written set satisfies the load rule "entry files
  present when state exists". The write is three primitives the **orchestrator** drives in
  order per the architecture — `write_entries_and_config`, `write_docs`,
  `write_state` (R20: state last); `analyzed_sha` enters through `write_state`, never
  through batches. Each primitive returns its own `WriteReport`; one that fails raises
  naming the failing file, and the reports already returned are the partial-write record
  the orchestrator holds (Failure visibility).
- **Rendering**: fixed templates, entries sorted by ID, generated-file header first line;
  byte-identical for identical YAML (R9). `coverage.md` carries the gap report.
- **R20 support**: `raw_bytes_match` compares the canonical YAML set — the entry-based
  files plus state, the spec's definition of canonical YAML; `config.yaml` is outside the
  comparison (a pre-existing config is legitimate on the fresh path and is never rewritten
  by Blare). Every file at those paths on disk must exist in the loaded baseline with
  identical bytes and vice versa, so a file edited, created, or deleted at a compared path
  mid-run all return false. A pure byte comparison, no validation: a mid-run hand edit
  surfaces as the R20 "changed mid-run" abort, not as a validation error. The fresh-run
  baseline is empty (`init_inspection` verified nothing was at those paths), so any
  compared-path file appearing mid-run fails the check.

## Error handling

`StructuralValidationError`, `StateMissingError`, `PreexistingFilesError`, `ConfigError`,
`SchemaVersionError` — distinct identities, each carrying file path, problem, and next
action, ready for R13 rendering; all derive from the system's one error type. `batch_check`
never raises for content problems: it returns a rejecting verdict with reasons (the tool
result the model sees); it raises only on programmer error.

## Failure visibility

Refusals name file and problem verbatim (R19/R23/R24 messages are these errors rendered).
Each write primitive returns a `WriteReport` listing every file it wrote or skipped, which
the orchestrator records in the run log as it drives them — a partial write (R20 crash
case) is reconstructable from the reports returned before the failing primitive raised,
plus the failing file named in its error.

## Test plan

Fakes: `FakeStack` — a pure verdict table (expression → ok/error), installed by
monkeypatching the stack registry, since `load`/`empty_set` resolve the stack internally.

Contract tests, one per behaviour:

- `state_exists` both ways; `load` without state raises `StateMissingError` naming
  `blare analyze`; `empty_set` yields a valid, semantically empty set with the default
  config.
- load of a valid set round-trips every entry type, state, config, and the raw bytes.
- one red case per R19 clause: schema conformance (bad enum, wrong-file ID prefix — which
  is also how any cross-file duplicate manifests), duplicate ID within a file, dangling
  reference from a non-user-visible entry, `caused_by`
  cycle, missing severity/flag/status, excluded without reason, missing coverage entry for
  an excluded failure mode, duplicate coverage key, a syntactically unparseable entry file,
  unparseable state, state missing a field, headerless derived-path file, state present
  with an entry file missing; each names file and problem.
- `init_inspection`: refuses on a pre-existing entry file and on a derived-path file; a
  lone config does not trigger; names the offending files.
- config and stack resolution: a valid config resolves its stack; unparseable →
  `ConfigError`; unsupported name → `UnsupportedStackError` listing supported values;
  missing config in update mode → `ConfigError` naming the file and the supported values
  (R23); missing config in
  analyze mode with state present → default resolved, set flagged to create the file at
  write; `empty_set` honors an existing config (including refusing an unsupported one) and
  defaults when absent.
- schema version mismatch names both versions (distinct from structural failure).
- semantic: unmapped non-excluded failure mode; invalid expression (via FakeStack); alert
  severity below its most severe failure mode (→ phase 4); alert↔coverage linkage
  disagreement; metric recommendation with empty linkage (→ phase 3) and alert
  recommendation with empty linkage (→ phase 4); an excluded failure mode with alert-side
  coverage (→ phase 4) and with metric-side coverage (→ phase 3); a violating set still
  loads; every violation carries its kind's repair phase.
- `referencing_phases` attributes coverage references by side — a changed metric or
  metric-recommendation ID referenced in a coverage entry yields phase 3, a changed alert
  ID yields phase 4 — and otherwise returns exactly the phases owning referencing entries.
- batch check rejects: mistagged phase edit, phase-4 edit to a coverage metric side,
  `add`/`remove` ops targeting coverage entries, removal that dangles a reference, cycle
  introduction, duplicate-ID add, malformed edit payload, bad expression, bad rule fields
  (invalid `for_duration` via `validate_rule_fields`); accepts a clean batch.
- apply is pure: input set unchanged, candidate reflects edits; a batch adding a failure
  mode yields its empty coverage entry in the candidate, and removing one drops its entry
  (mechanical completeness).
- surgical write: hand-formatted untouched entry keeps exact bytes after an edit elsewhere;
  an empty edit set with an unchanged SHA produces zero byte changes; an empty edit set
  with a new SHA changes exactly the state file (the R9/R18 SHA-only advance); unchanged
  YAML renders byte-identical docs twice (R9); a manually edited derived doc is restored
  (R10).
- write primitives: a fresh set's first write creates every canonical file, zero-entry
  files included; the default config is created when none exists and an existing one is
  preserved byte-identically; each primitive's report lists exactly its own files.
- rendering: the generated-file header is the first line of every derived doc; entries
  appear sorted by ID; `coverage.md` contains the gap report.
- `raw_bytes_match`: true on untouched disk; false after a canonical hand edit, after a
  canonical file is deleted, and after a new file appears at a canonical path (including
  over the empty fresh-run baseline); unaffected by a derived-doc edit or a stray file.
- gap counts match the coverage-status split.

Failure-mode tests, per dependency:

- filesystem: unreadable artifact file (permission removed) → structural error naming it;
  a failure injected (`_write_file` monkeypatched) mid-`write_entries_and_config` → the
  primitive raises naming the failing file, the state file is untouched on disk, and the
  reports returned so far show exactly what landed — asserting the R20 partial-write
  contract on observable filesystem state.
- stack: validator raising → treated as an invalid-expression verdict carrying the message,
  never a crash (FakeStack armed to raise).
- ruamel round-trip: a YAML construct round-trip mode cannot preserve (rare) → load error
  naming the file, not silent reformatting (fixture file with such a construct).
