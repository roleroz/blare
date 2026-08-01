# Module design — cli

## Decisions needed from you

This section contains only open items. **No open items** — D9 (checkpoint input convention:
exact reserved words `approve` / `abort`, anything else is chat) is settled and logged in
`engineering/decisions.md`.

**Changes since last approval**: `TerminalPresenter` gained `progress` (R25, added
2026-07-31) — periodic status rendering during a driving call. See Interface and Rendering
rules. `parse_args` gained `--unattended` (R26, added 2026-07-31); `TerminalPresenter` gained
an `unattended` constructor flag changing every reply-pending method's behavior, plus a
completion bell. See Interface, Error handling, and Rendering rules. `progress` now
consolidates same-key ticks instead of appending one line per tick (T4.6, added
2026-07-31) — discovered via live testing: a slow phase produced dozens of near-identical
per-second lines. See Interface, Error handling, and Rendering rules.

## Responsibility

Entry point and terminal surface (architecture): command parsing, checkpoint presentation,
the chat loop, summaries and error rendering per `brand/design-language.md` §6, TTY
detection. No run logic — it renders what the orchestrator reports and forwards what the
user types.

## Interface

```python
def parse_args(argv: list[str]) -> ParsedCommand    # argparse wrapper, unit-testable
# ParsedCommand gains `unattended: bool` (R26) -- `--unattended` on both `analyze` and
# `update`; false by default, no other subcommand accepts it
def main(argv: list[str], run: RunFn = orchestrator.run) -> int   # console entry point
# main passes the invocation cwd as run()'s repo_path (gitrepo discovers the root from
# it), passes parsed.unattended as run()'s keyword-only unattended argument, and
# constructs the TerminalPresenter over the process's real stdin/stdout/stderr with
# unattended=parsed.unattended; when parsed.unattended, main rings the terminal bell
# once after run() returns, regardless of the exit code -- one bell per invocation,
# independent of which ending kind occurred (R26)

class TerminalPresenter:                            # orchestrator's presenter protocol
    def __init__(self, stdin, stdout, stderr, *, unattended: bool = False) -> None
    def present_checkpoint(self, view: CheckpointView) -> CheckpointReply
    def present_amendment(self, view: AmendmentView, rejectable: bool) -> AmendmentReply
    def present_no_impact(self, view: NoImpactView) -> CheckpointReply
    def show_chat_reply(self, text: str, prompt: PromptKind | None) -> AmendmentReply | None
    def progress(self, label: str, elapsed_seconds: float, last_activity: str | None) -> None
    def notice(self, text: str) -> None
    def error(self, cause: str, next_action: str, detail: str | None = None) -> None
    def summary(self, s: RunSummary) -> None
    def is_interactive(self) -> bool                # R22: stdin is a TTY
```

`CheckpointReply` is `Approve | Abort | Chat(text)`: a line that is exactly `approve` or
exactly `abort` (lowercase, no arguments) acts, and any other input is chat passed to the
agent — the prompt names the two verbs. `AmendmentReply` adds `Reject`: at a
`rejectable=True` amendment prompt, exactly `reject` is a third reserved word (named in
that prompt), returning the rejection verdict the orchestrator acts on (R2's
reject-and-restore is a mechanical action, not chat); at a non-rejectable prompt `reject`
is ordinary chat.

With `unattended=True` (R26), `present_checkpoint`/`present_amendment`/`present_no_impact`
still render the view in full — unattended output is meant to be reviewed later, e.g.
redirected to a file — but skip the reserved-word prompt line (`$ approve · ...`) and never
call the stdin-reading step at all, returning `Approve()` immediately: the prompt naming
words nobody will type is exactly the confusing artifact the timing analysis that motivated
R26 already found once, and there is nothing to gain by leaving it in a log meant for later
review. `show_chat_reply` is simply never reached, since nothing ever produces a `Chat`
reply to route through it. This is a rendering-layer decision entirely: the orchestrator
gets back the identical `Approve()` an interactive user's own typed `approve` would produce,
and needs no awareness that nobody actually typed it.

