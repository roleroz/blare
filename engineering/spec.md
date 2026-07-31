# Blare — Specification

## Decisions needed from you

This section contains only open items — the absence of a topic means it is settled and logged in
`engineering/decisions.md`.

**No open items at spec level.** All decisions raised by this document (D1 test codebase/stack,
D2 checkpoint structure, D3 failure-mode model, D4 derived documentation, D5 git interaction)
are settled and logged in `engineering/decisions.md`. Two items are explicitly deferred to the
architecture phase, not open here: the CLI command names, and whether the personal configuration
layer is needed in the MVP.

**Changes since last approval**: Non-goals gained "configurable model selection" — the MVP
runs on the Claude Code subscription's default model rather than pinning one; a config field
to select or pin a model is future work. Surfaced during T4.1 (release suite) prep, since
`create_client`'s live branch needs a model-selection stance before it can be built; logged in
`engineering/decisions.md`. Diagnosability gained **R25** (progress feedback during any
agent-driving call), approved 2026-07-31 — discovered via live user testing against
`~/blare_test/oauth2-proxy`, where a run gave zero console indication of which phase was
active or whether it was still alive across phases that ran for minutes to nearly two hours.
Architecture and cli.md/orchestrator.md now need to work out R25's implications before it's
implemented. Run lifecycle gained **R26** (`--unattended`: auto-approve everything, a round
cap, a completion bell), approved 2026-07-31 — a timing analysis of that same run found ~10
hours dominated by two checkpoints the model finished in minutes and nobody was watching.
Non-goals also gained a named, deferred future-work item, a passive `blare review` command —
explicitly not designed or built now. Architecture and the affected module docs still need
to work out R26's implications before it's implemented.

---

## What Blare is

Blare is an AI agent that gives a service the observability it needs for production use. Given a
codebase, it documents the service's failure modes, inventories the metrics the code actually
implements, compares them against what the failure modes require, and recommends the metric
changes and alert definitions needed so every failure mode can be alerted on. Its artifacts are
designed to later guide an agent that implements the recommendations — that implementation is
future work, not part of Blare's MVP.

First principles:

- **Alerting is the target.** Documenting failure modes is the first step, not the goal; every
  failure mode must end in an alert recommendation or an explicit, reasoned exclusion.
- **Failure chains, not just user-visible ends.** A user-visible failure is usually the last
  link of a chain. Blare documents the upstream links as failure modes in their own right so
  each can be detected as early as possible.

## Terms

- **Failure mode**: one way the service can fail, documented as one artifact entry with a
  stable ID.
- **User-visible failure mode**: a failure mode whose effect is directly observable by the
  service's users. Marked by a boolean field on the entry; the analysis, not the graph shape,
  decides it.
- **Severity**: `critical` (demands immediate human intervention — this alert pages) or
  `warning` (demands action soon — this alert files a ticket). Every failure mode carries one.
- **Coverage status**: every failure mode carries exactly one of:
  - `alertable` — the implemented metrics suffice to detect it; only the recommended alert
    rule(s) are missing;
  - `metric-gap` — the implemented metrics cannot detect it adequately; metric changes are
    needed in addition to alert rule(s);
  - `excluded` — deliberately not covered, with the reason recorded (e.g. not detectable,
    accepted risk).
- **Gap**: any non-excluded failure mode. The MVP reports every one of them as a gap — it
  cannot verify alert-rule implementation (it does not inventory existing alerting
  configuration — future work), so no gap is ever reported closed in the MVP; the coverage
  status tells the user what kind of work closes it (alert rules only, or metric changes too).
- **Effective delta**: the diff from the recorded analyzed SHA to the current commit, excluding
  everything under `.blare/`.
- **Zero diff**: the run changed no file's bytes anywhere in the repository.
- **Canonical form** (of a derived doc): the bytes produced by rendering the current YAML;
  rendering is deterministic, so unchanged YAML renders byte-identically.
