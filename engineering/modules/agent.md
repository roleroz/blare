# Module design — agent

## Decisions needed from you

This section contains only open items. **No open items** — the seam variable name and fixture
format below are implementation detail. 

## Responsibility

The Claude Agent SDK boundary (architecture): one session per run, subscription-login
preflight, phase prompts, chat pass-through, the two structured tools backed by injected
handlers, transcript persistence. The SDK client behind this module is the system's only
mock boundary.

## Interface

```python
def create_client() -> SDKClient        # env-var seam selection (see Client seam)

class AgentSession:
    def __init__(self, client: SDKClient, sink: EditSink, control: RunControlHandler,
                 stack: ObservabilityStack, transcript: TranscriptWriter) -> None
    def start(self, mode: RunMode, context: RunContext) -> None   # raises AuthRequiredError
    def triage(self) -> None            # update mode: produce the R18 verdict
    def run_phase(self, phase: Phase) -> None
    def chat(self, text: str) -> str
    def request_repair(self, phases: list[Phase], violations: list[Violation]) -> None
    def notify_amendment_outcome(self, approved: bool,
                                 restored_phases: list[Phase]) -> None
    def close(self) -> None
    @property
    def transcript_path(self) -> Path   # delegates to the injected TranscriptWriter
```

`close` ends the SDK session only — the orchestrator owns the `TranscriptWriter` and closes
it itself. `close` is idempotent and safe after any `AgentSessionError`.

- This module owns the prompt templates: `run_phase(phase)` composes and sends that phase's
  instructions — the phase contract, the failure-chain and early-detection principles, and
  the stack fragment for phases that have one (`instrumentation_hints()` in phase 3,
  `alerting_hints()` in phase 4; phases 1 and 2 carry none); callers pass no instruction
  text. Dynamic context
  (worktree root; in update mode the effective delta's file list and patch text) enters once
  through `RunContext` at `start`.
- `triage` drives diff-mode's first step: it sends the triage instruction — the effective
  delta's file list and patch text (from `RunContext`; the delta travels in the triage
  message, not in phase prompts) plus the verdict contract (answer through run-control
  with an `affected_verdict` or a `no_impact` conclusion) — and returns once a verdict has
  arrived (R18). It is an error for the model to end its
  triage turn without one — the session reminds it once, then raises `AgentSessionError`.
- `run_phase` returns when the model completes its phase turn; results live in the candidate
  set (edits flowed through the sink), so there is nothing to return — checkpoint rendering
  derives from the candidate set (orchestrator).
- `EditSink` and `RunControlHandler` are orchestrator-injected callables; their verdicts
  become tool results verbatim. Either handler *raising* (programmer error, not a rejecting
  verdict) fails the session with `AgentSessionError`; exit codes are the orchestrator's to
  assign, and R20 holds.
- `request_repair` is the channel for every system-initiated repair: the approval-gate
  system amendment, and the load-seeded violations of R18 (in update mode the orchestrator
  calls it right after triage seeds the queue, naming the repair phases and violations) —
  loaded-state violations do not travel in `RunContext`. It is also how the orchestrator
  resumes an agent-proposed amendment whose turn ended after the `amend_proposal` but
  before `amend_complete` — such a turn end is legal, the proposal stands, and the
  orchestrator drives the repairs through `request_repair` once it has opened the phases;
  on that resume path `violations` is empty and the message instead states that the
  standing proposal's named phases are now open. Cascade expansion rides the same call:
  when the recompute after an `amend_complete` joins further phases to the unit, the
  orchestrator issues another `request_repair` naming them (violations carried when the
  join came from the invariant half, empty for pure reference invalidation); each call
  waits for its own `amend_complete`, and the unit closes when a recompute adds nothing.
  The message discriminator between the two empty-violations classes is the session's own
  state, not the argument: the resume wording is used iff the session holds an unresolved
  `amend_proposal` from a prior drained turn; otherwise an empty-violations call gets the
  cascade wording (the phases joined the open unit through reference invalidation). Turn
  boundaries: every driving call —
  `triage`, `run_phase`, `chat`, `request_repair`, `notify_amendment_outcome` — drains the
  model's turn to its end before returning, so no call ever leaves a turn in flight;
  `chat` returns the turn's text blocks concatenated (the empty string when the turn
  produced only tool calls), with those tool calls handled normally; `triage` and
  `request_repair` then check that their required event (a verdict; `amend_complete`)
  arrived during the turn, remind once via a follow-up message when it did not, and raise
  `AgentSessionError` after a second eventless turn.
