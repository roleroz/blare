# Module design — agent

## Decisions needed from you

This section contains only open items. **No open items** — the seam variable name and fixture
format below are implementation detail.

**Changes since last approval**: `AgentSession.__init__` gained `on_activity` (R25, added
2026-07-31) — a callback invoked with the dispatched tool's name each time a tool call
arrives during a driving call, feeding the orchestrator's progress ticker. See Interface.

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
                 stack: ObservabilityStack, transcript: TranscriptWriter,
                 on_activity: Callable[[str], None] | None = None) -> None
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
- `on_activity` (R25) is invoked with a tool's name immediately when the model calls it,
  for *every* tool call in the turn — `propose_edits` and `run_control` alike, but also
  every filesystem read tool (`Read`, `Grep`, `Glob`, ...) the SDK itself executes: those
  dominate a phase's actual wall-clock time (a phase is mostly the model exploring the
  codebase, not proposing edits), so `on_activity` must not be scoped to Blare's own two
  tools or R25's progress line would sit stale through most of a long call. It fires from
  whatever thread is draining the turn — today, the same thread that called the driving
  method, since draining blocks synchronously — and fires independently of, not instead of,
  the propose_edits/run_control round trip those two tools still go through. It is a pure
  notification: its return value is ignored, and an exception it raises must not be allowed
  to break the turn (caught and dropped at the call site, not propagated as an
  `AgentSessionError` — R25 is presentation-only and must never affect a run's outcome).
  `None` (the default) means no callback runs.
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
  through `propose_edits`. `on_activity` (R25, Interface) sees every one of these calls
  too, not only the two above.
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
release-suite run; emptying this list is a precondition for the first release. Entries are
captured by the release suite's scripted scenarios (architecture, Test strategy) unless a
dedicated capture path is named.

**2026-08-01 revert — T4.1's `~/external_git/miniflux_v2` captures undone.** T4.1 had
captured seven scenarios for real against `~/external_git/miniflux_v2` (listed as
provisional again below). The user reverted all seven back to their hand-authored
provisional state, for two reasons, both decided by the user directly rather than found by
testing: (1) several of the captures embedded byte-exact copies of miniflux_v2's real
source files and literal `git diff` output inside committed fixtures and
`tests/e2e/testdata/*`, with no attribution or notice anywhere in the repo — not a license
problem (miniflux_v2 is Apache-2.0, same as this project) but a real attribution gap; (2)
miniflux_v2 is a large, real production codebase — expensive in live-API tokens and
wall-clock time as a release-suite target, and it's also the checkout the user uses
separately for their own manual testing of `blare`, which shouldn't be conflated with the
automated suite's target. A smaller, dedicated test codebase will be chosen in a separate
design task before any of these are recaptured; nothing in this section should be read as
predicting what it will be. `tests/e2e/testdata/*` (the four subdirectories holding the
copied source) was deleted entirely rather than reverted, since nothing hand-authored ever
lived there.

Captured for real (no longer provisional): ~~auth-failure handshake shape (R12)~~ — T4.1
replaced T2.2's hand-authored instance at
`tests/fixtures/claude-sdk/auth-required/scenario.jsonl` with a real capture (unchanged
shape). Exempt from the 2026-08-01 revert above: this scenario runs against a throwaway
scratch repo, never miniflux_v2 (`tests/release/test_capture_auth_required.py`'s own
docstring says so) — it captures only the SDK's own auth-handshake failure shape, with no
target-codebase content at all, so neither reason for the revert applies to it.

T4.1's live testing also found and fixed a real bug (2026-08-01), independent of which
target codebase exposed it and unaffected by the revert above: `_LiveSDKClient.send()`
only forwarded an event's `text` field, silently dropping `delta_files`/`patch_text` on
`triage` events — every diff-mode triage was showing the live model *zero* real diff
content despite `_TRIAGE_MESSAGE` saying "review... (above)". Fixed via
`_fold_triage_delta_into_query`, scoped to the live client only (the recorded/replayed wire
event's own `text` field is untouched, so no existing fixture needed re-recording).

- analyze happy path (four phases, approvals only) — T2.1 hand-authored a provisional
  instance at `tests/fixtures/claude-sdk/analyze-happy-path/scenario.jsonl` (marked
  provisional in the file, generated from the real phase-prompt/stack code to keep the
  prompt text byte-exact). T4.1 replaced it with a real `~/external_git/miniflux_v2`
  capture (123 entries, clean, no amendments); reverted 2026-08-01 (see above) back to
  T2.1's hand-authored instance. A release-suite capture against the future dedicated test
  codebase still supersedes it.
- analyze re-run over an existing state file (R16 re-analysis, edits against existing IDs)
  — T2.5 hand-authored two provisional instances at
  `tests/fixtures/claude-sdk/analyze-reanalysis-noop/scenario.jsonl` (unchanged
  conclusions) and `tests/fixtures/claude-sdk/analyze-reanalysis-update/scenario.jsonl`
  (one entry changed). T4.1 replaced the latter with a real
  `~/external_git/miniflux_v2` capture (a genuinely messy re-analysis: duplicated work,
  self-diagnosis via `.blare/`, a 3-phase `amend_proposal` escalation, converged
  correctly); reverted 2026-08-01 (see above) back to T2.5's hand-authored instance. For
  the noop half: T4.1 tried three live captures against `~/external_git/miniflux_v2` and
  none converged to a genuine zero-diff (a real re-analysis has no way to learn prior
  analysis exists except its own initiative; the phase prompts never mention it), so it
  was never replaced and needed no revert. A release-suite capture against the future
  dedicated test codebase still supersedes both.
- update with an affected subset of phases — T3.1 hand-authored provisional instances at
  `tests/fixtures/claude-sdk/update-happy-path/scenario.jsonl` and
  `update-multi-commit/scenario.jsonl` (R8's multi-commit delta), both now carrying real
  `patch_text` (T4.4, synthetic — a placeholder single-line diff, not target-codebase
  content, so unaffected by the revert). T4.1 replaced both with real
  `~/external_git/miniflux_v2` captures (a single real commit, a defensive
  `migrations.go` fix, correctly concluded `no_impact`; and a 3-commit range across four
  files, named in one triage call as R8 requires, also concluded `no_impact`); reverted
  2026-08-01 (see above) back to T3.1's hand-authored instances plus T4.4's patch_text. A
  release-suite capture against the future dedicated test codebase still supersedes both.
- update no-impact conclusion (R18) — T3.1 hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/update-no-impact/scenario.jsonl`; T3.2 hand-authored its
  chat-redirected variant at `update-no-impact-redirect/scenario.jsonl`, both now carrying
  real `patch_text` (T4.4, synthetic, same as above). T4.1 replaced both with real
  `~/external_git/miniflux_v2` captures (a single-commit, test-only delta correctly
  recognized as having no production impact; and a docs-only delta's `no_impact`
  conclusion, withdrawn by a directive chat redirect into phase 2, where the model added
  an excluded, non-alertable failure mode); reverted 2026-08-01 (see above) back to their
  hand-authored instances plus T4.4's patch_text. A release-suite capture against the
  future dedicated test codebase still supersedes all three.
- update dynamic expansion: a revised `affected_verdict` opening a phase mid-run, including
  a behind-position phase — T3.2 hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/update-dynamic-expansion/scenario.jsonl`, now carrying real
  `patch_text` (T4.4); still unverified. T4.1's continuation tried twice against real
  miniflux_v2 ranges without producing this shape: a broad six-commit range spiralled into a
  38-minute, 60-round repair loop that never reached a final confirmation (the model's own
  edits kept re-tripping the invariant gate across an unusually rich delta — the catalog
  itself had zero semantic violations once checked offline, so this wasn't a stuck or
  corrupted run); a narrower five-commit range with a chat nudge converged cleanly in under
  two minutes, but the model's triage had already concluded `no_impact` and it explicitly
  stood by that after genuine re-examination — no phase ever opened. Never finalized, so
  there was nothing to revert here — it is untouched by the 2026-08-01 revert, and stays
  exactly as T4.1 left it. `tests/release/test_capture_update_dynamic_expansion.py` now
  hard-fails rather than finalizing unless a real run actually opens more than one distinct
  phase, so a future attempt (against the dedicated test codebase, once chosen) can't
  silently corrupt the fixture with the wrong shape.
- checkpoint chat that alters results (R2) — T2.3 hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/analyze-checkpoint-chat/scenario.jsonl`; a release-suite
  capture still supersedes it
- agent-proposed amendment, approved; and rejected (restore) — T2.4 hand-authored provisional
  instances at `tests/fixtures/claude-sdk/amendment-agent-{approved,rejected}/scenario.jsonl`;
  a release-suite capture still supersedes both
- amendment cascade: a unit spanning multiple phases, approved; and rejected as one unit —
  T2.4 hand-authored provisional instances at
  `tests/fixtures/claude-sdk/amendment-cascade-{approved,rejected}/scenario.jsonl`; a
  release-suite capture still supersedes both
- system-originated amendment (semantic violation at the approval gate) — T2.4
  hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/amendment-system/scenario.jsonl`; a release-suite capture
  still supersedes it
- update whose affected phases were seeded by a load-time semantic violation (R18),
  repaired via `request_repair` — T3.2 hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/update-load-seeded-repair/scenario.jsonl`. T4.1 replaced the
  model's analysis with a real `~/external_git/miniflux_v2` capture (a genuine 3-round
  proactive-repair escalation: two phase-4-only patches rejected as
  `linkage_inconsistency`, then an escalation into phase 2 that reclassified the
  unmappable entry as `excluded`) while leaving `patch_text` a synthetic placeholder
  throughout (its behavior is orchestrator-driven — a hand-seeded R18 violation — not
  diff-content-driven); reverted 2026-08-01 (see above) back to T3.2's hand-authored
  instance for delta_files/reasoning/text. `patch_text` could not simply be reverted to
  T2.2's hardcoded `""`, though, because T4.4's `gitrepo.patch_text` plumbing (kept, not
  reverted) computes it live from whatever the e2e test's own repo actually contains —
  with the delta_files/reasoning reverted to T3.2's `src/handlers.py` seed, replaying the
  fixture now byte-mismatches unless `patch_text` carries the real diff *that* seed
  produces, so it was recomputed against the reverted test's own repo construction
  (`git hash-object` on the literal string the test writes, `"# request handlers\n"`,
  confirms the blob hash below is exactly what `gitrepo.patch_text` will produce, not
  invented): `diff --git a/src/handlers.py b/src/handlers.py\nnew file mode
  100644\nindex 0000000..e8a8944\n--- /dev/null\n+++ b/src/handlers.py\n@@ -0,0 +1
  @@\n+# request handlers\n`. This is test-fixture content the reverted test itself
  seeds, not target-codebase content, so it carries no attribution concern. A
  release-suite capture against the future dedicated test codebase still supersedes it.
- progress feedback (R25) — a slow phase 1 turn with scripted filesystem-read
  `"activity"` events (each carrying a real `delay_before`) ahead of the ordinary
  `propose_edits` round trip, giving the e2e test real wall-clock time to observe
  genuine progress ticks against. T4.3 hand-authored a provisional instance at
  `tests/fixtures/claude-sdk/progress-feedback/scenario.jsonl`; a release-suite capture
  still supersedes it
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
- `on_activity` (R25): fires with the tool's name for `propose_edits` and `run_control`
  calls, and for a scripted filesystem-read tool call (`Read`, or similar), in call order;
  a session constructed with `on_activity=None` drives a turn with tool calls normally and
  raises nothing; a callback that raises is caught and does not interrupt the turn or
  surface as `AgentSessionError` (asserted via a raising fake callback: the turn still
  completes and the driving call still returns normally).

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