- **Final confirmation**: the user approval after which a run writes to `.blare/`. In full
  analysis, approval of the phase-4 checkpoint; in diff mode, approval of the last affected
  phase's checkpoint, or — when the delta affects no artifacts — confirmation of the agent's
  no-impact conclusion.

## Users

Engineers who operate services in production and manage their code in git. Any number of
engineers may author the commits in a delta; what the MVP assumes is that *analysis runs* are
serialized on one branch lineage (in practice: run Blare on the default branch), and that one
user at a time runs it from a terminal on the machine where the codebase lives.

## Scope (MVP)

Two run modes, both through a CLI:

1. **Full analysis** (`blare analyze`): analyze a codebase, produce the artifact set under
   `.blare/`, record the analyzed commit. Autonomous with checkpoints: the run has four
   phases — (1) system map: the agent's understanding of what the service does and its
   dependencies, persisted as an artifact like every other phase's output; (2) failure modes;
   (3) metric coverage: implemented-metric inventory, each failure mode's coverage status, and
   metric-change recommendations; (4) alert recommendations — and pauses at each of the four
   phase boundaries (including after phase 4, whose approval is the final confirmation) to
   present results and take free-form chat guidance before continuing. Each artifact belongs
   to the phase that produces it: the system map to (1), the failure-mode inventory to (2),
   the metric inventory and metric-change recommendations to (3), the alert recommendations to
   (4); the coverage mapping spans (3) (which metrics detect each failure mode) and (4) (which
   alerts fire on them) and counts as affected when either side is. Approving a checkpoint
   freezes that phase's results for the run; a later phase may surface an amendment to an
   earlier phase, which is re-presented for approval rather than silently applied.
2. **Diff mode** (`blare update`): on an already-analyzed codebase, compute the effective delta
   (any number of commits, any number of authors), update only the artifacts that delta
   affects, and record the new analyzed commit. Same checkpoint-and-chat behavior, pausing only
   at phases whose artifacts the delta affects.

At a checkpoint the user either approves (possibly after revising results through chat) or
aborts the run; aborting writes nothing (R20) and exits non-zero. When a later phase's
amendment to an earlier phase is rejected, the run continues with the earlier phase's frozen
results.

Command names are provisional until the architecture doc.

## Non-goals (MVP) — the future-work list

- **Implementing the recommendations**: writing the recommended metrics into the target
  codebase or deploying the recommended alerts. The artifacts must carry enough detail to guide
  a build agent later.
- **Inventorying existing alerting configuration** (e.g. alert rule files in the repo): the
  MVP recommends alerts but cannot verify they exist, so no gap is ever reported closed.
- **CI/CD integration** (e.g. GitHub Actions running diff mode on merge) and any
  non-interactive operation. `--unattended` (R26) does not reopen this: it still needs an
  interactive session to start (R22 unchanged), it just never pauses once running.
- **Monorepos and git submodules.**
- **Concurrent analysis on divergent branches**: merging two independently produced artifact
  sets. The MVP serializes analysis on one branch lineage and fails safely when that assumption
  breaks. A true merge of two analyzed branches keeps ancestry intact: git merges `.blare/`
  (same-ID edits conflict textually and the user resolves them), the surviving recorded SHA is
  an ancestor, and the next `blare update` re-analyzes the other side's delta, converging per
  R9. Two complementary solutions for later:
  - *Prevention*: the CI/CD integration above — diff mode runs post-merge on the default
    branch, so analysis is serialized by the merge queue and branches never carry their own
    artifact edits.
  - *Repair* (`blare merge` or similar): structural three-way merge of the YAML keyed by stable
    entry IDs (not line merge — same-ID edits conflict, disjoint entries union cleanly), reset
    the recorded SHA to the branches' merge-base, then run diff mode over the combined delta.
- **Artifact schema migration**: the MVP refuses on a schema-version mismatch (R24) rather than
  migrating.
