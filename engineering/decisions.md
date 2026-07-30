# Blare — Decision Log

Append-only. Each entry states what was chosen, what was rejected, and why. Entries are marked
superseded when reversed, never edited away.

## 2026-07-29 — Target-codebase analysis is language-agnostic

**Chosen**: the agent analyzes whatever language the target codebase is written in; no
per-language tooling in the MVP.
**Rejected**: constraining the MVP to Python-only or Go-only targets for more precise metric
extraction.
**Why**: the agent reads code directly, so language constraints buy little precision at a large
cost in utility; metric detection keys on the metrics stack's client-library usage rather than
on the host language.

## 2026-07-29 — Full analysis runs autonomously with checkpoints

**Chosen**: the agent works through analysis phases and pauses at each phase boundary to present
results and take free-form chat guidance before continuing.
**Rejected**: fully conversational (user drives every step — more control, slower, harder to
make repeatable); autonomous with chat only on demand (fastest, but guidance arrives after work
may need redoing).
**Why**: checkpoints put user guidance at the points where it changes downstream work, without
making the user drive.

## 2026-07-29 — Artifacts are canonical YAML with derived markdown

**Chosen**: structured YAML files with stable IDs per failure mode / metric / alert are the
source of truth; markdown views are derived deterministically from them. The agent edits entries
by ID and never regenerates documents.
**Rejected**: markdown as canonical with the agent editing it directly (simpler, but diff
hygiene depends entirely on prompt discipline); hybrid YAML-data + markdown-prose (structured
where it must not churn, but two sources of truth to keep aligned).
**Why**: preventing spurious diffs is a spec-level requirement (R7, R9); structured edits with
stable IDs make "no change in conclusions → no change on disk" enforceable by construction, and
stable IDs also enable the future structural merge of concurrent analyses.

## 2026-07-29 — MVP metrics/alerting stack: Prometheus, set by the miniflux test codebase

**Chosen**: the MVP targets Prometheus — instrumentation detected via Prometheus client-library
usage, alert recommendations emitted as Prometheus alerting-rule definitions with PromQL
expressions. Determined by the chosen test codebase, `~/external_git/miniflux_v2`, which is
instrumented with `prometheus/client_golang`. The stack interface is abstracted so other stacks
can be added without changing the artifact schema.
**Rejected**: assuming Prometheus + Alertmanager upfront before a test codebase existed; the
interim call (also this session) was to defer the stack decision until the test codebase was
chosen, and this entry resolves that deferral.
**Why**: developing against a real codebase's real stack keeps the MVP honest; miniflux has an
existing, partial Prometheus metric inventory, which exercises gap-finding rather than only
greenfield recommendation.

## 2026-07-29 — Checkpoint structure: four phases, approval freezes with amendments

**Chosen**: a full analysis run has four phases — (1) system map, (2) failure modes, (3) metric
coverage, (4) alert recommendations — with a checkpoint at each phase boundary. Approving a
checkpoint freezes that phase's artifact for the run, but a later phase may surface an
amendment to an earlier phase (e.g. writing alerts reveals a missing failure mode), which is
re-presented for approval rather than silently applied.
**Rejected**: fewer checkpoints (guidance arrives after more work is already done); a strict
freeze with no amendment path (forces a full re-run when late phases learn something).
**Why**: four phases match the natural dependency order of the analysis, and the amendment path
keeps late discoveries cheap without letting the agent silently rewrite approved results.

## 2026-07-29 — Failure-mode model: causal graph with per-entry severity

**Chosen**: each failure mode is one entry with a stable ID and optional `caused_by` references
to other failure-mode IDs, forming a causal graph; a user-visible failure is the end of a chain
whose upstream links are themselves documented, independently detectable entries. Every failure
mode carries a severity enum (`critical` / `warning`).
**Rejected**: a flat list with narrative chain descriptions (simpler to write, but cannot be
queried for "which upstream failures have no alert", which is the early-detection requirement);
deferring severity past the MVP (keeps the schema smaller but pushes the paging decision into
every alert's review instead of settling it once per failure mode).
**Why**: the causal graph makes early detection enforceable — upstream links are entries in
their own right, so their coverage gaps are visible — and an alert recommendation cannot be
written without deciding whether it should page.

## 2026-07-29 — Derived markdown views are committed to the target repo

**Chosen**: the markdown views derived from the canonical YAML are committed under `.blare/`,
generated deterministically and carrying a "generated — do not edit" header.
**Rejected**: generating them on demand (smaller repo, but analysis results become unreadable
in PR review, where coverage changes most need to be seen).
**Why**: reviewers read prose, not YAML; determinism keeps the committed views from churning
when conclusions have not changed.

## 2026-07-29 — Git interaction: the user commits, and dirty trees outside .blare/ block

**Chosen**: Blare never runs a git write operation (no add, commit, stash, checkout) — it
modifies files under `.blare/` and leaves them uncommitted for the user to review and commit;
committing is acceptance. Blare refuses to run when the working tree differs from HEAD anywhere
outside `.blare/` (modified tracked files or untracked files, named in the message);
differences confined to `.blare/` never block.
**Rejected**: Blare committing its own output (one-command convenience, but writes to history
without review and needs commit-message and authorship policy); analyzing a dirty tree with a
warning (friendlier mid-development, but attributes uncommitted content to a SHA that does not
contain it, and diff mode inherits the error).
**Why**: the artifacts are recommendations and the checkpoint model makes review the acceptance
step, so the commit belongs to the user; refusing on dirt outside `.blare/` is what keeps the
recorded SHA an honest statement of what was analyzed.