`notice` renders one informational line outside any view (a reclaimed
stale lock, a phase opening) — plain, no `→ ` prefix; that prefix marks results and next
actions, which a notice is neither.
`progress` (R25) renders status while an agent-driving call is in flight — `label` names the
active phase or operation (e.g. `"phase 3 — metric coverage"`, `"triage"`), `elapsed_seconds`
is time since the call began, and `last_activity` is the most recent tool call's name,
rendered as `waiting` when `None` (no tool call has arrived yet). `(label, last_activity)`
is this call's *key*; consecutive calls sharing a key are the same ongoing state (elapsed
time is the only thing changing), and a key change means the state itself changed (a new
phase, or the model moved on to a different tool) — **except** `elapsed_seconds == 0.0` is
*always* a key change regardless of whether `(label, last_activity)` repeats: the ticker's
own invariant (orchestrator.md: "the first tick is always emitted at exactly `elapsed=0.0`")
is what marks a fresh driving call starting, and two separate calls can share an identical
key with nothing in between them — repeated repair rounds each driven by `_drive(...,
"repair", ...)` with no tool call arriving before either ends both render `("repair", None)`
— so without this rule a second round's first tick would be read as a continuation of the
first round's *last* tick, silently merging two distinct rounds into one line whose elapsed
time then runs backwards. Because the ticker (orchestrator.md) fires on a short interval
regardless of how long a state lasts, most calls otherwise share a key with the one before —
one line per second of "system map, waiting" during a slow turn (2026-07-31's timing
analysis) reads as noise, not signal, in a way a single line updating in place, plus one
permanent line per state actually reached, does not (added 2026-07-31, T4.6 — refining the
"no motion beyond appearing" reading below):
- **TTY**: a same-key call rewrites the current line in place (`\r` then clear-to-end-of-line,
  then the new content, no trailing newline — the terminal shows one line, its elapsed time
  ticking). A key change first finalizes the *previous* key's line — a bare `\n`, since its
  last-rendered content is already exactly right — before the new key's content is written
  the same way. The very last key of a call is finalized by whatever the next presenter call
  is (any other method — `_finalize_progress_line`, Stream I/O), never by `progress` itself,
  since it cannot know it was the last tick until something else happens next.
- **Non-TTY** (unattended output redirected to a file, e.g.): a same-key call writes nothing
  — control sequences in a plain file are noise, not a redraw anyone will see live — and only
  updates an internal buffer holding that key's latest content. A key change, or the next
  finalize, commits exactly one plain line (no control characters) for the key that just
  ended, holding its *last* elapsed time, not its first. TTY and non-TTY therefore agree on
  what ends up permanently visible — one line per state reached, each showing that state's
  final elapsed time — differing only in whether a live in-place preview exists while a state
  is still ongoing.

It takes no reply and never blocks: the orchestrator calls it from its own ticker, off the
thread draining the turn, purely to inform — a broken stream here is swallowed like `notice`,
never mapped to `Abort` the way a reply-pending view is, since no reply was ever expected.
Every other stream-writing method (Stream I/O) finalizes a pending progress line — if one is
open — before writing its own content, so a checkpoint, notice, or summary can never land
mid-line or get overwritten by a later progress tick.
`show_chat_reply` is the chat loop's continuation: after a `Chat` reply, the orchestrator
routes the text to the agent and passes the response here together with the kind of prompt
in progress (`PromptKind`: checkpoint, no-impact, amendment, rejectable amendment). It
renders the response multi-line, inline in the conversation — the view is *not* redrawn —
then re-offers that kind's prompt itself and returns the next reply, which is what makes
the loop implementable against a stateless presenter: the reply-reading stays inside one
call. `Reject` is only returnable for the rejectable-amendment kind; the reply type is
`AmendmentReply` as the superset. `prompt=None` renders the response *without* re-offering
and returns `None` — the path for a chat whose reply made the in-progress prompt moot,
e.g. a run-control verdict during the exchange withdrew the no-impact conclusion (R18's
redirect): the orchestrator shows the answer and moves on to the newly affected phase's
checkpoint instead of demanding a reply to a prompt whose subject no longer exists. `summary` is called at exactly two kinds of ending: session-bearing ones (success, abort,
or failure — R14's transcript line lives here) and the sessionless R7 success (R13 — it
summarizes "no changes" with gap counts, no transcript line). Every other sessionless
ending — refusals and validation failures, whether or not artifacts had been loaded (R12
and R15 included) — renders `error` only; its diagnosis is the R13 message. `RunSummary`
carries the outcome, the entry counts split as added / updated / removed (R13's exact
split, or "no changes"), the gap counts, and the transcript path when a session ran. At a
non-writing ending — an abort or a pre-confirmation failure, where R20 guarantees nothing
landed — the split describes the pending edits that were discarded and is rendered under
an explicit "discarded" label, never as if applied
(R14's line appears on every session-bearing ending — success, abort, or failure — and
never on the R7 path, which has no transcript). Argument parsing is stdlib
`argparse`: subcommands `analyze` and `update`, each accepting `--unattended` (R26; false by
default) alongside `--help` and `--version`; unknown usage exits 2 with usage text,
argparse's convention. Exit codes are
the orchestrator's to define — its taxonomy records that usage errors share code 2 with
run failures, an overlap it tolerates because a usage error occurs before any run exists.