- **Resuming an interrupted run**: MVP runs are atomic (R20); an aborted run is re-run.
- **A passive review command** (`blare review`, working name) over an already-written
  `.blare/`: walk the existing phases for read-only browsing straight from disk, with no
  agent session and no inference at all unless the user chats — at which point any resulting
  edit would flow through the same propose_edits/amendment/write machinery every other mode
  already uses. Complements `--unattended` (R26): run unattended, then review and steer
  after the fact instead of during. Not designed or built in the MVP.
- **Additional metrics/alerting stacks** beyond Prometheus; the stack interface is abstracted
  from the start, but only the Prometheus implementation ships.
- **API-billing mode**: Blare uses the Claude Agent SDK in subscription mode only.
- **Configurable model selection**: the MVP always runs on the Claude Code subscription's
  default model rather than pinning one. A config field to select or pin a specific model is
  future work.
- **Non-git codebases; non-Linux hosts; any UI beyond the CLI.**

## Artifacts

All artifacts live under `.blare/` at the target repo's root — the location is fixed in the
MVP, not configurable. The canonical artifacts are structured YAML in which every entry — a
system-map component, failure mode, implemented metric, or recommendation (metric change or
alert) — has a stable ID; human-readable markdown views are derived deterministically from the
YAML and committed alongside it, carrying a "generated — do not edit" header. The agent never
regenerates documents: it proposes structured edits — add, update, or remove entries by ID —
and an empty edit set leaves every entry-based file untouched; the only writes that exist
outside the edit set are the state SHA advance and derived-doc restoration (R9, R10).

Blare writes the files but never runs a git write operation; the user reviews and commits
them — committing is acceptance. Hand-editing the canonical YAML is supported — it is how a
user records e.g. an accepted-risk exclusion outside a run; Blare validates the YAML on load
(R19) and treats it as the current state of the analysis. Derived docs are not an input: on
each successful run they are restored to the canonical form of the current YAML.

The artifact set comprises:

- the **system map**: the analyzed service's components, external dependencies, and entry
  points — the phase-1 output, and the recorded understanding diff mode updates;
- the **failure-mode inventory**: entries with optional `caused_by` references to other
  entries' IDs, forming a causal graph, each carrying a severity, a user-visible flag, and a
  coverage status;
- the **metric inventory**: metrics the codebase actually implements, each tied to where in the
  code it is emitted;
- the **coverage mapping**: one entry per failure mode, excluded ones included — the metrics
  (existing or recommended) that detect it and the recommended alerts on those metrics.
  Excluded failure modes appear with empty metric and alert sets; their reason lives on the
  failure-mode entry;
