# Module design — orchestrator

## Decisions needed from you

This section contains only open items. **No open items** — the exit-code taxonomy below is a
conventional mapping documented as fact; say so if you want it surfaced as a choice instead.


## Responsibility

The run lifecycle (architecture): preflight, lock, the phase state machine, checkpoints and
amendments, the approval gate, the single write, the summary. Owns the phase-state rule, the
pending edit set, and the exit-code taxonomy. Coordinates every other module and injects the
edit sink and run-control handler into the agent session. Its entry contract for the cli:
`run(mode: RunMode, repo_path: Path, presenter: Presenter) -> int` — it returns the exit
code and never exits the process itself.

## Exit-code taxonomy

Assigned by **run stage**, not by which module raised: `0` success (including R7
up-to-date); `1` refusal — any failure during the preflight sequence below, whichever
module raised it (an auth failure inside `AgentSession.start` is step 9 and exits 1; a
state-directory filesystem error at step 2 or a lock error at step 3 exits 1); `2` run failure — any failure
after preflight completes (agent session death, a `GitCommandError` from the write-time
re-check, a write failure); `3` user abort — SIGINT, the `abort` command, or any `Abort` reply from the presenter,
which per the cli's contract includes its mapping of stream death (EOF, broken stdin) to
`Abort`: the orchestrator sees a reply, never a stream exception; a presenter that
*raises* is an unexpected exception, exit 2. Two
carve-outs cut across the stage rule: an *unexpected* (non-module) exception exits 2
whatever stage it strikes — it is a defect, not a refusal — and argparse usage errors exit
2 by argparse convention, an overlap tolerated because a usage error occurs before any run
exists. Module exceptions carry no exit code; `run()` assigns from the stage it is in.
Every non-zero exit prints cause and next action (R13), except exit 3: a user abort is
not a failure and renders no `error` — its record is the summary (outcome aborted,
discarded counts, transcript path) when a session ran, and a single `aborted` notice for
a pre-session SIGINT, where no artifacts or counts exist to summarize.

## Preflight sequence

Fail-fast, in order, per the spec's precedence rules:

1. repo discovery; no-commits check (R11) — failures here have no repo-id, hence no run log:
   their diagnosis is the R13 message alone
2. state-directory creation under `$XDG_STATE_HOME/blare/<repo-id>/` (uncreatable or
   unwritable → refusal naming the path) and run-log start; then dirty tree outside
   `.blare/` (R11)
3. lock acquisition (R21) — the lock file only; its directory already exists from step 2
4. artifact dispatch via `artifacts.state_exists`: analyze without state →
   `init_inspection` (R1) then `artifacts.empty_set(root)`; update without state → R17
   (`StateMissingError`); otherwise `s = artifacts.load(root, mode)` (the dispatch's
   result is always the run's `ArtifactSet`, `s`, whichever branch produced it). Config
   and stack resolution happen inside artifacts on every branch (R19, R23, R24 — an
   existing config's stack is validated on the fresh branch too, and analyze with a
   missing config resolves the default with the file created at write); the orchestrator
   never touches the stack module and hands `s.stack` to the agent session as a value
5. update only: recorded SHA resolves and is an ancestor (R15)
6. update only: empty effective delta → up-to-date summary with gap counts from the loaded
   set, exit 0 (R7; no session, no login, no transcript)
7. semantic check on the loaded set → violations seed the affected-phase queue (R18)
8. TTY check, only reached when checkpoints will be presented (R22)
9. auth preflight via `AgentSession.start` (R12)

The config file itself is first created by the final write (R20 forbids creating anything
under `.blare/` earlier); an existing config is preserved.

## Phase engine

State per phase: `unvisited` | `open` | `frozen`. Freezing snapshots that phase's pending
edits (the amendment-restore baseline); an unvisited phase's snapshot is empty by
definition. The queue: analyze runs phases 1–4 in order (fresh or over a loaded set — R16
re-analysis is the same engine against existing entries). Update starts with
`AgentSession.triage`: the agent analyzes the delta and answers through run-control with an
`affected_verdict` (which seeds the queue) or a `no_impact` conclusion; `triage` returning
implies a verdict arrived, since the session enforces remind-once-then-raise (agent.md),
so a verdict-less triage surfaces here only as `AgentSessionError`, exit 2 — the affected-phase
judgment is the agent's (architecture's run-control channel); the orchestrator contributes
only the semantic-violation seeds from step 7 and grows the queue on revised verdicts
(ahead or behind the run position — a behind-position phase's checkpoint is presented like
any other). **Opening enqueues**: any transition of a phase out of `unvisited` — a revised
verdict or the amendment mechanism naming it — inserts it into the queue in phase order,
so the gate's queue-empty condition cannot be met while an amendment-opened phase still
awaits its ordinary checkpoint; that is what makes "opening a phase for a repair never
substitutes for running it" enforceable rather than aspirational.