## Rendering rules (from brand/design-language.md §6)

- Result lines start `→ `; interactive input prompts carry the `$ ` marker (brand §6:
  "`$` for the prompt"); counts read `14 failure modes · 9 covered`; numbers precede
  lowercase nouns.
- Color only the severity word: `critical` in the alert color, `warning` in the warn color;
  everything else uncolored. Respect `NO_COLOR` and non-TTY stdout (no escape codes either
  way).
- Errors render as the cause line, then `→ ` + next action.
- Terse and factual; no apologies, no exclamation marks; vocabulary per the brand file
  (failure mode, coverage, gap, breach, recommendation).
- Progress lines (R25) start `· ` — distinct from both `→ ` (results) and `$ ` (prompts),
  since a progress line is neither: `· phase 3 — metric coverage (12s, propose_edits)`, or
  `(12s, waiting)` before any tool call has arrived. No color. On a TTY, the *current* state's
  line updates in place (T4.6, 2026-07-31: this refines the original "no motion beyond
  appearing" reading below) — this is real, non-decorative information (elapsed time, the
  model's actual activity), not the idle-pulsing brand's §7 rules out; every *previous* state
  the run passed through stays as its own permanent line, so the log still reads as a
  sequence of what happened, never overwritten. Non-TTY output never redraws — one plain line
  per state, no control characters.
- The unattended completion bell (R26) is the single ASCII BEL byte (`\a`, `0x07`) written
  to stdout by `main`, once, after `run()` returns — no text, no prefix, nothing else added
  to the summary/error rendering that already happens; terminals that honor BEL (most do)
  surface it as an audible or visual alert with no code on Blare's side beyond writing the
  byte.

Checkpoint screen: phase name as header, then this module's rendering of the structured
`CheckpointView` (entries added/updated/removed with their content, gap summary — the
orchestrator supplies data, this module owns all text formatting per the architecture),
then the prompt naming the reserved words. The amendment screen reuses the same layout,
one section per involved phase (phase name as section header, that phase's changed entries
with content), topped by an origin line (`amendment · proposed by agent` or
`amendment · invariant repair`) and ended by the amendment prompt. The no-impact screen:
header `no changes needed`, the delta summary from `NoImpactView` (changed-file count and
list), the agent's conclusion text, then the checkpoint prompt. Ctrl-C and EOF (Ctrl-D)
at any prompt are `Abort`. Streams: everything conversational — views, chat replies,
notices, summaries — goes to stdout; `error` and argparse usage text go to stderr.

## Error handling

The presenter never raises for user input oddities (empty line → re-prompt). Stream
failures split by method kind — the split follows whether the *call* reads a reply, so
`show_chat_reply` is reply-pending when given a `PromptKind` and void when given
`prompt=None` (a broken stream there is swallowed, `None` returned, and the run proceeds;
the next reply-pending call converts the dead stream to `Abort`): a reply-pending method
(checkpoint, amendment, no-impact, a prompting `show_chat_reply`) hit by a broken pipe or
closed stdout returns `Abort` — the run cannot continue without a user, and before final
confirmation R20 guarantees nothing is written; with `unattended=True`, a reply-pending
method's stdin-read step never runs at all, so a broken stdin can never surface there
(rendering the view is still subject to the same stdout write-failure handling as any other
call) — an `Abort` from a reply-pending method is therefore only reachable when
`unattended=False`; a void method (`notice`, `error`,
`summary`, `progress`) swallows the write failure and continues — for `error` and
`summary` because the run's outcome is already determined and a render crash would
corrupt it, and for `notice`/`progress` as a deliberate call: a mid-run notice or progress
line lost to a dead stdout is tolerable because the next reply-pending call converts the
dead stream to `Abort`, whereas raising from a fire-and-forget render would abort runs for
a line nobody could read anyway — in particular a pipe break during `summary`,
after the write, must not turn a completed run into a reported abort, and the exit code
reflects the actual outcome. `_finalize_progress_line` (T4.6) — called at the start of every
other stream-writing method, reply-pending or void alike — is itself void: a write failure
while finalizing a pending progress line is swallowed the same way, never surfacing as
`Abort` even from a reply-pending caller (the caller's own subsequent write is what
determines whether *it* returns `Abort`, unaffected by whether finalizing succeeded).
`main` catches nothing itself — exit codes are the
orchestrator's; the argparse-decided exits are the exceptions (usage errors exit 2,
`--help` and `--version` exit 0). Any stream error on a reply-pending read — EOF,
`BrokenPipeError`, or an `OSError` such as EIO when the controlling terminal dies —
returns `Abort`, the same rule as EOF.