- `notify_amendment_outcome` closes the loop on every amendment unit (R2): it sends the
  model a structured message stating approval, or rejection with the restored phases —
  without it the session's context would keep the rejected repair batches as accepted and
  the model would build later work on entries that no longer exist. It blocks until the
  model's acknowledgment turn ends; anything the model does in that turn flows through the
  normal tool handlers (a batch against a re-frozen phase is rejected by the sink as
  usual; a fresh `amend_proposal` is legal and starts a new unit). The approved and
  rejected fixture variants are distinguishable at the SDK boundary precisely by this
  message and what follows it.
- `TranscriptWriter` is constructed by the orchestrator (which owns the state-dir path
  scheme, repo-id via gitrepo) and injected; this module writes through it and exposes its
  path.
- The model is not pinned: the session runs on the user's Claude Code default, in
  subscription mode (spec constraint — no API billing).

## SDK usage

- Session: `claude_agent_sdk.ClaudeSDKClient` held open for the whole run (one session per
  run, D8), streaming input for chat pass-through.
- Tools: an in-process MCP server exposes exactly two tools — `propose_edits(batch)` and
  `run_control(action, payload)` (`affected_verdict` | `no_impact` | `amend_proposal` |
  `amend_complete`) — schemas mirroring artifacts' `EditBatch` and the orchestrator's
  run-control payloads. Filesystem read tools are the SDK's own; the session runs with
  write tools disallowed — the target repo is read-only to the model, edits flow only
  through `propose_edits`.
- System prompt per mode (analyze / update), assembled by this module. Phase prompts carry
  the stack's fragment for their phase: `instrumentation_hints()` in phase 3,
  `alerting_hints()` in phase 4.
- Auth preflight: `start` performs a minimal SDK handshake; an SDK auth error maps to
  `AuthRequiredError` whose message names running `claude` and logging in (R12). No
  credential is ever read or written by Blare.

## Client seam and fixtures

`SDKClient` is a small protocol wrapping the SDK client surface this module uses.
`create_client()` selects on one environment variable — the architecture's single test seam:

- unset — the real SDK client (subscription).
- `BLARE_SDK_FIXTURES=replay:<dir>` — the fixture-replaying client (the e2e mock). Matching
  semantics: each outbound message is compared byte-exact *after one normalization* — the
  run's worktree root (known to this module from `RunContext`) is replaced by a fixed
  placeholder in both the recording and the live message, since e2e runs replay
  release-captured fixtures inside per-test temporary repositories whose absolute paths
  differ every run. Nothing else is normalized: e2e tests construct their repositories to
  reproduce the recorded content exactly, which is deterministic given identical file
  bytes (git patch text, blob hashes included, is a function of content). Any remaining
  divergence is a hard `FixtureMismatchError` naming the first mismatch, never a silent
  improvisation. Closing the session with recorded events still unconsumed is legal, not a
  mismatch — abort-path e2e tests replay a longer fixture and end early.
- `BLARE_SDK_FIXTURES=record:<dir>` — the real client wrapped in a recorder; used by
  release-suite runs, producing the fixture files the replaying client consumes (the
  record-then-replay cycle the global mock rules require). The recorder performs the
  capture-side half of the normalization: it substitutes the placeholder for the capture
  run's worktree root as it writes, so recordings are portable and the replay comparison
  is placeholder-to-placeholder. On the inbound side the replaying client re-roots the
  placeholder to the current run's worktree root as it emits, so replayed tool calls and
  edit batches carry valid paths for the run in progress and e2e byte assertions stay
  deterministic.

Fixture format: one directory per scenario, JSONL of `{direction, event}` entries in order,
scrubbed before commit per the global recording rules; each file records capture date and
SDK version.

## Transcripts

Every SDK event (both directions, tool calls and results included) is appended as JSONL
through the injected `TranscriptWriter`, flushed per event so a crash loses nothing already
exchanged (R14, R20). The path (under `$XDG_STATE_HOME/blare/<repo-id>/transcripts/`, built
by the orchestrator) is exposed via `transcript_path` for the run summary.