**No-impact flow (R18)**: a `no_impact` conclusion with a non-empty queue (semantic seeds
exist) is rejected back to the agent through the run-control verdict — the seeded phases
still need work. With an empty queue it is presented for confirmation as a checkpoint
(approve / abort / chat, where chat can redirect into an `affected_verdict`); approval is
the final confirmation for the run, the semantic gate applies to it, and the write path
then changes exactly the state SHA plus any derived-doc restoration (R9/R18) — a claim
carried by the queue-empty, no-open-unit precondition plus the byte-for-byte restore
semantics of any rejected unit: a unit may have opened and been rejected along the way,
and the restore is what re-establishes the state the claim needs. An
`amend_proposal` arriving during this confirmation withdraws the conclusion the same way
a redirect does: the pending prompt is mooted (`prompt=None` for any in-flight chat
reply) and the unit runs to closure. On unit approval the run continues through the
now-open phases and the no-impact prompt never returns; on rejection the restore covers
the whole pre-unit state, the withdrawn conclusion included, and the no-impact
confirmation is re-presented — rejection of the withdrawal is what puts the conclusion
back on the table. A `no_impact` verdict arriving while any unit is open is rejected as a
verdict (close the unit first).

Checkpoint loop, per phase: the agent runs the phase (edits accumulate through the sink,
which enforces open-phase state before artifacts' content check); the orchestrator builds a
structured `CheckpointView` (phase, entries added/updated/removed with their content, gap
summary) and passes it to the presenter — rendering is the cli's (architecture); the
presenter returns approve, abort, or chat text; chat routes through
`AgentSession.chat(text)`, and `show_chat_reply(reply, prompt)` renders it, re-offers
the prompt, and returns the next reply — the loop's reply-reading stays inside the
presenter call. When a run-control verdict during the exchange made the in-progress
prompt moot (R18's redirect withdrawing a no-impact conclusion), the orchestrator passes
`prompt=None`: the reply renders, nothing re-offers, and the run proceeds to the newly
affected phase. The same `prompt=None` call handles an `amend_proposal` opening a unit
during an ordinary checkpoint's chat: the unit defers the checkpoint, which moots the
*current offer* of its prompt (not the checkpoint itself — it re-presents after the unit
closes), so the chat reply still renders, the call returns `None`, and the unit resumes. Approval freezes the phase. Abort exits 3, writing nothing (R20), the summary still
naming the transcript path (R14 — a session ran). Two further presenter payloads cover the
non-phase presentations: `AmendmentView` (the unit's changed entries grouped per involved
phase, plus origin) for the atomic re-presentation, and `NoImpactView` (the delta summary
and the agent's conclusion) for the R18 confirmation; the presenter protocol has one method
per view type, plus the non-view methods `notice`, `error`, `summary`,
`show_chat_reply`, and `is_interactive` (the step-8 R22 query — TTY detection stays the
cli's per the architecture) — the channels through which R13's cause-and-next-action, the run-end
summary (called at session-bearing endings and the sessionless R7 success — refusals
render `error` only, matching the cli's contract; the transcript line appears only when a
session ran), and chat replies actually reach the terminal. `error` takes an optional
`detail` string — the channel for the pre-run-log traceback, rendered beneath the cause
on stderr. The agent
runs a phase via `AgentSession.run_phase(phase)`.

Approval gate: at every approval that would leave the queue empty **with no amendment
unit open** — a phase checkpoint or a unit's re-presentation alike — run the semantic
check on the candidate; pass → final confirmation; fail → system-originated amendment.
The loop continues until the check passes: approving a system-originated unit
re-evaluates the gate *when that approval satisfies the same precondition* — queue empty,
no unit open; a unit that opened an unvisited phase leaves the queue non-empty instead,
and the run proceeds through that phase's ordinary checkpoint before the gate can fire
again. Either way, repairs that fixed one violation while leaving another cannot reach
the write. An open unit defers everything downstream of it: when any driving call
returns while a unit is open (the agent's turn ended before `amend_complete`), the
orchestrator immediately resumes the unit via `request_repair` — the agent module's
resume path — through closure and atomic re-presentation, and only then presents the
pending checkpoint. No unit can be open at final confirmation, so unit repairs can
neither be written without their R2 re-presentation nor silently dropped.

## Amendments

One mechanism, two origins (agent `amend_proposal`; system-originated on a failed approval
gate — driven into the session via `AgentSession.request_repair`, since the agent did not
initiate it). Unit tracking: origin, opened phases (frozen or unvisited), their snapshots
(empty for unvisited). Flow: open named phases → repairs arrive as ordinary batches →
after `amend_complete`, the blast radius is recomputed as the architecture requires,
references *and* invariants: the union of `artifacts.referencing_phases` over the changed
IDs and the repair phases of `artifacts.semantic_violations` on the candidate — and the
cascade pulls in **frozen phases only**, per the architecture. Unvisited phases never join
through the recompute: mid-run candidates violate R3–R5 by construction (a phase-2 failure
mode has no alert until phase 4), so cascading into unvisited repair phases would drag
unbuilt phases into every early amendment; violations whose repair phase is unvisited are
left for that phase's own run or the approval gate. Loop to closure → the unit's full
changed set is re-presented — the reply alphabet there is approve, chat, and abort
always (R2 grants abort at any checkpoint), plus reject exactly when the unit is
agent-origin: the cli's `rejectable` flag encodes it, and a `Reject` returned for a
non-rejectable presentation is a protocol violation handled as an unexpected exception
(exit 2). The unit stays open through its re-presentation — approval is what closes it —
so re-presentation chat behaves like any open-unit turn: accepted batches and joining
proposals return the unit to the closure loop (recompute, repairs, and a fresh
re-presentation over the updated changed set — "once" means once per closure, which is
what makes chat repair converge rather than bypass the cascade), and the `AmendmentView`
always shows the current set. Approval re-freezes exactly the phases that were frozen
when the unit opened; a phase the unit opened from `unvisited` (possible only by being
*named*, e.g. a system repair target) stays open, keeps its repairs as pending edits, and
takes its ordinary checkpoint when the queue reaches it — opening a phase for a repair
never substitutes for running it. Reject (agent-origin only) restores every snapshot — a
phase opened from `unvisited` returns to `unvisited`, its repairs discarded and the queue
entry its opening created removed with it — a rejected unit leaves no trace in the queue.
Either outcome is sent back to the session via
`AgentSession.notify_amendment_outcome`, so the model never continues on a context that
believes rejected repairs still stand. System-originated units offer no reject: chat or
abort. Run-control handling is total, with join-over-reject precedence: an `amend_proposal`
adding at least one non-open phase joins the open unit (or starts one), its already-open
named phases a no-op within it; a proposal naming *only* already-open phases is rejected
as a verdict (those phases are open — just edit them); `affected_verdict` and `no_impact`
in analyze mode are rejected as verdicts; an
`amend_complete` with no unit open is rejected as a verdict; an `affected_verdict` naming
an already-open phase is acknowledged as a no-op, and one naming a frozen phase is
rejected with a verdict directing the agent to `amend_proposal`; and while a unit is
open, a `no_impact` or an `affected_verdict` naming an unvisited phase are both rejected
as verdicts (close the unit first — unit tracking stays free of concurrent non-unit
openings).

## Write path

At final confirmation: `gitrepo.tree_matches(start_sha, ".blare")` and
`artifacts.raw_bytes_match` (canonical YAML only — a mid-run hand edit to canonical YAML
aborts; derived-doc edits and stray files never do, per R10 and the ignored-files rule) →
the orchestrator drives artifacts' three write primitives in order (the architecture's
"write ordering primitives"): `write_entries_and_config`, `write_docs`,
`write_state(new_sha)` — recording each returned `WriteReport` in the run log as it goes →
summary: entry counts, gap counts, transcript path → exit 0. SIGINT is masked from final
confirmation until the write completes — the write is never interrupted by it, and a
signal received during it is honored only after `write_state` returns, with the run
reported as what it is: completed. A re-check failure or a raising primitive exits 2; the
failure message reports the reports collected so far, the failing file, the old-SHA
guarantee, and git as the recovery (R20). Every exit-2 session-bearing ending then renders
the summary — outcome failed, discarded counts, transcript path — the single R14 channel:
`error` carries cause and next action, the summary carries the transcript line, never
duplicated between them.

## Lock

`$XDG_STATE_HOME/blare/<repo-id>/lock`, containing PID and start time, created with
`O_EXCL`. Held: second invocation exits 1 naming the PID (R21). Stale: PID not alive
(checked via `/proc/<pid>`, Linux-only per spec) → reclaimed with a notice. The liveness
check is an injected callable for testability. The lock is released in a `finally` on
**every** exit path after acquisition — success, refusal, failure, and abort alike — so no
ordinary sequence of runs ever takes the stale-reclaim path.

## Error handling

One `BlareError(cause, next_action)` shape at the boundary (the system error type): every
module exception is caught in `run()` and rendered with the exit code assigned by stage.
KeyboardInterrupt → 3, with one carve-out: a signal arriving inside the masked write
window is noted after `write_state` returns and the run exits 0 as the completed run it
is. Unexpected exceptions → 2 with the traceback preserved in the run log — or, in the
window before the run log exists (step 1, or step 2's own failure), printed to stderr
beneath the rendered cause and next action — never a bare stack trace as the whole
message, never a swallowed error.

## Failure visibility

Every refusal and failure renders cause + next action through the presenter (R13). A run
log (JSONL, one file per run named by the same timestamp as the run's transcript slot,
existing from step 2 onward once the repo-id is known — so two contending invocations each
write their own log and never clobber) records preflight outcomes, phase transitions,
freezes, amendment units, gate results, and the `WriteReport`. The orchestrator mints the
run timestamp at step 2 and uses it both for the run-log name and for the
`TranscriptWriter` it constructs at step 9, which is the whole coordination behind
"named by the same timestamp". A run-log write failure after step 2 never fails the run:
logging degrades to a presenter notice naming the path, so the loss is visible rather
than a silent black hole, and the run continues. The summary states counts and gap counts, and the transcript path
whenever a session ran (R14); the R7 summary says it has no transcript. Pre-session
endings other than the R7 success render no summary: refusals render `error` only, and a
pre-session SIGINT renders the `aborted` notice.

## Test plan

Fakes: `FakeAgentSession` (scripted per scenario: triage verdicts, phase edit batches,
run-control calls, chat replies — stateful, records what it was asked); `FakePresenter`
(scripted replies, records every `CheckpointView`, `AmendmentView`, and `NoImpactView`
presented). gitrepo and artifacts are real, over temp repos.

Contract tests, one per behaviour (each also asserts the exit code and, where relevant,
R20's nothing-written on observable filesystem state):

- preflight ordering: for each adjacent pair through step 5, a repo violating both
  conditions reports the earlier one. The later steps have their own observable orderings,
  tested explicitly: (5,6) a non-ancestor SHA refuses even when the delta would be empty;
  (6,7) an empty delta exits 0 with no seeding and no session even when the loaded set has
  semantic violations (R7 precedence); (7,8) semantic seeds never terminate the run — with
  a seeded queue and a non-TTY stdin, the R22 refusal is what fires.
- analyze happy path: four checkpoints, approvals; artifacts on disk match the candidate;
  default config created; state written last per the report; exit 0; summary counts, gap
  counts, transcript path.
- analyze over an existing state file (R16): edits land against existing entries, unchanged
  entries keep IDs and bytes, existing config preserved.
- abort at each checkpoint: exit 3, `.blare/` untouched, transcript path in the output.
- chat at a checkpoint routes to the session and re-presents; approval then proceeds.
- sink rejects a batch tagged for a frozen phase and for an unvisited phase.
- agent amendment: proposal opens phases; the cascade pulls in one *frozen* phase via
  `referencing_phases` and another *frozen* one via a semantic-violation repair phase (the
  invariant half); a violation whose repair phase is unvisited does not expand the unit;
  unit re-presented once, approve re-freezes exactly the previously frozen phases; a
  named-open unvisited phase stays open, keeps its repairs, and its ordinary checkpoint
  fires when the queue reaches it; reject restores snapshots byte-for-byte and the outcome
  notification reaches the session in both variants; an amendment opening an unvisited
  phase returns it to unvisited on reject, repairs discarded.
- run-control totality: a proposal adding a non-open phase mid-unit joins the unit
  (join-over-reject precedence); a proposal naming only open phases is rejected as a
  verdict; `affected_verdict`/`no_impact` in analyze mode are rejected; `amend_complete`
  with no unit open is rejected; an `affected_verdict` naming an open phase is a no-op
  acknowledgment; one naming a frozen phase is rejected directing to `amend_proposal` —
  one test per disposition.
- an amendment unit open when `run_phase` returns: the checkpoint is deferred, the unit is
  resumed via `request_repair` to closure and re-presentation, then the checkpoint
  presents; the gate never fires while a unit is open.
- system amendment on gate failure: no reject offered; chat repair converges; abort works.
- dynamic expansion: a revised verdict opens a behind-position phase; its checkpoint is
  presented; the gate re-fires after the queue empties again.
- update: triage `affected_verdict` seeds exactly the named phases; unaffected phases never
  pause (R18); semantic-violation seeds from a hand-edited violating set open the repair
  phase and pause there; `no_impact` with seeds present is rejected back to the agent;
  `no_impact` with an empty queue is presented, confirmed → exactly the state SHA and
  restored docs change; chat at that confirmation redirects into affected phases.
- update: R7 empty delta → exit 0, zero byte changes, no session, summary says no
  transcript.
- update happy path through final confirmation: the recorded SHA equals the delta's end
  commit captured at run start, only delta-affected artifacts changed, unaffected entries
  byte-identical (R6, R9).
- SIGINT during the write (injected between primitives): write completes, exit 0, summary
  reports a completed run.
- gate loop: a system-originated unit approved with a residual violation re-fails the gate
  and raises a second unit; the write is reached only after a passing check.
- an amendment names an unvisited ahead phase: after unit approval the phase is still open,
  its checkpoint fires when the queue reaches it, and its unit repairs are present as
  pending edits there.
- R11 (outside a git repository; a repository with no commits — each with its exit code
  and message, beyond the ordering-pair coverage), R15 (non-ancestor and unresolvable
  SHA), R17, R22 (non-TTY before any session), R12 (auth failure exits 1) — one each.
- R1 inverse: analyze over orphaned canonical files exits 1 naming them, nothing touched.
- R23 (unsupported stack in each mode; missing config at update) and R24 (mismatch naming
  both versions) — exit 1 with the artifacts-provided message.
- SIGINT mid-checkpoint → exit 3, nothing written; SIGINT during preflight → exit 3, the
  `aborted` notice, no summary and no error; `FakePresenter` returning `Reject` at a
  system-originated unit's re-presentation → exit 2 (protocol violation); an unexpected
  exception mid-run →
  exit 2 with the traceback in the run log and a rendered cause; an unexpected exception
  at step 1 → exit 2, traceback on stderr beneath the rendered cause.
- an `amend_proposal` during the no-impact confirmation moots the prompt, runs the unit,
  and on approval the run proceeds through the opened phases; on rejection the pre-unit
  state is restored, the conclusion included, and the no-impact confirmation is
  re-presented.
- a `no_impact` verdict with a unit open is rejected as a verdict; so is an
  `affected_verdict` naming an unvisited phase while a unit is open (close the unit
  first — the same precedent, keeping unit tracking free of concurrent non-unit
  openings).
- re-presentation chat that lands a batch returns the unit to the closure loop: recompute
  runs, and the next `AmendmentView` shows the updated set.
- run log contents after a full run: preflight outcomes, phase transitions, freezes,
  amendment units, gate results, and the write reports all present (asserted on the file);
  the run log and transcript share the step-2 timestamp; a contending losing invocation
  writes its own distinct log file, never clobbering the winner's.
- an `amend_proposal` during an ordinary checkpoint's chat: the reply renders via
  `prompt=None`, the checkpoint re-presents after unit closure.
- lock: contention exits 1 naming the PID; stale lock reclaimed with notice (liveness
  callable injected); a run immediately after a success, after an abort, after a refusal
  (R15), and after a run failure (agent death) each acquire cleanly with no stale notice.
- write re-check: a mid-run commit aborts the write (exit 2); a mid-run hand edit to
  canonical YAML aborts; a derived-doc edit does not (restored per R10).

Failure-mode tests, per dependency:

- gitrepo: `GitCommandError` during preflight → exit 1 with git's stderr;
  `GitCommandError` from the write-time re-check → exit 2.
- artifacts: structural error → exit 1 naming file; write failure mid-write (injected) →
  exit 2, state file untouched on disk, report shows what landed.
- agent: `AgentSessionError` mid-phase → exit 2, `.blare/` untouched, transcript path still
  printed.
- state-dir filesystem: `$XDG_STATE_HOME` unwritable → exit 1 at step 2 naming the path;
  a run-log write failing mid-run (injected) → presenter notice naming the path, run
  continues to its normal outcome.
- config/stack (via artifacts): `ConfigError` and `UnsupportedStackError` → exit 1
  carrying the artifacts message with supported values.
- presenter: an `Abort` reply produced by the cli's stream-death mapping → exit 3,
  nothing written (a reply, not an exception); a presenter raising → unexpected
  exception, exit 2.