## Failure visibility

This module is the visibility surface: R13's cause-and-next-action rendering and the R14
transcript-path line in the summary are emitted here. It writes nothing to disk and holds no
state, so its own failures are terminal I/O failures, which surface as the abort path above.

## Test plan

Fakes: in-memory text streams standing in for stdin/stdout/stderr (fed to the presenter's
constructor — no PTY needed at unit level; the PTY lives in the e2e harness).

Contract tests, one per behaviour:

- `analyze` and `update` parse to the right run mode (via `parse_args`; `main`'s wiring is
  asserted with an injected recording `run` callable — ordinary dependency injection like
  gitrepo's `git_executable`, not a test seam in the architecture's sense: its single-seam
  claim covers environment-activated substitution in the assembled binary, which this
  default parameter is not); unknown command and stray flags exit 2 with usage on captured
  stderr; `--version` and `--help` each print and exit 0.
- checkpoint reply mapping: exact `approve` → Approve; exact `abort` → Abort;
  `approve the second one` → Chat; the near-misses `Approve` and ` approve ` → Chat (the
  contract is exact-match, not normalized); empty line re-prompts; EOF → Abort; Ctrl-C
  (KeyboardInterrupt at the prompt) → Abort, distinct from the EOF case.
- checkpoint screen rendering: header, entry sections with content, gap summary, and the
  verb-naming prompt, asserted byte-exact for a fixed view.
- `show_chat_reply` with `prompt=None` renders the reply, offers no prompt, returns
  `None` (the R18-redirect contract path).
- chat loop: a `Chat` reply followed by `show_chat_reply` renders the response inline
  without redrawing the view, re-offers the prompt, and returns the next reply; the reply
  mapping is kind-dependent — `reject` typed at a rejectable-amendment continuation returns
  `Reject`, while at a checkpoint, no-impact, or plain (non-rejectable) amendment
  continuation it returns `Chat("reject")` — the plain-amendment case being the one backing
  the no-rejection rule for system-originated units; `approve` and `abort` map at every
  kind.
- amendment screen rendering: origin line, one section per involved phase, prompt —
  asserted for a fixed two-phase unit.
- amendment replies: `rejectable=True` — exact `reject` → Reject and the prompt names it;
  `rejectable=False` — the prompt offers no reject wording and `reject` → Chat.
- no-impact screen rendering: header, changed-file summary, conclusion text, checkpoint
  prompt — asserted byte-exact for a fixed view, replies per the checkpoint convention.
- `main` wiring: the injected recording `run` receives the mode from `parse_args`, the
  invocation cwd as `repo_path`, and a `TerminalPresenter` constructed over the process's
  real streams.
- summary content: outcome, entry counts split added / updated / removed, gap count split
  by coverage status, and the transcript path, present on each of a session-bearing
  success, an abort, and a failure ending; at the abort and failure endings the split
  carries the "discarded" label and never reads as applied; the sessionless R7-style
  summary renders "no changes" with gap counts and no transcript line.
- error output lands on stderr; views, chat replies, notices, and summaries on stdout.
- notice renders one plain line without the result prefix.
- severity words colored on a TTY stream, bare with `NO_COLOR`, bare on a non-TTY stream —
  byte-exact assertions on the rendered output.