## Error handling

All three types derive from the system's one error type (architecture), carrying cause and
next action (R13):

- `AuthRequiredError` — next action: run `claude` and log in (R12).
- `AgentSessionError` — transport, rate/overload, protocol failure, a raising handler, a
  triage turn without a verdict, a repair turn without `amend_complete` after the
  reminder, or a transcript write failure (an unwritable transcript
  aborts the run — R14 is a hard requirement, and losing the diagnostic record silently
  would violate it). Carries the SDK error (or underlying cause), a context label — the
  phase in progress when one is open, otherwise the driving call's name (`start`,
  `triage`, `notify_amendment_outcome`, or `request_repair` with its phase list) — and
  whether a tool call was in flight; next action: re-run, and read the transcript at
  the named path (when one was written).
- `FixtureMismatchError` — replay divergence, a missing scenario, a malformed or
  unreadable scenario file (truncated line, unparseable entry, missing capture metadata),
  a recording failure (unwritable `record:<dir>` at `create_client`, or a mid-capture
  write failure, which also deletes the partial scenario), or a malformed
  `BLARE_SDK_FIXTURES` value at `create_client` (naming the expected `replay:<dir>` /
  `record:<dir>` forms); test-only paths but hard, named errors whose next action is: fix
  or re-record the named scenario, or correct the variable to one of the named forms.

Tool payloads that fail schema validation return an error verdict to the model (the SDK
retry loop handles it); they never raise into the run.

## Failure visibility

The transcript is the diagnostic record: every prompt, event, tool call, and verdict is in
it, flushed eagerly, path printed at run end (R14). `AgentSessionError` messages carry the
underlying error verbatim plus the phase in progress, so "the agent died" is always
attributable.

## Provisional mocks — unverified

All SDK fixtures are provisional until first captured against the real SDK by a
release-suite run against `~/external_git/miniflux_v2`; emptying this list is a
precondition for the first release. Entries are captured by the release suite's scripted
scenarios (architecture, Test strategy) unless a dedicated capture path is named:

- analyze happy path (four phases, approvals only)
- analyze re-run over an existing state file (R16 re-analysis, edits against existing IDs)
- update with an affected subset of phases
- update no-impact conclusion (R18), and its chat-redirected variant
- update dynamic expansion: a revised `affected_verdict` opening a phase mid-run, including
  a behind-position phase
- checkpoint chat that alters results (R2)
- agent-proposed amendment, approved; and rejected (restore)
- amendment cascade: a unit spanning multiple phases, approved; and rejected as one unit
- system-originated amendment (semantic violation at the approval gate)
- update whose affected phases were seeded by a load-time semantic violation (R18),
  repaired via `request_repair`
- auth-failure handshake shape (R12) — captured by a dedicated logged-out release
  scenario: the suite runs `blare` once with a scratch `HOME` carrying no credentials
- the minimal handshake fixture T1.1 hand-authored to reach session start
  (`tests/fixtures/claude-sdk/handshake/handshake.jsonl`, marked provisional in the file):
  a metadata line plus one `session_ready` handshake event, this task's own guess at the
  format below; T2.1 may reshape it once the real SDK handshake shape is known — it
  supersedes this entry rather than adding to the release-capture list, since the full
  analyze-happy-path fixture (already listed above) subsumes a bare handshake

The transport-error and rate/overload shapes are deliberately *not* fixture entries: both
are typed exception classes of the pinned SDK, verified by unit tests importing those
classes from the real package — the fakes script the SDK's own types, so there is no wire
shape to record and nothing left unverified. The amendment resume path (a turn ending
after `amend_proposal`, before `amend_complete`) is likewise excluded: the fake scripts a
sequence that is legal under the SDK's turn contract rather than an observed response
shape — there is no deterministic way to make the live model end its turn there — and the
recorder captures one opportunistically if a release run ever produces it.

## Test plan

Fakes: `FakeSDKClient` — scripted event streams per scenario; models conversation state
(records every outbound message) so tests assert on prompts actually sent, not on call
counts. `FakeTranscriptWriter` holds written events in memory and can be armed to fail.

Contract tests, one per behaviour:

- both tools registered with the exact schemas; no write tool available to the model.
- an inbound `propose_edits` call reaches the injected sink verbatim; the sink's verdict is
  returned to the model unchanged; same for `run_control` and each action kind.
- the system prompt differs by mode and is sent at `start` (asserted on recorded outbound).
- `run_phase` sends that phase's template including the phase contract; phase 3's prompt
  contains `instrumentation_hints()` output, phase 4's contains `alerting_hints()` output,
  and phases 1–2 contain neither.
- `triage` sends the delta file list, the patch text, and the verdict contract (asserted
  on recorded outbound); phase prompts do not carry the delta.
- `request_repair` wording follows the standing-proposal discriminator: with an unresolved
  proposal held, an empty-violations call states the standing proposal's phases are open;
  with none held, the same arguments produce the cascade wording.
- `notify_amendment_outcome` returns when the acknowledgment turn ends.
- a turn ending with an unresolved `amend_proposal` returns normally; a subsequent
  `request_repair` resumes it.
- `triage` returns after an `affected_verdict`; after a `no_impact`; reminds once and then
  raises when the turn ends verdict-less.
- chat text passes through to the live session; the reply is the turn's concatenated text
  blocks, empty when the turn was tool-calls-only, and the turn is drained at return.
- a truncated or unparseable scenario file raises `FixtureMismatchError` naming the file;
  so do an unreadable file and one that parses but lacks the capture date or SDK version.
- recorder failures: an unwritable `record:<dir>` raises `FixtureMismatchError` at
  `create_client`; a write failure mid-capture raises `FixtureMismatchError` and deletes
  the partial scenario, so a truncated leftover can never later fail replay as a
  malformed file.
- `request_repair` names the phases and violations in the message sent; returns on
  `amend_complete`; reminds once then raises on a completion-less turn.
- `notify_amendment_outcome`: the approved message states approval; the rejected message
  names every restored phase (asserted on recorded outbound — this is what distinguishes
  the two fixture variants).
- transcript contains every exchanged event in order, flushed after each (asserted
  mid-session on the fake writer); `transcript_path` reports the writer's path.
- `create_client`: unset → real; `replay:<dir>` → replaying client; `record:<dir>` →
  recorder; malformed value → error naming the expected forms.
- replaying client: identical run replays deterministically; a run from a different
  worktree root replays cleanly (the outbound normalization), and the events it emits
  there carry the new root, not the placeholder or the capture root (the inbound
  re-rooting, asserted on the batch the sink receives); any other divergence raises
  `FixtureMismatchError` naming the mismatch; closing with recorded events unconsumed is
  legal and raises nothing.
- driving calls drain the turn: after `triage` returns, no turn is in flight (asserted on
  the fake's stream state).
- record-then-replay round trip: a session run through the recorder (over the fake real
  client) produces a scenario directory — JSONL entries in order, capture date and SDK
  version present — that the replaying client then replays to an identical event stream.
- `close` ends the SDK session, is idempotent, does not close the TranscriptWriter, and is
  safe after a session error.

Failure-mode tests, per dependency:

- SDK client: auth failure at `start` → `AuthRequiredError` naming the login step; transport
  error mid-phase → `AgentSessionError` carrying phase, cause, and the in-flight flag;
  rate/overload → same type, distinguishable message; protocol failure (a scripted
  malformed, out-of-contract event in the stream) → `AgentSessionError`; malformed tool
  payload → error verdict returned, session continues, nothing raised; fixture scenario
  missing → `FixtureMismatchError`. The transport and rate/overload fakes script the pinned
  SDK's own typed exception classes, and a unit test imports those classes from the real
  package so the scripted types cannot drift.
- TranscriptWriter: armed to fail on write → `AgentSessionError` naming the transcript path
  and the write failure; session closed.
- Injected handlers: sink raising → `AgentSessionError`; run-control handler raising →
  `AgentSessionError` (each distinct from a rejecting verdict, which is returned to the
  model — tested side by side).
- stack: no failure-mode tests by design — this module's whole stack surface is
  `instrumentation_hints()` and `alerting_hints()`, both pure in-process calls returning
  static text (the verdict functions are artifacts' consultations, not this module's);
  there is nothing this module can observe failing.