- **recommendations**: metric changes and alert definitions (expressions in the configured
  stack's language — PromQL in the MVP) needed to close the gaps;
- **state**: the analyzed commit SHA and the artifact schema version. The SHA changes only
  when a Blare run records an analysis, or by hand in the R15 recovery — ordinary development
  never touches it, and it is expected to lag HEAD between runs;
  the lag is the queue of unanalyzed work. The state file's presence is what marks a codebase
  as analyzed: `blare analyze` without it initializes (R1), with it re-analyzes (R16);
  `blare update` without it refuses (R17);
- **config**: repo-shared settings (see Configuration).

State and config belong to the artifact set but are not entry-based: the state file is plain
fields, hand-editable per R15 — its parseability and required fields are checked by R19, its
SHA semantics by R15, its schema version by R24 — and the config file is owned and validated
by R23. "Canonical YAML" in this document means the entry-based artifacts plus state. Files
under `.blare/` at paths Blare does not use are ignored and never touched.

Exact file layout is an architecture-doc concern.

## Configuration

Two layers:

- **Repo-shared** (`.blare/config.yaml`, committed): settings every user of the repo needs — in
  the MVP, the configured metrics/alerting stack. Created by `blare analyze` with defaults
  (stack: `prometheus`) when absent; an existing config is never overwritten (R23).
- **Personal** (`~/.config/blare/`, never committed): per-user settings. Credentials are never
  stored in the repo. Authentication is delegated to the Claude Code subscription login via the
  Agent SDK; Blare stores no API key. Whether the personal layer is needed at all in the MVP is
  verified at architecture time.

Run transcripts (R14) and the run lock (R21) live outside the target repository, in per-user
state locations named in the architecture doc — never under `.blare/`, so they can neither
appear in commits nor violate the zero-diff guarantees.

## Requirements — acceptance criteria

End-to-end tests trace to these criteria. "Zero diff", "effective delta", "final confirmation",
and other terms are as defined in Terms. R3–R5 state invariants of the artifact set: any run
that reaches final confirmation must write a set that satisfies them — a violation already
present in the loaded state (e.g. from hand edits) makes the phases needed to repair it
affected phases in diff mode (R18). Their placement under "Full analysis" names where they are
first established, not their scope.

### Full analysis

- **R1** — Running `blare analyze` in a git-managed codebase with no state file produces the
  full artifact set: system map, failure-mode inventory, metric inventory, coverage mapping,
  recommendations, state, derived docs, and a default config (unless a config already exists,
  which is kept); every entry has a stable ID; state records the SHA of the analyzed commit.
  If entry-based canonical files, or files at the paths derived docs use, exist without a
  state file, the run refuses and names them rather than overwriting them; an existing config
  alone does not trigger this.
- **R2** — The run pauses at all four phase boundaries (including after the final phase),
  presents that phase's results, and accepts free-form chat that can alter the results before
  the run continues; it proceeds only on explicit user confirmation, and the user can instead
  abort at any checkpoint (per R20, nothing is written). An amendment to an already-approved
  phase is re-presented for approval, never applied silently; a rejected amendment leaves the
  earlier phase's frozen results in force and the run continues. An approved amendment updates
  the amended phase's results, and any already-frozen intermediate phase whose results it
  invalidates is re-presented as an amendment itself before the final confirmation. The
  amendment and its cascade are accepted or rejected as one unit: rejecting any cascaded
  re-presentation rejects the originating amendment as well, restoring the pre-amendment
  results of every phase involved, so the run never carries an inconsistent set to final
  confirmation.
- **R3** — Failure modes form chains: an entry may reference upstream causes by ID, and every
  user-visible failure mode's upstream causes are themselves documented entries, each with its
  own severity, user-visible flag, and coverage status.
- **R4** — Every failure mode carries exactly one coverage status; every non-excluded failure
  mode is mapped to at least one recommended alert whose expression is written in the
  configured stack's language; every excluded one records its reason. No failure mode is
  silently unmapped. A recommended alert serving several failure modes carries the highest
  severity among them.
- **R5** — Metric recommendations distinguish "new metric needed" from "existing metric
  insufficient" (e.g. missing label), and every recommendation names the failure mode(s) it
  serves.
- **R16** — Running `blare analyze` when the state file exists performs a full re-analysis
  expressed as structured edits against the existing entries: entries whose conclusions are
  unchanged keep their IDs and bytes; the run never discards and recreates the artifact set.
  State records the SHA of the analyzed commit, as in R1.

### Diff mode

- **R6** — `blare update` computes the effective delta, edits only the artifacts that delta
  affects, and records the delta's end commit as the new analyzed SHA. The end commit is
  captured at run start; if the repository changes while the run is in progress, the
  write-time re-check in R20 aborts the run.
- **R7** — When the effective delta is empty — the net diff outside `.blare/` from the
  recorded SHA to the current commit is empty: same commit, commits touching only `.blare/`,
  or a change and its revert — the run reports the analysis is up to date, exits 0, and
  produces zero diff; the recorded SHA is not rewritten. Detecting an empty delta is a git
  operation and never invokes the agent. Preflight refusals (R11, R21) and validation
  failures (R19, R23, R24) take precedence over the up-to-date success; the path needs no
  login, per R12's scope.
- **R8** — Diff mode handles a range spanning multiple commits as one delta, not per-commit.
- **R15** — If the recorded SHA does not resolve to a commit in the repository, or is not an
  ancestor of the current commit (e.g. after a hand-edit typo, a rebase, or other history
  rewrite), diff mode refuses to run and names the recovery options: re-run
  full analysis (R16), or hand-edit the recorded SHA in the state file to a real ancestor
  (hand-editing state is sanctioned, as for all canonical YAML).
- **R17** — `blare update` in a repo without the state file exits non-zero and names
  `blare analyze` as the first step.
- **R18** — Diff mode pauses only at the checkpoints of phases whose artifacts the delta
  affects. When the agent concludes a non-empty delta affects no artifacts, it presents that
  conclusion for confirmation (the final confirmation for that run), after which the only
  changes are the recorded SHA advancing and any derived-doc restoration (per R9 and R10).
  The affected-phase set is dynamic: when analysis or checkpoint chat reveals that a phase
  judged unaffected needs changes — including repairs to invariant violations already in the
  loaded state — that phase becomes affected and its checkpoint is presented, and the final
  confirmation is the last checkpoint actually presented. The no-impact conclusion is a
  checkpoint like any other: chat can redirect the run rather than only accepting or aborting.
  This is distinct from R7: R7's empty delta is detected without the agent and changes
  nothing; R18's no-impact delta was analyzed, and the SHA advance records that.

### Artifact stability

- **R9** — A run whose analysis reaches no different conclusions leaves every artifact
  unchanged: YAML entries are not rewritten, and derived docs regenerate byte-identically from
  unchanged YAML. Exactly two changes are permitted in such a run: the state file's analyzed
  SHA advancing after a non-empty delta was analyzed and found to need nothing — recording that
  those commits were considered is what prevents the agent from re-analyzing them on every
  later run — and the restoration of manually edited derived docs (R10).
- **R10** — Derived docs carry a generated-file header. On each run that reaches final
  confirmation they are restored to the canonical form of the current YAML, so a manual edit
  to a derived doc is overwritten without affecting the YAML; this restoration is the second
  permitted change named in R9. Runs that end before final confirmation — R7's empty-delta
  path, preflight failures, aborts — write nothing and restore nothing, so R7's zero diff is
  absolute.
- **R19** — Blare validates the canonical YAML on load and exits non-zero naming the file and
  the problem, modifying nothing, when any of these fail: schema conformance; every ID unique;
  no reference to a nonexistent ID (from any entry, `caused_by` or otherwise); the `caused_by`
  graph acyclic; every failure mode carrying severity, user-visible flag, and coverage status;
  every `excluded` entry carrying its reason. A `.blare/` directory whose state file exists but
  whose entry-based files are missing or unreadable fails this same validation, as does a
  state file that is unparseable or missing its SHA or schema-version field, and a file at a
  derived-doc path that lacks the generated-file header (such a file is not Blare's to
  overwrite). The config file is outside R19's scope: it is validated by R23.

### Run lifecycle

- **R20** — A run is atomic with respect to `.blare/`: nothing under it is created or modified
  before the final confirmation. Aborting at a checkpoint, a crash, or any exit before the
  final confirmation leaves `.blare/` and the recorded SHA untouched; any transcript already
  written (R14) survives. At final confirmation, before writing, Blare re-checks that the
  working tree outside `.blare/` still matches the commit captured at run start and that the
  canonical YAML still matches what the run loaded, and aborts without writing when either
  check fails — a repository that changed mid-run invalidates what was analyzed. The
  post-confirmation write orders the state file last: a crash mid-write leaves the old SHA
  with partially updated artifacts — converged by the next run (R9, R16) when they still
  validate, rejected by R19 when they do not — and because Blare never commits, git always
  shows the partial write and can revert it.
- **R21** — Two Blare processes started by the same user cannot run against the same repo
  concurrently: the second invocation exits non-zero naming the running one. The lock lives
  outside the repository (see Configuration), and a lock whose owning process is dead is
  reclaimed automatically. Concurrent runs by different users fall outside the single-user
  assumption (see Users) and are not guarded.
- **R22** — The MVP is interactive-only: when checkpoints cannot be presented (stdin is not a
  TTY), the run exits non-zero saying so instead of hanging or skipping confirmations. The
  check is itself a preflight check, firing before any agent session: such a run needs no
  login (R12) and writes no transcript (R14). A run that ends before any checkpoint would be
  presented — R7's up-to-date path, preflight failures — is unaffected by this rule.
- **R26** — Either mode accepts `--unattended`: every checkpoint, no-impact confirmation, and
  amendment (system-originated or agent-proposed) auto-approves without prompting or reading
  input, and the run proceeds straight through to the write; chat never happens, since
  nothing ever offers a prompt to type into. R22's TTY requirement is unchanged — unattended
  is a scoped exception to *pausing*, not to needing an interactive session to have started
  one; it is not a path to non-interactive/scripted invocation (see Non-goals). A hard cap on
  the total number of amendment rounds aborts the run, writing nothing (R20), if repairs have
  not converged within it — a bound `--unattended` needs precisely because nobody is present
  to notice or steer a non-converging loop the way interactive chat could. Once
  `--unattended` was given, the terminal rings a bell in addition to the ordinary summary or
  error (R13) at whatever ending the run actually reaches — success, this abort, a refusal,
  or any other failure — so a user who has stepped away is notified regardless of outcome
  without needing to watch the screen.

### Configuration & environment

- **R11** — Blare refuses to run, naming the reason: outside a git repository; in a repository
  with no commits yet; or when the working tree differs from HEAD outside `.blare/` (modified
  tracked files or untracked files, listed in the message; git-ignored files never count).
  Differences confined to `.blare/` never block.
- **R12** — A run that would invoke the agent, finding no Claude Code subscription login
  available, exits non-zero with a message naming the actual login step to take; runs that end
  before any agent session need no login. No credentials are read from or written to the
  target repo.
- **R23** — In either mode, an existing config naming an unsupported stack (or otherwise
  invalid) exits non-zero naming the file and the supported values; an existing config is never
  overwritten with defaults. A missing config at `blare update` time is the same error; at
  `blare analyze` time it is created with defaults.
- **R24** — A state file whose schema version does not match the running Blare's exits
  non-zero, naming both versions and the recovery options: run the Blare version matching the
  recorded schema, or delete `.blare/` and re-run full analysis — which discards hand-recorded
  content, and the message says so. Editing the version field by hand does not convert the
  artifacts and is not a recovery. Migration is future work.

### Diagnosability

- **R13** — Every failure exits non-zero and states the cause and the user's next action; every
  success summarizes what changed (counts of entries added / updated / removed, or "no
  changes") and states the current gap count: non-excluded failure modes, split by coverage
  status.
- **R14** — Every run that invokes the agent writes a transcript of the agent session
  (prompts, tool use, decisions) to a location outside the target repository, stated in the
  run's output, so any artifact change can be traced to the run and reasoning that made it.
  Runs that end before any agent session — R7's up-to-date path, preflight failures — write no
  transcript; their diagnosis is the R13 message.
- **R25** — While any agent-driving call is in progress (a phase run, triage, chat, or a
  repair), the terminal shows which phase or operation is active and periodic evidence the run
  is still alive — at minimum an elapsed-time tick and the name of the most recent tool call the
  model made — so a user watching a live session that runs for many minutes can distinguish
  "working" from "hung" without inspecting the transcript. This is presentation only: it never
  alters turn-taking and is never mistaken for a chat exchange.

## Constraints

- Implementation in Python; build system Bazel.
- Agent runs on the Claude Agent SDK in subscription mode (no API billing).
- Target codebases are git-managed; Blare runs on Linux.
- Artifacts live under `.blare/` at the target repo root.
- MVP metrics/alerting stack: Prometheus — instrumentation is detected via Prometheus
  client-library usage, and alert recommendations are Prometheus alerting-rule definitions
  with PromQL expressions. The test codebase is `~/external_git/miniflux_v2`.
- The metrics/alerting stack interface is abstracted so stacks beyond Prometheus can be added
  without changing the artifact schema.