- counts and result lines match the brand format exactly (`→ `, `n noun · m noun`).
- error rendering: cause line then `→ next action`, byte-exact; with `detail` set (the
  orchestrator's pre-run-log traceback channel) the detail renders beneath, on stderr.
- `is_interactive` false exactly when stdin is not a TTY (R22's criterion); a non-TTY
  stdout alone leaves it true and only disables color.
- `progress` rendering (single call, TTY stdout): `· ` prefix, the label verbatim, elapsed
  seconds, and `last_activity` when set; `last_activity=None` renders `waiting` in its
  place — byte-exact assertion on the in-place-update bytes (`\r` + clear-to-end-of-line +
  content, no trailing newline) for a fixed set of arguments.
- `progress` consolidation (T4.6), TTY stdout: two consecutive calls with the same
  `(label, last_activity)` key and `elapsed_seconds > 0` — only elapsed time differs —
  produce a single in-place update, asserted byte-exact on the stream; a third call with a
  different `last_activity` first finalizes the prior line with a bare `\n` (its
  already-rendered content unchanged), then writes the new key's content the same in-place
  way; a call with a different `label` (a new phase) finalizes and starts fresh identically.
  A call with `elapsed_seconds == 0.0` *always* finalizes and starts fresh, even when
  `(label, last_activity)` is identical to the immediately preceding call — the regression
  test for two consecutive `"repair"`/`None` rounds (no tool call in either) that must not
  merge into one line with elapsed time running backwards. Each of
  `present_checkpoint`/`present_amendment`/`present_no_impact`/`show_chat_reply` (both the
  `prompt=None` and prompting branches)/`notice`/`error`/`summary`, called while a progress
  line is open, finalizes it first — one test per method, asserted on the exact byte
  sequence: the pending line's `\n`, then that method's own content, never interleaved or
  missing the boundary.
- `progress` consolidation, non-TTY stdout: consecutive same-key calls (`elapsed_seconds >
  0`) write nothing at all; a key change — including the `elapsed_seconds == 0.0` case above
  — or the next finalize, writes exactly one plain line (no `\r` or escape sequences) holding
  the *superseded* key's last-seen elapsed time and activity, not its first. A run whose
  every progress call shares one key end to end still produces exactly one committed line
  once something finalizes it. Scope note (not a test, a documented boundary): this means a
  same-key state produces zero output for its whole duration until superseded — acceptable
  because non-TTY output (chiefly `--unattended`, T4.5) is specified as reviewed after the
  run ends, never tailed live; live-tailing a redirected run is not a use case this design
  covers.
- `--unattended` parses on both `analyze` and `update`, defaulting false; `TerminalPresenter
  (unattended=True)`'s `present_checkpoint`/`present_amendment`/`present_no_impact` render
  the view content in full (byte-exact assertions matching the interactive case's view
  rendering) but omit the reserved-word prompt line, and return `Approve()` without reading
  the injected stdin stream at all — asserted by constructing the presenter over a stdin
  double that raises on any read, confirming it is never touched; `main` writes exactly one
  `\a` to stdout after `run()` returns whenever `parsed.unattended`, regardless of the exit
  code — asserted across a success, the round-cap failure, an ordinary preflight refusal,
  and an ordinary run failure, one test per ending kind — and writes none when `unattended`
  was never passed.

Failure-mode tests, dependency = the terminal streams:

- stdin closing mid-chat (stream raises EOF) → Abort reply, no exception; stdin raising
  `OSError` (EIO, terminal gone) mid-prompt → Abort reply, no traceback.
- stdout raising BrokenPipeError inside a reply-pending method → Abort reply, no
  traceback; same for `show_chat_reply`.
- stdout raising BrokenPipeError inside `summary` after a successful run → swallowed, no
  traceback, and the run's exit code is unchanged.
- stderr raising BrokenPipeError inside `error` → swallowed, the mapped exit code is
  unchanged; stdout failing inside `notice` → swallowed; stdout failing inside a
  `show_chat_reply(prompt=None)` render → swallowed, returns `None`, no traceback (the
  void-class rule — this test lives here because its trigger is the stream failing).
- stdout raising BrokenPipeError inside `progress` → swallowed, no traceback, no effect on
  the run (same void-class rule as `notice`).
- stdout raising BrokenPipeError inside `_finalize_progress_line` (T4.6), triggered from
  within a reply-pending method (`present_checkpoint`) → the finalize failure is swallowed;
  the method's own subsequent write still runs and its own success/failure (not the
  finalize's) is what determines the reply.
